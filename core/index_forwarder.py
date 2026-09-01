"""Forward indexed media via the same Index Bot using send_cached_media.

Success policy for multi-target + delete-after:
A record is deleted only if it was successfully sent to ALL selected targets.
Partial failures keep the record.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, RPCError

from core.index_db import fetch_indexed_batch, delete_indexed_by_ids, get_index_collection

logger = logging.getLogger(__name__)

FWD_PROGRESS: Dict[int, Dict[str, Any]] = {}
FWD_CANCEL: Dict[int, bool] = {}
_FWD_LOCKS: Dict[int, asyncio.Lock] = {}


def _lock(user_id: int) -> asyncio.Lock:
    if user_id not in _FWD_LOCKS:
        _FWD_LOCKS[user_id] = asyncio.Lock()
    return _FWD_LOCKS[user_id]


def is_forwarding(user_id: int) -> bool:
    p = FWD_PROGRESS.get(user_id)
    return bool(p and p.get("status") == "running")


def request_cancel(user_id: int) -> None:
    FWD_CANCEL[user_id] = True


def get_progress(user_id: int) -> Optional[Dict[str, Any]]:
    return FWD_PROGRESS.get(user_id)


async def run_index_forward(
    *,
    user_id: int,
    bot_client: Client,
    index_bot_id: str,
    target_chat_ids: List[int],
    count: int,
    delete_after: bool,
    status_message,
) -> None:
    lock = _lock(user_id)
    if lock.locked():
        try:
            await status_message.edit_text("⚠️ Another index-forward task is already running.")
        except Exception:
            pass
        return

    async with lock:
        FWD_CANCEL[user_id] = False
        start = time.time()
        FWD_PROGRESS[user_id] = {
            "status": "running",
            "total": count,
            "forwarded": 0,
            "errors": 0,
            "deleted": 0,
            "start_time": start,
            "targets": len(target_chat_ids),
        }

        if get_index_collection(user_id) is None:
            FWD_PROGRESS[user_id]["status"] = "error"
            try:
                await status_message.edit_text("❌ Index DB not connected.")
            except Exception:
                pass
            return

        if not target_chat_ids:
            FWD_PROGRESS[user_id]["status"] = "error"
            try:
                await status_message.edit_text("❌ No targets selected.")
            except Exception:
                pass
            return

        batch = await fetch_indexed_batch(user_id, index_bot_id, count)
        if not batch:
            FWD_PROGRESS[user_id]["status"] = "done"
            try:
                await status_message.edit_text("📭 No indexed media available for this Index Bot.")
            except Exception:
                pass
            FWD_PROGRESS.pop(user_id, None)
            return

        FWD_PROGRESS[user_id]["total"] = len(batch)
        to_delete: List[Any] = []

        try:
            for doc in batch:
                if FWD_CANCEL.get(user_id):
                    FWD_PROGRESS[user_id]["status"] = "cancelled"
                    break

                file_id = doc.get("file_id")
                caption = doc.get("caption") or ""
                all_ok = True

                for tid in target_chat_ids:
                    if FWD_CANCEL.get(user_id):
                        all_ok = False
                        break
                    success = await _send_one(bot_client, tid, file_id, caption)
                    if not success:
                        all_ok = False
                        # continue other targets; record stays if any fail

                if all_ok:
                    FWD_PROGRESS[user_id]["forwarded"] += 1
                    if delete_after:
                        to_delete.append(doc["_id"])
                else:
                    FWD_PROGRESS[user_id]["errors"] += 1

                await asyncio.sleep(0.8)

            if delete_after and to_delete:
                deleted = await delete_indexed_by_ids(user_id, to_delete)
                FWD_PROGRESS[user_id]["deleted"] = deleted

            p = FWD_PROGRESS[user_id]
            if p.get("status") == "running":
                p["status"] = "done"
            p["elapsed"] = time.time() - start
            try:
                await status_message.edit_text(_format_fwd_text(p, final=True))
            except Exception:
                pass
        except Exception as e:
            logger.exception("Index forward crashed")
            try:
                FWD_PROGRESS[user_id]["status"] = "error"
            except Exception:
                pass
            try:
                await status_message.edit_text(f"❌ Forward failed: {type(e).__name__}")
            except Exception:
                pass
            try:
                from core.log_chat import report_user_auto_stop
                await report_user_auto_stop(
                    user_id,
                    feature="Index-Forward",
                    title="Indexed media forward",
                    reason="Index-forward stopped automatically after a crash.",
                    error=f"{type(e).__name__}: {e}",
                )
            except Exception:
                pass
        finally:
            await asyncio.sleep(2)
            if FWD_PROGRESS.get(user_id, {}).get("status") in ("done", "cancelled", "error"):
                FWD_PROGRESS.pop(user_id, None)
            FWD_CANCEL.pop(user_id, None)


async def _send_one(client: Client, chat_id: int, file_id: str, caption: str) -> bool:
    attempts = 0
    while attempts < 5:
        try:
            await client.send_cached_media(
                chat_id=chat_id,
                file_id=file_id,
                caption=caption[:1024] if caption else None,
                parse_mode=ParseMode.HTML,
            )
            return True
        except FloodWait as e:
            wait = int(getattr(e, "value", 5)) + 1
            logger.warning("FloodWait %ss on index forward", wait)
            await asyncio.sleep(wait)
            attempts += 1
        except RPCError as e:
            logger.warning("send_cached_media RPCError: %s", e)
            return False
        except Exception:
            logger.exception("send_cached_media failed")
            return False
    return False


def _format_fwd_text(p: Dict[str, Any], final: bool = False) -> str:
    elapsed = p.get("elapsed") or (time.time() - p.get("start_time", time.time()))
    status = p.get("status", "running")
    title = {
        "running": "📤 Forwarding indexed media...",
        "cancelled": "🛑 Forward cancelled",
        "done": "✅ Index forward completed",
        "error": "❌ Index forward error",
    }.get(status, "📤 Forwarding...")
    mins = int(elapsed // 60)
    secs = int(elapsed % 60)
    return (
        f"**{title}**\n\n"
        f"Selected: **{p.get('total', 0):,}**\n"
        f"Success (all targets): **{p.get('forwarded', 0):,}**\n"
        f"Failed / partial: **{p.get('errors', 0):,}**\n"
        f"Deleted from Index DB: **{p.get('deleted', 0):,}**\n"
        f"Targets: **{p.get('targets', 0)}**\n\n"
        f"Runtime: **{mins}m {secs}s**"
    )


def format_live_progress(user_id: int) -> Optional[str]:
    p = get_progress(user_id)
    if not p:
        return None
    return _format_fwd_text(p, final=False)
