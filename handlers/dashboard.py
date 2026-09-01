from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery
from pyrogram.enums import ParseMode

from database import is_admin, ensure_user, get_stats_overview
from handlers.keyboards import dashboard_keyboard
from handlers.ui import HR, safe_answer, safe_edit
import logging

logger = logging.getLogger(__name__)



async def _allowed_features(user_id: int) -> dict:
    from core.access import (
        is_owner, is_config_admin, is_db_admin, normal_users_enabled,
        get_system_settings, FEATURES,
    )
    if is_owner(user_id) or is_config_admin(user_id) or await is_db_admin(user_id):
        return {f: True for f in FEATURES}
    if not await normal_users_enabled():
        return {f: False for f in FEATURES}
    s = await get_system_settings()
    return dict(s.get("normal_user_features") or {})


async def _dashboard_kb(user_id: int):
    from handlers.keyboards import dashboard_keyboard
    from core.access import is_owner
    return dashboard_keyboard(await _allowed_features(user_id), is_owner=is_owner(user_id))

async def build_dashboard_text(user_id: int) -> str:
    o = await get_stats_overview(user_id)
    return (
        "**🚀 Forward Manager**\n\n"
        f"**📊 Overview**\n"
        f"{HR}\n"
        f"🟢 Running Jobs: **{o['running_jobs']}**\n"
        f"⏸ Paused Jobs: **{o['paused_jobs']}**\n"
        f"✅ Completed: **{o['completed_jobs']}**\n"
        f"👤 Accounts: **{o['active_accounts'] + o['sleeping_accounts'] + o['disabled_accounts']}**\n"
        f"🤖 Bots: **{o['active_bots'] + o['disabled_bots']}**\n"
        f"🎯 Targets: **{o['targets']}**\n"
        f"📤 Forwarded: **{o['total_forwarded']:,}**\n"
        f"{HR}\n"
        "Select an option below:"
    )


HELP_TEXT = (
    "**❓ Help**\n\n"
    f"{HR}\n"
    "• **Jobs** — create, start, pause, monitor future posts\n"
    "• **Accounts** — user accounts for high-volume forwarding\n"
    "• **Forward Bots** — extra bots (token never shown)\n"
    "• **Targets** — destination channels + filters\n"
    "• **Quick Forward** — one-time forward (no job)\n"
    "• **Indexing** — cache media, later forward with same bot\n"
    "• **Wroxen Search** — group search with message links\n"
    "• **Delete Manager** — delete group messages via a forwarding account\n"
    "• **CNL Auto-Post** — isolated live auto-forward (own MongoDB URI, rules, bots, accounts)\n"
    "• **Log Chat** — group/channel for auto-stop reports (bot must be admin)\n"
    "• **Statistics** — live counters with Refresh\n\n"
    "Send a source **link** or **forward a post** to start Quick Forward / Create Job.\n"
    "Type `/cancel` anytime to stop an input flow."
)


@Client.on_message(filters.private & filters.command("start"))
async def cmd_start(client: Client, message: Message):
    user_id = message.from_user.id
    from core.access import can_access_bot, is_owner
    if not await can_access_bot(user_id):
        return await message.reply("❌ You are not allowed to use this bot.")

    await ensure_user(user_id)
    text = await build_dashboard_text(user_id)
    await message.reply(text, reply_markup=await _dashboard_kb(user_id), parse_mode=ParseMode.MARKDOWN)


@Client.on_callback_query(filters.regex(r"^dash:"))
async def dashboard_callbacks(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    from core.access import can_access_bot, can_use_feature
    if not await can_access_bot(user_id):
        return await query.answer("Not allowed", show_alert=True)

    data = query.data

    if data in ["dash:home", "dash:refresh"]:
        await safe_edit(query, await build_dashboard_text(user_id), await _dashboard_kb(user_id))
        return await safe_answer(query)


    # feature permission gate
    _feat_map = {
        "dash:jobs": "jobs", "dash:targets": "targets", "dash:accounts": "accounts",
        "dash:bots": "bots", "dash:stats": "stats", "dash:wroxen": "wroxen",
        "dash:index": "indexing", "dash:delete": "delete_manager", "dash:cnl": "cnl",
        "dash:quick": "quick_forward", "dash:settings": "settings",
    }
    feat = _feat_map.get(data)
    if feat and not await can_use_feature(user_id, feat):
        return await query.answer("This feature is not available for your account.", show_alert=True)

    if data == "dash:existing":
        from handlers.keyboards import existing_forward_keyboard
        await safe_edit(
            query,
            "**📂 Existing Forward**\n\nJobs, Targets, Statistics and Quick Forward.",
            existing_forward_keyboard(),
        )
        return await safe_answer(query)

    if data == "dash:mydbs":
        from handlers.db_settings_handlers import show_my_databases
        await show_my_databases(client, query)
        return

    if data == "dash:storage":
        from handlers.owner_handlers import show_user_storage
        await show_user_storage(client, query)
        return

    if data == "dash:targets":
        from handlers.target_handlers import cmd_targets_internal
        await cmd_targets_internal(client, query)
        return

    if data == "dash:accounts":
        from handlers.accounts_handlers import show_accounts_list
        await show_accounts_list(client, query)
        return

    if data == "dash:bots":
        from handlers.bots_handlers import show_bots_list
        await show_bots_list(client, query)
        return

    if data == "dash:jobs":
        from handlers.jobs_handlers import show_jobs_list
        await show_jobs_list(client, query)
        return

    if data == "dash:stats":
        from handlers.stats_handlers import show_stats_home
        await show_stats_home(client, query)
        return

    if data == "dash:wroxen":
        from handlers.wroxen_handlers import show_wroxen_home
        await show_wroxen_home(client, query)
        return

    if data == "dash:index":
        from handlers.indexing_handlers import show_indexing_home
        await show_indexing_home(client, query)
        return

    if data == "dash:cnl":
        from handlers.cnl_handlers import show_cnl_home
        await show_cnl_home(client, query)
        return

    if data == "dash:delete":
        from handlers.delete_handlers import show_delete_home
        await show_delete_home(client, query)
        return

    if data == "dash:quick":
        await safe_edit(
            query,
            "**⚡ Quick Forward**\n\n"
            "Send a Telegram **message link** or **forward a post** from the source.\n\n"
            "Example:\n`https://t.me/c/1234567890/100`\n\n"
            "Then pick a target. This is a one-time forward (no Job).\n\n"
            "Type /cancel to stop.",
            dashboard_keyboard(),
        )
        return await safe_answer(query)

    if data == "dash:help":
        from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        await safe_edit(
            query,
            HELP_TEXT,
            InlineKeyboardMarkup([[InlineKeyboardButton("« Dashboard", callback_data="dash:home")]]),
        )
        return await safe_answer(query)

    if data == "dash:settings":
        from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        await safe_edit(
            query,
            "**⚙️ Settings**\n\n"
            "Settings are **per target** (caption, filters, delay, future posts).\n\n"
            "Open **Targets** → a target → **Settings**.\n\n"
            "Job monitoring interval: **Jobs** → job → **Monitor**.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🎯 Open Targets", callback_data="dash:targets")],
                [InlineKeyboardButton("« Dashboard", callback_data="dash:home")],
            ]),
        )
        return await safe_answer(query)


@Client.on_callback_query(filters.regex(r"^ui:noop$"))
async def ui_noop(client: Client, query: CallbackQuery):
    await safe_answer(query)
