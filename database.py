# Complete Database Layer — PyMongo Async API (AsyncMongoClient)
# Motor is not used. All I/O helpers are async and must be awaited.

from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Union
from bson import ObjectId
from pymongo import AsyncMongoClient, ASCENDING, DESCENDING


from pymongo.errors import DuplicateKeyError
import copy
import logging
import os
from enum import Enum

from config import Config

logger = logging.getLogger(__name__)


# ============================================================
# ENUMS / CONSTANTS
# ============================================================

class AccountStatus(str, Enum):
    ACTIVE = "active"
    SLEEPING = "sleeping"
    DISABLED = "disabled"
    ERROR = "error"


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class MethodType(str, Enum):
    BOT = "bot"
    USER = "user"


class AccountStrategy(str, Enum):
    SEQUENTIAL = "sequential"
    MANUAL = "manual"


# ============================================================
# DATABASE CLASS
# ============================================================

class Database:
    def __init__(self):
        self.client = None
        self.db = None

        self.users = None
        self.targets = None
        self.duplicates = None
        self.forward_accounts = None
        self.forward_bots = None
        self.forward_jobs = None
        self.statistics = None
        self.job_logs = None
        self.delete_configs = None

    async def connect(self) -> None:
        """Connect to MongoDB and create indexes."""
        try:
            # Termux: /etc/resolv.conf missing — configure sync+async dnspython
            # before AsyncMongoClient does mongodb+srv SRV lookup.
            from core.dns_fix import apply_termux_dns_fix
            apply_termux_dns_fix()

            self.client = AsyncMongoClient(
                Config.MONGO_URI,
                serverSelectionTimeoutMS=5000
            )
            await self.client.admin.command("ping")

            self.db = self.client[Config.DB_NAME]

            self.users = self.db["users"]
            self.targets = self.db["targets"]
            self.duplicates = self.db["duplicates"]
            self.forward_accounts = self.db["forward_accounts"]
            self.forward_bots = self.db["forward_bots"]
            self.forward_jobs = self.db["forward_jobs"]
            self.statistics = self.db["statistics"]
            self.job_logs = self.db["job_logs"]
            self.delete_configs = self.db["delete_configs"]

            await self._create_indexes()
            logger.info("✅ MongoDB connected successfully")

        except Exception as e:
            logger.error(f"❌ MongoDB connection failed: {e}")
            raise
          
    async def _create_indexes(self) -> None:
        # users
        await self.users.create_index([("user_id", ASCENDING)], unique=True)

        # targets
        await self.targets.create_index([("user_id", ASCENDING)])
        await self.targets.create_index(
            [("user_id", ASCENDING), ("chat_id", ASCENDING)],
            unique=True
        )

        # duplicates
        await self.duplicates.create_index(
            [
                ("user_id", ASCENDING),
                ("target_chat_id", ASCENDING),
                ("unique_file_id", ASCENDING)
            ],
            unique=True
        )
        await self.duplicates.create_index([("created_at", ASCENDING)])

        # forward_accounts
        await self.forward_accounts.create_index([("user_id", ASCENDING)])
        await self.forward_accounts.create_index(
            [("user_id", ASCENDING), ("account_id", ASCENDING)],
            unique=True
        )
        await self.forward_accounts.create_index([("status", ASCENDING)])

        # forward_bots
        await self.forward_bots.create_index([("user_id", ASCENDING)])
        await self.forward_bots.create_index(
            [("user_id", ASCENDING), ("bot_id", ASCENDING)],
            unique=True
        )

        # forward_jobs
        await self.forward_jobs.create_index([("user_id", ASCENDING)])
        await self.forward_jobs.create_index([("status", ASCENDING)])
        await self.forward_jobs.create_index([("created_at", DESCENDING)])
        await self.forward_jobs.create_index(
            [("user_id", ASCENDING), ("status", ASCENDING)]
        )

        # statistics
        await self.statistics.create_index(
            [("user_id", ASCENDING), ("entity_type", ASCENDING), ("entity_id", ASCENDING)],
            unique=True
        )

        # job_logs
        await self.job_logs.create_index([("job_id", ASCENDING)])
        await self.job_logs.create_index([("created_at", DESCENDING)])

        # delete_configs (Delete Manager)
        await self.delete_configs.create_index([("user_id", ASCENDING)])
        await self.delete_configs.create_index(
            [("user_id", ASCENDING), ("delete_config_id", ASCENDING)],
            unique=True,
        )
        await self.delete_configs.create_index(
            [("user_id", ASCENDING), ("target_chat_id", ASCENDING)]
        )
        await self.delete_configs.create_index([("auto_delete", ASCENDING), ("next_run_at", ASCENDING)])

        logger.info("✅ Database indexes created")

    async def close(self) -> None:
        if self.client:
            await self.client.close()
            logger.info("MongoDB connection closed")


# Global instance
db = Database()


# ============================================================
# USERS
# ============================================================

async def ensure_user(user_id: int) -> Dict[str, Any]:
    """Create user if not exists and return the document."""
    user = await db.users.find_one({"user_id": user_id})
    if user:
        return user

    now = datetime.now(timezone.utc)
    doc = {
        "user_id": user_id,
        "is_admin": user_id in Config.ADMINS,
        "created_at": now,
        "updated_at": now
    }
    await db.users.insert_one(doc)
    return doc


