# Delete Manager UI — uses existing forwarding user accounts. No new login.

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from pyrogram import Client, filters
from pyrogram.enums import ChatType
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from database import (
    create_delete_config,
    delete_delete_config,
    ensure_user,
    get_account,
    get_delete_config,
    get_user_accounts,
    get_user_delete_configs,
    get_user_targets,
    is_admin,
    update_delete_config,
    get_visible_delete_configs,
)
from handlers.ui import (
    fmt_dt,
    fmt_duration,
    fmt_interval,
    paginate,
    pager_row,
    safe_answer,
    safe_edit,
)
from core.state import get_state, set_state
from core.delete_manager.engine import (
    ALL_TYPES,
    TYPE_LABELS,
    CANCEL,
    RUNNING,
    cancel_delete_job,
    is_delete_running,
    progress_text,
    run_delete_job,
)
from core.delete_manager.permissions import (
    check_delete_permissions,
    permission_fail_text,
)
from core.delete_manager.worker import _next_run

logger = logging.getLogger(__name__)

CHECK_PRESETS = [
    (3600, "1 hour"),
    (6 * 3600, "6 hours"),
    (12 * 3600, "12 hours"),
    (86400, "24 hours"),
    (2 * 86400, "2 days"),
    (7 * 86400, "7 days"),
]
AGE_PRESETS = [
    (86400, "24 hours"),
    (2 * 86400, "2 days"),
    (7 * 86400, "7 days"),
    (30 * 86400, "30 days"),
    (90 * 86400, "90 days"),
    (180 * 86400, "180 days"),
    (365 * 86400, "365 days"),
    (2 * 365 * 86400, "2 years"),
]
MIN_CHECK = 3600
MAX_SPAN = 2 * 365 * 86400
MIN_AGE = 86400


async def _acc_name(user_id: int, account_id: str) -> str:
    from handlers.ui import format_account_label

    acc = await get_account(user_id, account_id)
    if not acc:
        return "—"
    return format_account_label(acc, short=True)


def _types_summary(cfg: dict) -> str:
    selected = cfg.get("message_types") or []
    if not selected or len(selected) >= len(ALL_TYPES):
        return "All"
    return ", ".join(TYPE_LABELS.get(t, t) for t in selected[:6]) + (
        "…" if len(selected) > 6 else ""
    )


def home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Add Group", callback_data="dm:add")],
            [InlineKeyboardButton("📋 Managed Groups", callback_data="dm:list:0")],
            [InlineKeyboardButton("« Dashboard", callback_data="dash:home")],
        ]
    )


async def list_keyboard(user_id: int, page: int = 0) -> InlineKeyboardMarkup:
    configs = await get_visible_delete_configs(user_id)
    slice_, page, total = paginate(configs, page, 8)
    rows = []
    for c in slice_:
        auto = "🟢" if c.get("auto_delete") else "⚪"
        title = (c.get("target_title") or str(c.get("target_chat_id")))[:24]
        rows.append(
            [
                InlineKeyboardButton(
                    f"{auto} {title}",
                    callback_data=f"dm:open:{c['delete_config_id']}",
                )
            ]
        )
    pager = pager_row("dm:list:", page, total)
    if pager:
        rows.append(pager)
    rows.append([InlineKeyboardButton("➕ Add Group", callback_data="dm:add")])
    rows.append([InlineKeyboardButton("« Delete Manager", callback_data="dm:home")])
    return InlineKeyboardMarkup(rows)


def detail_keyboard(cfg: dict) -> InlineKeyboardMarkup:
    cid = cfg["delete_config_id"]
    auto = "🟢 ON" if cfg.get("auto_delete") else "⚪ OFF"
    running = is_delete_running(cid)
    rows = []
    if running:
        rows.append([InlineKeyboardButton("⛔ Cancel", callback_data=f"dm:cancel:{cid}")])
    else:
        rows.append([InlineKeyboardButton("▶️ Delete Now", callback_data=f"dm:now:{cid}")])
    rows.extend(
        [
            [InlineKeyboardButton(f"Auto Delete: {auto}", callback_data=f"dm:auto:{cid}")],
            [
                InlineKeyboardButton("📦 Types", callback_data=f"dm:types:{cid}"),
                InlineKeyboardButton("⏱️ Monitoring", callback_data=f"dm:mon:{cid}"),
            ],
            [
                InlineKeyboardButton("👤 Protected Users", callback_data=f"dm:pusers:{cid}"),
                InlineKeyboardButton("🆔 Protected IDs", callback_data=f"dm:pids:{cid}"),
            ],
            [
                InlineKeyboardButton("📊 Statistics", callback_data=f"dm:stats:{cid}"),
                InlineKeyboardButton("🔄 Refresh", callback_data=f"dm:open:{cid}"),
            ],
            [InlineKeyboardButton("👤 Change Account", callback_data=f"dm:reacc:{cid}")],
            [InlineKeyboardButton("🗑 Remove Configuration", callback_data=f"dm:rm:{cid}")],
            [InlineKeyboardButton("« Managed Groups", callback_data="dm:list:0")],
        ]
    )
    return InlineKeyboardMarkup(rows)


