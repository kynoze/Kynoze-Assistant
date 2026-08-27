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
        [InlineKeyboardButton("« Dashboard", callback_data="dash:home")],
    ])


async def show_user_storage(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    from database import get_user_bots, get_user_accounts, get_user_targets
    bots = await get_user_bots(user_id)
    accs = await get_user_accounts(user_id)
    targets = await get_user_targets(user_id)
    # jobs count if available
    jobs_n = 0
    try:
        from database import db
        jobs_n = await db.db["jobs"].count_documents({"user_id": user_id})
    except Exception:
        pass
    cnl_rules = 0
    try:
        from core.cnl.db import get_cnl
        cnl = await get_cnl(user_id)
        if cnl:
            rules = await cnl.get_rules_by_owner(user_id)
            cnl_rules = len(rules)
    except Exception:
        pass
    text = (
        f"**🗄️ My Storage**\n\n"
        f"🤖 My Bots: `{len(bots)}`\n"
        f"👤 My Accounts: `{len(accs)}`\n"
        f"🎯 Targets: `{len(targets)}`\n"
        f"📋 Jobs: `{jobs_n}`\n"
        f"📡 CNL Rules: `{cnl_rules}`\n"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Clear My Jobs", callback_data="stor:clear:jobs")],
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
            res = await db.db["jobs"].delete_many({"user_id": user_id})
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

    if data == "own:nu:tog":
        s = await get_system_settings()
        new_v = not s.get("normal_users_enabled", False)
        await update_system_settings({"normal_users_enabled": new_v})
        await query.answer(f"Normal users {'enabled' if new_v else 'disabled'}")
        query.data = "own:home"
        return await owner_callbacks(client, query)

    if data == "own:nu:feats" or data.startswith("own:nu:feat:"):
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
        for k in FEATURES:
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
        names = await db.db.list_collection_names()
        lines = ["**🗄️ Bot Storage**\n"]
        for n in sorted(names):
            try:
                c = await db.db[n].count_documents({})
            except Exception:
                c = "?"
            lines.append(f"`{n}`: {c}")
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
