"""Centralized Telegram membership / permission checks.

Always verify the ACTUAL executor (selected bot / user account) and M_USER
independently. Management Bot admin is NEVER required as a substitute.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from pyrogram import Client
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import (
    ChannelPrivate,
    ChatAdminRequired,
    PeerIdInvalid,
    UserNotParticipant,
)

logger = logging.getLogger(__name__)

ADMIN_STATUSES = (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
MEMBER_STATUSES = (
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.OWNER,
    ChatMemberStatus.RESTRICTED,
)


def _status(member) -> Any:
    return getattr(member, "status", None)


def is_admin_or_owner(member) -> bool:
    return _status(member) in ADMIN_STATUSES


def is_member_or_above(member) -> bool:
    st = _status(member)
    if st in MEMBER_STATUSES:
        return True
    # restricted still counts as present/member for "at least member"
    return st == ChatMemberStatus.RESTRICTED


def has_privilege(member, privilege: str) -> bool:
    """Owners always have all privileges. Admins need explicit privilege flags."""
    if _status(member) == ChatMemberStatus.OWNER:
        return True
    if _status(member) != ChatMemberStatus.ADMINISTRATOR:
        return False
    priv = getattr(member, "privileges", None)
    if priv is None:
        return False
    return bool(getattr(priv, privilege, False))


async def get_chat_member_safe(client: Client, chat_id: Union[int, str], user: Union[int, str]):
    """Return ChatMember or raise a friendly mapped error string via exception message."""
    return await client.get_chat_member(chat_id, user)


async def fetch_member(
    client: Client,
    chat_id: Union[int, str],
    user: Union[int, str] = "me",
) -> Tuple[Optional[Any], Optional[str]]:
    try:
        m = await client.get_chat_member(chat_id, user)
        return m, None
    except UserNotParticipant:
        return None, "not_participant"
    except ChannelPrivate:
        return None, "private"
    except PeerIdInvalid:
        return None, "peer_invalid"
    except ChatAdminRequired:
        return None, "admin_required"
    except Exception as e:
        logger.debug("fetch_member chat=%s user=%s: %s", chat_id, user, type(e).__name__)
        return None, type(e).__name__


def _err_map_self(kind: str, role: str, chat_label: str) -> str:
    """kind = bot|account|you ; role describes requirement."""
    if kind == "bot":
        who = "Selected bot"
    elif kind == "account":
        who = "Selected account"
    else:
        who = "You"
    return f"❌ {who} {role} in the {chat_label}."


async def check_self_admin(
    client: Client,
    chat_id: Union[int, str],
    *,
    kind: str = "bot",
    chat_label: str = "chat",
    required_privileges: Optional[Sequence[str]] = None,
) -> Optional[str]:
    """Verify client identity ('me') is admin/owner, optionally with privileges."""
    m, err = await fetch_member(client, chat_id, "me")
    if err == "not_participant":
        return _err_map_self(kind, "must be present", chat_label)
    if err:
        return f"❌ Cannot verify {kind} in the {chat_label} ({err})."
    if not is_admin_or_owner(m):
        return _err_map_self(kind, "must be an administrator", chat_label)
    if required_privileges:
        for p in required_privileges:
            if not has_privilege(m, p):
                return (
                    f"❌ Selected {kind} does not have the required permission "
                    f"`{p}` in the {chat_label}."
                )
    return None


async def check_self_member(
    client: Client,
    chat_id: Union[int, str],
    *,
    kind: str = "account",
    chat_label: str = "source chat",
) -> Optional[str]:
    m, err = await fetch_member(client, chat_id, "me")
    if err == "not_participant":
        return _err_map_self(kind, "must be a member", chat_label)
    if err:
        return f"❌ Cannot verify {kind} in the {chat_label} ({err})."
    if not is_member_or_above(m):
        return _err_map_self(kind, "must be a member", chat_label)
    return None


async def check_user_admin(
    client: Client,
    chat_id: Union[int, str],
    user_id: int,
    *,
    chat_label: str = "chat",
    required_privileges: Optional[Sequence[str]] = None,
) -> Optional[str]:
    """Verify M_USER is admin/owner in chat (queried via executor client)."""
    m, err = await fetch_member(client, chat_id, int(user_id))
    if err == "not_participant":
        return f"❌ You must be an administrator or owner of this {chat_label}."
    if err:
        return f"❌ Cannot verify your status in the {chat_label} ({err})."
    if not is_admin_or_owner(m):
        return f"❌ You must be an administrator or owner of this {chat_label}."
    if required_privileges:
        for p in required_privileges:
            if not has_privilege(m, p):
                return (
                    f"❌ You do not have the required permission `{p}` "
                    f"in the {chat_label}."
                )
    return None


# ── ownership ──────────────────────────────────────────────────────────────

async def assert_bot_owned(user_id: int, bot_id: str) -> Tuple[Optional[dict], Optional[str]]:
    from database import get_bot
    b = await get_bot(user_id, str(bot_id))
    if not b:
        return None, "❌ Bot not found or not owned by you."
    return b, None


async def assert_account_owned(user_id: int, account_id: str) -> Tuple[Optional[dict], Optional[str]]:
    from database import get_account
    a = await get_account(user_id, str(account_id))
    if not a:
        return None, "❌ Account not found or not owned by you."
    return a, None


async def start_bot_client(bot_doc: dict) -> Tuple[Optional[Client], Optional[str]]:
    try:
        from core.job_worker import get_bot_client
        c = await get_bot_client(bot_doc)
        if not c:
            return None, "❌ Could not start selected bot."
        return c, None
    except Exception as e:
        logger.exception("start_bot_client")
        return None, f"❌ Could not start selected bot ({type(e).__name__})."


async def start_account_client(account_doc: dict) -> Tuple[Optional[Client], Optional[str]]:
    try:
        from core.job_worker import get_user_client
        c = await get_user_client(account_doc)
        if not c:
            return None, "❌ Could not start selected account."
        return c, None
    except Exception as e:
        logger.exception("start_account_client")
        return None, f"❌ Could not start selected account ({type(e).__name__})."


# ── feature composites ─────────────────────────────────────────────────────

# Bot posting/forwarding typically needs post messages in channels
BOT_SEND_PRIVS = ("can_post_messages",)
# Some groups use can_manage_chat; post_messages is the critical one for channels
ACCOUNT_SEND_PRIVS = ("can_post_messages",)
DELETE_PRIVS = ("can_delete_messages",)


async def verify_cnl_bot_rule(
    user_id: int,
    bot_id: str,
    source_id: int,
    target_id: int,
) -> Optional[str]:
    """CNL method=bot: M_USER admin src+tgt; bot admin src+tgt (+send on target)."""
    bot_doc, err = await assert_bot_owned(user_id, bot_id)
    if err:
        return err
    client, err = await start_bot_client(bot_doc)
    if err:
        return err

    # Selected bot
    e = await check_self_admin(client, source_id, kind="bot", chat_label="source chat")
    if e:
        return e.replace("must be an administrator", "must be an administrator").replace(
            "Selected bot must be an administrator in the source chat.",
            "❌ Selected bot must be an administrator in the source chat.",
        ) if False else (
            "❌ Selected bot must be an administrator in the source chat."
            if "administrator" in (e or "").lower() and "source" in (e or "").lower()
            else e
        )
    e = await check_self_admin(
        client, target_id, kind="bot", chat_label="target chat",
    )
    if e:
        return (
            "❌ Selected bot must be an administrator in the target chat."
            if "administrator" in e.lower() or "present" in e.lower()
            else e
        )
    # Exact privilege hard-fail for channel-style post rights when privileges are present
    m, _ = await fetch_member(client, target_id, "me")
    if m and _status(m) == ChatMemberStatus.ADMINISTRATOR:
        priv = getattr(m, "privileges", None)
        if priv is not None:
            can_post = getattr(priv, "can_post_messages", None)
            can_manage = getattr(priv, "can_manage_chat", None)
            # If Telegram exposes can_post_messages and it is explicitly False, reject
            if can_post is False and can_manage is not True:
                return (
                    "❌ Selected bot does not have the required permission "
                    "`can_post_messages` in the target chat."
                )

    # M_USER via bot client
    e = await check_user_admin(client, source_id, user_id, chat_label="source chat")
    if e:
        return e
    e = await check_user_admin(client, target_id, user_id, chat_label="target chat")
    if e:
        return e
    return None


async def verify_cnl_account_rule(
    user_id: int,
    account_id: str,
    source_id: int,
    target_id: int,
) -> Optional[str]:
    """CNL method=account: M_USER admin target only; account member source, admin target."""
    acc, err = await assert_account_owned(user_id, account_id)
    if err:
        return err
    client, err = await start_account_client(acc)
    if err:
        return err

    e = await check_self_member(client, source_id, kind="account", chat_label="source chat")
    if e:
        return "❌ Selected account must be a member of the source chat." if "member" in e.lower() else e
    e = await check_self_admin(client, target_id, kind="account", chat_label="target chat")
    if e:
        return (
            "❌ Selected account must be an administrator in the target chat."
            if "administrator" in e.lower() or "present" in e.lower()
            else e
        )
    m, _ = await fetch_member(client, target_id, "me")
    if m and _status(m) == ChatMemberStatus.ADMINISTRATOR:
        priv = getattr(m, "privileges", None)
        if priv is not None and getattr(priv, "can_post_messages", None) is False:
            if getattr(priv, "can_manage_chat", None) is not True:
                return (
                    "❌ Selected account does not have the required permission "
                    "`can_post_messages` in the target chat."
                )

    e = await check_user_admin(client, target_id, user_id, chat_label="target chat")
    if e:
        return e
    return None


async def verify_wroxen(
    user_id: int,
    bot_id: str,
    source_id: int,
    target_id: int,
) -> Optional[str]:
    bot_doc, err = await assert_bot_owned(user_id, bot_id)
    if err:
        return err
    client, err = await start_bot_client(bot_doc)
    if err:
        return err

    e = await check_self_admin(client, source_id, kind="bot", chat_label="source chat")
    if e:
        return (
            "❌ Selected bot must be an administrator in the source chat."
            if "administrator" in e.lower() or "present" in e.lower()
            else e
        )
    e = await check_self_admin(client, target_id, kind="bot", chat_label="target chat")
    if e:
        return (
            "❌ Selected bot must be an administrator in the target chat."
            if "administrator" in e.lower() or "present" in e.lower()
            else e
        )
    m, _ = await fetch_member(client, target_id, "me")
    if m and _status(m) == ChatMemberStatus.ADMINISTRATOR:
        priv = getattr(m, "privileges", None)
        if priv is not None and getattr(priv, "can_post_messages", None) is False:
            if getattr(priv, "can_manage_chat", None) is not True:
                return (
                    "❌ Selected bot does not have the required permission "
                    "`can_post_messages` in the target chat."
                )

    e = await check_user_admin(client, source_id, user_id, chat_label="source chat")
    if e:
        return e
    e = await check_user_admin(client, target_id, user_id, chat_label="target chat")
    if e:
        return e
    return None


async def verify_delete_manager(
    user_id: int,
    account_client: Client,
    chat_id: int,
    account_label: str = "account",
) -> Optional[str]:
    """Executor + M_USER both need admin + can_delete_messages. Uses account client."""
    # Executor
    m, err = await fetch_member(account_client, chat_id, "me")
    if err == "not_participant":
        return f"❌ Selected account must be a member of this chat."
    if err:
        return f"❌ Cannot verify selected account ({err})."
    if not is_admin_or_owner(m):
        return "❌ Selected account must be an administrator in this chat."
    if not has_privilege(m, "can_delete_messages"):
        return "❌ Selected account does not have the required permission to delete messages."

    # M_USER
    mu, err = await fetch_member(account_client, chat_id, int(user_id))
    if err == "not_participant":
        return "❌ You must be an administrator or owner of this chat."
    if err:
        return f"❌ Cannot verify your status in this chat ({err})."
    if not is_admin_or_owner(mu):
        return "❌ You must be an administrator or owner of this chat."
    if not has_privilege(mu, "can_delete_messages"):
        return "❌ You do not have the required permission to delete messages."
    return None


async def verify_target_executor(
    user_id: int,
    chat_id: int,
    *,
    bot_id: Optional[str] = None,
    account_id: Optional[str] = None,
) -> Optional[str]:
    """Target chat add: executor admin + M_USER admin. Management bot ignored."""
    if bot_id:
        bot_doc, err = await assert_bot_owned(user_id, bot_id)
        if err:
            return err
        client, err = await start_bot_client(bot_doc)
        if err:
            return err
        e = await check_self_admin(client, chat_id, kind="bot", chat_label="chat")
        if e:
            return (
                "❌ Selected bot must be an administrator in this chat."
                if "administrator" in e.lower() or "present" in e.lower()
                else e
            )
        e = await check_user_admin(client, chat_id, user_id, chat_label="chat")
        if e:
            return e
        return None

    if account_id:
        acc, err = await assert_account_owned(user_id, account_id)
        if err:
            return err
        client, err = await start_account_client(acc)
        if err:
            return err
        e = await check_self_admin(client, chat_id, kind="account", chat_label="chat")
        if e:
            return (
                "❌ Selected account must be an administrator in this chat."
                if "administrator" in e.lower() or "present" in e.lower()
                else e
            )
        e = await check_user_admin(client, chat_id, user_id, chat_label="chat")
        if e:
            return e
        return None

    return "❌ Select a bot or account to verify this chat."


# ── legacy job helpers (kept for existing forward jobs) ─────────────────────

async def check_bot_access_to_source(
    client: Client,
    source_chat_id: Union[int, str],
    is_private: bool = False,
) -> Tuple[bool, str]:
    try:
        chat = await client.get_chat(source_chat_id)
        if getattr(chat, "username", None):
            return True, "Public source accessible"
        m, err = await fetch_member(client, source_chat_id, "me")
        if err or not m:
            return False, "Bot is not a member of the private source"
        if is_admin_or_owner(m):
            return True, "Bot is admin in private source"
        return False, "Bot is not admin in private source channel"
    except Exception as e:
        return False, f"Error accessing source: {type(e).__name__}"


async def check_user_access_to_source(
    client: Client,
    source_chat_id: Union[int, str],
) -> Tuple[bool, str]:
    m, err = await fetch_member(client, source_chat_id, "me")
    if err or not m:
        return False, "User account is not a member of the source"
    if is_member_or_above(m):
        return True, "User account is member of source"
    return False, "User account is not a member of the source"


async def check_admin_in_target(
    client: Client,
    target_chat_id: Union[int, str],
) -> Tuple[bool, str]:
    m, err = await fetch_member(client, target_chat_id, "me")
    if err or not m:
        return False, "Not a member of the target chat"
    if is_admin_or_owner(m):
        return True, "Is admin in target"
    return False, "Not admin in target chat"


async def validate_job_permissions(
    client: Client,
    method: str,
    source_chat_id: Union[int, str],
    target_chat_ids: List[int],
) -> Tuple[bool, str]:
    try:
        source_chat = await client.get_chat(source_chat_id)
        is_private_source = source_chat.username is None
    except Exception as e:
        return False, f"Cannot access source: {type(e).__name__}"

    if method == "bot":
        ok, msg = await check_bot_access_to_source(client, source_chat_id, is_private_source)
        if not ok:
            return False, f"Source access failed: {msg}"
    else:
        ok, msg = await check_user_access_to_source(client, source_chat_id)
        if not ok:
            return False, f"Source access failed: {msg}"

    for target_id in target_chat_ids:
        ok, msg = await check_admin_in_target(client, target_id)
        if not ok:
            return False, f"Target `{target_id}` → {msg}"
    return True, "All permissions OK"



async def validate_job_create_permissions(
    *,
    user_id: int,
    method: str,
    source_chat_id,
    target_chat_ids: list | None = None,
    account_ids: list | None = None,
    bot_id: str | None = None,
    mgmt_client: Client | None = None,
    check_targets: bool = True,
    check_source: bool = True,
) -> tuple[bool, str]:
    """Check ONLY the selected bot / accounts — never a random executor.

    method=user: each selected account member+ on source; admin on each target (if check_targets).
    method=bot: selected bot access on source; admin on targets (if check_targets).
    """
    from core.job_worker import get_user_client, get_bot_client
    from database import get_account, get_bot, AccountStatus

    method = (method or "").lower()
    targets = [int(x) for x in (target_chat_ids or [])]

    if method in ("bot",):
        if not bot_id:
            return False, "Select a forward bot"
        bot = await get_bot(user_id, bot_id)
        if not bot:
            return False, "Selected bot not found"
        bclient = await get_bot_client(bot)
        if not bclient:
            return False, "Could not start selected bot"
        if check_source:
            try:
                schat = await bclient.get_chat(source_chat_id)
                is_private = not bool(getattr(schat, "username", None))
            except Exception as e:
                return False, (
                    f"Selected bot cannot access source (`{type(e).__name__}`). "
                    "Private source → bot must be admin there."
                )
            if is_private:
                ok, msg = await check_bot_access_to_source(bclient, source_chat_id, True)
                if not ok:
                    return False, f"Selected bot on private source: {msg}"
        if check_targets:
            if not targets:
                return False, "Select at least one target"
            for tid in targets:
                ok, msg = await check_admin_in_target(bclient, tid)
                if not ok:
                    return False, f"Selected bot on target `{tid}`: {msg}"
        return True, "OK"

    # user method
    ids = [str(a) for a in (account_ids or [])]
    if not ids:
        return False, "Select at least one user account"
    for aid in ids:
        acc = await get_account(user_id, aid)
        if not acc:
            return False, f"Account `{aid}` not found"
        if (acc.get("status") or "") != AccountStatus.ACTIVE.value:
            return False, f"Account `{aid}` is not active"
        uclient = await get_user_client(acc)
        if not uclient:
            return False, f"Could not start account `{aid}`"
        if check_source:
            ok, msg = await check_user_access_to_source(uclient, source_chat_id)
            if not ok:
                return False, (
                    f"Selected account `{aid}` on source: must be at least a member — {msg}"
                )
        if check_targets:
            if not targets:
                return False, "Select at least one target"
            for tid in targets:
                ok, msg = await check_admin_in_target(uclient, tid)
                if not ok:
                    return False, (
                        f"Selected account `{aid}` on target `{tid}`: must be admin/owner — {msg}"
                    )
    return True, "OK"
