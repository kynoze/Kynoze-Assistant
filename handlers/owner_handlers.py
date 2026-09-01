"""Owner Control + My Storage."""
from __future__ import annotations

import logging
from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from core.access import (
    is_owner, get_system_settings, update_system_settings,
    DEFAULT_NORMAL_FEATURES, DEFAULT_NORMAL_LIMITS, FEATURES,
    ADMIN_PERMISSION_KEYS, can_access_bot,
)
from handlers.ui import safe_answer, safe_edit

logger = logging.getLogger(__name__)


def _owner_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Dashboard Stats", callback_data="own:stats")],
        [InlineKeyboardButton("👤 Normal Users ON/OFF", callback_data="own:nu:tog")],
        [InlineKeyboardButton("🧩 Normal Features", callback_data="own:nu:feats")],
        [InlineKeyboardButton("📏 Normal Limits", callback_data="own:nu:limits")],
        [InlineKeyboardButton("👮 Admins", callback_data="own:admins")],
        [InlineKeyboardButton("🗄️ Bot Storage", callback_data="own:storage")],
        [InlineKeyboardButton("🗄️ User Databases", callback_data="own:dbs")],
        [InlineKeyboardButton("🛡️ CNL Dupe TTL", callback_data="own:cnlttl")],
        [InlineKeyboardButton("📢 Owner Log Chat", callback_data="own:log")],
        [InlineKeyboardButton("« Dashboard", callback_data="dash:home")],
    ])


async def show_user_storage(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    from database import get_user_bots, get_user_accounts, get_user_targets, db
    bots = await get_user_bots(user_id)
    accs = await get_user_accounts(user_id)
    targets = await get_user_targets(user_id)
    jobs_n = 0
    try:
        jobs_n = await db.db["forward_jobs"].count_documents({"user_id": user_id})
    except Exception:
        pass
    cnl_rules = 0
    try:
        from core.cnl.db import get_cnl
        cnl = await get_cnl(user_id)
        if cnl and hasattr(cnl, "get_rules_by_owner"):
            rules = await cnl.get_rules_by_owner(user_id)
            cnl_rules = len(rules or [])
        elif cnl:
            rules = await cnl.forward_rules.find({"owner_id": int(user_id)}).to_list(500)
            cnl_rules = len(rules)
    except Exception:
        pass
    # Per-user document counts (not whole-DB bytes)
    my_lines = []
    try:
        from core.db_resolver import get_user_data_counts, FEATURES as DB_FEATURES
        for feat in DB_FEATURES:
            c = await get_user_data_counts(user_id, feat)
            if c.get("total_docs"):
                my_lines.append(f"• `{feat}`: `{c['total_docs']}` docs ({c.get('source')})")
    except Exception:
        pass
    text = (
        f"**🗄️ My Storage**\n\n"
        f"**My resources**\n"
        f"🤖 Bots: `{len(bots)}` · 👤 Accounts: `{len(accs)}`\n"
        f"🎯 Targets: `{len(targets)}` · 📋 Jobs: `{jobs_n}`\n"
        f"📡 CNL Rules: `{cnl_rules}`\n"
    )
    if my_lines:
        text += "\n**My data (document counts)**\n" + "\n".join(my_lines) + "\n"
    text += (
        "\n_Note: MongoDB byte size is per-database, not per-user. "
        "Use My Databases → Storage for whole-DB size._"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Clear My Jobs", callback_data="stor:clear:jobs")],
        [InlineKeyboardButton("🗄️ My Databases", callback_data="dash:mydbs")],
        [InlineKeyboardButton("« Dashboard", callback_data="dash:home")],
    ])
    await safe_edit(query, text, kb)
    await safe_answer(query)



