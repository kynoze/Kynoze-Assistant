"""Resolve chats WITHOUT requiring the management bot to join/admin.

Resolution order:
1. Active user-account clients (get_chat)
2. Same accounts: scan recent dialogs for matching id (peer cache warm)
3. Active forward-bot clients (get_chat) — bot already in channel knows peer
4. Management bot only for public @username
5. Numeric private IDs may still be accepted as unresolved — caller can
   defer full resolve to the selected executor on permission check.
"""
from __future__ import annotations

import logging
import re
from typing import Any, List, Optional, Tuple

from pyrogram import Client

logger = logging.getLogger(__name__)

_TME = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/(?:c/)?([a-zA-Z0-9_]+|\d+)",
    re.I,
)


def parse_chat_ref(raw: str) -> Tuple[Optional[int], Optional[str], str]:
    """Return (chat_id or None, username or None, display_ref)."""
    raw = (raw or "").strip()
    if not raw:
        return None, None, ""
    if raw.startswith("@"):
        return None, raw, raw
    m = _TME.search(raw)
    if m:
        part = m.group(1)
        if part.isdigit():
            cid = int(f"-100{part}")
            return cid, None, str(cid)
        uname = part if part.startswith("@") else f"@{part}"
        return None, uname, uname
    # pure numeric / -100...
    try:
        if raw.lstrip("-").isdigit():
            cid = int(raw)
            if cid > 0:
                # user pasted internal channel id without -100
                cid = int(f"-100{cid}")
            return cid, None, str(cid)
    except Exception:
        pass
    return None, raw if raw.startswith("@") else None, raw


async def _try_get_chat(client: Client, ref) -> Tuple[Optional[Any], Optional[str]]:
    try:
        if isinstance(ref, str) and ref.lstrip("-").isdigit():
            ref = int(ref)
        chat = await client.get_chat(ref)
        return chat, None
    except Exception as e:
        return None, type(e).__name__


async def _find_in_dialogs(client: Client, chat_id: int) -> Optional[Any]:
    """Warm peer cache by scanning dialogs; return chat if id matches."""
    try:
        async for d in client.get_dialogs(limit=200):
            ch = getattr(d, "chat", None)
            if ch is not None and int(getattr(ch, "id", 0) or 0) == int(chat_id):
                return ch
    except Exception:
        logger.exception("dialog scan failed")
    return None


async def _iter_user_clients(user_id: int, account_ids: Optional[list] = None):
    from database import get_user_accounts, get_account, AccountStatus
    from core.job_worker import get_user_client
    from handlers.ui import active_accounts_only

    accounts: List[dict] = []
    if account_ids:
        for aid in account_ids:
            a = await get_account(user_id, str(aid))
            if a:
                accounts.append(a)
    else:
        accounts = await get_user_accounts(user_id) or []
    accounts = active_accounts_only(accounts)
    for acc in accounts:
        if (acc.get("status") or "").lower() == AccountStatus.SLEEPING.value:
            # still try — useful for resolve
            pass
        uc = await get_user_client(acc)
        if uc:
            yield acc, uc


async def _iter_bot_clients(user_id: int):
    from database import get_user_bots
    from core.job_worker import get_bot_client

    bots = await get_user_bots(user_id) or []
    for b in bots:
        st = (b.get("status") or "active").lower()
        if st in ("disabled", "inactive", "error"):
            continue
        try:
            bc = await get_bot_client(b)
        except Exception:
            bc = None
        if bc:
            yield b, bc


async def resolve_chat_for_user(
    mgmt_client: Client,
    user_id: int,
    raw: str,
    *,
    account_ids: Optional[list] = None,
) -> Tuple[Optional[Any], str]:
    """Resolve channel/group. Returns (chat, error). error empty on success.

    If only a numeric id is known and no client can resolve yet, returns
    (None, special) — callers for target-add may accept unresolved numeric ids.
    """
    chat_id, username, display = parse_chat_ref(raw)
    last_err = "unknown"
    tried = 0

    refs: list = []
    if username:
        refs.append(username)
    if chat_id is not None:
        refs.append(chat_id)
        # also try without forcing -100 if user sent full id
        refs.append(chat_id)

    # 1) User accounts — get_chat
    async for acc, uc in _iter_user_clients(user_id, account_ids):
        tried += 1
        for ref in refs:
            chat, err = await _try_get_chat(uc, ref)
            if chat:
                return chat, ""
            last_err = err or last_err
        if chat_id is not None:
            found = await _find_in_dialogs(uc, int(chat_id))
            if found:
                return found, ""

    # 2) Forward bots already in the chat often know the peer
    async for bot, bc in _iter_bot_clients(user_id):
        tried += 1
        for ref in refs:
            chat, err = await _try_get_chat(bc, ref)
            if chat:
                return chat, ""
            last_err = err or last_err

    # 3) Management bot — public username only
    if username:
        chat, err = await _try_get_chat(mgmt_client, username)
        if chat:
            return chat, ""
        last_err = err or last_err

    if tried == 0:
        return None, (
            "No **active** user account or bot available to resolve this chat.\n"
            "Add/enable a My Account or Forward Bot first."
        )

    # Numeric private id: allow caller to continue with unresolved id
    if chat_id is not None and not username:
        return None, f"UNRESOLVED:{chat_id}:{last_err}"

    return None, (
        f"Could not resolve that chat (`{last_err}`).\n\n"
        "• Use **@username** or invite link when possible\n"
        "• For private channels: the linked account must be a **member**\n"
        "• Or **forward a message** from that chat to this bot\n"
        "• Management Bot does **not** need to be a member or admin"
    )


async def resolve_source_chat_id(
    mgmt_client: Client,
    user_id: int,
    source_chat_id,
    *,
    account_ids: Optional[list] = None,
) -> Tuple[Optional[Any], str]:
    return await resolve_chat_for_user(
        mgmt_client, user_id, str(source_chat_id), account_ids=account_ids
    )
