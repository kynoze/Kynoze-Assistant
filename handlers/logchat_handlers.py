"""User + owner log-chat setup. Management bot must be admin in the chat."""
from __future__ import annotations

import logging

from pyrogram import Client, filters
from pyrogram.enums import ChatType
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from core.access import can_access_bot, is_owner
from core.log_chat import (
    get_owner_log_chat,
    get_user_log_chat,
    resolve_log_chat,
    set_owner_log_chat,
    set_user_log_chat,
    verify_mgmt_admin,
)
from core.state import get_state, set_state
from handlers.ui import safe_answer, safe_edit

logger = logging.getLogger(__name__)

LOG_STATE = "log_chat_state"


def _user_kb(has: bool) -> InlineKeyboardMarkup:
    rows = []
    if has:
        rows.append([InlineKeyboardButton("✏️ Change Log Chat", callback_data="log:set")])
        rows.append([InlineKeyboardButton("🗑 Remove Log Chat", callback_data="log:rm")])
        rows.append([InlineKeyboardButton("📨 Send test message", callback_data="log:test")])
    else:
        rows.append([InlineKeyboardButton("➕ Set Log Chat", callback_data="log:set")])
    rows.append([InlineKeyboardButton("« Dashboard", callback_data="dash:home")])
    return InlineKeyboardMarkup(rows)


def _owner_kb(has: bool) -> InlineKeyboardMarkup:
    rows = []
    if has:
        rows.append([InlineKeyboardButton("✏️ Change Owner Log Chat", callback_data="own:log:set")])
        rows.append([InlineKeyboardButton("🗑 Remove Owner Log Chat", callback_data="own:log:rm")])
        rows.append([InlineKeyboardButton("📨 Send test message", callback_data="own:log:test")])
    else:
        rows.append([InlineKeyboardButton("➕ Set Owner Log Chat", callback_data="own:log:set")])
    rows.append([InlineKeyboardButton("« Owner Control", callback_data="own:home")])
    return InlineKeyboardMarkup(rows)


def _user_text(info) -> str:
    if not info:
        return (
            "**📢 Log Chat**\n\n"
            "Not set.\n\n"
            "Pick a **group or channel**. The **management bot must be admin** there.\n\n"
            "The bot posts a report when work **stops by itself** (error / crash / "
            "lost permission / dead session) — **not** when you press Pause / Cancel / Stop.\n\n"
            "Covered:\n"
            "• Jobs (auto pause / fail)\n"
            "• CNL Auto-Post (rule auto-disabled)\n"
            "• Delete Manager (auto-delete paused)\n"
            "• Wroxen Search (index / client auto-stop)\n"
            "• Index-Forward (crash / auto-fail)\n"
        )
    return (
        "**📢 Log Chat**\n\n"
        f"**Chat:** {info.get('title')}\n"
        f"**ID:** `{info.get('chat_id')}`\n\n"
        "Auto-stop reports for Jobs, CNL, Delete Manager, Wroxen, Index-Forward "
        "are sent here when something fails **without your tap**."
    )


def _owner_text(info) -> str:
    if not info:
        return (
            "**📢 Owner Log Chat**\n\n"
            "Not set.\n\n"
            "Pick a **group or channel**. The **management bot must be admin** there.\n\n"
            "You get **errors and warnings** the owner must see: uncaught exceptions, "
            "Mongo / session / client start failures, log-chat send failures, runtime crashes."
        )
    return (
        "**📢 Owner Log Chat**\n\n"
        f"**Chat:** {info.get('title')}\n"
        f"**ID:** `{info.get('chat_id')}`\n\n"
        "Bot-wide errors and warnings are posted here with full details."
    )


async def show_user_log_chat(client: Client, query: CallbackQuery):
    info = await get_user_log_chat(query.from_user.id)
    await safe_edit(query, _user_text(info), _user_kb(bool(info)))
    await safe_answer(query)


