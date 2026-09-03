"""Operation-level filters (Job / Quick Forward) — independent of target settings.

Default media: video + document only.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

DEFAULT_MEDIA_TYPES = ["video", "document"]

ALL_MEDIA_TYPES = [
    "video",
    "document",
    "photo",
    "audio",
    "animation",
    "voice",
    "video_note",
    "sticker",
    "text",
]


def default_op_filters() -> Dict[str, Any]:
    return {
        "media_types": list(DEFAULT_MEDIA_TYPES),
        "block_enabled": False,
        "block_words": [],
        "whitelist_enabled": False,
        "whitelist_words": [],
    }


def normalize_op_filters(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    base = default_op_filters()
    if not isinstance(raw, dict):
        return base
    mt = raw.get("media_types")
    if isinstance(mt, list) and mt:
        base["media_types"] = [str(x) for x in mt if x]
    else:
        base["media_types"] = list(DEFAULT_MEDIA_TYPES)
    base["block_enabled"] = bool(raw.get("block_enabled", False))
    base["block_words"] = [str(w) for w in (raw.get("block_words") or []) if w]
    base["whitelist_enabled"] = bool(
        raw.get("whitelist_enabled", raw.get("whitelist_mode", False))
    )
    wl = raw.get("whitelist_words")
    if wl is None:
        wl = raw.get("whitelist") or []
    base["whitelist_words"] = [str(w) for w in wl if w]
    return base


def merge_settings_for_forward(
    target_settings: Optional[Dict[str, Any]],
    op_filters: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Target settings as base; operation filters OVERRIDE media/block/whitelist layers.

    Target anti-duplicate / caption / buttons stay from target.
    """
    settings = dict(target_settings or {})
    op = normalize_op_filters(op_filters)
    # Operation media types take precedence when op_filters provided
    if op_filters is not None:
        settings["media_types"] = list(op["media_types"])
        settings["block_words_enabled"] = bool(op["block_enabled"])
        settings["block_words"] = list(op["block_words"])
        settings["whitelist_mode"] = bool(op["whitelist_enabled"])
        settings["whitelist"] = list(op["whitelist_words"])
    return settings
