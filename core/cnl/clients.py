"""CNL user-account manager — MTProto clients for live forward + global copy."""
from __future__ import annotations
import asyncio
import logging
from typing import Dict, Optional
from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler
from config import Config
from core.cnl.db import get_cnl

logger = logging.getLogger(__name__)

class CnlClientManager:
    def __init__(self, max_clients=100, workers_per_client=8):
        self._clients: Dict[int, Client] = {}
        self._lock = asyncio.Lock()
        self.max_clients = max_clients
        self.workers = workers_per_client

    def _attach_handler(self, client: Client, owner_id: int):
        async def _rules(c: Client, message):
            from core.cnl.engine import process_and_forward
            cnl = await get_cnl(owner_id)
            if not cnl:
                return
            rules = await cnl.get_rules_by_source(message.chat.id, only_enabled=True)
            for rule in rules:
                if rule.get("forward_via") != "user_account":
                    continue
                if int(rule.get("owner_id") or 0) != int(owner_id):
                    continue
                try:
                    await process_and_forward(c, message, rule, owner_id)
                except Exception:
                    logger.exception("CNL account forward fail")
        async def _gcopy(c: Client, message):
            from core.cnl.engine import process_global_copy
            try:
                await process_global_copy(c, message, owner_id)
            except Exception:
                logger.exception("CNL global copy fail")
        client.add_handler(MessageHandler(_rules, filters.incoming & ~filters.service & ~filters.me), group=50)
        client.add_handler(MessageHandler(_gcopy, filters.incoming & ~filters.service & ~filters.me), group=51)

    async def startup_for_owner(self, owner_id: int):
        cnl = await get_cnl(owner_id)
        if not cnl or not await cnl.has_active_session(owner_id):
            return
        ss = await cnl.get_decrypted_session_string(owner_id)
        if ss:
            await self.start_user_client(owner_id, session_string=ss, from_startup=True)

    async def get_client(self, user_id: int) -> Optional[Client]:
        return self._clients.get(int(user_id))

    async def start_user_client(self, user_id: int, session_string: str = None, from_startup: bool = False) -> tuple:
        uid = int(user_id)
        async with self._lock:
            if uid in self._clients:
                c = self._clients[uid]
                if c.is_connected:
                    return True, "already running"
                try:
                    await c.stop()
                except Exception:
                    pass
                self._clients.pop(uid, None)
            cnl = await get_cnl(uid)
            if not cnl:
                return False, "CNL DB not configured"
            ss = session_string or await cnl.get_decrypted_session_string(uid)
            if not ss:
                return False, "No session"
            if len(self._clients) >= self.max_clients:
                return False, "Max clients reached"
            client = Client(
                name=f"cnl_user_{uid}",
                api_id=Config.API_ID,
                api_hash=Config.API_HASH,
                session_string=ss,
                in_memory=True,
                workers=self.workers,
            )
            self._attach_handler(client, uid)
            try:
                await client.start()
                me = await client.get_me()
                await cnl.mark_session_active(uid)
                await cnl.touch_session(uid)
                self._clients[uid] = client
                uname = f"@{me.username}" if me.username else str(me.id)
                logger.info("CNL account started for %s as %s", uid, uname)
                return True, uname
            except Exception as e:
                try:
                    await client.stop()
                except Exception:
                    pass
                return False, f"{type(e).__name__}: {e}"

    async def stop_user_client(self, user_id: int, delete_session: bool = False):
        uid = int(user_id)
        client = self._clients.pop(uid, None)
        if client:
            try:
                await client.stop()
            except Exception:
                pass
        cnl = await get_cnl(uid)
        if cnl:
            if delete_session:
                await cnl.delete_user_session(uid)
            else:
                await cnl.mark_session_inactive(uid)

    async def disconnect_and_delete(self, user_id: int):
        return await self.stop_user_client(user_id, delete_session=True)

    async def stop_all(self):
        for uid in list(self._clients):
            await self.stop_user_client(uid)

    def is_running(self, user_id: int) -> bool:
        c = self._clients.get(int(user_id))
        return bool(c and c.is_connected)

_mgr: Optional[CnlClientManager] = None

def get_user_client_manager() -> CnlClientManager:
    global _mgr
    if _mgr is None:
        _mgr = CnlClientManager()
    return _mgr
