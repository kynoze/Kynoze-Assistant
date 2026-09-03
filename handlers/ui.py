"""Shared UI helpers: safe edits, pagination, status icons, formatting."""

from __future__ import annotations

# Link preview: prefer link_preview_options (new Pyrogram/Kurigram),
# never pass deprecated disable_web_page_preview.

from datetime import datetime, timezone
from typing import Any, List, Optional, Sequence, Tuple

PAGE_SIZE = 8

def no_link_preview_kwargs() -> dict:
    """Kwargs to disable link preview without deprecated disable_web_page_preview."""
    try:
        from pyrogram.types import LinkPreviewOptions
        return {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
    except Exception:
        try:
            return {"link_preview_options": {"is_disabled": True}}
        except Exception:
            return {}

HR = ""  # separators removed per UI request

STATUS_ICON = {
    "pending": "⏳",
    "running": "🟢",
    "paused": "⏸",
    "completed": "✅",
    "cancelled": "⚪",
    "failed": "🔴",
    "active": "🟢",
    "sleeping": "😴",
    "disabled": "⚪",
    "error": "🔴",
    "connected": "🟢",
}

MIN_MONITOR_INTERVAL = 5
MAX_MONITOR_INTERVAL = 864000  # 10 days
DEFAULT_MONITOR_INTERVAL = 10
INTERVAL_PRESETS = [
    5, 10, 15, 30, 60, 120, 300, 600, 1800,
    3600, 21600, 43200, 86400, 259200, 604800, 864000,
]
INTERVAL_LABELS = {
    5: "5s", 10: "10s", 15: "15s", 30: "30s",
    60: "1m", 120: "2m", 300: "5m", 600: "10m", 1800: "30m",
    3600: "1h", 21600: "6h", 43200: "12h",
    86400: "1d", 259200: "3d", 604800: "7d", 864000: "10d",
}

FEATURE_CATEGORY = {
    "block_words": "filters",
    "whitelist": "filters",
    "media_types": "filters",
    "replacements": "content",
    "inline_buttons": "content",
    "caption_template": "content",
    "delay": "forward",
}

TOGGLE_CATEGORY = {
    "caption_enabled": "content",
    "rich_message_enabled": "content",
    "replace_enabled": "content",
    "remove_links": "content",
    "inline_buttons_enabled": "content",
    "block_words_enabled": "filters",
    "whitelist_mode": "filters",
    "forward_tag": "forward",
    "anti_duplicate": "forward",
    "future_new_posts": "future",
}


def status_icon(status: Optional[str]) -> str:
    return STATUS_ICON.get((status or "").lower(), "⚪")


def format_account_label(acc: Optional[dict], *, short: bool = False) -> str:
    """
    short=True (inline buttons): display name only.
    short=False (detail): @username · id or Name · id.
    """
    if not acc:
        return "Unknown"
    uname = (acc.get("username") or "").strip().lstrip("@")
    tg_id = acc.get("tg_user_id") or acc.get("telegram_id")
    name = (acc.get("name") or acc.get("first_name") or "").strip()
    last = (acc.get("last_name") or "").strip()
    if name and last and last not in name:
        name = f"{name} {last}".strip()
    phone = acc.get("phone") or ""

    if short:
        if name and name != phone and not str(name).startswith("+"):
            return name[:40]
        if uname:
            return f"@{uname}"[:40]
        if name:
            return name[:40]
        if phone:
            return str(phone)[:40]
        return str(acc.get("account_id") or "Account")[:12]

    if uname:
        return f"@{uname} · `{tg_id}`" if tg_id else f"@{uname}"
    if name:
        return f"{name} · `{tg_id}`" if tg_id else name
    if tg_id:
        return f"`{tg_id}`"
    if phone:
        return str(phone)
    return str(acc.get("account_id") or "Account")[:12]


def format_bot_label(bot: Optional[dict], *, short: bool = False) -> str:
    """short=True: name only for buttons."""
    if not bot:
        return "Bot"
    name = (bot.get("name") or "").strip() or "Bot"
    uname = (bot.get("bot_username") or bot.get("username") or "").strip().lstrip("@")
    if short:
        return name[:40]
    if uname:
        return f"{name} · @{uname}"
    bid = bot.get("bot_id")
    if bid:
        return f"{name} · `{str(bid)[:10]}`"
    return name


def is_account_disabled(acc: Optional[dict]) -> bool:
    if not acc:
        return True
    return (acc.get("status") or "").lower() in ("disabled", "inactive", "error")


def active_accounts_only(accounts: Optional[List[dict]]) -> List[dict]:
    return [a for a in (accounts or []) if not is_account_disabled(a)]


def clamp_interval(raw) -> int:
    try:
        n = int(float(raw))
    except (TypeError, ValueError):
        return DEFAULT_MONITOR_INTERVAL
    return max(MIN_MONITOR_INTERVAL, min(MAX_MONITOR_INTERVAL, n))


def _ui_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Asia/Kolkata")
    except Exception:
        return timezone.utc


def fmt_interval(seconds: int) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} seconds"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        if s == 0:
            return f"{m} minute" if m == 1 else f"{m} minutes"
        return f"{m}m {s}s"
    if seconds < 86400:
        h, rem = divmod(seconds, 3600)
        m = rem // 60
        if m == 0:
            return f"{h} hour" if h == 1 else f"{h} hours"
        return f"{h}h {m}m"
    d, rem = divmod(seconds, 86400)
    h = rem // 3600
    if h == 0:
        return f"{d} day" if d == 1 else f"{d} days"
    return f"{d}d {h}h"


