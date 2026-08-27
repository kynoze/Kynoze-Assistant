"""Central DB resolution: Feature Custom → Global → Main.

Uses AsyncMongoClient only (no Motor).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from pymongo.asynchronous.mongo_client import AsyncMongoClient

from config import Config
from core.security import decrypt_session, encrypt_session

logger = logging.getLogger(__name__)

FEATURES = (
    "existing_forward", "delete_manager", "indexing", "wroxen", "cnl",
)

COLL = "user_db_config"


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


def db_name_from_uri(uri: str, default: str = "cloner_boy") -> str:
    try:
        path = (urlparse(uri).path or "").lstrip("/")
        name = path.split("?")[0].strip()
        if name:
            return name.split("/")[0]
    except Exception:
        pass
    return default


_indexes_ready = False


async def ensure_indexes() -> None:
    """Idempotent. Existing indexes with same name but different options are left alone."""
    global _indexes_ready
    if _indexes_ready:
        return
    try:
        existing = await _coll().index_information()
    except Exception:
        existing = {}
    # user_id unique — keep whatever already exists under this name
    if "user_db_config_uid" not in existing:
        try:
            await _coll().create_index(
                [("user_id", 1)], unique=True, name="user_db_config_uid",
                partialFilterExpression={"user_id": {"$exists": True}},
            )
        except Exception as e:
            # Code 86 = same name, different options — safe to ignore
            if "IndexKeySpecsConflict" not in type(e).__name__ and getattr(e, "code", None) != 86:
                try:
                    await _coll().create_index([("user_id", 1)], unique=True, name="user_db_config_uid")
                except Exception:
                    logger.debug("user_db_config_uid index: %s", e)
    if "user_db_config_scope" not in existing:
        try:
            await _coll().create_index(
                [("scope_key", 1)], unique=True, name="user_db_config_scope",
                sparse=True,
            )
        except Exception as e:
            logger.debug("user_db_config_scope index: %s", e)
    _indexes_ready = True


async def get_user_db_config(user_id: int) -> Dict[str, Any]:
    await ensure_indexes()
    doc = await _coll().find_one({"user_id": int(user_id)}) or {}
    return doc



async def set_global_db(user_id: int, uri: str) -> Tuple[bool, str]:
    ok, msg = await test_uri(uri)
    if not ok:
        return False, msg
    enc = encrypt_session(uri.strip())
    name = db_name_from_uri(uri)
    filt, extra = await _write_filter(user_id)
    await _coll().update_one(
        filt,
        {"$set": {
            **extra,
            "global_uri_encrypted": enc,
            "global_db_name": name,
            "global_updated_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )
    return True, f"Global DB set ({name})"


async def _write_filter(user_id: int) -> tuple:
    return {"user_id": int(user_id)}, {"user_id": int(user_id)}




async def remove_global_db(user_id: int) -> None:
    filt, _ = await _write_filter(user_id)
    await _coll().update_one(
        filt,
        {"$unset": {"global_uri_encrypted": "", "global_db_name": "", "global_updated_at": ""}},
    )


async def set_feature_db(user_id: int, feature: str, uri: str) -> Tuple[bool, str]:
    if feature not in FEATURES:
        return False, "Unknown feature"
    ok, msg = await test_uri(uri)
    if not ok:
        return False, msg
    enc = encrypt_session(uri.strip())
    name = db_name_from_uri(uri)
    filt, extra = await _write_filter(user_id)
    await _coll().update_one(
        filt,
        {"$set": {
            **extra,
            f"features.{feature}.uri_encrypted": enc,
            f"features.{feature}.db_name": name,
            f"features.{feature}.updated_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )
    return True, f"{feature} DB set ({name})"


async def remove_feature_db(user_id: int, feature: str) -> None:
    filt, _ = await _write_filter(user_id)
    await _coll().update_one(filt, {"$unset": {f"features.{feature}": ""}})


async def test_uri(uri: str, timeout_ms: int = 8000) -> Tuple[bool, str]:
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
        return True, f"Connected ({name})"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:120]}"
    finally:
        if client:
            try:
                await client.close()
            except Exception:
                pass


async def resolve_feature_db(user_id: int, feature: str) -> Dict[str, Any]:
    """Return active URI source for a feature.

    Priority: feature custom → global → main (Config.MONGO_URI).
    """
    cfg = await get_user_db_config(user_id)
    features = cfg.get("features") or {}
    feat = features.get(feature) or {}

    if feat.get("uri_encrypted"):
        uri = decrypt_session(feat["uri_encrypted"])
        return {
            "uri": uri,
            "db_name": feat.get("db_name") or db_name_from_uri(uri or ""),
            "source": "custom",
            "feature": feature,
            "masked": mask_uri(uri),
            "configured": bool(uri),
        }

    if cfg.get("global_uri_encrypted"):
        uri = decrypt_session(cfg["global_uri_encrypted"])
        return {
            "uri": uri,
            "db_name": cfg.get("global_db_name") or db_name_from_uri(uri or ""),
            "source": "global",
            "feature": feature,
            "masked": mask_uri(uri),
            "configured": bool(uri),
        }

    main = (Config.MONGO_URI or "").strip()
    return {
        "uri": main or None,
        "db_name": Config.DB_NAME or db_name_from_uri(main or "", "cloner_boy"),
        "source": "main",
        "feature": feature,
        "masked": mask_uri(main),
        "configured": bool(main),
    }


async def list_user_databases(user_id: int) -> list:
    """Summary rows for My Databases UI."""
    rows = []
    main = await resolve_feature_db(user_id, "existing_forward")
    rows.append({"label": "Main Database", "source": "main", **{k: main[k] for k in ("db_name", "masked", "configured")}})
    cfg = await get_user_db_config(user_id)
    if cfg.get("global_uri_encrypted"):
        uri = decrypt_session(cfg["global_uri_encrypted"])
        rows.append({
            "label": "Global Database",
            "source": "global",
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
