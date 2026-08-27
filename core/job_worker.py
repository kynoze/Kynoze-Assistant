# Selected Forward Bot is used (NOT the management bot).
# Resume from current_msg_id. Pause/cancel via job status.
# Future New Posts: after historical range, poll source history for newer ids.
# Quick Forward is a separate UI path — this worker only runs Jobs.
# Albums: not implemented (out of scope).

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Dict, Optional, Tuple, List

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import (
    UserDeactivated,
    AuthKeyUnregistered,
    SessionRevoked,
)

from config import Config
from database import (
    db,
    get_active_jobs,
    get_job,
    get_target,
    get_account,
    get_bot,
    get_next_available_account,
    wake_sleeping_accounts,
    set_job_status,
    update_job,
    add_job_log,
    job_monitor_interval,
    JobStatus,
    AccountStatus,
    MethodType,
)
from core.forwarder import forward_messages

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5
FUTURE_POLL_SECONDS = 10  # default; live jobs use job.monitor_interval_seconds

PAUSE_REASON_ACCOUNTS = "accounts_unavailable"
ACCOUNT_PAUSE_MESSAGES = {
    "All accounts sleeping or unavailable",
    "No available user accounts",
    "Could not start user account client",
    "All accounts sleeping — will auto-resume",
}

RUNNING_JOB_TASKS: Dict[str, asyncio.Task] = {}


async def cancel_running_job(user_id: int, job_id: str) -> bool:
    """Mark cancelled and cancel in-memory task so delete/stop leaves no orphan worker."""
    try:
        await update_job(
            user_id,
            job_id,
            {
                "status": JobStatus.CANCELLED.value,
                "pause_reason": "deleted_or_stopped",
            },
        )
    except Exception:
        logger.exception("cancel_running_job status update failed for %s", job_id)
    task = RUNNING_JOB_TASKS.pop(job_id, None)
    if task and not task.done():
        task.cancel()
        return True
    return False
CLIENTS: Dict[str, Client] = {}


async def pause_job_for_accounts(user_id: int, job_id: str, detail: str):
    await update_job(
        user_id,
        job_id,
        {
            "status": JobStatus.PAUSED.value,
            "pause_reason": PAUSE_REASON_ACCOUNTS,
            "error_message": detail,
        },
    )


async def resume_jobs_waiting_on_accounts() -> int:
    resumed = 0
    try:
        paused = await db.forward_jobs.find(
                {
                    "status": JobStatus.PAUSED.value,
                    "method": {"$in": ["user", MethodType.USER.value]},
                }
            ).to_list(length=None)
    except Exception:
        logger.exception("Failed to query paused jobs")
        return 0

    for job in paused:
        reason = job.get("pause_reason")
        msg = job.get("error_message") or ""
        auto = reason == PAUSE_REASON_ACCOUNTS or msg in ACCOUNT_PAUSE_MESSAGES
        if not auto:
            continue

        user_id = job["user_id"]
        account_ids = job.get("account_ids") or []
        available = await get_next_available_account(
            user_id, account_ids, job.get("account_strategy", "sequential")
        )
        if not available:
            continue

        await update_job(
            user_id,
            job["job_id"],
            {
                "status": JobStatus.RUNNING.value,
                "pause_reason": None,
                "error_message": None,
            },
        )
        resumed += 1
        logger.info(
            "Auto-resumed job %s after account wake (account %s)",
            job["job_id"],
            available.get("account_id"),
        )
    return resumed


