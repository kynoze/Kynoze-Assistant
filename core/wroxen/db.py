"""Wroxen MongoDB layer — PyMongo Async API (AsyncMongoClient).

No Motor. Operations are awaited.
"""


from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from pymongo import ASCENDING, TEXT, AsyncMongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

logger = logging.getLogger(__name__)

# user_id -> (client, db, media collection)
_CLIENTS: Dict[int, tuple] = {}


def mask_uri(uri: Optional[str]) -> str:
    if not uri:
        return "Not set"
    try:
        if "@" in uri:
            return f"••••@{uri.split('@', 1)[1].split('/', 1)[0]}"
        if "://" in uri:
            return f"••••@{uri.split('://', 1)[1].split('/', 1)[0]}"
        return "••••"
    except Exception:
        return "••••"


def _normalize_uri(uri: str) -> str:
    u = (uri or "").strip()
    for ch in ("`", '"', "'", "<", ">"):
        u = u.strip(ch)
    u = u.strip().replace("\u200b", "").replace("\xa0", " ").strip()
    return u


def _apply_dns_fix() -> None:
    from core.dns_fix import apply_termux_dns_fix
    apply_termux_dns_fix()


def _friendly_mongo_error(exc: BaseException) -> str:
    name = type(exc).__name__
    msg = str(exc) or ""
    low = msg.lower()
    if "dnspython" in low or ("mongodb+srv" in low and "dns" in low):
        return (
            "dnspython missing for mongodb+srv. "
            "pip install dnspython — or use mongodb:// host-list URI"
        )
    if name == "ConfigurationError":
        if any(x in low for x in ("password", "username", "escape", "percent")):
            return (
                "URI config error — encode special password chars "
                "(@→%40, #→%23, :→%3A, /→%2F)"
            )
        return f"ConfigurationError: {msg[:120]}" if msg else "ConfigurationError"
    if name in ("ServerSelectionTimeoutError", "NetworkTimeout"):
        return "Timeout — check network / Atlas IP allowlist / firewall"
    if "authentication failed" in low:
        return "Auth failed — wrong user/password"
    if "ssl" in low or "tls" in low or "certificate" in low:
        return f"TLS/SSL error: {msg[:100]}"
    return f"{name}: {msg[:120]}" if msg else name


def _db_name_from_uri(uri: str, default: str = "WroxenDB") -> str:
    try:
        path = (urlparse(uri).path or "").lstrip("/")
        if path and "/" not in path:
            return path.split("?")[0] or default
    except Exception:
        pass
    return default


async def test_uri(uri: str, timeout_ms: int = 8000) -> Tuple[bool, str]:
    """Validate MongoDB URI with ping (PyMongo AsyncMongoClient)."""
    uri = _normalize_uri(uri)
    if not (uri.startswith("mongodb://") or uri.startswith("mongodb+srv://")):
        return False, "URI must start with mongodb:// or mongodb+srv://"
    _apply_dns_fix()
    client = None
    try:
        client = AsyncMongoClient(
            uri,
            maxPoolSize=15,
            minPoolSize=0,
            maxIdleTimeMS=45000,
            serverSelectionTimeoutMS=timeout_ms,
            connectTimeoutMS=timeout_ms,
        )
        await client.admin.command("ping")
        return True, "Connected"
    except Exception as e:
        logger.warning("Wroxen test_uri failed: %s: %s", type(e).__name__, e)
        return False, _friendly_mongo_error(e)
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass


async def connect(user_id: int, uri: str) -> Tuple[bool, str]:
    await disconnect(user_id)
    uri = _normalize_uri(uri)
    ok, msg = await test_uri(uri)
    if not ok:
        return False, msg
    try:
        _apply_dns_fix()
        client = AsyncMongoClient(
            uri,
            serverSelectionTimeoutMS=8000,
            connectTimeoutMS=8000,
        )
        await client.admin.command("ping")
        mdb = client[_db_name_from_uri(uri)]
        col = mdb["wroxen_media"]
        await col.create_index(
            [("wroxen_id", ASCENDING), ("file_unique_id", ASCENDING)],
            unique=True,
            name="uniq_wroxen_file",
            background=True,
        )
        try:
            await col.create_index(
                [("title", TEXT), ("caption", TEXT), ("codec", TEXT)],
                name="wroxen_text",
                default_language="english",
                weights={"title": 5, "caption": 1, "codec": 2},
                background=True,
            )
        except Exception as e:
            logger.debug("wroxen text index: %s", e)
        await col.create_index(
            [("wroxen_id", ASCENDING), ("media_type", ASCENDING)],
            name="wroxen_type",
            background=True,
        )
        await col.create_index(
            [("wroxen_id", ASCENDING), ("indexed_at", ASCENDING)],
            name="wroxen_time",
            background=True,
        )
        # Speed up S/E filtered search (e.g. "Show S01E05")
        try:
            await col.create_index(
                [("wroxen_id", ASCENDING), ("season", ASCENDING), ("episode", ASCENDING)],
                name="wroxen_se",
                background=True,
            )
        except Exception as e:
            logger.debug("wroxen se index: %s", e)
        _CLIENTS[user_id] = (client, mdb, col)
        return True, "Connected"
    except Exception as e:
        logger.exception("Wroxen DB connect failed user=%s", user_id)
        return False, _friendly_mongo_error(e)


