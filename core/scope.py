"""Private resource ownership only.

Shared DB / shared settings / admin groups resource sharing were removed.
Every user (admin or normal) only sees their own resources.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.access import is_owner


async def get_effective_scope(user_id: int) -> Dict[str, Any]:
    uid = int(user_id)
    return {
        "user_id": uid,
        "role": "owner" if is_owner(uid) else "user",
        "scope_id": f"admin:{uid}",
        "group_id": None,
        "group": None,
        "members": [uid],
        "share_db": False,
        "share_resources": False,
        "is_owner": is_owner(uid),
    }


def scope_owner_ids(scope: Dict[str, Any]) -> List[int]:
    return [int(scope["user_id"])]


async def owns_resource(user_id: int, resource_user_id: Optional[int]) -> bool:
    if resource_user_id is None:
        return False
    uid = int(user_id)
    rid = int(resource_user_id)
    if is_owner(uid) or uid == rid:
        return True
    return False


async def ownership_query(user_id: int) -> Dict[str, Any]:
    return {"user_id": int(user_id)}


async def assert_owns(user_id: int, resource_user_id: Optional[int], label: str = "resource") -> Optional[str]:
    if await owns_resource(user_id, resource_user_id):
        return None
    return f"❌ You cannot access this {label}."


async def find_across_owners(user_id: int, finder):
    return await finder(int(user_id))


async def db_config_key(user_id: int) -> str:
    return f"user:{int(user_id)}"
