"""CNL async database — isolated per-user MongoDB via AsyncMongoClient."""
from __future__ import annotations
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from pymongo import ASCENDING, IndexModel
from pymongo.asynchronous.mongo_client import AsyncMongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

from core.cnl.constants import (
    ALLOWED_CAPTION_POSITIONS, ALLOWED_FORWARD_VIA, ALLOWED_MEDIA_TYPES,
    DAILY_FORWARD_LIMIT, DEFAULT_DB_NAME, DUPE_DB_NAME, GLOBAL_COPY_FILTER_KEYS,
    RULE_CACHE_TTL_SECONDS, RULE_FORWARD_PROJECTION, RULE_LIMIT,
)
from core.cnl.gate import get_gate, get_gate_uri_plain
from core.security import decrypt_session, encrypt_session

logger = logging.getLogger(__name__)
_INSTANCES: Dict[int, "CnlDatabase"] = {}


def _apply_dns():
    try:
        from core.dns_fix import apply_termux_dns_fix
        apply_termux_dns_fix()
    except Exception:
        pass


class CnlDatabase:
    def __init__(self, owner_user_id: int):
        self.owner_user_id = int(owner_user_id)
        self.client: Optional[AsyncMongoClient] = None
        self.db = None
        self.users = self.forward_rules = self.channel_admins = None
        self.bot_admins = self.message_hashes = self.stats = None
        self.user_sessions = self.user_bots = None
        self._connected = False
        self._dupe_clients: Dict[int, Any] = {}
        self._rules_by_source_cache: Dict[int, Tuple[float, list]] = {}

    @property
    def is_connected(self) -> bool:
        return self._connected and self.client is not None

    async def connect(self, uri: str, db_name: Optional[str] = None) -> Tuple[bool, str]:
        _apply_dns()
        try:
            if self.client:
                try:
                    await self.client.close()
                except Exception:
                    pass
            self.client = AsyncMongoClient(uri, serverSelectionTimeoutMS=8000)
            await self.client.admin.command("ping")
            name = db_name or DEFAULT_DB_NAME
            try:
                path = (urlparse(uri).path or "").lstrip("/")
                n = path.split("?")[0].strip()
                if n:
                    name = n.split("/")[0]
            except Exception:
                pass
            self.db = self.client[name]
            self.users = self.db["users"]
            self.forward_rules = self.db["forward_rules"]
            self.channel_admins = self.db["channel_admins"]
            self.bot_admins = self.db["bot_admins"]
            self.message_hashes = self.db["message_hashes"]
            self.stats = self.db["stats"]
            self.user_sessions = self.db["user_sessions"]
            self.user_bots = self.db["user_bots"]
            await self._ensure_indexes()
            await self._ensure_owners()
            await self._ensure_stats_doc()
            self._connected = True
            return True, f"Connected ({name})"
        except Exception as e:
            self._connected = False
            try:
                if self.client:
                    await self.client.close()
            except Exception:
                pass
            self.client = None
            return False, f"{type(e).__name__}: {str(e)[:120]}"

    async def close(self):
        self._connected = False
        for uid in list(self._dupe_clients):
            await self._close_dupe_client(uid)
        if self.client:
            try:
                await self.client.close()
            except Exception:
                pass
        self.client = None
        self._rules_by_source_cache.clear()

    async def ping(self) -> bool:
        try:
            if not self.client:
                return False
            await self.client.admin.command("ping")
            return True
        except PyMongoError:
            return False

    async def _safe_create_index(self, coll, keys, **kwargs):
        """Create index; ignore if same keys already exist under another name."""
        try:
            await coll.create_index(keys, **kwargs)
        except Exception as e:
            msg = str(e).lower()
            code = getattr(e, "code", None)
            # 85 = IndexOptionsConflict, 86 = IndexKeySpecsConflict
            if code in (85, 86) or "already exists" in msg or "indexkeyspecsconflict" in msg:
                logger.debug(
                    "CNL index skip %s %s: %s",
                    getattr(coll, "name", "?"),
                    kwargs.get("name"),
                    type(e).__name__,
                )
                return
            logger.warning(
                "CNL index warn %s %s: %s",
                getattr(coll, "name", "?"),
                kwargs.get("name"),
                e,
            )

    async def _ensure_indexes(self):
        """Idempotent. Existing indexes with different names are left alone — do not fail connect."""
        await self._safe_create_index(
            self.users, [("user_id", ASCENDING)], unique=True, name="user_id_unique",
        )
        # Multi-user Global DB: uniqueness is per owner, not global source→target
        await self._safe_create_index(
            self.forward_rules,
            [("owner_id", ASCENDING), ("source_chat_id", ASCENDING), ("target_chat_id", ASCENDING)],
            unique=True, name="owner_source_target_unique",
        )
        await self._safe_create_index(
            self.forward_rules, [("source_chat_id", ASCENDING)], name="source_idx",
        )
        try:
            await self._safe_create_index(
                self.forward_rules, [("source_chat_id", ASCENDING)],
                partialFilterExpression={"enabled": True}, name="source_enabled_partial_idx",
            )
        except Exception:
            pass
        await self._safe_create_index(
            self.forward_rules, [("target_chat_id", ASCENDING)], name="target_idx",
        )
        await self._safe_create_index(
            self.forward_rules, [("owner_id", ASCENDING)], name="owner_idx",
        )
        await self._safe_create_index(
            self.forward_rules, [("enabled", ASCENDING)], name="enabled_idx",
        )
        await self._safe_create_index(
            self.channel_admins,
            [("chat_id", ASCENDING), ("user_id", ASCENDING)],
            unique=True, name="chat_user_unique",
        )
        await self._safe_create_index(
            self.bot_admins, [("user_id", ASCENDING)], unique=True, name="bot_admin_unique",
        )
        await self._safe_create_index(
            self.message_hashes,
            [("owner_id", ASCENDING), ("hash", ASCENDING), ("target_chat_id", ASCENDING)],
            unique=True, name="owner_hash_target_unique",
        )
        await self._safe_create_index(
            self.message_hashes, [("owner_id", ASCENDING)], name="hash_owner_idx",
        )
        await self._safe_create_index(
            self.message_hashes, [("target_chat_id", ASCENDING)], name="hash_target_idx",
        )
        # Default CNL DB hashes expire; Custom Dupe DB never gets this index
        await self._ensure_default_hash_ttl()
        await self._safe_create_index(
            self.user_sessions, [("user_id", ASCENDING)], unique=True, name="session_user_unique",
        )
        await self._safe_create_index(
            self.user_bots, [("user_id", ASCENDING)], unique=True, name="bot_user_unique",
        )

    async def _ensure_owners(self):
        pass

    async def _ensure_stats_doc(self):
        await self.stats.update_one({"_id": "global"}, {"$setOnInsert": {
            "forwards": 0, "blocked": 0, "failed": 0, "duplicates": 0,
            "created_at": datetime.now(timezone.utc),
        }}, upsert=True)

    def _today_str(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    async def add_or_update_user(self, user_id, username=None, first_name=None, last_name=None):
        now = datetime.now(timezone.utc)
        await self.users.update_one(
            {"user_id": int(user_id)},
            {"$set": {"username": username, "first_name": first_name, "last_name": last_name, "updated_at": now},
             "$setOnInsert": {"user_id": int(user_id), "created_at": now, "daily_forwards": 0, "quota_date": self._today_str()}},
            upsert=True,
        )

    async def get_user(self, user_id):
        return await self.users.find_one({"user_id": int(user_id)})

    async def get_total_users(self):
        return await self.users.count_documents({})

    async def _ensure_user_quota_fields(self, user_id):
        await self.users.update_one(
            {"user_id": int(user_id)},
            {"$setOnInsert": {"user_id": int(user_id), "daily_forwards": 0, "quota_date": self._today_str()}},
            upsert=True,
        )

    async def _reset_quota_if_needed(self, user_id):
        today = self._today_str()
        doc = await self.users.find_one({"user_id": int(user_id)}, {"quota_date": 1, "daily_forwards": 1})
        if not doc or doc.get("quota_date") != today:
            await self.users.update_one(
                {"user_id": int(user_id)},
                {"$set": {"daily_forwards": 0, "quota_date": today}},
                upsert=True,
            )

    async def get_user_daily_forwards(self, user_id) -> int:
        await self._reset_quota_if_needed(user_id)
        doc = await self.users.find_one({"user_id": int(user_id)}, {"daily_forwards": 1})
        return int((doc or {}).get("daily_forwards") or 0)

    async def is_bot_admin(self, user_id) -> bool:
        return await self.bot_admins.find_one({"user_id": int(user_id)}) is not None

    async def is_bot_owner(self, user_id) -> bool:
        return await self.is_bot_admin(user_id)

    async def try_consume_quota(self, user_id) -> bool:
        await self._reset_quota_if_needed(user_id)
        if await self.is_bot_admin(user_id):
            await self.users.update_one({"user_id": int(user_id)}, {"$inc": {"daily_forwards": 1}})
            return True
        res = await self.users.find_one_and_update(
            {"user_id": int(user_id), "daily_forwards": {"$lt": DAILY_FORWARD_LIMIT}},
            {"$inc": {"daily_forwards": 1}},
            return_document=True,
        )
        return res is not None

    async def get_user_quota_info(self, user_id) -> Dict[str, Any]:
        await self._reset_quota_if_needed(user_id)
        used = await self.get_user_daily_forwards(user_id)
        admin = await self.is_bot_admin(user_id)
        return {"used": used, "limit": None if admin else DAILY_FORWARD_LIMIT, "remaining": None if admin else max(0, DAILY_FORWARD_LIMIT - used), "is_admin": admin}

    def _default_global_copy(self) -> Dict[str, Any]:
        return {
            "enabled": False, "target_chat_id": None, "allowed_types": ["all"],
            "block_words": [], "whitelist_words": [], "replacements": [],
            "add_caption": None, "caption_position": "end", "custom_caption": None,
            "remove_old_caption": False, "remove_links": False, "buttons": None,
            "delay": 0, "anti_dupe": False, "forward_tag": False, "my_account_id": None,
        }

    async def get_global_copy(self, user_id) -> Optional[Dict[str, Any]]:
        doc = await self.users.find_one({"user_id": int(user_id)}, {"global_copy": 1})
        if not doc:
            return None
        gc = doc.get("global_copy") or {}
        base = self._default_global_copy()
        base.update({k: gc.get(k, base[k]) for k in base})
        return base

    async def set_global_copy(self, user_id, enabled, target_chat_id=None, allowed_types=None, **extra):
        await self._ensure_user_quota_fields(user_id)
        existing = await self.get_global_copy(user_id) or self._default_global_copy()
        update = {
            "global_copy.enabled": bool(enabled),
            "global_copy.allowed_types": allowed_types or existing.get("allowed_types") or ["all"],
        }
        if target_chat_id is not None:
            update["global_copy.target_chat_id"] = int(target_chat_id) if target_chat_id else None
        for k, v in extra.items():
            if k in GLOBAL_COPY_FILTER_KEYS or k == "my_account_id":
                update[f"global_copy.{k}"] = v
        await self.users.update_one({"user_id": int(user_id)}, {"$set": update}, upsert=True)

    async def update_global_copy_filters(self, user_id, updates: dict):
        allowed = set(GLOBAL_COPY_FILTER_KEYS) | {"my_account_id"}
        clean = {k: v for k, v in updates.items() if k in allowed}
        if not clean:
            return
        set_ops = {f"global_copy.{k}": v for k, v in clean.items()}
        await self.users.update_one({"user_id": int(user_id)}, {"$set": set_ops}, upsert=True)

    async def disable_global_copy(self, user_id):
        await self.users.update_one({"user_id": int(user_id)}, {"$set": {"global_copy.enabled": False}})

    def _normalize_word_list(self, words):
        if not words:
            return []
        if isinstance(words, str):
            words = [w.strip() for w in words.replace(",", "\n").split("\n") if w.strip()]
        return [str(w).strip().lower() for w in words if str(w).strip()]

    def _validate_rule_data(self, data: dict) -> dict:
        out = dict(data)
        pos = (out.get("caption_position") or "end").lower()
        out["caption_position"] = pos if pos in ALLOWED_CAPTION_POSITIONS else "end"
        via = (out.get("forward_via") or "user_bot").lower()
        out["forward_via"] = via if via in ALLOWED_FORWARD_VIA else "user_bot"
        types = out.get("allowed_types") or ["all"]
        if isinstance(types, str):
            types = [t.strip().lower() for t in types.replace(",", " ").split() if t.strip()]
        out["allowed_types"] = [t for t in types if t in ALLOWED_MEDIA_TYPES] or ["all"]
        out["block_words"] = self._normalize_word_list(out.get("block_words"))
        out["whitelist_words"] = self._normalize_word_list(out.get("whitelist_words"))
        out["delay"] = max(0, int(out.get("delay") or 0))
        out["enabled"] = bool(out.get("enabled", True))
        out["forward_tag"] = bool(out.get("forward_tag", False))
        out["remove_links"] = bool(out.get("remove_links", False))
        out["anti_dupe"] = bool(out.get("anti_dupe", False))
        out["remove_old_caption"] = bool(out.get("remove_old_caption", False))
        if out.get("my_bot_id") is not None:
            out["my_bot_id"] = str(out["my_bot_id"]) if out["my_bot_id"] else None
        if out.get("my_account_id") is not None:
            out["my_account_id"] = str(out["my_account_id"]) if out["my_account_id"] else None
        return out

    def _invalidate_source_cache(self, source_chat_id=None):
        if source_chat_id is None:
            self._rules_by_source_cache.clear()
        else:
            self._rules_by_source_cache.pop(int(source_chat_id), None)

    async def create_forward_rule(self, source_chat_id, target_chat_id, owner_id, **kwargs):
        data = self._validate_rule_data({
            "source_chat_id": int(source_chat_id), "target_chat_id": int(target_chat_id),
            "owner_id": int(owner_id), **kwargs,
        })
        data["created_at"] = datetime.now(timezone.utc)
        data["updated_at"] = data["created_at"]
        try:
            await self.forward_rules.insert_one(data)
        except DuplicateKeyError:
            # Same owner re-saving same pair — update their rule only
            await self.forward_rules.update_one(
                {
                    "owner_id": data["owner_id"],
                    "source_chat_id": data["source_chat_id"],
                    "target_chat_id": data["target_chat_id"],
                },
                {"$set": {**data, "updated_at": datetime.now(timezone.utc)}},
            )
        self._invalidate_source_cache(data["source_chat_id"])
        return data

    async def update_forward_rule(self, source_chat_id, target_chat_id, updates: dict, owner_id=None):
        clean = self._validate_rule_data({**updates, "source_chat_id": source_chat_id, "target_chat_id": target_chat_id})
        clean.pop("source_chat_id", None)
        clean.pop("target_chat_id", None)
        clean["updated_at"] = datetime.now(timezone.utc)
        q = {"source_chat_id": int(source_chat_id), "target_chat_id": int(target_chat_id)}
        if owner_id is not None:
            q["owner_id"] = int(owner_id)
        await self.forward_rules.update_one(
            q,
            {"$set": clean},
        )
        self._invalidate_source_cache(source_chat_id)

    async def delete_forward_rule(self, source_chat_id, target_chat_id, owner_id=None):
        q = {"source_chat_id": int(source_chat_id), "target_chat_id": int(target_chat_id)}
        if owner_id is not None:
            q["owner_id"] = int(owner_id)
        await self.forward_rules.delete_one(q)
        self._invalidate_source_cache(source_chat_id)

    async def get_forward_rule(self, source_chat_id, target_chat_id, owner_id=None):
        q = {"source_chat_id": int(source_chat_id), "target_chat_id": int(target_chat_id)}
        if owner_id is not None:
            q["owner_id"] = int(owner_id)
        return await self.forward_rules.find_one(q)

    async def get_rules_by_source(self, source_chat_id, only_enabled=True):
        sid = int(source_chat_id)
        now = time.time()
        cached = self._rules_by_source_cache.get(sid)
        if cached and (now - cached[0]) < RULE_CACHE_TTL_SECONDS:
            rules = cached[1]
        else:
            q = {"source_chat_id": sid}
            if only_enabled:
                q["enabled"] = True
            rules = await self.forward_rules.find(q, RULE_FORWARD_PROJECTION).to_list(length=None)
            self._rules_by_source_cache[sid] = (now, rules)
        if only_enabled:
            return [r for r in rules if r.get("enabled", True)]
        return rules

    async def get_rules_by_owner(self, owner_id):
        return await self.forward_rules.find({"owner_id": int(owner_id)}).to_list(length=None)

    async def get_total_rules(self):
        return await self.forward_rules.count_documents({})

    async def get_total_enabled_rules(self):
        return await self.forward_rules.count_documents({"enabled": True})

    async def set_rule_enabled(self, source_chat_id, target_chat_id, enabled, owner_id=None):
        q = {"source_chat_id": int(source_chat_id), "target_chat_id": int(target_chat_id)}
        if owner_id is not None:
            q["owner_id"] = int(owner_id)
        await self.forward_rules.update_one(
            q,
            {"$set": {"enabled": bool(enabled), "updated_at": datetime.now(timezone.utc)}},
        )
        self._invalidate_source_cache(source_chat_id)

    async def delete_all_rules_of_user(self, owner_id):
        rules = await self.get_rules_by_owner(owner_id)
        res = await self.forward_rules.delete_many({"owner_id": int(owner_id)})
        for r in rules:
            self._invalidate_source_cache(r.get("source_chat_id"))
        return int(getattr(res, "deleted_count", 0) or len(rules))

    async def set_add_caption(self, s, t, caption, position="end", owner_id=None):
        await self.update_forward_rule(s, t, {"add_caption": caption, "caption_position": position}, owner_id=owner_id)

    async def set_custom_caption(self, s, t, template, owner_id=None):
        await self.update_forward_rule(s, t, {"custom_caption": template}, owner_id=owner_id)

    async def set_remove_old_caption(self, s, t, remove, owner_id=None):
        await self.update_forward_rule(s, t, {"remove_old_caption": bool(remove)}, owner_id=owner_id)

    async def set_replacements(self, s, t, replacements, owner_id=None):
        await self.update_forward_rule(s, t, {"replacements": replacements or []}, owner_id=owner_id)

    async def set_block_words(self, s, t, words, owner_id=None):
        await self.update_forward_rule(s, t, {"block_words": self._normalize_word_list(words)}, owner_id=owner_id)

    async def set_whitelist_words(self, s, t, words, owner_id=None):
        await self.update_forward_rule(s, t, {"whitelist_words": self._normalize_word_list(words)}, owner_id=owner_id)

    async def set_buttons(self, s, t, buttons, owner_id=None):
        await self.update_forward_rule(s, t, {"buttons": buttons}, owner_id=owner_id)

    async def set_forward_tag(self, s, t, enabled, owner_id=None):
        await self.update_forward_rule(s, t, {"forward_tag": bool(enabled)}, owner_id=owner_id)

    async def set_remove_links(self, s, t, enabled, owner_id=None):
        await self.update_forward_rule(s, t, {"remove_links": bool(enabled)}, owner_id=owner_id)

    async def set_allowed_types(self, s, t, types, owner_id=None):
        await self.update_forward_rule(s, t, {"allowed_types": types}, owner_id=owner_id)

    async def set_delay(self, s, t, delay_seconds, owner_id=None):
        await self.update_forward_rule(s, t, {"delay": max(0, int(delay_seconds or 0))}, owner_id=owner_id)

    async def set_anti_dupe(self, s, t, enabled, owner_id=None):
        await self.update_forward_rule(s, t, {"anti_dupe": bool(enabled)}, owner_id=owner_id)

    # sessions
    async def save_user_session(self, user_id, session_string, phone_number, tg_user_id,
                                tg_username=None, tg_first_name=None, tg_last_name=None, dc_id=None):
        enc = encrypt_session(session_string)
        now = datetime.now(timezone.utc)
        await self.user_sessions.update_one(
            {"user_id": int(user_id)},
            {"$set": {
                "user_id": int(user_id), "session_encrypted": enc, "phone_number": phone_number,
                "tg_user_id": tg_user_id, "tg_username": tg_username, "tg_first_name": tg_first_name,
                "tg_last_name": tg_last_name, "dc_id": dc_id, "active": True, "updated_at": now,
            }, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )

    async def get_user_session(self, user_id, include_session=False):
        proj = None if include_session else {"session_encrypted": 0}
        return await self.user_sessions.find_one({"user_id": int(user_id)}, proj)

    async def get_decrypted_session_string(self, user_id) -> Optional[str]:
        doc = await self.user_sessions.find_one({"user_id": int(user_id)}, {"session_encrypted": 1})
        if not doc or not doc.get("session_encrypted"):
            return None
        return decrypt_session(doc["session_encrypted"])

    async def has_active_session(self, user_id) -> bool:
        doc = await self.user_sessions.find_one({"user_id": int(user_id), "active": True}, {"_id": 1})
        return doc is not None

    async def mark_session_inactive(self, user_id):
        await self.user_sessions.update_one({"user_id": int(user_id)}, {"$set": {"active": False}})

    async def mark_session_active(self, user_id):
        await self.user_sessions.update_one({"user_id": int(user_id)}, {"$set": {"active": True}})

    async def touch_session(self, user_id):
        await self.user_sessions.update_one({"user_id": int(user_id)}, {"$set": {"updated_at": datetime.now(timezone.utc)}})

    async def delete_user_session(self, user_id):
        await self.user_sessions.delete_one({"user_id": int(user_id)})

    async def get_session_info(self, user_id):
        return await self.get_user_session(user_id, include_session=False)

    async def get_all_sessions_for_startup(self):
        return await self.user_sessions.find({"active": True}).to_list(length=None)

    # bots
    async def save_user_bot(self, user_id, bot_token, bot_id, bot_username=None, bot_name=None):
        enc = encrypt_session(bot_token)
        now = datetime.now(timezone.utc)
        await self.user_bots.update_one(
            {"user_id": int(user_id)},
            {"$set": {
                "user_id": int(user_id), "token_encrypted": enc, "bot_id": bot_id,
                "bot_username": bot_username, "bot_name": bot_name, "active": True, "updated_at": now,
            }, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )

    async def get_user_bot(self, user_id, include_token=False):
        proj = None if include_token else {"token_encrypted": 0}
        return await self.user_bots.find_one({"user_id": int(user_id)}, proj)

    async def get_decrypted_bot_token(self, user_id) -> Optional[str]:
        doc = await self.user_bots.find_one({"user_id": int(user_id)}, {"token_encrypted": 1})
        if not doc or not doc.get("token_encrypted"):
            return None
        return decrypt_session(doc["token_encrypted"])

    async def has_active_bot(self, user_id) -> bool:
        doc = await self.user_bots.find_one({"user_id": int(user_id), "active": True}, {"_id": 1})
        return doc is not None

    async def get_user_bot_info(self, user_id):
        return await self.get_user_bot(user_id, include_token=False)

    async def mark_bot_inactive(self, user_id):
        await self.user_bots.update_one({"user_id": int(user_id)}, {"$set": {"active": False}})

    async def mark_bot_active(self, user_id):
        await self.user_bots.update_one({"user_id": int(user_id)}, {"$set": {"active": True}})

    async def touch_bot(self, user_id):
        await self.user_bots.update_one({"user_id": int(user_id)}, {"$set": {"updated_at": datetime.now(timezone.utc)}})

    async def delete_user_bot(self, user_id):
        await self.user_bots.delete_one({"user_id": int(user_id)})

    async def get_all_bots_for_startup(self):
        return await self.user_bots.find({"active": True}).to_list(length=None)


    async def _get_default_dupe_ttl_seconds(self) -> int:
        """Owner-configurable TTL for default (non-custom) message_hashes."""
        try:
            from core.access import get_system_settings
            from core.cnl.constants import DEFAULT_DUPE_TTL_DAYS
            s = await get_system_settings()
            days = s.get("cnl_default_dupe_ttl_days")
            if days is None:
                days = DEFAULT_DUPE_TTL_DAYS
            days = int(days)
            if days <= 0:
                return 0  # 0 = no TTL (owner disabled)
            return max(1, days) * 86400
        except Exception:
            from core.cnl.constants import DEFAULT_DUPE_TTL_DAYS
            return int(DEFAULT_DUPE_TTL_DAYS) * 86400

    async def _ensure_default_hash_ttl(self) -> None:
        """TTL only on CNL DB message_hashes — not on custom Anti-Dupe DB."""
        seconds = await self._get_default_dupe_ttl_seconds()
        try:
            # Always drop named TTL index first so expireAfterSeconds can change
            try:
                await self.message_hashes.drop_index("hash_created_ttl")
            except Exception:
                pass
            if seconds <= 0:
                return
            await self.message_hashes.create_index(
                [("created_at", ASCENDING)],
                name="hash_created_ttl",
                expireAfterSeconds=int(seconds),
            )
        except Exception:
            logger.debug("ensure default hash TTL failed", exc_info=True)

    async def _ensure_custom_dupe_indexes(self, coll) -> None:
        """Custom Anti-Dupe DB: unique + owner indexes, NO TTL (permanent)."""
        await self._safe_create_index(
            coll,
            [("owner_id", ASCENDING), ("hash", ASCENDING), ("target_chat_id", ASCENDING)],
            unique=True, name="owner_hash_target_unique",
        )
        await self._safe_create_index(coll, [("owner_id", ASCENDING)], name="hash_owner_idx")
        # Explicitly do not create expireAfterSeconds on custom DB

    async def get_dupe_stats(self, owner_id: int) -> dict:
        """Stats for current user's hashes only (default or custom collection)."""
        oid = int(owner_id)
        info = await self.get_dupe_db_info(oid) or {}
        custom = bool(info.get("enabled") and info.get("has_uri"))
        coll = await self._get_dupe_collection(oid)
        out = {
            "mode": "custom" if custom else "default",
            "db_name": (info.get("db_name") if custom else (self.db.name if self.db is not None else "cnl")),
            "collection": "message_hashes",
            "total_hashes": 0,
            "storage": "—",
            "oldest": None,
            "newest": None,
            "ttl": None,
        }
        try:
            q = {"owner_id": oid}
            out["total_hashes"] = await coll.count_documents(q)
            oldest = await coll.find_one(q, sort=[("created_at", 1)], projection={"created_at": 1})
            newest = await coll.find_one(q, sort=[("created_at", -1)], projection={"created_at": 1})
            if oldest and oldest.get("created_at"):
                out["oldest"] = oldest["created_at"].isoformat()
            if newest and newest.get("created_at"):
                out["newest"] = newest["created_at"].isoformat()
        except Exception:
            logger.debug("dupe stats count failed", exc_info=True)
        if custom:
            out["ttl"] = "Permanent (no automatic deletion)"
            try:
                # approx storage for whole custom DB (honest label)
                client = self._dupe_clients.get(oid)
                if client is not None:
                    name = info.get("db_name") or "cnl_dupe"
                    st = await client[name].command("dbStats")
                    def _fmt(n):
                        try:
                            n = float(n or 0)
                        except Exception:
                            return "—"
                        for u in ("B", "KB", "MB", "GB"):
                            if n < 1024:
                                return f"{n:.1f} {u}"
                            n /= 1024
                        return f"{n:.1f} TB"
                    out["storage"] = _fmt(st.get("storageSize"))
                    out["storage_note"] = "Whole custom DB size (may include only your hashes if dedicated)"
            except Exception:
                pass
        else:
            sec = await self._get_default_dupe_ttl_seconds()
            out["ttl"] = f"{sec // 86400} days (auto-delete)" if sec > 0 else "Disabled (no TTL)"
            try:
                # cannot get per-user bytes easily
                out["storage"] = "n/a (shared collection; see count)"
            except Exception:
                pass
        return out

    # dupe db
    async def set_dupe_db(self, user_id, mongo_uri, db_name=None):
        name = db_name or DUPE_DB_NAME
        try:
            _apply_dns()
            test = AsyncMongoClient(mongo_uri, serverSelectionTimeoutMS=6000)
            await test.admin.command("ping")
            await test.close()
        except Exception as e:
            return False, f"Could not connect: {type(e).__name__}: {e}"
        await self._close_dupe_client(user_id)
        enc = encrypt_session(mongo_uri)
        await self.users.update_one(
            {"user_id": int(user_id)},
            {"$set": {
                "dupe_db.uri_encrypted": enc, "dupe_db.db_name": name,
                "dupe_db.enabled": True, "dupe_db.updated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
        try:
            coll = await self._get_dupe_collection(user_id)
            if coll is not self.message_hashes:
                await self._ensure_custom_dupe_indexes(coll)
        except Exception:
            logger.debug("custom dupe indexes failed", exc_info=True)
        return True, "OK"

    async def get_dupe_db_info(self, user_id):
        doc = await self.users.find_one({"user_id": int(user_id)}, {"dupe_db": 1})
        if not doc or not doc.get("dupe_db"):
            return None
        d = doc["dupe_db"]
        return {"enabled": d.get("enabled", False), "db_name": d.get("db_name") or DUPE_DB_NAME, "has_uri": bool(d.get("uri_encrypted"))}

    async def remove_dupe_db(self, user_id):
        await self._close_dupe_client(user_id)
        await self.users.update_one({"user_id": int(user_id)}, {"$unset": {"dupe_db": ""}})

    async def _close_dupe_client(self, user_id):
        c = self._dupe_clients.pop(int(user_id), None)
        if c:
            try:
                await c.close()
            except Exception:
                pass

    async def _get_dupe_collection(self, user_id):
        doc = await self.users.find_one({"user_id": int(user_id)}, {"dupe_db": 1})
        if not doc or not (doc.get("dupe_db") or {}).get("enabled"):
            return self.message_hashes
        d = doc["dupe_db"]
        uid = int(user_id)
        if uid not in self._dupe_clients:
            uri = decrypt_session(d.get("uri_encrypted") or "")
            if not uri:
                return self.message_hashes
            _apply_dns()
            self._dupe_clients[uid] = AsyncMongoClient(uri, serverSelectionTimeoutMS=6000)
        name = d.get("db_name") or DUPE_DB_NAME
        return self._dupe_clients[uid][name]["message_hashes"]

    async def try_claim_hash(self, content_hash, target_chat_id, source_chat_id=None, message_id=None, owner_id=None):
        """Legacy wrapper — prefer try_claim_hash_for_owner with owner_id."""
        if owner_id is not None:
            return await self.try_claim_hash_for_owner(
                owner_id, content_hash, target_chat_id, source_chat_id, message_id
            )
        try:
            doc = {
                "hash": content_hash,
                "target_chat_id": int(target_chat_id),
                "source_chat_id": source_chat_id,
                "message_id": message_id,
                "created_at": datetime.now(timezone.utc),
            }
            await self.message_hashes.insert_one(doc)
            return True
        except DuplicateKeyError:
            return False
        except Exception:
            logger.debug("try_claim_hash failed", exc_info=True)
            return False

    async def try_claim_hash_for_owner(self, owner_id, content_hash, target_chat_id, source_chat_id=None, message_id=None):
        """Claim hash scoped to owner_id (safe on shared Global DB)."""
        coll = await self._get_dupe_collection(owner_id)
        oid = int(owner_id)
        doc = {
            "owner_id": oid,
            "hash": content_hash,
            "target_chat_id": int(target_chat_id),
            "source_chat_id": source_chat_id,
            "message_id": message_id,
            "created_at": datetime.now(timezone.utc),
        }
        try:
            await coll.insert_one(doc)
            return True
        except DuplicateKeyError:
            return False
        except Exception:
            try:
                existing = await coll.find_one({
                    "hash": content_hash,
                    "target_chat_id": int(target_chat_id),
                    "owner_id": oid,
                })
                if existing:
                    return False
                await coll.insert_one(doc)
                return True
            except DuplicateKeyError:
                return False
            except Exception:
                logger.debug("try_claim_hash_for_owner failed", exc_info=True)
                return False

    async def release_hash_for_owner(self, owner_id, content_hash, target_chat_id) -> bool:
        """Drop a hash reservation after failed send / quota reject (anti-dupe)."""
        if not content_hash:
            return False
        coll = await self._get_dupe_collection(owner_id)
        try:
            res = await coll.delete_one({
                "owner_id": int(owner_id),
                "hash": content_hash,
                "target_chat_id": int(target_chat_id),
            })
            return bool(getattr(res, "deleted_count", 0))
        except Exception:
            logger.debug("release_hash_for_owner failed", exc_info=True)
            return False

    async def clear_dupe_for_owner(self, owner_id, target_chat_id=None):
        """Delete only this owner's hashes (never other users on shared DB)."""
        coll = await self._get_dupe_collection(owner_id)
        q = {"owner_id": int(owner_id)}
        if target_chat_id is not None:
            q["target_chat_id"] = int(target_chat_id)
        res = await coll.delete_many(q)
        return int(getattr(res, "deleted_count", 0) or 0)

    async def clear_hashes_for_target(self, target_chat_id, owner_id=None):
        """Prefer owner_id. Without owner_id only clears docs missing owner_id (legacy)."""
        if owner_id is not None:
            return await self.clear_dupe_for_owner(owner_id, target_chat_id)
        await self.message_hashes.delete_many({
            "target_chat_id": int(target_chat_id),
            "$or": [{"owner_id": {"$exists": False}}, {"owner_id": None}],
        })

    async def increment_stat(self, field, amount=1, source_chat_id=None, target_chat_id=None, owner_id=None):
        await self.stats.update_one({"_id": "global"}, {"$inc": {field: amount}}, upsert=True)
        if owner_id is not None:
            await self.stats.update_one(
                {"_id": f"user:{int(owner_id)}"},
                {"$inc": {field: amount}, "$set": {"owner_id": int(owner_id)}},
                upsert=True,
            )

    async def record_forward_success(self, source_chat_id, target_chat_id, owner_id=None):
        await self.increment_stat("forwards", owner_id=owner_id)

    async def record_blocked(self, source_chat_id, target_chat_id, owner_id=None):
        await self.increment_stat("blocked", owner_id=owner_id)

    async def record_failed(self, source_chat_id, target_chat_id, owner_id=None):
        await self.increment_stat("failed", owner_id=owner_id)

    async def record_duplicate_skipped(self, source_chat_id, target_chat_id, owner_id=None):
        await self.increment_stat("duplicates", owner_id=owner_id)

    async def get_stats(self, owner_id=None):
        """Per-user stats when owner_id set; global only for owner tooling."""
        if owner_id is not None:
            doc = await self.stats.find_one({"_id": f"user:{int(owner_id)}"}) or {}
            rules = await self.forward_rules.count_documents({"owner_id": int(owner_id)})
            enabled = await self.forward_rules.count_documents({"owner_id": int(owner_id), "enabled": True})
            return {
                "forwards": doc.get("forwards", 0), "blocked": doc.get("blocked", 0),
                "failed": doc.get("failed", 0), "duplicates": doc.get("duplicates", 0),
                "rules": rules, "enabled_rules": enabled,
                "users": 1, "scope": "user",
            }
        doc = await self.stats.find_one({"_id": "global"}) or {}
        return {
            "forwards": doc.get("forwards", 0), "blocked": doc.get("blocked", 0),
            "failed": doc.get("failed", 0), "duplicates": doc.get("duplicates", 0),
            "rules": await self.get_total_rules(), "enabled_rules": await self.get_total_enabled_rules(),
            "users": await self.get_total_users(), "scope": "global",
        }


    async def wipe_database(self, owner_id: int = None, *, allow_drop: bool = False):
        """Dangerous. Default: never drop collections (Global DB may be shared).

        - owner_id set → delete only that owner's CNL documents
        - allow_drop=True and owner_id is None → full collection drop (dedicated CNL DB only)
        """
        result = {}
        if owner_id is not None:
            oid = int(owner_id)
            # Scoped wipe — safe on shared Global DB
            scopes = {
                "forward_rules": {"owner_id": oid},
                "message_hashes": {"owner_id": oid},
                "users": {"user_id": oid},
                "user_sessions": {"user_id": oid},
                "user_bots": {"user_id": oid},
                "stats": {"_id": f"user:{oid}"},
                "channel_admins": {"user_id": oid},
                "bot_admins": {"user_id": oid},
            }
            for coll_name, q in scopes.items():
                try:
                    res = await self.db[coll_name].delete_many(q)
                    result[coll_name] = int(getattr(res, "deleted_count", 0) or 0)
                except Exception:
                    result[coll_name] = -1
            return result

        if not allow_drop:
            logger.warning("wipe_database refused: pass owner_id=... or allow_drop=True for dedicated DB only")
            return {"error": "refused", "hint": "use owner_id for scoped wipe, or allow_drop=True only on dedicated CNL DB"}

        for coll_name in ("users", "forward_rules", "channel_admins", "bot_admins",
                          "message_hashes", "stats", "user_sessions", "user_bots"):
            try:
                count = await self.db[coll_name].count_documents({})
                await self.db.drop_collection(coll_name)
                result[coll_name] = count
            except Exception:
                result[coll_name] = -1
        await self._ensure_indexes()
        await self._ensure_owners()
        await self._ensure_stats_doc()
        return result


async def get_cnl(user_id) -> Optional[CnlDatabase]:
    uid = int(user_id)
    inst = _INSTANCES.get(uid)
    if inst is not None and inst.is_connected:
        return inst
    uri = None
    db_name = DEFAULT_DB_NAME
    try:
        from core.db_resolver import get_feature_database
        dbh, resolved = await get_feature_database(uid, "cnl")
        if resolved.get("error") and not resolved.get("uri"):
            logger.info("CNL DB not available for %s: %s", uid, resolved.get("source"))
            return None
        uri = resolved.get("uri")
        db_name = resolved.get("db_name") or DEFAULT_DB_NAME
    except Exception:
        logger.exception("CNL resolver failed user %s", uid)
        uri = None
    if not uri:
        return None
    inst = CnlDatabase(uid)
    ok, msg = await inst.connect(uri, db_name)
    if not ok:
        logger.warning("CNL connect failed for %s: %s", uid, msg)
        return None
    _INSTANCES[uid] = inst
    return inst


async def close_cnl(user_id):
    inst = _INSTANCES.pop(int(user_id), None)
    if inst:
        await inst.close()


async def close_all_cnl():
    for uid in list(_INSTANCES):
        await close_cnl(uid)


async def test_cnl_uri(uri, db_name=None):
    tmp = CnlDatabase(0)
    ok, msg = await tmp.connect(uri, db_name)
    await tmp.close()
    return ok, msg
