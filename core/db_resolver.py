"""Central DB resolution: Feature Custom → Global → Main (if allowed).

AsyncMongoClient only (no Motor). Clients are cached per URI.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from pymongo.asynchronous.mongo_client import AsyncMongoClient

from config import Config
from core.security import decrypt_session, encrypt_session

logger = logging.getLogger(__name__)

FEATURES = (
    "existing_forward", "delete_manager", "indexing", "wroxen", "cnl",
)

# Normal users cannot fall back to Management Main DB for these.
EXTERNAL_REQUIRED = frozenset({"indexing", "wroxen", "cnl"})

COLL = "user_db_config"

FEATURE_COLLECTIONS = {
    "indexing": ("indexed_media",),
    "wroxen": ("wroxen_media",),
    "cnl": ("forward_rules", "message_hashes", "users", "user_sessions", "user_bots", "stats"),
    "delete_manager": ("delete_configs",),
    "existing_forward": ("targets", "forward_jobs", "duplicates", "job_logs"),
}

REQUIRED_DB_MSG = (
    "❌ Database is required for this feature.\n"
    "Please configure your Global Database or feature-specific database first."
)

_indexes_ready = False
_clients: Dict[str, AsyncMongoClient] = {}
_client_lock = asyncio.Lock()
_stats_cache: Dict[str, Tuple[float, dict]] = {}
_STATS_TTL = 60.0


def _coll():
    from database import db
    return db.db[COLL]


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


def _mask_error(exc: BaseException) -> str:
    name = type(exc).__name__
    msg = str(exc) or ""
    low = msg.lower()
    if "authentication failed" in low:
        return "Authentication failed"
    if "timeout" in low or name in ("ServerSelectionTimeoutError", "NetworkTimeout"):
        return "Timeout — check network / IP allowlist"
    if "dns" in low or "dnspython" in low:
        return "DNS/SRV lookup failed"
    if "ssl" in low or "tls" in low:
        return "TLS/SSL error"
    return name


def db_name_from_uri(uri: str, default: str = "cloner_boy") -> str:
    try:
        path = (urlparse(uri).path or "").lstrip("/")
        name = path.split("?")[0].strip()
        if name:
            return name.split("/")[0]
    except Exception:
        pass
    return default


def _uri_key(uri: str) -> str:
    return hashlib.sha256((uri or "").encode("utf-8")).hexdigest()[:24]


async def _is_privileged(user_id: int) -> bool:
    from core.access import is_owner, is_config_admin, is_db_admin
    return is_owner(user_id) or is_config_admin(user_id) or await is_db_admin(user_id)


def main_db_allowed(user_id: int, feature: str, privileged: bool) -> bool:
    if privileged:
        return True
    if feature in EXTERNAL_REQUIRED:
        return False
    return True


async def ensure_indexes() -> None:
    """Idempotent. Existing indexes with same name but different options are left alone."""
    global _indexes_ready
    if _indexes_ready:
        return
    try:
        existing = await _coll().index_information()
    except Exception:
        existing = {}
    if "user_db_config_uid" not in existing:
        try:
            await _coll().create_index(
                [("user_id", 1)], unique=True, name="user_db_config_uid",
                partialFilterExpression={"user_id": {"$exists": True}},
            )
        except Exception as e:
            if getattr(e, "code", None) != 86:
                try:
                    await _coll().create_index([("user_id", 1)], unique=True, name="user_db_config_uid")
                except Exception:
                    logger.debug("user_db_config_uid index: %s", e)
    if "user_db_config_enabled" not in existing:
        try:
            await _coll().create_index([("user_id", 1), ("global_db_name", 1)], name="user_db_config_enabled")
        except Exception:
            pass
    _indexes_ready = True
    try:
        await migrate_legacy_db_uris()
    except Exception:
        logger.exception("legacy DB URI migration")


async def get_user_db_config(user_id: int) -> Dict[str, Any]:
    await ensure_indexes()
    return await _coll().find_one({"user_id": int(user_id)}) or {}


async def _write_filter(user_id: int) -> tuple:
    return {"user_id": int(user_id)}, {"user_id": int(user_id)}


async def test_uri(uri: str, timeout_ms: int = 8000) -> Tuple[bool, str]:
    uri = (uri or "").strip()
    if not (uri.startswith("mongodb://") or uri.startswith("mongodb+srv://")):
        return False, "URI must start with mongodb:// or mongodb+srv://"
    try:
        from core.dns_fix import apply_termux_dns_fix
        apply_termux_dns_fix()
    except Exception:
        pass
    client = None
    try:
        client = AsyncMongoClient(uri, serverSelectionTimeoutMS=timeout_ms)
        await client.admin.command("ping")
        name = db_name_from_uri(uri)
        dbh = client[name]
        try:
            await dbh.command("ping")
        except Exception:
            pass
        return True, f"Connected ({name})"
    except Exception as e:
        return False, f"Unable to connect: {_mask_error(e)}"
    finally:
        if client:
            try:
                await client.close()
            except Exception:
                pass


async def get_cached_client(uri: str) -> AsyncMongoClient:
    key = _uri_key(uri)
    async with _client_lock:
        existing = _clients.get(key)
        if existing is not None:
            return existing
        try:
            from core.dns_fix import apply_termux_dns_fix
            apply_termux_dns_fix()
        except Exception:
            pass
        client = AsyncMongoClient(
            uri,
            serverSelectionTimeoutMS=20000,
            connectTimeoutMS=20000,
            socketTimeoutMS=45000,
            retryWrites=True,
            retryReads=True,
        )
        _clients[key] = client
        return client


async def close_cached_client(uri: Optional[str]) -> None:
    if not uri:
        return
    key = _uri_key(uri)
    async with _client_lock:
        client = _clients.pop(key, None)
    if client:
        try:
            await client.close()
        except Exception:
            pass


async def close_all_cached_clients() -> None:
    async with _client_lock:
        items = list(_clients.items())
        _clients.clear()
    for _, client in items:
        try:
            await client.close()
        except Exception:
            pass


async def set_global_db(user_id: int, uri: str) -> Tuple[bool, str]:
    uri = (uri or "").strip()
    ok, msg = await test_uri(uri)
    if not ok:
        return False, msg
    old = await get_user_db_config(user_id)
    old_uri = None
    if old.get("global_uri_encrypted"):
        try:
            old_uri = decrypt_session(old["global_uri_encrypted"])
        except Exception:
            pass
    enc = encrypt_session(uri)
    name = db_name_from_uri(uri)
    filt, extra = await _write_filter(user_id)
    await _coll().update_one(
        filt,
        {"$set": {
            **extra,
            "global_uri_encrypted": enc,
            "global_db_name": name,
            "global_enabled": True,
            "global_status": "connected",
            "global_updated_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )
    if old_uri and old_uri != uri:
        await close_cached_client(old_uri)
    await _invalidate_feature_runtimes(user_id)
    return True, f"Global DB set ({name})"


async def remove_global_db(user_id: int) -> None:
    cfg = await get_user_db_config(user_id)
    uri = None
    if cfg.get("global_uri_encrypted"):
        try:
            uri = decrypt_session(cfg["global_uri_encrypted"])
        except Exception:
            pass
    filt, _ = await _write_filter(user_id)
    await _coll().update_one(
        filt,
        {"$unset": {
            "global_uri_encrypted": "", "global_db_name": "",
            "global_updated_at": "", "global_status": "",
        }, "$set": {"global_enabled": False}},
    )
    await close_cached_client(uri)
    await _invalidate_feature_runtimes(user_id)


async def set_feature_db(user_id: int, feature: str, uri: str) -> Tuple[bool, str]:
    if feature not in FEATURES:
        return False, "Unknown feature"
    uri = (uri or "").strip()
    ok, msg = await test_uri(uri)
    if not ok:
        return False, msg
    enc = encrypt_session(uri)
    name = db_name_from_uri(uri)
    filt, extra = await _write_filter(user_id)
    now = datetime.now(timezone.utc)
    await _coll().update_one(
        filt,
        {"$set": {
            **extra,
            f"features.{feature}.uri_encrypted": enc,
            f"features.{feature}.db_name": name,
            f"features.{feature}.enabled": True,
            f"features.{feature}.status": "connected",
            f"features.{feature}.updated_at": now,
        }},
        upsert=True,
    )
    await _sync_legacy_uri(user_id, feature, uri)
    await _invalidate_feature_runtimes(user_id, feature)
    return True, f"{feature} DB set ({name})"


async def remove_feature_db(user_id: int, feature: str) -> None:
    cfg = await get_user_db_config(user_id)
    feat = (cfg.get("features") or {}).get(feature) or {}
    uri = None
    if feat.get("uri_encrypted"):
        try:
            uri = decrypt_session(feat["uri_encrypted"])
        except Exception:
            pass
    filt, _ = await _write_filter(user_id)
    await _coll().update_one(filt, {"$unset": {f"features.{feature}": ""}})
    await _sync_legacy_uri(user_id, feature, None)
    await close_cached_client(uri)
    await _invalidate_feature_runtimes(user_id, feature)


async def _sync_legacy_uri(user_id: int, feature: str, uri: Optional[str]) -> None:
    """Keep old per-feature URI fields in sync so existing code still works."""
    try:
        if feature == "wroxen":
            from database import set_wroxen_db_uri
            await set_wroxen_db_uri(user_id, uri)
        elif feature == "indexing":
            from database import set_index_db_uri
            await set_index_db_uri(user_id, uri)
        elif feature == "cnl":
            from core.cnl.gate import set_gate_uri, remove_gate
            if uri:
                await set_gate_uri(user_id, uri)
            else:
                await remove_gate(user_id)
    except Exception:
        logger.debug("legacy URI sync failed feature=%s", feature, exc_info=True)


async def _legacy_feature_uri(user_id: int, feature: str) -> Optional[Tuple[str, str]]:
    """Read old URI fields without going through public getters (avoids recursion)."""
    try:
        from database import db
        if feature == "wroxen":
            user = await db.users.find_one({"user_id": int(user_id)}, {"wroxen_db_uri": 1})
            enc = (user or {}).get("wroxen_db_uri")
            if enc:
                uri = decrypt_session(enc)
                if uri:
                    return uri, db_name_from_uri(uri, "WroxenDB")
        elif feature == "indexing":
            user = await db.users.find_one({"user_id": int(user_id)}, {"index_db_uri": 1})
            enc = (user or {}).get("index_db_uri")
            if enc:
                uri = decrypt_session(enc)
                if uri:
                    return uri, db_name_from_uri(uri, "IndexDB")
        elif feature == "cnl":
            doc = await db.db["cnl_gate"].find_one(
                {"user_id": int(user_id)}, {"uri_encrypted": 1, "db_name": 1}
            )
            if doc and doc.get("uri_encrypted"):
                uri = decrypt_session(doc["uri_encrypted"])
                if uri:
                    return uri, doc.get("db_name") or db_name_from_uri(uri, "cnl_autopost")
    except Exception:
        return None
    return None


async def migrate_legacy_db_uris() -> None:
    """Copy existing per-feature URIs into user_db_config without overwriting."""
    from database import db
    users = await db.users.find(
        {"$or": [
            {"wroxen_db_uri": {"$exists": True, "$nin": [None, ""]}},
            {"index_db_uri": {"$exists": True, "$nin": [None, ""]}},
        ]},
        {"user_id": 1, "wroxen_db_uri": 1, "index_db_uri": 1},
    ).to_list(2000)
    for u in users:
        uid = int(u["user_id"])
        cfg = await _coll().find_one({"user_id": uid}) or {}
        feats = dict(cfg.get("features") or {})
        updates = {}
        if u.get("wroxen_db_uri") and not (feats.get("wroxen") or {}).get("uri_encrypted"):
            updates["features.wroxen.uri_encrypted"] = u["wroxen_db_uri"]
            updates["features.wroxen.enabled"] = True
        if u.get("index_db_uri") and not (feats.get("indexing") or {}).get("uri_encrypted"):
            updates["features.indexing.uri_encrypted"] = u["index_db_uri"]
            updates["features.indexing.enabled"] = True
        if updates:
            updates["user_id"] = uid
            await _coll().update_one({"user_id": uid}, {"$set": updates}, upsert=True)
    try:
        gates = await db.db["cnl_gate"].find({"uri_encrypted": {"$exists": True, "$ne": ""}}).to_list(2000)
    except Exception:
        gates = []
    for g in gates:
        uid = int(g["user_id"])
        cfg = await _coll().find_one({"user_id": uid}) or {}
        feats = dict(cfg.get("features") or {})
        if (feats.get("cnl") or {}).get("uri_encrypted"):
            continue
        await _coll().update_one(
            {"user_id": uid},
            {"$set": {
                "user_id": uid,
                "features.cnl.uri_encrypted": g["uri_encrypted"],
                "features.cnl.db_name": g.get("db_name"),
                "features.cnl.enabled": bool(g.get("enabled", True)),
            }},
            upsert=True,
        )


async def resolve_feature_db(user_id: int, feature: str) -> Dict[str, Any]:
    """Priority: feature custom → global → main (only if role/feature allows)."""
    cfg = await get_user_db_config(user_id)
    features = cfg.get("features") or {}
    feat = features.get(feature) or {}
    privileged = await _is_privileged(user_id)

    def _pack(uri: str, db_name: str, source: str) -> Dict[str, Any]:
        return {
            "uri": uri,
            "db_name": db_name or db_name_from_uri(uri or ""),
            "source": source,
            "feature": feature,
            "masked": mask_uri(uri),
            "configured": bool(uri),
            "error": None,
            "main_allowed": main_db_allowed(user_id, feature, privileged),
        }

    if feat.get("uri_encrypted") and feat.get("enabled", True):
        try:
            uri = decrypt_session(feat["uri_encrypted"])
        except Exception:
            uri = None
        if uri:
            return _pack(uri, feat.get("db_name") or db_name_from_uri(uri), "custom")

    if cfg.get("global_uri_encrypted") and cfg.get("global_enabled", True):
        try:
            uri = decrypt_session(cfg["global_uri_encrypted"])
        except Exception:
            uri = None
        if uri:
            return _pack(uri, cfg.get("global_db_name") or db_name_from_uri(uri), "global")

    legacy = await _legacy_feature_uri(user_id, feature)
    if legacy:
        uri, name = legacy
        return _pack(uri, name, "custom")

    if not main_db_allowed(user_id, feature, privileged):
        return {
            "uri": None,
            "db_name": None,
            "source": "none",
            "feature": feature,
            "masked": "Not set",
            "configured": False,
            "error": REQUIRED_DB_MSG,
            "main_allowed": False,
        }

    main = (Config.MONGO_URI or "").strip()
    return {
        "uri": main or None,
        "db_name": Config.DB_NAME or db_name_from_uri(main or "", "cloner_boy"),
        "source": "main",
        "feature": feature,
        "masked": mask_uri(main),
        "configured": bool(main),
        "error": None if main else REQUIRED_DB_MSG,
        "main_allowed": True,
    }


async def get_feature_database(user_id: int, feature: str):
    """Return (pymongo Database, resolve dict) or (None, resolve dict) if unavailable."""
    resolved = await resolve_feature_db(user_id, feature)
    uri = resolved.get("uri")
    if not uri:
        return None, resolved
    if resolved.get("source") == "main" and feature in EXTERNAL_REQUIRED:
        if not resolved.get("main_allowed"):
            resolved["error"] = REQUIRED_DB_MSG
            resolved["uri"] = None
            resolved["configured"] = False
            return None, resolved
    try:
        client = await get_cached_client(uri)
        name = resolved.get("db_name") or db_name_from_uri(uri)
        return client[name], resolved
    except Exception as e:
        resolved["error"] = f"Unable to connect: {_mask_error(e)}"
        resolved["configured"] = False
        return None, resolved


async def ping_resolved(resolved: Dict[str, Any]) -> str:
    uri = resolved.get("uri")
    if not uri:
        return "disconnected"
    try:
        client = await get_cached_client(uri)
        await client.admin.command("ping")
        return "connected"
    except Exception as e:
        return _mask_error(e)


def _fmt_bytes(n: Any) -> str:
    try:
        n = float(n)
    except Exception:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    return f"{n:.2f} {units[i]}"


async def get_storage_stats(user_id: int, feature: str) -> Dict[str, Any]:
    """dbStats for the resolved feature database. Never invent numbers."""
    import time
    dbh, resolved = await get_feature_database(user_id, feature)
    out = {
        "ok": False,
        "source": resolved.get("source"),
        "db_name": resolved.get("db_name"),
        "masked": resolved.get("masked"),
        "status": "disconnected",
        "error": resolved.get("error"),
        "storage": None,
        "data": None,
        "index": None,
        "collections": None,
        "documents": None,
        "checked_at": datetime.now(timezone.utc),
    }
    if dbh is None:
        return out
    cache_key = f"{user_id}:{feature}:{resolved.get('source')}:{resolved.get('db_name')}"
    now = time.time()
    cached = _stats_cache.get(cache_key)
    if cached and now - cached[0] < _STATS_TTL:
        return cached[1]
    try:
        stats = await dbh.command("dbStats")
        names = await dbh.list_collection_names()
        docs = None
        try:
            docs = int(stats.get("objects") or 0)
        except Exception:
            docs = None
        out.update({
            "ok": True,
            "status": "connected",
            "error": None,
            # Entire MongoDB database (shared Global DB = shared size)
            "storage": _fmt_bytes(stats.get("storageSize")),
            "data": _fmt_bytes(stats.get("dataSize")),
            "index": _fmt_bytes(stats.get("indexSize")),
            "total": _fmt_bytes(
                (stats.get("storageSize") or 0) + (stats.get("indexSize") or 0)
            ),
            "collections": len(names),
            "documents": docs,
            "scope": "database",  # not per-user
            "note": "Size is for the whole database, not one user.",
        })
    except Exception as e:
        out["status"] = "unavailable"
        out["error"] = "⚠️ Storage information unavailable"
        logger.debug("dbStats failed: %s", _mask_error(e))
    _stats_cache[cache_key] = (now, out)
    return out


# Per-collection ownership filters for scoped deletes (never drop shared collections).
FEATURE_DELETE_FILTERS = {
    "indexing": {
        "indexed_media": lambda uid: {"user_id": int(uid)},
    },
    "wroxen": {
        # wroxen_media is keyed by wroxen_id owned by user — delete via user's configs
        "wroxen_media": None,  # special handling
    },
    "cnl": {
        "forward_rules": lambda uid: {"owner_id": int(uid)},
        "message_hashes": lambda uid: {"owner_id": int(uid)},
        "user_bots": lambda uid: {"user_id": int(uid)},
        "user_sessions": lambda uid: {"user_id": int(uid)},
        "users": lambda uid: {"user_id": int(uid)},
        "stats": lambda uid: {"_id": f"user:{int(uid)}"},
    },
    "delete_manager": {
        "delete_configs": lambda uid: {"user_id": int(uid)},
    },
    "existing_forward": {
        "targets": lambda uid: {"user_id": int(uid)},
        "forward_jobs": lambda uid: {"user_id": int(uid)},
        "duplicates": lambda uid: {"user_id": int(uid)},
        "job_logs": lambda uid: {"user_id": int(uid)},
    },
}


async def clear_feature_data(user_id: int, feature: str) -> Tuple[bool, str]:
    """Delete only this user's documents. NEVER drop collections (Global DB may be shared)."""
    if feature not in FEATURES:
        return False, "Unknown feature"
    dbh, resolved = await get_feature_database(user_id, feature)
    if dbh is None:
        return False, resolved.get("error") or "Database not configured"
    uid = int(user_id)
    deleted = 0
    filters = FEATURE_DELETE_FILTERS.get(feature) or {}
    colls = FEATURE_COLLECTIONS.get(feature) or ()

    # Wroxen: resolve this user's wroxen_ids then delete media for those ids only
    if feature == "wroxen":
        try:
            from database import get_user_wroxen_configs
            configs = await get_user_wroxen_configs(uid)
            wids = [c.get("wroxen_id") for c in (configs or []) if c.get("wroxen_id")]
            if wids:
                res = await dbh["wroxen_media"].delete_many({"wroxen_id": {"$in": wids}})
                deleted += int(res.deleted_count or 0)
        except Exception:
            logger.exception("clear wroxen_media user=%s", uid)
        await _invalidate_feature_runtimes(user_id, feature)
        return True, f"Cleared `{feature}` data ({deleted} docs). Collections were not dropped."

    for name in colls:
        try:
            col = dbh[name]
            filt_fn = filters.get(name)
            if filt_fn is None:
                # Unknown ownership — try common keys, never drop
                for key in ("user_id", "owner_id"):
                    try:
                        res = await col.delete_many({key: uid})
                        deleted += int(res.deleted_count or 0)
                    except Exception:
                        pass
                continue
            res = await col.delete_many(filt_fn(uid))
            deleted += int(res.deleted_count or 0)
        except Exception:
            logger.exception("clear_feature_data %s %s", feature, name)
    await _invalidate_feature_runtimes(user_id, feature)
    return True, f"Cleared `{feature}` data ({deleted} docs). Collections were not dropped."