async def disconnect(user_id: int) -> None:
    entry = _CLIENTS.pop(user_id, None)
    if not entry:
        return
    client, _, _ = entry
    try:
        await client.close()
    except Exception:
        pass


async def ensure_connected(user_id: int, uri: Optional[str]) -> Tuple[bool, str]:
    if user_id in _CLIENTS:
        try:
            await _CLIENTS[user_id][0].admin.command("ping")
            return True, "Connected"
        except Exception:
            await disconnect(user_id)
    if not uri:
        return False, "Not configured"
    return await connect(user_id, uri)


def _col(user_id: int):
    entry = _CLIENTS.get(user_id)
    return entry[2] if entry else None


async def save_media(
    user_id: int,
    *,
    wroxen_id: str,
    source_chat_id: int,
    message_id: int,
    link: str,
    media_type: str,
    file_unique_id: str,
    caption: Optional[str],
    title: Optional[str] = None,
    year: Optional[int] = None,
    quality: Optional[str] = None,
    lang: Optional[str] = None,
    print_type: Optional[str] = None,
    season: Any = None,
    episode: Any = None,
    codec: Optional[str] = None,
) -> str:
    col = _col(user_id)
    if col is None or not file_unique_id:
        return "error"
    doc = {
        "wroxen_id": wroxen_id,
        "source_chat_id": int(source_chat_id),
        "message_id": int(message_id),
        "link": link,
        "media_type": media_type,
        "file_unique_id": file_unique_id,
        "caption": caption,
        "title": title.strip() if isinstance(title, str) else title,
        "year": int(year) if year else None,
        "quality": quality,
        "lang": lang,
        "print": print_type,
        "season": season,
        "episode": episode,
        "codec": codec.strip() if isinstance(codec, str) else codec,
        "indexed_at": datetime.now(timezone.utc),
    }
    clean = {k: v for k, v in doc.items() if v is not None}
    try:
        await col.insert_one(clean)
        return "saved"
    except DuplicateKeyError:
        return "duplicate"
    except PyMongoError:
        logger.exception("wroxen save failed")
        return "error"


