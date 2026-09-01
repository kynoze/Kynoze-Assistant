"""CNL user-account client manager — one client per (user_id, account_id)."""
from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

from pyrogram import Client
from pyrogram.handlers import MessageHandler
from pyrogram import filters

logger = logging.getLogger(__name__)


class CnlClientManager:
    def __init__(self, max_clients=100, workers_per_client=8):
        # key: "user_id" (legacy) or "user_id:account_id"
        self._clients: Dict[str, Client] = {}
        self.max_clients = max_clients
        self.workers_per_client = workers_per_client

    def _key(self, user_id: int, account_id: Optional[str] = None) -> str:
        if account_id:
            return f"{int(user_id)}:{account_id}"
        return str(int(user_id))

    def _attach_handler(self, client: Client, owner_id: int, account_id: Optional[str] = None):
        async def _wrapper(c: Client, message):
            from core.cnl.engine import process_and_forward, process_global_copy
            from core.cnl.db import get_cnl
            from database import get_account, AccountStatus
            # Disabled account must not process anything
            if account_id:
                try:
                    a = await get_account(owner_id, str(account_id))
                    if a and (a.get("status") or "").lower() == AccountStatus.DISABLED.value:
                        return
                except Exception:
                    pass
            cnl = await get_cnl(owner_id)
            if not cnl:
                return
            # Global Copy: only the selected account runs it
            try:
                gc = await cnl.get_global_copy(owner_id)
                if gc and gc.get("enabled") and gc.get("my_account_id"):
                    if account_id and str(gc.get("my_account_id")) == str(account_id):
                        await process_global_copy(c, message, owner_id)
            except Exception:
                logger.exception("CNL global copy fail owner=%s", owner_id)
            rules = await cnl.get_rules_by_source(message.chat.id, only_enabled=True)
            for rule in rules:
                if rule.get("forward_via") not in ("user_account", "user"):
                    continue
                if int(rule.get("owner_id") or 0) != int(owner_id):
                    continue
                rule_acc = rule.get("my_account_id") or rule.get("exec_account_id")
                if rule_acc and account_id and str(rule_acc) != str(account_id):
                    continue
                try:
                    await process_and_forward(c, message, rule, owner_id)
                except Exception:
                    logger.exception("CNL account forward fail rule %s", rule.get("_id"))
        client.add_handler(MessageHandler(_wrapper, filters.incoming & ~filters.service), group=50)

    def is_running(self, user_id: int, account_id: Optional[str] = None) -> bool:
        k = self._key(user_id, account_id)
        c = self._clients.get(k)
        if c and getattr(c, "is_connected", False):
            return True
        # legacy single-client key
        if account_id:
            c2 = self._clients.get(str(int(user_id)))
            return bool(c2 and getattr(c2, "is_connected", False))
        return False

    def get_client(self, user_id: int, account_id: Optional[str] = None) -> Optional[Client]:
        k = self._key(user_id, account_id)
        c = self._clients.get(k)
        if c:
            return c
        if account_id:
            return self._clients.get(str(int(user_id)))
        return None

    async def startup_for_owner(self, owner_id: int):
        """Start account clients for enabled CNL rules (per account_id)."""
        from core.cnl.db import get_cnl
        from database import get_account
        from handlers.ui import load_secret

        cnl = await get_cnl(owner_id)
        if not cnl:
            return
        try:
            rules = await cnl.forward_rules.find({
                "owner_id": int(owner_id),
                "enabled": True,
                "forward_via": {"$in": ["user_account", "user"]},
            }).to_list(500)
        except Exception:
            rules = []
        seen = set()
        try:
            gc = await cnl.get_global_copy(owner_id)
            if gc and gc.get("enabled") and gc.get("my_account_id"):
                rules = list(rules) + [{
                    "my_account_id": str(gc["my_account_id"]),
                    "forward_via": "user_account",
                    "enabled": True,
                }]
        except Exception:
            pass
        for r in rules:
            aid = r.get("my_account_id") or r.get("exec_account_id")
            if not aid or str(aid) in seen:
                continue
            seen.add(str(aid))
            try:
                a = await get_account(owner_id, str(aid))
                if not a:
                    continue
                ss = load_secret(a.get("session_string") or "")
                if not ss:
                    continue
                await self.start_user_client(
                    owner_id, session_string=ss, account_id=str(aid), from_startup=True
                )
            except Exception:
                logger.exception("CNL startup account %s for %s", aid, owner_id)

    async def start_user_client(
        self,
        user_id: int,
        session_string: str = None,
        account_id: Optional[str] = None,
        from_startup: bool = False,
    ) -> Tuple[bool, str]:
        from config import Config

        uid = int(user_id)
        key = self._key(uid, account_id)
        if key in self._clients:
            c = self._clients[key]
            if getattr(c, "is_connected", False):
                return True, "already running"
            self._clients.pop(key, None)
        if not session_string:
            return False, "No session string"
        if len(self._clients) >= self.max_clients:
            return False, "Max CNL account clients reached"
        try:
            client = Client(
                name=f"cnl_acc_{key.replace(':', '_')}",
                api_id=Config.API_ID,
                api_hash=Config.API_HASH,
                session_string=session_string,
                in_memory=True,
                workers=self.workers_per_client,
            )
            self._attach_handler(client, uid, account_id)
            await client.start()
            self._clients[key] = client
            logger.info("CNL account client started %s", key)
            return True, "started"
        except Exception as e:
            logger.exception("CNL account start failed %s", key)
            return False, f"{type(e).__name__}: {e}"

    async def stop_user_client(self, user_id: int, account_id: Optional[str] = None) -> None:
        key = self._key(user_id, account_id)
        client = self._clients.pop(key, None)
        if not client and account_id is None:
            # stop all for user
            for k in list(self._clients):
                if k == str(int(user_id)) or k.startswith(f"{int(user_id)}:"):
                    c = self._clients.pop(k, None)
                    if c:
                        try:
                            await c.stop()
                        except Exception:
                            pass
            return
        if client:
            try:
                await client.stop()
            except Exception:
                pass
            logger.info("CNL account client stopped %s", key)

    async def stop_all(self) -> None:
        for k in list(self._clients):
            c = self._clients.pop(k, None)
            if c:
                try:
                    await c.stop()
                except Exception:
                    pass


_mgr: Optional[CnlClientManager] = None


def get_user_client_manager() -> CnlClientManager:
    global _mgr
    if _mgr is None:
        _mgr = CnlClientManager()
    return _mgr
