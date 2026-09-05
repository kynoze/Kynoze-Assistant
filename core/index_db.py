"""Separate Index MongoDB — media index only.

Uses PyMongo Async API (AsyncMongoClient). Never logs or returns full URIs.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

logger = logging.getLogger(__name__)

from core.dns_fix import apply_termux_dns_fix

# Per-user live connections: user_id -> (client, db, collection)
_INDEX_CLIENTS: Dict[int, tuple] = {}

SUPPORTED_MEDIA_TYPES = (
    "video",
    "document",
    "photo",
    "audio",
    "animation",
    "voice",
    "video_note",
)


def mask_uri(uri: Optional[str]) -> str:
    """Show only scheme + host hint, never credentials."""
    if not uri:
        return "Not set"
    try:
        # mongodb+srv://user:pass@host/...  or mongodb://...
        if "@" in uri:
            after = uri.split("@", 1)[1]
            host = after.split("/", 1)[0]
            return f"••••@{host}"
        if "://" in uri:
            rest = uri.split("://", 1)[1]
            host = rest.split("/", 1)[0]
            return f"••••@{host}" if host else "••••"
        return "••••"
    except Exception:
        return "••••"


async def test_index_uri(uri: str, timeout_ms: int = 5000) -> Tuple[bool, str]:
    """Validate MongoDB URI with ping. Returns (ok, message). Never echo URI."""
    apply_termux_dns_fix()
    if not uri or not isinstance(uri, str):
        return False, "Empty URI"
    uri = uri.strip()
    if not (uri.startswith("mongodb://") or uri.startswith("mongodb+srv://")):
        return False, "URI must start with mongodb:// or mongodb+srv://"
    client = None
    try:
        client = AsyncMongoClient(uri, serverSelectionTimeoutMS=timeout_ms, maxPoolSize=5, minPoolSize=0)
        await client.admin.command("ping")
        return True, "Connected"
    except Exception as e:
        err = type(e).__name__
        return False, f"Connection failed ({err})"
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass


async def connect_index_db(user_id: int, uri: str, db_name: str = "IndexDB") -> Tuple[bool, str]:
    """Open (or replace) Index DB connection for user. Creates indexes."""
    apply_termux_dns_fix()
    await disconnect_index_db(user_id)
    uri = (uri or "").strip()
    ok, msg = await test_index_uri(uri)
    if not ok:
        return False, msg
    try:
        client = AsyncMongoClient(uri, serverSelectionTimeoutMS=15000, maxPoolSize=10, minPoolSize=0, maxIdleTimeMS=45000)
        await client.admin.command("ping")
        # Prefer DB name from URI path if present, else IndexDB
        try:
            from urllib.parse import urlparse
            parsed = urlparse(uri)
            path = (parsed.path or "").lstrip("/")
            if path and "/" not in path:
                db_name = path.split("?")[0] or db_name
        except Exception:
            pass
        mdb = client[db_name]
        col = mdb["indexed_media"]
        await col.create_index(
            [("user_id", ASCENDING), ("file_unique_id", ASCENDING)],
            unique=True,
            name="uniq_user_file",
        )
        await col.create_index(
            [("user_id", ASCENDING), ("index_bot_id", ASCENDING)],
            name="user_bot",
        )
        await col.create_index(
            [("user_id", ASCENDING), ("indexed_at", DESCENDING)],
            name="user_time",
        )
        await col.create_index(
            [("user_id", ASCENDING), ("media_type", ASCENDING)],
            name="user_type",
        )
        _INDEX_CLIENTS[user_id] = (client, mdb, col)
        return True, "Connected"
    except Exception as e:
        logger.exception("Index DB connect failed for user %s", user_id)
        return False, f"Connect error ({type(e).__name__})"


async def disconnect_index_db(user_id: int) -> None:
    entry = _INDEX_CLIENTS.pop(user_id, None)
    if not entry:
        return
    client, _, _ = entry
    try:
        await client.close()
    except Exception:
        pass


def get_index_collection(user_id: int):
    entry = _INDEX_CLIENTS.get(user_id)
    if not entry:
        return None
    return entry[2]


async def ensure_index_connected(user_id: int, uri: Optional[str]) -> Tuple[bool, str]:
    """Reconnect if needed using stored URI."""
    if user_id in _INDEX_CLIENTS:
        try:
            await _INDEX_CLIENTS[user_id][0].admin.command("ping")
            return True, "Connected"
        except Exception:
            await disconnect_index_db(user_id)
    if not uri:
        return False, "Not configured"
    return await connect_index_db(user_id, uri)


async def save_indexed_media(
    user_id: int,
    index_bot_id: str,
    source_chat_id: int,
    source_message_id: int,
    media_type: str,
    file_id: str,
    file_unique_id: str,
    caption: Optional[str] = None,
) -> str:
    """
    Insert one indexed media doc.
    Returns: 'suc' | 'dup' | 'err'
    """
    col = get_index_collection(user_id)
    if col is None:
        return "err"
    if not file_unique_id or not file_id:
        return "err"
    doc = {
        "user_id": user_id,
        "index_bot_id": index_bot_id,
        "source_chat_id": source_chat_id,
        "source_message_id": source_message_id,
        "media_type": media_type,
        "file_id": file_id,
        "file_unique_id": file_unique_id,
        "caption": caption,
        "indexed_at": datetime.now(timezone.utc),
    }
    try:
        await col.insert_one(doc)
        return "suc"
    except DuplicateKeyError:
        return "dup"
    except PyMongoError:
        logger.exception("Index insert failed")
        return "err"


async def count_indexed(user_id: int, index_bot_id: Optional[str] = None) -> int:
    col = get_index_collection(user_id)
    if col is None:
        return 0
    q: Dict[str, Any] = {"user_id": user_id}
    if index_bot_id:
        q["index_bot_id"] = index_bot_id
    try:
        return await col.count_documents(q)
    except Exception:
        return 0


async def stats_by_type(user_id: int, index_bot_id: Optional[str] = None) -> Dict[str, int]:
    col = get_index_collection(user_id)
    out = {t: 0 for t in SUPPORTED_MEDIA_TYPES}
    out["total"] = 0
    if col is None:
        return out
    match: Dict[str, Any] = {"user_id": user_id}
    if index_bot_id:
        match["index_bot_id"] = index_bot_id
    try:
        pipeline = [
            {"$match": match},
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
        logger.exception("Index stats failed")
    return out


async def fetch_indexed_batch(
    user_id: int,
    index_bot_id: str,
    limit: int,
) -> List[Dict[str, Any]]:
    """Oldest first — FIFO for forward."""
    col = get_index_collection(user_id)
    if col is None or limit <= 0:
        return []
    try:
        cursor = (
            col.find({"user_id": user_id, "index_bot_id": index_bot_id})
            .sort("indexed_at", ASCENDING)
            .limit(limit)
        )
        return await cursor.to_list(length=None)
    except Exception:
        logger.exception("fetch_indexed_batch failed")
        return []


async def delete_indexed_by_ids(user_id: int, object_ids: List[Any]) -> int:
    """Delete only given _id list. Returns deleted count."""
    col = get_index_collection(user_id)
    if col is None or not object_ids:
        return 0
    try:
        res = await col.delete_many({"user_id": user_id, "_id": {"$in": object_ids}})
        return res.deleted_count
    except Exception:
        logger.exception("delete_indexed_by_ids failed")
        return 0


async def clear_all_indexed(user_id: int) -> int:
    """Clear only this user's indexed_media. Returns deleted count."""
    col = get_index_collection(user_id)
    if col is None:
        return 0
    try:
        res = await col.delete_many({"user_id": user_id})
        return res.deleted_count
    except Exception:
        logger.exception("clear_all_indexed failed")
        return 0
