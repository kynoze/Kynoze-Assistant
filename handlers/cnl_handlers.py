"""CNL Auto-Post — full dashboard management UI (isolated)."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    InlineQuery, InlineQueryResultArticle, InputTextMessageContent, Message,
)
from pyrogram.enums import ParseMode

from database import is_admin
from core.state import get_state, set_state
from handlers.ui import safe_answer, safe_edit
from core.cnl.constants import NOT_CONFIGURED, RULE_LIMIT, ALLOWED_MEDIA_TYPES
from core.cnl.gate import (
    is_cnl_configured, get_gate, get_gate_uri_plain, set_gate_uri,
    remove_gate, mask_uri,
)
from core.cnl.db import get_cnl, close_cnl, test_cnl_uri
from core.cnl.helpers import format_rule, resolve_chat_id, parse_buttons
from core.cnl.bots import get_user_bot_manager
from core.cnl.clients import get_user_client_manager

logger = logging.getLogger(__name__)
CNL_STATE = "cnl_state"

MEDIA_TYPES = [
    "all", "photo", "video", "document", "audio", "voice",
    "animation", "sticker", "text", "poll", "contact", "location", "venue",
]


# ── keyboards ──────────────────────────────────────────────────────────────

def _kb_home(configured: bool) -> InlineKeyboardMarkup:
    rows = []
    if configured:
        rows += [
            [InlineKeyboardButton("📋 Rules", callback_data="cnl:rules"),
             InlineKeyboardButton("➕ Add Rule", callback_data="cnl:addrule")],
            [InlineKeyboardButton("🤖 Bot", callback_data="cnl:bot"),
             InlineKeyboardButton("👤 Account", callback_data="cnl:account")],
            [InlineKeyboardButton("📋 Global Copy", callback_data="cnl:gcopy"),
             InlineKeyboardButton("🔒 Dupe DB", callback_data="cnl:dupe")],
            [InlineKeyboardButton("📊 Stats / Quota", callback_data="cnl:stats")],
            [InlineKeyboardButton("🗄 Database", callback_data="cnl:db")],
        ]
    else:
        rows.append([InlineKeyboardButton("🗄 Configure Database", callback_data="cnl:db")])
    rows.append([InlineKeyboardButton("« Dashboard", callback_data="dash:home")])
    return InlineKeyboardMarkup(rows)


def _kb_back(to: str = "cnl:home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data=to)]])


def _rule_back(sid: int, tid: int) -> str:
    return f"cnl:rule:{sid}:{tid}"


def _fmt_words(words) -> str:
    if not words:
        return "_(none)_"
    if isinstance(words, str):
        words = [words]
    return ", ".join(f"`{w}`" for w in words[:30]) + (f" …(+{len(words)-30})" if len(words) > 30 else "")


def _fmt_reps(reps) -> str:
    if not reps:
        return "_(none)_"
    lines = []
    for i, r in enumerate(reps[:15], 1):
        if isinstance(r, dict):
            old, new = r.get("from") or r.get("old") or "", r.get("to") if "to" in r else r.get("new") or ""
        elif isinstance(r, (list, tuple)) and len(r) >= 2:
            old, new = r[0], r[1]
        else:
            continue
        lines.append(f"{i}. `{old}` → `{new}`")
    if len(reps) > 15:
        lines.append(f"…(+{len(reps)-15})")
    return "\n".join(lines) or "_(none)_"


def _fmt_buttons(btns) -> str:
    if not btns:
        return "_(none)_"
    lines = []
    for ri, row in enumerate(btns):
        for b in row:
            lines.append(f"• {b.get('text','?')} → `{b.get('url','')[:40]}`")
    return "\n".join(lines) or "_(none)_"


def _rule_summary(rule: Dict[str, Any]) -> str:
    sid, tid = rule["source_chat_id"], rule["target_chat_id"]
    en = rule.get("enabled", True)
    types = rule.get("allowed_types") or ["all"]
    lines = [
        f"**⚙️ Rule Settings**",
        f"`{sid}` → `{tid}`",
        f"Status: {'✅ Enabled' if en else '⏸ Disabled'}",
        f"Via: `{rule.get('forward_via') or 'user_bot'}`",
        f"Types: `{', '.join(types)}`",
        f"Delay: `{rule.get('delay') or 0}s`",
        f"Anti-dupe: {'ON' if rule.get('anti_dupe') else 'OFF'}",
        f"Forward tag: {'ON' if rule.get('forward_tag') else 'OFF'}",
        f"Remove links: {'ON' if rule.get('remove_links') else 'OFF'}",
        f"Remove old caption: {'ON' if rule.get('remove_old_caption') else 'OFF'}",
    ]
    add = rule.get("add_caption")
    custom = rule.get("custom_caption")
    pos = rule.get("caption_position") or "end"
    if custom:
        lines.append(f"Caption template: `{str(custom)[:60]}`")
    elif add:
        lines.append(f"Add caption ({pos}): `{str(add)[:60]}`")
    else:
        lines.append("Caption: _(none)_")
    lines.append(f"Block: {_fmt_words(rule.get('block_words'))}")
    lines.append(f"Whitelist: {_fmt_words(rule.get('whitelist_words'))}")
    lines.append(f"Replacements:\n{_fmt_reps(rule.get('replacements'))}")
    lines.append(f"URL buttons:\n{_fmt_buttons(rule.get('buttons'))}")
    return "\n".join(lines)


def _rule_settings_kb(rule: Dict[str, Any]) -> InlineKeyboardMarkup:
    sid, tid = rule["source_chat_id"], rule["target_chat_id"]
    en = rule.get("enabled", True)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏸ Disable" if en else "▶️ Enable",
                              callback_data=f"cnl:rtog:{sid}:{tid}")],
        [InlineKeyboardButton("🚀 Forward Via", callback_data=f"cnl:rvia:{sid}:{tid}"),
         InlineKeyboardButton("📦 Forward Types", callback_data=f"cnl:rtype:{sid}:{tid}")],
        [InlineKeyboardButton("✏️ Caption", callback_data=f"cnl:rcap:{sid}:{tid}"),
         InlineKeyboardButton("🔗 Remove Links", callback_data=f"cnl:rrl:{sid}:{tid}")],
        [InlineKeyboardButton("🚫 Block Words", callback_data=f"cnl:rblk:{sid}:{tid}"),
         InlineKeyboardButton("✅ Whitelist", callback_data=f"cnl:rwht:{sid}:{tid}")],
        [InlineKeyboardButton("🔄 Replacements", callback_data=f"cnl:rrep:{sid}:{tid}"),
         InlineKeyboardButton("🔘 URL Buttons", callback_data=f"cnl:rbtn:{sid}:{tid}")],
        [InlineKeyboardButton("⏱ Delay", callback_data=f"cnl:rdelay:{sid}:{tid}"),
         InlineKeyboardButton("♻️ Anti-Dupe", callback_data=f"cnl:radm:{sid}:{tid}")],
        [InlineKeyboardButton("🏷 Forward Tag", callback_data=f"cnl:rft:{sid}:{tid}")],
        [InlineKeyboardButton("🔄 Reset Settings", callback_data=f"cnl:rreset:{sid}:{tid}"),
         InlineKeyboardButton("🗑 Delete Rule", callback_data=f"cnl:rdelc:{sid}:{tid}")],
        [InlineKeyboardButton("« Rules", callback_data="cnl:rules")],
    ])


# ── status / home ───────────────────────────────────────────────────────────

async def _status_text(user_id: int) -> str:
    configured = await is_cnl_configured(user_id)
    if not configured:
        return (
            "**📡 CNL Auto-Post**\n\n"
            "Isolated live auto-forward system.\n"
            "Uses **your own MongoDB** — separate from Jobs/Targets.\n\n"
            + NOT_CONFIGURED
        )
    gate = await get_gate(user_id)
    cnl = await get_cnl(user_id)
    lines = ["**📡 CNL Auto-Post**\n"]
    if gate:
        lines.append(f"🗄 DB: `{gate.get('db_name') or 'cnl_autopost'}`")
        uri = await get_gate_uri_plain(user_id)
        lines.append(f"URI: `{mask_uri(uri)}`")
    if cnl:
        rules = await cnl.get_rules_by_owner(user_id)
        en = sum(1 for r in rules if r.get("enabled", True))
        lines.append(f"📋 Rules: **{len(rules)}** ({en} enabled)")
        bot_ok = await cnl.has_active_bot(user_id)
        acc_ok = await cnl.has_active_session(user_id)
        bot_run = get_user_bot_manager().is_running(user_id)
        acc_run = get_user_client_manager().is_running(user_id)
        lines.append(f"🤖 Bot: {'🟢 online' if bot_run else ('⚪ saved' if bot_ok else '❌ none')}")
        lines.append(f"👤 Account: {'🟢 online' if acc_run else ('⚪ saved' if acc_ok else '❌ none')}")
        q = await cnl.get_user_quota_info(user_id)
        if q.get("is_admin"):
            lines.append(f"📊 Quota: unlimited — used {q['used']}")
        else:
            lines.append(f"📊 Quota: {q['used']}/{q['limit']} today")
        gc = await cnl.get_global_copy(user_id)
        if gc and gc.get("enabled"):
            lines.append(f"📋 Global Copy: ON → `{gc.get('target_chat_id')}`")
    else:
        lines.append("⚠️ Could not connect to CNL DB")
    return "\n".join(lines)


async def show_cnl_home(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    text = await _status_text(user_id)
    configured = await is_cnl_configured(user_id)
    await safe_edit(query, text, _kb_home(configured))
    await safe_answer(query)


async def _show_rule(client, query, user_id, sid, tid):
    cnl = await get_cnl(user_id)
    if not cnl:
        await safe_edit(query, NOT_CONFIGURED, _kb_home(False))
        return
    rule = await cnl.get_forward_rule(sid, tid)
    if not rule:
        await query.answer("Rule not found", show_alert=True)
        return
    await safe_edit(query, _rule_summary(rule), _rule_settings_kb(rule))


# ── callbacks ───────────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^cnl:"))
async def cnl_callbacks(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    from core.access import can_access_bot
    if not await can_access_bot(user_id):
        return await query.answer("Not allowed", show_alert=True)
    data = query.data

    # ── via (add rule step 3) ──
    if data.startswith("cnl:via:"):
        state = get_state(client, CNL_STATE, user_id)
        if not state or state.get("step") != "rule_via":
            return await query.answer("Session expired — start Add Rule again", show_alert=True)
        via = "user_bot" if data.endswith(":bot") else "user_account"
        sid, tid = state.get("source_id"), state.get("target_id")
        if not sid or not tid:
            return await query.answer("Missing source/target", show_alert=True)
        cnl = await get_cnl(user_id)
        if not cnl:
            return await query.answer("CNL not configured", show_alert=True)
        from core.cnl.helpers import check_user_bot_permissions, check_user_account_permissions
        err = await (check_user_bot_permissions if via == "user_bot" else check_user_account_permissions)(
            user_id, sid, tid
        )
        if err:
            return await query.answer(err, show_alert=True)
        from core.access import check_limit
        rules = await cnl.get_user_rules(user_id) if hasattr(cnl, "get_user_rules") else []
        try:
            if not rules:
                rules = await cnl.forward_rules.find({"owner_id": int(user_id)}).to_list(500)
        except Exception:
            rules = rules or []
        _err = await check_limit(user_id, "cnl_rules", len(rules))
        if _err:
            return await query.answer(_err, show_alert=True)
        await cnl.create_forward_rule(sid, tid, user_id, forward_via=via, enabled=True)
        set_state(client, CNL_STATE, user_id, None)
        await safe_edit(
            query,
            f"✅ Rule created: `{sid}` → `{tid}` via `{via}`",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("⚙️ Open Rule", callback_data=f"cnl:rule:{sid}:{tid}")],
                [InlineKeyboardButton("📋 Rules", callback_data="cnl:rules")],
                [InlineKeyboardButton("« Home", callback_data="cnl:home")],
            ]),
        )
        return await safe_answer(query)

    if data in ("cnl:home", "cnl:refresh"):
        return await show_cnl_home(client, query)

    # ── DB ──
    if data == "cnl:db":
        configured = await is_cnl_configured(user_id)
        gate = await get_gate(user_id)
        if configured and gate:
            uri = await get_gate_uri_plain(user_id)
            text = (
                f"**🗄 CNL Database**\n\nStatus: ✅ configured\n"
                f"DB: `{gate.get('db_name')}`\nURI: `{mask_uri(uri)}`"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Change URI", callback_data="cnl:db:set")],
                [InlineKeyboardButton("🗑 Remove", callback_data="cnl:db:rm")],
                [InlineKeyboardButton("« Back", callback_data="cnl:home")],
            ])
        else:
            text = NOT_CONFIGURED + "\n\nSend your MongoDB connection URI."
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Set URI", callback_data="cnl:db:set")],
                [InlineKeyboardButton("« Back", callback_data="cnl:home")],
            ])
        await safe_edit(query, text, kb)
        return await safe_answer(query)

    if data == "cnl:db:set":
        set_state(client, CNL_STATE, user_id, {"step": "db_uri"})
        await safe_edit(query,
            "**Set CNL MongoDB URI**\n\n"
            "Send full connection string, e.g.\n"
            "`mongodb+srv://user:pass@cluster.mongodb.net/cnl_autopost`\n\n"
            "/cancel to abort.",
            _kb_back("cnl:db"))
        return await safe_answer(query)

    if data == "cnl:db:rm":
        await close_cnl(user_id)
        await remove_gate(user_id)
        await safe_edit(query, "✅ CNL database removed.", _kb_home(False))
        return await safe_answer(query)

    # ── Rules list ──
    if data == "cnl:rules" or data.startswith("cnl:rules:"):
        page = 0
        if data.startswith("cnl:rules:"):
            try:
                page = int(data.split(":")[2])
            except Exception:
                page = 0
        cnl = await get_cnl(user_id)
        if not cnl:
            await safe_edit(query, NOT_CONFIGURED, _kb_home(False))
            return await safe_answer(query)
        rules = await cnl.get_rules_by_owner(user_id)
        page_size = 8
        total_pages = max(1, (len(rules) + page_size - 1) // page_size)
        page = max(0, min(page, total_pages - 1))
        slice_ = rules[page * page_size:(page + 1) * page_size]
        if not rules:
            text = "**📋 Rules**\n\nNo rules yet."
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Add Rule", callback_data="cnl:addrule")],
                [InlineKeyboardButton("« Back", callback_data="cnl:home")],
            ])
        else:
            lines = [f"**📋 Rules** ({len(rules)}) — page {page+1}/{total_pages}\n"]
            buttons = []
            for i, r in enumerate(slice_, page * page_size + 1):
                lines.append(format_rule(r, i))
                sid, tid = r["source_chat_id"], r["target_chat_id"]
                buttons.append([InlineKeyboardButton(
                    f"{'✅' if r.get('enabled', True) else '⏸'} {sid} → {tid}",
                    callback_data=f"cnl:rule:{sid}:{tid}",
                )])
            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton("‹ Prev", callback_data=f"cnl:rules:{page-1}"))
            if page < total_pages - 1:
                nav.append(InlineKeyboardButton("Next ›", callback_data=f"cnl:rules:{page+1}"))
            if nav:
                buttons.append(nav)
            buttons.append([InlineKeyboardButton("➕ Add Rule", callback_data="cnl:addrule")])
            if rules:
                buttons.append([InlineKeyboardButton("🗑 Delete All Rules", callback_data="cnl:rules:delall")])
            buttons.append([InlineKeyboardButton("« Back", callback_data="cnl:home")])
            text = "\n".join(lines)
            kb = InlineKeyboardMarkup(buttons)
        await safe_edit(query, text, kb)
        return await safe_answer(query)

    if data == "cnl:rules:delall":
        await safe_edit(query,
            "⚠️ **Delete ALL rules?**\nThis cannot be undone.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, delete all", callback_data="cnl:rules:delall:yes")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cnl:rules")],
            ]))
        return await safe_answer(query)

    if data == "cnl:rules:delall:yes":
        cnl = await get_cnl(user_id)
        if cnl:
            await cnl.delete_all_rules_of_user(user_id)
        await query.answer("All rules deleted")
        query.data = "cnl:rules"
        return await cnl_callbacks(client, query)

    if data == "cnl:addrule":
        cnl = await get_cnl(user_id)
        if not cnl:
            await safe_edit(query, NOT_CONFIGURED, _kb_home(False))
            return await safe_answer(query)
        rules = await cnl.get_rules_by_owner(user_id)
        if len(rules) >= RULE_LIMIT:
            return await query.answer(f"Rule limit ({RULE_LIMIT}) reached", show_alert=True)
        set_state(client, CNL_STATE, user_id, {"step": "rule_source"})
        await safe_edit(query,
            "**➕ Add Rule** — Step 1/3\n\n"
            "Send **source** chat ID, @username, or invite link.\n\n/cancel to abort.",
            _kb_back("cnl:rules"))
        return await safe_answer(query)

    # ── open rule ──
    if data.startswith("cnl:rule:"):
        parts = data.split(":")
        if len(parts) < 4:
            return await safe_answer(query)
        sid, tid = int(parts[2]), int(parts[3])
        await _show_rule(client, query, user_id, sid, tid)
        return await safe_answer(query)

    # ── toggles ──
    if data.startswith("cnl:rtog:"):
        _, _, sid, tid = data.split(":")
        sid, tid = int(sid), int(tid)
        cnl = await get_cnl(user_id)
        rule = await cnl.get_forward_rule(sid, tid) if cnl else None
        if rule:
            await cnl.set_rule_enabled(sid, tid, not rule.get("enabled", True))
        await _show_rule(client, query, user_id, sid, tid)
        return await safe_answer(query)

    if data.startswith("cnl:rrl:"):
        _, _, sid, tid = data.split(":")
        sid, tid = int(sid), int(tid)
        cnl = await get_cnl(user_id)
        rule = await cnl.get_forward_rule(sid, tid) if cnl else None
        if rule:
            await cnl.set_remove_links(sid, tid, not rule.get("remove_links", False))
        await _show_rule(client, query, user_id, sid, tid)
        return await safe_answer(query)

    if data.startswith("cnl:rft:"):
        _, _, sid, tid = data.split(":")
        sid, tid = int(sid), int(tid)
        cnl = await get_cnl(user_id)
        rule = await cnl.get_forward_rule(sid, tid) if cnl else None
        if rule:
            await cnl.set_forward_tag(sid, tid, not rule.get("forward_tag", False))
        await _show_rule(client, query, user_id, sid, tid)
        return await safe_answer(query)

    # ── forward via ──
    if data.startswith("cnl:rvia:"):
        parts = data.split(":")
        sid, tid = int(parts[2]), int(parts[3])
        if len(parts) == 4:
            cnl = await get_cnl(user_id)
            rule = await cnl.get_forward_rule(sid, tid) if cnl else None
            cur = (rule or {}).get("forward_via") or "user_bot"
            text = f"**🚀 Forward Via**\n\nCurrent: `{cur}`\n\nChoose method:"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    ("✅ " if cur == "user_bot" else "") + "🤖 User Bot",
                    callback_data=f"cnl:rvia:{sid}:{tid}:bot")],
                [InlineKeyboardButton(
                    ("✅ " if cur == "user_account" else "") + "👤 User Account",
                    callback_data=f"cnl:rvia:{sid}:{tid}:acc")],
                [InlineKeyboardButton("« Back", callback_data=_rule_back(sid, tid))],
            ])
            await safe_edit(query, text, kb)
            return await safe_answer(query)
        via = "user_bot" if parts[4] == "bot" else "user_account"
        cnl = await get_cnl(user_id)
        if cnl:
            await cnl.update_forward_rule(sid, tid, {"forward_via": via})
        await query.answer(f"Via set to {via}")
        await _show_rule(client, query, user_id, sid, tid)
        return await safe_answer(query)

    # ── media types ──
    if data.startswith("cnl:rtype:"):
        parts = data.split(":")
        sid, tid = int(parts[2]), int(parts[3])
        cnl = await get_cnl(user_id)
        rule = await cnl.get_forward_rule(sid, tid) if cnl else None
        if not rule:
            return await query.answer("Rule not found", show_alert=True)
        selected = list(rule.get("allowed_types") or ["all"])

        if len(parts) >= 5:
            action = parts[4]
            if action in ("all", "selectall"):
                selected = ["all"]
            elif action == "clear":
                selected = ["photo"]
            elif action == "save":
                await cnl.set_allowed_types(sid, tid, selected)
                await query.answer("Types saved")
                await _show_rule(client, query, user_id, sid, tid)
                return await safe_answer(query)
            else:
                tname = action
                if tname == "all":
                    selected = ["all"]
                else:
                    selected = [x for x in selected if x != "all"]
                    if tname in selected:
                        selected.remove(tname)
                    else:
                        selected.append(tname)
                    if not selected:
                        selected = ["all"]
            await cnl.set_allowed_types(sid, tid, selected)
            rule = await cnl.get_forward_rule(sid, tid)
            selected = list(rule.get("allowed_types") or ["all"])

        rows = []
        row = []
        all_on = "all" in selected
        for mt in MEDIA_TYPES:
            if mt == "all":
                mark = "☑" if all_on else "☐"
            else:
                mark = "☑" if (not all_on and mt in selected) else ("☑" if all_on else "☐")
                if all_on:
                    mark = "☑"  # all means every type
            row.append(InlineKeyboardButton(f"{mark} {mt}", callback_data=f"cnl:rtype:{sid}:{tid}:{mt}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([
            InlineKeyboardButton("✅ Select All", callback_data=f"cnl:rtype:{sid}:{tid}:all"),
            InlineKeyboardButton("🗑 Clear→photo", callback_data=f"cnl:rtype:{sid}:{tid}:clear"),
        ])
        rows.append([InlineKeyboardButton("« Back", callback_data=_rule_back(sid, tid))])
        text = (
            f"**📦 Forward Types**\n\n"
            f"Selected: `{', '.join(selected)}`\n\n"
            "Tap to toggle. `all` means every type."
        )
        await safe_edit(query, text, InlineKeyboardMarkup(rows))
        return await safe_answer(query)

    # ── caption menu ──
    if data.startswith("cnl:rcap:"):
        parts = data.split(":")
        sid, tid = int(parts[2]), int(parts[3])
        action = parts[4] if len(parts) > 4 else "menu"
        cnl = await get_cnl(user_id)
        rule = await cnl.get_forward_rule(sid, tid) if cnl else None
        if not rule:
            return await query.answer("Rule not found", show_alert=True)

        if action == "rmadd":
            await cnl.set_add_caption(sid, tid, None)
            await query.answer("Add caption cleared")
        elif action == "rmcustom":
            await cnl.set_custom_caption(sid, tid, None)
            await query.answer("Template cleared")
        elif action == "rmold":
            await cnl.set_remove_old_caption(sid, tid, not rule.get("remove_old_caption", False))
            await query.answer("Toggled")
        elif action == "reset":
            await cnl.update_forward_rule(sid, tid, {
                "add_caption": None, "custom_caption": None,
                "caption_position": "end", "remove_old_caption": False,
            })
            await query.answer("Caption reset")
        elif action == "add":
            set_state(client, CNL_STATE, user_id, {"step": "rule_caption", "sid": sid, "tid": tid})
            await safe_edit(query,
                "**➕ Add Caption**\n\n"
                "Send text to append/prepend.\n"
                "Prefix with `start:`, `end:`, or `gap:` for position.\n"
                "Send `-` to clear.\n/cancel to abort.",
                _kb_back(f"cnl:rcap:{sid}:{tid}"))
            return await safe_answer(query)
        elif action == "tpl":
            set_state(client, CNL_STATE, user_id, {"step": "rule_caption_tpl", "sid": sid, "tid": tid})
            await safe_edit(query,
                "**✏️ Custom Caption Template**\n\n"
                "Send template. Use `{caption}` where original should appear.\n\n"
                "Example:\n`📦 {caption}\\n\\nJoin @channel`\n\n"
                "Send `-` to clear.\n/cancel to abort.",
                _kb_back(f"cnl:rcap:{sid}:{tid}"))
            return await safe_answer(query)
        elif action == "preview":
            add = rule.get("add_caption") or ""
            custom = rule.get("custom_caption") or ""
            pos = rule.get("caption_position") or "end"
            sample = "Original sample caption"
            if custom:
                result = custom.replace("{caption}", "" if rule.get("remove_old_caption") else sample)
            elif add:
                orig = "" if rule.get("remove_old_caption") else sample
                if pos == "start":
                    result = f"{add}\n{orig}".strip()
                elif pos == "end_with_gap":
                    result = f"{orig}\n\n{add}".strip()
                else:
                    result = f"{orig}\n{add}".strip()
            else:
                result = "" if rule.get("remove_old_caption") else sample
            await query.answer()
            await safe_edit(query,
                f"**👁 Caption Preview**\n\n```\n{result or '(empty)'}\n```",
                InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data=f"cnl:rcap:{sid}:{tid}")]]))
            return

        # refresh menu
        rule = await cnl.get_forward_rule(sid, tid)
        text = (
            f"**✏️ Caption**\n\n"
            f"Add caption: `{rule.get('add_caption') or '—'}`\n"
            f"Position: `{rule.get('caption_position') or 'end'}`\n"
            f"Template: `{str(rule.get('custom_caption') or '—')[:80]}`\n"
            f"Remove original: {'ON' if rule.get('remove_old_caption') else 'OFF'}\n\n"
            "Template (if set) overrides add-caption."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add Caption", callback_data=f"cnl:rcap:{sid}:{tid}:add")],
            [InlineKeyboardButton("✏️ Custom Template", callback_data=f"cnl:rcap:{sid}:{tid}:tpl")],
            [InlineKeyboardButton(
                ("✅ " if rule.get("remove_old_caption") else "") + "Remove original caption",
                callback_data=f"cnl:rcap:{sid}:{tid}:rmold")],
            [InlineKeyboardButton("👁 Preview", callback_data=f"cnl:rcap:{sid}:{tid}:preview")],
            [InlineKeyboardButton("🗑 Clear add", callback_data=f"cnl:rcap:{sid}:{tid}:rmadd"),
             InlineKeyboardButton("🗑 Clear template", callback_data=f"cnl:rcap:{sid}:{tid}:rmcustom")],
            [InlineKeyboardButton("🔄 Reset Caption", callback_data=f"cnl:rcap:{sid}:{tid}:reset")],
            [InlineKeyboardButton("« Back", callback_data=_rule_back(sid, tid))],
        ])
        await safe_edit(query, text, kb)
        return await safe_answer(query)

    # ── block words ──
    if data.startswith("cnl:rblk:"):
        parts = data.split(":")
        sid, tid = int(parts[2]), int(parts[3])
        action = parts[4] if len(parts) > 4 else "menu"
        cnl = await get_cnl(user_id)
        rule = await cnl.get_forward_rule(sid, tid) if cnl else None
        if not rule:
            return await query.answer("Rule not found", show_alert=True)
        words = list(rule.get("block_words") or [])

        if action == "add":
            set_state(client, CNL_STATE, user_id, {"step": "rule_block", "sid": sid, "tid": tid})
            await safe_edit(query,
                "Send block words (comma or newline separated).\n"
                "They are **added** to the existing list.\n"
                "Send `-` to clear all.\n/cancel to abort.",
                _kb_back(f"cnl:rblk:{sid}:{tid}"))
            return await safe_answer(query)
        if action == "clear":
            await safe_edit(query, "⚠️ Remove **all** block words?",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Yes", callback_data=f"cnl:rblk:{sid}:{tid}:clearyes")],
                    [InlineKeyboardButton("❌ No", callback_data=f"cnl:rblk:{sid}:{tid}")],
                ]))
            return await safe_answer(query)
        if action == "clearyes":
            await cnl.set_block_words(sid, tid, [])
            words = []
            await query.answer("Cleared")
        if action == "rm" and len(parts) > 5:
            idx = int(parts[5])
            if 0 <= idx < len(words):
                words.pop(idx)
                await cnl.set_block_words(sid, tid, words)
                await query.answer("Removed")

        rule = await cnl.get_forward_rule(sid, tid)
        words = list(rule.get("block_words") or [])
        text = f"**🚫 Block Words** ({len(words)})\n\n{_fmt_words(words)}\n\nMessages containing these words are skipped."
        buttons = []
        for i, w in enumerate(words[:20]):
            buttons.append([InlineKeyboardButton(f"🗑 {w}", callback_data=f"cnl:rblk:{sid}:{tid}:rm:{i}")])
        buttons.append([InlineKeyboardButton("➕ Add", callback_data=f"cnl:rblk:{sid}:{tid}:add")])
        if words:
            buttons.append([InlineKeyboardButton("🗑 Remove All", callback_data=f"cnl:rblk:{sid}:{tid}:clear")])
        buttons.append([InlineKeyboardButton("« Back", callback_data=_rule_back(sid, tid))])
        await safe_edit(query, text, InlineKeyboardMarkup(buttons))
        return await safe_answer(query)

    # ── whitelist ──
    if data.startswith("cnl:rwht:"):
        parts = data.split(":")
        sid, tid = int(parts[2]), int(parts[3])
        action = parts[4] if len(parts) > 4 else "menu"
        cnl = await get_cnl(user_id)
        rule = await cnl.get_forward_rule(sid, tid) if cnl else None
        if not rule:
            return await query.answer("Rule not found", show_alert=True)
        words = list(rule.get("whitelist_words") or [])

        if action == "add":
            set_state(client, CNL_STATE, user_id, {"step": "rule_white", "sid": sid, "tid": tid})
            await safe_edit(query,
                "Send whitelist words (comma/newline).\n"
                "When set, **only** messages containing at least one word are forwarded.\n"
                "Send `-` to clear all.\n/cancel to abort.",
                _kb_back(f"cnl:rwht:{sid}:{tid}"))
            return await safe_answer(query)
        if action == "clear":
            await safe_edit(query, "⚠️ Remove **all** whitelist words?",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Yes", callback_data=f"cnl:rwht:{sid}:{tid}:clearyes")],
                    [InlineKeyboardButton("❌ No", callback_data=f"cnl:rwht:{sid}:{tid}")],
                ]))
            return await safe_answer(query)
        if action == "clearyes":
            await cnl.set_whitelist_words(sid, tid, [])
            await query.answer("Cleared")
        if action == "rm" and len(parts) > 5:
            idx = int(parts[5])
            words = list((await cnl.get_forward_rule(sid, tid)).get("whitelist_words") or [])
            if 0 <= idx < len(words):
                words.pop(idx)
                await cnl.set_whitelist_words(sid, tid, words)
                await query.answer("Removed")

        rule = await cnl.get_forward_rule(sid, tid)
        words = list(rule.get("whitelist_words") or [])
        text = f"**✅ Whitelist** ({len(words)})\n\n{_fmt_words(words)}\n\nEmpty = no whitelist filter."
        buttons = []
        for i, w in enumerate(words[:20]):
            buttons.append([InlineKeyboardButton(f"🗑 {w}", callback_data=f"cnl:rwht:{sid}:{tid}:rm:{i}")])
        buttons.append([InlineKeyboardButton("➕ Add", callback_data=f"cnl:rwht:{sid}:{tid}:add")])
        if words:
            buttons.append([InlineKeyboardButton("🗑 Remove All", callback_data=f"cnl:rwht:{sid}:{tid}:clear")])
        buttons.append([InlineKeyboardButton("« Back", callback_data=_rule_back(sid, tid))])
        await safe_edit(query, text, InlineKeyboardMarkup(buttons))
        return await safe_answer(query)

    # ── replacements ──
    if data.startswith("cnl:rrep:"):
        parts = data.split(":")
        sid, tid = int(parts[2]), int(parts[3])
        action = parts[4] if len(parts) > 4 else "menu"
        cnl = await get_cnl(user_id)
        rule = await cnl.get_forward_rule(sid, tid) if cnl else None
        if not rule:
            return await query.answer("Rule not found", show_alert=True)
        reps = list(rule.get("replacements") or [])

        if action == "add":
            set_state(client, CNL_STATE, user_id, {"step": "rule_repl", "sid": sid, "tid": tid})
            await safe_edit(query,
                "Send replacements as `old => new` (one per line).\n"
                "They are **added** to existing list.\n"
                "Send `-` to clear all.\n/cancel to abort.",
                _kb_back(f"cnl:rrep:{sid}:{tid}"))
            return await safe_answer(query)
        if action == "clear":
            await safe_edit(query, "⚠️ Remove **all** replacements?",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Yes", callback_data=f"cnl:rrep:{sid}:{tid}:clearyes")],
                    [InlineKeyboardButton("❌ No", callback_data=f"cnl:rrep:{sid}:{tid}")],
                ]))
            return await safe_answer(query)
        if action == "clearyes":
            await cnl.set_replacements(sid, tid, [])
            await query.answer("Cleared")
        if action == "rm" and len(parts) > 5:
            idx = int(parts[5])
            reps = list((await cnl.get_forward_rule(sid, tid)).get("replacements") or [])
            if 0 <= idx < len(reps):
                reps.pop(idx)
                await cnl.set_replacements(sid, tid, reps)
                await query.answer("Removed")

        rule = await cnl.get_forward_rule(sid, tid)
        reps = list(rule.get("replacements") or [])
        text = f"**🔄 Replacements** ({len(reps)})\n\n{_fmt_reps(reps)}"
        buttons = []
        for i, r in enumerate(reps[:15]):
            if isinstance(r, dict):
                label = f"{r.get('from') or r.get('old') or '?'} → {r.get('to') if 'to' in r else r.get('new') or ''}"
            else:
                label = str(r)[:40]
            buttons.append([InlineKeyboardButton(f"🗑 {label[:40]}", callback_data=f"cnl:rrep:{sid}:{tid}:rm:{i}")])
        buttons.append([InlineKeyboardButton("➕ Add", callback_data=f"cnl:rrep:{sid}:{tid}:add")])
        if reps:
            buttons.append([InlineKeyboardButton("🗑 Remove All", callback_data=f"cnl:rrep:{sid}:{tid}:clear")])
        buttons.append([InlineKeyboardButton("« Back", callback_data=_rule_back(sid, tid))])
        await safe_edit(query, text, InlineKeyboardMarkup(buttons))
        return await safe_answer(query)

    # ── URL buttons ──
    if data.startswith("cnl:rbtn:"):
        parts = data.split(":")
        sid, tid = int(parts[2]), int(parts[3])
        action = parts[4] if len(parts) > 4 else "menu"
        cnl = await get_cnl(user_id)
        rule = await cnl.get_forward_rule(sid, tid) if cnl else None
        if not rule:
            return await query.answer("Rule not found", show_alert=True)

        if action == "add":
            set_state(client, CNL_STATE, user_id, {"step": "rule_buttons", "sid": sid, "tid": tid})
            await safe_edit(query,
                "Send buttons:\n`Label - https://url`\n"
                "One per line; use `|` for same row.\n"
                "This **replaces** existing buttons.\n"
                "Send `-` to clear.\n/cancel to abort.",
                _kb_back(f"cnl:rbtn:{sid}:{tid}"))
            return await safe_answer(query)
        if action == "clear":
            await safe_edit(query, "⚠️ Remove **all** URL buttons?",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Yes", callback_data=f"cnl:rbtn:{sid}:{tid}:clearyes")],
                    [InlineKeyboardButton("❌ No", callback_data=f"cnl:rbtn:{sid}:{tid}")],
                ]))
            return await safe_answer(query)
        if action == "clearyes":
            await cnl.set_buttons(sid, tid, None)
            await query.answer("Cleared")

        rule = await cnl.get_forward_rule(sid, tid)
        text = f"**🔘 URL Buttons**\n\n{_fmt_buttons(rule.get('buttons'))}"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Set / Replace", callback_data=f"cnl:rbtn:{sid}:{tid}:add")],
            [InlineKeyboardButton("🗑 Remove All", callback_data=f"cnl:rbtn:{sid}:{tid}:clear")],
            [InlineKeyboardButton("« Back", callback_data=_rule_back(sid, tid))],
        ])
        await safe_edit(query, text, kb)
        return await safe_answer(query)

    # ── delay ──
    if data.startswith("cnl:rdelay:"):
        parts = data.split(":")
        sid, tid = int(parts[2]), int(parts[3])
        action = parts[4] if len(parts) > 4 else "menu"
        cnl = await get_cnl(user_id)
        rule = await cnl.get_forward_rule(sid, tid) if cnl else None
        if not rule:
            return await query.answer("Rule not found", show_alert=True)

        if action == "edit":
            set_state(client, CNL_STATE, user_id, {"step": "rule_delay", "sid": sid, "tid": tid})
            await safe_edit(query, "Send delay in **seconds** (0–300).\n/cancel to abort.",
                            _kb_back(f"cnl:rdelay:{sid}:{tid}"))
            return await safe_answer(query)
        if action == "off":
            await cnl.set_delay(sid, tid, 0)
            await query.answer("Delay disabled")
        if action == "reset":
            await cnl.set_delay(sid, tid, 0)
            await query.answer("Reset to 0")

        rule = await cnl.get_forward_rule(sid, tid)
        d = rule.get("delay") or 0
        text = f"**⏱ Delay**\n\nCurrent: **{d}** seconds"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Edit", callback_data=f"cnl:rdelay:{sid}:{tid}:edit")],
            [InlineKeyboardButton("🚫 Disable (0)", callback_data=f"cnl:rdelay:{sid}:{tid}:off")],
            [InlineKeyboardButton("🔄 Reset", callback_data=f"cnl:rdelay:{sid}:{tid}:reset")],
            [InlineKeyboardButton("« Back", callback_data=_rule_back(sid, tid))],
        ])
        await safe_edit(query, text, kb)
        return await safe_answer(query)

    # ── anti-dupe menu ──
    if data.startswith("cnl:radm:"):
        parts = data.split(":")
        sid, tid = int(parts[2]), int(parts[3])
        action = parts[4] if len(parts) > 4 else "menu"
        cnl = await get_cnl(user_id)
        rule = await cnl.get_forward_rule(sid, tid) if cnl else None
        if not rule:
            return await query.answer("Rule not found", show_alert=True)

        if action == "on":
            await cnl.set_anti_dupe(sid, tid, True)
            await query.answer("Anti-dupe ON")
        elif action == "off":
            await cnl.set_anti_dupe(sid, tid, False)
            await query.answer("Anti-dupe OFF")
        elif action == "clear":
            await safe_edit(query,
                "⚠️ Clear duplicate hashes for this **target** only?\n"
                "(Does not remove external Dupe DB config.)",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Yes, clear", callback_data=f"cnl:radm:{sid}:{tid}:clearyes")],
                    [InlineKeyboardButton("❌ Cancel", callback_data=f"cnl:radm:{sid}:{tid}")],
                ]))
            return await safe_answer(query)
        elif action == "clearyes":
            await cnl.clear_dupe_for_owner(user_id, tid)
            await query.answer("Hashes cleared for target")

        rule = await cnl.get_forward_rule(sid, tid)
        on = rule.get("anti_dupe", False)
        text = f"**♻️ Anti-Duplicate**\n\nStatus: {'✅ ON' if on else '❌ OFF'}\nTarget: `{tid}`"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Enable" if not on else "❌ Disable",
                                  callback_data=f"cnl:radm:{sid}:{tid}:{'off' if on else 'on'}")],
            [InlineKeyboardButton("🗑 Clear target hashes", callback_data=f"cnl:radm:{sid}:{tid}:clear")],
            [InlineKeyboardButton("« Back", callback_data=_rule_back(sid, tid))],
        ])
        await safe_edit(query, text, kb)
        return await safe_answer(query)

    # ── reset rule settings ──
    if data.startswith("cnl:rreset:"):
        parts = data.split(":")
        sid, tid = int(parts[2]), int(parts[3])
        if len(parts) == 4:
            await safe_edit(query,
                "⚠️ Reset all filters/caption/delay for this rule?\n(Source/target/via kept)",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Yes, reset", callback_data=f"cnl:rreset:{sid}:{tid}:yes")],
                    [InlineKeyboardButton("❌ Cancel", callback_data=_rule_back(sid, tid))],
                ]))
            return await safe_answer(query)
        cnl = await get_cnl(user_id)
        if cnl:
            await cnl.update_forward_rule(sid, tid, {
                "add_caption": None, "custom_caption": None, "caption_position": "end",
                "remove_old_caption": False, "replacements": [], "block_words": [],
                "whitelist_words": [], "buttons": None, "forward_tag": False,
                "remove_links": False, "allowed_types": ["all"], "delay": 0, "anti_dupe": False,
            })
        await query.answer("Settings reset")
        await _show_rule(client, query, user_id, sid, tid)
        return await safe_answer(query)

    # ── delete rule confirm ──
    if data.startswith("cnl:rdelc:"):
        parts = data.split(":")
        sid, tid = int(parts[2]), int(parts[3])
        await safe_edit(query,
            f"⚠️ Delete rule `{sid}` → `{tid}`?",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, delete", callback_data=f"cnl:rdel:{sid}:{tid}")],
                [InlineKeyboardButton("❌ Cancel", callback_data=_rule_back(sid, tid))],
            ]))
        return await safe_answer(query)

    if data.startswith("cnl:rdel:"):
        _, _, sid, tid = data.split(":")
        sid, tid = int(sid), int(tid)
        cnl = await get_cnl(user_id)
        if cnl:
            await cnl.delete_forward_rule(sid, tid)
        await query.answer("Deleted")
        query.data = "cnl:rules"
        return await cnl_callbacks(client, query)

    # ── Bot (My Bots multi-select) ──
    if data == "cnl:bot":
        cnl = await get_cnl(user_id)
        if not cnl:
            await safe_edit(query, NOT_CONFIGURED, _kb_home(False))
            return await safe_answer(query)
        from database import get_user_bots
        bots = await get_user_bots(user_id)
        mgr = get_user_bot_manager()
        running = mgr.running_count(user_id) if hasattr(mgr, "running_count") else (1 if mgr.is_running(user_id) else 0)
        doc = await cnl.user_bots.find_one({"user_id": int(user_id)}) or {}
        selected = [str(x) for x in (doc.get("selected_bot_ids") or [])]
        mid = doc.get("main_bot_id")
        if mid and str(mid) not in selected:
            selected.append(str(mid))
        text = (
            f"**🤖 CNL — My Bots (multi)**\n\n"
            f"Selected: **{len(selected)}** / {len(bots)}\n"
            f"Running: **{running}**\n\n"
            f"Toggle bots below, then Start."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 Select / Toggle Bots", callback_data="cnl:bot:pick")],
            [InlineKeyboardButton("▶️ Start selected", callback_data="cnl:bot:startall")],
            [InlineKeyboardButton("⏹ Stop all", callback_data="cnl:bot:stopall")],
            [InlineKeyboardButton("« Back", callback_data="cnl:home")],
        ])
        await safe_edit(query, text, kb)
        return await safe_answer(query)

    if data == "cnl:bot:pick":
        from database import get_user_bots
        cnl = await get_cnl(user_id)
        if not cnl:
            return await query.answer("CNL DB not configured", show_alert=True)
        bots = await get_user_bots(user_id)
        if not bots:
            await query.answer("Add a bot under My Bots first", show_alert=True)
            return
        doc = await cnl.user_bots.find_one({"user_id": int(user_id)}) or {}
        selected = set(str(x) for x in (doc.get("selected_bot_ids") or []))
        mid = doc.get("main_bot_id")
        if mid:
            selected.add(str(mid))
        rows = []
        for b in bots[:20]:
            name = b.get("bot_username") or b.get("name") or b.get("bot_id") or "?"
            bid = str(b.get("bot_id") or "")
            mark = "✅" if bid in selected else "⬜"
            rows.append([InlineKeyboardButton(
                f"{mark} 🤖 {name}",
                callback_data=f"cnl:bot:toggleid:{bid}",
            )])
        rows.append([InlineKeyboardButton("« Back", callback_data="cnl:bot")])
        await safe_edit(query, "**Toggle My Bots for CNL** (multiple allowed)", InlineKeyboardMarkup(rows))
        return await safe_answer(query)

    if data.startswith("cnl:bot:toggleid:"):
        bot_id = data.split(":")[-1]
        cnl = await get_cnl(user_id)
        if not cnl:
            return await query.answer("CNL DB not configured", show_alert=True)
        doc = await cnl.user_bots.find_one({"user_id": int(user_id)}) or {}
        selected = [str(x) for x in (doc.get("selected_bot_ids") or [])]
        mid = doc.get("main_bot_id")
        if mid and str(mid) not in selected:
            selected.append(str(mid))
        if bot_id in selected:
            selected = [x for x in selected if x != bot_id]
        else:
            selected.append(bot_id)
        await cnl.user_bots.update_one(
            {"user_id": int(user_id)},
            {"$set": {
                "user_id": int(user_id),
                "selected_bot_ids": selected,
                "main_bot_id": selected[0] if selected else None,
            }},
            upsert=True,
        )
        await query.answer("Updated")
        query.data = "cnl:bot:pick"
        return await cnl_callbacks(client, query)

    if data == "cnl:bot:startall":
        from database import get_bot
        from handlers.ui import load_secret
        cnl = await get_cnl(user_id)
        if not cnl:
            return await query.answer("CNL DB not configured", show_alert=True)
        doc = await cnl.user_bots.find_one({"user_id": int(user_id)}) or {}
        selected = [str(x) for x in (doc.get("selected_bot_ids") or [])]
        if not selected and doc.get("main_bot_id"):
            selected = [str(doc["main_bot_id"])]
        if not selected:
            return await query.answer("Select at least one My Bot", show_alert=True)
        ok_n, fail = 0, []
        mgr = get_user_bot_manager()
        for bid in selected:
            b = await get_bot(user_id, bid)
            if not b:
                fail.append(bid)
                continue
            try:
                token = load_secret(b.get("bot_token") or "")
            except Exception:
                token = None
            if not token:
                fail.append(bid)
                continue
            ok, msg = await mgr.start_user_bot(user_id, bot_token=token, bot_id=bid)
            if ok:
                ok_n += 1
            else:
                fail.append(f"{bid}:{msg}")
        await query.answer(f"Started {ok_n}" + (f", fail {len(fail)}" if fail else ""), show_alert=True)
        query.data = "cnl:bot"
        return await cnl_callbacks(client, query)

    if data == "cnl:bot:stopall":
        await get_user_bot_manager().stop_user_bot(user_id)
        await query.answer("Stopped all")
        query.data = "cnl:bot"
        return await cnl_callbacks(client, query)

    # legacy single paths kept for old callbacks
    if data.startswith("cnl:bot:use:"):
        bot_id = data.split(":")[-1]
        query.data = f"cnl:bot:toggleid:{bot_id}"
        return await cnl_callbacks(client, query)

    if data == "cnl:bot:toggle":
        query.data = "cnl:bot:startall"
        return await cnl_callbacks(client, query)

    # ── Account (My Accounts multi-select) ──
    if data == "cnl:account":
        cnl = await get_cnl(user_id)
        if not cnl:
            await safe_edit(query, NOT_CONFIGURED, _kb_home(False))
            return await safe_answer(query)
        from database import get_user_accounts
        accs = await get_user_accounts(user_id)
        running = get_user_client_manager().is_running(user_id)
        doc = await cnl.user_sessions.find_one({"user_id": int(user_id)}) or {}
        selected = [str(x) for x in (doc.get("selected_account_ids") or [])]
        text = (
            f"**👤 CNL — My Accounts (multi)**\n\n"
            f"Selected: **{len(selected)}** / {len(accs)}\n"
            f"Runtime: {'🟢 online' if running else '⚪ offline'}\n\n"
            f"Toggle accounts below. Runtime starts the primary selected session."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 Select / Toggle Accounts", callback_data="cnl:acc:pick")],
            [InlineKeyboardButton("▶️ Start primary", callback_data="cnl:acc:toggle")],
            [InlineKeyboardButton("« Back", callback_data="cnl:home")],
        ])
        await safe_edit(query, text, kb)
        return await safe_answer(query)

    if data == "cnl:acc:pick":
        from database import get_user_accounts
        cnl = await get_cnl(user_id)
        if not cnl:
            return await query.answer("CNL DB not configured", show_alert=True)
        accs = await get_user_accounts(user_id)
        if not accs:
            await query.answer("Add an account under My Accounts first", show_alert=True)
            return
        doc = await cnl.user_sessions.find_one({"user_id": int(user_id)}) or {}
        selected = set(str(x) for x in (doc.get("selected_account_ids") or []))
        rows = []
        for a in accs[:20]:
            name = a.get("name") or a.get("phone") or a.get("account_id") or "?"
            aid = str(a.get("account_id") or "")
            mark = "✅" if aid in selected else "⬜"
            rows.append([InlineKeyboardButton(
                f"{mark} 👤 {name}",
                callback_data=f"cnl:acc:toggleid:{aid}",
            )])
        rows.append([InlineKeyboardButton("« Back", callback_data="cnl:account")])
        await safe_edit(query, "**Toggle My Accounts for CNL** (multiple allowed)", InlineKeyboardMarkup(rows))
        return await safe_answer(query)

    if data.startswith("cnl:acc:toggleid:"):
        aid = data.split(":")[-1]
        cnl = await get_cnl(user_id)
        if not cnl:
            return await query.answer("CNL DB not configured", show_alert=True)
        doc = await cnl.user_sessions.find_one({"user_id": int(user_id)}) or {}
        selected = [str(x) for x in (doc.get("selected_account_ids") or [])]
        if aid in selected:
            selected = [x for x in selected if x != aid]
        else:
            selected.append(aid)
        await cnl.user_sessions.update_one(
            {"user_id": int(user_id)},
            {"$set": {
                "user_id": int(user_id),
                "selected_account_ids": selected,
                "main_account_id": selected[0] if selected else None,
            }},
            upsert=True,
        )
        await query.answer("Updated")
        query.data = "cnl:acc:pick"
        return await cnl_callbacks(client, query)

    if data.startswith("cnl:acc:use:"):
        aid = data.split(":")[-1]
        query.data = f"cnl:acc:toggleid:{aid}"
        return await cnl_callbacks(client, query)

    if data == "cnl:acc:toggle":
        mgr = get_user_client_manager()
        if mgr.is_running(user_id):
            await mgr.stop_user_client(user_id)
            await query.answer("Stopped")
        else:
            from database import get_account
            from handlers.ui import load_secret
            cnl = await get_cnl(user_id)
            doc = await cnl.user_sessions.find_one({"user_id": int(user_id)}) if cnl else {}
            selected = [str(x) for x in ((doc or {}).get("selected_account_ids") or [])]
            mid = (doc or {}).get("main_account_id")
            if mid and str(mid) not in selected:
                selected.insert(0, str(mid))
            ss = None
            for aid in selected:
                a = await get_account(user_id, aid)
                if not a:
                    continue
                try:
                    ss = load_secret(a.get("session_string") or "")
                except Exception:
                    ss = None
                if ss:
                    break
            if not ss and cnl:
                ss = await cnl.get_decrypted_session_string(user_id)
            if not ss:
                return await query.answer("Select a My Account first", show_alert=True)
            if cnl and selected:
                a = await get_account(user_id, selected[0])
                if a:
                    try:
                        await cnl.save_user_session(
                            user_id, ss,
                            phone_number=a.get("phone") or "",
                            tg_user_id=a.get("tg_user_id") or 0,
                            tg_username=a.get("username"),
                            tg_first_name=a.get("name"),
                        )
                    except Exception:
                        pass
            ok, msg = await mgr.start_user_client(user_id, session_string=ss)
            await query.answer(msg if ok else f"Failed: {msg}", show_alert=not ok)
        query.data = "cnl:account"
        return await cnl_callbacks(client, query)

    # ── Global Copy (full settings) ──
    if data == "cnl:gcopy" or data.startswith("cnl:gcopy:"):
        cnl = await get_cnl(user_id)
        if not cnl:
            await safe_edit(query, NOT_CONFIGURED, _kb_home(False))
            return await safe_answer(query)
        gc = await cnl.get_global_copy(user_id) or {}
        parts = data.split(":")
        action = parts[2] if len(parts) > 2 else "menu"

        if action == "tog":
            if gc.get("enabled"):
                await cnl.disable_global_copy(user_id)
            else:
                if not gc.get("target_chat_id"):
                    return await query.answer("Set a target first", show_alert=True)
                await cnl.set_global_copy(user_id, True, target_chat_id=gc["target_chat_id"])
            gc = await cnl.get_global_copy(user_id) or {}
        elif action == "target":
            set_state(client, CNL_STATE, user_id, {"step": "gcopy_target"})
            await safe_edit(query, "Send target chat ID / @username / link.\n/cancel to abort.",
                            _kb_back("cnl:gcopy"))
            return await safe_answer(query)
        elif action == "types":
            # reuse simple toggle menu stored in global_copy
            selected = list(gc.get("allowed_types") or ["all"])
            if len(parts) > 3:
                tname = parts[3]
                if tname == "all":
                    selected = ["all"]
                else:
                    selected = [x for x in selected if x != "all"]
                    if tname in selected:
                        selected.remove(tname)
                    else:
                        selected.append(tname)
                    if not selected:
                        selected = ["all"]
                await cnl.update_global_copy_filters(user_id, {"allowed_types": selected})
                gc = await cnl.get_global_copy(user_id) or {}
                selected = list(gc.get("allowed_types") or ["all"])
            rows = []
            row = []
            for mt in MEDIA_TYPES:
                mark = "☑" if mt in selected else "☐"
                if "all" in selected and mt != "all":
                    mark = "☐"
                if mt == "all" and "all" in selected:
                    mark = "☑"
                row.append(InlineKeyboardButton(f"{mark} {mt}", callback_data=f"cnl:gcopy:types:{mt}"))
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
            rows.append([InlineKeyboardButton("« Back", callback_data="cnl:gcopy")])
            await safe_edit(query, f"**📦 Global Copy Types**\n`{', '.join(selected)}`",
                            InlineKeyboardMarkup(rows))
            return await safe_answer(query)
        elif action == "block":
            set_state(client, CNL_STATE, user_id, {"step": "gcopy_block"})
            await safe_edit(query, "Send block words for Global Copy (or `-` to clear).",
                            _kb_back("cnl:gcopy"))
            return await safe_answer(query)
        elif action == "white":
            set_state(client, CNL_STATE, user_id, {"step": "gcopy_white"})
            await safe_edit(query, "Send whitelist words for Global Copy (or `-` to clear).",
                            _kb_back("cnl:gcopy"))
            return await safe_answer(query)
        elif action == "repl":
            set_state(client, CNL_STATE, user_id, {"step": "gcopy_repl"})
            await safe_edit(query, "Send replacements `old => new` for Global Copy (or `-` to clear).",
                            _kb_back("cnl:gcopy"))
            return await safe_answer(query)
        elif action == "delay":
            set_state(client, CNL_STATE, user_id, {"step": "gcopy_delay"})
            await safe_edit(query, "Send delay seconds for Global Copy (0–300).",
                            _kb_back("cnl:gcopy"))
            return await safe_answer(query)
        elif action == "ad":
            await cnl.update_global_copy_filters(user_id, {"anti_dupe": not gc.get("anti_dupe", False)})
            gc = await cnl.get_global_copy(user_id) or {}
        elif action == "ft":
            await cnl.update_global_copy_filters(user_id, {"forward_tag": not gc.get("forward_tag", False)})
            gc = await cnl.get_global_copy(user_id) or {}
        elif action == "rl":
            await cnl.update_global_copy_filters(user_id, {"remove_links": not gc.get("remove_links", False)})
            gc = await cnl.get_global_copy(user_id) or {}
        elif action == "cap":
            set_state(client, CNL_STATE, user_id, {"step": "gcopy_cap"})
            await safe_edit(query,
                "Send add-caption for Global Copy.\n"
                "Prefix `start:` / `end:` / `gap:`.\n`-` to clear.",
                _kb_back("cnl:gcopy"))
            return await safe_answer(query)

        en = gc.get("enabled", False)
        text = (
            f"**📋 Global Copy**\n\n"
            f"Status: {'✅ ON' if en else '⏸ OFF'}\n"
            f"Target: `{gc.get('target_chat_id') or '—'}`\n"
            f"Types: `{', '.join(gc.get('allowed_types') or ['all'])}`\n"
            f"Delay: `{gc.get('delay') or 0}s`\n"
            f"Anti-dupe: {'ON' if gc.get('anti_dupe') else 'OFF'}\n"
            f"Forward tag: {'ON' if gc.get('forward_tag') else 'OFF'}\n"
            f"Remove links: {'ON' if gc.get('remove_links') else 'OFF'}\n"
            f"Caption: `{str(gc.get('add_caption') or '—')[:40]}`\n"
            f"Block: {_fmt_words(gc.get('block_words'))}\n"
            f"Whitelist: {_fmt_words(gc.get('whitelist_words'))}\n"
            f"Replacements:\n{_fmt_reps(gc.get('replacements'))}\n\n"
            "Requires connected CNL **account**."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏸ Disable" if en else "▶️ Enable", callback_data="cnl:gcopy:tog")],
            [InlineKeyboardButton("🎯 Set target", callback_data="cnl:gcopy:target"),
             InlineKeyboardButton("📦 Types", callback_data="cnl:gcopy:types")],
            [InlineKeyboardButton("🚫 Block", callback_data="cnl:gcopy:block"),
             InlineKeyboardButton("✅ White", callback_data="cnl:gcopy:white")],
            [InlineKeyboardButton("🔄 Replacements", callback_data="cnl:gcopy:repl"),
             InlineKeyboardButton("✏️ Caption", callback_data="cnl:gcopy:cap")],
            [InlineKeyboardButton("⏱ Delay", callback_data="cnl:gcopy:delay"),
             InlineKeyboardButton("♻️ Anti-Dupe", callback_data="cnl:gcopy:ad")],
            [InlineKeyboardButton("🏷 Fwd Tag", callback_data="cnl:gcopy:ft"),
             InlineKeyboardButton("🔗 Rm Links", callback_data="cnl:gcopy:rl")],
            [InlineKeyboardButton("« Back", callback_data="cnl:home")],
        ])
        await safe_edit(query, text, kb)
        return await safe_answer(query)

    # ── Dupe DB ──
    if data == "cnl:dupe":
        cnl = await get_cnl(user_id)
        if not cnl:
            await safe_edit(query, NOT_CONFIGURED, _kb_home(False))
            return await safe_answer(query)
        info = await cnl.get_dupe_db_info(user_id)
        if info and info.get("enabled"):
            text = f"**🔒 Dupe DB**\n\n✅ External anti-dupe enabled\nDB: `{info.get('db_name')}`"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 Remove", callback_data="cnl:dupe:rm")],
                [InlineKeyboardButton("« Back", callback_data="cnl:home")],
            ])
        else:
            text = (
                "**🔒 Dupe DB**\n\n"
                "Optional external MongoDB for permanent anti-dupe hashes.\n"
                "If unset, uses CNL DB `message_hashes`."
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Set Dupe URI", callback_data="cnl:dupe:set")],
                [InlineKeyboardButton("« Back", callback_data="cnl:home")],
            ])
        await safe_edit(query, text, kb)
        return await safe_answer(query)

    if data == "cnl:dupe:set":
        set_state(client, CNL_STATE, user_id, {"step": "dupe_uri"})
        await safe_edit(query, "Send MongoDB URI for anti-dupe storage.\n/cancel to abort.",
                        _kb_back("cnl:dupe"))
        return await safe_answer(query)

    if data == "cnl:dupe:rm":
        cnl = await get_cnl(user_id)
        if cnl:
            await cnl.remove_dupe_db(user_id)
        await query.answer("Removed")
        query.data = "cnl:dupe"
        return await cnl_callbacks(client, query)

    # ── Stats ──
    if data == "cnl:stats":
        cnl = await get_cnl(user_id)
        if not cnl:
            await safe_edit(query, NOT_CONFIGURED, _kb_home(False))
            return await safe_answer(query)
        stats = await cnl.get_stats()
        q = await cnl.get_user_quota_info(user_id)
        text = (
            f"**📊 CNL Stats**\n\n"
            f"Forwards: `{stats.get('forwards', 0)}`\n"
            f"Blocked: `{stats.get('blocked', 0)}`\n"
            f"Failed: `{stats.get('failed', 0)}`\n"
            f"Duplicates: `{stats.get('duplicates', 0)}`\n"
            f"Rules: `{stats.get('rules', 0)}` ({stats.get('enabled_rules', 0)} on)\n\n"
            f"**Quota today:** {q['used']}"
            + (f"/{q['limit']}" if q.get("limit") else " (unlimited)")
        )
        await safe_edit(query, text, InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="cnl:stats")],
            [InlineKeyboardButton("« Back", callback_data="cnl:home")],
        ]))
        return await safe_answer(query)

    await safe_answer(query)


# ── text input ──────────────────────────────────────────────────────────────

async def handle_cnl_text(client: Client, message: Message) -> bool:
    user_id = message.from_user.id
    from core.access import can_access_bot
    if not await can_access_bot(user_id):
        return False
    state = get_state(client, CNL_STATE, user_id)
    if not state or not isinstance(state, dict):
        return False
    text = (message.text or "").strip()
    if text.lower() in ("/cancel", "cancel"):
        set_state(client, CNL_STATE, user_id, None)
        await message.reply("✅ Cancelled.")
        return True
    step = state.get("step")

    if step == "db_uri":
        ok, msg = await test_cnl_uri(text)
        if not ok:
            await message.reply(f"❌ Connection failed: `{msg}`\nTry again or /cancel.")
            return True
        await set_gate_uri(user_id, text)
        await close_cnl(user_id)
        cnl = await get_cnl(user_id)
        set_state(client, CNL_STATE, user_id, None)
        await message.reply(f"✅ CNL database connected.\n`{msg}`" if cnl else f"⚠️ Saved but reconnect issue: {msg}")
        return True

    if step == "bot_token":
        set_state(client, CNL_STATE, user_id, None)
        await message.reply("CNL no longer accepts raw tokens. Use CNL → Bot → Select My Bot.")
        return True

    
    if step == "acc_session":
        set_state(client, CNL_STATE, user_id, None)
        await message.reply("CNL no longer accepts raw sessions. Use CNL → Account → Select My Account.")
        return True

    
    if step == "rule_source":
        cid = await resolve_chat_id(client, text)
        if cid is None:
            await message.reply("Could not resolve. Send chat ID / @username / link.")
            return True
        state["source_id"] = cid
        state["step"] = "rule_target"
        set_state(client, CNL_STATE, user_id, state)
        await message.reply(f"Source: `{cid}`\n\n**Step 2/3** — Send **target** chat ID / @username / link.")
        return True

    if step == "rule_target":
        cid = await resolve_chat_id(client, text)
        if cid is None:
            await message.reply("Could not resolve target.")
            return True
        state["target_id"] = cid
        state["step"] = "rule_via"
        set_state(client, CNL_STATE, user_id, state)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🤖 Via Bot", callback_data="cnl:via:bot")],
            [InlineKeyboardButton("👤 Via Account", callback_data="cnl:via:acc")],
            [InlineKeyboardButton("« Cancel", callback_data="cnl:rules")],
        ])
        await message.reply(f"Target: `{cid}`\n\n**Step 3/3** — Choose forward method:", reply_markup=kb)
        return True

    if step == "rule_delay":
        try:
            d = max(0, min(300, int(text)))
        except ValueError:
            await message.reply("Send a number 0–300.")
            return True
        cnl = await get_cnl(user_id)
        if cnl:
            await cnl.set_delay(state["sid"], state["tid"], d)
        set_state(client, CNL_STATE, user_id, None)
        await message.reply(f"✅ Delay set to {d}s")
        return True

    if step == "rule_caption":
        cnl = await get_cnl(user_id)
        if not cnl:
            set_state(client, CNL_STATE, user_id, None)
            return True
        if text == "-":
            await cnl.set_add_caption(state["sid"], state["tid"], None)
            await message.reply("✅ Add caption cleared")
        else:
            pos, cap = "end", text
            low = text.lower()
            if low.startswith("start:"):
                pos, cap = "start", text[6:].strip()
            elif low.startswith("gap:"):
                pos, cap = "end_with_gap", text[4:].strip()
            elif low.startswith("end:"):
                pos, cap = "end", text[4:].strip()
            await cnl.set_add_caption(state["sid"], state["tid"], cap, pos)
            await message.reply(f"✅ Add caption set ({pos})")
        set_state(client, CNL_STATE, user_id, None)
        return True

    if step == "rule_caption_tpl":
        cnl = await get_cnl(user_id)
        if not cnl:
            set_state(client, CNL_STATE, user_id, None)
            return True
        if text == "-":
            await cnl.set_custom_caption(state["sid"], state["tid"], None)
            await message.reply("✅ Template cleared")
        else:
            await cnl.set_custom_caption(state["sid"], state["tid"], text)
            await message.reply("✅ Custom caption template saved")
        set_state(client, CNL_STATE, user_id, None)
        return True

    if step == "rule_repl":
        cnl = await get_cnl(user_id)
        if not cnl:
            set_state(client, CNL_STATE, user_id, None)
            return True
        if text == "-":
            await cnl.set_replacements(state["sid"], state["tid"], [])
        else:
            existing = (await cnl.get_forward_rule(state["sid"], state["tid"]) or {}).get("replacements") or []
            reps = list(existing)
            for line in text.split("\n"):
                if "=>" in line:
                    a, b = line.split("=>", 1)
                    reps.append({"from": a.strip(), "to": b.strip()})
            await cnl.set_replacements(state["sid"], state["tid"], reps)
        set_state(client, CNL_STATE, user_id, None)
        await message.reply("✅ Replacements updated")
        return True

    if step == "rule_block":
        cnl = await get_cnl(user_id)
        if cnl:
            if text == "-":
                await cnl.set_block_words(state["sid"], state["tid"], [])
            else:
                existing = list((await cnl.get_forward_rule(state["sid"], state["tid"]) or {}).get("block_words") or [])
                new = cnl._normalize_word_list(text)
                merged = list(dict.fromkeys(existing + new))
                await cnl.set_block_words(state["sid"], state["tid"], merged)
        set_state(client, CNL_STATE, user_id, None)
        await message.reply("✅ Block words updated")
        return True

    if step == "rule_white":
        cnl = await get_cnl(user_id)
        if cnl:
            if text == "-":
                await cnl.set_whitelist_words(state["sid"], state["tid"], [])
            else:
                existing = list((await cnl.get_forward_rule(state["sid"], state["tid"]) or {}).get("whitelist_words") or [])
                new = cnl._normalize_word_list(text)
                merged = list(dict.fromkeys(existing + new))
                await cnl.set_whitelist_words(state["sid"], state["tid"], merged)
        set_state(client, CNL_STATE, user_id, None)
        await message.reply("✅ Whitelist updated")
        return True

    if step == "rule_buttons":
        cnl = await get_cnl(user_id)
        if cnl:
            btns = None if text == "-" else parse_buttons(text)
            await cnl.set_buttons(state["sid"], state["tid"], btns)
        set_state(client, CNL_STATE, user_id, None)
        await message.reply("✅ Buttons updated")
        return True

    if step == "gcopy_target":
        cid = await resolve_chat_id(client, text)
        if cid is None:
            await message.reply("Could not resolve.")
            return True
        cnl = await get_cnl(user_id)
        if cnl:
            await cnl.set_global_copy(user_id, True, target_chat_id=cid)
        set_state(client, CNL_STATE, user_id, None)
        await message.reply(f"✅ Global copy target `{cid}` (enabled)")
        return True

    if step == "gcopy_block":
        cnl = await get_cnl(user_id)
        if cnl:
            words = [] if text == "-" else cnl._normalize_word_list(text)
            await cnl.update_global_copy_filters(user_id, {"block_words": words})
        set_state(client, CNL_STATE, user_id, None)
        await message.reply("✅ Global copy block words updated")
        return True

    if step == "gcopy_white":
        cnl = await get_cnl(user_id)
        if cnl:
            words = [] if text == "-" else cnl._normalize_word_list(text)
            await cnl.update_global_copy_filters(user_id, {"whitelist_words": words})
        set_state(client, CNL_STATE, user_id, None)
        await message.reply("✅ Global copy whitelist updated")
        return True

    if step == "gcopy_repl":
        cnl = await get_cnl(user_id)
        if cnl:
            if text == "-":
                reps = []
            else:
                reps = []
                for line in text.split("\n"):
                    if "=>" in line:
                        a, b = line.split("=>", 1)
                        reps.append({"from": a.strip(), "to": b.strip()})
            await cnl.update_global_copy_filters(user_id, {"replacements": reps})
        set_state(client, CNL_STATE, user_id, None)
        await message.reply("✅ Global copy replacements updated")
        return True

    if step == "gcopy_delay":
        try:
            d = max(0, min(300, int(text)))
        except ValueError:
            await message.reply("Send a number 0–300.")
            return True
        cnl = await get_cnl(user_id)
        if cnl:
            await cnl.update_global_copy_filters(user_id, {"delay": d})
        set_state(client, CNL_STATE, user_id, None)
        await message.reply(f"✅ Global copy delay {d}s")
        return True

    if step == "gcopy_cap":
        cnl = await get_cnl(user_id)
        if cnl:
            if text == "-":
                await cnl.update_global_copy_filters(user_id, {"add_caption": None})
            else:
                pos, cap = "end", text
                low = text.lower()
                if low.startswith("start:"):
                    pos, cap = "start", text[6:].strip()
                elif low.startswith("gap:"):
                    pos, cap = "end_with_gap", text[4:].strip()
                elif low.startswith("end:"):
                    pos, cap = "end", text[4:].strip()
                await cnl.update_global_copy_filters(user_id, {"add_caption": cap, "caption_position": pos})
        set_state(client, CNL_STATE, user_id, None)
        await message.reply("✅ Global copy caption updated")
        return True

    if step == "dupe_uri":
        cnl = await get_cnl(user_id)
        if not cnl:
            set_state(client, CNL_STATE, user_id, None)
            return True
        ok, msg = await cnl.set_dupe_db(user_id, text)
        set_state(client, CNL_STATE, user_id, None)
        await message.reply("✅ Dupe DB set" if ok else f"❌ {msg}")
        return True

    return False


@Client.on_inline_query(filters.regex(r"(?i)^cnl\b"))
async def cnl_inline(client: Client, query: InlineQuery):
    user_id = query.from_user.id
    from core.access import can_access_bot
    if not await can_access_bot(user_id):
        return await query.answer([], cache_time=5)
    q = (query.query or "").strip()
    parts = q.split(None, 1)
    search = (parts[1] if len(parts) > 1 else "").lower()
    results = []
    if not await is_cnl_configured(user_id):
        results.append(InlineQueryResultArticle(
            title="CNL not configured",
            description="Open the bot and set MongoDB URI",
            input_message_content=InputTextMessageContent(NOT_CONFIGURED),
        ))
        return await query.answer(results, cache_time=5)
    cnl = await get_cnl(user_id)
    if not cnl:
        return await query.answer([], cache_time=5)
    rules = await cnl.get_rules_by_owner(user_id)
    for i, r in enumerate(rules[:20], 1):
        title = format_rule(r, i).replace("**", "")
        if search and search not in title.lower() and search not in str(r.get("source_chat_id")) and search not in str(r.get("target_chat_id")):
            continue
        results.append(InlineQueryResultArticle(
            id=f"cnl-{r['source_chat_id']}-{r['target_chat_id']}",
            title=title[:60],
            description=f"via {r.get('forward_via')} | types {','.join((r.get('allowed_types') or ['all'])[:3])}",
            input_message_content=InputTextMessageContent(
                f"CNL rule `{r['source_chat_id']}` → `{r['target_chat_id']}`\n"
                f"Enabled: {r.get('enabled', True)} | via {r.get('forward_via')}"
            ),
        ))
    if not results:
        results.append(InlineQueryResultArticle(
            title="No matching rules",
            description="Add rules in the CNL dashboard",
            input_message_content=InputTextMessageContent("No CNL rules found."),
        ))
    await query.answer(results[:20], cache_time=5)


@Client.on_message(filters.private & filters.command("cnl"))
async def cmd_cnl(client: Client, message: Message):
    user_id = message.from_user.id
    from core.access import can_access_bot
    if not await can_access_bot(user_id):
        return
    text = await _status_text(user_id)
    configured = await is_cnl_configured(user_id)
    await message.reply(text, reply_markup=_kb_home(configured), parse_mode=ParseMode.MARKDOWN)
