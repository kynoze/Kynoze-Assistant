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

    async def _ensure_indexes(self):
        await self.users.create_indexes([IndexModel([("user_id", ASCENDING)], unique=True, name="user_id_unique")])
        await self.forward_rules.create_indexes([
            IndexModel([("source_chat_id", ASCENDING), ("target_chat_id", ASCENDING)], unique=True, name="source_target_unique"),
            IndexModel([("source_chat_id", ASCENDING)], name="source_idx"),
            IndexModel([("source_chat_id", ASCENDING)], partialFilterExpression={"enabled": True}, name="source_enabled_partial_idx"),
            IndexModel([("target_chat_id", ASCENDING)], name="target_idx"),
            IndexModel([("owner_id", ASCENDING)], name="owner_idx"),
            IndexModel([("enabled", ASCENDING)], name="enabled_idx"),
        ])
        await self.channel_admins.create_indexes([IndexModel([("chat_id", ASCENDING), ("user_id", ASCENDING)], unique=True, name="chat_user_unique")])
        await self.bot_admins.create_indexes([IndexModel([("user_id", ASCENDING)], unique=True, name="bot_admin_unique")])
        await self.message_hashes.create_indexes([
            IndexModel([("hash", ASCENDING), ("target_chat_id", ASCENDING)], unique=True, name="hash_target_unique"),
            IndexModel([("target_chat_id", ASCENDING)], name="hash_target_idx"),
        ])
        await self.user_sessions.create_indexes([IndexModel([("user_id", ASCENDING)], unique=True, name="session_user_unique")])
        await self.user_bots.create_indexes([IndexModel([("user_id", ASCENDING)], unique=True, name="bot_user_unique")])

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
            "delay": 0, "anti_dupe": False, "forward_tag": False,
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
            if k in GLOBAL_COPY_FILTER_KEYS:
                update[f"global_copy.{k}"] = v
        await self.users.update_one({"user_id": int(user_id)}, {"$set": update}, upsert=True)

    async def update_global_copy_filters(self, user_id, updates: dict):
        clean = {k: v for k, v in updates.items() if k in GLOBAL_COPY_FILTER_KEYS}
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
            await self.forward_rules.update_one(
                {"source_chat_id": data["source_chat_id"], "target_chat_id": data["target_chat_id"]},
                {"$set": {**data, "updated_at": datetime.now(timezone.utc)}},
            )
        self._invalidate_source_cache(data["source_chat_id"])
        return data

    async def update_forward_rule(self, source_chat_id, target_chat_id, updates: dict):
        clean = self._validate_rule_data({**updates, "source_chat_id": source_chat_id, "target_chat_id": target_chat_id})
        clean.pop("source_chat_id", None)
        clean.pop("target_chat_id", None)
        clean["updated_at"] = datetime.now(timezone.utc)
        await self.forward_rules.update_one(
            {"source_chat_id": int(source_chat_id), "target_chat_id": int(target_chat_id)},
            {"$set": clean},
        )
        self._invalidate_source_cache(source_chat_id)

    async def delete_forward_rule(self, source_chat_id, target_chat_id):
        await self.forward_rules.delete_one({"source_chat_id": int(source_chat_id), "target_chat_id": int(target_chat_id)})
        self._invalidate_source_cache(source_chat_id)

    async def get_forward_rule(self, source_chat_id, target_chat_id):
        return await self.forward_rules.find_one({"source_chat_id": int(source_chat_id), "target_chat_id": int(target_chat_id)})

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

    async def set_rule_enabled(self, source_chat_id, target_chat_id, enabled):
        await self.forward_rules.update_one(
            {"source_chat_id": int(source_chat_id), "target_chat_id": int(target_chat_id)},
            {"$set": {"enabled": bool(enabled), "updated_at": datetime.now(timezone.utc)}},
        )
        self._invalidate_source_cache(source_chat_id)

    async def delete_all_rules_of_user(self, owner_id):
        rules = await self.get_rules_by_owner(owner_id)
        await self.forward_rules.delete_many({"owner_id": int(owner_id)})
        for r in rules:
            self._invalidate_source_cache(r.get("source_chat_id"))

    async def set_add_caption(self, s, t, caption, position="end"):
        await self.update_forward_rule(s, t, {"add_caption": caption, "caption_position": position})

    async def set_custom_caption(self, s, t, template):
        await self.update_forward_rule(s, t, {"custom_caption": template})

    async def set_remove_old_caption(self, s, t, remove):
        await self.update_forward_rule(s, t, {"remove_old_caption": bool(remove)})

    async def set_replacements(self, s, t, replacements):
        await self.update_forward_rule(s, t, {"replacements": replacements or []})

    async def set_block_words(self, s, t, words):
        await self.update_forward_rule(s, t, {"block_words": self._normalize_word_list(words)})

    async def set_whitelist_words(self, s, t, words):
        await self.update_forward_rule(s, t, {"whitelist_words": self._normalize_word_list(words)})

    async def set_buttons(self, s, t, buttons):
        await self.update_forward_rule(s, t, {"buttons": buttons})

    async def set_forward_tag(self, s, t, enabled):
        await self.update_forward_rule(s, t, {"forward_tag": bool(enabled)})

    async def set_remove_links(self, s, t, enabled):
        await self.update_forward_rule(s, t, {"remove_links": bool(enabled)})

    async def set_allowed_types(self, s, t, types):
        await self.update_forward_rule(s, t, {"allowed_types": types})

    async def set_delay(self, s, t, delay_seconds):
        await self.update_forward_rule(s, t, {"delay": max(0, int(delay_seconds or 0))})

    async def set_anti_dupe(self, s, t, enabled):
        await self.update_forward_rule(s, t, {"anti_dupe": bool(enabled)})

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

    async def try_claim_hash(self, content_hash, target_chat_id, source_chat_id=None, message_id=None):
        try:
            await self.message_hashes.insert_one({
                "hash": content_hash, "target_chat_id": int(target_chat_id),
                "source_chat_id": source_chat_id, "message_id": message_id,
                "created_at": datetime.now(timezone.utc),
            })
            return True
        except DuplicateKeyError:
            return False

    async def try_claim_hash_for_owner(self, owner_id, content_hash, target_chat_id, source_chat_id=None, message_id=None):
        coll = await self._get_dupe_collection(owner_id)
        try:
            await coll.insert_one({
                "hash": content_hash, "target_chat_id": int(target_chat_id),
                "source_chat_id": source_chat_id, "message_id": message_id,
                "created_at": datetime.now(timezone.utc),
            })
            return True
        except DuplicateKeyError:
            return False
        except Exception:
            return await self.try_claim_hash(content_hash, target_chat_id, source_chat_id, message_id)

    async def clear_dupe_for_owner(self, owner_id, target_chat_id=None):
        coll = await self._get_dupe_collection(owner_id)
        q = {"target_chat_id": int(target_chat_id)} if target_chat_id else {}
        await coll.delete_many(q)

    async def clear_hashes_for_target(self, target_chat_id):
        await self.message_hashes.delete_many({"target_chat_id": int(target_chat_id)})

    async def increment_stat(self, field, amount=1, source_chat_id=None, target_chat_id=None):
        await self.stats.update_one({"_id": "global"}, {"$inc": {field: amount}}, upsert=True)

    async def record_forward_success(self, source_chat_id, target_chat_id, owner_id=None):
        await self.increment_stat("forwards")

    async def record_blocked(self, source_chat_id, target_chat_id):
        await self.increment_stat("blocked")

    async def record_failed(self, source_chat_id, target_chat_id):
        await self.increment_stat("failed")

    async def record_duplicate_skipped(self, source_chat_id, target_chat_id):
        await self.increment_stat("duplicates")

    async def get_stats(self):
        doc = await self.stats.find_one({"_id": "global"}) or {}
        return {
            "forwards": doc.get("forwards", 0), "blocked": doc.get("blocked", 0),
            "failed": doc.get("failed", 0), "duplicates": doc.get("duplicates", 0),
            "rules": await self.get_total_rules(), "enabled_rules": await self.get_total_enabled_rules(),
            "users": await self.get_total_users(),
        }

    async def wipe_database(self):
        result = {}
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
    uri = await get_gate_uri_plain(uid)
    if not uri:
        # Fallback: Global / Main via central resolver
        try:
            from core.db_resolver import resolve_feature_db
            resolved = await resolve_feature_db(uid, "cnl")
            uri = resolved.get("uri")
        except Exception:
            uri = None
    if not uri:
        return None
    gate = await get_gate(uid)
    db_name = (gate or {}).get("db_name") or DEFAULT_DB_NAME
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
