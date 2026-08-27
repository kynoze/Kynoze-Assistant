"""CNL user-bot manager — supports multiple My Bots per owner."""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional, Tuple

from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler

from config import Config
from core.cnl.db import get_cnl

logger = logging.getLogger(__name__)


class CnlBotManager:
    def __init__(self, max_bots=150, workers_per_bot=8):
        # key: "user_id" or "user_id:bot_id"
        self._bots: Dict[str, Client] = {}
        self._lock = asyncio.Lock()
        self.max_bots = max_bots
        self.workers = workers_per_bot

    def _key(self, user_id: int, bot_id: Optional[str] = None) -> str:
        if bot_id:
            return f"{int(user_id)}:{bot_id}"
        return str(int(user_id))

    def _attach_handler(self, client: Client, owner_id: int):
        async def _wrapper(c: Client, message):
            from core.cnl.engine import process_and_forward
            from core.cnl.db import get_cnl
            cnl = await get_cnl(owner_id)
            if not cnl:
                return
            rules = await cnl.get_rules_by_source(message.chat.id, only_enabled=True)
            for rule in rules:
                if rule.get("forward_via") != "user_bot":
                    continue
                if int(rule.get("owner_id") or 0) != int(owner_id):
                    continue
                try:
                    await process_and_forward(c, message, rule, owner_id)
                except Exception:
                    logger.exception("CNL bot forward fail rule %s", rule.get("_id"))
        client.add_handler(MessageHandler(_wrapper, filters.incoming & ~filters.service), group=50)

    def is_running(self, user_id: int, bot_id: Optional[str] = None) -> bool:
        uid = int(user_id)
        if bot_id:
            k = self._key(uid, bot_id)
            c = self._bots.get(k)
            return bool(c and c.is_connected)
        return any(
            c.is_connected
            for k, c in self._bots.items()
            if k == str(uid) or k.startswith(f"{uid}:")
        )

    def running_count(self, user_id: int) -> int:
        uid = int(user_id)
        return sum(
            1 for k, c in self._bots.items()
            if (k == str(uid) or k.startswith(f"{uid}:")) and c.is_connected
        )

    async def startup_for_owner(self, owner_id: int):
        """Start all selected My Bots for this owner."""
        cnl = await get_cnl(owner_id)
        if not cnl:
            return
        ids = await _selected_bot_ids(cnl, owner_id)
        from database import get_bot
        from handlers.ui import load_secret
        for bid in ids:
            try:
                b = await get_bot(owner_id, str(bid))
                if not b:
                    continue
                token = load_secret(b.get("bot_token") or "")
                if token:
                    await self.start_user_bot(owner_id, bot_token=token, bot_id=str(bid), from_startup=True)
            except Exception:
                logger.exception("CNL startup bot %s for %s", bid, owner_id)
        # legacy single token
        if not ids:
            token = await cnl.get_decrypted_bot_token(owner_id)
            if token:
                await self.start_user_bot(owner_id, bot_token=token, from_startup=True)

    async def get_bot(self, user_id: int, bot_id: Optional[str] = None) -> Optional[Client]:
        if bot_id:
            return self._bots.get(self._key(user_id, bot_id))
        uid = int(user_id)
        for k, c in self._bots.items():
            if k == str(uid) or k.startswith(f"{uid}:"):
                if c.is_connected:
                    return c
        return None

    async def start_user_bot(
        self,
        user_id: int,
        bot_token: str = None,
        bot_id: Optional[str] = None,
        from_startup: bool = False,
    ) -> Tuple[bool, str]:
        uid = int(user_id)
        key = self._key(uid, bot_id)
        async with self._lock:
            if key in self._bots:
                b = self._bots[key]
                if b.is_connected:
                    return True, "already running"
                try:
                    await b.stop()
                except Exception:
                    pass
                self._bots.pop(key, None)
            cnl = await get_cnl(uid)
            if not cnl:
                return False, "CNL DB not configured"
            token = bot_token or await cnl.get_decrypted_bot_token(uid)
            if not token:
                return False, "No bot token"
            if len(self._bots) >= self.max_bots:
                return False, "Max bots reached"
            client = Client(
                name=f"cnl_bot_{key.replace(':', '_')}",
                api_id=Config.API_ID,
                api_hash=Config.API_HASH,
                bot_token=token,
                in_memory=True,
                workers=self.workers,
            )
            self._attach_handler(client, uid)
            try:
                await client.start()
                me = await client.get_me()
                await cnl.save_user_bot(uid, token, me.id, me.username, me.first_name)
                if bot_id:
                    await cnl.user_bots.update_one(
                        {"user_id": uid},
                        {"$addToSet": {"selected_bot_ids": str(bot_id)}, "$set": {"main_bot_id": str(bot_id)}},
                        upsert=True,
                    )
                await cnl.mark_bot_active(uid)
                self._bots[key] = client
                uname = f"@{me.username}" if me.username else str(me.id)
                logger.info("CNL bot started for %s key=%s as %s", uid, key, uname)
                return True, uname
            except Exception as e:
                try:
                    await client.stop()
                except Exception:
                    pass
                return False, f"{type(e).__name__}: {e}"

    async def stop_user_bot(self, user_id: int, bot_id: Optional[str] = None) -> None:
        uid = int(user_id)
        async with self._lock:
            keys = []
            if bot_id:
                keys = [self._key(uid, bot_id)]
            else:
                keys = [k for k in list(self._bots.keys()) if k == str(uid) or k.startswith(f"{uid}:")]
            for key in keys:
                c = self._bots.pop(key, None)
                if c:
                    try:
                        await c.stop()
                    except Exception:
                        pass
            cnl = await get_cnl(uid)
            if cnl and not bot_id:
                try:
                    await cnl.mark_bot_inactive(uid)
                except Exception:
                    pass


_mgr: Optional[CnlBotManager] = None


def get_user_bot_manager() -> CnlBotManager:
    global _mgr
    if _mgr is None:
        _mgr = CnlBotManager()
    return _mgr


async def _selected_bot_ids(cnl, owner_id: int) -> list:
    try:
        doc = await cnl.user_bots.find_one({"user_id": int(owner_id)}) or {}
        ids = list(doc.get("selected_bot_ids") or [])
        mid = doc.get("main_bot_id")
        if mid and str(mid) not in ids:
            ids.append(str(mid))
        return [str(x) for x in ids]
    except Exception:
        return []