async def show_owner_log_chat(client: Client, query: CallbackQuery):
    info = await get_owner_log_chat()
    await safe_edit(query, _owner_text(info), _owner_kb(bool(info)))
    await safe_answer(query)


async def _prompt_set(client: Client, query: CallbackQuery, *, owner: bool):
    user_id = query.from_user.id
    set_state(client, LOG_STATE, user_id, {"step": "await_chat", "owner": owner})
    who = "owner log chat" if owner else "your log chat"
    await safe_edit(
        query,
        f"**Set {who}**\n\n"
        "1. Add this management bot as **admin** in the group/channel "
        "(for channels: allow **Post Messages**).\n"
        "2. Then send **@username**, invite link, chat ID, **or forward a message** "
        "from that chat.\n\n"
        "/cancel to abort.",
        InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "« Back",
                callback_data="own:log" if owner else "log:home",
            )
        ]]),
    )
    await safe_answer(query)


@Client.on_callback_query(filters.regex(r"^log:"))
async def user_log_callbacks(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    if not await can_access_bot(user_id):
        return await query.answer("Not allowed", show_alert=True)
    data = query.data
    if data in ("log:home", "log:open"):
        return await show_user_log_chat(client, query)
    if data == "log:set":
        return await _prompt_set(client, query, owner=False)
    if data == "log:rm":
        await set_user_log_chat(user_id, None)
        await query.answer("Log chat removed")
        return await show_user_log_chat(client, query)
    if data == "log:test":
        info = await get_user_log_chat(user_id)
        if not info:
            return await query.answer("Set a log chat first", show_alert=True)
        from core.log_chat import _send
        ok, err = await _send(
            info["chat_id"],
            "✅ **Log chat test**\n\nThis chat will receive auto-stop reports for your jobs and features.",
        )
        return await query.answer("Test sent" if ok else err[:180], show_alert=not ok)
    await safe_answer(query)


@Client.on_message(filters.private & filters.forwarded)
async def logchat_forwarded(client: Client, message: Message):
    await handle_log_chat_text(client, message)


async def handle_log_chat_text(client: Client, message: Message) -> bool:
    user_id = message.from_user.id
    st = get_state(client, LOG_STATE, user_id)
    if not st or st.get("step") != "await_chat":
        return False
    text = (message.text or message.caption or "").strip()
    if text.lower() in ("/cancel", "cancel"):
        set_state(client, LOG_STATE, user_id, None)
        await message.reply("Cancelled.")
        return True

    chat = None
    err = None
    fwd = getattr(message, "forward_from_chat", None)
    if fwd and getattr(fwd, "type", None) in (ChatType.CHANNEL, ChatType.SUPERGROUP, ChatType.GROUP):
        chat = fwd
        ok, err = await verify_mgmt_admin(client, chat)
        if not ok:
            chat = None
    else:
        if not text:
            await message.reply("Send @username / link / chat ID, or forward a message from the chat.")
            return True
        chat, err = await resolve_log_chat(client, text)

    if not chat:
        await message.reply(f"❌ {err}\n\nAdd the bot as admin, then try again. /cancel to abort.")
        return True

    title = chat.title or getattr(chat, "username", None) or str(chat.id)
    owner = bool(st.get("owner"))
    set_state(client, LOG_STATE, user_id, None)
    from core.log_chat import _send
    if owner:
        if not is_owner(user_id):
            await message.reply("Owner only.")
            return True
        await set_owner_log_chat(chat.id, title)
        await _send(
            chat.id,
            "✅ **Owner log chat connected**\n\nErrors and warnings for the bot owner will be posted here.",
        )
        await message.reply(f"✅ Owner log chat set: **{title}**\n`{chat.id}`")
    else:
        await set_user_log_chat(user_id, chat.id, title)
        await _send(
            chat.id,
            "✅ **Log chat connected**\n\nAuto-stop reports for this user will be posted here.",
        )
        await message.reply(f"✅ Log chat set: **{title}**\n`{chat.id}`")
    return True