async def job_worker_loop(_management_client=None):
    logger.info("Job worker started (selected bot / user accounts only).")
    while True:
        try:
            woken = await wake_sleeping_accounts()
            if woken:
                logger.info("Woke %s sleeping account(s)", woken)

            resumed = await resume_jobs_waiting_on_accounts()
            if resumed:
                logger.info("Auto-resumed %s job(s) after account sleep", resumed)

            jobs = await get_active_jobs()
            for job in jobs:
                if job.get("status") != JobStatus.RUNNING.value:
                    continue
                job_id = job["job_id"]
                task = RUNNING_JOB_TASKS.get(job_id)
                if task and not task.done():
                    continue
                RUNNING_JOB_TASKS[job_id] = asyncio.create_task(run_single_job(job))
        except Exception:
            logger.exception("Job worker poll failed")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def get_bot_client(bot_doc: dict) -> Optional[Client]:
    bot_id = bot_doc["bot_id"]
    cached = CLIENTS.get(f"bot:{bot_id}")
    if cached:
        return cached

    token = bot_doc.get("bot_token")
    if not token:
        logger.error("Bot %s has no token", bot_id)
        return None
    try:
        from core.security import decrypt_session
        token = decrypt_session(token)
    except Exception:
        logger.exception("Could not decrypt bot token for %s", bot_id)
        return None

    try:
        client = Client(
            name=f"fwd_bot_{bot_id}",
            api_id=Config.API_ID,
            api_hash=Config.API_HASH,
            bot_token=token,
            in_memory=True,
            parse_mode=ParseMode.HTML,
            no_updates=True,
        )
        await client.start()
        CLIENTS[f"bot:{bot_id}"] = client
        logger.info("Forward bot started: %s (%s)", bot_doc.get("name"), bot_id)
        return client
    except Exception:
        logger.exception("Failed to start forward bot %s", bot_id)
        return None


async def get_user_client(account_doc: dict) -> Optional[Client]:
    acc_id = account_doc["account_id"]
    cached = CLIENTS.get(f"acc:{acc_id}")
    if cached:
        return cached

    session = account_doc.get("session_string")
    if not session:
        return None

    try:
        from core.security import decrypt_session
        session = decrypt_session(session)
    except Exception:
        logger.exception("Could not decrypt session for %s", acc_id)
        return None

    try:
        client = Client(
            name=f"fwd_user_{acc_id}",
            api_id=Config.API_ID,
            api_hash=Config.API_HASH,
            session_string=session,
            in_memory=True,
            parse_mode=ParseMode.HTML,
            no_updates=True,
        )
        await client.start()
        CLIENTS[f"acc:{acc_id}"] = client
        logger.info("User client started: %s", acc_id)
        return client
    except (UserDeactivated, AuthKeyUnregistered, SessionRevoked) as e:
        from database import set_account_status

        logger.error("Account %s session invalid: %s", acc_id, e)
        await set_account_status(
            account_doc["user_id"],
            acc_id,
            AccountStatus.ERROR.value,
            str(e),
        )
        return None
    except Exception:
        logger.exception("Failed to start user client %s", acc_id)
        return None


async def drop_client(key: str):
    client = CLIENTS.pop(key, None)
    if not client:
        return
    try:
        await client.stop()
    except Exception:
        pass


async def get_new_client_for_rotation(
    user_id: int,
    account_ids: List[str],
    strategy: str,
    exclude_id: Optional[str] = None,
) -> Tuple[Optional[Client], Optional[str]]:
    await wake_sleeping_accounts(user_id)
    available = []
    from database import get_available_accounts

    for acc in await get_available_accounts(user_id, account_ids):
        if exclude_id and acc["account_id"] == exclude_id:
            continue
        available.append(acc)

    if not available:
        account = await get_next_available_account(user_id, account_ids, strategy)
        if not account or account.get("account_id") == exclude_id:
            return None, None
        available = [account]

    for account in available:
        client = await get_user_client(account)
        if client:
            return client, account["account_id"]
    return None, None


async def resolve_exec_client(job: dict) -> Tuple[Optional[Client], Optional[str], str]:
    user_id = job["user_id"]
    method = job.get("method")

    if method == MethodType.BOT.value or method == "bot":
        bot = await get_bot(user_id, job.get("bot_id"))
        if not bot or bot.get("status") != "active":
            return None, None, "Selected forward bot is missing or disabled"
        client = await get_bot_client(bot)
        if not client:
            return None, None, "Could not start selected forward bot"
        return client, None, ""

    if method == MethodType.USER.value or method == "user":
        await wake_sleeping_accounts(user_id)
        account_ids = job.get("account_ids") or []
        account = await get_next_available_account(
            user_id, account_ids, job.get("account_strategy", "sequential")
        )
        if not account:
            return None, None, "No available user accounts"
        client = await get_user_client(account)
        if not client:
            return None, None, "Could not start user account client"
        return client, account["account_id"], ""

    return None, None, f"Unknown method: {method}"


