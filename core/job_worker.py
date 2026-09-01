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
    job_progress_ui_interval,
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


async def pause_running_job(user_id: int, job_id: str, reason: str = "user") -> bool:
    """User (or system) pause: persist PAUSED and cancel in-memory worker task."""
    try:
        await update_job(
            user_id,
            job_id,
            {
                "status": JobStatus.PAUSED.value,
                "pause_reason": reason,
                "error_message": None if reason == "user" else None,
            },
        )
    except Exception:
        logger.exception("pause_running_job status update failed for %s", job_id)
    task = RUNNING_JOB_TASKS.pop(job_id, None)
    if task and not task.done():
        task.cancel()
        return True
    return False


async def pause_job_for_accounts(user_id: int, job_id: str, detail: str):
    fresh = await get_job(user_id, job_id) or {}
    # Never override intentional user pause
    if (fresh.get("status") or "").lower() == JobStatus.PAUSED.value and (
        fresh.get("pause_reason") or ""
    ) == "user":
        task = RUNNING_JOB_TASKS.pop(job_id, None)
        if task and not task.done():
            task.cancel()
        return
    await update_job(
        user_id,
        job_id,
        {
            "status": JobStatus.PAUSED.value,
            "pause_reason": PAUSE_REASON_ACCOUNTS,
            "error_message": detail,
        },
    )
    task = RUNNING_JOB_TASKS.pop(job_id, None)
    if task and not task.done():
        task.cancel()
    detail_l = (detail or "").lower()
    # Expected sleep auto-pause — no log-chat spam
    if "sleep" in detail_l or "will auto-resume" in detail_l:
        return
    try:
        from core.log_chat import report_user_auto_stop
        job = await get_job(user_id, job_id) or {}
        await report_user_auto_stop(
            user_id,
            feature="Jobs",
            title=job.get("name") or job_id,
            reason="Job automatically paused — no usable user account (disabled / session dead).",
            error=detail,
        )
    except Exception:
        logger.exception("log-chat job pause report")


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




async def progress_ui_refresh_loop(app: Client):
    """Edit bound job progress messages on user interval (1m–1d, default 5m)."""
    from datetime import datetime, timezone
    from handlers.jobs_handlers import job_detail_text, job_controls_keyboard
    while True:
        try:
            await asyncio.sleep(60)
            now = datetime.now(timezone.utc)
            try:
                jobs = await db.forward_jobs.find({
                    "status": JobStatus.RUNNING.value,
                    "progress_message_id": {"$ne": None},
                    "progress_chat_id": {"$ne": None},
                }).to_list(200)
            except Exception:
                continue
            for job in jobs:
                try:
                    interval = job_progress_ui_interval(job)
                    last = job.get("progress_ui_last_at")
                    if last is not None and getattr(last, "tzinfo", None) is None:
                        last = last.replace(tzinfo=timezone.utc)
                    if last and (now - last).total_seconds() < interval:
                        continue
                    # first bind: wait full interval unless never refreshed
                    bound = job.get("progress_ui_bound_at")
                    if last is None and bound is not None:
                        if getattr(bound, "tzinfo", None) is None:
                            bound = bound.replace(tzinfo=timezone.utc)
                        if (now - bound).total_seconds() < interval:
                            continue
                    chat_id = job.get("progress_chat_id")
                    msg_id = job.get("progress_message_id")
                    user_id = job.get("user_id")
                    job_id = job.get("job_id")
                    if not chat_id or not msg_id or not user_id or not job_id:
                        continue
                    fresh = await get_job(user_id, job_id) or job
                    if (fresh.get("status") or "").lower() != JobStatus.RUNNING.value:
                        continue
                    text = job_detail_text(fresh)
                    from handlers.ui import fmt_interval
                    text += (
                        f"\n\n⏱ **Progress auto-update:** "
                        f"`{fmt_interval(job_progress_ui_interval(fresh))}` "
                        f"(auto · 30m–1d)"
                    )
                    try:
                        await app.edit_message_text(
                            chat_id,
                            int(msg_id),
                            text,
                            reply_markup=job_controls_keyboard(fresh),
                        )
                    except Exception as e:
                        # message deleted / not modified — unbind on hard failure
                        err = str(e).lower()
                        if "message" in err and ("not" in err or "modify" in err or "id" in err):
                            await update_job(user_id, job_id, {
                                "progress_message_id": None,
                                "progress_chat_id": None,
                            })
                        continue
                    await update_job(user_id, job_id, {"progress_ui_last_at": now})
                except Exception:
                    logger.exception("progress_ui refresh one job")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("progress_ui_refresh_loop")

