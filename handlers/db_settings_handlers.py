"""My Databases / Global DB / optional feature custom DB.

Priority: Feature Custom → Global → Main.

After Global DB is saved, all features work without extra DB setup.
Feature-specific DB is optional and only shown for features the user is allowed to use.
"""
from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from core.access import can_access_bot, can_use_feature
from core.db_resolver import (
    list_user_databases, resolve_feature_db, set_global_db, remove_global_db,
    set_feature_db, remove_feature_db, FEATURES, get_user_db_config,
)
from core.state import get_state, set_state
from handlers.ui import safe_answer, safe_edit

DB_STATE = "db_config_state"

# feature key used in can_use_feature / dashboard
FEATURE_UI = {
    "cnl": ("📡 CNL Custom DB", "cnl"),
    "indexing": ("📦 Indexing Custom DB", "indexing"),
    "wroxen": ("🔎 Wroxen Custom DB", "wroxen"),
    "delete_manager": ("🗑️ Delete Manager Custom DB", "delete_manager"),
    "existing_forward": ("📂 Existing Forward Custom DB", "jobs"),
}


async def show_my_databases(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    rows_data = await list_user_databases(user_id)
    lines = [
        "**🗄️ My Databases**\n",
        "Priority: **Custom → Global → Main**\n",
        "Save **Global DB** once → all features work.\n"
        "Feature custom DB is optional.\n",
    ]
    for r in rows_data:
        lines.append(
            f"**{r.get('label')}**\n"
            f"Source: `{r.get('source')}` · DB: `{r.get('db_name') or '—'}`\n"
            f"URI: `{r.get('masked')}`\n"
        )
    lines.append("\n**Feature resolution**")
    for feat in FEATURES:
        # only show features user can use
        ui_feat = FEATURE_UI.get(feat, (None, feat))[1]
        if not await can_use_feature(user_id, ui_feat) and not await can_use_feature(user_id, feat):
            # still show resolution for transparency for owner/admin
            from core.access import is_owner, is_config_admin
            if not (is_owner(user_id) or is_config_admin(user_id)):
                continue
        r = await resolve_feature_db(user_id, feat)
        lines.append(f"• `{feat}` → {r['source']} (`{r['db_name']}`)")

    buttons = [[InlineKeyboardButton("🌐 Global DB", callback_data="mydb:global")]]
    for feat, (label, gate) in FEATURE_UI.items():
        if await can_use_feature(user_id, gate) or await can_use_feature(user_id, feat):
            buttons.append([InlineKeyboardButton(label, callback_data=f"mydb:feat:{feat}")])
    buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data="dash:mydbs")])
    buttons.append([InlineKeyboardButton("« Dashboard", callback_data="dash:home")])
    await safe_edit(query, "\n".join(lines)[:3500], InlineKeyboardMarkup(buttons))
    await safe_answer(query)


@Client.on_callback_query(filters.regex(r"^mydb:"))
async def mydb_callbacks(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    if not await can_access_bot(user_id):
        return await query.answer("Not allowed", show_alert=True)
    data = query.data

    if data == "mydb:global":
        cfg = await get_user_db_config(user_id)
        has = bool(cfg.get("global_uri_encrypted"))
        text = (
            f"**🌐 Global Database**\n\n"
            f"Configured: {'✅ Yes' if has else '❌ No'}\n\n"
            f"When Global DB is set, **all features** use it by default.\n"
            f"You only need a feature custom DB if you want isolation.\n"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Set / Change URI", callback_data="mydb:global:set")],
            [InlineKeyboardButton("🗑 Remove Global", callback_data="mydb:global:rm")],
            [InlineKeyboardButton("« My Databases", callback_data="dash:mydbs")],
        ])
        await safe_edit(query, text, kb)
        return await safe_answer(query)

    if data == "mydb:global:set":
        set_state(client, DB_STATE, user_id, {"step": "global_uri"})
        await safe_edit(query, "Send **Global MongoDB URI**.\n/cancel to abort.",
                        InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="mydb:global")]]))
        return await safe_answer(query)

    if data == "mydb:global:rm":
        await remove_global_db(user_id)
        await query.answer("Global DB removed")
        query.data = "mydb:global"
        return await mydb_callbacks(client, query)

    if data.startswith("mydb:feat:"):
        parts = data.split(":")
        feat = parts[2]
        action = parts[3] if len(parts) > 3 else "menu"
        # permission: only allowed features
        gate = FEATURE_UI.get(feat, (None, feat))[1]
        if not (await can_use_feature(user_id, gate) or await can_use_feature(user_id, feat)):
            from core.access import is_owner, is_config_admin
            if not (is_owner(user_id) or is_config_admin(user_id)):
                return await query.answer("This feature is not allowed for you.", show_alert=True)
        if action == "set":
            set_state(client, DB_STATE, user_id, {"step": "feat_uri", "feature": feat})
            await safe_edit(
                query,
                f"Send **optional custom MongoDB URI** for `{feat}`.\n"
                f"(Global/Main already works if set.)\n/cancel to abort.",
                InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data=f"mydb:feat:{feat}")]]),
            )
            return await safe_answer(query)
        if action == "rm":
            await remove_feature_db(user_id, feat)
            await query.answer("Removed custom DB")
        r = await resolve_feature_db(user_id, feat)
        text = (
            f"**DB — `{feat}`**\n\n"
            f"Active source: `{r['source']}`\n"
            f"DB name: `{r['db_name']}`\n"
            f"URI: `{r['masked']}`\n\n"
            f"Custom is optional if Global or Main is available."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Set Custom URI", callback_data=f"mydb:feat:{feat}:set")],
            [InlineKeyboardButton("🗑 Remove Custom", callback_data=f"mydb:feat:{feat}:rm")],
            [InlineKeyboardButton("« My Databases", callback_data="dash:mydbs")],
        ])
        await safe_edit(query, text, kb)
        return await safe_answer(query)

    await safe_answer(query)


async def handle_db_config_text(client: Client, message: Message) -> bool:
    user_id = message.from_user.id
    state = get_state(client, DB_STATE, user_id)
    if not state or not isinstance(state, dict):
        return False
    text = (message.text or "").strip()
    if text.lower() in ("/cancel", "cancel"):
        set_state(client, DB_STATE, user_id, None)
        await message.reply("Cancelled.")
        return True
    step = state.get("step")
    if step == "global_uri":
        ok, msg = await set_global_db(user_id, text)
        set_state(client, DB_STATE, user_id, None)
        await message.reply(
            (("✅ " if ok else "❌ ") + msg)
            + ("\n\nAll features can now use this Global DB (Custom still optional)." if ok else "")
        )
        return True
    if step == "feat_uri":
        feat = state.get("feature") or "cnl"
        gate = FEATURE_UI.get(feat, (None, feat))[1]
        if not (await can_use_feature(user_id, gate) or await can_use_feature(user_id, feat)):
            from core.access import is_owner, is_config_admin
            if not (is_owner(user_id) or is_config_admin(user_id)):
                set_state(client, DB_STATE, user_id, None)
                await message.reply("❌ This feature is not allowed for you.")
                return True
        ok, msg = await set_feature_db(user_id, feat, text)
        set_state(client, DB_STATE, user_id, None)
        await message.reply(("✅ " if ok else "❌ ") + msg)
        return True
    # legacy group_name steps ignored
    if step in ("group_name", "group_add_member"):
        set_state(client, DB_STATE, user_id, None)
        await message.reply("Shared admin groups were removed.")
        return True
    return False