async def job_still_running(user_id: int, job_id: str) -> bool:
    fresh = await get_job(user_id, job_id)
    return bool(fresh and fresh.get("status") == JobStatus.RUNNING.value)


async def _probe_latest_after(
    client: Client, source_chat_id, after_id: int, look_ahead: int = 100
) -> Optional[int]:
    """Bot-safe: bots cannot call messages.GetHistory."""
    if after_id < 0:
        after_id = 0
    start = after_id + 1
    ids = list(range(start, start + max(1, look_ahead)))
    try:
        messages = await client.get_messages(source_chat_id, ids)
    except Exception as e:
        logger.warning("Cannot probe source %s after %s: %s", source_chat_id, after_id, e)
        return None
    if not isinstance(messages, list):
        messages = [messages]
    highest = None
    for msg in messages:
        if not msg or getattr(msg, "empty", False):
            continue
        mid = getattr(msg, "id", None)
        if mid is None:
            continue
        mid = int(mid)
        if highest is None or mid > highest:
            highest = mid
    return highest


async def latest_source_message_id(
    client: Client,
    source_chat_id,
    after_id: int = 0,
    method: Optional[str] = None,
) -> Optional[int]:
    """
    Bots cannot call messages.GetHistory (BOT_METHOD_INVALID).
    method == bot  → probe via get_messages only
    method == user → get_chat_history, then probe fallback
    """
    is_bot = (method or "").lower() in ("bot", MethodType.BOT.value)
    if is_bot:
        return await _probe_latest_after(client, source_chat_id, after_id)

    try:
        async for msg in client.get_chat_history(source_chat_id, limit=1):
            if msg and getattr(msg, "id", None):
                return int(msg.id)
    except Exception as e:
        err = str(e)
        if "BOT_METHOD_INVALID" not in err and "GetHistory" not in err:
            logger.warning("Cannot read latest source id for %s: %s", source_chat_id, e)
    return await _probe_latest_after(client, source_chat_id, after_id)


async def run_single_job(job: dict):
    user_id = job["user_id"]
    job_id = job["job_id"]
    source_chat_id = job.get("source_chat_id")
    last_msg_id = int(job.get("last_msg_id") or 0)
    skip = int(job.get("current_msg_id") or job.get("skip") or 0)
    strategy = job.get("account_strategy", "sequential")
    account_ids = job.get("account_ids") or []
    future = bool(job.get("future_new_posts"))

    logger.info(
        "Job %s run | method=%s | resume=%s → last=%s | future=%s",
        job_id,
        job.get("method"),
        skip,
        last_msg_id,
        future,
    )

    current_account_id = None
    rotation_cb = None
    client = None

    try:
        client, current_account_id, err = await resolve_exec_client(job)
        if not client:
            await pause_job_for_accounts(user_id, job_id, err)
            logger.warning("Job %s paused: %s", job_id, err)
            return

        async def rotation_cb(uid, ids, strat):
            return await get_new_client_for_rotation(
                uid, ids, strat, exclude_id=current_account_id
            )

        if last_msg_id > skip:
            await forward_job_range(
                job=job,
                client=client,
                current_account_id=current_account_id,
                rotation_cb=rotation_cb,
                start_id=skip,
                end_id=last_msg_id,
            )

        if not await job_still_running(user_id, job_id):
            return

        fresh = await get_job(user_id, job_id) or job
        future = bool(fresh.get("future_new_posts"))
        if not future:
            await set_job_status(user_id, job_id, JobStatus.COMPLETED.value)
            logger.info("Job %s completed", job_id)
            return

        logger.info("Job %s entering future-post monitor", job_id)
        await monitor_future_posts(
            job=job,
            client=client,
            current_account_id=current_account_id,
            rotation_cb=rotation_cb,
        )

    except asyncio.CancelledError:
        await set_job_status(user_id, job_id, JobStatus.CANCELLED.value)
        logger.info("Job %s cancelled", job_id)
    except Exception as e:
        logger.exception("Job %s crashed", job_id)
        await set_job_status(user_id, job_id, JobStatus.FAILED.value, "Internal error — check logs")
    finally:
        RUNNING_JOB_TASKS.pop(job_id, None)


