"""User log-chat + owner log-chat reports.

User log chat: auto-stop / auto-pause of the user's work (no user tap).
Management bot must be admin in that chat.

Owner log chat: errors/warnings the bot owner must see.
"""
from __future__ import annotations

import asyncio
import logging
import time
import traceback
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_mgmt_bot = None
_rate: dict[str, float] = {}

USER_RATE_SEC = 20.0
OWNER_RATE_SEC = 8.0
MAX_LEN = 3800


def set_mgmt_bot(client) -> None:
    global _mgmt_bot
    _mgmt_bot = client


def get_mgmt_bot():
    return _mgmt_bot


def _now_ist() -> str:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Kolkata")
    except Exception:
        tz = timezone.utc
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")


def _clip(text: str, n: int = MAX_LEN) -> str:
    text = (text or "").strip()
    if len(text) <= n:
        return text
    return text[: n - 20] + "\n\n…(truncated)"


def _should_rate(key: str, window: float) -> bool:
    now = time.monotonic()
    last = _rate.get(key, 0.0)
    if now - last < window:
        return False
    _rate[key] = now
    if len(_rate) > 4000:
        cutoff = now - 600
        for k in [k for k, v in _rate.items() if v < cutoff]:
            _rate.pop(k, None)
    return True


async def _send(chat_id: int, text: str) -> tuple[bool, str]:
    bot = get_mgmt_bot()
    if not bot:
        return False, "Management bot not started"
    body = _clip(text)
    try:
        try:
            await bot.send_message(
                int(chat_id),
                body,
                link_preview_options={"is_disabled": True},
            )
        except TypeError:
            # older pyrogram
            await bot.send_message(int(chat_id), body, disable_web_page_preview=True)
        except Exception:
            await bot.send_message(int(chat_id), body)
        return True, "ok"
    except Exception as e:
        logger.warning("log-chat send to %s failed: %s", chat_id, e)
        return False, f"{type(e).__name__}: {e}"


async def get_user_log_chat(user_id: int) -> Optional[dict]:
    from database import get_user
    u = await get_user(int(user_id))
    if not u or not u.get("log_chat_id"):
        return None
    return {
        "chat_id": int(u["log_chat_id"]),
        "title": u.get("log_chat_title") or str(u["log_chat_id"]),
    }


async def set_user_log_chat(user_id: int, chat_id: Optional[int], title: Optional[str] = None) -> None:
    from database import db
    now = datetime.now(timezone.utc)
    if chat_id is None:
        await db.users.update_one(
            {"user_id": int(user_id)},
            {"$unset": {"log_chat_id": "", "log_chat_title": ""}, "$set": {"updated_at": now}},
        )
        return
    await db.users.update_one(
        {"user_id": int(user_id)},
        {
            "$set": {
                "log_chat_id": int(chat_id),
                "log_chat_title": title or str(chat_id),
                "updated_at": now,
            }
        },
        upsert=True,
    )


async def get_owner_log_chat() -> Optional[dict]:
    from core.access import get_system_settings
    s = await get_system_settings()
    cid = s.get("owner_log_chat_id")
    if not cid:
        return None
    return {
        "chat_id": int(cid),
        "title": s.get("owner_log_chat_title") or str(cid),
    }


async def set_owner_log_chat(chat_id: Optional[int], title: Optional[str] = None) -> None:
    from core.access import update_system_settings
    if chat_id is None:
        await update_system_settings({
            "owner_log_chat_id": None,
            "owner_log_chat_title": None,
        })
        return
    await update_system_settings({
        "owner_log_chat_id": int(chat_id),
        "owner_log_chat_title": title or str(chat_id),
    })


async def verify_mgmt_admin(client, chat) -> tuple[bool, str]:
    from pyrogram.enums import ChatType
    from core.permissions import fetch_member, is_admin_or_owner, has_privilege

    chat_id = getattr(chat, "id", chat)
    member, err = await fetch_member(client, chat_id, "me")
    if err or not member:
        return False, (
            "Management bot is not in this chat. "
            f"Add the bot as **admin** first. ({err or 'unknown'})"
        )
    if not is_admin_or_owner(member):
        return False, "Management bot must be an **admin** in this log chat."
    ctype = str(getattr(chat, "type", "") or "").lower()
    is_channel = "channel" in ctype or getattr(chat, "type", None) == ChatType.CHANNEL
    if is_channel and not has_privilege(member, "can_post_messages"):
        return False, "Bot is admin but **cannot post messages** in this channel."
    return True, "ok"