@Client.on_callback_query(filters.regex(r"^stor:"))
async def storage_callbacks(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    if not await can_access_bot(user_id):
        return await query.answer("Not allowed", show_alert=True)
    data = query.data
    if data == "stor:clear:jobs":
        await safe_edit(query, "⚠️ Delete **all your jobs**?",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes", callback_data="stor:clear:jobs:yes")],
                [InlineKeyboardButton("❌ Cancel", callback_data="dash:storage")],
            ]))
        return await safe_answer(query)
    if data == "stor:clear:jobs:yes":
        try:
            from database import db
            res = await db.db["forward_jobs"].delete_many({"user_id": user_id})
            await query.answer(f"Deleted {res.deleted_count} jobs")
        except Exception as e:
            await query.answer(f"Error: {e}", show_alert=True)
        await show_user_storage(client, query)
        return
    await safe_answer(query)


@Client.on_callback_query(filters.regex(r"^own:"))
async def owner_callbacks(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    if not is_owner(user_id):
        return await query.answer("Owner only", show_alert=True)
    data = query.data

    if data == "own:home":
        s = await get_system_settings()
        nu = "✅ ON" if s.get("normal_users_enabled") else "❌ OFF"
        text = (
            "**👑 Owner Control**\n\n"
            f"Normal Users: {nu}\n\n"
            "Manage access, limits, admins and storage."
        )
        await safe_edit(query, text, _owner_kb())
        return await safe_answer(query)

    if data == "own:log" or data.startswith("own:log:"):
        from handlers.logchat_handlers import show_owner_log_chat, _prompt_set
        from core.log_chat import set_owner_log_chat, get_owner_log_chat, _send
        if data == "own:log":
            return await show_owner_log_chat(client, query)
        if data == "own:log:set":
            return await _prompt_set(client, query, owner=True)
        if data == "own:log:rm":
            await set_owner_log_chat(None)
            await query.answer("Owner log chat removed")
            return await show_owner_log_chat(client, query)
        if data == "own:log:test":
            info = await get_owner_log_chat()
            if not info:
                return await query.answer("Set owner log chat first", show_alert=True)
            ok, err = await _send(
                info["chat_id"],
                "✅ **Owner log chat test**\n\nErrors and warnings will be posted here.",
            )
            return await query.answer("Test sent" if ok else err[:180], show_alert=not ok)

    if data == "own:nu:tog":
        s = await get_system_settings()
        new_v = not s.get("normal_users_enabled", False)
        await update_system_settings({"normal_users_enabled": new_v})
        await query.answer(f"Normal users {'enabled' if new_v else 'disabled'}")
        query.data = "own:home"
        return await owner_callbacks(client, query)

    if data == "own:nu:feats" or data.startswith("own:nu:feat:"):
        from core.access import FEATURES as ACCESS_FEATURES
        s = await get_system_settings()
        feats = dict(s.get("normal_user_features") or DEFAULT_NORMAL_FEATURES)
        if data.startswith("own:nu:feat:"):
            key = data.split(":")[-1]
            if key in feats:
                feats[key] = not feats[key]
                await update_system_settings({"normal_user_features": feats})
                s = await get_system_settings()
                feats = dict(s.get("normal_user_features") or {})
        rows = []
        for k in ACCESS_FEATURES:
            on = feats.get(k, False)
            rows.append([InlineKeyboardButton(
                f"{'✅' if on else '❌'} {k}",
                callback_data=f"own:nu:feat:{k}",
            )])
        rows.append([InlineKeyboardButton("« Owner", callback_data="own:home")])
        await safe_edit(query, "**🧩 Normal User Features**\nTap to toggle.", InlineKeyboardMarkup(rows))
        return await safe_answer(query)

    if data == "own:nu:limits" or data.startswith("own:nu:lim:"):
        s = await get_system_settings()
        limits = dict(s.get("normal_user_limits") or DEFAULT_NORMAL_LIMITS)
        if data.startswith("own:nu:lim:"):
            # own:nu:lim:targets:inc / dec
            parts = data.split(":")
            if len(parts) >= 5:
                key, op = parts[3], parts[4]
                if key in limits:
                    if op == "inc":
                        limits[key] = min(100, int(limits[key]) + 1)
                    elif op == "dec":
                        limits[key] = max(0, int(limits[key]) - 1)
                    await update_system_settings({"normal_user_limits": limits})
                    s = await get_system_settings()
                    limits = dict(s.get("normal_user_limits") or {})
        rows = []
        for k, v in limits.items():
            rows.append([
                InlineKeyboardButton(f"➖", callback_data=f"own:nu:lim:{k}:dec"),
                InlineKeyboardButton(f"{k}: {v}", callback_data="own:noop"),
                InlineKeyboardButton(f"➕", callback_data=f"own:nu:lim:{k}:inc"),
            ])
        rows.append([InlineKeyboardButton("« Owner", callback_data="own:home")])
        await safe_edit(query, "**📏 Normal User Limits**", InlineKeyboardMarkup(rows))
        return await safe_answer(query)

    if data == "own:noop":
        return await safe_answer(query)

    if data == "own:stats":
        from database import db
        users = await db.db["users"].count_documents({})
        bots = await db.db["forward_bots"].count_documents({}) if "forward_bots" in await db.db.list_collection_names() else 0
        # try common collection names
        names = await db.db.list_collection_names()
        counts = {}
        for n in names:
            try:
                counts[n] = await db.db[n].count_documents({})
            except Exception:
                counts[n] = -1
        lines = [f"**📊 Bot Stats**\n", f"Users: `{users}`"]
        for n, c in sorted(counts.items()):
            lines.append(f"`{n}`: {c}")
        await safe_edit(query, "\n".join(lines)[:3500], InlineKeyboardMarkup([
            [InlineKeyboardButton("« Owner", callback_data="own:home")],
        ]))
        return await safe_answer(query)

    if data == "own:storage":
        from database import db
        from core.db_resolver import mask_uri
        from config import Config
        lines = ["**🗄️ Bot Databases — Main**\n"]
        try:
            st = await db.client[Config.DB_NAME].command("dbStats")
            names = await db.db.list_collection_names()
            lines.append(f"DB: `{Config.DB_NAME}`")
            lines.append(f"URI: `{mask_uri(Config.MONGO_URI)}`")
            lines.append(f"Storage: `{st.get('storageSize', '—')}` bytes")
            lines.append(f"Data: `{st.get('dataSize', '—')}` · Index: `{st.get('indexSize', '—')}`")
            lines.append(f"Collections: `{len(names)}` · Docs: `{st.get('objects', '—')}`\n")
        except Exception:
            lines.append("⚠️ Storage information unavailable\n")
        for n in sorted(await db.db.list_collection_names()):
            try:
                c = await db.db[n].estimated_document_count()
            except Exception:
                c = "?"
            lines.append(f"`{n}`: {c}")
        await safe_edit(query, "\n".join(lines)[:3500], InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 User DBs", callback_data="own:dbs")],
            [InlineKeyboardButton("« Owner", callback_data="own:home")],
        ]))
        return await safe_answer(query)


    if data == "own:cnlttl" or data.startswith("own:cnlttl:"):
        from core.cnl.constants import DEFAULT_DUPE_TTL_DAYS
        s = await get_system_settings()
        cur = s.get("cnl_default_dupe_ttl_days", DEFAULT_DUPE_TTL_DAYS)
        if data.startswith("own:cnlttl:set:"):
            val = data.split(":")[-1]
            days = 0 if val == "off" else int(val)
            await update_system_settings({"cnl_default_dupe_ttl_days": days})
            try:
                from core.cnl.db import _INSTANCES
                for inst in list(_INSTANCES.values()):
                    if getattr(inst, "is_connected", False):
                        await inst._ensure_default_hash_ttl()
            except Exception:
                pass
            cur = days
            await query.answer(("TTL %s days" % days) if days else "TTL disabled")
        text = (
            "**🛡️ CNL Default Anti-Dupe TTL**\n\n"
            "Applies only when user has **no Custom Dupe DB**.\n"
            "Custom Dupe DB hashes stay permanent.\n\n"
            f"Current: **{cur} days**" + (" (off)" if int(cur or 0) == 0 else "")
        )
        rows = [
            [InlineKeyboardButton("7d", callback_data="own:cnlttl:set:7"),
             InlineKeyboardButton("30d", callback_data="own:cnlttl:set:30"),
             InlineKeyboardButton("60d", callback_data="own:cnlttl:set:60")],
            [InlineKeyboardButton("90d", callback_data="own:cnlttl:set:90"),
             InlineKeyboardButton("180d", callback_data="own:cnlttl:set:180"),
             InlineKeyboardButton("Off", callback_data="own:cnlttl:set:off")],
            [InlineKeyboardButton("« Owner", callback_data="own:home")],
        ]
        await safe_edit(query, text, InlineKeyboardMarkup(rows))
        return await safe_answer(query)


    if data == "own:dbs":
        from core.db_resolver import (
            list_all_user_db_configs, resolve_feature_db, get_storage_stats,
            get_user_data_counts, FEATURES as DB_FEATURES, ping_resolved,
        )
        docs = await list_all_user_db_configs(50)
        lines = ["**🗄️ User Databases**\n(credentials hidden)\n"]
        if not docs:
            lines.append("No user DB configs yet.")
        for d in docs[:30]:
            uid = d.get("user_id")
            gname = d.get("global_db_name") or "—"
            has_g = bool(d.get("global_uri_encrypted"))
            feats = d.get("features") or {}
            custom = [k for k, v in feats.items() if (v or {}).get("uri_encrypted")]
            lines.append(f"• **user `{uid}`**")
            if has_g:
                try:
                    r = await resolve_feature_db(int(uid), "cnl")
                    st = await get_storage_stats(int(uid), "cnl") if r.get("source") == "global" else {}
                    status = await ping_resolved(r) if r.get("uri") else "—"
                    lines.append(
                        f"  Global: `{gname}` · status `{status}`"
                    )
                    if st.get("ok"):
                        lines.append(
                            f"  DB size: `{st.get('storage')}` · cols `{st.get('collections')}` · docs `{st.get('documents')}`"
                        )
                except Exception:
                    lines.append(f"  Global: `{gname}`")
            else:
                lines.append("  Global: —")
            if custom:
                lines.append(f"  Custom: {', '.join(custom)}")
            # my data counts sample
            try:
                parts = []
                for feat in ("wroxen", "indexing", "cnl"):
                    c = await get_user_data_counts(int(uid), feat)
                    if c.get("total_docs"):
                        parts.append(f"{feat}:{c['total_docs']}")
                if parts:
                    lines.append(f"  My docs: {', '.join(parts)}")
            except Exception:
                pass
            lines.append("")
        await safe_edit(query, "\n".join(lines)[:3500], InlineKeyboardMarkup([
            [InlineKeyboardButton("« Owner", callback_data="own:home")],
        ]))
        return await safe_answer(query)

    if data == "own:admins" or data.startswith("own:admin:"):
        from database import db
        from config import Config
        from core.access import ADMIN_PERMISSION_KEYS, get_admin_permissions

        if data.startswith("own:admin:perm:"):
            # own:admin:perm:<uid>:<perm>
            parts = data.split(":")
            uid, perm = int(parts[3]), parts[4]
            # Always toggle against EFFECTIVE permissions (so Config admin "*" doesn't collapse to one)
            perms = set(await get_admin_permissions(uid))
            if not perms and (doc := await db.db["bot_admins"].find_one({"user_id": uid})):
                raw = doc.get("permissions")
                if raw == ["*"] or raw == "all":
                    perms = set(ADMIN_PERMISSION_KEYS)
            if perm in perms:
                perms.discard(perm)
            else:
                perms.add(perm)
            await db.db["bot_admins"].update_one(
                {"user_id": uid},
                {"$set": {
                    "user_id": uid,
                    "enabled": True,
                    # explicit list — never leave ["*"] after a manual toggle
                    "permissions": sorted(perms),
                }},
                upsert=True,
            )
            data = f"own:admin:{uid}"

        if data.startswith("own:admin:") and data.count(":") == 2:
            uid = int(data.split(":")[2])
            perms = await get_admin_permissions(uid)
            rows = []
            for k in ADMIN_PERMISSION_KEYS:
                on = k in perms
                rows.append([InlineKeyboardButton(
                    f"{'✅' if on else '❌'} {k}",
                    callback_data=f"own:admin:perm:{uid}:{k}",
                )])
            rows.append([InlineKeyboardButton("« Admins", callback_data="own:admins")])
            await safe_edit(query, f"**🛡 Permissions for** `{uid}`\n\nTap to toggle. Others stay as-is.", InlineKeyboardMarkup(rows))
            return await safe_answer(query)

        lines = ["**👮 Admins**\n", "Tap an admin to set permissions.\n", "Config ADMINS:"]
        buttons = []
        seen = set()
        for a in Config.ADMINS:
            tag = " (owner)" if is_owner(a) else ""
            lines.append(f"• `{a}`{tag}")
            if not is_owner(a) and int(a) not in seen:
                seen.add(int(a))
                buttons.append([InlineKeyboardButton(f"🛡 {a}", callback_data=f"own:admin:{a}")])
        db_admins = await db.db["bot_admins"].find({}).to_list(100)
        if db_admins:
            lines.append("\nDB admins:")
            for d in db_admins:
                uid = int(d.get("user_id") or 0)
                lines.append(f"• `{uid}` enabled={d.get('enabled', True)}")
                if uid and uid not in seen and not is_owner(uid):
                    seen.add(uid)
                    buttons.append([InlineKeyboardButton(f"🛡 {uid}", callback_data=f"own:admin:{uid}")])
        lines.append("\n`/addadmin <id>` · `/rmadmin <id>`")
        buttons.append([InlineKeyboardButton("« Owner", callback_data="own:home")])
        await safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(buttons))
        return await safe_answer(query)

    await safe_answer(query)


