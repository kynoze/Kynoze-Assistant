"""Jobs Log Channel — one progress message per job in user's log channel.

Create message ONLY on explicit user action (Send Log Message).
Auto-update is independent from the normal progress_ui binding.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from pyrogram import Client
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

# per-job locks to prevent double-send race
_LOG_LOCKS: Dict[str, asyncio.Lock] = {}


def _lock_for(job_id: str) -> asyncio.Lock:
    if job_id not in _LOG_LOCKS:
        _LOG_LOCKS[job_id] = asyncio.Lock()
    return _LOG_LOCKS[job_id]


def log_channel_keyboard(job: dict) -> InlineKeyboardMarkup:
    """Limited controls for Jobs Log Channel message."""
    job_id = job["job_id"]
    status = (job.get("status") or "").lower()
    rows = []

    terminal = status in ("completed", "cancelled", "failed")
    if terminal:
        rows.append([
            InlineKeyboardButton("🔄 Refresh", callback_data=f"jlog:refresh:{job_id}"),
        ])
        return InlineKeyboardMarkup(rows)

    if status == "running":
        rows.append([
            InlineKeyboardButton("⏸ Pause", callback_data=f"jlog:pause:{job_id}"),
            InlineKeyboardButton("🔄 Refresh", callback_data=f"jlog:refresh:{job_id}"),
        ])
    elif status in ("paused", "pending"):
        # hide Start when waiting for accounts (auto-resume)
        pr = (job.get("pause_reason") or "").strip()
        if pr == "accounts_unavailable":
            rows.append([
                InlineKeyboardButton("⏳ Waiting accounts", callback_data="jlog:noop"),
                InlineKeyboardButton("🔄 Refresh", callback_data=f"jlog:refresh:{job_id}"),
            ])
        else:
            rows.append([
                InlineKeyboardButton("▶️ Start", callback_data=f"jlog:start:{job_id}"),
                InlineKeyboardButton("🔄 Refresh", callback_data=f"jlog:refresh:{job_id}"),
            ])
    else:
        rows.append([
            InlineKeyboardButton("🔄 Refresh", callback_data=f"jlog:refresh:{job_id}"),
        ])

    if status in ("running", "paused", "pending", "indexing"):
        rows.append([
            InlineKeyboardButton("⏹ Stop", callback_data=f"jlog:stop:{job_id}"),
            InlineKeyboardButton("⏱ Auto Update", callback_data=f"jlog:pui:{job_id}"),
        ])
    return InlineKeyboardMarkup(rows)


def format_log_progress_text(job: dict) -> str:
    """Reuse job_detail_text content style — compact for channel."""
    from handlers.jobs_handlers import job_detail_text
    try:
        return job_detail_text(job)
    except Exception:
        logger.exception("format_log_progress_text")
        status = job.get("status") or "?"
        stats = job.get("stats") or {}
        return (
            f"**JOB** `{str(job.get('job_id') or '')[:8]}`\n"
            f"Status: `{status}`\n"
            f"Forwarded: `{stats.get('forwarded', 0)}`\n"
        )


async def message_still_exists(client: Client, chat_id: int, message_id: int) -> bool:
    try:
        await client.get_messages(chat_id, message_id)
        return True
    except Exception:
        # some pyrogram builds return empty
        try:
            msg = await client.get_messages(chat_id, message_id)
            if msg is None:
                return False
            if getattr(msg, "empty", False):
                return False
            return True
        except Exception:
            return False


async def edit_log_message(client: Client, job: dict) -> bool:
    """Edit existing log message. Clear binding if deleted."""
    chat_id = job.get("log_progress_chat_id")
    msg_id = job.get("log_progress_message_id")
    if not chat_id or not msg_id:
        return False
    user_id = job["user_id"]
    job_id = job["job_id"]
    text = format_log_progress_text(job)
    kb = log_channel_keyboard(job)
    try:
        await client.edit_message_text(
            int(chat_id),
            int(msg_id),
            text,
            reply_markup=kb,
        )
        from database import update_job
        await update_job(
            user_id,
            job_id,
            {"log_progress_last_at": datetime.now(timezone.utc)},
        )
        return True
    except Exception as e:
        name = type(e).__name__
        if name in ("MessageNotModified",):
            return True
        if name in (
            "MessageIdInvalid",
            "MessageDeleteForbidden",
            "ChannelInvalid",
            "ChatAdminRequired",
            "PeerIdInvalid",
        ) or "MESSAGE_ID_INVALID" in str(e).upper() or "not found" in str(e).lower():
            from database import clear_job_log_binding
            await clear_job_log_binding(user_id, job_id)
            logger.info("Cleared stale log binding job=%s (%s)", job_id, name)
            return False
        logger.warning("edit_log_message job=%s: %s", job_id, name)
        return False


async def send_or_reuse_log_message(client: Client, user_id: int, job_id: str) -> Tuple[str, bool]:
    """
    Explicit Send Log Message.
    Returns (user_message, ok).
    """
    from database import (
        get_job,
        get_jobs_log_channel,
        try_bind_job_log_message,
        clear_job_log_binding,
        update_job,
    )

    async with _lock_for(job_id):
        job = await get_job(user_id, job_id)
        if not job:
            return "Job not found.", False

        cfg = await get_jobs_log_channel(user_id)
        log_chat = cfg.get("chat_id")
        if not log_chat:
            return "Jobs Log Channel is not configured.", False

        # Existing binding?
        existing_chat = job.get("log_progress_chat_id")
        existing_msg = job.get("log_progress_message_id")
        if existing_chat and existing_msg:
            if await message_still_exists(client, int(existing_chat), int(existing_msg)):
                # refresh content
                job = await get_job(user_id, job_id) or job
                await edit_log_message(client, job)
                return "Job log progress message is already active.", True
            # stale
            await clear_job_log_binding(user_id, job_id)

        # Send one new message
        job = await get_job(user_id, job_id) or job
        text = format_log_progress_text(job)
        kb = log_channel_keyboard(job)
        try:
            msg = await client.send_message(
                int(log_chat),
                text,
                reply_markup=kb,
            )
        except Exception as e:
            return f"Failed to send log message: {type(e).__name__}", False

        bound = await try_bind_job_log_message(
            user_id, job_id, int(log_chat), int(msg.id)
        )
        if not bound:
            # race: another callback already bound — delete ours to keep one message
            try:
                await client.delete_messages(int(log_chat), int(msg.id))
            except Exception:
                pass
            return "Job log progress message is already active.", True

        return "Log message sent to Jobs Log Channel.", True


async def jobs_log_refresh_loop(app: Client):
    """Periodic update for log channel messages (independent of progress_ui)."""
    from database import get_jobs_with_log_auto_update, get_job, job_log_progress_interval

    logger.info("Jobs Log Channel refresh loop started")
    while True:
        try:
            await asyncio.sleep(15)
            jobs = await get_jobs_with_log_auto_update()
            now = datetime.now(timezone.utc)
            for job in jobs or []:
                try:
                    user_id = job["user_id"]
                    job_id = job["job_id"]
                    if not job.get("log_progress_auto_update_enabled"):
                        continue
                    if not job.get("log_progress_message_id"):
                        continue
                    interval = job_log_progress_interval(job)
                    last = job.get("log_progress_last_at")
                    if last is not None:
                        if getattr(last, "tzinfo", None) is None:
                            last = last.replace(tzinfo=timezone.utc)
                        age = (now - last).total_seconds()
                        if age < interval:
                            continue
                    fresh = await get_job(user_id, job_id) or job
                    status = (fresh.get("status") or "").lower()
                    # terminal: one final edit then disable auto
                    if status in ("completed", "cancelled", "failed"):
                        await edit_log_message(app, fresh)
                        from database import update_job
                        await update_job(
                            user_id,
                            job_id,
                            {"log_progress_auto_update_enabled": False},
                        )
                        continue
                    await edit_log_message(app, fresh)
                except Exception:
                    logger.exception("jobs_log refresh one job")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            from core.errors import is_mongo_unreachable
            if is_mongo_unreachable(e):
                logger.warning("jobs_log: MongoDB unreachable — reconnect + backoff")
                try:
                    from database import db
                    await db.ensure_connected()
                except Exception:
                    pass
                await asyncio.sleep(30)
                continue
            logger.exception("jobs_log_refresh_loop")