def is_admin(user_id: int) -> bool:
    """Legacy sync admin check (Config.ADMINS + owner).
    Prefer `await core.access.is_admin(user_id)` for DB admins + normal-user gates.
    """
    try:
        from core.access import is_admin_sync
        return is_admin_sync(user_id)
    except Exception:
        return user_id in Config.ADMINS


async def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    return await db.users.find_one({"user_id": user_id})


# ============================================================
# TARGETS  (FULLY PRESERVED + EXTENDED)
# ============================================================

DEFAULT_TARGET_SETTINGS = {
    "caption_enabled": False,
    "rich_message_enabled": False,  # Bot API 10.1 rich text for text posts
    "caption_template": "<b>{caption}</b>",
    "replace_enabled": False,
    "replacements": [],                    # [{"from": "...", "to": "..."}]
    "block_words": [],
    "block_words_enabled": True,           # ON/OFF independent of stored list
    "whitelist_mode": False,
    "whitelist": [],
    "remove_links": False,
    "inline_buttons": [],                  # [[{"text": "...", "url": "..."}]]
    "inline_buttons_enabled": True,        # ON/OFF independent of stored buttons
    "media_types": ["photo", "video", "document", "audio", "animation", "voice", "text", "sticker", "video_note"],
    "forward_tag": False,
    "delay": 1.0,
    "anti_duplicate": True,
    "future_new_posts": False,
}


