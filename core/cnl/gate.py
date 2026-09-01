"""CNL gate — stores per-user MongoDB URI pointer in main DB only."""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from pymongo import ASCENDING
from core.cnl.constants import DEFAULT_DB_NAME
from core.security import decrypt_session, encrypt_session

logger = logging.getLogger(__name__)
COLL = "cnl_gate"

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

def db_name_from_uri(uri: str, default: str = DEFAULT_DB_NAME) -> str:
    try:
        path = (urlparse(uri).path or "").lstrip("/")
        name = path.split("?")[0].strip()
        if name:
            return name.split("/")[0]
    except Exception:
        pass
    return default

def _coll():
    from database import db
    return db.db[COLL]

async def ensure_gate_indexes() -> None:
    try:
        await _coll().create_index([("user_id", ASCENDING)], unique=True, name="cnl_gate_user")
        await _coll().create_index([("enabled", ASCENDING)], name="cnl_gate_enabled")
    except Exception:
        logger.exception("cnl_gate index")

async def get_gate(user_id: int) -> Optional[Dict[str, Any]]:
    return await _coll().find_one({"user_id": int(user_id)}, {"uri_encrypted": 0})

async def get_gate_uri_plain(user_id: int) -> Optional[str]:
    doc = await _coll().find_one({"user_id": int(user_id)}, {"uri_encrypted": 1})
    if not doc or not doc.get("uri_encrypted"):
        return None
    return decrypt_session(doc["uri_encrypted"])

async def is_cnl_configured(user_id: int) -> bool:
    try:
        from core.db_resolver import resolve_feature_db
        r = await resolve_feature_db(int(user_id), "cnl")
        return bool(r.get("configured") and r.get("uri"))
    except Exception:
        pass
    doc = await _coll().find_one(
        {"user_id": int(user_id), "enabled": True, "uri_encrypted": {"$exists": True, "$ne": ""}},
        {"_id": 1},
    )
    return doc is not None

async def set_gate_uri(user_id: int, uri: str, db_name: Optional[str] = None) -> bool:
    await ensure_gate_indexes()
    stored = encrypt_session((uri or "").strip())
    name = db_name or db_name_from_uri(uri)
    now = datetime.now(timezone.utc)
    await _coll().update_one(
        {"user_id": int(user_id)},
        {"$set": {
            "user_id": int(user_id), "uri_encrypted": stored, "db_name": name,
            "enabled": True, "updated_at": now,
        }, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    return True

async def remove_gate(user_id: int) -> bool:
    return (await _coll().delete_one({"user_id": int(user_id)})).deleted_count > 0

async def list_enabled_gates() -> List[Dict[str, Any]]:
    await ensure_gate_indexes()
    return await _coll().find({"enabled": True}).to_list(length=None)
