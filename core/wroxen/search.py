"""Search result formatting — metadata + message.link only (no media send)."""

from __future__ import annotations

import hashlib
import time
from html import escape
from typing import Any, Dict, List, Optional, Tuple

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

RESULTS_PER_PAGE = 10
MAX_RESULTS = 200
# in-memory cache: key -> (expires_ts, results, total)
_CACHE: Dict[str, Tuple[float, List[Dict], int]] = {}
CACHE_TTL = 300


def _cache_key(wroxen_id: str, query: str) -> str:
    # Prefix with wroxen_id so we can clear one Wroxen without wiping all caches
    qh = hashlib.md5(query.strip().lower().encode()).hexdigest()
    return f"wx:{wroxen_id}:{qh}"


def get_cached(wroxen_id: str, query: str) -> Optional[Tuple[List[Dict], int]]:
    key = _cache_key(wroxen_id, query)
    item = _CACHE.get(key)
    if not item:
        return None
    exp, results, total = item
    if time.time() > exp:
        _CACHE.pop(key, None)
        return None
    return results, total


def set_cached(wroxen_id: str, query: str, results: List[Dict], total: int) -> None:
    _CACHE[_cache_key(wroxen_id, query)] = (time.time() + CACHE_TTL, results, total)
    # soft bound
    if len(_CACHE) > 2000:
        now = time.time()
        dead = [k for k, v in _CACHE.items() if v[0] < now]
        for k in dead[:500]:
            _CACHE.pop(k, None)



def clear_cache_for_wroxen(wroxen_id: str) -> int:
    """Clear only this Wroxen's search cache (does not touch other Wroxens)."""
    prefix = f"wx:{wroxen_id}:"
    removed = 0
    for k in list(_CACHE.keys()):
        if k.startswith(prefix):
            _CACHE.pop(k, None)
            removed += 1
    return removed


def clear_cache() -> None:
    _CACHE.clear()



def format_result_line(i: int, movie: Dict[str, Any]) -> str:
    title = movie.get("title") or "Unknown"
    year = movie.get("year")
    quality = movie.get("quality")
    print_type = movie.get("print")
    lang = movie.get("lang")
    season = movie.get("season")
    episode = movie.get("episode")
    codec = movie.get("codec")
    link = movie.get("link")

    parts = [str(title)]
    if year:
        parts.append(f"({year})")
    if quality:
        parts.append(str(quality))
    if codec:
        parts.append(str(codec))
    if print_type:
        parts.append(str(print_type))
    if lang:
        parts.append(str(lang))
    if season is not None:
        try:
            parts.append(f"S{int(season):02d}")
        except (TypeError, ValueError):
            parts.append(f"S{season}")
    if episode is not None:
        ep = str(episode)
        if ep.lower() == "complete":
            parts.append("Complete")
        elif ep.lower().startswith("e"):
            parts.append(ep.upper() if len(ep) <= 4 else ep)
        else:
            try:
                parts.append(f"E{int(episode)}")
            except (TypeError, ValueError):
                parts.append(f"E{ep}")

    caption = " ".join(parts)
    line = f"{i}. <b>{escape(caption)}</b>\n"
    if link:
        line += f"🔗 {escape(str(link))}\n"
    return line + "\n"


def build_results_text(
    query: str,
    page: int,
    all_results: List[Dict],
    total: int,
) -> Tuple[str, InlineKeyboardMarkup]:
    pages = max(1, (len(all_results) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE)
    page = max(1, min(page, pages))
    start = (page - 1) * RESULTS_PER_PAGE
    end = start + RESULTS_PER_PAGE
    chunk = all_results[start:end]

    text = (
        f"<b>🔎 Results for:</b> <code>{escape(query)}</code>\n"
        f"📄 Page {page}/{pages} • Total: {total}\n\n"
    )
    for i, movie in enumerate(chunk, start=start + 1):
        text += format_result_line(i, movie)

    buttons = []
    row = []
    # callback: wxpage|{wroxen_id}|{page}|{owner_user_id}|{query_hash}
    # query stored via cache keyed by wroxen+query — pagination uses cache
    return text, pages, page  # type hint flexible


def pagination_keyboard(
    wroxen_id: str,
    query: str,
    page: int,
    pages: int,
    owner_id: int,
) -> Optional[InlineKeyboardMarkup]:
    qhash = hashlib.md5(query.strip().lower().encode()).hexdigest()[:12]
    row = []
    if page > 1:
        row.append(
            InlineKeyboardButton(
                "⬅️ Prev",
                callback_data=f"wxpage:{wroxen_id}:{page - 1}:{owner_id}:{qhash}",
            )
        )
    if page < pages:
        row.append(
            InlineKeyboardButton(
                "Next ➡️",
                callback_data=f"wxpage:{wroxen_id}:{page + 1}:{owner_id}:{qhash}",
            )
        )
    if not row:
        return None
    return InlineKeyboardMarkup([row])


# Map qhash -> query text for pagination (same process)
_QUERY_MAP: Dict[str, str] = {}


def remember_query(query: str) -> str:
    qhash = hashlib.md5(query.strip().lower().encode()).hexdigest()[:12]
    _QUERY_MAP[qhash] = query.strip()
    return qhash


def recall_query(qhash: str) -> Optional[str]:
    return _QUERY_MAP.get(qhash)