@Client.on_message(filters.private & filters.command("addadmin"))
async def cmd_addadmin(client: Client, message: Message):
    if not is_owner(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await message.reply("Usage: `/addadmin <user_id>`")
    uid = int(parts[1])
    from database import db
    await db.db["bot_admins"].update_one(
        {"user_id": uid},
        {"$set": {"user_id": uid, "enabled": True, "permissions": ["*"]}},
        upsert=True,
    )
    await message.reply(f"✅ Admin `{uid}` added (full permissions).")


@Client.on_message(filters.private & filters.command("rmadmin"))
async def cmd_rmadmin(client: Client, message: Message):
    if not is_owner(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await message.reply("Usage: `/rmadmin <user_id>`")
    uid = int(parts[1])
    from database import db
    await db.db["bot_admins"].delete_one({"user_id": uid})
    await message.reply(f"✅ Admin `{uid}` removed.")


@Client.on_message(filters.private & filters.command("ban"))
async def cmd_ban(client: Client, message: Message):
    if not is_owner(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        return await message.reply("Usage: `/ban <user_id>`")
    uid = int(parts[1])
    from database import db
    await db.db["bot_settings"].update_one(
        {"_id": "main"},
        {"$addToSet": {"banned_user_ids": uid}},
        upsert=True,
    )
    await message.reply(f"🚫 User `{uid}` banned from the bot.")


@Client.on_message(filters.private & filters.command("unban"))
async def cmd_unban(client: Client, message: Message):
    if not is_owner(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        return await message.reply("Usage: `/unban <user_id>`")
    uid = int(parts[1])
    from database import db
    await db.db["bot_settings"].update_one(
        {"_id": "main"},
        {"$pull": {"banned_user_ids": uid}},
        upsert=True,
    )
    await message.reply(f"✅ User `{uid}` unbanned.")