async def add_target(
    user_id: int,
    chat_id: int,
    title: str,
    username: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Add a new target. Returns the document or None if already exists."""
    existing = await db.targets.find_one({"user_id": user_id, "chat_id": chat_id})
    if existing:
        return None

    now = datetime.now(timezone.utc)
    doc = {
        "user_id": user_id,
        "chat_id": chat_id,
        "title": title,
        "username": username,
        "settings": copy.deepcopy(DEFAULT_TARGET_SETTINGS),
        "created_at": now,
        "updated_at": now
    }
    result = await db.targets.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


async def get_user_targets(user_id: int) -> List[Dict[str, Any]]:
    """Return all targets of a user sorted by creation time."""
    cursor = db.targets.find({"user_id": user_id}).sort("created_at", 1)
    return await cursor.to_list(length=None)



async def get_visible_targets(user_id: int) -> List[Dict[str, Any]]:
    """Private targets only (shared scope removed)."""
    return await get_user_targets(user_id)


async def get_visible_bots(user_id: int) -> List[Dict[str, Any]]:
    return await get_user_bots(user_id)


async def get_visible_accounts(user_id: int) -> List[Dict[str, Any]]:
    return await get_user_accounts(user_id)


async def get_visible_jobs(
    user_id: int,
    status: Optional[str] = None,
    limit: int = 50,
    **kwargs,
) -> List[Dict[str, Any]]:
    """Same signature as get_user_jobs for drop-in use."""
    return await get_user_jobs(user_id, status=status, limit=limit)


async def get_visible_wroxen_configs(user_id: int) -> List[Dict[str, Any]]:
    return await get_user_wroxen_configs(user_id)


async def get_visible_delete_configs(user_id: int) -> List[Dict[str, Any]]:
    return await get_user_delete_configs(user_id)


async def get_bot_scoped(user_id: int, bot_id: str) -> Optional[Dict[str, Any]]:
    return await get_bot(user_id, bot_id)


async def get_account_scoped(user_id: int, account_id: str) -> Optional[Dict[str, Any]]:
    return await get_account(user_id, account_id)


async def get_job_scoped(user_id: int, job_id: str) -> Optional[Dict[str, Any]]:
    return await get_job(user_id, job_id)


async def get_target_scoped(user_id: int, chat_id: int) -> Optional[Dict[str, Any]]:
    return await get_target(user_id, chat_id)


async def get_target(user_id: int, chat_id: int) -> Optional[Dict[str, Any]]:
    """Get a specific target of a user."""
    return await db.targets.find_one({"user_id": user_id, "chat_id": chat_id})


async def get_target_by_id(target_id: Union[str, ObjectId]) -> Optional[Dict[str, Any]]:
    if isinstance(target_id, str):
        try:
            target_id = ObjectId(target_id)
        except Exception:
            return None
    return await db.targets.find_one({"_id": target_id})


async def update_target_settings(
    user_id: int,
    chat_id: int,
    settings_update: Dict[str, Any]
) -> bool:
    """
    Update one or more settings of a target.
    Example: {"delay": 2.0, "anti_duplicate": False, "future_new_posts": True}
    """
    set_fields = {f"settings.{k}": v for k, v in settings_update.items()}
    set_fields["updated_at"] = datetime.now(timezone.utc)

    result = await db.targets.update_one(
        {"user_id": user_id, "chat_id": chat_id},
        {"$set": set_fields}
    )
    return result.modified_count > 0


async def update_full_settings(
    user_id: int,
    chat_id: int,
    full_settings: Dict[str, Any]
) -> bool:
    """Replace the entire settings object of a target."""
    result = await db.targets.update_one(
        {"user_id": user_id, "chat_id": chat_id},
        {
            "$set": {
                "settings": full_settings,
                "updated_at": datetime.now(timezone.utc)
            }
        }
    )
    return result.modified_count > 0


async def delete_target(user_id: int, chat_id: int) -> bool:
    """Delete a target and all its duplicate records."""
    result = await db.targets.delete_one({"user_id": user_id, "chat_id": chat_id})
    if result.deleted_count > 0:
        await db.duplicates.delete_many({
            "user_id": user_id,
            "target_chat_id": chat_id
        })
        return True
    return False


async def rename_target(user_id: int, chat_id: int, new_title: str) -> bool:
    result = await db.targets.update_one(
        {"user_id": user_id, "chat_id": chat_id},
        {
            "$set": {
                "title": new_title,
                "updated_at": datetime.now(timezone.utc)
            }
        }
    )
    return result.modified_count > 0


def get_setting(target: Dict[str, Any], key: str, default: Any = None) -> Any:
    """Safe getter for a setting inside target document."""
    return target.get("settings", {}).get(key, default)


# ============================================================
# DUPLICATES (FULLY PRESERVED)
# ============================================================

async def is_duplicate(user_id: int, target_chat_id: int, unique_file_id: str) -> bool:
    doc = await db.duplicates.find_one({
        "user_id": user_id,
        "target_chat_id": target_chat_id,
        "unique_file_id": unique_file_id
    })
    return doc is not None


async def mark_as_forwarded(
    user_id: int,
    target_chat_id: int,
    unique_file_id: str
) -> bool:
    """
    Save unique_file_id for this user + target.
    Returns True if inserted, False if already exists.
    """
    try:
        await db.duplicates.insert_one({
            "user_id": user_id,
            "target_chat_id": target_chat_id,
            "unique_file_id": unique_file_id,
            "created_at": datetime.now(timezone.utc)
        })
        return True
    except DuplicateKeyError:
        return False
    except Exception as e:
        logger.error(f"Error marking duplicate: {e}")
        return False


async def clear_duplicates(user_id: int, target_chat_id: int) -> int:
    result = await db.duplicates.delete_many({
        "user_id": user_id,
        "target_chat_id": target_chat_id
    })
    return result.deleted_count


async def get_duplicate_count(user_id: int, target_chat_id: int) -> int:
    return await db.duplicates.count_documents({
        "user_id": user_id,
        "target_chat_id": target_chat_id
    })


# ============================================================
# FORWARD ACCOUNTS (USER ACCOUNTS)
# ============================================================

async def add_forward_account(
    user_id: int,
    phone: str,
    session_string: str,               # encrypted session
    name: Optional[str] = None,
    forward_limit: int = 500,
    sleep_after_limit_minutes: int = 30
) -> Optional[Dict[str, Any]]:
    """
    Add a new user account.
    Returns the document or None if phone already exists for this user.
    """
    existing = await db.forward_accounts.find_one({
        "user_id": user_id,
        "phone": phone
    })
    if existing:
        return None

    now = datetime.now(timezone.utc)
    account_id = str(ObjectId())

    try:
        from core.security import encrypt_session
        session_string = encrypt_session(session_string)
    except Exception:
        logger.exception("Session encrypt failed — refusing to store plaintext")
        raise

    doc = {
        "user_id": user_id,
        "account_id": account_id,
        "phone": phone,
        "name": name or phone,
        "session_string": session_string,
        "status": AccountStatus.ACTIVE.value,
        "forward_limit": forward_limit,
        "sleep_after_limit_minutes": sleep_after_limit_minutes,
        "forwarded_count": 0,               # current cycle count
        "total_forwarded": 0,
        "sleep_until": None,
        "last_used_at": None,
        "error_message": None,
        "created_at": now,
        "updated_at": now
    }
    result = await db.forward_accounts.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


async def get_user_accounts(user_id: int) -> List[Dict[str, Any]]:
    cursor = db.forward_accounts.find({"user_id": user_id}).sort("created_at", 1)
    return await cursor.to_list(length=None)


async def get_account(user_id: int, account_id: str) -> Optional[Dict[str, Any]]:
    return await db.forward_accounts.find_one({
        "user_id": user_id,
        "account_id": account_id
    })


async def get_account_by_id(account_id: str) -> Optional[Dict[str, Any]]:
    return await db.forward_accounts.find_one({"account_id": account_id})


async def update_account(
    user_id: int,
    account_id: str,
    updates: Dict[str, Any]
) -> bool:
    updates["updated_at"] = datetime.now(timezone.utc)
    result = await db.forward_accounts.update_one(
        {"user_id": user_id, "account_id": account_id},
        {"$set": updates}
    )
    return result.modified_count > 0


async def set_account_status(
    user_id: int,
    account_id: str,
    status: str,
    error_message: Optional[str] = None
) -> bool:
    updates = {
        "status": status,
        "updated_at": datetime.now(timezone.utc)
    }
    if error_message is not None:
        updates["error_message"] = error_message
    if status == AccountStatus.ACTIVE.value:
        updates["error_message"] = None
        updates["sleep_until"] = None

    result = await db.forward_accounts.update_one(
        {"user_id": user_id, "account_id": account_id},
        {"$set": updates}
    )
    return result.modified_count > 0


async def increment_account_forwarded(
    user_id: int,
    account_id: str,
    count: int = 1
) -> Optional[Dict[str, Any]]:
    """
    Atomically increment forwarded_count and total_forwarded.
    If limit reached → put account to sleep.
    Returns the updated account document.
    """
    account = await get_account(user_id, account_id)
    if not account:
        return None

    new_count = account.get("forwarded_count", 0) + count
    limit = account.get("forward_limit", 500)

    updates = {
        "forwarded_count": new_count,
        "total_forwarded": account.get("total_forwarded", 0) + count,
        "last_used_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc)
    }

    if new_count >= limit:
        sleep_minutes = account.get("sleep_after_limit_minutes", 30)
        updates["status"] = AccountStatus.SLEEPING.value
        updates["sleep_until"] = datetime.now(timezone.utc) + timedelta(minutes=sleep_minutes)
        updates["forwarded_count"] = 0   # reset for next cycle

    await db.forward_accounts.update_one(
        {"user_id": user_id, "account_id": account_id},
        {"$set": updates}
    )
    return await get_account(user_id, account_id)


async def wake_sleeping_accounts(user_id: Optional[int] = None) -> int:
    """
    Wake up all accounts whose sleep_until has passed.
    Returns number of accounts woken.
    """
    now = datetime.now(timezone.utc)
    query = {
        "status": AccountStatus.SLEEPING.value,
        "sleep_until": {"$lte": now}
    }
    if user_id is not None:
        query["user_id"] = user_id

    result = await db.forward_accounts.update_many(
        query,
        {
            "$set": {
                "status": AccountStatus.ACTIVE.value,
                "sleep_until": None,
                "forwarded_count": 0,
                "updated_at": now
            }
        }
    )
    return result.modified_count


async def get_available_accounts(
    user_id: int,
    account_ids: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """
    Return accounts that are currently ACTIVE and not sleeping.
    Optionally filter by a list of account_ids.
    """
    query = {
        "user_id": user_id,
        "status": AccountStatus.ACTIVE.value
    }
    if account_ids:
        query["account_id"] = {"$in": account_ids}

    cursor = db.forward_accounts.find(query).sort("last_used_at", 1)
    return await cursor.to_list(length=None)


async def delete_account(user_id: int, account_id: str) -> bool:
    result = await db.forward_accounts.delete_one({
        "user_id": user_id,
        "account_id": account_id
    })
    return result.deleted_count > 0


async def reset_account_cycle(user_id: int, account_id: str) -> bool:
    """Manually reset the current cycle counter."""
    return await update_account(user_id, account_id, {
        "forwarded_count": 0,
        "status": AccountStatus.ACTIVE.value,
        "sleep_until": None
    })


# ============================================================
# FORWARD BOTS
# ============================================================

async def add_forward_bot(
    user_id: int,
    bot_token: str,
    bot_username: Optional[str] = None,
    name: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Add a forwarding bot. Returns document or None if token already exists."""
    import hashlib
    token_hash = hashlib.sha256(bot_token.encode("utf-8")).hexdigest()
    existing = await db.forward_bots.find_one({
        "user_id": user_id,
        "$or": [{"bot_token": bot_token}, {"token_hash": token_hash}],
    })
    if existing:
        return None

    now = datetime.now(timezone.utc)
    bot_id = str(ObjectId())

    try:
        from core.security import encrypt_session
        stored_token = encrypt_session(bot_token)
    except Exception:
        logger.exception("Bot token encrypt failed — refusing to store plaintext")
        raise

    doc = {
        "user_id": user_id,
        "bot_id": bot_id,
        "bot_token": stored_token,
        "token_hash": token_hash,
        "bot_username": bot_username,
        "name": name or (bot_username or f"Bot {bot_id[:6]}"),
        "status": "active",
        "total_forwarded": 0,
        "last_used_at": None,
        "error_message": None,
        "created_at": now,
        "updated_at": now
    }
    result = await db.forward_bots.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


async def get_user_bots(user_id: int) -> List[Dict[str, Any]]:
    cursor = db.forward_bots.find({"user_id": user_id}).sort("created_at", 1)
    return await cursor.to_list(length=None)


async def get_bot(user_id: int, bot_id: str) -> Optional[Dict[str, Any]]:
    return await db.forward_bots.find_one({
        "user_id": user_id,
        "bot_id": bot_id
    })


async def update_bot(user_id: int, bot_id: str, updates: Dict[str, Any]) -> bool:
    updates["updated_at"] = datetime.now(timezone.utc)
    result = await db.forward_bots.update_one(
        {"user_id": user_id, "bot_id": bot_id},
        {"$set": updates}
    )
    return result.modified_count > 0


async def delete_bot(user_id: int, bot_id: str) -> bool:
    result = await db.forward_bots.delete_one({
        "user_id": user_id,
        "bot_id": bot_id
    })
    return result.deleted_count > 0


async def increment_bot_forwarded(user_id: int, bot_id: str, count: int = 1) -> bool:
    result = await db.forward_bots.update_one(
        {"user_id": user_id, "bot_id": bot_id},
        {
            "$inc": {"total_forwarded": count},
            "$set": {
                "last_used_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc)
            }
        }
    )
    return result.modified_count > 0


# ============================================================
# FORWARD JOBS
# ============================================================

async def create_job(
    user_id: int,
    source_chat_id: Union[int, str],
    source_title: str,
    target_chat_ids: List[int],
    method: str,                           # "bot" | "user"
    account_ids: Optional[List[str]] = None,
    bot_id: Optional[str] = None,
    last_msg_id: int = 0,
    skip: int = 0,
    initial_limit: Optional[int] = None,   # None = unlimited until last_msg_id
    future_new_posts: bool = False,
    account_strategy: str = AccountStrategy.SEQUENTIAL.value,
    name: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create a new forward job.
    """
    now = datetime.now(timezone.utc)
    job_id = str(ObjectId())

    doc = {
        "user_id": user_id,
        "job_id": job_id,
        "name": name or f"Job #{job_id[:6]}",
        "source_chat_id": source_chat_id,
        "source_title": source_title,
        "target_chat_ids": target_chat_ids,
        "method": method,
        "account_ids": account_ids or [],
        "bot_id": bot_id,
        "last_msg_id": last_msg_id,
        "skip": skip,
        "current_msg_id": skip,             # progress pointer
        "initial_limit": initial_limit,
        "future_new_posts": future_new_posts,
        "monitor_interval_seconds": 10,
        "last_monitor_at": None,
        "last_detected_msg_id": None,
        "new_posts_forwarded": 0,
        "account_strategy": account_strategy,
        "status": JobStatus.PENDING.value,
        "stats": {
            "fetched": 0,
            "forwarded": 0,
            "skipped_filter": 0,
            "skipped_duplicate": 0,
            "skipped_deleted": 0,
            "errors": 0
        },
        "started_at": None,
        "completed_at": None,
        "error_message": None,
        "created_at": now,
        "updated_at": now
    }
    result = await db.forward_jobs.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


async def get_job(user_id: int, job_id: str) -> Optional[Dict[str, Any]]:
    return await db.forward_jobs.find_one({
        "user_id": user_id,
        "job_id": job_id
    })


async def get_job_by_id(job_id: str) -> Optional[Dict[str, Any]]:
    return await db.forward_jobs.find_one({"job_id": job_id})


async def get_user_jobs(
    user_id: int,
    status: Optional[str] = None,
    limit: int = 50
) -> List[Dict[str, Any]]:
    query = {"user_id": user_id}
    if status:
        query["status"] = status
    cursor = db.forward_jobs.find(query).sort("created_at", DESCENDING).limit(limit)
    return await cursor.to_list(length=None)


async def get_active_jobs(user_id: Optional[int] = None) -> List[Dict[str, Any]]:
    query = {"status": {"$in": [JobStatus.RUNNING.value, JobStatus.PENDING.value]}}
    if user_id is not None:
        query["user_id"] = user_id
    return await db.forward_jobs.find(query).sort("created_at", 1).to_list(length=None)


async def update_job(user_id: int, job_id: str, updates: Dict[str, Any]) -> bool:
    updates["updated_at"] = datetime.now(timezone.utc)
    result = await db.forward_jobs.update_one(
        {"user_id": user_id, "job_id": job_id},
        {"$set": updates}
    )
    return result.modified_count > 0


async def update_job_stats(
    user_id: int,
    job_id: str,
    stats_increment: Dict[str, int],
    current_msg_id: Optional[int] = None
) -> bool:
    """
    Atomically increment job stats.
    Example stats_increment = {"forwarded": 1, "fetched": 1}
    """
    inc = {f"stats.{k}": v for k, v in stats_increment.items()}
    set_fields = {"updated_at": datetime.now(timezone.utc)}
    if current_msg_id is not None:
        set_fields["current_msg_id"] = current_msg_id

    result = await db.forward_jobs.update_one(
        {"user_id": user_id, "job_id": job_id},
        {"$inc": inc, "$set": set_fields}
    )
    return result.modified_count > 0


async def set_job_status(
    user_id: int,
    job_id: str,
    status: str,
    error_message: Optional[str] = None
) -> bool:
    updates = {
        "status": status,
        "updated_at": datetime.now(timezone.utc)
    }
    _cur = await get_job(user_id, job_id)
    if status == JobStatus.RUNNING.value and _cur and not _cur.get("started_at"):
        updates["started_at"] = datetime.now(timezone.utc)
    if status in [JobStatus.COMPLETED.value, JobStatus.CANCELLED.value, JobStatus.FAILED.value]:
        updates["completed_at"] = datetime.now(timezone.utc)
    if error_message is not None:
        updates["error_message"] = error_message

    return await update_job(user_id, job_id, updates)


async def delete_job(user_id: int, job_id: str) -> bool:
    result = await db.forward_jobs.delete_one({
        "user_id": user_id,
        "job_id": job_id
    })
    if result.deleted_count > 0:
        await db.job_logs.delete_many({"job_id": job_id})
        return True
    return False


# ============================================================
# JOB LOGS (optional detailed logging)
# ============================================================

async def add_job_log(
    job_id: str,
    level: str,
    message: str,
    extra: Optional[Dict] = None
) -> None:
    await db.job_logs.insert_one({
        "job_id": job_id,
        "level": level,
        "message": message,
        "extra": extra or {},
        "created_at": datetime.now(timezone.utc)
    })


async def get_job_logs(job_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    cursor = db.job_logs.find({"job_id": job_id}).sort("created_at", DESCENDING).limit(limit)
    return await cursor.to_list(length=None)


async def clear_job_logs(job_id: str) -> int:
    result = await db.job_logs.delete_many({"job_id": job_id})
    return int(result.deleted_count or 0)


def job_monitor_interval(job: Optional[Dict[str, Any]]) -> int:
    """Persisted interval with safe default for old jobs. Max 10 days."""
    if not job:
        return 10
    try:
        n = int(job.get("monitor_interval_seconds") or 10)
    except (TypeError, ValueError):
        n = 10
    return max(5, min(864000, n))  # 5s .. 10 days


# ============================================================
# STATISTICS (Dashboard)
# ============================================================

async def get_or_create_stats(
    user_id: int,
    entity_type: str,          # "account" | "target" | "bot" | "job" | "global"
    entity_id: str
) -> Dict[str, Any]:
    doc = await db.statistics.find_one({
        "user_id": user_id,
        "entity_type": entity_type,
        "entity_id": entity_id
    })
    if doc:
        return doc

    now = datetime.now(timezone.utc)
    doc = {
        "user_id": user_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "forwarded": 0,
        "fetched": 0,
        "duplicates": 0,
        "blocked": 0,
        "errors": 0,
        "created_at": now,
        "updated_at": now
    }
    await db.statistics.insert_one(doc)
    return doc


async def increment_stats(
    user_id: int,
    entity_type: str,
    entity_id: str,
    increments: Dict[str, int]
) -> None:
    """
    increments example: {"forwarded": 1, "duplicates": 1}
    """
    set_on_insert = {
        "user_id": user_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "created_at": datetime.now(timezone.utc)
    }
    inc = {k: v for k, v in increments.items()}
    await db.statistics.update_one(
        {
            "user_id": user_id,
            "entity_type": entity_type,
            "entity_id": entity_id
        },
        {
            "$inc": inc,
            "$set": {"updated_at": datetime.now(timezone.utc)},
            "$setOnInsert": set_on_insert
        },
        upsert=True
    )


async def get_dashboard_counts(user_id: int) -> Dict[str, int]:
    """Quick counts for the main dashboard."""
    return {
        "targets": await db.targets.count_documents({"user_id": user_id}),
        "accounts": await db.forward_accounts.count_documents({"user_id": user_id}),
        "bots": await db.forward_bots.count_documents({"user_id": user_id}),
        "active_jobs": await db.forward_jobs.count_documents({
            "user_id": user_id,
            "status": {"$in": [JobStatus.RUNNING.value, JobStatus.PENDING.value]}
        }),
        "duplicates": await db.duplicates.count_documents({"user_id": user_id}),
    }


async def get_stats_overview(user_id: int) -> Dict[str, Any]:
    """Aggregate counters already stored — does not invent new collections."""
    jobs = await db.forward_jobs.find({"user_id": user_id}).to_list(length=None)
    accounts = await db.forward_accounts.find({"user_id": user_id}).to_list(length=None)
    bots = await db.forward_bots.find({"user_id": user_id}).to_list(length=None)

    def job_count(*statuses: str) -> int:
        return sum(1 for j in jobs if j.get("status") in statuses)

    forwarded = fetched = skipped = dups = errors = 0
    for j in jobs:
        s = j.get("stats") or {}
        forwarded += int(s.get("forwarded") or 0)
        fetched += int(s.get("fetched") or 0)
        skipped += int(s.get("skipped_filter") or 0) + int(s.get("skipped_deleted") or 0)
        dups += int(s.get("skipped_duplicate") or 0)
        errors += int(s.get("errors") or 0)

    return {
        "total_jobs": len(jobs),
        "running_jobs": job_count(JobStatus.RUNNING.value),
        "paused_jobs": job_count(JobStatus.PAUSED.value),
        "completed_jobs": job_count(JobStatus.COMPLETED.value),
        "failed_jobs": job_count(JobStatus.FAILED.value),
        "pending_jobs": job_count(JobStatus.PENDING.value),
        "total_forwarded": forwarded,
        "total_fetched": fetched,
        "total_skipped": skipped,
        "total_duplicates": dups,
        "stored_duplicates": await db.duplicates.count_documents({"user_id": user_id}),
        "total_errors": errors,
        "active_accounts": sum(1 for a in accounts if a.get("status") == AccountStatus.ACTIVE.value),
        "sleeping_accounts": sum(1 for a in accounts if a.get("status") == AccountStatus.SLEEPING.value),
        "disabled_accounts": sum(1 for a in accounts if a.get("status") == AccountStatus.DISABLED.value),
        "error_accounts": sum(1 for a in accounts if a.get("status") == AccountStatus.ERROR.value),
        "active_bots": sum(1 for b in bots if b.get("status") == "active"),
        "disabled_bots": sum(1 for b in bots if b.get("status") != "active"),
        "targets": await db.targets.count_documents({"user_id": user_id}),
    }


async def get_entity_stats(
    user_id: int,
    entity_type: str,
    entity_id: str
) -> Dict[str, Any]:
    doc = await db.statistics.find_one({
        "user_id": user_id,
        "entity_type": entity_type,
        "entity_id": entity_id
    })
    if not doc:
        return {
            "forwarded": 0,
            "fetched": 0,
            "duplicates": 0,
            "blocked": 0,
            "errors": 0,
            "updated_at": None,
        }
    return {
        "forwarded": doc.get("forwarded", 0),
        "fetched": doc.get("fetched", 0),
        "duplicates": doc.get("duplicates", 0),
        "blocked": doc.get("blocked", 0),
        "errors": doc.get("errors", 0),
        "updated_at": doc.get("updated_at"),
    }


# ============================================================
# HELPERS
# ============================================================

async def get_next_available_account(
    user_id: int,
    account_ids: List[str],
    strategy: str = AccountStrategy.SEQUENTIAL.value
) -> Optional[Dict[str, Any]]:
    """
    Pick the next available account according to strategy.
    Currently only sequential is fully implemented (least recently used).
    """
    available = await get_available_accounts(user_id, account_ids)
    if not available:
        return None

    # Sequential = least recently used first
    return available[0]


async def can_use_future_posts(
    user_id: int,
    source_chat_id: Union[int, str],
    method: str,
    bot_id: Optional[str] = None,
    account_ids: Optional[List[str]] = None
) -> bool:
    """
    Placeholder helper – real access check will be done in the worker.
    Here we only check if the required resources exist.
    """
    if method == MethodType.BOT.value:
        if not bot_id:
            return False
        bot = await get_bot(user_id, bot_id)
        return bot is not None and bot.get("status") == "active"

    if method == MethodType.USER.value:
        if not account_ids:
            return False
        accounts = await get_available_accounts(user_id, account_ids)
        return len(accounts) > 0

    return False


# ============================================================
# INDEXING SETTINGS (stored on user doc; media lives in separate Index DB)
# ============================================================

async def get_index_settings(user_id: int) -> Dict[str, Any]:
    """Return index_db_uri (encrypted), index_bot_id, etc."""
    user = await get_user(user_id) or await ensure_user(user_id)
    return {
        "index_db_uri": user.get("index_db_uri"),
        "index_bot_id": user.get("index_bot_id"),
        "index_db_configured": bool(user.get("index_db_uri")),
    }


async def set_index_db_uri(user_id: int, uri: Optional[str]) -> bool:
    """Store encrypted Index MongoDB URI on user. None = remove."""
    await ensure_user(user_id)
    if uri is None or uri == "":
        result = await db.users.update_one(
            {"user_id": user_id},
            {"$unset": {"index_db_uri": ""}, "$set": {"updated_at": datetime.now(timezone.utc)}},
        )
        return result.modified_count >= 0
    from core.security import encrypt_session
    stored = encrypt_session(uri.strip())
    result = await db.users.update_one(
        {"user_id": user_id},
        {"$set": {"index_db_uri": stored, "updated_at": datetime.now(timezone.utc)}},
    )
    return result.modified_count >= 0


async def get_index_db_uri_plain(user_id: int) -> Optional[str]:
    """Decrypt stored Index DB URI for connection. Never log result."""
    user = await get_user(user_id)
    if not user or not user.get("index_db_uri"):
        return None
    from core.security import decrypt_session
    return decrypt_session(user["index_db_uri"])


async def set_index_bot_id(user_id: int, bot_id: Optional[str]) -> bool:
    await ensure_user(user_id)
    if bot_id is None or bot_id == "":
        result = await db.users.update_one(
            {"user_id": user_id},
            {"$unset": {"index_bot_id": ""}, "$set": {"updated_at": datetime.now(timezone.utc)}},
        )
        return result.modified_count >= 0
    result = await db.users.update_one(
        {"user_id": user_id},
        {"$set": {"index_bot_id": bot_id, "updated_at": datetime.now(timezone.utc)}},
    )
    return result.modified_count >= 0


async def get_index_bot_id(user_id: int) -> Optional[str]:
    user = await get_user(user_id)
    if not user:
        return None
    return user.get("index_bot_id")


# ============================================================
# WROXEN SEARCH CONFIGS (metadata in main DB; media in Wroxen DB)
# ============================================================

async def ensure_wroxen_indexes() -> None:
    try:
        await db.db["wroxen_configs"].create_index(
            [("user_id", ASCENDING), ("wroxen_id", ASCENDING)],
            unique=True,
        )
        await db.db["wroxen_configs"].create_index([("user_id", ASCENDING)])
        await db.db["wroxen_configs"].create_index([("bot_id", ASCENDING)])
        await db.db["wroxen_configs"].create_index([("target_chat_id", ASCENDING)])
        await db.db["wroxen_configs"].create_index([("source_chat_id", ASCENDING)])
        await db.db["wroxen_configs"].create_index([("enabled", ASCENDING)])
    except Exception:
        logger.exception("wroxen_configs index create failed")


async def get_wroxen_db_uri_plain(user_id: int) -> Optional[str]:
    user = await get_user(user_id)
    if not user or not user.get("wroxen_db_uri"):
        return None
    from core.security import decrypt_session
    return decrypt_session(user["wroxen_db_uri"])


async def set_wroxen_db_uri(user_id: int, uri: Optional[str]) -> bool:
    await ensure_user(user_id)
    if uri is None or uri == "":
        await db.users.update_one(
            {"user_id": user_id},
            {"$unset": {"wroxen_db_uri": ""}, "$set": {"updated_at": datetime.now(timezone.utc)}},
        )
        return True
    from core.security import encrypt_session
    stored = encrypt_session(uri.strip())
    await db.users.update_one(
        {"user_id": user_id},
        {"$set": {"wroxen_db_uri": stored, "updated_at": datetime.now(timezone.utc)}},
    )
    return True


async def create_wroxen_config(
    user_id: int,
    *,
    bot_id: str,
    source_chat_id: int,
    source_title: str,
    target_chat_id: int,
    target_title: str,
    name: Optional[str] = None,
) -> Dict[str, Any]:
    await ensure_wroxen_indexes()
    now = datetime.now(timezone.utc)
    wroxen_id = str(ObjectId())
    doc = {
        "user_id": user_id,
        "wroxen_id": wroxen_id,
        "name": name or f"Wroxen {wroxen_id[:6]}",
        "bot_id": bot_id,
        "source_chat_id": int(source_chat_id),
        "source_title": source_title,
        "target_chat_id": int(target_chat_id),
        "target_title": target_title,
        "enabled": True,
        "auto_index": True,
        "created_at": now,
        "updated_at": now,
        "last_index_at": None,
        "search_count": 0,
    }
    await db.db["wroxen_configs"].insert_one(doc)
    return doc


async def get_user_wroxen_configs(user_id: int) -> List[Dict[str, Any]]:
    return await db.db["wroxen_configs"].find({"user_id": user_id}).sort("created_at", 1).to_list(length=None)


async def get_wroxen_config(user_id: int, wroxen_id: str) -> Optional[Dict[str, Any]]:
    return await db.db["wroxen_configs"].find_one(
        {"user_id": user_id, "wroxen_id": wroxen_id}
    )


async def update_wroxen_config(user_id: int, wroxen_id: str, updates: Dict[str, Any]) -> bool:
    updates = dict(updates)
    updates["updated_at"] = datetime.now(timezone.utc)
    res = await db.db["wroxen_configs"].update_one(
        {"user_id": user_id, "wroxen_id": wroxen_id},
        {"$set": updates},
    )
    return res.modified_count > 0


async def delete_wroxen_config(user_id: int, wroxen_id: str) -> bool:
    res = await db.db["wroxen_configs"].delete_one(
        {"user_id": user_id, "wroxen_id": wroxen_id}
    )
    return res.deleted_count > 0


async def list_all_enabled_wroxen() -> List[Dict[str, Any]]:
    return await db.db["wroxen_configs"].find({"enabled": True}).to_list(length=None)


# ============================================================
# DELETE MANAGER
# ============================================================

DEFAULT_DELETE_TYPES = [
    "text", "photo", "video", "document", "audio", "voice",
    "animation", "sticker", "poll", "contact", "location", "other",
]


async def create_delete_config(
    user_id: int,
    target_chat_id: int,
    target_title: str,
    account_id: str,
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    cid = str(ObjectId())
    interval = 86400
    doc = {
        "user_id": user_id,
        "delete_config_id": cid,
        "target_chat_id": int(target_chat_id),
        "target_title": target_title or str(target_chat_id),
        "account_id": account_id,
        "enabled": True,
        "auto_delete": False,
        "check_interval_seconds": interval,
        "message_age_seconds": 7 * 86400,
        "message_types": list(DEFAULT_DELETE_TYPES),
        "protected_user_ids": [],
        "protected_message_ids": [],
        "stats": {
            "processed": 0,
            "deleted": 0,
            "skipped": 0,
            "protected": 0,
            "failed": 0,
        },
        "last_run_at": None,
        "next_run_at": None,
        "last_error": None,
        "created_at": now,
        "updated_at": now,
    }
    await db.delete_configs.insert_one(doc)
    return doc


async def get_user_delete_configs(user_id: int) -> List[Dict[str, Any]]:
    return await db.delete_configs.find({"user_id": user_id}).sort("created_at", 1).to_list(length=None)


async def get_delete_config(user_id: int, config_id: str) -> Optional[Dict[str, Any]]:
    return await db.delete_configs.find_one(
        {"user_id": user_id, "delete_config_id": config_id}
    )


async def get_delete_config_by_id(config_id: str) -> Optional[Dict[str, Any]]:
    return await db.delete_configs.find_one({"delete_config_id": config_id})


async def update_delete_config(user_id: int, config_id: str, updates: Dict[str, Any]) -> bool:
    updates = dict(updates)
    updates["updated_at"] = datetime.now(timezone.utc)
    res = await db.delete_configs.update_one(
        {"user_id": user_id, "delete_config_id": config_id},
        {"$set": updates},
    )
    return res.modified_count > 0 or res.matched_count > 0


async def delete_delete_config(user_id: int, config_id: str) -> bool:
    res = await db.delete_configs.delete_one(
        {"user_id": user_id, "delete_config_id": config_id}
    )
    return res.deleted_count > 0


async def bump_delete_stats(
    user_id: int,
    config_id: str,
    increments: Dict[str, int],
    last_run_at: Optional[datetime] = None,
) -> None:
    inc = {f"stats.{k}": int(v) for k, v in increments.items() if v}
    sets: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
    if last_run_at is not None:
        sets["last_run_at"] = last_run_at
    update: Dict[str, Any] = {"$set": sets}
    if inc:
        update["$inc"] = inc
    await db.delete_configs.update_one(
        {"user_id": user_id, "delete_config_id": config_id},
        update,
    )


async def list_due_delete_configs() -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc)
    return await db.delete_configs.find(
            {
                "auto_delete": True,
                "enabled": True,
                "$or": [
                    {"next_run_at": None},
                    {"next_run_at": {"$lte": now}},
                ],
            }
        ).to_list(length=None)
