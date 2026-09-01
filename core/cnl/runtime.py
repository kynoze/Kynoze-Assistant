"""CNL runtime startup/shutdown — uses centralized lifecycle."""
from __future__ import annotations
import logging
from core.cnl.gate import list_enabled_gates, get_gate_uri_plain
from core.cnl.db import get_cnl, close_all_cnl

logger = logging.getLogger(__name__)


async def start_cnl_runtime():
    gates = await list_enabled_gates()
    if not gates:
        logger.info("CNL: no enabled gates")
        return
    for g in gates:
        uid = int(g["user_id"])
        if not await get_gate_uri_plain(uid):
            continue
        cnl = await get_cnl(uid)
        if not cnl:
            logger.warning("CNL connect failed user %s", uid)
            continue
        try:
            from core.lifecycle import reconcile_cnl_user
            await reconcile_cnl_user(uid)
            logger.info("CNL ready user %s", uid)
        except Exception:
            logger.exception("CNL start failed %s", uid)


async def stop_cnl_runtime():
    try:
        from core.lifecycle import shutdown_lifecycle
        # shutdown_lifecycle stops cnl+wroxen; only cnl bots/clients if needed
        from core.cnl.bots import get_user_bot_manager
        from core.cnl.clients import get_user_client_manager
        await get_user_bot_manager().stop_all()
        await get_user_client_manager().stop_all()
    except Exception:
        logger.exception("cnl runtime stop")
    try:
        await close_all_cnl()
    except Exception:
        logger.exception("cnl close")
