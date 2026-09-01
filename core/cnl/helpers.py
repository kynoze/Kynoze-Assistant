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

async def check_user_bot_permissions(user_id: int, source_id: int, target_id: int, bot_id: str = None) -> Optional[str]:
    """Prefer verify_cnl_bot_rule with explicit bot_id. Legacy path uses running CNL bot."""
    from core.permissions import verify_cnl_bot_rule
    if bot_id:
        return await verify_cnl_bot_rule(user_id, str(bot_id), int(source_id), int(target_id))
    from core.cnl.bots import get_user_bot_manager
    from database import get_user_bots
    bots = await get_user_bots(user_id)
    if not bots:
        return "❌ Add a bot under My Bots first."
    bid = str(bots[0].get("bot_id") or "")
    if not bid:
        return "❌ Add a bot under My Bots first."
    return await verify_cnl_bot_rule(user_id, bid, int(source_id), int(target_id))

async def check_user_account_permissions(user_id: int, source_id: int, target_id: int, account_id: str = None) -> Optional[str]:
    from core.permissions import verify_cnl_account_rule
    if account_id:
        return await verify_cnl_account_rule(user_id, str(account_id), int(source_id), int(target_id))
    from database import get_user_accounts
    accs = await get_user_accounts(user_id)
    if not accs:
        return "❌ Add an account under My Accounts first."
    aid = str(accs[0].get("account_id") or "")
    if not aid:
        return "❌ Add an account under My Accounts first."
    return await verify_cnl_account_rule(user_id, aid, int(source_id), int(target_id))