async def forward_job_range(
    job: dict,
    client: Client,
    current_account_id,
    rotation_cb,
    start_id: int,
    end_id: int,
) -> int:
    """Forward range for all targets. Returns total successfully forwarded count."""
    user_id = job["user_id"]
    job_id = job["job_id"]
    source_chat_id = job.get("source_chat_id")
    account_ids = job.get("account_ids") or []
    strategy = job.get("account_strategy", "sequential")
    total_forwarded = 0

    for target_chat_id in job.get("target_chat_ids") or []:
        if not await job_still_running(user_id, job_id):
            return total_forwarded

        target = await get_target(user_id, target_chat_id)
        if not target:
            continue

        stats = await forward_messages(
            client=client,
            user_id=user_id,
            source_chat_id=source_chat_id,
            target=target,
            last_msg_id=end_id,
            skip=start_id,
            job_id=job_id,
            account_id=current_account_id,
            account_ids=account_ids,
            strategy=strategy,
            get_new_client_callback=rotation_cb if account_ids else None,
            allow_empty_range=True,
            bot_id=job.get("bot_id") if job.get("method") in ("bot", MethodType.BOT.value) else None,
        )
        if stats is not None:
            total_forwarded += int(getattr(stats, "forwarded", 0) or 0)
    return total_forwarded


async def monitor_future_posts(job: dict, client: Client, current_account_id, rotation_cb):
    """
    After historical range:
      latest_id via history (users) or id-probe (bots)
      if latest_id > current_msg_id → forward that window
    Interval is read from MongoDB each loop so UI changes apply live.
    """
    from datetime import datetime, timezone

    user_id = job["user_id"]
    job_id = job["job_id"]
    source_chat_id = job.get("source_chat_id")

    while True:
        if not await job_still_running(user_id, job_id):
            return

        fresh = await get_job(user_id, job_id)
        if not fresh:
            return
        if not fresh.get("future_new_posts"):
            await set_job_status(user_id, job_id, JobStatus.COMPLETED.value)
            logger.info("Job %s future posts turned OFF — completed", job_id)
            return

        interval = job_monitor_interval(fresh)
        cursor = int(fresh.get("current_msg_id") or fresh.get("last_msg_id") or 0)
        method = fresh.get("method")
        latest = await latest_source_message_id(
            client, source_chat_id, after_id=cursor, method=method
        )

        now = datetime.now(timezone.utc)
        monitor_set = {
            "last_monitor_at": now,
            "next_monitor_at": now + timedelta(seconds=interval),
        }
        if latest is not None:
            monitor_set["last_detected_msg_id"] = latest
        await update_job(user_id, job_id, monitor_set)

        if latest is None:
            await asyncio.sleep(interval)
            continue

        if latest > cursor:
            logger.info("Job %s new posts %s → %s", job_id, cursor + 1, latest)
            try:
                await add_job_log(job_id, "info", f"New posts detected {cursor + 1} → {latest}")
            except Exception:
                pass
            await update_job(user_id, job_id, {"last_msg_id": latest})
            forwarded_n = await forward_job_range(
                job=fresh,
                client=client,
                current_account_id=current_account_id,
                rotation_cb=rotation_cb,
                start_id=cursor,
                end_id=latest,
            )
            # Count only successfully forwarded messages, not id delta
            if forwarded_n:
                try:
                    fresh2 = await get_job(user_id, job_id) or fresh
                    await update_job(
                        user_id,
                        job_id,
                        {
                            "new_posts_forwarded": int(fresh2.get("new_posts_forwarded") or 0)
                            + int(forwarded_n),
                        },
                    )
                    await add_job_log(job_id, "info", f"Forwarded {forwarded_n} new post(s)")
                except Exception:
                    pass
        await asyncio.sleep(interval)