async def get_user_data_counts(user_id: int, feature: str) -> Dict[str, Any]:
    """Approximate per-user document counts (not byte size)."""
    dbh, resolved = await get_feature_database(user_id, feature)
    out = {"feature": feature, "source": resolved.get("source"), "counts": {}, "total_docs": 0}
    if dbh is None:
        out["error"] = resolved.get("error")
        return out
    uid = int(user_id)
    filters = FEATURE_DELETE_FILTERS.get(feature) or {}
    if feature == "wroxen":
        try:
            from database import get_user_wroxen_configs
            configs = await get_user_wroxen_configs(uid)
            wids = [c.get("wroxen_id") for c in (configs or []) if c.get("wroxen_id")]
            n = 0
            if wids:
                n = await dbh["wroxen_media"].count_documents({"wroxen_id": {"$in": wids}})
            out["counts"]["wroxen_media"] = n
            out["total_docs"] = n
        except Exception:
            out["counts"]["wroxen_media"] = None
        return out
    for name in (FEATURE_COLLECTIONS.get(feature) or ()):
        try:
            fn = filters.get(name)
            if fn is None:
                n = await dbh[name].count_documents({"$or": [{"user_id": uid}, {"owner_id": uid}]})
            else:
                n = await dbh[name].count_documents(fn(uid))
            out["counts"][name] = n
            out["total_docs"] += int(n or 0)
        except Exception:
            out["counts"][name] = None
    return out