async def job_worker_loop(_management_client=None):
    logger.info("Job worker started (selected bot / user accounts only).")
    try:
        active = await get_active_jobs()
        running_n = sum(
            1 for j in (active or [])
            if (j.get("status") or "").lower() == JobStatus.RUNNING.value
        )
        if running_n:
            logger.info(
                "Boot: %s job(s) still RUNNING in DB — will resume automatically",
                running_n,
            )
    except Exception:
        logger.exception("Boot job inventory failed")
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
    """Next account in job order: 1→2→3→4→1… Skip sleeping/unavailable."""
    await wake_sleeping_accounts(user_id)
    from database import get_account
    from database import AccountStatus

    ids = [str(a) for a in (account_ids or [])]
    if not ids:
        return None, None

    # sequential cycle after current
    if exclude_id and str(exclude_id) in ids:
        i = ids.index(str(exclude_id))
        ordered = ids[i + 1 :] + ids[:i]
    else:
        ordered = list(ids)

    # if strategy ever non-sequential, still prefer ordered list
    for aid in ordered:
        acc = await get_account(user_id, aid)
        if not acc:
            continue
        if acc.get("status") != AccountStatus.ACTIVE.value:
            continue
        client = await get_user_client(acc)
        if client:
            return client, aid
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


async def _bump_progress_cursor(user_id: int, job_id: str, msg_id: int, **extra):
    """Advance current_msg_id / high_water_msg_id upward only (never backwards)."""
    fresh = await get_job(user_id, job_id) or {}
    skip = int(fresh.get("skip") or 0)
    prev = int(fresh.get("current_msg_id") or 0)
    hw = int(fresh.get("high_water_msg_id") or 0)
    mid = int(msg_id or 0)
    new_cur = max(prev, mid, skip)
    new_hw = max(hw, new_cur, skip)
    payload = {"current_msg_id": new_cur, "high_water_msg_id": new_hw}
    payload.update(extra)
    await update_job(user_id, job_id, payload)


