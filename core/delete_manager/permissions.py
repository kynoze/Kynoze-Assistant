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
) -> Tuple[bool, str]:
    """
    Selected UserBot must:
      1. Be in the target group
      2. Be ADMIN or OWNER
      3. Have can_delete_messages (owners always pass)
    """
    try:
        member = await client.get_chat_member(target_chat_id, "me")
    except UserNotParticipant:
        return False, "The selected account is not a member of this group."
    except ChannelPrivate:
        return False, "This group is private and the account has no access."
    except PeerIdInvalid:
        return False, "Invalid or inaccessible group."
    except (UserDeactivated, AuthKeyUnregistered, SessionRevoked) as e:
        logger.warning("Delete Manager session invalid: %s", e)
        return False, "Account session is invalid or disconnected. Re-add the account."
    except Exception as e:
        logger.exception("Permission check failed")
        return False, "Could not check permissions. Try again."

    status = member.status
    if status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        return (
            False,
            "The selected account is not an administrator in this group.",
        )

    if status == ChatMemberStatus.OWNER:
        return True, "Owner — can delete messages"

    priv = getattr(member, "privileges", None)
    if not priv or not getattr(priv, "can_delete_messages", False):
        return (
            False,
            "The selected account is an admin but does not have permission to delete messages.",
        )
    return True, "Admin with delete permission"


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
