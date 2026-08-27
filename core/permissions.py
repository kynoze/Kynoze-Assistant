# Permission checking helpers

import logging
from typing import Union, Optional, Tuple, List
from pyrogram import Client
from pyrogram.enums import ChatMemberStatus, ChatType
from pyrogram.errors import UserNotParticipant, ChannelPrivate, ChatAdminRequired

logger = logging.getLogger(__name__)


async def check_bot_access_to_source(
    client: Client,
    source_chat_id: Union[int, str],
    is_private: bool = False
) -> Tuple[bool, str]:
    """
    Check if Bot has access to source.
    - Public source → just needs to be able to read
    - Private source → must be Admin
    """
    try:
        chat = await client.get_chat(source_chat_id)
        
        # Public channel
        if chat.username:
            return True, "Public source accessible"

        # Private channel → must be admin
        try:
            member = await client.get_chat_member(source_chat_id, "me")
            if member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
                return True, "Bot is admin in private source"
            else:
                return False, "Bot is not admin in private source channel"
        except UserNotParticipant:
            return False, "Bot is not a member of the private source"
        except Exception as e:
            return False, f"Cannot check bot membership: {e}"

    except ChannelPrivate:
        return False, "Source is private and bot has no access"
    except Exception as e:
        return False, f"Error accessing source: {e}"


async def check_user_access_to_source(
    client: Client,
    source_chat_id: Union[int, str]
) -> Tuple[bool, str]:
    """
    User Account only needs to be a Member of the source.
    """
    try:
        member = await client.get_chat_member(source_chat_id, "me")
        if member.status in [
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER
        ]:
            return True, "User account is member of source"
        else:
            return False, "User account is not a member of the source"
    except UserNotParticipant:
        return False, "User account is not a member of the source"
    except Exception as e:
        return False, f"Error checking user access: {e}"


async def check_admin_in_target(
    client: Client,
    target_chat_id: Union[int, str]
) -> Tuple[bool, str]:
    """
    Bot or User Account must be Admin in Target.
    """
    try:
        member = await client.get_chat_member(target_chat_id, "me")
        if member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
            return True, "Is admin in target"
        else:
            return False, "Not admin in target chat"
    except UserNotParticipant:
        return False, "Not a member of the target chat"
    except ChatAdminRequired:
        return False, "Admin rights required in target"
    except Exception as e:
        return False, f"Error checking target admin: {e}"


async def validate_job_permissions(
    client: Client,
    method: str,
    source_chat_id: Union[int, str],
    target_chat_ids: List[int],
) -> Tuple[bool, str]:
    """
    Full validation before starting a job.
    Returns (is_valid, error_message)
    """
    # 1. Check Source access
    try:
        source_chat = await client.get_chat(source_chat_id)
        is_private_source = source_chat.username is None
    except Exception as e:
        return False, f"Cannot access source: {e}"

    if method == "bot":
        ok, msg = await check_bot_access_to_source(client, source_chat_id, is_private_source)
        if not ok:
            return False, f"Source access failed: {msg}"
    else:  # user
        ok, msg = await check_user_access_to_source(client, source_chat_id)
        if not ok:
            return False, f"Source access failed: {msg}"

    # 2. Check Target admin rights
    for target_id in target_chat_ids:
        ok, msg = await check_admin_in_target(client, target_id)
        if not ok:
            return False, f"Target `{target_id}` → {msg}"

    return True, "All permissions OK"    