def fail_keyboard(cid: str | None = None) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("🔄 Select Another Account", callback_data="dm:add")]]
    if cid:
        rows = [[InlineKeyboardButton("🔄 Select Another Account", callback_data=f"dm:reacc:{cid}")]]
    rows.append([InlineKeyboardButton("« Back", callback_data="dm:home")])
    return InlineKeyboardMarkup(rows)


async def detail_text(user_id: int, cfg: dict) -> str:
    auto = "🟢 ON" if cfg.get("auto_delete") else "⚪ OFF"
    running = "🔄 Running" if is_delete_running(cfg["delete_config_id"]) else "—"
    err = cfg.get("last_error")
    err_line = f"\n🔴 Last error: {err}" if err else ""
    acc_name = await _acc_name(user_id, cfg.get("account_id"))
    return (
        "🗑️ **Delete Manager**\n\n"
        f"👥 **Group:** {cfg.get('target_title')}\n"
        f"👤 **Account:** {acc_name}\n\n"
        f"Auto Delete: **{auto}**\n"
        f"🔍 Check Every: **{fmt_interval(int(cfg.get('check_interval_seconds') or 86400))}**\n"
        f"🗑️ Delete After: **{fmt_interval(int(cfg.get('message_age_seconds') or 86400))}**\n\n"
        f"📦 Types: {_types_summary(cfg)}\n"
        f"🚫 Protected Users: **{len(cfg.get('protected_user_ids') or [])}**\n"
        f"🚫 Protected IDs: **{len(cfg.get('protected_message_ids') or [])}**\n"
        f"Job: {running}{err_line}"
    )


async def home_text(user_id: int) -> str:
    n = len(await get_visible_delete_configs(user_id))
    return (
        "🗑️ **Delete Manager**\n\n"
        "Delete messages in groups using an **existing forwarding account**.\n"
        "No new login. Admin + delete permission is checked live before every job.\n\n"
        f"Configurations: **{n}**"
    )


