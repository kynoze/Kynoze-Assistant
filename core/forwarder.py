# Phase 1 engine: resume, rotation, FloodWait, progress updates, no album grouping.

from __future__ import annotations

import asyncio
import logging
from typing import (
    Any,
    AsyncGenerator,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
    Union,
)

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import (
    AuthKeyUnregistered,
    FloodWait,
    SessionRevoked,
    SlowmodeWait,
    UserDeactivated,
)
from pyrogram.types import Message

from database import (
    AccountStatus,
    JobStatus,
    increment_account_forwarded,
    increment_bot_forwarded,
    increment_stats,
    set_job_status,
    update_job,
    update_job_stats,
)
from core.anti_duplicate import is_target_duplicate, mark_target_forwarded
from database import is_job_preindex_duplicate, mark_job_preindex_id
from core.filters import get_unique_file_id
from core.caption import build_inline_keyboard, process_caption
from core.filters import should_process_message

logger = logging.getLogger(__name__)

FLOODWAIT_ROTATE_AFTER = 30
PAUSE_REASON_ACCOUNTS = "accounts_unavailable"
PROGRESS_EVERY = 10  # update progress message every N fetched msgs


async def _pause_for_accounts(user_id: int, job_id: str, detail: str):
    from database import get_job
    fresh = await get_job(user_id, job_id) or {}
    if (fresh.get("status") or "").lower() == JobStatus.PAUSED.value and (
        fresh.get("pause_reason") or ""
    ) == "user":
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
    detail_l = (detail or "").lower()
    if "sleep" in detail_l or "will auto-resume" in detail_l:
        return
    try:
        from core.log_chat import report_user_auto_stop
        job = await get_job(user_id, job_id) or {}
        await report_user_auto_stop(
            user_id,
            feature="Jobs",
            title=job.get("name") or job_id,
            reason="Job automatically paused during forwarding — no usable user account.",
            error=detail,
        )
    except Exception:
        logger.exception("log-chat forwarder pause")


class ForwardStats:
    def __init__(self):
        self.fetched = 0
        self.forwarded = 0
        self.skipped_deleted = 0
        self.skipped_filter = 0
        self.skipped_duplicate = 0
        self.errors = 0

    def summary(self) -> str:
        return (
            f"Fetched: `{self.fetched}`\n"
            f"Forwarded: `{self.forwarded}`\n"
            f"Skipped (filter): `{self.skipped_filter}`\n"
            f"Skipped (deleted): `{self.skipped_deleted}`\n"
            f"Duplicates: `{self.skipped_duplicate}`\n"
            f"Errors: `{self.errors}`"
        )


def _qf_progress_keyboard():
    """Inline controls for Quick Forward progress message."""
    from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏸ Pause", callback_data="qf:pause"),
            InlineKeyboardButton("▶️ Resume", callback_data="qf:resume"),
        ],
        [
            InlineKeyboardButton("⏹ Cancel", callback_data="qf:cancel"),
            InlineKeyboardButton("🔄 Refresh", callback_data="qf:refresh"),
        ],
    ])


async def _edit_progress(
    progress_message: Optional[Message],
    text: str,
    reply_markup=None,
):
    if not progress_message:
        return
    try:
        kwargs = {}
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        await progress_message.edit_text(text, **kwargs)
    except Exception:
        pass


async def custom_iter_messages(
    client: Client,
    chat_id: Union[int, str],
    limit: int,
    offset: int = 0,
) -> AsyncGenerator[Message, None]:
    """
    Iterate message IDs (offset+1) .. limit inclusive.
    Batch errors are logged and the batch is skipped (not the whole job).
    """
    current = offset
    consecutive_failures = 0
    while current < limit:
        batch_size = min(200, limit - current)
        message_ids = list(range(current + 1, current + batch_size + 1))
        try:
            messages = await client.get_messages(chat_id, message_ids)
            consecutive_failures = 0
        except FloodWait as e:
            await asyncio.sleep(int(e.value) + 1)
            continue
        except Exception as e:
            consecutive_failures += 1
            logger.exception(
                "get_messages failed at id~%s (failure %s): %s",
                current + 1,
                consecutive_failures,
                e,
            )
            # Skip this batch so one bad window does not kill the whole job
            current += batch_size
            if consecutive_failures >= 5:
                logger.error("Too many get_messages failures — stopping iteration")
                return
            await asyncio.sleep(0.5)
            continue

        if not isinstance(messages, list):
            messages = [messages]

        for msg in messages:
            current += 1
            if msg is None:
                continue
            yield msg


