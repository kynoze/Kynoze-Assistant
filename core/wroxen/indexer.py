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
    details = extract_details(caption)
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
        PROGRESS[owner_user_id] = {
            "status": "running",
            "processed": 0,
            "indexed": 0,
            "duplicates": 0,
            "skipped": 0,
            "errors": 0,
            "start_time": start,
            "wroxen_id": wroxen_id,
        }
        current = max(skip, 0)
        end_id = last_msg_id
        BATCH = 80

        try:
            if current >= end_id:
                PROGRESS[owner_user_id]["status"] = "done"
                await status_message.edit_text("⚠️ No messages in range.")
                return

            while current < end_id:
                if CANCEL.get(owner_user_id):
                    PROGRESS[owner_user_id]["status"] = "cancelled"
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
                    continue
                except Exception:
                    logger.exception("wroxen get_messages")
                    PROGRESS[owner_user_id]["errors"] += len(ids)
                    current = batch_end
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
                await asyncio.sleep(0.05)

            p = PROGRESS[owner_user_id]
            if p.get("status") == "running":
                p["status"] = "done"
            p["elapsed"] = time.time() - start
            try:
                await status_message.edit_text(_fmt(p))
            except Exception:
                pass
        except Exception as e:
            logger.exception("wroxen initial index failed")
            try:
                await status_message.edit_text(f"❌ Index failed: {type(e).__name__}")
            except Exception:
                pass
        finally:
            await asyncio.sleep(1)
            PROGRESS.pop(owner_user_id, None)
            CANCEL.pop(owner_user_id, None)


def _fmt(p: Dict[str, Any]) -> str:
    elapsed = p.get("elapsed") or (time.time() - p.get("start_time", time.time()))
    status = p.get("status", "running")
    title = {
        "running": "📥 Wroxen Indexing...",
        "cancelled": "🛑 Wroxen index cancelled",
        "done": "🎉 Wroxen index completed",
    }.get(status, "📥 Wroxen Indexing...")
    return (
        f"**{title}**\n\n"
        f"Processed: **{p.get('processed', 0):,}**\n"
        f"Indexed: **{p.get('indexed', 0):,}**\n"
        f"Duplicates: **{p.get('duplicates', 0):,}**\n"
        f"Skipped: **{p.get('skipped', 0):,}**\n"
        f"Errors: **{p.get('errors', 0):,}**\n"
        f"Runtime: **{int(elapsed // 60)}m {int(elapsed % 60)}s**"
    )


def format_live(uid: int) -> Optional[str]:
    p = get_progress(uid)
    return _fmt(p) if p else None
