from pyrogram import Client, filters
from pyrogram.types import CallbackQuery

from database import (
    is_admin,
    ensure_user,
    get_user_bots,
    get_bot,
    update_bot,
    delete_bot,
    get_user_jobs,
    get_bot_scoped,
)
from handlers.keyboards import (
    bots_list_keyboard, bot_settings_keyboard,
    confirm_delete_bot_keyboard
)
from core.state import set_state
from core.errors import friendly_error
from handlers.ui import (
    HR,
    load_secret,
    paginate,
    safe_answer,
    safe_edit,
    status_icon,
    with_pager,
)
from config import Config
import logging

logger = logging.getLogger(__name__)


async def _jobs_using_bot(user_id: int, bot_id: str) -> int:
    try:
        jobs = await get_user_jobs(user_id, limit=200)
    except Exception:
        return 0
    return sum(1 for j in jobs if j.get("bot_id") == bot_id)


async def bot_detail_text(bot: dict, user_id: int) -> str:
    from handlers.ui import format_bot_label

    name = bot.get("name") or bot.get("bot_username") or "Bot"
    status = bot.get("status", "active")
    total = bot.get("total_forwarded", 0)
    uname = bot.get("bot_username")
    uname_s = f"@{uname}" if uname else "—"
    icon = status_icon("active" if status == "active" else "disabled")
    jobs = await _jobs_using_bot(user_id, bot.get("bot_id"))
    connected = "🟢 Connected" if status == "active" else "⚪ Disabled"
    return (
        f"**🤖 {format_bot_label(bot, short=True)}**\n\n"
        f"**Name:** {name}\n"
        f"**Username:** {uname_s}\n"
        f"{HR}\n"
        f"**Status:** {icon} {connected}\n"
        f"**Jobs:** `{jobs}`\n"
        f"**Total Forwarded:** `{int(total):,}`\n"
    )


async def show_bots_list(client: Client, query: CallbackQuery, page: int = 0):
    user_id = query.from_user.id
    try:
        from database import get_visible_bots
        bots = await get_visible_bots(user_id)
    except ImportError:
        bots = await get_user_bots(user_id)

    if not bots:
        text = (
            "**🤖 Forward Bots**\n\n"
            "You have no forwarding bots yet.\n"
            "Click **Add Bot** to add a bot token."
        )
        await safe_edit(query, text, bots_list_keyboard([]))
        return await safe_answer(query)

    from handlers.ui import format_bot_label

    slice_, page, total_pages = paginate(bots, page)
    lines = [f"**🤖 My Bots** ({len(bots)})\n"]
    for b in slice_:
        label = format_bot_label(b, short=True)
        icon = status_icon("active" if b.get("status") == "active" else "disabled")
        lines.append(f"{icon} **{label}**")
    kb = with_pager(bots_list_keyboard(slice_), "bot:listp:", page, total_pages)
    await safe_edit(query, "\n".join(lines), kb)
    await safe_answer(query)


async def _test_bot(bot: dict) -> str:
    from pyrogram import Client as TempClient

    try:
        token = load_secret(bot.get("bot_token") or "")
    except Exception as e:
        return f"❌ Could not read stored bot token.\n{e}"

    temp = TempClient(
        name=f"bot_test_{bot.get('bot_id')}",
        api_id=Config.API_ID,
        api_hash=Config.API_HASH,
        bot_token=token,
        in_memory=True,
        no_updates=True,
    )
    try:
        await temp.start()
        me = await temp.get_me()
        uname = f"@{me.username}" if me.username else str(me.id)
        return f"✅ Connected as **{me.first_name or 'Bot'}** ({uname})"
    except Exception as e:
        return friendly_error("bot test", e)
    finally:
        try:
            await temp.stop()
        except Exception:
            pass


@Client.on_callback_query(filters.regex(r"^bot:"))
async def bots_callbacks(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    from core.access import can_access_bot
    if not await can_access_bot(user_id):
        return await safe_answer(query, "Not allowed", True)

    data = query.data
    await ensure_user(user_id)

    if data == "bot:list":
        await show_bots_list(client, query, 0)
        return

    if data.startswith("bot:listp:"):
        try:
            page = int(data.split(":")[2])
        except Exception:
            page = 0
        await show_bots_list(client, query, page)
        return

    if data == "bot:add":
        await safe_edit(
            query,
            "**➕ Add Forwarding Bot**\n\n"
            "Send the **Bot Token** you got from @BotFather.\n\n"
            "The token is stored encrypted and is **never shown** again.\n\n"
            "Type /cancel to cancel.",
        )
        set_state(client, "bot_add_state", user_id, True)
        return await safe_answer(query)

    if data.startswith("bot:open:") or data.startswith("bot:stats:"):
        bot_id = data.split(":")[2]
        bot = await get_bot_scoped(user_id, bot_id)
        if not bot:
            return await safe_answer(query, "Bot not found", True)
        await safe_edit(query, await bot_detail_text(bot, user_id), bot_settings_keyboard(bot))
        return await safe_answer(query)

    if data.startswith("bot:test:"):
        bot_id = data.split(":")[2]
        bot = await get_bot_scoped(user_id, bot_id)
        if not bot:
            return await safe_answer(query, "Bot not found", True)
        await safe_answer(query, "Testing connection...")
        result = await _test_bot(bot)
        bot = await get_bot_scoped(user_id, bot_id)
        text = await bot_detail_text(bot, user_id) + f"\n{HR}\n{result}"
        await safe_edit(query, text, bot_settings_keyboard(bot))
        return

    if data.startswith("bot:toggle_status:"):
        bot_id = data.split(":")[2]
        bot = await get_bot_scoped(user_id, bot_id)
        if not bot:
            return await safe_answer(query, "Bot not found", True)

        current = bot.get("status", "active")
        new_status = "disabled" if current == "active" else "active"
        await update_bot(user_id, bot_id, {"status": new_status})

        bot = await get_bot_scoped(user_id, bot_id)
        await safe_edit(query, await bot_detail_text(bot, user_id), bot_settings_keyboard(bot))
        return await safe_answer(query, f"Status → {new_status}")

    if data.startswith("bot:delete:"):
        bot_id = data.split(":")[2]
        bot = await get_bot_scoped(user_id, bot_id)
        if not bot:
            return await safe_answer(query, "Bot not found", True)

        await safe_edit(
            query,
            f"**⚠️ Delete Bot?**\n\n"
            f"**{bot.get('name')}**\n\n"
            f"This action cannot be undone.\n"
            f"The token will be removed and is never displayed.",
            confirm_delete_bot_keyboard(bot_id),
        )
        return await safe_answer(query)

    if data.startswith("bot:confirm_delete:"):
        bot_id = data.split(":")[2]
        success = await delete_bot(user_id, bot_id)
        if success:
            await safe_answer(query, "✅ Bot deleted", True)
            await show_bots_list(client, query, 0)
        else:
            await safe_answer(query, "Failed to delete", True)
        return
