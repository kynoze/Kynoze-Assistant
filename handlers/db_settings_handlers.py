"""My Databases / Global DB / optional feature custom DB.

Priority: Feature Custom → Global → Main (Main only if role/feature allows).
"""
from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from core.access import can_access_bot, can_use_feature, is_owner, is_config_admin
from core.db_resolver import (
    list_user_databases, resolve_feature_db, set_global_db, remove_global_db,
    set_feature_db, remove_feature_db, FEATURES, get_user_db_config,
    get_storage_stats, clear_feature_data, features_using_global, ping_resolved,
    EXTERNAL_REQUIRED,
)
from core.state import get_state, set_state
from handlers.ui import safe_answer, safe_edit

DB_STATE = "db_config_state"

FEATURE_UI = {
    "cnl": ("📡 CNL Custom DB", "cnl"),
    "indexing": ("📦 Indexing Custom DB", "indexing"),
    "wroxen": ("🔎 Wroxen Custom DB", "wroxen"),
    "delete_manager": ("🗑️ Delete Manager Custom DB", "delete_manager"),
    "existing_forward": ("📂 Existing Forward Custom DB", "jobs"),
}


async def _allowed_feat(user_id: int, feat: str) -> bool:
    gate = FEATURE_UI.get(feat, (None, feat))[1]
    if await can_use_feature(user_id, gate) or await can_use_feature(user_id, feat):
        return True
    return is_owner(user_id) or is_config_admin(user_id)


