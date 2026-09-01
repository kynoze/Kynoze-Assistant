"""Runtime health snapshot + dead rule/job detection."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _ago(dt) -> str:
    if not dt:
        return "—"
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        sec = int((datetime.now(timezone.utc) - dt).total_seconds())
    except Exception:
        return "—"
    if sec < 60:
        return f"{sec}s ago"
    if sec < 3600:
        return f"{sec // 60}m ago"
    if sec < 86400:
        return f"{sec // 3600}h ago"
    return f"{sec // 86400}d ago"


async def build_user_health(user_id: int) -> str:
    from database import (
        db, get_user_bots, get_user_accounts, JobStatus,
        get_visible_wroxen_configs, get_visible_delete_configs,
    )
    from handlers.ui import format_bot_label, format_account_label, active_accounts_only

    lines = ["**🩺 Runtime Health**", ""]

    # Jobs
    try:
        jobs = await db.forward_jobs.find({"user_id": int(user_id)}).to_list(200)
    except Exception:
        jobs = []
    running = [j for j in jobs if (j.get("status") or "").lower() == JobStatus.RUNNING.value]
    paused = [j for j in jobs if (j.get("status") or "").lower() == JobStatus.PAUSED.value]
    failed = [j for j in jobs if (j.get("status") or "").lower() == JobStatus.FAILED.value]
    lines.append(f"**📋 Jobs** — run `{len(running)}` · pause `{len(paused)}` · fail `{len(failed)}`")
    for j in running[:8]:
        name = j.get("name") or j.get("job_id")
        cur = j.get("current_msg_id") or 0
        last = j.get("last_msg_id") or 0
        lines.append(f"  🟢 `{name}` · cursor `{cur}`/`{last}`")
    for j in paused[:5]:
        name = j.get("name") or j.get("job_id")
        reason = j.get("pause_reason") or j.get("error_message") or "paused"
        lines.append(f"  ⏸ `{name}` — {str(reason)[:80]}")
    for j in failed[:5]:
        name = j.get("name") or j.get("job_id")
        err = j.get("error_message") or "failed"
        lines.append(f"  🔴 `{name}` — {str(err)[:80]}")
    if not running and not paused and not failed:
        lines.append("  _No active job issues_")
    lines.append("")

    # CNL rules
    try:
        from core.cnl.db import get_cnl
        cnl = await get_cnl(user_id)
        rules = []
        if cnl:
            if hasattr(cnl, "get_rules_by_owner"):
                rules = await cnl.get_rules_by_owner(user_id) or []
            else:
                rules = await cnl.forward_rules.find({"owner_id": int(user_id)}).to_list(200)
    except Exception:
        rules = []
        cnl = None
    en_rules = [r for r in rules if r.get("enabled", True)]
    dis_rules = [r for r in rules if not r.get("enabled", True)]
    lines.append(f"**📡 CNL rules** — on `{len(en_rules)}` · off `{len(dis_rules)}`")
    for r in en_rules[:6]:
        sid, tid = r.get("source_chat_id"), r.get("target_chat_id")
        via = r.get("forward_via") or "user_bot"
        lines.append(f"  🟢 `{sid}`→`{tid}` · via `{via}`")
    dead = [r for r in dis_rules if r.get("last_error")]
    for r in dead[:5]:
        sid, tid = r.get("source_chat_id"), r.get("target_chat_id")
        lines.append(f"  🔴 `{sid}`→`{tid}` — {str(r.get('last_error'))[:80]}")
    if not en_rules and not dead:
        lines.append("  _No CNL activity / dead rules_")
    lines.append("")

    # Wroxen
    try:
        wx = await get_visible_wroxen_configs(user_id)
    except Exception:
        wx = []
    on_wx = [c for c in wx if c.get("enabled", True)]
    lines.append(f"**🔎 Wroxen** — enabled `{len(on_wx)}` / `{len(wx)}`")
    for c in on_wx[:5]:
        lines.append(f"  🟢 {c.get('name') or c.get('wroxen_id')} · bot `{str(c.get('bot_id') or '')[:8]}`")
    lines.append("")

    # Delete manager
    try:
        dels = await get_visible_delete_configs(user_id)
    except Exception:
        dels = []
    auto = [d for d in dels if d.get("auto_delete")]
    err_d = [d for d in dels if d.get("last_error")]
    lines.append(f"**🗑 Delete Manager** — auto `{len(auto)}` · errors `{len(err_d)}`")
    for d in err_d[:5]:
        lines.append(
            f"  🔴 {d.get('target_title') or d.get('target_chat_id')} — "
            f"{str(d.get('last_error'))[:80]}"
        )
    lines.append("")

    # Executors snapshot
    bots = await get_user_bots(user_id)
    accs = active_accounts_only(await get_user_accounts(user_id))
    lines.append(f"**🤖 My Bots:** `{len(bots)}` · **👤 Active accounts:** `{len(accs)}`")
    for b in bots[:5]:
        lines.append(f"  · {format_bot_label(b, short=True)}")
    for a in accs[:5]:
        lines.append(f"  · {format_account_label(a, short=True)}")

    lines.append("")
    lines.append(f"_Updated {_ago(datetime.now(timezone.utc))} · open again to refresh_")
    return "\n".join(lines)


async def list_dead_items(user_id: int) -> List[Dict[str, Any]]:
    """Structured dead/paused items for alerts."""
    out: List[Dict[str, Any]] = []
    from database import db, JobStatus, get_visible_delete_configs

    try:
        jobs = await db.forward_jobs.find({
            "user_id": int(user_id),
            "status": {"$in": [JobStatus.PAUSED.value, JobStatus.FAILED.value]},
        }).to_list(50)
        for j in jobs:
            out.append({
                "feature": "Jobs",
                "title": j.get("name") or j.get("job_id"),
                "reason": j.get("error_message") or j.get("pause_reason") or j.get("status"),
            })
    except Exception:
        pass

    try:
        from core.cnl.db import get_cnl
        cnl = await get_cnl(user_id)
        if cnl:
            rules = await cnl.forward_rules.find({
                "owner_id": int(user_id),
                "enabled": False,
                "last_error": {"$exists": True, "$ne": None},
            }).to_list(50)
            for r in rules:
                out.append({
                    "feature": "CNL",
                    "title": f"{r.get('source_chat_id')}→{r.get('target_chat_id')}",
                    "reason": r.get("last_error"),
                })
    except Exception:
        pass

    try:
        for d in await get_visible_delete_configs(user_id):
            if d.get("last_error") and not d.get("auto_delete"):
                out.append({
                    "feature": "Delete Manager",
                    "title": d.get("target_title") or str(d.get("target_chat_id")),
                    "reason": d.get("last_error"),
                })
    except Exception:
        pass
    return out
