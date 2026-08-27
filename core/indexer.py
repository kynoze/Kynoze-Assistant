"""Index media from a source chat into the user's separate Index DB.

Runs in-process (management bot event loop). Progress kept in INDEX_PROGRESS.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from pyrogram import Client
from pyrogram.enums import MessageMediaType
from pyrogram.errors import FloodWait, RPCError

from core.index_db import save_indexed_media, get_index_collection

logger = logging.getLogger(__name__)

# user_id -> progress dict
INDEX_PROGRESS: Dict[int, Dict[str, Any]] = {}
INDEX_CANCEL: Dict[int, bool] = {}
INDEX_PAUSE: Dict[int, bool] = {}
_INDEX_LOCKS: Dict[int, asyncio.Lock] = {}

SUPPORTED = {
    MessageMediaType.VIDEO: "video",
    MessageMediaType.DOCUMENT: "document",
    MessageMediaType.PHOTO: "photo",
    MessageMediaType.AUDIO: "audio",
    MessageMediaType.ANIMATION: "animation",
    MessageMediaType.VOICE: "voice",
    MessageMediaType.VIDEO_NOTE: "video_note",
}


def _lock(user_id: int) -> asyncio.Lock:
    if user_id not in _INDEX_LOCKS:
        _INDEX_LOCKS[user_id] = asyncio.Lock()
    return _INDEX_LOCKS[user_id]


def is_indexing(user_id: int) -> bool:
    p = INDEX_PROGRESS.get(user_id)
    return bool(p and p.get("status") == "running")


def request_cancel(user_id: int) -> None:
    INDEX_CANCEL[user_id] = True
    INDEX_PAUSE[user_id] = False


def request_pause(user_id: int) -> None:
    INDEX_PAUSE[user_id] = True


def request_resume(user_id: int) -> None:
    INDEX_PAUSE[user_id] = False


def get_progress(user_id: int) -> Optional[Dict[str, Any]]:
    return INDEX_PROGRESS.get(user_id)


def _extract_media(message) -> Optional[tuple]:
    """Return (media_type_str, media_obj) or None."""
    if not message or not message.media:
        return None
    media_type = message.media
    if media_type not in SUPPORTED:
        return None
    attr = media_type.value  # e.g. "video"
    media = getattr(message, attr, None)
    if not media:
        return None
    # Photo may be list of sizes — use largest
    if media_type == MessageMediaType.PHOTO and hasattr(message, "photo") and message.photo:
        media = message.photo
    file_id = getattr(media, "file_id", None)
    file_unique_id = getattr(media, "file_unique_id", None)
    if not file_id or not file_unique_id:
        return None
    return SUPPORTED[media_type], media


async def run_indexing(
    *,
    user_id: int,
    bot_client: Client,
    index_bot_id: str,
    source_chat_id: int,
    last_msg_id: int,
    skip: int,
    status_message,
) -> None:
    """Index media messages from skip+1 .. last_msg_id inclusive via iter style get_messages batches."""
    lock = _lock(user_id)
    if lock.locked():
        try:
            await status_message.edit_text("⚠️ Another indexing task is already running for you.")
        except Exception:
            pass
        return

    async with lock:
        INDEX_CANCEL[user_id] = False
        INDEX_PAUSE[user_id] = False
        start = time.time()
        INDEX_PROGRESS[user_id] = {
            "status": "running",
            "processed": 0,
            "indexed": 0,
            "duplicates": 0,
            "skipped": 0,
            "errors": 0,
            "start_time": start,
            "source_chat_id": source_chat_id,
            "last_msg_id": last_msg_id,
            "skip": skip,
        }

        if get_index_collection(user_id) is None:
            INDEX_PROGRESS[user_id]["status"] = "error"
            try:
                await status_message.edit_text("❌ Index DB not connected.")
            except Exception:
                pass
            return

        current = max(skip, 0)
        end_id = last_msg_id
        if current >= end_id:
            INDEX_PROGRESS[user_id]["status"] = "done"
            try:
                await status_message.edit_text("⚠️ No messages to index after skip.")
            except Exception:
                pass
            INDEX_PROGRESS.pop(user_id, None)
            return

        BATCH = 100
        try:
            while current < end_id:
                if INDEX_CANCEL.get(user_id):
                    INDEX_PROGRESS[user_id]["status"] = "cancelled"
                    break

                while INDEX_PAUSE.get(user_id) and not INDEX_CANCEL.get(user_id):
                    INDEX_PROGRESS[user_id]["status"] = "paused"
                    await asyncio.sleep(1)

                if INDEX_CANCEL.get(user_id):
                    INDEX_PROGRESS[user_id]["status"] = "cancelled"
                    break

                INDEX_PROGRESS[user_id]["status"] = "running"
                batch_end = min(current + BATCH, end_id)
                ids = list(range(current + 1, batch_end + 1))
                if not ids:
                    break

                try:
                    messages = await bot_client.get_messages(source_chat_id, ids)
                except FloodWait as e:
                    wait = int(getattr(e, "value", 5)) + 1
                    await asyncio.sleep(wait)
                    continue
                except RPCError as e:
                    logger.warning("get_messages error: %s", e)
                    INDEX_PROGRESS[user_id]["errors"] += len(ids)
                    current = batch_end
                    continue
                except Exception:
                    logger.exception("get_messages failed")
                    INDEX_PROGRESS[user_id]["errors"] += len(ids)
                    current = batch_end
                    continue

                if not isinstance(messages, list):
                    messages = [messages]

                for message in messages:
                    if INDEX_CANCEL.get(user_id):
                        break
                    INDEX_PROGRESS[user_id]["processed"] += 1
                    if message is None or getattr(message, "empty", False):
                        INDEX_PROGRESS[user_id]["skipped"] += 1
                        continue
                    extracted = _extract_media(message)
                    if not extracted:
                        INDEX_PROGRESS[user_id]["skipped"] += 1
                        continue
                    media_type, media = extracted
                    caption = message.caption or getattr(media, "file_name", None)
                    try:
                        result = await save_indexed_media(
                            user_id=user_id,
                            index_bot_id=index_bot_id,
                            source_chat_id=source_chat_id,
                            source_message_id=message.id,
                            media_type=media_type,
                            file_id=media.file_id,
                            file_unique_id=media.file_unique_id,
                            caption=caption,
                        )
                        if result == "suc":
                            INDEX_PROGRESS[user_id]["indexed"] += 1
                        elif result == "dup":
                            INDEX_PROGRESS[user_id]["duplicates"] += 1
                        else:
                            INDEX_PROGRESS[user_id]["errors"] += 1
                    except Exception:
                        INDEX_PROGRESS[user_id]["errors"] += 1
                        logger.exception("save_indexed_media")

                current = batch_end
                await asyncio.sleep(0.05)

            p = INDEX_PROGRESS.get(user_id) or {}
            elapsed = time.time() - start
            status = p.get("status", "done")
            if status == "running":
                status = "done"
            p["status"] = status
            p["elapsed"] = elapsed

            text = _format_progress_text(p, final=True)
            try:
                await status_message.edit_text(text)
            except Exception:
                pass
        except Exception as e:
            logger.exception("Indexing crashed")
            try:
                await status_message.edit_text(f"❌ Indexing failed: {type(e).__name__}")
            except Exception:
                pass
        finally:
            # Keep progress briefly for Refresh, then clear if done/cancelled
            await asyncio.sleep(2)
            if INDEX_PROGRESS.get(user_id, {}).get("status") in ("done", "cancelled", "error"):
                INDEX_PROGRESS.pop(user_id, None)
            INDEX_CANCEL.pop(user_id, None)
            INDEX_PAUSE.pop(user_id, None)


def _format_progress_text(p: Dict[str, Any], final: bool = False) -> str:
    elapsed = p.get("elapsed") or (time.time() - p.get("start_time", time.time()))
    processed = max(1, p.get("processed", 0))
    speed = p.get("processed", 0) / elapsed if elapsed > 0 else 0
    status = p.get("status", "running")
    title = {
        "running": "📥 Indexing...",
        "paused": "⏸ Indexing paused",
        "cancelled": "🛑 Indexing cancelled",
        "done": "🎉 Indexing completed",
        "error": "❌ Indexing error",
    }.get(status, "📥 Indexing...")
    mins = int(elapsed // 60)
    secs = int(elapsed % 60)
    return (
        f"**{title}**\n\n"
        f"Processed: **{p.get('processed', 0):,}**\n"
        f"Indexed: **{p.get('indexed', 0):,}**\n"
        f"Duplicates: **{p.get('duplicates', 0):,}**\n"
        f"Skipped: **{p.get('skipped', 0):,}**\n"
        f"Errors: **{p.get('errors', 0):,}**\n\n"
        f"Speed: **{speed:.1f}** msg/s\n"
        f"Runtime: **{mins}m {secs}s**"
    )


def format_live_progress(user_id: int) -> Optional[str]:
    p = get_progress(user_id)
    if not p:
        return None
    return _format_progress_text(p, final=False)
