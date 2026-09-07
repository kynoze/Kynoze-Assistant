from typing import Any, Dict, Optional

from pyrogram.types import Message


def should_process_message(message: Message, settings: Dict[str, Any]) -> tuple[bool, str]:
    if getattr(message, "empty", False):
        return False, "deleted"

    allowed_media = list(settings.get("media_types") or [])
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
    """Extract file_unique_id from any media (Pyrogram/Kurigram safe)."""
    if not message or getattr(message, "empty", False):
        return None

    def _from_obj(obj) -> Optional[str]:
        if obj is None:
            return None
        # list/tuple of PhotoSize
        if isinstance(obj, (list, tuple)):
            for item in reversed(list(obj)):
                u = _from_obj(item)
                if u:
                    return u
            return None
        u = getattr(obj, "file_unique_id", None)
        if u:
            return str(u)
        # Photo container with .sizes
        sizes = getattr(obj, "sizes", None)
        if sizes:
            return _from_obj(sizes)
        # document nested
        doc = getattr(obj, "document", None)
        if doc is not None and doc is not obj:
            return _from_obj(doc)
        return None

    for attr in (
        "document",
        "video",
        "photo",
        "audio",
        "animation",
        "voice",
        "video_note",
        "sticker",
    ):
        u = _from_obj(getattr(message, attr, None))
        if u:
            return u

    media_enum = getattr(message, "media", None)
    if media_enum is not None:
        key = getattr(media_enum, "value", None) or str(media_enum)
        if isinstance(key, str):
            key = key.split(".")[-1].lower()
        u = _from_obj(getattr(message, key, None))
        if u:
            return u
    return None
