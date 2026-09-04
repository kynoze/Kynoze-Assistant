"""Wroxen Search management UI (Management Bot)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from database import (
    is_admin,
    ensure_user,
    get_user_bots,
    get_bot,
    get_user_accounts,
    get_account,
    get_wroxen_db_uri_plain,
    set_wroxen_db_uri,
    create_wroxen_config,
    get_user_wroxen_configs,
    get_wroxen_config,
    update_wroxen_config,
    delete_wroxen_config,
    get_visible_wroxen_configs,
)
from core.state import set_state, get_state
from core.wroxen import db as wxdb
from core.wroxen.db import mask_uri
from core.wroxen.indexer import run_initial_index, request_cancel, format_live, is_running
from core.wroxen.search import clear_cache_for_wroxen
from core.job_worker import get_bot_client, get_user_client
from handlers.ui import safe_edit, safe_answer

logger = logging.getLogger(__name__)


def _wx_home_kb(has_db: bool) -> InlineKeyboardMarkup:
    rows = []
    if not has_db:
        rows.append([InlineKeyboardButton("🔗 Setup Wroxen DB",
                                          callback_data="wx:setup_db")])
    else:
        rows.append([InlineKeyboardButton("🔗 Change / Remove Wroxen DB",
                                          callback_data="wx:setup_db")])
    rows.append([InlineKeyboardButton("➕ Add Wroxen",
                                      callback_data="wx:add")])
    rows.append([InlineKeyboardButton("📋 My Wroxen",
                                      callback_data="wx:list")])
    rows.append([InlineKeyboardButton("« Back to Dashboard",
                                      callback_data="dash:home")])
    return InlineKeyboardMarkup(rows)


def _config_kb(wroxen_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Statistics",
                              callback_data=f"wx:stats:{wroxen_id}")],
        [InlineKeyboardButton("▶️ Start / Re-index",
                              callback_data=f"wx:reindex:{wroxen_id}")],
        [InlineKeyboardButton("👤 Change Index Account",
                              callback_data=f"wx:chgacc:{wroxen_id}")],
        [InlineKeyboardButton("🗑 Clear Index",
                              callback_data=f"wx:clear:{wroxen_id}")],
        [
            InlineKeyboardButton("🟢/🔴 Toggle Enable",
                                 callback_data=f"wx:toggle:{wroxen_id}"),
        ],
        [InlineKeyboardButton("❌ Delete Wroxen",
                              callback_data=f"wx:del:{wroxen_id}")],
        [InlineKeyboardButton("« Back",
                              callback_data="wx:list")],
    ])


async def _account_picker_kb(user_id: int, prefix: str) -> InlineKeyboardMarkup:
    """Build account selection keyboard. prefix e.g. wx:pickacc: or wx:setacc:WID:"""
    accounts = await get_user_accounts(user_id)
    from handlers.ui import format_account_label
    rows = []
    for acc in accounts:
        if acc.get("status") in ("error", "banned"):
            continue
        label = format_account_label(acc, short=True)[:40]
        rows.append([
            InlineKeyboardButton(
                f"👤 {label}",
                callback_data=f"{prefix}{acc['account_id']}",
            )
        ])
    rows.append([
        InlineKeyboardButton("🤖 Use Bot only (no userbot)", callback_data=f"{prefix}bot")
    ])
    rows.append([InlineKeyboardButton("« Cancel", callback_data="wx:home")])
    return InlineKeyboardMarkup(rows)


async def _check_account_member(account_doc: dict, source_chat_id: int) -> Optional[str]:
    """Return error string if account is not a member of source chat, else None."""
    try:
        uc = await get_user_client(account_doc)
        if not uc:
            return "❌ Could not start selected account session."
        from core.permissions import fetch_member, is_member_or_above
        m, err = await fetch_member(uc, source_chat_id, "me")
        if err == "not_participant":
            return "❌ Selected account must be a **member** of the source chat."
        if err:
            return f"❌ Cannot verify account in source chat ({err})."
        if not is_member_or_above(m):
            return "❌ Selected account must be a **member** of the source chat."
        return None
    except Exception as e:
        logger.exception("wroxen account membership check")
        return f"❌ Account membership check failed: {type(e).__name__}"


async def _home_text(user_id: int) -> str:
    """Fast home — no live Wroxen Mongo ping on every open."""
    uri = await get_wroxen_db_uri_plain(user_id)
    if not uri:
        db_line = "❌ Not Configured"
    else:
        db_line = f"✅ Configured ({mask_uri(uri)})"
    configs = await get_visible_wroxen_configs(user_id)
    return (
        "**🔎 Wroxen Search**\n\n"
        f"Database: {db_line}\n"
        f"Configurations: **{len(configs)}**\n\n"
        "_Separate search subsystem. Results show **message links**, "
        "not media._\n"
        "Same bot indexes source + answers searches in target group.\n"
        "_DB is verified when you save URI / index / search — not on every open._"
    )


async def show_wroxen_home(client: Client, query: CallbackQuery) -> None:
    """Open Wroxen home quickly (main DB only)."""
    user_id = query.from_user.id
    await safe_answer(query)
    await ensure_user(user_id)
    uri = await get_wroxen_db_uri_plain(user_id)
    has_db = bool(uri)
    await safe_edit(query, await _home_text(user_id), _wx_home_kb(has_db))


@Client.on_callback_query(filters.regex(r"^wx:") & ~filters.regex(r"^wx:idx_"))
async def wroxen_callbacks(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    from core.access import can_access_bot, can_use_feature
    if not await can_access_bot(user_id):
        return await query.answer("Not allowed", show_alert=True)
    await ensure_user(user_id)
    data = query.data

    if data == "wx:home":
        return await show_wroxen_home(client, query)

    if data == "wx:setup_db":
        has = bool(await get_wroxen_db_uri_plain(user_id))
        rows = [[InlineKeyboardButton("✏️ Enter / Replace URI", callback_data="wx:db_prompt")]]
        if has:
            rows.append([InlineKeyboardButton("🗑 Remove Wroxen DB", callback_data="wx:db_remove")])
        rows.append([InlineKeyboardButton("« Back", callback_data="wx:home")])
        await safe_edit(
            query,
            "**🔗 Wroxen Database**\n\n"
            "Separate MongoDB for Wroxen media index.\n"
            f"Current: **{mask_uri(await get_wroxen_db_uri_plain(user_id))}**",
            InlineKeyboardMarkup(rows),
        )
        return await safe_answer(query)

    if data == "wx:db_prompt":
        set_state(client, "wroxen_state", user_id, {"step": "await_uri"})
        await safe_edit(
            query,
            "**✏️ Send Wroxen MongoDB URI**\n\n"
            "`mongodb+srv://user:***@host/dbname`\n\n"
            "Send `/cancel` to abort.",
            None,
        )
        return await safe_answer(query)

    if data == "wx:db_remove":
        await wxdb.disconnect(user_id)
        await set_wroxen_db_uri(user_id, None)
        await query.answer("Wroxen DB removed", show_alert=True)
        return await show_wroxen_home(client, query)

    if data == "wx:list":
        configs = await get_visible_wroxen_configs(user_id)
        if not configs:
            await query.answer("No Wroxen configs yet", show_alert=True)
            return await show_wroxen_home(client, query)
        rows = []
        for c in configs:
            mark = "🟢" if c.get("enabled", True) else "🔴"
            name = (c.get("target_title") or c.get("name") or str(c.get("target_chat_id") or c["wroxen_id"]))[:32]
            rows.append([
                InlineKeyboardButton(
                    f"{mark} {name}",
                    callback_data=f"wx:open:{c['wroxen_id']}",
                )
            ])
        rows.append([InlineKeyboardButton("« Back", callback_data="wx:home")])
        await safe_edit(query, "**📋 My Wroxen**\n\nSelect a configuration:", InlineKeyboardMarkup(rows))
        return await safe_answer(query)

    if data.startswith("wx:open:"):
        wid = data.split(":", 2)[2]
        cfg = await get_wroxen_config(user_id, wid)
        if not cfg:
            await query.answer("Not found", show_alert=True)
            return
        bot = await get_bot(user_id, cfg["bot_id"])
        from handlers.ui import format_bot_label, format_account_label
        bot_name = format_bot_label(bot, short=True) if bot else "?"
        acc_line = "Bot only"
        aid = cfg.get("index_account_id")
        if aid:
            acc = await get_account(user_id, aid)
            if acc:
                acc_line = format_account_label(acc, short=True)
            else:
                acc_line = f"`{aid[:8]}…` (missing)"
        uri = await get_wroxen_db_uri_plain(user_id)
        count = 0
        if uri:
            ok, _ = await wxdb.ensure_connected(user_id, uri)
            if ok:
                count = await wxdb.count_media(user_id, wid)
        enabled = "🟢 ON" if cfg.get("enabled", True) else "🔴 OFF"
        text = (
            f"**🔎 {cfg.get('name', 'Wroxen')}**\n\n"
            f"🤖 Bot: **{bot_name}**\n"
            f"👤 Index account: **{acc_line}**\n"
            f"📥 Source: **{cfg.get('source_title') or cfg['source_chat_id']}**\n"
            f"🎯 Target: **{cfg.get('target_title') or cfg['target_chat_id']}**\n\n"
            f"📊 Indexed: **{count:,}**\n"
            f"Status: {enabled}\n"
            f"Auto Index: **{'ON' if cfg.get('auto_index', True) else 'OFF'}**"
        )
        await safe_edit(query, text, _config_kb(wid))
        return await safe_answer(query)

    if data.startswith("wx:stats:"):
        wid = data.split(":", 2)[2]
        cfg = await get_wroxen_config(user_id, wid)
        if not cfg:
            return await query.answer("Not found", show_alert=True)
        uri = await get_wroxen_db_uri_plain(user_id)
        if not uri:
            return await query.answer("DB not configured", show_alert=True)
        ok, msg = await wxdb.ensure_connected(user_id, uri)
        if not ok:
            return await query.answer(msg, show_alert=True)
        st = await wxdb.stats_by_type(user_id, wid)
        last = await wxdb.last_indexed_message_id(user_id, wid)
        text = (
            f"**📊 Wroxen Statistics**\n\n"
            f"Total: **{st.get('total', 0):,}**\n"
            f"🎬 Video: **{st.get('video', 0):,}**\n"
            f"📄 Document: **{st.get('document', 0):,}**\n"
            f"🖼 Photo: **{st.get('photo', 0):,}**\n"
            f"🎵 Audio: **{st.get('audio', 0):,}**\n"
            f"🎞 Animation: **{st.get('animation', 0):,}**\n"
            f"Last message id: `{last or '—'}`"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data=f"wx:stats:{wid}")],
            [InlineKeyboardButton("« Back", callback_data=f"wx:open:{wid}")],
        ])
        await safe_edit(query, text, kb)
        return await safe_answer(query)

    if data.startswith("wx:toggle:"):
        wid = data.split(":", 2)[2]
        cfg = await get_wroxen_config(user_id, wid)
        if not cfg:
            return await query.answer("Not found", show_alert=True)
        new_val = not cfg.get("enabled", True)
        await update_wroxen_config(user_id, wid, {"enabled": new_val})
        try:
            from core.wroxen.runtime import refresh_routing
            await refresh_routing()
        except Exception:
            logger.exception("refresh_routing")
        await query.answer("Enabled" if new_val else "Disabled")
        query.data = f"wx:open:{wid}"
        return await wroxen_callbacks(client, query)

    if data.startswith("wx:clear:"):
        wid = data.split(":", 2)[2]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, Clear", callback_data=f"wx:clear_yes:{wid}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"wx:open:{wid}")],
        ])
        await safe_edit(
            query,
            "⚠️ **Clear this Wroxen index?**\n\nOnly indexed media for this config is deleted.",
            kb,
        )
        return await safe_answer(query)

    if data.startswith("wx:clear_yes:"):
        wid = data.split(":", 2)[2]
        uri = await get_wroxen_db_uri_plain(user_id)
        if not uri:
            return await query.answer("DB not configured", show_alert=True)
        ok, msg = await wxdb.ensure_connected(user_id, uri)
        if not ok:
            return await query.answer(msg, show_alert=True)
        n = await wxdb.clear_media(user_id, wid)
        clear_cache_for_wroxen(wid)
        await query.answer(f"Cleared {n} records", show_alert=True)
        query.data = f"wx:open:{wid}"
        return await wroxen_callbacks(client, query)

    if data.startswith("wx:del:"):
        wid = data.split(":", 2)[2]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Delete config", callback_data=f"wx:del_yes:{wid}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"wx:open:{wid}")],
        ])
        await safe_edit(
            query,
            "⚠️ **Delete this Wroxen configuration?**\n\n"
            "Index data for this config will also be cleared.",
            kb,
        )
        return await safe_answer(query)

    if data.startswith("wx:del_yes:"):
        wid = data.split(":", 2)[2]
        uri = await get_wroxen_db_uri_plain(user_id)
        if uri:
            ok, _ = await wxdb.ensure_connected(user_id, uri)
            if ok:
                await wxdb.clear_media(user_id, wid)
        await delete_wroxen_config(user_id, wid)
        clear_cache_for_wroxen(wid)
        try:
            from core.wroxen.runtime import refresh_routing
            await refresh_routing()
        except Exception:
            pass
        await query.answer("Deleted", show_alert=True)
        return await show_wroxen_home(client, query)

    if data == "wx:add":
        uri = await get_wroxen_db_uri_plain(user_id)
        if not uri:
            return await query.answer("Setup Wroxen DB first", show_alert=True)
        ok, msg = await wxdb.ensure_connected(user_id, uri)
        if not ok:
            return await query.answer(msg, show_alert=True)
        bots = await get_user_bots(user_id)
        if not bots:
            return await query.answer("Add a Forward Bot first", show_alert=True)
        rows = []
        for b in bots:
            from handlers.ui import format_bot_label
            name = format_bot_label(b, short=True)[:40]
            rows.append([InlineKeyboardButton(f"🤖 {name}", callback_data=f"wx:addbot:{b['bot_id']}")])
        rows.append([InlineKeyboardButton("« Cancel", callback_data="wx:home")])
        set_state(client, "wroxen_state", user_id, {"step": "add_bot"})
        await safe_edit(
            query,
            "**➕ Add Wroxen — Select Bot**\n\n"
            "1. Bot (search + auto-index)\n"
            "2. **User account** (full history index) — next step\n"
            "3. Source → Target → Index",
            InlineKeyboardMarkup(rows),
        )
        return await safe_answer(query)

    if data.startswith("wx:addbot:"):
        bot_id = data.split(":", 2)[2]
        if not await get_bot(user_id, bot_id):
            return await query.answer("Bot not found", show_alert=True)
        # Next: select index account (userbot) before source
        accounts = await get_user_accounts(user_id)
        usable = [a for a in accounts if a.get("status") not in ("error", "banned")]
        if usable:
            set_state(
                client,
                "wroxen_state",
                user_id,
                {"step": "pick_account_add", "bot_id": bot_id},
            )
            kb = await _account_picker_kb(user_id, prefix="wx:pickacc:")
            await safe_edit(
                query,
                "**👤 Select account for indexing**\n\n"
                "Userbot = **full history** (recommended, old Wroxen jaisa).\n"
                "Account must be a **member** of the source chat.\n\n"
                "This is saved — re-index pe dobara choose nahi karna padega.\n\n"
                "Or pick **Use Bot only** if no user account.",
                kb,
            )
            return await safe_answer(query)
        # No accounts → bot only, go to source
        set_state(
            client,
            "wroxen_state",
            user_id,
            {"step": "await_source", "bot_id": bot_id, "index_account_id": None},
        )
        await safe_edit(
            query,
            "**📥 Source Chat**\n\n"
            "Forward a message from the **source channel**, or send a `t.me` link.\n\n"
            "_No user accounts found — indexing will use bot only._\n\n"
            "`/cancel` to abort.",
            None,
        )
        return await safe_answer(query)

    if data.startswith("wx:reindex:"):
        wid = data.split(":", 2)[2]
        cfg = await get_wroxen_config(user_id, wid)
        if not cfg:
            return await query.answer("Not found", show_alert=True)
        if is_running(user_id):
            return await query.answer("Index already running", show_alert=True)
        # Reuse saved index_account_id — no re-choice on reindex
        set_state(
            client,
            "wroxen_state",
            user_id,
            {
                "step": "await_reindex_last",
                "wroxen_id": wid,
                "source_chat_id": cfg.get("source_chat_id"),
                "source_title": cfg.get("source_title"),
                "target_chat_id": cfg.get("target_chat_id"),
                "target_title": cfg.get("target_title"),
                "bot_id": cfg.get("bot_id"),
                "index_account_id": cfg.get("index_account_id"),  # may be None → bot only
            },
        )
        await safe_edit(
            query,
            "**▶️ Re-index**\n\n"
            "Forward the **last message** from the source, or send a t.me link.\n\n"
            "`/cancel` to abort.",
            None,
        )
        return await safe_answer(query)

    if data.startswith("wx:chgacc:"):
        wid = data.split(":", 2)[2]
        cfg = await get_wroxen_config(user_id, wid)
        if not cfg:
            return await query.answer("Not found", show_alert=True)
        accounts = await get_user_accounts(user_id)
        if not accounts:
            return await query.answer("No accounts added. Add an account first.", show_alert=True)
        kb = await _account_picker_kb(user_id, prefix=f"wx:setacc:{wid}:")
        await safe_edit(
            query,
            "**👤 Select Index Account**\n\n"
            "This account will be used for bulk indexing (full history).\n"
            "It must be a **member** of the source chat.\n\n"
            "Re-index will reuse this choice automatically.",
            kb,
        )
        return await safe_answer(query)

    if data.startswith("wx:setacc:"):
        # wx:setacc:{wroxen_id}:{account_id|bot}
        parts = data.split(":")
        if len(parts) < 4:
            return await query.answer("Invalid", show_alert=True)
        wid = parts[2]
        aid = parts[3]
        cfg = await get_wroxen_config(user_id, wid)
        if not cfg:
            return await query.answer("Not found", show_alert=True)
        if aid == "bot":
            await update_wroxen_config(user_id, wid, {"index_account_id": None})
            await query.answer("Index account cleared (bot only)", show_alert=True)
        else:
            acc = await get_account(user_id, aid)
            if not acc:
                return await query.answer("Account not found", show_alert=True)
            # membership check
            err = await _check_account_member(acc, int(cfg["source_chat_id"]))
            if err:
                return await query.answer(err[:180], show_alert=True)
            await update_wroxen_config(user_id, wid, {"index_account_id": aid})
            await query.answer("Index account saved", show_alert=True)
        query.data = f"wx:open:{wid}"
        return await wroxen_callbacks(client, query)

    if data.startswith("wx:pickacc:"):
        # wx:pickacc:{account_id|bot}
        # Used in: (1) add flow after bot  (2) late pick after skip
        aid = data.split(":", 2)[2]
        st = get_state(client, "wroxen_state", user_id) or {}
        step = st.get("step")
        if step not in ("pick_account_add", "pick_account", "confirm_index"):
            await query.answer("Session expired — start Add Wroxen again", show_alert=True)
            return await show_wroxen_home(client, query)

        if aid == "bot":
            st["index_account_id"] = None
        else:
            acc = await get_account(user_id, aid)
            if not acc:
                return await query.answer("Account not found", show_alert=True)
            sid = int(st.get("source_chat_id") or 0)
            # If source already known → verify membership now
            if sid:
                err = await _check_account_member(acc, sid)
                if err:
                    return await query.answer(err[:180], show_alert=True)
            st["index_account_id"] = aid

        # --- Path A: early pick during Add (right after bot) ---
        if step == "pick_account_add":
            st["step"] = "await_source"
            set_state(client, "wroxen_state", user_id, st)
            from handlers.ui import format_account_label
            acc_label = "Bot only"
            if st.get("index_account_id"):
                acc = await get_account(user_id, st["index_account_id"])
                if acc:
                    acc_label = format_account_label(acc, short=True)
            await safe_edit(
                query,
                f"**📥 Source Chat**\n\n"
                f"Index account: **{acc_label}**\n\n"
                "Forward a message from the **source channel**, or send a `t.me` link.\n\n"
                "Account must already be a **member** of that source.\n\n"
                "`/cancel` to abort.",
                None,
            )
            return await safe_answer(query)

        # --- Path B: late pick (after skip) → confirm ---
        st["step"] = "confirm_index"
        set_state(client, "wroxen_state", user_id, st)
        from handlers.ui import format_account_label
        acc_label = "Bot only"
        if st.get("index_account_id"):
            acc = await get_account(user_id, st["index_account_id"])
            if acc:
                acc_label = format_account_label(acc, short=True)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ START INDEX", callback_data="wx:do_index")],
            [InlineKeyboardButton("❌ Cancel", callback_data="wx:home")],
        ])
        await safe_edit(
            query,
            f"**📚 Confirm Wroxen Index**\n\n"
            f"Source: `{st.get('source_chat_id')}`\n"
            f"Target: `{st.get('target_chat_id', '—')}`\n"
            f"Last msg: `{st.get('last_msg_id')}`\n"
            f"Skip: `{st.get('skip', 0)}`\n"
            f"Index account: **{acc_label}**\n\n"
            "Userbot indexes full history. Search still uses the bot.",
            kb,
        )
        return await safe_answer(query)

    if data == "wx:do_index":
        st = get_state(client, "wroxen_state", user_id) or {}
        if st.get("step") != "confirm_index":
            await query.answer("Session expired", show_alert=True)
            return await show_wroxen_home(client, query)
        return await _start_index_job(client, query, user_id, st)

    await safe_answer(query)


async def _start_index_job(client: Client, query: CallbackQuery, user_id: int, st: Dict):
    uri = await get_wroxen_db_uri_plain(user_id)
    if not uri:
        return await query.answer("DB missing", show_alert=True)
    ok, msg = await wxdb.ensure_connected(user_id, uri)
    if not ok:
        return await query.answer(msg, show_alert=True)

    bot_id = st["bot_id"]
    bot_doc = await get_bot(user_id, bot_id)
    if not bot_doc:
        return await query.answer("Bot missing", show_alert=True)
    bot_client = await get_bot_client(bot_doc)
    if not bot_client:
        return await query.answer("Could not start bot client", show_alert=True)

    # Live Telegram permission checks (Management Bot admin NOT required)
    try:
        from core.permissions import verify_wroxen
        sid = int(st.get("source_chat_id") or 0)
        tid = int(st.get("target_chat_id") or 0)
        if not sid or not tid:
            return await query.answer(
                "Missing source/target chat in session. Open Wroxen config and try Re-index again.",
                show_alert=True,
            )
        perm_err = await verify_wroxen(user_id, str(bot_id), sid, tid)
        if perm_err:
            return await query.answer(perm_err[:180], show_alert=True)
    except KeyError as e:
        logger.exception("wroxen perm KeyError")
        return await query.answer(
            f"Permission check failed: missing field {e}. Re-open config and try again.",
            show_alert=True,
        )
    except Exception as e:
        logger.exception("wroxen perm check")
        return await query.answer(
            f"Permission check failed: {type(e).__name__}",
            show_alert=True,
        )

    # Resolve index account (userbot)
    index_account_id = st.get("index_account_id")
    user_client = None
    if index_account_id:
        acc = await get_account(user_id, index_account_id)
        if not acc:
            return await query.answer(
                "Saved index account missing. Use Change Index Account.",
                show_alert=True,
            )
        # re-check membership at start time
        err = await _check_account_member(acc, int(st["source_chat_id"]))
        if err:
            return await query.answer(err[:180], show_alert=True)
        user_client = await get_user_client(acc)
        if not user_client:
            return await query.answer("Could not start index account client", show_alert=True)

    # create config if new
    wid = st.get("wroxen_id")
    if not wid:
        from core.access import check_limit
        from database import get_user_wroxen_configs
        _err = await check_limit(user_id, "wroxen", len(await get_user_wroxen_configs(user_id)))
        if _err:
            return await query.answer(_err, show_alert=True)

        cfg = await create_wroxen_config(
            user_id,
            bot_id=bot_id,
            source_chat_id=int(st["source_chat_id"]),
            source_title=str(st.get("source_title") or st["source_chat_id"]),
            target_chat_id=int(st["target_chat_id"]),
            target_title=str(st.get("target_title") or st["target_chat_id"]),
        )
        wid = cfg["wroxen_id"]
        try:
            from core.wroxen.runtime import refresh_routing
            await refresh_routing()
        except Exception:
            logger.exception("refresh_routing after create")

    # Persist chosen index account on config (so reindex reuses it)
    await update_wroxen_config(
        user_id,
        wid,
        {"index_account_id": index_account_id},  # None = bot only
    )

    set_state(client, "wroxen_state", user_id, None)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="wx:idx_prog")],
        [InlineKeyboardButton("❌ Stop", callback_data="wx:idx_stop")],
    ])
    mode = "Userbot" if user_client else "Bot"
    await safe_edit(query, f"📥 Wroxen indexing starting ({mode})...", kb)
    await safe_answer(query)
    asyncio.create_task(run_initial_index(
            owner_user_id=user_id,
            bot_client=bot_client,
            wroxen_id=wid,
            source_chat_id=int(st["source_chat_id"]),
            last_msg_id=int(st["last_msg_id"]),
            skip=int(st.get("skip") or 0),
            status_message=query.message,
            user_client=user_client,
            index_account_id=index_account_id,
        )
    )



@Client.on_callback_query(filters.regex(r"^wx:idx_"))
async def wroxen_index_progress(client: Client, query: CallbackQuery):
    """Refresh / Stop for Wroxen indexing progress message."""
    user_id = query.from_user.id
    from core.access import can_access_bot
    if not await can_access_bot(user_id):
        try:
            return await query.answer("Not allowed", show_alert=True)
        except Exception:
            return

    data = query.data or ""
    from core.wroxen.indexer import (
        request_cancel,
        format_live,
        get_progress,
        is_running,
    )
    from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    def _kb(status: str):
        if status in ("done", "cancelled", "error"):
            return InlineKeyboardMarkup([
                [InlineKeyboardButton("« Wroxen", callback_data="wx:home")],
            ])
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="wx:idx_prog")],
            [InlineKeyboardButton("❌ Stop", callback_data="wx:idx_stop")],
        ])

    p = get_progress(user_id)
    status = (p or {}).get("status") or "idle"

    if data == "wx:idx_stop":
        request_cancel(user_id)
        if p is not None:
            p["status"] = "cancelled"
        text = format_live(user_id) or "🛑 Stop requested — indexing will stop shortly."
        try:
            await query.message.edit_text(text, reply_markup=_kb("cancelled"))
        except Exception:
            try:
                await safe_edit(query, text, _kb("cancelled"))
            except Exception:
                pass
        try:
            await query.answer("Stop requested")
        except Exception:
            pass
        return

    if data == "wx:idx_prog":
        if not p:
            try:
                await query.answer(
                    "No active index (finished or not started)",
                    show_alert=True,
                )
            except Exception:
                pass
            return
        text = format_live(user_id) or "No progress data."
        st = p.get("status") or "running"
        try:
            await query.message.edit_text(text, reply_markup=_kb(st))
        except Exception as e:
            # MessageNotModified = same text; still ack
            if "MESSAGE_NOT_MODIFIED" not in str(e).upper() and type(e).__name__ != "MessageNotModified":
                try:
                    await safe_edit(query, text, _kb(st))
                except Exception:
                    pass
        try:
            await query.answer("Refreshed" if st == "running" else st.title())
        except Exception:
            pass
        return

    try:
        await query.answer()
    except Exception:
        pass



async def handle_wroxen_text(client: Client, message: Message) -> bool:
    user_id = message.from_user.id
    from core.access import can_access_bot
    if not await can_access_bot(user_id):
        return False
    st = get_state(client, "wroxen_state", user_id)
    if not st:
        return False
    text = (message.text or message.caption or "").strip()
    if text.lower() in ("/cancel", "cancel"):
        set_state(client, "wroxen_state", user_id, None)
        await message.reply("Cancelled.")
        return True

    step = st.get("step")

    if step == "await_uri":
        ok, msg = await wxdb.test_uri(text)
        if not ok:
            await message.reply(f"❌ {msg}\nSend valid URI or /cancel.")
            return True
        await set_wroxen_db_uri(user_id, text)
        cok, cmsg = await wxdb.connect(user_id, text)
        if not cok:
            await set_wroxen_db_uri(user_id, None)
            await message.reply(f"❌ {cmsg}")
            set_state(client, "wroxen_state", user_id, None)
            return True
        set_state(client, "wroxen_state", user_id, None)
        await message.reply(f"✅ Wroxen DB connected.\n`{mask_uri(text)}`", parse_mode=ParseMode.MARKDOWN)
        return True

    if step in ("await_source", "await_reindex_last", "await_target"):
        from handlers.source_handler import parse_source_from_message

        chat_id, msg_id, err = parse_source_from_message(message)
        # For target we only need chat id — allow link without msg or forward
        if step == "await_target":
            if chat_id is None:
                # try parse chat only from link
                if text.startswith("https://t.me/") or "t.me/" in text:
                    try:
                        parts = text.rstrip("/").split("/")
                        chat_part = parts[-2] if parts[-1].isdigit() else parts[-1]
                        if chat_part.isnumeric():
                            chat_id = int("-100" + chat_part)
                        else:
                            chat = await client.get_chat(chat_part)
                            chat_id = chat.id
                    except Exception:
                        chat_id = None
                if chat_id is None and message.forward_from_chat:
                    chat_id = message.forward_from_chat.id
            if chat_id is None:
                await message.reply("❌ Forward a message from target group or send group link.")
                return True
            try:
                chat = await client.get_chat(chat_id)
                title = getattr(chat, "title", None) or str(chat_id)
            except Exception:
                title = str(chat_id)
            st["target_chat_id"] = int(chat_id)
            st["target_title"] = title
            st["step"] = "await_skip"
            set_state(client, "wroxen_state", user_id, st)
            await message.reply(
                f"Target: **{title}** (`{chat_id}`)\n\n"
                "Now send the **last message** from **source** (or t.me link) for initial index range.\n"
                "You already set source — enter **skip count** (0 = none):",
                parse_mode=ParseMode.MARKDOWN,
            )
            # If source already known, go to skip
            if st.get("source_chat_id") and st.get("last_msg_id"):
                st["step"] = "await_skip"
                set_state(client, "wroxen_state", user_id, st)
                await message.reply("✏️ Enter number of messages to **skip from start** (0 = none):")
            return True

        if chat_id is None or msg_id is None:
            await message.reply("❌ Forward a channel message or send a t.me link.")
            return True
        try:
            chat = await client.get_chat(chat_id)
            title = getattr(chat, "title", None) or str(chat_id)
        except Exception:
            title = str(chat_id)

        if step == "await_source":
            st["source_chat_id"] = int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id
            st["source_title"] = title
            st["last_msg_id"] = int(msg_id)
            # If index account already chosen, verify membership now
            if st.get("index_account_id"):
                acc = await get_account(user_id, st["index_account_id"])
                if not acc:
                    await message.reply("❌ Saved index account missing. /cancel and start again.")
                    return True
                err = await _check_account_member(acc, int(st["source_chat_id"]))
                if err:
                    await message.reply(
                        f"{err}\n\n"
                        "Account ko source chat mein **member** banao, phir dubara source bhejo.\n"
                        "Ya `/cancel` karke **Use Bot only** choose karo."
                    )
                    return True
            st["step"] = "await_target"
            set_state(client, "wroxen_state", user_id, st)
            await message.reply(
                f"Source: **{title}** (`{chat_id}`)\nLast msg: `{msg_id}`\n\n"
                "**🎯 Target Group**\nForward a message from the **search group**, or send its link.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return True

        if step == "await_reindex_last":
            st["last_msg_id"] = int(msg_id)
            st["step"] = "await_skip"
            set_state(client, "wroxen_state", user_id, st)
            await message.reply("✏️ Enter skip count from start (0 = none):")
            return True

    if step == "await_skip":
        try:
            skip = int(text)
            if skip < 0:
                raise ValueError
        except ValueError:
            await message.reply("❌ Non-negative integer or /cancel.")
            return True
        st["skip"] = skip

        # If account still not chosen (e.g. reindex without saved account), offer picker
        if "index_account_id" not in st:
            accounts = await get_user_accounts(user_id)
            usable = [a for a in accounts if a.get("status") not in ("error", "banned")]
            if usable:
                st["step"] = "pick_account"
                set_state(client, "wroxen_state", user_id, st)
                kb = await _account_picker_kb(user_id, prefix="wx:pickacc:")
                await message.reply(
                    "**👤 Select account for indexing**\n\n"
                    "Userbot gives **full history** (recommended).\n"
                    "Account must be a **member** of the source chat.\n\n"
                    "This choice is saved — re-index will reuse it.",
                    reply_markup=kb,
                    parse_mode=ParseMode.MARKDOWN,
                )
                return True
            st["index_account_id"] = None

        st["step"] = "confirm_index"
        set_state(client, "wroxen_state", user_id, st)
        from handlers.ui import format_account_label
        acc_label = "Bot only"
        if st.get("index_account_id"):
            acc = await get_account(user_id, st["index_account_id"])
            if acc:
                acc_label = format_account_label(acc, short=True)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ START INDEX", callback_data="wx:do_index")],
            [InlineKeyboardButton("❌ Cancel", callback_data="wx:home")],
        ])
        await message.reply(
            f"**📚 Confirm Wroxen Index**\n\n"
            f"Source: `{st.get('source_chat_id')}`\n"
            f"Target: `{st.get('target_chat_id', '—')}`\n"
            f"Last msg: `{st.get('last_msg_id')}`\n"
            f"Skip: `{skip}`\n"
            f"Index account: **{acc_label}**\n\n"
            "Only media will be indexed. Search returns links only.",
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN,
        )
        return True

    return False