async def _send_text(
    client: Client,
    target_chat_id,
    text: str,
    reply_markup,
    use_rich: bool,
):
    """
    Text posts: optional Telegram Rich Message (Bot API 10.1 / Kurigram send_rich_message).
    Media captions stay classic (1024 limit) — rich messages are a separate message type.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("MESSAGE_EMPTY: nothing to send")
    if use_rich:
        try:
            from pyrogram.types import InputRichMessage

            await client.send_rich_message(
                chat_id=target_chat_id,
                rich_message=InputRichMessage(html=text),
                reply_markup=reply_markup,
            )
            return
        except Exception:
            logger.debug("send_rich_message failed — falling back to send_message", exc_info=True)
    try:
        from pyrogram.types import LinkPreviewOptions
        _lp = {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
    except Exception:
        _lp = {"link_preview_options": {"is_disabled": True}}
    await client.send_message(
        chat_id=target_chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
        **_lp,
    )


async def send_one(
    client: Client,
    source_chat_id,
    target_chat_id,
    message: Message,
    final_caption,
    reply_markup,
    forward_tag: bool,
    use_rich_message: bool = False,
):
    if forward_tag:
        await client.forward_messages(
            chat_id=target_chat_id,
            from_chat_id=source_chat_id,
            message_ids=message.id,
        )
        return

    if message.media:
        media = getattr(message, message.media.value, None)
        # Media captions remain classic HTML (Telegram 1024-char caption limit).
        if media and hasattr(media, "file_id"):
            try:
                await client.send_cached_media(
                    chat_id=target_chat_id,
                    file_id=media.file_id,
                    caption=final_caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )
                return
            except Exception:
                logger.debug("send_cached_media failed, trying copy_message", exc_info=True)

        await client.copy_message(
            chat_id=target_chat_id,
            from_chat_id=source_chat_id,
            message_id=message.id,
            caption=final_caption,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
        return

    text = final_caption if final_caption is not None else (message.text or message.caption or "")
    text = (text or "").strip()
    if not text:
        # service/empty posts — do not send, not an account-limit count
        return
    await _send_text(
        client, target_chat_id, text, reply_markup, use_rich=use_rich_message
    )


async def forward_messages(
    client: Client,
    user_id: int,
    source_chat_id: Union[int, str],
    target: Dict[str, Any],
    last_msg_id: int,
    skip: int = 0,
    progress_message: Optional[Message] = None,
    cancel_flag: Optional[Dict] = None,
    job_id: Optional[str] = None,
    account_id: Optional[str] = None,
    account_ids: Optional[List[str]] = None,
    strategy: str = "sequential",
    get_new_client_callback: Optional[
        Callable[
            [int, List[str], str],
            Awaitable[Tuple[Optional[Client], Optional[str]]],
        ]
    ] = None,
    allow_empty_range: bool = False,
    op_filters: Optional[Dict[str, Any]] = None,
    pause_flag: Optional[Dict] = None,
    auto_progress: bool = True,
    bot_id: Optional[str] = None,
):
    from core.op_filters import merge_settings_for_forward
    settings = merge_settings_for_forward(target.get("settings", {}) or {}, op_filters)
    target_chat_id = target["chat_id"]
    delay = float(settings.get("delay", 1.0) or 0)
    forward_tag = bool(settings.get("forward_tag", False))
    anti_dup = settings.get("anti_duplicate", True)
    job_preindex_enabled = False
    if job_id:
        try:
            from database import get_job_by_id
            _j = await get_job_by_id(job_id)
            job_preindex_enabled = bool((_j or {}).get("pre_index_target_duplicates"))
        except Exception:
            job_preindex_enabled = False

    stats = ForwardStats()
    CANCEL = cancel_flag or {}
    PAUSE = pause_flag or {}
    # Keep QF control buttons on progress edits when auto_progress is off
    progress_kb = None if auto_progress else _qf_progress_keyboard()
    # Throttle expensive Mongo reads/writes (old clone.py had none per-skip)
    JOB_DB_EVERY = 25  # messages between job status/stats DB hits
    _since_db = 0
    _pending_stats = {"fetched": 0, "forwarded": 0, "errors": 0,
                     "skipped_filter": 0, "skipped_deleted": 0, "skipped_duplicate": 0}
    _last_cursor = skip

    async def _flush_job_stats(force: bool = False):
        nonlocal _since_db, _pending_stats, _last_cursor
        if not job_id:
            return
        if not force and _since_db < JOB_DB_EVERY:
            return
        delta = {k: v for k, v in _pending_stats.items() if v}
        if not delta and not force:
            _since_db = 0
            return
        try:
            await update_job_stats(
                user_id, job_id, delta or {"fetched": 0}, current_msg_id=_last_cursor
            )
        except Exception:
            pass
        for k in _pending_stats:
            _pending_stats[k] = 0
        _since_db = 0


    current_client = client
    current_account_id = account_id

    title = target.get("title") or str(target_chat_id)
    try:
        from handlers.source_handler import QF_PROGRESS
        if not auto_progress:
            QF_PROGRESS[user_id] = {
                "status": "running",
                "title": title,
                "last_msg_id": last_msg_id,
                "skip": skip,
                "current_id": skip,
                "stats": {
                    "fetched": 0, "forwarded": 0,
                    "skipped_filter": 0, "skipped_duplicate": 0,
                    "skipped_deleted": 0, "errors": 0,
                },
            }
    except Exception:
        pass

    if last_msg_id <= skip and not allow_empty_range:
        await _edit_progress(
            progress_message,
            f"**Nothing to forward**\n\n"
            f"Skip `{skip}` >= last `{last_msg_id}`.\n"
            f"Target: {title}",
            reply_markup=progress_kb,
        )
        return stats

    await _edit_progress(
        progress_message,
        f"**Forwarding…**\n\n"
        f"Target: **{title}**\n"
        f"Range: `{skip + 1}` → `{last_msg_id}`\n"
        f"Send `cancel` to stop.",
        reply_markup=progress_kb,
    )

    try:
        async for message in custom_iter_messages(
            current_client, source_chat_id, limit=last_msg_id, offset=skip
        ):
            while PAUSE.get(user_id) and not CANCEL.get(user_id):
                await asyncio.sleep(0.5)
                if job_id:
                    from database import get_job
                    fresh = await get_job(user_id, job_id)
                    if not fresh or fresh.get("status") != JobStatus.RUNNING.value:
                        return stats

            if CANCEL.get(user_id):
                if job_id:
                    await set_job_status(user_id, job_id, JobStatus.CANCELLED.value)
                await _edit_progress(
                    progress_message,
                    f"**Cancelled**\n\n{stats.summary()}",
                    reply_markup=None,
                )
                try:
                    from handlers.source_handler import QF_PROGRESS
                    if user_id in QF_PROGRESS and not auto_progress:
                        QF_PROGRESS[user_id]["status"] = "cancelled"
                except Exception:
                    pass
                return stats

            # Cheap cancel flag check every message; Mongo job status every N
            if job_id and _since_db == 0:
                from database import get_job
                fresh = await get_job(user_id, job_id)
                if not fresh or fresh.get("status") != JobStatus.RUNNING.value:
                    await _flush_job_stats(force=True)
                    return stats

            stats.fetched += 1
            _since_db += 1
            _last_cursor = message.id
            _pending_stats["fetched"] += 1

            try:
                from handlers.source_handler import QF_PROGRESS
                if user_id in QF_PROGRESS and not auto_progress:
                    QF_PROGRESS[user_id]["current_id"] = message.id
                    QF_PROGRESS[user_id]["stats"] = {
                        "fetched": stats.fetched,
                        "forwarded": stats.forwarded,
                        "skipped_filter": stats.skipped_filter,
                        "skipped_duplicate": stats.skipped_duplicate,
                        "skipped_deleted": stats.skipped_deleted,
                        "errors": stats.errors,
                    }
                    if PAUSE.get(user_id):
                        QF_PROGRESS[user_id]["status"] = "paused"
                    elif CANCEL.get(user_id):
                        QF_PROGRESS[user_id]["status"] = "cancelled"
                    else:
                        QF_PROGRESS[user_id]["status"] = "running"
            except Exception:
                pass

            should, reason = should_process_message(message, settings)
            if not should:
                if reason == "deleted":
                    stats.skipped_deleted += 1
                    _pending_stats["skipped_deleted"] += 1
                else:
                    stats.skipped_filter += 1
                    _pending_stats["skipped_filter"] += 1
                # No per-skip Mongo write — flush every JOB_DB_EVERY (fast skip like old clone)
                await _flush_job_stats(force=False)
                if auto_progress and progress_message and stats.fetched % (PROGRESS_EVERY * 5) == 0:
                    await _edit_progress(
                        progress_message,
                        f"**Forwarding…** `{message.id}` / `{last_msg_id}`\n\n{stats.summary()}",
                        reply_markup=progress_kb,
                    )
                continue

            # Order: filter already done → pre-index check → target anti-dupe CHECK
            # → send → only then MARK target + job index (no premature claim)
            is_dup = False
            if job_id and job_preindex_enabled:
                uid = get_unique_file_id(message)
                if uid and await is_job_preindex_duplicate(
                    job_id, uid, target_chat_id=target_chat_id
                ):
                    is_dup = True
            if not is_dup:
                is_dup = await is_target_duplicate(
                    user_id=user_id,
                    target_chat_id=target_chat_id,
                    message=message,
                    anti_duplicate_enabled=anti_dup,
                )
            if is_dup:
                stats.skipped_duplicate += 1
                if job_id:
                    await update_job_stats(
                        user_id,
                        job_id,
                        {"fetched": 1, "skipped_duplicate": 1},
                        current_msg_id=message.id,
                    )
                continue

            final_caption = process_caption(message, settings)
            reply_markup = build_inline_keyboard(settings)

            try:
                await send_one(
                    current_client,
                    source_chat_id,
                    target_chat_id,
                    message,
                    final_caption,
                    reply_markup,
                    forward_tag,
                    use_rich_message=bool(settings.get("rich_message_enabled")),
                )
                stats.forwarded += 1

                # Claim only after successful send
                try:
                    await mark_target_forwarded(
                        user_id=user_id,
                        target_chat_id=target_chat_id,
                        message=message,
                        anti_duplicate_enabled=anti_dup,
                    )
                except Exception:
                    pass
                if job_id and job_preindex_enabled:
                    try:
                        _uid = get_unique_file_id(message)
                        if _uid:
                            await mark_job_preindex_id(
                                job_id, _uid,
                                target_chat_id=target_chat_id,
                                source_msg_id=message.id,
                            )
                    except Exception:
                        pass

                # CRITICAL: advance cursor BEFORE account sleep/rotate/pause.
                # Otherwise resume re-sends the last successful message (duplicate).
                # record_job_forward_tick also updates accurate Current/Avg/Peak speed.
                if job_id:
                    try:
                        from database import record_job_forward_tick
                        await record_job_forward_tick(
                            user_id,
                            job_id,
                            current_msg_id=message.id,
                            forwarded_delta=1,
                            fetched_delta=1,
                        )
                        # already counted in DB — don't re-flush as pending fetched
                        _since_db = max(0, _since_db - 1)
                        _last_cursor = message.id
                    except Exception:
                        await update_job_stats(
                            user_id,
                            job_id,
                            {"fetched": 1, "forwarded": 1},
                            current_msg_id=message.id,
                        )

                if current_account_id:
                    updated = await increment_account_forwarded(
                        user_id, current_account_id, 1
                    )
                    if (
                        updated
                        and updated.get("status") == AccountStatus.SLEEPING.value
                    ):
                        logger.info(
                            "Account %s hit limit — rotating", current_account_id
                        )
                        if get_new_client_callback and account_ids:
                            new_client, new_acc_id = await get_new_client_callback(
                                user_id, account_ids, strategy
                            )
                            if new_client and new_acc_id:
                                current_client = new_client
                                current_account_id = new_acc_id
                            else:
                                if job_id:
                                    await _pause_for_accounts(
                                        user_id,
                                        job_id,
                                        "All accounts sleeping — will auto-resume",
                                    )
                                await _edit_progress(
                                    progress_message,
                                    f"**Paused** — all accounts sleeping\n\n{stats.summary()}",
                                    reply_markup=progress_kb,
                                )
                                return stats
                await increment_stats(
                    user_id, "target", str(target_chat_id), {"forwarded": 1}
                )
                if current_account_id:
                    await increment_stats(
                        user_id, "account", current_account_id, {"forwarded": 1}
                    )
                if bot_id:
                    try:
                        await increment_bot_forwarded(user_id, bot_id, 1)
                    except Exception:
                        pass
                    try:
                        await increment_stats(
                            user_id, "bot", str(bot_id), {"forwarded": 1}
                        )
                    except Exception:
                        pass

            except (FloodWait, SlowmodeWait) as e:
                wait = int(getattr(e, "value", 0) or 0)
                logger.warning(
                    "FloodWait %ss on account %s", wait, current_account_id
                )
                if (
                    wait >= FLOODWAIT_ROTATE_AFTER
                    and get_new_client_callback
                    and account_ids
                ):
                    new_client, new_acc_id = await get_new_client_callback(
                        user_id, account_ids, strategy
                    )
                    if new_client and new_acc_id:
                        current_client = new_client
                        current_account_id = new_acc_id
                        logger.info("Rotated after FloodWait → %s", new_acc_id)
                        continue
                await asyncio.sleep(wait + 1)
                continue

            except (UserDeactivated, AuthKeyUnregistered, SessionRevoked) as e:
                logger.error("Account dead %s: %s", current_account_id, e)
                if get_new_client_callback and account_ids:
                    new_client, new_acc_id = await get_new_client_callback(
                        user_id, account_ids, strategy
                    )
                    if new_client and new_acc_id:
                        current_client = new_client
                        current_account_id = new_acc_id
                        continue
                if job_id:
                    await _pause_for_accounts(user_id, job_id, f"Account error: {e}")
                await _edit_progress(
                    progress_message,
                    f"**Paused** — account error\n\n{stats.summary()}",
                    reply_markup=progress_kb,
                )
                return stats

            except Exception as e:
                logger.exception("Error on message %s: %s", getattr(message, "id", "?"), e)
                stats.errors += 1
                if job_id:
                    await update_job_stats(
                        user_id, job_id, {"errors": 1}, current_msg_id=message.id
                    )
                continue

            if auto_progress and progress_message and (
                stats.forwarded % PROGRESS_EVERY == 0 or stats.fetched % PROGRESS_EVERY == 0
            ):
                await _edit_progress(
                    progress_message,
                    f"**Forwarding…** `{message.id}` / `{last_msg_id}`\n\n{stats.summary()}",
                    reply_markup=progress_kb,
                )

            if delay > 0:
                await asyncio.sleep(delay)

    except Exception as e:
        logger.exception("Forwarder crashed")
        if job_id:
            await set_job_status(user_id, job_id, JobStatus.FAILED.value, str(e))
            try:
                from core.log_chat import report_user_auto_stop
                from database import get_job
                job = await get_job(user_id, job_id) or {}
                await report_user_auto_stop(
                    user_id,
                    feature="Jobs",
                    title=job.get("name") or job_id,
                    reason="Forwarder crashed. Job marked FAILED.",
                    error=f"{type(e).__name__}: {e}",
                )
            except Exception:
                pass
        await _edit_progress(
            progress_message,
            f"**Failed**\n\n{stats.summary()}\n\nError: `{e}`",
            reply_markup=None,
        )
        raise

    await _edit_progress(
        progress_message,
        f"**Done** — {title}\n\n{stats.summary()}",
        reply_markup=None,
    )
    return stats