async def search_media(
    user_id: int,
    wroxen_id: str,
    query: str,
    *,
    limit: int = 200,
) -> Dict[str, Any]:
    """Search with strict ranking: movies ≈ exact title; series ordered S/E.

    - Token + year filters applied in Python so "War 2 2025" does not
      match "Beast of War" or "Outer Banks ... 2.0".
    - Series queries ("Flash", "Flash S02") sort by season then episode.
    - No count_documents (slow); total = ranked result count.
    """
    col = _col(user_id)
    if col is None or not (query or "").strip():
        return {"results": [], "total": 0}

    raw = query.strip()
    limit = max(1, min(int(limit or 200), 300))
    fetch_n = min(500, max(limit * 4, 80))

    season_f, episode_f, text_q = _parse_season_episode_query(raw)
    year_f, text_q = _parse_year_query(text_q)
    tokens = _query_tokens(text_q)

    base: Dict[str, Any] = {"wroxen_id": wroxen_id}
    _apply_se_filters(base, season_f, episode_f)
    if year_f is not None:
        base["year"] = {"$in": [year_f, str(year_f)]}

    projection = {
        "title": 1,
        "year": 1,
        "quality": 1,
        "lang": 1,
        "print": 1,
        "codec": 1,
        "season": 1,
        "episode": 1,
        "caption": 1,
        "link": 1,
    }

    candidates: List[Dict[str, Any]] = []

    # Broad candidate fetch (text index), then strict rank in Python
    search_text = text_q or raw
    if search_text:
        try:
            filt: Dict[str, Any] = {**base, "$text": {"$search": search_text}}
            proj = {**projection, "score": {"$meta": "textScore"}}
            cursor = (
                col.find(filt, proj)
                .sort([("score", {"$meta": "textScore"})])
                .limit(fetch_n)
            )
            candidates = await cursor.to_list(length=None)
        except Exception as e:
            logger.debug("text candidate fetch failed: %s", e)
            candidates = []

    if not candidates and tokens:
        # Title-anchored regex: all tokens must appear in title (not caption)
        and_parts = []
        for w in tokens[:6]:
            safe = re.escape(w)
            and_parts.append({"title": {"$regex": safe, "$options": "i"}})
        fb = {**base, "$and": and_parts} if and_parts else base
        try:
            cursor = col.find(fb, projection).limit(fetch_n)
            candidates = await cursor.to_list(length=None)
        except Exception:
            logger.exception("title regex candidate fetch failed")
            candidates = []

    if not candidates and (season_f is not None or episode_f is not None or year_f is not None):
        try:
            cursor = (
                col.find(base, projection)
                .sort([("season", ASCENDING), ("episode", ASCENDING)])
                .limit(fetch_n)
            )
            candidates = await cursor.to_list(length=None)
        except Exception:
            candidates = []

    ranked = _rank_and_filter_results(
        candidates,
        tokens=tokens,
        year=year_f,
        season=season_f,
        episode=episode_f,
        phrase=text_q or "",
    )
    results = ranked[:limit]
    return {"results": results, "total": len(ranked)}


def _parse_year_query(q: str):
    """Pull a 19xx/20xx year out of the remaining query text."""
    if not q:
        return None, q
    m = re.search(r"\b((?:19|20)\d{2})\b", q)
    if not m:
        return None, q
    year = int(m.group(1))
    rest = (q[: m.start()] + " " + q[m.end() :]).strip()
    rest = re.sub(r"\s+", " ", rest).strip()
    return year, rest


def _query_tokens(q: str) -> List[str]:
    if not q:
        return []
    # Keep short tokens like "2" (War 2) — important for exact movie match
    parts = re.split(r"\s+", q.strip().lower())
    out = []
    for p in parts:
        p = p.strip(".-_|")
        if not p:
            continue
        if len(p) == 1 and not p.isdigit():
            continue
        out.append(p)
    return out[:10]


def _norm_title(t: Any) -> str:
    s = str(t or "").lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _episode_sort_key(ep: Any):
    if ep is None:
        return (1, 0, "")
    if isinstance(ep, int):
        return (0, ep, "")
    s = str(ep).strip()
    low = s.lower()
    if low == "complete":
        return (0, 9999, "")
    m = re.search(r"(\d+)", s)
    if m:
        return (0, int(m.group(1)), s)
    return (0, 0, s)


def _season_sort_key(se: Any):
    if se is None:
        return (1, 0)
    try:
        return (0, int(se))
    except (TypeError, ValueError):
        m = re.search(r"(\d+)", str(se))
        return (0, int(m.group(1))) if m else (0, 0)


def _rank_and_filter_results(
    docs: List[Dict[str, Any]],
    *,
    tokens: List[str],
    year: Optional[int],
    season,
    episode,
    phrase: str,
) -> List[Dict[str, Any]]:
    """Reject weak matches; score exact/phrase title hits highest."""
    phrase_n = _norm_title(phrase)
    ranked: List[Tuple[tuple, Dict[str, Any]]] = []

    for doc in docs:
        title_n = _norm_title(doc.get("title"))
        if not title_n:
            # Fall back to caption first line only for scoring presence
            cap = str(doc.get("caption") or "").split("\n", 1)[0]
            title_n = _norm_title(cap)
        if not title_n:
            continue

        # Year must match when user asked for a year
        if year is not None:
            dy = doc.get("year")
            try:
                if dy is None or int(dy) != year:
                    continue
            except (TypeError, ValueError):
                continue

        # Every query token must appear in TITLE (not caption / "2.0" audio tags)
        if tokens:
            ok = True
            for tok in tokens:
                # whole-token style: digit or word boundary in normalized title
                if tok.isdigit():
                    if not re.search(rf"(?:^|\s){re.escape(tok)}(?:\s|$)", title_n):
                        ok = False
                        break
                else:
                    if tok not in title_n.split() and tok not in title_n:
                        # require contiguous substring at least
                        if tok not in title_n:
                            ok = False
                            break
            if not ok:
                continue

        score = 0
        # Exact title == phrase
        if phrase_n and title_n == phrase_n:
            score += 200
        elif phrase_n and title_n.startswith(phrase_n + " "):
            score += 150
        elif phrase_n and phrase_n in title_n:
            score += 100
        else:
            score += 20

        # Prefer titles that start with the first token (series name)
        if tokens and title_n.startswith(tokens[0]):
            score += 40

        # Series docs (have season/episode) slightly boosted when user searched S/E
        has_se = doc.get("season") is not None or doc.get("episode") is not None
        if season is not None or episode is not None:
            if has_se:
                score += 30
        elif not has_se:
            # Movie query — prefer non-episode rows
            score += 10

        sort_key = (
            -score,
            _season_sort_key(doc.get("season")),
            _episode_sort_key(doc.get("episode")),
            title_n,
        )
        ranked.append((sort_key, doc))

    ranked.sort(key=lambda x: x[0])
    return [d for _, d in ranked]