async def resolve_log_chat(client, raw: str):
    from pyrogram.enums import ChatType

    raw = (raw or "").strip()
    chat = None
    last_err = None
    try:
        if raw.startswith("@") or "t.me/" in raw:
            chat = await client.get_chat(raw)
        else:
            cid = int(raw)
            try:
                chat = await client.get_chat(cid)
            except Exception as e:
                last_err = e
                if cid > 0:
                    chat = await client.get_chat(int(f"-100{cid}"))
                else:
                    raise
    except Exception as e:
        last_err = e
        chat = None
    if not chat:
        return None, f"Could not resolve chat: {type(last_err).__name__ if last_err else 'unknown'}"
    if chat.type not in (ChatType.CHANNEL, ChatType.SUPERGROUP, ChatType.GROUP):
        return None, "Only a **group or channel** can be used as log chat."
    ok, msg = await verify_mgmt_admin(client, chat)
    if not ok:
        return None, msg
    return chat, "ok"


async def report_user_auto_stop(
    user_id: int,
    *,
    feature: str,
    title: str,
    reason: str,
    error: Optional[str] = None,
    extra: Optional[str] = None,
    user_initiated: bool = False,
) -> None:
    """Notify the user's log chat when work stops without their tap."""
    if user_initiated:
        return
    try:
        info = await get_user_log_chat(user_id)
        if not info:
            return
        key = f"u:{user_id}:{feature}:{title}:{reason[:80]}"
        if not _should_rate(key, USER_RATE_SEC):
            return
        lines = [
            "⚠️ **AUTO-STOP REPORT**",
            "",
            f"**Feature:** {feature}",
            f"**Item:** {title}",
            f"**When:** `{_now_ist()}`",
            f"**User ID:** `{user_id}`",
            "",
            f"**What happened:** {reason}",
        ]
        if error:
            lines += ["", "**Error / details:**", f"`{_clip(str(error), 1500)}`"]
        if extra:
            lines += ["", extra]
        lines += [
            "",
            "_This stopped automatically — you did not press Pause/Cancel/Stop._",
            "_Open the bot to fix the issue, then start it again._",
        ]
        ok, send_err = await _send(info["chat_id"], "\n".join(lines))
        if not ok:
            await report_owner(
                "WARNING",
                "User log-chat send failed",
                f"user={user_id} chat={info['chat_id']} feature={feature}\n{send_err}",
            )
    except Exception:
        logger.exception("report_user_auto_stop failed")


async def report_owner(
    level: str,
    title: str,
    detail: str,
    *,
    extra: Optional[str] = None,
) -> None:
    try:
        info = await get_owner_log_chat()
        if not info:
            return
        key = f"o:{level}:{title}:{str(detail)[:80]}"
        if not _should_rate(key, OWNER_RATE_SEC):
            return
        icon = {"ERROR": "🔴", "WARNING": "🟡", "INFO": "🔵"}.get(level.upper(), "⚪")
        lines = [
            f"{icon} **OWNER {level.upper()}**",
            "",
            f"**{title}**",
            f"**When:** `{_now_ist()}`",
            "",
            _clip(str(detail), 2500),
        ]
        if extra:
            lines += ["", _clip(str(extra), 800)]
        await _send(info["chat_id"], "\n".join(lines))
    except Exception:
        logger.exception("report_owner failed")


class OwnerLogHandler(logging.Handler):
    """Forward WARNING+ app logs to owner log chat (async, rate-limited).

    Skips noisy library loggers (pyrogram, pymongo, asyncio, etc.).
    """

    _SKIP_PREFIXES = (
        "pyrogram", "pymongo", "asyncio", "urllib3", "httpx",
        "motor", "aiohttp", "charset_normalizer",
    )

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING:
            return
        name = record.name or ""
        for pref in self._SKIP_PREFIXES:
            if name == pref or name.startswith(pref + "."):
                return
        # Ignore pure deprecation noise even if somehow not filtered
        try:
            raw = record.getMessage()
        except Exception:
            raw = ""
        low = (raw or "").lower()
        if "deprecated" in low or "will be removed" in low:
            return
        try:
            msg = self.format(record)
        except Exception:
            msg = raw
        tb = ""
        if record.exc_info:
            tb = "".join(traceback.format_exception(*record.exc_info))
        level = "ERROR" if record.levelno >= logging.ERROR else "WARNING"
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(
            report_owner(
                level,
                f"{record.name}: {record.funcName}",
                msg,
                extra=tb or None,
            )
        )


def install_owner_log_handler() -> None:
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, OwnerLogHandler):
            return
    h = OwnerLogHandler()
    h.setLevel(logging.WARNING)
    h.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(h)


async def report_user_job_complete(
    user_id: int,
    *,
    title: str,
    stats_text: str = "",
    extra: str = "",
) -> None:
    """Notify user log chat when a job finishes successfully."""
    try:
        info = await get_user_log_chat(user_id)
        if not info:
            return
        key = f"u:{user_id}:jobdone:{title}"
        if not _should_rate(key, 5.0):
            return
        lines = [
            "✅ **JOB COMPLETED**",
            "",
            f"**Job:** {title}",
            f"**When:** `{_now_ist()}`",
            f"**User ID:** `{user_id}`",
        ]
        if stats_text:
            lines += ["", stats_text]
        if extra:
            lines += ["", extra]
        await _send(info["chat_id"], "\n".join(lines))
    except Exception:
        logger.exception("report_user_job_complete failed")