async def show_delete_home(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    await safe_edit(query, await home_text(user_id), home_keyboard())
    await safe_answer(query)


# ---------- add flow ----------

async def pick_group_keyboard(user_id: int) -> InlineKeyboardMarkup:
    targets = await get_user_targets(user_id)
    rows = []
    for t in targets[:20]:
        title = (t.get("title") or str(t["chat_id"]))[:26]
        rows.append(
            [
                InlineKeyboardButton(
                    f"🎯 {title}",
                    callback_data=f"dm:pickg:{t['chat_id']}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("« Cancel", callback_data="dm:home")])
    return InlineKeyboardMarkup(rows)


async def pick_account_keyboard(user_id: int, prefix: str) -> InlineKeyboardMarkup:
    from handlers.ui import active_accounts_only
    accounts = active_accounts_only(await get_user_accounts(user_id))
    rows = []
    for a in accounts:
        from handlers.ui import format_account_label
        name = format_account_label(a, short=True)[:24]
        st = a.get("status", "active")
        icon = {"active": "🟢", "sleeping": "😴", "disabled": "🔴", "error": "⚠️"}.get(st, "⚪")
        rows.append(
            [
                InlineKeyboardButton(
                    f"{icon} {name}",
                    callback_data=f"{prefix}{a['account_id']}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("« Back", callback_data="dm:home")])
    return InlineKeyboardMarkup(rows)


async def _resolve_account_client(user_id: int, account_id: str):
    from core.job_worker import get_user_client

    acc = await get_account(user_id, account_id)
    if not acc:
        return None, None, "Account not found."
    client = await get_user_client(acc)
    if not client:
        return acc, None, "Could not start this account (invalid session?)."
    return acc, client, None


@Client.on_callback_query(filters.regex(r"^dm:"))
async def delete_callbacks(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    from core.access import can_access_bot
    if not await can_access_bot(user_id):
        return await safe_answer(query, "Not allowed", True)
    await ensure_user(user_id)
    data = query.data
    parts = data.split(":")

    if data == "dm:home":
        await show_delete_home(client, query)
        return

    if data.startswith("dm:list:"):
        page = int(parts[2]) if len(parts) > 2 else 0
        await safe_edit(
            query,
            "🗑️ **Managed Groups**\n\nSelect a configuration:",
            await list_keyboard(user_id, page),
        )
        return await safe_answer(query)

    if data == "dm:add":
        from handlers.ui import active_accounts_only
        accounts = active_accounts_only(await get_user_accounts(user_id))
        if not accounts:
            return await safe_answer(
                query, "Add a forwarding User Account first (Accounts → Add).", True
            )
        set_state(client, "delete_state", user_id, {"step": "await_group"})
        await safe_edit(
            query,
            "🗑️ **Add Group**\n\n"
            "Pick a target from the list, **or** send a group username / chat ID / "
            "forward a message from the group.\n\n"
            "/cancel to abort.",
            await pick_group_keyboard(user_id),
        )
        return await safe_answer(query)

    if data.startswith("dm:pickg:"):
        chat_id = int(parts[2])
        title = str(chat_id)
        for t in await get_user_targets(user_id):
            if int(t["chat_id"]) == chat_id:
                title = t.get("title") or title
                break
        set_state(
            client,
            "delete_state",
            user_id,
            {"step": "pick_account", "chat_id": chat_id, "title": title},
        )
        await safe_edit(
            query,
            f"👥 **Group:** {title}\n\nSelect the forwarding **User Account** that is admin there:",
            await pick_account_keyboard(user_id, "dm:newacc:"),
        )
        return await safe_answer(query)

    if data.startswith("dm:newacc:"):
        account_id = parts[2]
        st = get_state(client, "delete_state", user_id) or {}
        chat_id = st.get("chat_id")
        title = st.get("title") or str(chat_id)
        if not chat_id:
            return await safe_answer(query, "Session expired. Start Add Group again.", True)
        await safe_answer(query, "Checking permissions...")
        acc, ub, err = await _resolve_account_client(user_id, account_id)
        if err or not ub:
            await safe_edit(
                query,
                permission_fail_text(
                    (acc or {}).get("name") or "account",
                    title,
                    err or "Account unavailable.",
                ),
                fail_keyboard(),
            )
            return
        ok, reason = await check_delete_permissions(ub, chat_id, (acc or {}).get("name"), m_user_id=user_id)
        if not ok:
            await safe_edit(
                query,
                permission_fail_text((acc or {}).get("name") or "account", title, reason),
                fail_keyboard(),
            )
            return
        from core.access import check_limit
        from database import get_user_delete_configs
        _err = await check_limit(user_id, "delete_manager", len(await get_user_delete_configs(user_id)))
        if _err:
            return await query.answer(_err, show_alert=True)

        cfg = await create_delete_config(user_id, int(chat_id), title, account_id)
        set_state(client, "delete_state", user_id, None)
        await safe_edit(query, await detail_text(user_id, cfg), detail_keyboard(cfg))
        return

    if data.startswith("dm:open:"):
        cid = parts[2]
        cfg = await get_delete_config(user_id, cid)
        if not cfg:
            return await safe_answer(query, "Not found", True)
        await safe_edit(query, await detail_text(user_id, cfg), detail_keyboard(cfg))
        return await safe_answer(query)

    if data.startswith("dm:auto:"):
        cid = parts[2]
        cfg = await get_delete_config(user_id, cid)
        if not cfg:
            return await safe_answer(query, "Not found", True)
        new_val = not bool(cfg.get("auto_delete"))
        updates = {"auto_delete": new_val, "last_error": None}
        if new_val:
            acc, ub, err = await _resolve_account_client(user_id, cfg["account_id"])
            if err or not ub:
                await safe_edit(
                    query,
                    permission_fail_text(
                        await _acc_name(user_id, cfg["account_id"]),
                        cfg.get("target_title"),
                        err or "Account unavailable.",
                    ),
                    fail_keyboard(cid),
                )
                return await safe_answer(query)
            ok, reason = await check_delete_permissions(ub, cfg["target_chat_id"], m_user_id=user_id)
            if not ok:
                await safe_edit(
                    query,
                    permission_fail_text(
                        await _acc_name(user_id, cfg["account_id"]),
                        cfg.get("target_title"),
                        reason,
                    ),
                    fail_keyboard(cid),
                )
                return await safe_answer(query)
            updates["next_run_at"] = _next_run(int(cfg.get("check_interval_seconds") or 86400))
        await update_delete_config(user_id, cid, updates)
        cfg = await get_delete_config(user_id, cid)
        await safe_edit(query, await detail_text(user_id, cfg), detail_keyboard(cfg))
        return await safe_answer(query, f"Auto Delete → {'ON' if new_val else 'OFF'}")

    if data.startswith("dm:now:"):
        cid = parts[2]
        cfg = await get_delete_config(user_id, cid)
        if not cfg:
            return await safe_answer(query, "Not found", True)
        if is_delete_running(cid):
            return await safe_answer(query, "Already running", True)
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Confirm Delete", callback_data=f"dm:go:{cid}"),
                    InlineKeyboardButton("❌ Cancel", callback_data=f"dm:open:{cid}"),
                ]
            ]
        )
        await safe_edit(
            query,
            "⚠️ **Confirm Deletion**\n\n"
            f"**Group:** {cfg.get('target_title')}\n"
            f"**Account:** {await _acc_name(user_id, cfg.get('account_id'))}\n\n"
            f"Messages older than **{fmt_interval(int(cfg.get('message_age_seconds') or 86400))}** "
            f"matching your type filters will be deleted.\n"
            f"Protected users/IDs are skipped.",
            kb,
        )
        return await safe_answer(query)

    if data.startswith("dm:go:"):
        cid = parts[2]
        cfg = await get_delete_config(user_id, cid)
        if not cfg:
            return await safe_answer(query, "Not found", True)
        if is_delete_running(cid):
            return await safe_answer(query, "Already running", True)
        await safe_answer(query, "Checking permissions...")
        acc, ub, err = await _resolve_account_client(user_id, cfg["account_id"])
        if err or not ub:
            await safe_edit(
                query,
                permission_fail_text(
                    await _acc_name(user_id, cfg["account_id"]),
                    cfg.get("target_title"),
                    err or "Account unavailable.",
                ),
                fail_keyboard(cid),
            )
            return
        ok, reason = await check_delete_permissions(ub, cfg["target_chat_id"], m_user_id=user_id)
        if not ok:
            await safe_edit(
                query,
                permission_fail_text(
                    await _acc_name(user_id, cfg["account_id"]),
                    cfg.get("target_title"),
                    reason,
                ),
                fail_keyboard(cid),
            )
            return

        await safe_edit(
            query,
            progress_text(cfg, {"processed": 0, "deleted": 0, "skipped": 0, "protected": 0, "failed": 0, "started_at": __import__("time").time()}),
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("⛔ Cancel", callback_data=f"dm:cancel:{cid}")]]
            ),
        )

        async def _job():
            stats = await run_delete_job(cfg, progress_message=query.message, auto=False)
            try:
                status = stats.get("status")
                if status == "cancelled":
                    title = "⛔ **Deletion Cancelled**"
                elif status in ("failed", "paused"):
                    title = "❌ **Deletion Stopped**"
                else:
                    title = "✅ **Deletion Completed**"
                extra = f"\n\n{stats.get('error')}" if stats.get("error") else ""
                await query.message.edit_text(
                    f"{title}\n\n"
                    f"🗑 Deleted: **{stats.get('deleted', 0):,}**\n"
                    f"⏭ Skipped: **{stats.get('skipped', 0):,}**\n"
                    f"🛡 Protected: **{stats.get('protected', 0):,}**\n"
                    f"❌ Failed: **{stats.get('failed', 0):,}**\n"
                    f"⏱ Runtime: **{fmt_duration(stats.get('runtime'))}**{extra}",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("« Back", callback_data=f"dm:open:{cid}")]]
                    ),
                )
            except Exception:
                pass

        RUNNING[cid] = asyncio.create_task(_job())
        return

    if data.startswith("dm:cancel:"):
        cid = parts[2]
        cancel_delete_job(cid)
        return await safe_answer(query, "Cancellation requested")

    if data.startswith("dm:types:"):
        cid = parts[2]
        cfg = await get_delete_config(user_id, cid)
        if not cfg:
            return await safe_answer(query, "Not found", True)
        selected = set(cfg.get("message_types") or [])
        rows = []
        row = []
        for key in ALL_TYPES:
            mark = "✅" if key in selected else "☐"
            row.append(
                InlineKeyboardButton(
                    f"{mark} {TYPE_LABELS[key]}",
                    callback_data=f"dm:type:{cid}:{key}",
                )
            )
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton("« Back", callback_data=f"dm:open:{cid}")])
        await safe_edit(
            query,
            "📦 **Message Types**\n\nOnly checked types can be deleted.",
            InlineKeyboardMarkup(rows),
        )
        return await safe_answer(query)

    if data.startswith("dm:type:"):
        cid, key = parts[2], parts[3]
        cfg = await get_delete_config(user_id, cid)
        if not cfg:
            return await safe_answer(query, "Not found", True)
        current = list(cfg.get("message_types") or [])
        if key in current:
            current.remove(key)
        else:
            current.append(key)
        await update_delete_config(user_id, cid, {"message_types": current})
        query.data = f"dm:types:{cid}"
        return await delete_callbacks(client, query)

    if data.startswith("dm:mon:"):
        cid = parts[2]
        cfg = await get_delete_config(user_id, cid)
        if not cfg:
            return await safe_answer(query, "Not found", True)
        check_rows = []
        cur_c = int(cfg.get("check_interval_seconds") or 86400)
        row = []
        for secs, label in CHECK_PRESETS:
            mark = "✅ " if secs == cur_c else ""
            row.append(
                InlineKeyboardButton(
                    f"{mark}{label}", callback_data=f"dm:chk:{cid}:{secs}"
                )
            )
            if len(row) == 2:
                check_rows.append(row)
                row = []
        if row:
            check_rows.append(row)
        check_rows.append(
            [InlineKeyboardButton("⚙️ Custom check", callback_data=f"dm:cchk:{cid}")]
        )

        age_rows = []
        cur_a = int(cfg.get("message_age_seconds") or 86400)
        row = []
        for secs, label in AGE_PRESETS:
            mark = "✅ " if secs == cur_a else ""
            row.append(
                InlineKeyboardButton(
                    f"{mark}{label}", callback_data=f"dm:age:{cid}:{secs}"
                )
            )
            if len(row) == 2:
                age_rows.append(row)
                row = []
        if row:
            age_rows.append(row)
        age_rows.append(
            [InlineKeyboardButton("⚙️ Custom age", callback_data=f"dm:cage:{cid}")]
        )
        age_rows.append([InlineKeyboardButton("« Back", callback_data=f"dm:open:{cid}")])

        await safe_edit(
            query,
            "⏱️ **Monitoring**\n\n"
            f"🔍 **Check every:** {fmt_interval(cur_c)}\n"
            f"🗑️ **Delete messages older than:** {fmt_interval(cur_a)}\n\n"
            "These are independent settings.",
            InlineKeyboardMarkup(check_rows + age_rows),
        )
        return await safe_answer(query)

    if data.startswith("dm:chk:"):
        cid, secs = parts[2], int(parts[3])
        secs = max(MIN_CHECK, min(MAX_SPAN, secs))
        cfg = await get_delete_config(user_id, cid)
        if not cfg:
            return await safe_answer(query, "Not found", True)
        updates = {"check_interval_seconds": secs}
        if cfg.get("auto_delete"):
            updates["next_run_at"] = _next_run(secs)
        await update_delete_config(user_id, cid, updates)
        query.data = f"dm:mon:{cid}"
        return await delete_callbacks(client, query)

    if data.startswith("dm:age:"):
        cid, secs = parts[2], int(parts[3])
        secs = max(MIN_AGE, min(MAX_SPAN, secs))
        await update_delete_config(user_id, cid, {"message_age_seconds": secs})
        query.data = f"dm:mon:{cid}"
        return await delete_callbacks(client, query)

    if data.startswith("dm:cchk:") or data.startswith("dm:cage:"):
        cid = parts[2]
        kind = "check" if data.startswith("dm:cchk:") else "age"
        set_state(
            client,
            "delete_state",
            user_id,
            {"step": "custom_duration", "kind": kind, "config_id": cid},
        )
        lo = "1 hour" if kind == "check" else "24 hours"
        await safe_edit(
            query,
            f"Send duration ({lo} – 2 years).\n"
            "Examples: `6h`  `2d`  `30d`  `1y`\n\n/cancel to abort.",
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("« Back", callback_data=f"dm:mon:{cid}")]]
            ),
        )
        return await safe_answer(query)

    if data.startswith("dm:pusers:"):
        cid = parts[2]
        cfg = await get_delete_config(user_id, cid)
        if not cfg:
            return await safe_answer(query, "Not found", True)
        ids = cfg.get("protected_user_ids") or []
        listing = "\n".join(f"`{i}`" for i in ids[:40]) or "None"
        await safe_edit(
            query,
            f"🚫 **Protected Users**\n\nMessages from these user IDs are **never** deleted.\n\n{listing}",
            InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("➕ Add User", callback_data=f"dm:au:{cid}")],
                    [InlineKeyboardButton("➖ Remove User", callback_data=f"dm:ru:{cid}")],
                    [InlineKeyboardButton("🗑 Clear All", callback_data=f"dm:cu:{cid}")],
                    [InlineKeyboardButton("« Back", callback_data=f"dm:open:{cid}")],
                ]
            ),
        )
        return await safe_answer(query)

    if data.startswith("dm:pids:"):
        cid = parts[2]
        cfg = await get_delete_config(user_id, cid)
        if not cfg:
            return await safe_answer(query, "Not found", True)
        ids = cfg.get("protected_message_ids") or []
        listing = "\n".join(f"`{i}`" for i in ids[:40]) or "None"
        await safe_edit(
            query,
            f"🚫 **Protected Message IDs**\n\nThese message IDs are **never** deleted.\n\n{listing}",
            InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("➕ Add IDs", callback_data=f"dm:ai:{cid}")],
                    [InlineKeyboardButton("➖ Remove ID", callback_data=f"dm:ri:{cid}")],
                    [InlineKeyboardButton("🗑 Clear All", callback_data=f"dm:ci:{cid}")],
                    [InlineKeyboardButton("« Back", callback_data=f"dm:open:{cid}")],
                ]
            ),
        )
        return await safe_answer(query)

    if data.startswith("dm:au:") or data.startswith("dm:ru:") or data.startswith("dm:ai:") or data.startswith("dm:ri:"):
        cid = parts[2]
        action = parts[1]
        step_map = {
            "au": "add_user",
            "ru": "rm_user",
            "ai": "add_ids",
            "ri": "rm_id",
        }
        set_state(
            client,
            "delete_state",
            user_id,
            {"step": step_map[action], "config_id": cid},
        )
        hint = {
            "au": "Send numeric **user ID**(s), comma or space separated.",
            "ru": "Send the user ID to remove.",
            "ai": "Send numeric **message ID**(s), comma or space separated.",
            "ri": "Send the message ID to remove.",
        }[action]
        back = f"dm:pusers:{cid}" if action in ("au", "ru") else f"dm:pids:{cid}"
        await safe_edit(
            query,
            hint + "\n\n/cancel to abort.",
            InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data=back)]]),
        )
        return await safe_answer(query)

    if data.startswith("dm:cu:") or data.startswith("dm:ci:"):
        cid = parts[2]
        field = "protected_user_ids" if data.startswith("dm:cu:") else "protected_message_ids"
        n = len((await get_delete_config(user_id, cid) or {}).get(field) or [])
        await safe_edit(
            query,
            f"⚠️ Clear all **{n}** protected item(s)? This cannot be undone.",
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Confirm",
                            callback_data=f"{'dm:yesu:' if field == 'protected_user_ids' else 'dm:yesi:'}{cid}",
                        ),
                        InlineKeyboardButton("❌ Cancel", callback_data=f"dm:open:{cid}"),
                    ]
                ]
            ),
        )
        return await safe_answer(query)

    if data.startswith("dm:xuser:") or data.startswith("dm:xids:"):
        # not used — keep simple below
        pass

    if data.startswith("dm:yesu:"):
        cid = parts[2]
        await update_delete_config(user_id, cid, {"protected_user_ids": []})
        query.data = f"dm:pusers:{cid}"
        return await delete_callbacks(client, query)

    if data.startswith("dm:yesi:"):
        cid = parts[2]
        await update_delete_config(user_id, cid, {"protected_message_ids": []})
        query.data = f"dm:pids:{cid}"
        return await delete_callbacks(client, query)

    # Fix clear-all confirm callbacks (cu/ci already showed a generic confirm).
    if data.startswith("dm:xuser_ids:") or False:
        pass

    if data.startswith("dm:stats:"):
        cid = parts[2]
        cfg = await get_delete_config(user_id, cid)
        if not cfg:
            return await safe_answer(query, "Not found", True)
        s = cfg.get("stats") or {}
        nxt = cfg.get("next_run_at") if cfg.get("auto_delete") else None
        await safe_edit(
            query,
            "📊 **Delete Statistics**\n\n"
            f"**Group:** {cfg.get('target_title')}\n\n"
            f"Total Processed: **{int(s.get('processed') or 0):,}**\n"
            f"Deleted: **{int(s.get('deleted') or 0):,}**\n"
            f"Skipped: **{int(s.get('skipped') or 0):,}**\n"
            f"Protected: **{int(s.get('protected') or 0):,}**\n"
            f"Failed: **{int(s.get('failed') or 0):,}**\n\n"
            f"Last Run: {fmt_dt(cfg.get('last_run_at'))}\n"
            f"Next Run: {fmt_dt(nxt)}",
            InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔄 Refresh", callback_data=f"dm:stats:{cid}")],
                    [InlineKeyboardButton("« Back", callback_data=f"dm:open:{cid}")],
                ]
            ),
        )
        return await safe_answer(query)

    if data.startswith("dm:reacc:"):
        cid = parts[2]
        await safe_edit(
            query,
            "Select another forwarding **User Account**:",
            await pick_account_keyboard(user_id, f"dm:seta:{cid}:"),
        )
        return await safe_answer(query)

    if data.startswith("dm:seta:"):
        cid, account_id = parts[2], parts[3]
        cfg = await get_delete_config(user_id, cid)
        if not cfg:
            return await safe_answer(query, "Not found", True)
        await safe_answer(query, "Checking permissions...")
        acc, ub, err = await _resolve_account_client(user_id, account_id)
        if err or not ub:
            await safe_edit(
                query,
                permission_fail_text(
                    (acc or {}).get("name") or "account",
                    cfg.get("target_title"),
                    err or "Account unavailable.",
                ),
                fail_keyboard(cid),
            )
            return
        ok, reason = await check_delete_permissions(ub, cfg["target_chat_id"], m_user_id=user_id)
        if not ok:
            await safe_edit(
                query,
                permission_fail_text(
                    (acc or {}).get("name") or "account",
                    cfg.get("target_title"),
                    reason,
                ),
                fail_keyboard(cid),
            )
            return
        await update_delete_config(user_id, cid, {"account_id": account_id, "last_error": None})
        cfg = await get_delete_config(user_id, cid)
        await safe_edit(query, await detail_text(user_id, cfg), detail_keyboard(cfg))
        return

    if data.startswith("dm:rm:"):
        cid = parts[2]
        cfg = await get_delete_config(user_id, cid)
        if not cfg:
            return await safe_answer(query, "Not found", True)
        await safe_edit(
            query,
            f"⚠️ **Remove configuration?**\n\n"
            f"**{cfg.get('target_title')}**\n\n"
            "This does not delete Telegram messages. It only removes this Delete Manager config.",
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ Remove", callback_data=f"dm:rmgo:{cid}"),
                        InlineKeyboardButton("❌ Cancel", callback_data=f"dm:open:{cid}"),
                    ]
                ]
            ),
        )
        return await safe_answer(query)

    if data.startswith("dm:rmgo:"):
        cid = parts[2]
        if is_delete_running(cid):
            cancel_delete_job(cid)
        await delete_delete_config(user_id, cid)
        await safe_edit(
            query,
            "🗑️ **Managed Groups**\n\nConfiguration removed.",
            await list_keyboard(user_id, 0),
        )
        return await safe_answer(query, "Removed")


