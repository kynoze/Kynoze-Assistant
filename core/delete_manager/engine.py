"""Scan + delete engine. Reuses existing forwarding user-account clients."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from pyrogram.errors import (
    ChatAdminRequired,
    FloodWait,
    MessageDeleteForbidden,
    UserNotParticipant,
)

from database import get_delete_config, update_delete_config, bump_delete_stats
from core.delete_manager.permissions import check_delete_permissions

logger = logging.getLogger(__name__)

ALL_TYPES = [
    "text",
    "photo",
    "video",
    "document",
    "audio",
    "voice",
    "animation",
    "sticker",
    "poll",
    "contact",
    "location",
    "other",
]

TYPE_LABELS = {
    "text": "Text",
    "photo": "Photo",
    "video": "Video",
    "document": "Document",
    "audio": "Audio",
    "voice": "Voice",
    "animation": "Animation",
    "sticker": "Sticker",
    "poll": "Poll",
    "contact": "Contact",
    "location": "Location",
    "other": "Other",
}

RUNNING: Dict[str, asyncio.Task] = {}
CANCEL: Dict[str, bool] = {}
PROGRESS: Dict[str, Dict[str, Any]] = {}


def is_delete_running(config_id: str) -> bool:
    t = RUNNING.get(config_id)
    return bool(t and not t.done())


def cancel_delete_job(config_id: str) -> bool:
    CANCEL[config_id] = True
    t = RUNNING.get(config_id)
    if t and not t.done():
        t.cancel()
        return True
    return False


def get_progress(config_id: str) -> Optional[Dict[str, Any]]:
    return PROGRESS.get(config_id)


def classify_message(message) -> str:
    if getattr(message, "poll", None):
        return "poll"
    if getattr(message, "contact", None):
        return "contact"
    if getattr(message, "location", None) or getattr(message, "venue", None):
        return "location"
    media = getattr(message, "media", None)
    if media:
        key = getattr(media, "value", None) or str(media)
        if key in TYPE_LABELS and key not in ("web_page",):
            return key
        return "other"
    if getattr(message, "text", None):
        return "text"
    return "other"


def _sender_id(message) -> Optional[int]:
    fu = getattr(message, "from_user", None)
    if fu and getattr(fu, "id", None):
        return int(fu.id)
    sc = getattr(message, "sender_chat", None)
    if sc and getattr(sc, "id", None):
        return int(sc.id)
    return None


def _msg_date(message) -> Optional[datetime]:
    d = getattr(message, "date", None)
    if not d:
        return None
    if isinstance(d, datetime):
        if d.tzinfo is None:
            return d.replace(tzinfo=timezone.utc)
        return d
    return None


def eligible(message, cfg: dict) -> tuple[bool, str]:
    """Return (should_delete, skip_reason). Protected always wins."""
    mid = int(getattr(message, "id", 0) or 0)
    protected_ids = {int(x) for x in (cfg.get("protected_message_ids") or [])}
    if mid in protected_ids:
        return False, "protected_id"

    sid = _sender_id(message)
    protected_users = {int(x) for x in (cfg.get("protected_user_ids") or [])}
    if sid is not None and sid in protected_users:
        return False, "protected_user"

    age = int(cfg.get("message_age_seconds") or 86400)
    md = _msg_date(message)
    if md:
        now = datetime.now(timezone.utc)
        if (now - md).total_seconds() < age:
            return False, "too_new"

    allowed = set(cfg.get("message_types") or ALL_TYPES)
    kind = classify_message(message)
    if kind not in allowed:
        return False, "type"

    if getattr(message, "service", None):
        return False, "service"

    return True, "ok"


async def run_delete_job(
    cfg: dict,
    *,
    progress_message=None,
    auto: bool = False,
) -> Dict[str, Any]:
    """
    Delete eligible messages in cfg.target_chat_id using cfg.account_id.
    Permission is re-checked live before scanning.
    """
    from core.job_worker import get_user_client
    from database import get_account

    config_id = cfg["delete_config_id"]
    user_id = cfg["user_id"]
    chat_id = cfg["target_chat_id"]
    account_id = cfg["account_id"]

    stats = {
        "processed": 0,
        "deleted": 0,
        "skipped": 0,
        "protected": 0,
        "failed": 0,
        "status": "running",
        "started_at": time.time(),
        "error": None,
    }
    PROGRESS[config_id] = stats
    CANCEL[config_id] = False

    account = await get_account(user_id, account_id)
    if not account:
        stats["status"] = "failed"
        stats["error"] = "Account not found"
        return stats

    client = await get_user_client(account)
    if not client:
        stats["status"] = "failed"
        stats["error"] = "Could not start the selected account (session invalid?)."
        await _persist_error(user_id, config_id, stats["error"], pause_auto=True)
        return stats

    ok, reason = await check_delete_permissions(
        client, chat_id, account.get("name") or account_id, m_user_id=int(user_id)
    )
    if not ok:
        stats["status"] = "failed"
        stats["error"] = reason
        await _persist_error(user_id, config_id, reason, pause_auto=True)
        return stats

    last_edit = 0.0
    batch: list[int] = []

    async def flush_batch():
        nonlocal batch
        if not batch:
            return
        ids = list(batch)
        batch = []
        try:
            await client.delete_messages(chat_id, ids)
            stats["deleted"] += len(ids)
        except FloodWait as e:
            await asyncio.sleep(int(getattr(e, "value", 1) or 1) + 1)
            try:
                await client.delete_messages(chat_id, ids)
                stats["deleted"] += len(ids)
            except Exception:
                stats["failed"] += len(ids)
        except (ChatAdminRequired, UserNotParticipant, MessageDeleteForbidden) as e:
            stats["failed"] += len(ids)
            stats["error"] = "Lost delete permission while running."
            stats["status"] = "paused"
            await _persist_error(user_id, config_id, stats["error"], pause_auto=True)
            raise PermissionError(str(e))
        except Exception:
            logger.exception("batch delete failed")
            # retry one-by-one
            for mid in ids:
                try:
                    await client.delete_messages(chat_id, mid)
                    stats["deleted"] += 1
                except FloodWait as e:
                    await asyncio.sleep(int(getattr(e, "value", 1) or 1) + 1)
                    try:
                        await client.delete_messages(chat_id, mid)
                        stats["deleted"] += 1
                    except Exception:
                        stats["failed"] += 1
                except Exception:
                    stats["failed"] += 1

    async def maybe_progress():
        nonlocal last_edit
        if not progress_message:
            return
        now = time.time()
        if now - last_edit < 2.5:
            return
        last_edit = now
        try:
            await progress_message.edit_text(progress_text(cfg, stats))
        except Exception:
            pass

    try:
        async for message in client.get_chat_history(chat_id):
            if CANCEL.get(config_id):
                stats["status"] = "cancelled"
                break
            if getattr(message, "empty", False):
                continue

            stats["processed"] += 1
            should, why = eligible(message, cfg)
            if not should:
                stats["skipped"] += 1
                if why in ("protected_id", "protected_user"):
                    stats["protected"] += 1
                continue

            batch.append(int(message.id))
            if len(batch) >= 100:
                await flush_batch()
                await maybe_progress()

        if stats["status"] == "running":
            await flush_batch()
            stats["status"] = "completed"
    except asyncio.CancelledError:
        stats["status"] = "cancelled"
        try:
            await flush_batch()
        except Exception:
            pass
    except PermissionError:
        try:
            await flush_batch()
        except Exception:
            pass
    except Exception as e:
        logger.exception("Delete job crashed")
        stats["status"] = "failed"
        stats["error"] = "Deletion stopped due to an unexpected error."
        await _persist_error(user_id, config_id, str(e), pause_auto=False)
    finally:
        elapsed = time.time() - stats["started_at"]
        stats["runtime"] = elapsed
        await bump_delete_stats(
            user_id,
            config_id,
            {
                "processed": stats["processed"],
                "deleted": stats["deleted"],
                "skipped": stats["skipped"],
                "protected": stats["protected"],
                "failed": stats["failed"],
            },
            last_run_at=datetime.now(timezone.utc),
        )
        PROGRESS[config_id] = stats
        RUNNING.pop(config_id, None)
        CANCEL.pop(config_id, None)

    return stats


def progress_text(cfg: dict, stats: dict) -> str:
    from handlers.ui import fmt_duration

    elapsed = time.time() - float(stats.get("started_at") or time.time())
    deleted = int(stats.get("deleted") or 0)
    speed = deleted / elapsed if elapsed > 0 and deleted else 0
    return (
        "🗑️ **Deleting Messages...**\n\n"
        f"**Group:** {cfg.get('target_title') or cfg.get('target_chat_id')}\n\n"
        f"Processed: **{stats.get('processed', 0):,}**\n"
        f"Deleted: **{deleted:,}**\n"
        f"Skipped: **{stats.get('skipped', 0):,}**\n"
        f"Protected: **{stats.get('protected', 0):,}**\n"
        f"Failed: **{stats.get('failed', 0):,}**\n\n"
        f"⚡ Speed: **{speed:.1f} msg/s**\n"
        f"⏱ Runtime: **{fmt_duration(elapsed)}**"
    )


async def _persist_error(user_id: int, config_id: str, error: str, pause_auto: bool):
    updates = {"last_error": error}
    if pause_auto:
        updates["auto_delete"] = False
        updates["enabled"] = True
    await update_delete_config(user_id, config_id, updates)
    if pause_auto:
        try:
            from core.log_chat import report_user_auto_stop
            from database import get_delete_config
            cfg = await get_delete_config(user_id, config_id) or {}
            await report_user_auto_stop(
                user_id,
                feature="Delete Manager",
                title=cfg.get("target_title") or str(cfg.get("target_chat_id") or config_id),
                reason="Auto-delete was paused automatically (permission / session / error).",
                error=error,
            )
        except Exception:
            logger.exception("log-chat delete persist")