async def list_user_databases(user_id: int) -> list:

    rows = []
    cfg = await get_user_db_config(user_id)
    privileged = await _is_privileged(user_id)
    if privileged:
        main = (Config.MONGO_URI or "").strip()
        rows.append({
            "label": "Main Database",
            "source": "main",
            "feature": None,
            "db_name": Config.DB_NAME,
            "masked": mask_uri(main),
            "configured": bool(main),
        })
    if cfg.get("global_uri_encrypted"):
        try:
            uri = decrypt_session(cfg["global_uri_encrypted"])
        except Exception:
            uri = None
        rows.append({
            "label": "Global Database",
            "source": "global",
            "feature": None,
            "db_name": cfg.get("global_db_name"),
            "masked": mask_uri(uri),
            "configured": True,
        })
    for feat in FEATURES:
        resolved = await resolve_feature_db(user_id, feat)
        if resolved["source"] == "custom":
            rows.append({
                "label": f"{feat} Custom",
                "source": "custom",
                "feature": feat,
                "db_name": resolved["db_name"],
                "masked": resolved["masked"],
                "configured": True,
            })
    return rows


async def features_using_global(user_id: int) -> List[str]:
    used = []
    for feat in FEATURES:
        r = await resolve_feature_db(user_id, feat)
        if r.get("source") == "global":
            used.append(feat)
    return used