def historical_range_complete(job: dict) -> bool:
    """True when every target has finished the historical msg-id range.

    New-post monitoring must only run after this is True.
    """
    targets = list(job.get("target_chat_ids") or [])
    t_idx = int(job.get("current_target_index") or 0)
    last_id = int(job.get("last_msg_id") or 0)
    skip = int(job.get("skip") or 0)
    # No targets or empty range → nothing historical left
    if not targets:
        return True
    if last_id <= skip:
        return True
    # forward_job_range advances current_target_index past the last target when done
    if t_idx >= len(targets):
        return True
    return False




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

        # ── Pre-Index Target Duplicates (optional, before any forward) ──
        if bool(job.get("pre_index_target_duplicates")) and job.get("pre_index_status") != "done":
            from core.job_preindex import preindex_job_targets
            logger.info("Job %s pre-indexing target media…", job_id)
            ok, msg, count = await preindex_job_targets(client, user_id, job)
            if not ok:
                logger.error("Job %s pre-index failed: %s", job_id, msg)
                try:
                    from core.log_chat import report_user_auto_stop
                    await report_user_auto_stop(
                        user_id,
                        feature="Jobs / Pre-Index",
                        title=job.get("name") or job_id,
                        reason="Pre-index of target duplicates failed. Forwarding did not start.",
                        error=msg,
                    )
                except Exception:
                    pass
                return
            logger.info("Job %s pre-index done (%s ids) — starting forward", job_id, count)
            await set_job_status(user_id, job_id, JobStatus.RUNNING.value)
            job = await get_job(user_id, job_id) or job

        async def rotation_cb(uid, ids, strat):
            # Live account list — user can add/remove mid-job
            fresh_job = await get_job(uid, job_id) or {}
            live_ids = list(fresh_job.get("account_ids") or ids or [])
            return await get_new_client_for_rotation(
                uid, live_ids, strat, exclude_id=current_account_id
            )

        targets = job.get("target_chat_ids") or []
        t_idx = int(job.get("current_target_index") or 0)
        # Historical range: run until every target is finished
        if t_idx < len(targets) and last_msg_id > int(job.get("skip") or 0):
            await forward_job_range(
                job=job,
                client=client,
                current_account_id=current_account_id,
                rotation_cb=rotation_cb,
                start_id=int(job.get("skip") or 0),
                end_id=last_msg_id,
            )

        if not await job_still_running(user_id, job_id):
            return

        fresh = await get_job(user_id, job_id) or job
        t_idx = int(fresh.get("current_target_index") or 0)
        targets = fresh.get("target_chat_ids") or targets
        future = bool(fresh.get("future_new_posts"))
        if t_idx < len(targets) and last_msg_id > int(fresh.get("skip") or 0):
            # Still incomplete targets (e.g. paused) — do not mark complete
            return
        if not historical_range_complete(fresh):
            # Historical still open (paused mid-range, etc.) — do not monitor yet
            logger.info(
                "Job %s historical incomplete (t_idx=%s) — skip monitor",
                job_id,
                fresh.get("current_target_index"),
            )
            return

        if not future:
            await set_job_status(user_id, job_id, JobStatus.COMPLETED.value)
            logger.info("Job %s completed (no future monitoring)", job_id)
            try:
                from core.log_chat import report_user_job_complete
                fresh = await get_job(user_id, job_id) or job
                stats = fresh.get("stats") or {}
                st = (
                    f"Fetched: `{stats.get('fetched', 0)}`\n"
                    f"Forwarded: `{stats.get('forwarded', 0)}`\n"
                    f"Errors: `{stats.get('errors', 0)}`"
                )
                await report_user_job_complete(
                    user_id,
                    title=fresh.get("name") or job_id,
                    stats_text=st,
                )
            except Exception:
                logger.exception("job complete log-chat")
            return

        # Historical done + monitoring ON + still RUNNING → log, then live new-post phase
        await _bump_progress_cursor(
            user_id, job_id, int(fresh.get("last_msg_id") or 0),
            job_phase="monitoring",
            current_target_index=len(list(fresh.get("target_chat_ids") or [])),
        )
        try:
            from core.log_chat import report_user_job_complete
            stats = (fresh.get("stats") or {})
            await report_user_job_complete(
                user_id,
                title=fresh.get("name") or job_id,
                stats_text=(
                    f"Historical range **complete**.\n"
                    f"Fetched: `{stats.get('fetched', 0)}` · "
                    f"Forwarded: `{stats.get('forwarded', 0)}`\n"
                    f"Now **listing / monitoring** for new posts."
                ),
                extra="Future New Posts is ON — job stays Running.",
            )
        except Exception:
            logger.exception("historical-complete log-chat")
        logger.info("Job %s historical done — entering future-post monitor", job_id)
        await monitor_future_posts(
            job=fresh,
            client=client,
            current_account_id=current_account_id,
            rotation_cb=rotation_cb,
        )

    except asyncio.CancelledError:
        fresh = await get_job(user_id, job_id) or {}
        pr = (fresh.get("pause_reason") or "")
        st = (fresh.get("status") or "").lower()
        # User pause / account sleep already set PAUSED — do not mark cancelled
        if st == JobStatus.PAUSED.value or pr in ("user", PAUSE_REASON_ACCOUNTS):
            logger.info("Job %s worker stopped (paused reason=%s)", job_id, pr or st)
            RUNNING_JOB_TASKS.pop(job_id, None)
            return
        await set_job_status(user_id, job_id, JobStatus.CANCELLED.value)
        logger.info("Job %s cancelled", job_id)
        if pr not in ("user", "deleted_or_stopped", PAUSE_REASON_ACCOUNTS):
            try:
                from core.log_chat import report_user_auto_stop
                await report_user_auto_stop(
                    user_id,
                    feature="Jobs",
                    title=fresh.get("name") or job_id,
                    reason="Job task was cancelled by the system (not a user Stop tap).",
                    error=pr or "CancelledError",
                )
            except Exception:
                pass
    except Exception as e:
        logger.exception("Job %s crashed", job_id)
        await set_job_status(user_id, job_id, JobStatus.FAILED.value, "Internal error — check logs")
        try:
            from core.log_chat import report_user_auto_stop, report_owner
            await report_user_auto_stop(
                user_id,
                feature="Jobs",
                title=job.get("name") or job_id,
                reason="Job crashed and was marked FAILED.",
                error=f"{type(e).__name__}: {e}",
            )
            await report_owner(
                "ERROR",
                f"Job crashed: {job.get('name') or job_id}",
                f"user={user_id} job={job_id}\n{type(e).__name__}: {e}",
            )
        except Exception:
            pass
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
    """Forward range target-by-target (complete one before the next).

    Progress is tracked with:
      - current_target_index: index into target_chat_ids
      - current_msg_id: last completed source message id *within that target*
    On resume after account sleep, only the active target continues from current_msg_id.
    Finished targets are not restarted from the shared cursor.
    """
    user_id = job["user_id"]
    job_id = job["job_id"]
    source_chat_id = job.get("source_chat_id")
    account_ids = job.get("account_ids") or []
    strategy = job.get("account_strategy", "sequential")
    # window_start: historical job.skip OR future-monitor cursor (start_id)
    window_start = int(start_id) if start_id is not None else int(job.get("skip") or 0)
    base_skip = window_start
    targets = [int(t) for t in (job.get("target_chat_ids") or [])]
    total_forwarded = 0

    if not targets:
        return 0

    # Refresh progress from DB (may have advanced during prior partial run)
    fresh = await get_job(user_id, job_id) or job
    t_idx = int(fresh.get("current_target_index") or 0)
    if t_idx < 0:
        t_idx = 0

    for i in range(t_idx, len(targets)):
        if not await job_still_running(user_id, job_id):
            return total_forwarded

        fresh = await get_job(user_id, job_id) or job
        # Another worker may have moved the index
        db_idx = int(fresh.get("current_target_index") or 0)
        if db_idx > i:
            continue
        if db_idx < i:
            # Align DB to this target
            await update_job(user_id, job_id, {"current_target_index": i})

        target_chat_id = targets[i]
        target = await get_target(user_id, target_chat_id)
        if not target:
            await _bump_progress_cursor(
                user_id, job_id, base_skip, current_target_index=i + 1,
            )
            continue

        # Active target: resume from current_msg_id; ensure index is set
        fresh = await get_job(user_id, job_id) or job
        if int(fresh.get("current_target_index") or 0) != i:
            await update_job(user_id, job_id, {"current_target_index": i})
            msg_skip = base_skip
        else:
            cur = fresh.get("current_msg_id")
            msg_skip = int(cur) if cur is not None else base_skip

        if end_id <= msg_skip:
            # Range already done for this target — advance index, keep watermark high
            await _bump_progress_cursor(
                user_id, job_id, max(end_id, msg_skip, base_skip),
                current_target_index=i + 1,
            )
            continue

        logger.info(
            "Job %s target %s/%s chat=%s resume=%s → %s",
            job_id, i + 1, len(targets), target_chat_id, msg_skip, end_id,
        )

        stats = await forward_messages(
            client=client,
            user_id=user_id,
            source_chat_id=source_chat_id,
            target=target,
            last_msg_id=end_id,
            skip=msg_skip,
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

        # Paused mid-target (accounts sleeping) — keep current_target_index=i
        if not await job_still_running(user_id, job_id):
            return total_forwarded

        # Target fully done → pin watermark at end_id (never drop to base_skip)
        # Next target resumes from base_skip via msg_skip logic, but high_water stays high.
        next_idx = i + 1
        await _bump_progress_cursor(
            user_id, job_id, end_id,
            current_target_index=next_idx,
        )
        logger.info("Job %s finished target %s/%s", job_id, i + 1, len(targets))

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
        # Must stay RUNNING — pause/stop ends monitoring immediately
        if not await job_still_running(user_id, job_id):
            logger.info("Job %s monitor exit — not running", job_id)
            return

        fresh = await get_job(user_id, job_id)
        if not fresh:
            return
        # Historical must be complete (never monitor mid-range)
        if not historical_range_complete(fresh):
            logger.info("Job %s monitor exit — historical incomplete", job_id)
            return
        # Monitoring toggle must stay ON
        if not fresh.get("future_new_posts"):
            await set_job_status(user_id, job_id, JobStatus.COMPLETED.value)
            logger.info("Job %s future posts turned OFF — completed", job_id)
            try:
                from core.log_chat import report_user_job_complete
                await report_user_job_complete(
                    user_id,
                    title=fresh.get("name") or job_id,
                    stats_text="Future monitoring turned OFF — historical range done.",
                )
            except Exception:
                pass
            return

        interval = job_monitor_interval(fresh)
        targets = list(fresh.get("target_chat_ids") or [])
        t_idx = int(fresh.get("current_target_index") or 0)
        skip = int(fresh.get("skip") or 0)
        cursor = int(fresh.get("current_msg_id") or 0)
        hw = int(fresh.get("high_water_msg_id") or 0)
        last_done = int(fresh.get("last_msg_id") or 0)
        # High-water mark: never go backwards into already-finished IDs
        cursor = max(cursor, hw, last_done, skip)
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
            logger.debug("Job %s new posts %s → %s", job_id, cursor + 1, latest)
            # New-post window only — do NOT restart from original skip
            n_targets = len(targets)
            await update_job(
                user_id,
                job_id,
                {
                    "last_msg_id": latest,
                    "current_target_index": 0,
                    "current_msg_id": cursor,
                    "monitor_window_start": cursor,
                    "job_phase": "monitoring",
                },
            )
            fresh = await get_job(user_id, job_id) or fresh
            forwarded_n = await forward_job_range(
                job=fresh,
                client=client,
                current_account_id=current_account_id,
                rotation_cb=rotation_cb,
                start_id=cursor,
                end_id=latest,
            )
            # Window done: park cursor at latest and mark all targets complete
            # (t_idx=0 after a window was wrongly treated as "historical incomplete")
            still = await job_still_running(user_id, job_id)
            if still:
                await _bump_progress_cursor(
                    user_id, job_id, latest,
                    last_msg_id=latest,
                    current_target_index=n_targets if n_targets else 0,
                    monitor_window_start=None,
                    job_phase="monitoring",
                )
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
                    pass  # quiet: no per-batch forward log
                except Exception:
                    pass
            elif still:
                logger.warning(
                    "Job %s detected %s→%s but forwarded 0 (check accounts/filters)",
                    job_id, cursor + 1, latest,
                )
        await asyncio.sleep(interval)


