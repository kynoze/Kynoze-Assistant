"""
Centralized client/runtime lifecycle with dependency tracking.

acquire_* when a feature needs a bot/account
release_* when that feature no longer needs it
reconcile_* rebuilds from DB (startup / after bulk changes)

Quick Forward uses the Management Bot — never registered here.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# key -> set of dependency ids, e.g. "bot:123:botid" -> {"cnl:sid:tid", "wroxen:wid"}
_bot_deps: Dict[str, Set[str]] = {}
_acc_deps: Dict[str, Set[str]] = {}
_lock = asyncio.Lock()
_last_error: Dict[str, str] = {}


def _bot_key(user_id: int, bot_id: str) -> str:
    return f"bot:{int(user_id)}:{bot_id}"


def _acc_key(user_id: int, account_id: str) -> str:
    return f"acc:{int(user_id)}:{account_id}"


def cnl_rule_dep(sid: int, tid: int) -> str:
    return f"cnl:{int(sid)}:{int(tid)}"


def wroxen_dep(wid: str) -> str:
    return f"wroxen:{wid}"


def job_dep(job_id: str) -> str:
    return f"job:{job_id}"


def get_last_error(key: str) -> Optional[str]:
    return _last_error.get(key)


# ── My Bot ────────────────────────────────────────────────────────────────

async def acquire_my_bot(user_id: int, bot_id: str, dep: str) -> Tuple[bool, str]:
    """Register dependency and start CNL bot client if needed."""
    if not bot_id:
        return False, "No bot_id"
    key = _bot_key(user_id, bot_id)
    async with _lock:
        try:
            from core.access import get_limit
            lim = await get_limit(user_id, "cnl_bots")
        except Exception:
            lim = 3
        if lim is not None:
            active_other = sum(
                1 for k, v in _bot_deps.items()
                if k.startswith(f"bot:{int(user_id)}:") and k != key and v
            )
            if key not in _bot_deps and active_other >= int(lim):
                return False, f"CNL bot client limit ({lim}) reached"
        deps = _bot_deps.setdefault(key, set())
        deps.add(dep)
        need_start = True
    try:
        from database import get_bot
        from handlers.ui import load_secret
        from core.cnl.bots import get_user_bot_manager

        b = await get_bot(user_id, str(bot_id))
        if not b:
            _last_error[key] = "Bot not found in My Bots"
            return False, _last_error[key]
        token = load_secret(b.get("bot_token") or "")
        if not token:
            _last_error[key] = "Could not read bot token"
            return False, _last_error[key]
        mgr = get_user_bot_manager()
        if mgr.is_running(user_id, bot_id):
            _last_error.pop(key, None)
            return True, "already running"
        ok, msg = await mgr.start_user_bot(user_id, bot_token=token, bot_id=str(bot_id))
        if ok:
            _last_error.pop(key, None)
            return True, msg
        _last_error[key] = str(msg)
        return False, str(msg)
    except Exception as e:
        logger.exception("acquire_my_bot %s", key)
        _last_error[key] = f"{type(e).__name__}: {e}"
        return False, _last_error[key]


async def release_my_bot(user_id: int, bot_id: str, dep: str) -> None:
    """Drop dependency; stop client only if no deps remain."""
    if not bot_id:
        return
    key = _bot_key(user_id, bot_id)
    async with _lock:
        deps = _bot_deps.get(key) or set()
        deps.discard(dep)
        if deps:
            _bot_deps[key] = deps
            return
        _bot_deps.pop(key, None)
    try:
        from core.cnl.bots import get_user_bot_manager
        await get_user_bot_manager().stop_user_bot(user_id, bot_id=str(bot_id))
        logger.info("lifecycle: stopped bot %s (no deps)", key)
    except Exception:
        logger.exception("release_my_bot stop %s", key)


# ── My Account (CNL user session) ─────────────────────────────────────────

async def acquire_my_account(user_id: int, account_id: str, dep: str) -> Tuple[bool, str]:
    if not account_id:
        return False, "No account_id"
    key = _acc_key(user_id, account_id)
    async with _lock:
        # Per-user concurrent account client cap (public multi-user protection)
        try:
            from core.access import get_limit
            lim = await get_limit(user_id, "cnl_accounts")
        except Exception:
            lim = 3
        if lim is not None:
            active_other = sum(
                1 for k, v in _acc_deps.items()
                if k.startswith(f"acc:{int(user_id)}:") and k != key and v
            )
            if key not in _acc_deps and active_other >= int(lim):
                return False, f"CNL account client limit ({lim}) reached"
        deps = _acc_deps.setdefault(key, set())
        deps.add(dep)
    try:
        from database import get_account
        from handlers.ui import load_secret
        from core.cnl.clients import get_user_client_manager

        a = await get_account(user_id, str(account_id))
        if not a:
            _last_error[key] = "Account not found in My Accounts"
            return False, _last_error[key]
        from database import AccountStatus
        if (a.get("status") or "").lower() == AccountStatus.DISABLED.value:
            _last_error[key] = "Account is disabled"
            return False, _last_error[key]
        ss = load_secret(a.get("session_string") or "")
        if not ss:
            _last_error[key] = "Could not read session"
            return False, _last_error[key]
        # My Accounts is source of truth — do not duplicate session into CNL DB
        mgr = get_user_client_manager()
        if mgr.is_running(user_id, account_id=str(account_id)):
            _last_error.pop(key, None)
            return True, "already running"
        ok, msg = await mgr.start_user_client(
            user_id, session_string=ss, account_id=str(account_id)
        )
        if ok:
            _last_error.pop(key, None)
            return True, msg
        _last_error[key] = str(msg)
        return False, str(msg)
    except Exception as e:
        logger.exception("acquire_my_account %s", key)
        _last_error[key] = f"{type(e).__name__}: {e}"
        return False, _last_error[key]


async def release_my_account(user_id: int, account_id: str, dep: str) -> None:
    """Stop THIS account only when its own deps hit zero (independent of other accounts)."""
    if not account_id:
        return
    key = _acc_key(user_id, account_id)
    async with _lock:
        deps = _acc_deps.get(key) or set()
        deps.discard(dep)
        if deps:
            _acc_deps[key] = deps
            return
        _acc_deps.pop(key, None)
    try:
        from core.cnl.clients import get_user_client_manager
        await get_user_client_manager().stop_user_client(
            user_id, account_id=str(account_id)
        )
        logger.info("lifecycle: stopped account client %s", key)
    except Exception:
        logger.exception("release_my_account stop %s", key)


# ── CNL reconcile ─────────────────────────────────────────────────────────

async def reconcile_cnl_user(user_id: int) -> None:
    """Rebuild deps from enabled CNL rules and start/stop clients accordingly."""
    from core.cnl.db import get_cnl

    cnl = await get_cnl(user_id)
    if not cnl:
        return
    try:
        rules = await cnl.forward_rules.find({"owner_id": int(user_id)}).to_list(1000)
    except Exception:
        logger.exception("reconcile_cnl_user load rules %s", user_id)
        return

    wanted_bots: Dict[str, Set[str]] = {}
    wanted_accs: Dict[str, Set[str]] = {}
    for r in rules:
        if not r.get("enabled", True):
            continue
        sid, tid = r.get("source_chat_id"), r.get("target_chat_id")
        if sid is None or tid is None:
            continue
        dep = cnl_rule_dep(sid, tid)
        via = (r.get("forward_via") or "user_bot").lower()
        if via == "user_bot":
            bid = r.get("my_bot_id") or r.get("exec_bot_id")
            if bid:
                wanted_bots.setdefault(str(bid), set()).add(dep)
        elif via in ("user_account", "user"):
            aid = r.get("my_account_id") or r.get("exec_account_id")
            if aid:
                wanted_accs.setdefault(str(aid), set()).add(dep)

    # Apply bot deps
    async with _lock:
        # remove stale cnl deps for this user
        prefix = f"bot:{int(user_id)}:"
        for key in list(_bot_deps.keys()):
            if not key.startswith(prefix):
                continue
            _bot_deps[key] = {d for d in _bot_deps[key] if not d.startswith("cnl:")}
            if not _bot_deps[key]:
                _bot_deps.pop(key, None)

    for bid, deps in wanted_bots.items():
        for dep in deps:
            await acquire_my_bot(user_id, bid, dep)

    # stop bots for this user with no deps left
    from core.cnl.bots import get_user_bot_manager
    mgr = get_user_bot_manager()
    async with _lock:
        bot_keys = [k for k in list(_bot_deps.keys()) if k.startswith(f"bot:{int(user_id)}:")]
    # also stop any running CNL bots for user not in wanted
    # scan wanted vs running via selected keys
    for bid in list(wanted_bots.keys()):
        pass
    # release bots that had only cnl deps and are no longer wanted
    async with _lock:
        for key in list(_bot_deps.keys()):
            if not key.startswith(f"bot:{int(user_id)}:"):
                continue
            bid = key.split(":", 2)[-1]
            if bid not in wanted_bots:
                # drop all cnl deps already done; if empty stop
                if not _bot_deps.get(key):
                    _bot_deps.pop(key, None)
                    try:
                        await mgr.stop_user_bot(user_id, bot_id=bid)
                    except Exception:
                        pass

    # Accounts
    async with _lock:
        for key in list(_acc_deps.keys()):
            if not key.startswith(f"acc:{int(user_id)}:"):
                continue
            _acc_deps[key] = {d for d in _acc_deps[key] if not d.startswith("cnl:")}
            if not _acc_deps[key]:
                _acc_deps.pop(key, None)

    for aid, deps in wanted_accs.items():
        for dep in deps:
            await acquire_my_account(user_id, aid, dep)

    # Stop each account that is no longer wanted (per account_id, independent)
    try:
        from core.cnl.clients import get_user_client_manager
        amgr = get_user_client_manager()
        async with _lock:
            stale_acc_keys = [
                k for k in list(_acc_deps.keys())
                if k.startswith(f"acc:{int(user_id)}:")
                and k.split(":", 2)[-1] not in wanted_accs
            ]
            for key in stale_acc_keys:
                if not _acc_deps.get(key):
                    _acc_deps.pop(key, None)
        # Also stop running clients for accounts not in wanted (even if deps already cleared)
        prefix = f"{int(user_id)}:"
        running = list(getattr(amgr, "_clients", {}).keys())
        for rkey in running:
            # keys: "uid" or "uid:account_id"
            if rkey == str(int(user_id)):
                if not wanted_accs:
                    try:
                        await amgr.stop_user_client(user_id)
                    except Exception:
                        pass
                continue
            if not rkey.startswith(prefix):
                continue
            aid = rkey.split(":", 1)[-1]
            if aid not in wanted_accs:
                try:
                    await amgr.stop_user_client(user_id, account_id=str(aid))
                    logger.info("reconcile: stopped unused account client %s", rkey)
                except Exception:
                    logger.exception("reconcile stop account %s", rkey)
        # deps-empty keys for this user accounts not wanted
        async with _lock:
            for key in list(_acc_deps.keys()):
                if not key.startswith(f"acc:{int(user_id)}:"):
                    continue
                aid = key.split(":", 2)[-1]
                if aid not in wanted_accs and not _acc_deps.get(key):
                    _acc_deps.pop(key, None)
                    try:
                        await amgr.stop_user_client(user_id, account_id=str(aid))
                    except Exception:
                        pass
    except Exception:
        logger.exception("reconcile account stop user=%s", user_id)


async def on_cnl_rule_saved(user_id: int, rule: dict) -> Tuple[bool, str]:
    """Call after create/update when rule is enabled."""
    if not rule.get("enabled", True):
        return await on_cnl_rule_disabled(user_id, rule)
    sid, tid = rule.get("source_chat_id"), rule.get("target_chat_id")
    dep = cnl_rule_dep(sid, tid)
    via = (rule.get("forward_via") or "user_bot").lower()
    if via == "user_bot":
        bid = rule.get("my_bot_id") or rule.get("exec_bot_id")
        if not bid:
            return False, "Select a My Bot for this rule"
        return await acquire_my_bot(user_id, str(bid), dep)
    if via in ("user_account", "user"):
        aid = rule.get("my_account_id") or rule.get("exec_account_id")
        if not aid:
            return False, "Select a My Account for this rule"
        return await acquire_my_account(user_id, str(aid), dep)
    return True, "ok"


async def on_cnl_rule_disabled(user_id: int, rule: dict) -> Tuple[bool, str]:
    sid, tid = rule.get("source_chat_id"), rule.get("target_chat_id")
    dep = cnl_rule_dep(sid, tid)
    bid = rule.get("my_bot_id") or rule.get("exec_bot_id")
    aid = rule.get("my_account_id") or rule.get("exec_account_id")
    if bid:
        await release_my_bot(user_id, str(bid), dep)
    if aid:
        await release_my_account(user_id, str(aid), dep)
    return True, "released"


async def on_cnl_rule_deleted(user_id: int, rule: dict) -> None:
    await on_cnl_rule_disabled(user_id, rule)


async def reconcile_all_cnl() -> None:
    from core.cnl.gate import list_enabled_gates
    gates = await list_enabled_gates()
    for g in gates:
        try:
            await reconcile_cnl_user(int(g["user_id"]))
        except Exception:
            logger.exception("reconcile_all_cnl user %s", g.get("user_id"))


async def reconcile_wroxen() -> None:
    """Wroxen already uses refresh_routing with enable flags — delegate."""
    try:
        from core.wroxen.runtime import refresh_routing
        await refresh_routing()
    except Exception:
        logger.exception("reconcile_wroxen")


async def startup_lifecycle() -> None:
    """Call once on bot start after Mongo is up."""
    logger.info("Lifecycle: startup reconciliation…")
    await reconcile_all_cnl()
    await reconcile_wroxen()
    logger.info("Lifecycle: startup done")


async def shutdown_lifecycle() -> None:
    try:
        from core.cnl.bots import get_user_bot_manager
        await get_user_bot_manager().stop_all()
    except Exception:
        logger.exception("shutdown bots")
    try:
        from core.cnl.clients import get_user_client_manager
        await get_user_client_manager().stop_all()
    except Exception:
        logger.exception("shutdown clients")
    try:
        from core.wroxen.runtime import stop_all
        await stop_all()
    except Exception:
        logger.exception("shutdown wroxen")
    _bot_deps.clear()
    _acc_deps.clear()
