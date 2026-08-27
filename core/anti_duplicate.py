from pyrogram.types import Message

from database import is_duplicate, mark_as_forwarded
from core.filters import get_unique_file_id


async def check_and_mark_duplicate(
    user_id: int,
    target_chat_id: int,
    message: Message,
    anti_duplicate_enabled: bool,
) -> bool:
    """True = duplicate, skip send."""
    if not anti_duplicate_enabled:
        return False

    unique_id = get_unique_file_id(message)
    if not unique_id:
        return False

    if await is_duplicate(user_id, target_chat_id, unique_id):
        return True

    await mark_as_forwarded(user_id, target_chat_id, unique_id)
    return False
