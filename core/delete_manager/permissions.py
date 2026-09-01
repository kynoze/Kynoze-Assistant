"""Live Telegram permission checks for Delete Manager.

Never trust cached DB flags — always query Telegram before a job starts.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from pyrogram import Client
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import (
    AuthKeyUnregistered,
    ChannelPrivate,
    ChatAdminRequired,
    PeerIdInvalid,
    SessionRevoked,
    UserDeactivated,
    UserNotParticipant,
)

logger = logging.getLogger(__name__)


async def check_delete_permissions(
    client: Client,
    target_chat_id,
    account_label: str = "account",
    m_user_id: int = None,
) -> Tuple[bool, str]:
    """
    Selected UserBot must be admin with can_delete_messages.
    If m_user_id is provided, M_USER must also be admin with can_delete_messages.
    Management Bot admin is NOT required.
    """
    try:
        from core.permissions import verify_delete_manager, fetch_member, is_admin_or_owner, has_privilege
        if m_user_id is not None:
            err = await verify_delete_manager(int(m_user_id), client, int(target_chat_id), account_label)
            if err:
                return False, err.lstrip("❌ ").strip()
            return True, "Account + user OK"
        # executor only (legacy)
        member, err = await fetch_member(client, target_chat_id, "me")
        if err == "not_participant":
            return False, "The selected account is not a member of this group."
        if err:
            return False, "Could not check permissions. Try again."
        if not is_admin_or_owner(member):
            return False, "The selected account is not an administrator in this group."
        if not has_privilege(member, "can_delete_messages"):
            return False, "The selected account is an admin but does not have permission to delete messages."
        return True, "Admin with delete permission"
    except (UserDeactivated, AuthKeyUnregistered, SessionRevoked) as e:
        logger.warning("Delete Manager session invalid: %s", e)
        return False, "Account session is invalid or disconnected. Re-add the account."
    except Exception:
        logger.exception("Permission check failed")
        return False, "Could not check permissions. Try again."


def permission_fail_text(
    account_name: str,
    group_title: str,
    reason: str,
) -> str:
    return (
        "❌ **Cannot start deletion.**\n\n"
        f"**Selected account:** {account_name}\n"
        f"**Group:** {group_title}\n\n"
        f"**Reason:** {reason}"
    )
