"""CNL helpers — resolve chats, permissions, format rules, parse buttons."""
from __future__ import annotations
import re
from typing import Any, Dict, List, Optional, Tuple
from pyrogram import Client
from pyrogram.enums import ChatMemberStatus, ChatType
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

async def resolve_chat_id(client: Client, text: str) -> Optional[int]:
    text = (text or "").strip()
    if not text:
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    m = re.search(r"(?:t\.me|telegram\.me)/(?:c/)?(?:joinchat/)?([A-Za-z0-9_+-]+)", text)
    if m:
        token = m.group(1)
        if token.isdigit():
            return int("-100" + token)
        text = token if not text.startswith("@") else text
    try:
        chat = await client.get_chat(text if text.startswith("@") else f"@{text.lstrip('@')}")
        return chat.id
    except Exception:
        try:
            chat = await client.get_chat(text)
            return chat.id
        except Exception:
            return None

def parse_buttons(text: str) -> Optional[List[List[Dict[str, str]]]]:
    """Parse button rows.
    Format per cell: `Label - https://url` or `Label | https://url`
    Cells on one line separated by `|`. New line = new row.
    """
    if not text or not text.strip():
        return None
    rows = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        row = []
        for part in line.split("|"):
            part = part.strip()
            if not part:
                continue
            label, url = None, None
            # "Label - url" (preferred)
            m = re.match(r"^(.+?)\s+-\s+(https?://\S+|tg://\S+)$", part, re.I)
            if m:
                label, url = m.group(1).strip(), m.group(2).strip()
            else:
                # "Label: url" only if label has no scheme
                if "://" in part:
                    # try last token as url
                    bits = part.rsplit(None, 1)
                    if len(bits) == 2 and bits[1].lower().startswith(("http://", "https://", "tg://")):
                        label, url = bits[0].strip(" :-"), bits[1].strip()
                elif ":" in part and "://" not in part.split(":", 1)[0]:
                    label, url = part.split(":", 1)
                    label, url = label.strip(), url.strip()
            if label and url and url.lower().startswith(("http://", "https://", "tg://")):
                row.append({"text": label[:64], "url": url})
        if row:
            rows.append(row)
    return rows or None

def buttons_to_markup(buttons) -> Optional[InlineKeyboardMarkup]:
    if not buttons:
        return None
    try:
        rows = []
        for row in buttons:
            rows.append([InlineKeyboardButton(b["text"], url=b["url"]) for b in row if b.get("text") and b.get("url")])
        return InlineKeyboardMarkup(rows) if rows else None
    except Exception:
        return None

def format_rule(rule: Dict[str, Any], idx: int = 0) -> str:
    src = rule.get("source_chat_id")
    tgt = rule.get("target_chat_id")
    en = "✅" if rule.get("enabled", True) else "⏸"
    via = rule.get("forward_via") or "user_bot"
    return f"{en} **#{idx}** `{src}` → `{tgt}` via `{via}`"

async def is_user_admin_of_chat(client: Client, chat_id: int, user_id: int) -> bool:
    try:
        m = await client.get_chat_member(chat_id, user_id)
        return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        return False

async def check_user_bot_permissions(user_id: int, source_id: int, target_id: int) -> Optional[str]:
    from core.cnl.db import get_cnl
    from core.cnl.bots import get_user_bot_manager
    cnl = await get_cnl(user_id)
    if not cnl or not await cnl.has_active_bot(user_id):
        return "CNL bot required. Connect a bot first."
    ub = await get_user_bot_manager().get_bot(user_id)
    if not ub or not ub.is_connected:
        return "CNL bot offline. Reconnect your bot."
    try:
        st = getattr((await ub.get_chat_member(source_id, "me")).status, "value", "").lower()
        if st in ("left", "kicked", "banned"):
            return "Bot not in source chat."
    except Exception as e:
        return f"Source check failed: {type(e).__name__}"
    try:
        m = await ub.get_chat_member(target_id, "me")
        st = getattr(m.status, "value", "").lower()
        if st in ("left", "kicked", "banned"):
            return "Bot not in target chat."
        chat = await ub.get_chat(target_id)
        if getattr(chat.type, "name", "").upper() == "CHANNEL" and m.status not in (
            ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER
        ):
            return "Bot must be admin in channel target."
    except Exception as e:
        return f"Target check failed: {type(e).__name__}"
    return None

async def check_user_account_permissions(user_id: int, source_id: int, target_id: int) -> Optional[str]:
    from core.cnl.db import get_cnl
    from core.cnl.clients import get_user_client_manager
    cnl = await get_cnl(user_id)
    if not cnl or not await cnl.has_active_session(user_id):
        return "CNL account required. Connect an account first."
    uc = await get_user_client_manager().get_client(user_id)
    if not uc or not uc.is_connected:
        return "CNL account offline. Reconnect your account."
    try:
        st = getattr((await uc.get_chat_member(source_id, "me")).status, "value", "").lower()
        if st in ("left", "kicked", "banned"):
            return "Account not in source."
    except Exception as e:
        return f"Source check failed: {type(e).__name__}"
    try:
        m = await uc.get_chat_member(target_id, "me")
        st = getattr(m.status, "value", "").lower()
        if st in ("left", "kicked", "banned"):
            return "Account not in target."
        chat = await uc.get_chat(target_id)
        if getattr(chat.type, "name", "").upper() == "CHANNEL" and m.status not in (
            ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER
        ):
            return "Account must be admin for channel target."
    except Exception as e:
        return f"Target check failed: {type(e).__name__}"
    return None