def _parse_season_episode_query(q: str):
    """Extract S/E from query; return (season|None, episode|None, remaining_text)."""
    season = None
    episode = None
    rest = q

    m = re.search(r"\bS(\d{1,2})\s*E(\d{1,3})\b", rest, re.I)
    if m:
        season, episode = int(m.group(1)), int(m.group(2))
        rest = (rest[: m.start()] + " " + rest[m.end() :]).strip()
    else:
        m = re.search(r"\b(\d{1,2})x(\d{1,3})\b", rest, re.I)
        if m:
            season, episode = int(m.group(1)), int(m.group(2))
            rest = (rest[: m.start()] + " " + rest[m.end() :]).strip()
        else:
            m = re.search(r"\bS(?:eason)?\s*(\d{1,2})\b", rest, re.I)
            if m:
                season = int(m.group(1))
                rest = (rest[: m.start()] + " " + rest[m.end() :]).strip()
            m = re.search(r"\bE(?:p|pisode)?\s*(\d{1,3})\b", rest, re.I)
            if m:
                try:
                    episode = int(m.group(1))
                except Exception:
                    episode = m.group(1)
                rest = (rest[: m.start()] + " " + rest[m.end() :]).strip()

    rest = re.sub(r"\s+", " ", rest).strip()
    return season, episode, rest


def _apply_se_filters(base: Dict[str, Any], season, episode) -> None:
    """Match season/episode whether stored as int or string."""
    if season is not None:
        base["season"] = {"$in": [season, str(season), f"{season:02d}"]}
    if episode is not None:
        variants = [episode, str(episode)]
        if isinstance(episode, int):
            variants.append(f"{episode:02d}")
            variants.append(f"E{episode}")
            variants.append(f"E{episode:02d}")
        base["episode"] = {"$in": variants}


async def count_media(user_id: int, wroxen_id: str) -> int:
    col = _col(user_id)
    if col is None:
        return 0
    try:
        return await col.count_documents({"wroxen_id": wroxen_id})
    except Exception:
        return 0


async def stats_by_type(user_id: int, wroxen_id: str) -> Dict[str, int]:
    col = _col(user_id)
    out: Dict[str, int] = {"total": 0}
    if col is None:
        return out
    try:
        pipeline = [
            {"$match": {"wroxen_id": wroxen_id}},
            {"$group": {"_id": "$media_type", "n": {"$sum": 1}}},
        ]
        total = 0
        async for row in await col.aggregate(pipeline):
            t = row.get("_id") or "unknown"
            n = int(row.get("n") or 0)
            out[t] = n
            total += n
        out["total"] = total
    except Exception:
        logger.exception("wroxen stats failed")
    return out


async def clear_media(user_id: int, wroxen_id: str) -> int:
    col = _col(user_id)
    if col is None:
        return 0
    try:
        res = await col.delete_many({"wroxen_id": wroxen_id})
        return res.deleted_count
    except Exception:
        logger.exception("wroxen clear failed")
        return 0


async def last_indexed_message_id(user_id: int, wroxen_id: str) -> Optional[int]:
    col = _col(user_id)
    if col is None:
        return None
    try:
        doc = await col.find_one(
            {"wroxen_id": wroxen_id},
            sort=[("message_id", -1)],
            projection={"message_id": 1},
        )
        return int(doc["message_id"]) if doc else None
    except Exception:
        return None