async def show_my_databases(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    rows_data = await list_user_databases(user_id)
    lines = [
        "**🗄️ My Databases**\n",
        "Priority: **Custom → Global → Main***\n",
        "*Main DB is **not** used for Indexing / Wroxen / CNL unless you are Owner/Admin.\n",
        "Save **Global DB** once → those features work.\n",
    ]
    for r in rows_data:
        lines.append(
            f"**{r.get('label')}**\n"
            f"Source: `{r.get('source')}` · DB: `{r.get('db_name') or '—'}`\n"
            f"URI: `{r.get('masked')}`\n"
        )
    lines.append("\n**Feature resolution**")
    for feat in FEATURES:
        if not await _allowed_feat(user_id, feat):
            continue
        r = await resolve_feature_db(user_id, feat)
        src = r.get("source") or "none"
        extra = ""
        if not r.get("configured"):
            extra = " ⚠️ required" if feat in EXTERNAL_REQUIRED else " — not set"
        lines.append(f"• `{feat}` → {src} (`{r.get('db_name') or '—'}`){extra}")

    buttons = [
        [InlineKeyboardButton("🌐 Global DB", callback_data="mydb:global")],
        [InlineKeyboardButton("📊 Storage", callback_data="mydb:stats")],
        [InlineKeyboardButton("🗑 Clear data", callback_data="mydb:clear")],
    ]
    for feat, (label, gate) in FEATURE_UI.items():
        if await _allowed_feat(user_id, feat):
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
        used = await features_using_global(user_id) if has else []
        r = await resolve_feature_db(user_id, "cnl")
        status = await ping_resolved(r) if has and r.get("source") == "global" else ("connected" if has else "disconnected")
        text = (
            f"**🌐 Global Database**\n\n"
            f"Configured: {'✅ Yes' if has else '❌ No'}\n"
            f"DB: `{cfg.get('global_db_name') or '—'}`\n"
            f"Status: `{status}`\n\n"
            f"Used by: {', '.join(used) if used else '—'}\n\n"
            f"Removing configuration does **not** delete MongoDB data."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Set / Change URI", callback_data="mydb:global:set")],
            [InlineKeyboardButton("🗑 Remove config", callback_data="mydb:global:rm")],
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
        await query.answer("Global DB configuration removed (data kept)")
        query.data = "mydb:global"
        return await mydb_callbacks(client, query)

    if data == "mydb:stats":
        from core.db_resolver import get_user_data_counts
        lines = [
            "**📊 Database storage**\n",
            "_DB size = whole MongoDB database (may be shared)._\n",
            "_My data = your document counts only._\n",
        ]
        seen = set()
        for feat in FEATURES:
            if not await _allowed_feat(user_id, feat):
                continue
            st = await get_storage_stats(user_id, feat)
            key = (st.get("source"), st.get("db_name"))
            if key not in seen:
                seen.add(key)
                if not st.get("ok"):
                    lines.append(
                        f"**DB via `{feat}`** (`{st.get('source')}`)\n"
                        f"{st.get('error') or '⚠️ Storage information unavailable'}\n"
                    )
                else:
                    lines.append(
                        f"**Database:** `{st.get('db_name')}` ({st.get('source')})\n"
                        f"Status: ✅ {st.get('status')}\n"
                        f"DB size: `{st.get('storage')}` · Data: `{st.get('data')}` · Index: `{st.get('index')}`\n"
                        f"Collections: `{st.get('collections')}` · DB docs: `{st.get('documents')}`\n"
                    )
            try:
                mine = await get_user_data_counts(user_id, feat)
                if mine.get("total_docs") is not None:
                    lines.append(f"My `{feat}` docs: `{mine.get('total_docs')}`\n")
            except Exception:
                pass
        await safe_edit(
            query,
            "\n".join(lines)[:3500],
            InlineKeyboardMarkup([[InlineKeyboardButton("« My Databases", callback_data="dash:mydbs")]]),
        )
        return await safe_answer(query)

    if data == "mydb:clear":
        rows = []
        for feat, (label, _) in FEATURE_UI.items():
            if await _allowed_feat(user_id, feat):
                rows.append([InlineKeyboardButton(f"🗑 Clear {feat}", callback_data=f"mydb:clear:{feat}")])
        rows.append([InlineKeyboardButton("« My Databases", callback_data="dash:mydbs")])
        await safe_edit(
            query,
            "**🗑 Clear feature data**\n\n"
            "This deletes **your** data in that feature's collections.\n"
            "It does **not** remove the DB configuration or drop the whole MongoDB database.\n"
            "Global DB: each feature is cleared separately.",
            InlineKeyboardMarkup(rows),
        )
        return await safe_answer(query)

    if data.startswith("mydb:clear:"):
        parts = data.split(":")
        feat = parts[2]
        if feat not in FEATURES:
            return await query.answer("Unknown feature", show_alert=True)
        if not await _allowed_feat(user_id, feat):
            return await query.answer("Not allowed", show_alert=True)
        if len(parts) == 3:
            await safe_edit(
                query,
                f"⚠️ **WARNING**\n\n"
                f"Permanently delete **{feat}** data?\n"
                f"This cannot be undone.\n\n"
                f"DB configuration will stay. Only documents/collections are cleared.",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data="mydb:clear")],
                    [InlineKeyboardButton("🗑 Confirm", callback_data=f"mydb:clear:{feat}:yes")],
                ]),
            )
            return await safe_answer(query)
        if len(parts) >= 4 and parts[3] == "yes":
            ok, msg = await clear_feature_data(user_id, feat)
            await query.answer(msg, show_alert=True)
            query.data = "mydb:clear"
            return await mydb_callbacks(client, query)

    if data.startswith("mydb:feat:"):
        parts = data.split(":")
        feat = parts[2]
        action = parts[3] if len(parts) > 3 else "menu"
        if not await _allowed_feat(user_id, feat):
            return await query.answer("This feature is not allowed for you.", show_alert=True)
        if action == "set":
            set_state(client, DB_STATE, user_id, {"step": "feat_uri", "feature": feat})
            await safe_edit(
                query,
                f"Send **optional custom MongoDB URI** for `{feat}`.\n"
                f"Global DB is used if this is not set.\n/cancel to abort.",
                InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data=f"mydb:feat:{feat}")]]),
            )
            return await safe_answer(query)
        if action == "rm":
            await remove_feature_db(user_id, feat)
            await query.answer("Custom DB configuration removed (data kept)")
        r = await resolve_feature_db(user_id, feat)
        st = await get_storage_stats(user_id, feat)
        need = feat in EXTERNAL_REQUIRED
        text = (
            f"**DB — `{feat}`**\n\n"
            f"Active source: `{r.get('source')}`\n"
            f"DB name: `{r.get('db_name') or '—'}`\n"
            f"URI: `{r.get('masked')}`\n"
            f"Status: `{st.get('status')}`\n"
        )
        if st.get("ok"):
            text += f"Storage: `{st.get('storage')}` · Collections: `{st.get('collections')}`\n"
        elif r.get("error"):
            text += f"\n{r['error']}\n"
        if need:
            text += "\nNormal users: **Global or Custom DB required** (Main DB is blocked)."
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Set Custom URI", callback_data=f"mydb:feat:{feat}:set")],
            [InlineKeyboardButton("🗑 Remove Custom config", callback_data=f"mydb:feat:{feat}:rm")],
            [InlineKeyboardButton("🗑 Clear this feature data", callback_data=f"mydb:clear:{feat}")],
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
            + ("\n\nAllowed features now use this Global DB unless a custom DB is set." if ok else "")
        )
        return True
    if step == "feat_uri":
        feat = state.get("feature") or "cnl"
        if not await _allowed_feat(user_id, feat):
            set_state(client, DB_STATE, user_id, None)
            await message.reply("❌ This feature is not allowed for you.")
            return True
        ok, msg = await set_feature_db(user_id, feat, text)
        set_state(client, DB_STATE, user_id, None)
        await message.reply(("✅ " if ok else "❌ ") + msg)
        return True
    if step in ("group_name", "group_add_member"):
        set_state(client, DB_STATE, user_id, None)
        await message.reply("Shared admin groups were removed.")
        return True
    return False
