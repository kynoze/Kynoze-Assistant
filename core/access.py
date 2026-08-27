"""Centralized Owner / Admin / Normal-User access control.

All protected handlers should use these helpers instead of inventing local checks.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

from config import Config

logger = logging.getLogger(__name__)

# Feature keys used in UI + backend
FEATURES = (
    "jobs", "targets", "quick_forward", "stats",
    "accounts", "bots", "indexing", "wroxen",
    "delete_manager", "cnl", "settings",
)

DEFAULT_NORMAL_FEATURES = {
    "jobs": False,
    "targets": True,
    "quick_forward": True,
    "stats": True,
    "accounts": True,
    "bots": True,
    "indexing": True,
    "wroxen": True,
    "delete_manager": False,
    "cnl": True,
    "settings": True,
}

DEFAULT_NORMAL_LIMITS = {
    "targets": 5,
    "wroxen": 5,
    "accounts": 5,
    "bots": 5,
    "jobs": 5,
    "cnl_rules": 5,
    "delete_manager": 5,
}

ADMIN_PERMISSION_KEYS = (
    "manage_users", "manage_bots", "manage_accounts", "manage_targets",
    "manage_wroxen", "manage_jobs", "manage_indexing", "manage_delete",
    "manage_cnl", "manage_databases", "manage_normal_users", "manage_admins",
)


def owner_ids() -> List[int]:
    ids = list(getattr(Config, "OWNER_IDS", None) or [])
    if ids:
        return [int(x) for x in ids]
    # first ADMIN is owner if OWNER_IDS not set
    return list(Config.ADMINS[:1]) if Config.ADMINS else []


def is_owner(user_id: int) -> bool:
    uid = int(user_id)
    if uid in owner_ids():
        return True
    # Config.ADMINS[0] treated as owner when OWNER_IDS empty
    if Config.ADMINS and uid == Config.ADMINS[0] and not owner_ids():
        return True
    return False


def is_config_admin(user_id: int) -> bool:
    return int(user_id) in Config.ADMINS


async def is_db_admin(user_id: int) -> bool:
    try:
        from database import db
        doc = await db.db["bot_admins"].find_one({"user_id": int(user_id), "enabled": True})
        return doc is not None
    except Exception:
        return False


async def is_admin(user_id: int) -> bool:
    """Owner, Config.ADMINS, or DB-enabled admin."""
    if is_owner(user_id) or is_config_admin(user_id):
        return True
    return await is_db_admin(user_id)


def is_admin_sync(user_id: int) -> bool:
    """Sync check for Config only (legacy call sites). Prefer await is_admin()."""
    return is_owner(user_id) or is_config_admin(user_id)


async def get_admin_permissions(user_id: int) -> Set[str]:
    if is_owner(user_id):
        return set(ADMIN_PERMISSION_KEYS)
    # Config.ADMINS: full access unless a bot_admins doc explicitly restricts them
    try:
        from database import db
        doc = await db.db["bot_admins"].find_one({"user_id": int(user_id)})
        if doc is not None:
            if not doc.get("enabled", True):
                return set()
            perms = doc.get("permissions")
            if perms is None:
                # explicit doc without permissions = no feature perms until Owner sets them
                if is_config_admin(user_id):
                    return set(ADMIN_PERMISSION_KEYS)  # config admin default full
                return set()
            if perms == ["*"] or perms == "all":
                return set(ADMIN_PERMISSION_KEYS)
            return set(perms)
        if is_config_admin(user_id):
            return set(ADMIN_PERMISSION_KEYS)
        return set()
    except Exception:
        if is_config_admin(user_id):
            return set(ADMIN_PERMISSION_KEYS)
        return set()


async def admin_has(user_id: int, perm: str) -> bool:
    if is_owner(user_id):
        return True
    perms = await get_admin_permissions(user_id)
    return perm in perms


async def get_system_settings() -> Dict[str, Any]:
    from database import db
    coll = db.db["system_settings"]
    doc = await coll.find_one({"_id": "global"})
    if not doc:
        doc = {
            "_id": "global",
            "normal_users_enabled": False,
            "normal_user_features": dict(DEFAULT_NORMAL_FEATURES),
            "normal_user_limits": dict(DEFAULT_NORMAL_LIMITS),
        }
        await coll.update_one({"_id": "global"}, {"$setOnInsert": doc}, upsert=True)
        return doc
    # merge defaults
    feats = dict(DEFAULT_NORMAL_FEATURES)
    feats.update(doc.get("normal_user_features") or {})
    limits = dict(DEFAULT_NORMAL_LIMITS)
    limits.update(doc.get("normal_user_limits") or {})
    doc["normal_user_features"] = feats
    doc["normal_user_limits"] = limits
    return doc


async def update_system_settings(updates: dict) -> None:
    from database import db
    await db.db["system_settings"].update_one(
        {"_id": "global"}, {"$set": updates}, upsert=True
    )


async def normal_users_enabled() -> bool:
    s = await get_system_settings()
    return bool(s.get("normal_users_enabled", False))


async def can_use_feature(user_id: int, feature: str) -> bool:
    """Can this user open/use a dashboard feature?"""
    if is_owner(user_id):
        return True
    if is_config_admin(user_id) or await is_db_admin(user_id):
        perm = FEATURE_ADMIN_PERM.get(feature)
        if not perm:
            return True
        # Config admins default full; DB admins use their permission set
        return await admin_has(user_id, perm)
    if not await normal_users_enabled():
        return False
    s = await get_system_settings()
    feats = dict(DEFAULT_NORMAL_FEATURES)
    feats.update(s.get("normal_user_features") or {})
    # aliases
    if feature == "existing_forward":
        return any(feats.get(k) for k in ("jobs", "targets", "stats", "quick_forward"))
    return bool(feats.get(feature, False))


async def get_limit(user_id: int, resource: str) -> Optional[int]:
    """None = unlimited (owner/admin)."""
    if is_owner(user_id) or is_config_admin(user_id) or await is_db_admin(user_id):
        return None
    s = await get_system_settings()
    limits = dict(DEFAULT_NORMAL_LIMITS)
    limits.update(s.get("normal_user_limits") or {})
    return int(limits.get(resource, 5))


async def check_limit(user_id: int, resource: str, current_count: int) -> Optional[str]:
    """Return error message if over limit, else None."""
    lim = await get_limit(user_id, resource)
    if lim is None:
        return None
    if current_count >= lim:
        return f"❌ You have reached your maximum limit of {lim} {resource}."
    return None


async def can_access_bot(user_id: int) -> bool:
    """May the user open /start at all?"""
    if is_owner(user_id) or is_config_admin(user_id):
        return True
    if await is_db_admin(user_id):
        return True
    return await normal_users_enabled()


# feature → admin permission (for owner control UI)
FEATURE_ADMIN_PERM = {
    "jobs": "manage_jobs",
    "targets": "manage_targets",
    "accounts": "manage_accounts",
    "bots": "manage_bots",
    "wroxen": "manage_wroxen",
    "indexing": "manage_indexing",
    "delete_manager": "manage_delete",
    "cnl": "manage_cnl",
    "stats": "manage_jobs",
    "quick_forward": "manage_jobs",
    "settings": "manage_targets",
    "existing_forward": "manage_jobs",
}