def fmt_dt(value) -> str:
    """User-facing date+time in Asia/Kolkata. Example: 23 Aug 2026, 18:30:45"""
    if not value:
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return str(value)[:19]
    if not isinstance(value, datetime):
        return str(value)[:19]
    dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_ui_tz()).strftime("%d %b %Y, %H:%M:%S")


def remaining(sleep_until) -> str:
    if not sleep_until:
        return "-"
    if isinstance(sleep_until, str):
        try:
            sleep_until = datetime.fromisoformat(sleep_until.replace("Z", "+00:00"))
        except Exception:
            return str(sleep_until)[:16]
    now = datetime.now(timezone.utc)
    if getattr(sleep_until, "tzinfo", None) is None:
        sleep_until = sleep_until.replace(tzinfo=timezone.utc)
    secs = int((sleep_until - now).total_seconds())
    if secs <= 0:
        return "due now"
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return "-"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def pct(done, total) -> int:
    try:
        if not total:
            return 0
        return min(100, int(done * 100 / total))
    except Exception:
        return 0


def paginate(items: Sequence[Any], page: int, size: int = PAGE_SIZE) -> Tuple[List[Any], int, int]:
    n = len(items)
    total_pages = max(1, (n + size - 1) // size)
    page = max(0, min(int(page or 0), total_pages - 1))
    start = page * size
    return list(items[start:start + size]), page, total_pages


def pager_row(prefix: str, page: int, total_pages: int) -> List:
    from pyrogram.types import InlineKeyboardButton

    if total_pages <= 1:
        return []
    prev_p = max(0, page - 1)
    next_p = min(total_pages - 1, page + 1)
    return [
        InlineKeyboardButton("⬅️", callback_data=f"{prefix}{prev_p}"),
        InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="ui:noop"),
        InlineKeyboardButton("➡️", callback_data=f"{prefix}{next_p}"),
    ]


def with_pager(
    markup,
    prefix: str,
    page: int,
    total_pages: int,
    insert_at: int = -2,
):
    from pyrogram.types import InlineKeyboardMarkup

    pager = pager_row(prefix, page, total_pages)
    if not pager:
        return markup
    rows = list(markup.inline_keyboard)
    idx = insert_at if insert_at >= 0 else len(rows) + insert_at
    idx = max(0, min(idx, len(rows)))
    rows.insert(idx, pager)
    return InlineKeyboardMarkup(rows)


async def safe_answer(query, text: str = "", show_alert: bool = False):
    from pyrogram.errors import QueryIdInvalid

    try:
        await query.answer(text, show_alert=show_alert)
    except QueryIdInvalid:
        pass
    except Exception:
        pass


async def safe_edit(
    target,
    text: str,
    reply_markup=None,
    **kwargs,
):
    """Edit a Message or CallbackQuery.message. Ignore MessageNotModified."""
    from pyrogram.errors import MessageNotModified
    from pyrogram.types import CallbackQuery

    msg = target
    if isinstance(target, CallbackQuery):
        msg = target.message
    try:
        await msg.edit_text(text, reply_markup=reply_markup, **kwargs)
    except MessageNotModified:
        pass
    except Exception:
        raise


def on_off(v: bool) -> str:
    return "🟢 ON" if v else "⚪ OFF"


def load_secret(value: str) -> str:
    """Decrypt stored session/token. Raises if still encrypted after decrypt."""
    from core.security import decrypt_session

    plain = decrypt_session(value or "")
    if isinstance(plain, str) and plain.startswith("enc:v1:"):
        raise RuntimeError("Could not decrypt stored secret. Check SESSION_ENC_KEY.")
    return plain