def _parse_int_list(text: str) -> list[int]:
    out = []
    for part in text.replace(",", " ").split():
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            continue
    return out


def parse_duration_seconds(text: str) -> int | None:
    raw = text.strip().lower().replace(" ", "")
    if not raw:
        return None
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}
    if raw.isdigit():
        return int(raw)
    num = ""
    unit = ""
    for ch in raw:
        if ch.isdigit():
            num += ch
        else:
            unit += ch
    if not num or unit not in mult:
        return None
    return int(num) * mult[unit]


async def handle_delete_text(client: Client, message: Message) -> bool:
    """Return True if this message was consumed by Delete Manager."""
    user_id = message.from_user.id
    from core.access import can_access_bot
    if not await can_access_bot(user_id):
        return False
    st = get_state(client, "delete_state", user_id)
    if not isinstance(st, dict) or not st.get("step"):
        return False

    text = (message.text or "").strip()
    if text.lower() in ("/cancel", "cancel"):
        set_state(client, "delete_state", user_id, None)
        await message.reply("✅ Cancelled.")
        return True

    step = st.get("step")

    if step == "await_group":
        chat = None
        try:
            if text.startswith("@"):
                chat = await client.get_chat(text)
            else:
                chat = await client.get_chat(int(text))
        except Exception:
            await message.reply(
                "Could not resolve that chat. Send `@username` or numeric chat ID, "
                "or forward a message from the group."
            )
            return True
        if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
            await message.reply("Only groups / supergroups / channels are supported.")
            return True
        set_state(
            client,
            "delete_state",
            user_id,
            {"step": "pick_account", "chat_id": chat.id, "title": chat.title or str(chat.id)},
        )
        await message.reply(
            f"👥 **Group:** {chat.title}\n\nSelect the forwarding User Account:",
            reply_markup=await pick_account_keyboard(user_id, "dm:newacc:"),
        )
        return True

    if step == "custom_duration":
        cid = st.get("config_id")
        kind = st.get("kind")
        secs = parse_duration_seconds(text)
        if secs is None:
            await message.reply("Invalid duration. Examples: `6h`, `2d`, `30d`, `1y`")
            return True
        if kind == "check":
            if secs < MIN_CHECK or secs > MAX_SPAN:
                await message.reply("Check interval must be between **1 hour** and **2 years**.")
                return True
            updates = {"check_interval_seconds": secs}
            cfg = await get_delete_config(user_id, cid)
            if cfg and cfg.get("auto_delete"):
                updates["next_run_at"] = _next_run(secs)
            await update_delete_config(user_id, cid, updates)
        else:
            if secs < MIN_AGE or secs > MAX_SPAN:
                await message.reply("Message age must be between **24 hours** and **2 years**.")
                return True
            await update_delete_config(user_id, cid, {"message_age_seconds": secs})
        set_state(client, "delete_state", user_id, None)
        cfg = await get_delete_config(user_id, cid)
        await message.reply(
            f"✅ Updated.\n\n{await detail_text(user_id, cfg)}" if cfg else "✅ Updated.",
            reply_markup=detail_keyboard(cfg) if cfg else home_keyboard(),
        )
        return True

    cid = st.get("config_id")
    cfg = await get_delete_config(user_id, cid) if cid else None
    if not cfg:
        set_state(client, "delete_state", user_id, None)
        await message.reply("Configuration not found.")
        return True

    ids = _parse_int_list(text)
    if not ids:
        await message.reply("Send numeric ID(s). Example: `123456789`")
        return True

    if step == "add_user":
        cur = list(cfg.get("protected_user_ids") or [])
        for i in ids:
            if i not in cur:
                cur.append(i)
        await update_delete_config(user_id, cid, {"protected_user_ids": cur})
        await message.reply(f"✅ Protected users: **{len(cur)}**")
    elif step == "rm_user":
        cur = [i for i in (cfg.get("protected_user_ids") or []) if i not in ids]
        await update_delete_config(user_id, cid, {"protected_user_ids": cur})
        await message.reply(f"✅ Protected users: **{len(cur)}**")
    elif step == "add_ids":
        cur = list(cfg.get("protected_message_ids") or [])
        for i in ids:
            if i not in cur:
                cur.append(i)
        await update_delete_config(user_id, cid, {"protected_message_ids": cur})
        await message.reply(f"✅ Protected message IDs: **{len(cur)}**")
    elif step == "rm_id":
        cur = [i for i in (cfg.get("protected_message_ids") or []) if i not in ids]
        await update_delete_config(user_id, cid, {"protected_message_ids": cur})
        await message.reply(f"✅ Protected message IDs: **{len(cur)}**")
    else:
        return False

    set_state(client, "delete_state", user_id, None)
    cfg = await get_delete_config(user_id, cid)
    await message.reply(await detail_text(user_id, cfg), reply_markup=detail_keyboard(cfg))
    return True


async def continue_delete_from_source(client, message, user_id, source_chat_id, _last_msg_id):
    """Forward/link while Add Group is waiting."""
    try:
        chat = await client.get_chat(source_chat_id)
    except Exception:
        await message.reply("Cannot access that chat with the management bot.")
        return
    set_state(
        client,
        "delete_state",
        user_id,
        {"step": "pick_account", "chat_id": chat.id, "title": chat.title or str(chat.id)},
    )
    await message.reply(
        f"👥 **Group:** {chat.title}\n\nSelect the forwarding User Account:",
        reply_markup=await pick_account_keyboard(user_id, "dm:newacc:"),
    )
