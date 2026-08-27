"""Media title/metadata extraction for Wroxen — PTT (parsett) + manual lang/quality.

Uses tested extract_details logic with `from PTT import parse_title`.
Install: pip install parsett
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

try:
    from PTT import parse_title
    _HAS_PTT = True
except ImportError:  # pragma: no cover
    parse_title = None  # type: ignore
    _HAS_PTT = False
    logger.warning("parsett (PTT) not installed — pip install parsett")


def extract_details(caption: Optional[str]) -> Dict[str, Any]:
    if not caption or len(caption.strip()) < 2:
        return {}

    data: Dict[str, Any] = {}
    if _HAS_PTT and parse_title is not None:
        try:
            data = parse_title(caption, translate_languages=True) or {}
        except Exception as e:
            logger.debug("PTT parse error: %s", e)
            data = {}
    else:
        data = {}

    # ---------- Manual Language Extraction ----------
    lang_match = re.search(
        r"\[([^\]]*?(?:Hin|Hindi|Tam|Tamil|Tel|Telugu|Eng|English|Kan|Kannada|"
        r"Mal|Malayalam|Beng|Bengali|Mar|Marathi)[^\]]*?)\]",
        caption,
        re.IGNORECASE,
    )
    lang = None
    if lang_match:
        raw_lang = lang_match.group(1)
        raw_lang = re.sub(r"['\"\[\]\(\)]", "", raw_lang).strip()
        langs = re.split(r"[+,/&\-]", raw_lang)
        langs = [x.strip().capitalize() for x in langs if x.strip()]
        lang = ", ".join(sorted(set(langs)))
    if lang:
        lang = f"[{lang}]"

    # ---------- Manual Quality (Resolution) Extraction ----------
    quality = None
    q_match = re.search(
        r"(2160p|1440p|1080p|720p|480p|360p|4K|8K)", caption, re.IGNORECASE
    )
    if q_match:
        quality = q_match.group(1).upper().replace("P", "p")

    # ---------- Other Fields from PTT ----------
    title = data.get("title")
    year = data.get("year")
    codec = data.get("codec")
    codec = codec.upper() if isinstance(codec, str) else None
    print_type = data.get("quality") or data.get("source")

    # ---------- Season & Episode ----------
    seasons = data.get("seasons") or []
    season = seasons[0] if seasons else None

    episodes = data.get("episodes") or []
    episode = None
    caption_lower = caption.lower()

    if "complete" in caption_lower:
        episode = "Complete"
    elif episodes:
        episode = (
            f"{episodes[0]}-{episodes[-1]}"
            if len(episodes) > 1
            else str(episodes[0])
        )
    else:
        ep_match = re.search(
            r"(?:E|Ep|Episode)\s*(\d{1,3})(?:\s*(?:-|to)\s*(\d{1,3}))?",
            caption,
            re.IGNORECASE,
        )
        if ep_match:
            start, end = ep_match.groups()
            episode = f"{start}-{end}" if end else start

    # If PTT missing title, rough first-line fallback so indexing still works
    if not title:
        first = caption.strip().split("\n", 1)[0][:120].strip()
        title = first or None

    return {
        "title": title,
        "year": year,
        "quality": quality,
        "lang": lang,
        "print": print_type,
        "season": season,
        "episode": episode,
        "codec": codec,
    }


def build_message_link(
    chat_id: int, message_id: int, username: Optional[str] = None
) -> str:
    """Build t.me link for a message (public username or private c/ form)."""
    if username:
        return f"https://t.me/{username}/{message_id}"
    s = str(chat_id)
    if s.startswith("-100"):
        return f"https://t.me/c/{s[4:]}/{message_id}"
    if s.startswith("-"):
        return f"https://t.me/c/{s.lstrip('-')}/{message_id}"
    return f"https://t.me/c/{s}/{message_id}"
