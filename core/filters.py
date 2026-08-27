from typing import Any, Dict, Optional

from pyrogram.types import Message


def should_process_message(message: Message, settings: Dict[str, Any]) -> tuple[bool, str]:
    if getattr(message, "empty", False):
        return False, "deleted"

    allowed_media = list(settings.get("media_types") or [])
    # Empty list used to block EVERYTHING (including text). Treat empty as "allow all".
    allow_all = len(allowed_media) == 0

    if message.media:
        media_type = message.media.value
        if not allow_all and media_type not in allowed_media:
            return False, f"media_type:{media_type}"
    else:
        if not allow_all and "text" not in allowed_media:
            return False, "media_type:text"

    text_content = message.caption or message.text or ""
    text_lower = text_content.lower()

    # block_words_enabled is independent of the stored list (list is never deleted on OFF)
    if settings.get("block_words_enabled", True):
        block_words = settings.get("block_words", []) or []
        if block_words and text_lower:
            for word in block_words:
                if word and word.lower() in text_lower:
                    return False, f"blocked_word:{word}"

    if settings.get("whitelist_mode", False):
        whitelist = settings.get("whitelist", []) or []
        if not whitelist:
            return False, "whitelist_empty"
        if not text_lower:
            return False, "whitelist_no_text"
        if not any(w.lower() in text_lower for w in whitelist if w):
            return False, "whitelist_miss"

    return True, "ok"


def get_unique_file_id(message: Message) -> Optional[str]:
    if not message.media:
        return None
    media = getattr(message, message.media.value, None)
    if media and hasattr(media, "file_unique_id"):
        return media.file_unique_id
    return None