async def list_all_user_db_configs(limit: int = 80) -> List[Dict[str, Any]]:
    await ensure_indexes()
    return await _coll().find({}).to_list(limit)


async def _invalidate_feature_runtimes(user_id: int, feature: Optional[str] = None) -> None:
    """Close feature clients for this user, then rebind CNL/Wroxen from new DB if needed."""
    feats = [feature] if feature else list(FEATURES)
    uid = int(user_id)
    try:
        if "cnl" in feats:
            from core.cnl.db import close_cnl
            from core.cnl.bots import get_user_bot_manager
            from core.cnl.clients import get_user_client_manager
            await close_cnl(uid)
            try:
                await get_user_bot_manager().stop_user_bot(uid)
            except Exception:
                pass
            try:
                await get_user_client_manager().stop_user_client(uid)
            except Exception:
                pass
    except Exception:
        logger.debug("invalidate cnl", exc_info=True)
    try:
        if "wroxen" in feats:
            from core.wroxen import db as wxdb
            await wxdb.disconnect(uid)
            try:
                from core.wroxen.runtime import refresh_routing
                await refresh_routing()
            except Exception:
                pass
    except Exception:
        logger.debug("invalidate wroxen", exc_info=True)
    try:
        if "indexing" in feats:
            from core.index_db import disconnect_index_db
            await disconnect_index_db(uid)
    except Exception:
        pass
    for k in list(_stats_cache.keys()):
        if k.startswith(f"{uid}:"):
            _stats_cache.pop(k, None)
    # Rebind CNL clients against the new resolved DB (enabled rules only)
    if "cnl" in feats:
        try:
            from core.lifecycle import reconcile_cnl_user
            await reconcile_cnl_user(uid)
        except Exception:
            logger.debug("cnl rebind after DB change failed user=%s", uid, exc_info=True)
