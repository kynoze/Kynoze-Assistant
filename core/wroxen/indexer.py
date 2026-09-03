"""Initial bulk index of source chat media (bot client only)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from pyrogram import Client
from pyrogram.enums import MessageMediaType
from pyrogram.errors import FloodWait, RPCError

from core.wroxen.db import save_media
from core.wroxen.extractor import build_message_link, extract_details

logger = logging.getLogger(__name__)

SUPPORTED = {
    MessageMediaType.VIDEO: "video",
    MessageMediaType.DOCUMENT: "document",
    MessageMediaType.PHOTO: "photo",
    MessageMediaType.AUDIO: "audio",
    MessageMediaType.ANIMATION: "animation",
}

# owner_user_id -> progress
PROGRESS: Dict[int, Dict[str, Any]] = {}
CANCEL: Dict[int, bool] = {}
_LOCKS: Dict[int, asyncio.Lock] = {}


def _lock(uid: int) -> asyncio.Lock:
    if uid not in _LOCKS:
        _LOCKS[uid] = asyncio.Lock()
    return _LOCKS[uid]


def request_cancel(uid: int) -> None:
    CANCEL[uid] = True


def get_progress(uid: int) -> Optional[Dict[str, Any]]:
    return PROGRESS.get(uid)


def is_running(uid: int) -> bool:
    p = PROGRESS.get(uid)
    return bool(p and p.get("status") == "running")


def _media_from_message(message):
    if not message or not message.media or message.media not in SUPPORTED:
        return None
    media_type = SUPPORTED[message.media]
    media = getattr(message, message.media.value, None)
    if message.media == MessageMediaType.PHOTO and message.photo:
        media = message.photo
    if not media:
        return None
    file_uid = getattr(media, "file_unique_id", None)
    if not file_uid:
        return None
    caption = (
        message.caption
        or getattr(media, "file_name", None)
        or ""
    )
    return media_type, media, caption, file_uid


async def index_message_to_db(
    owner_user_id: int,
    wroxen_id: str,
    source_chat_id: int,
    message,
    source_username: Optional[str] = None,
) -> str:
    """Index a single message if it has supported media. Returns save result or 'skip'."""
    extracted = _media_from_message(message)
    if not extracted:
        return "skip"
    media_type, media, caption, file_uid = extracted
    if not caption:
        return "skip"
    details = extract_details(caption or "")
    link = getattr(message, "link", None) or build_message_link(
        source_chat_id, message.id, source_username
    )
    return await save_media(
        owner_user_id,
        wroxen_id=wroxen_id,
        source_chat_id=source_chat_id,
        message_id=message.id,
        link=link,
        media_type=media_type,
        file_unique_id=file_uid,
        caption=caption,
        title=details.get("title"),
        year=details.get("year"),
        quality=details.get("quality"),
        lang=details.get("lang"),
        print_type=details.get("print"),
        season=details.get("season"),
        episode=details.get("episode"),
        codec=details.get("codec"),
    )


async def run_initial_index(
    *,
    owner_user_id: int,
    bot_client: Client,
    wroxen_id: str,
    source_chat_id: int,
    last_msg_id: int,
    skip: int,
    status_message,
    source_username: Optional[str] = None,
) -> None:
    lock = _lock(owner_user_id)
    if lock.locked():
        try:
            await status_message.edit_text("⚠️ Another Wroxen index is already running.")
        except Exception:
            pass
        return

    async with lock:
        CANCEL[owner_user_id] = False
        start = time.time()
        range_start = max(skip, 0)
        end_id = int(last_msg_id)
        total_est = max(0, end_id - range_start)
        PROGRESS[owner_user_id] = {
            "status": "running",
            "processed": 0,
            "indexed": 0,
            "duplicates": 0,
            "skipped": 0,
            "errors": 0,
            "start_time": start,
            "wroxen_id": wroxen_id,
            "current_id": range_start,
            "end_id": end_id,
            "range_start": range_start,
            "total_est": total_est,
            "pct": 0,
            "status_chat_id": getattr(getattr(status_message, "chat", None), "id", None),
            "status_message_id": getattr(status_message, "id", None),
        }
        current = range_start
        BATCH = 80
        last_ui = 0.0

        async def _maybe_ui(force: bool = False):
            """Only update Telegram on force=True (final / explicit). No periodic edits."""
            nonlocal last_ui
            now = time.time()
            p = PROGRESS.get(owner_user_id) or {}
            p["elapsed"] = now - start
            done = max(0, current - range_start)
            p["pct"] = int(min(99, round(100.0 * done / total_est))) if total_est else 0
            p["current_id"] = current
            if not force:
                return
            last_ui = now
            try:
                from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Refresh", callback_data="wx:idx_prog")],
                    [InlineKeyboardButton("❌ Stop", callback_data="wx:idx_stop")],
                ])
                await status_message.edit_text(_fmt(p), reply_markup=kb)
            except Exception:
                try:
                    await status_message.edit_text(_fmt(p))
                except Exception:
                    pass

        try:
            if current >= end_id:
                PROGRESS[owner_user_id]["status"] = "done"
                PROGRESS[owner_user_id]["pct"] = 100
                await _maybe_ui(True)
                try:
                    await status_message.edit_text("⚠️ No messages in range.")
                except Exception:
                    pass
                return

            await _maybe_ui(force=True)
            while current < end_id:
                if CANCEL.get(owner_user_id):
                    PROGRESS[owner_user_id]["status"] = "cancelled"
                    await _maybe_ui(True)
                    break

                batch_end = min(current + BATCH, end_id)
                ids = list(range(current + 1, batch_end + 1))
                if not ids:
                    break
                try:
                    messages = await bot_client.get_messages(source_chat_id, ids)
                except FloodWait as e:
                    await asyncio.sleep(int(getattr(e, "value", 5)) + 1)
                    continue
                except RPCError:
                    PROGRESS[owner_user_id]["errors"] += len(ids)
                    current = batch_end
                    await _maybe_ui(False)
                    continue
                except Exception:
                    logger.exception("wroxen get_messages")
                    PROGRESS[owner_user_id]["errors"] += len(ids)
                    current = batch_end
                    await _maybe_ui(False)
                    continue

                if not isinstance(messages, list):
                    messages = [messages]

                for msg in messages:
                    if CANCEL.get(owner_user_id):
                        break
                    PROGRESS[owner_user_id]["processed"] += 1
                    if msg is None or getattr(msg, "empty", False):
                        PROGRESS[owner_user_id]["skipped"] += 1
                        continue
                    try:
                        result = await index_message_to_db(
                            owner_user_id, wroxen_id, source_chat_id, msg, source_username
                        )
                        if result == "saved":
                            PROGRESS[owner_user_id]["indexed"] += 1
                        elif result == "duplicate":
                            PROGRESS[owner_user_id]["duplicates"] += 1
                        elif result == "skip":
                            PROGRESS[owner_user_id]["skipped"] += 1
                        else:
                            PROGRESS[owner_user_id]["errors"] += 1
                    except Exception:
                        PROGRESS[owner_user_id]["errors"] += 1

                current = batch_end
                PROGRESS[owner_user_id]["current_id"] = current
                await _maybe_ui(False)
                await asyncio.sleep(0.05)

            p = PROGRESS[owner_user_id]
            if p.get("status") == "running":
                p["status"] = "done"
                p["pct"] = 100
            p["elapsed"] = time.time() - start
            p["current_id"] = current
            await _maybe_ui(force=True)
        except Exception as e:
            logger.exception("wroxen initial index failed")
            if owner_user_id in PROGRESS:
                PROGRESS[owner_user_id]["status"] = "error"
            try:
                await status_message.edit_text(f"❌ Index failed: {type(e).__name__}: {e}")
            except Exception:
                pass
            try:
                from core.log_chat import report_user_auto_stop
                await report_user_auto_stop(
                    owner_user_id,
                    feature="Wroxen Search",
                    title=f"wroxen `{wroxen_id}`",
                    reason="Wroxen indexing stopped automatically after a crash.",
                    error=f"{type(e).__name__}: {e}",
                )
            except Exception:
                pass
        finally:
            # Keep progress ~2 min so Refresh/Stop still work after finish
            await asyncio.sleep(120)
            PROGRESS.pop(owner_user_id, None)
            CANCEL.pop(owner_user_id, None)


def _bar(pct: int) -> str:
    pct = max(0, min(100, int(pct or 0)))
    filled = pct // 10
    return "█" * filled + "░" * (10 - filled)


def _fmt(p: Dict[str, Any]) -> str:
    elapsed = p.get("elapsed") or (time.time() - p.get("start_time", time.time()))
    status = p.get("status", "running")
    title = {
        "running": "📥 Wroxen Indexing...",
        "cancelled": "🛑 Wroxen index cancelled",
        "done": "🎉 Wroxen index completed",
        "error": "❌ Wroxen index failed",
    }.get(status, "📥 Wroxen Indexing...")
    pct = int(p.get("pct") or 0)
    cur = int(p.get("current_id") or 0)
    end = int(p.get("end_id") or 0)
    start_id = int(p.get("range_start") or 0)
    speed = 0.0
    if elapsed > 1:
        speed = float(p.get("processed") or 0) / elapsed
    return (
        f"**{title}**\n\n"
        f"`{_bar(pct)}` **{pct}%**\n"
        f"Cursor: `#{cur:,}` / `#{end:,}` (from `#{start_id:,}`)\n\n"
        f"Processed: **{p.get('processed', 0):,}**\n"
        f"Indexed: **{p.get('indexed', 0):,}**\n"
        f"Duplicates: **{p.get('duplicates', 0):,}**\n"
        f"Skipped: **{p.get('skipped', 0):,}**\n"
        f"Errors: **{p.get('errors', 0):,}**\n"
        f"Speed: **{speed:.1f} msg/s**\n"
        f"Runtime: **{int(elapsed // 60)}m {int(elapsed % 60)}s**\n\n"
        f"_Tap Refresh for latest · Stop to cancel_"
    )


def format_live(uid: int) -> Optional[str]:
    """Fresh snapshot for Refresh button — recompute elapsed/pct."""
    p = get_progress(uid)
    if not p:
        return None
    try:
        start = float(p.get("start_time") or time.time())
        p["elapsed"] = max(0.0, time.time() - start)
        range_start = int(p.get("range_start") or 0)
        end_id = int(p.get("end_id") or 0)
        current = int(p.get("current_id") or range_start)
        total_est = max(0, end_id - range_start) or int(p.get("total_est") or 0)
        if (p.get("status") or "") == "done":
            p["pct"] = 100
        elif total_est > 0:
            done = max(0, current - range_start)
            p["pct"] = int(min(99, round(100.0 * done / total_est)))
    except Exception:
        pass
    return _fmt(p)
