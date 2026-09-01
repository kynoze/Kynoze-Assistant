"""Separate Indexing feature — UI + orchestration.

Does not mix with normal Jobs. Uses Index Bot + separate Index DB.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, Message

from database import (
    is_admin,
    ensure_user,
    get_user_bots,
    get_bot,
    get_user_targets,
    get_index_settings,
    set_index_db_uri,
    get_index_db_uri_plain,
    set_index_bot_id,
    get_index_bot_id,
)
from core.index_db import (
    mask_uri,
    get_index_collection,
    test_index_uri,
    connect_index_db,
    disconnect_index_db,
    ensure_index_connected,
    count_indexed,
    stats_by_type,
    clear_all_indexed,
)
from core.indexer import (
    run_indexing,
    is_indexing,
    request_cancel as index_cancel,
    request_pause,
    request_resume,
    format_live_progress as index_live,
    get_progress as index_get_progress,
)
from core.index_forwarder import (
    run_index_forward,
    is_forwarding,
    request_cancel as fwd_cancel,
    format_live_progress as fwd_live,
)
from core.state import set_state, get_state
from core.job_worker import get_bot_client
from handlers.keyboards import (
    indexing_home_keyboard,
    index_bot_select_keyboard,
    index_db_setup_keyboard,
    index_progress_keyboard,
    index_fwd_count_keyboard,
    index_fwd_targets_keyboard,
    index_fwd_delete_keyboard,
    index_clear_confirm_keyboard,
    index_start_confirm_keyboard,
)
from handlers.ui import safe_edit, safe_answer

logger = logging.getLogger(__name__)


async def _ensure_idx_db(user_id: int) -> tuple:
    """Returns (ok, status_msg)."""
    uri = await get_index_db_uri_plain(user_id)
    if not uri:
        return False, "Not configured"
    return await ensure_index_connected(user_id, uri)


async def _home_text(user_id: int) -> str:
    """Fast home text — no live Index Mongo ping (that made the button slow)."""
    settings = await get_index_settings(user_id)
    uri = await get_index_db_uri_plain(user_id)
    if not uri:
        db_msg = "❌ Not Configured"
        media_line = "—"
    else:
        db_msg = f"✅ Configured ({mask_uri(uri)})"
        # Count only if this process already has an open Index connection
        col = get_index_collection(user_id)
        if col is not None:
            media_line = f"**{await count_indexed(user_id, settings.get('index_bot_id')):,}**"
        else:
            media_line = "_tap Refresh after connect_"

    bot_id = settings.get("index_bot_id")
    bot_line = "❌ Not Configured"
    if bot_id:
        bot = await get_bot(user_id, bot_id)
        if bot:
            from handlers.ui import format_bot_label
            name = format_bot_label(bot, short=True) if bot else bot_id[:8]
            bot_line = f"🤖 {name}"
        else:
            bot_line = "⚠️ Bot missing (re-select)"

    return (
        "**📦 Indexing**\n\n"
        f"Database: {db_msg}\n"
        f"Index Bot: {bot_line}\n"
        f"Indexed Media: {media_line}\n\n"
        "_Separate from normal Jobs. Media only. "
        "Same bot must forward what it indexed._\n"
        "_DB is checked when you Start / Forward / Clear — not on every open._"
    )


async def show_indexing_home(client: Client, query: CallbackQuery) -> None:
    """Open Indexing home quickly (main DB only; no Atlas ping)."""
    user_id = query.from_user.id
    await safe_answer(query)
    await ensure_user(user_id)
    settings = await get_index_settings(user_id)
    uri = await get_index_db_uri_plain(user_id)
    # Treat "URI saved" as ready for UI; real connect happens on Start/Forward
    db_ok = bool(uri)
    bot_ok = bool(settings.get("index_bot_id") and await get_bot(user_id, settings["index_bot_id"]))
    can_start = db_ok and bot_ok
    await safe_edit(
        query,
        await _home_text(user_id),
        indexing_home_keyboard(db_ok, bot_ok, can_start),
    )


# -------------------- callbacks --------------------

@Client.on_callback_query(filters.regex(r"^idx:"))
async def indexing_callbacks(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    from core.access import can_access_bot
    if not await can_access_bot(user_id):
        return await query.answer("Not allowed", show_alert=True)

    data = query.data
    await ensure_user(user_id)

    if data == "idx:home":
        return await show_indexing_home(client, query)

    if data == "idx:setup_db":
        has = bool(await get_index_db_uri_plain(user_id))
        text = (
            "**🔗 Index Database**\n\n"
            "Indexing uses a **separate** MongoDB database.\n"
            "Paste a MongoDB URI (credentials never shown back).\n\n"
            f"Current: **{mask_uri(await get_index_db_uri_plain(user_id))}**"
        )
        await safe_edit(query, text, index_db_setup_keyboard(has))
        return await safe_answer(query)

    if data == "idx:db_prompt":
        set_state(client, "index_state", user_id, {"step": "await_uri"})
        await safe_edit(
            query,
            "**✏️ Send Index MongoDB URI**\n\n"
            "Example format: `mongodb+srv://user:***@host/dbname`\n\n"
            "Send `/cancel` to abort.",
            None,
        )
        return await safe_answer(query)

    if data == "idx:db_remove":
        await disconnect_index_db(user_id)
        await set_index_db_uri(user_id, None)
        await query.answer("Index DB removed", show_alert=True)
        return await show_indexing_home(client, query)

    if data == "idx:select_bot":
        bots = await get_user_bots(user_id)
        current = await get_index_bot_id(user_id)
        if not bots:
            await query.answer("Add a Forward Bot first", show_alert=True)
            return await show_indexing_home(client, query)
        await safe_edit(
            query,
            "**🤖 Select Index Bot**\n\n"
            "This bot will index media and must be the same bot that later forwards it "
            "(`send_cached_media`).",
            index_bot_select_keyboard(bots, current),
        )
        return await safe_answer(query)

    if data.startswith("idx:setbot:"):
        bid = data.split(":", 2)[2]
        if bid == "__clear__":
            await set_index_bot_id(user_id, None)
            await query.answer("Index Bot cleared")
        else:
            bot = await get_bot(user_id, bid)
            if not bot:
                return await query.answer("Bot not found", show_alert=True)
            await set_index_bot_id(user_id, bid)
            await query.answer("Index Bot set")
        return await show_indexing_home(client, query)

    if data == "idx:stats":
        ok, msg = await _ensure_idx_db(user_id)
        if not ok:
            await query.answer(msg, show_alert=True)
            return await show_indexing_home(client, query)
        bot_id = await get_index_bot_id(user_id)
        st = await stats_by_type(user_id, bot_id)
        text = (
            "**📊 Index Statistics**\n\n"
            f"Total Indexed: **{st.get('total', 0):,}**\n\n"
            f"🎬 Videos: **{st.get('video', 0):,}**\n"
            f"📄 Documents: **{st.get('document', 0):,}**\n"
            f"🖼 Photos: **{st.get('photo', 0):,}**\n"
            f"🎵 Audio: **{st.get('audio', 0):,}**\n"
            f"🎞 Animation: **{st.get('animation', 0):,}**\n"
            f"🎤 Voice: **{st.get('voice', 0):,}**\n"
            f"⏺ Video notes: **{st.get('video_note', 0):,}**"
        )
        from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="idx:stats")],
            [InlineKeyboardButton("« Back", callback_data="idx:home")],
        ])
        await safe_edit(query, text, kb)
        return await safe_answer(query)

    if data == "idx:clear":
        ok, msg = await _ensure_idx_db(user_id)
        if not ok:
            await query.answer(msg, show_alert=True)
            return
        n = await count_indexed(user_id)
        await safe_edit(
            query,
            f"**⚠️ Clear Index Database?**\n\n"
            f"Total indexed media: **{n:,}**\n\n"
            "This only deletes indexed media records.\n"
            "Main users / targets / jobs / bots are **not** touched.\n\n"
            "This cannot be undone.",
            index_clear_confirm_keyboard(),
        )
        return await safe_answer(query)

    if data == "idx:clear_yes":
        ok, msg = await _ensure_idx_db(user_id)
        if not ok:
            await query.answer(msg, show_alert=True)
            return
        deleted = await clear_all_indexed(user_id)
        await query.answer(f"Cleared {deleted} records", show_alert=True)
        return await show_indexing_home(client, query)

    # ---- start indexing flow ----
    if data == "idx:start":
        ok, msg = await _ensure_idx_db(user_id)
        if not ok:
            await query.answer(f"Index DB: {msg}", show_alert=True)
            return
        bot_id = await get_index_bot_id(user_id)
        if not bot_id or not await get_bot(user_id, bot_id):
            await query.answer("Select Index Bot first", show_alert=True)
            return
        if is_indexing(user_id):
            await query.answer("Indexing already running", show_alert=True)
            return
        set_state(client, "index_state", user_id, {"step": "await_source"})
        await safe_edit(
            query,
            "**📥 Start Indexing**\n\n"
            "Forward the **last message** from the source channel/group,\n"
            "or send a `t.me/...` message link.\n\n"
            "Send `/cancel` to abort.",
            None,
        )
        return await safe_answer(query)

    if data == "idx:do_start":
        st = get_state(client, "index_state", user_id) or {}
        if st.get("step") != "confirm":
            await query.answer("Session expired — start again", show_alert=True)
            return await show_indexing_home(client, query)
        if is_indexing(user_id):
            await query.answer("Already running", show_alert=True)
            return
        bot_id = await get_index_bot_id(user_id)
        bot_doc = await get_bot(user_id, bot_id) if bot_id else None
        if not bot_doc:
            await query.answer("Index Bot missing", show_alert=True)
            return
        ok, msg = await _ensure_idx_db(user_id)
        if not ok:
            await query.answer(msg, show_alert=True)
            return
        bot_client = await get_bot_client(bot_doc)
        if not bot_client:
            await query.answer("Could not start Index Bot client", show_alert=True)
            return
        # Source access: public can work without membership; private needs access/admin
        try:
            src = int(st["source_chat_id"])
            chat = await bot_client.get_chat(src)
            chat_type = str(getattr(chat, "type", "") or "").lower()
            is_private_group = "private" in chat_type or chat_type in (
                "chatttype.supergroup",
                "chatttype.group",
                "supergroup",
                "group",
            )
            # If we can get_chat, bot already has some access. For restricted private, require admin.
            if is_private_group:
                try:
                    mem = await bot_client.get_chat_member(src, "me")
                    status = str(getattr(mem, "status", "") or "").lower()
                    if "left" in status or "banned" in status:
                        return await query.answer(
                            "❌ Bot has no access to this private source chat.",
                            show_alert=True,
                        )
                    # private: prefer admin for reliable history
                    if "admin" not in status and "owner" not in status and "creator" not in status:
                        # still allow if member and can read history — warn only for pure channels
                        if "channel" in chat_type:
                            return await query.answer(
                                "❌ Bot must be admin in private channel source.",
                                show_alert=True,
                            )
                except Exception as e:
                    return await query.answer(
                        f"❌ Cannot access source chat: {type(e).__name__}",
                        show_alert=True,
                    )
        except Exception as e:
            return await query.answer(
                f"❌ Source access failed: {type(e).__name__}: {e}",
                show_alert=True,
            )
        set_state(client, "index_state", user_id, None)
        await safe_edit(
            query,
            "📥 Indexing starting...",
            index_progress_keyboard(user_id, paused=False),
        )
        await safe_answer(query)
        asyncio.create_task(
            run_indexing(
                user_id=user_id,
                bot_client=bot_client,
                index_bot_id=bot_id,
                source_chat_id=int(st["source_chat_id"]),
                last_msg_id=int(st["last_msg_id"]),
                skip=int(st.get("skip") or 0),
                status_message=query.message,
            )
        )
        return

    if data == "idx:prog_refresh":
        text = index_live(user_id)
        if not text:
            await query.answer("No active indexing", show_alert=True)
            return await show_indexing_home(client, query)
        p = index_get_progress(user_id) or {}
        paused = p.get("status") == "paused"
        await safe_edit(query, text, index_progress_keyboard(user_id, paused=paused))
        return await safe_answer(query)

    if data == "idx:prog_pause":
        request_pause(user_id)
        await query.answer("Pause requested")
        text = index_live(user_id) or "⏸ Pausing..."
        await safe_edit(query, text, index_progress_keyboard(user_id, paused=True))
        return

    if data == "idx:prog_resume":
        request_resume(user_id)
        await query.answer("Resumed")
        text = index_live(user_id) or "📥 Running..."
        await safe_edit(query, text, index_progress_keyboard(user_id, paused=False))
        return

    if data == "idx:prog_stop":
        index_cancel(user_id)
        await query.answer("Stop requested")
        return

    # ---- forward indexed ----
    if data == "idx:fwd":
        ok, msg = await _ensure_idx_db(user_id)
        if not ok:
            await query.answer(msg, show_alert=True)
            return
        bot_id = await get_index_bot_id(user_id)
        if not bot_id or not await get_bot(user_id, bot_id):
            await query.answer("Select Index Bot first", show_alert=True)
            return
        if is_forwarding(user_id):
            await query.answer("Forward already running", show_alert=True)
            return
        n = await count_indexed(user_id, bot_id)
        set_state(client, "index_state", user_id, {"step": "fwd_count", "bot_id": bot_id})
        await safe_edit(
            query,
            f"**📤 Forward Indexed Media**\n\n"
            f"Available (this Index Bot): **{n:,}**\n\n"
            "How many do you want to forward?",
            index_fwd_count_keyboard(n),
        )
        return await safe_answer(query)

    if data.startswith("idx:fwd_count:"):
        try:
            count = int(data.split(":")[-1])
        except ValueError:
            return await query.answer("Invalid", show_alert=True)
        if count <= 0:
            return await query.answer("Must be > 0", show_alert=True)
        st = get_state(client, "index_state", user_id) or {}
        st.update({"step": "fwd_targets", "count": count, "selected": []})
        set_state(client, "index_state", user_id, st)
        targets = await get_user_targets(user_id)
        if not targets:
            await query.answer("Add a target first", show_alert=True)
            return
        await safe_edit(
            query,
            f"**🎯 Select Targets**\n\n"
            f"Will forward **{count:,}** indexed media.\n"
            "Toggle targets, then Continue.",
            index_fwd_targets_keyboard(targets, []),
        )
        return await safe_answer(query)

    if data == "idx:fwd_custom":
        st = get_state(client, "index_state", user_id) or {}
        st["step"] = "await_fwd_count"
        set_state(client, "index_state", user_id, st)
        await safe_edit(
            query,
            "**✏️ Custom count**\n\nSend a number (how many indexed media to forward).\n\n`/cancel` to abort.",
            None,
        )
        return await safe_answer(query)

    if data.startswith("idx:fwd_tg:"):
        try:
            cid = int(data.split(":")[-1])
        except ValueError:
            return await query.answer("Invalid")
        st = get_state(client, "index_state", user_id) or {}
        selected = list(st.get("selected") or [])
        if cid in selected:
            selected.remove(cid)
        else:
            selected.append(cid)
        st["selected"] = selected
        set_state(client, "index_state", user_id, st)
        targets = await get_user_targets(user_id)
        count = st.get("count", 0)
        await safe_edit(
            query,
            f"**🎯 Select Targets**\n\n"
            f"Will forward **{count:,}** indexed media.\n"
            f"Selected: **{len(selected)}**",
            index_fwd_targets_keyboard(targets, selected),
        )
        return await safe_answer(query)

    if data == "idx:fwd_continue":
        st = get_state(client, "index_state", user_id) or {}
        selected = st.get("selected") or []
        if not selected:
            return await query.answer("Select at least one target", show_alert=True)
        st["step"] = "fwd_delete"
        set_state(client, "index_state", user_id, st)
        await safe_edit(
            query,
            "**🗑 Delete indexed media after forwarding?**\n\n"
            "Yes → delete only records that succeeded on **all** selected targets.\n"
            "Failed / partial records stay in Index DB.",
            index_fwd_delete_keyboard(),
        )
        return await safe_answer(query)

    if data.startswith("idx:fwd_del:"):
        delete_after = data.endswith(":yes")
        st = get_state(client, "index_state", user_id) or {}
        bot_id = st.get("bot_id") or await get_index_bot_id(user_id)
        count = int(st.get("count") or 0)
        selected = [int(x) for x in (st.get("selected") or [])]
        bot_doc = await get_bot(user_id, bot_id) if bot_id else None
        if not bot_doc or count <= 0 or not selected:
            await query.answer("Session invalid — start again", show_alert=True)
            set_state(client, "index_state", user_id, None)
            return await show_indexing_home(client, query)
        ok, msg = await _ensure_idx_db(user_id)
        if not ok:
            await query.answer(msg, show_alert=True)
            return
        bot_client = await get_bot_client(bot_doc)
        if not bot_client:
            await query.answer("Could not start Index Bot", show_alert=True)
            return
        # Target permission: bot must be admin + can post
        try:
            from core.permissions import check_self_admin, has_privilege
            for tid in selected:
                ok, msg = await check_self_admin(bot_client, tid)
                if not ok:
                    return await query.answer(
                        f"❌ Bot not admin in target `{tid}`: {msg}",
                        show_alert=True,
                    )
                try:
                    if not await has_privilege(bot_client, tid, "can_post_messages"):
                        # groups use can_post_messages False often; try send
                        mem = await bot_client.get_chat_member(tid, "me")
                        priv = getattr(mem, "privileges", None)
                        can_post = True
                        if priv is not None and hasattr(priv, "can_post_messages"):
                            if priv.can_post_messages is False:
                                can_post = False
                        if not can_post:
                            return await query.answer(
                                f"❌ Bot cannot post messages in target `{tid}`",
                                show_alert=True,
                            )
                except Exception:
                    pass
        except Exception as e:
            return await query.answer(
                f"Permission check failed: {type(e).__name__}", show_alert=True
            )
        set_state(client, "index_state", user_id, None)
        from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="idx:fwd_prog")],
            [InlineKeyboardButton("❌ Stop", callback_data="idx:fwd_stop")],
        ])
        await safe_edit(query, "📤 Starting index forward...", kb)
        await safe_answer(query)
        asyncio.create_task(
            run_index_forward(
                user_id=user_id,
                bot_client=bot_client,
                index_bot_id=bot_id,
                target_chat_ids=selected,
                count=count,
                delete_after=delete_after,
                status_message=query.message,
            )
        )
        return

    if data == "idx:fwd_prog":
        text = fwd_live(user_id)
        if not text:
            await query.answer("No active forward", show_alert=True)
            return await show_indexing_home(client, query)
        from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="idx:fwd_prog")],
            [InlineKeyboardButton("❌ Stop", callback_data="idx:fwd_stop")],
        ])
        await safe_edit(query, text, kb)
        return await safe_answer(query)

    if data == "idx:fwd_stop":
        fwd_cancel(user_id)
        await query.answer("Stop requested")
        return

    await safe_answer(query)


# -------------------- text inputs for index flows --------------------


async def continue_index_from_source(
    client: Client,
    message: Message,
    user_id: int,
    source_chat_id,
    last_msg_id: int,
) -> None:
    """Called from source_detector when user is in indexing await_source step."""
    st = get_state(client, "index_state", user_id) or {}
    if st.get("step") != "await_source":
        return
    st = {
        "step": "await_skip",
        "source_chat_id": source_chat_id,
        "last_msg_id": last_msg_id,
    }
    set_state(client, "index_state", user_id, st)
    await message.reply(
        f"Source set for **Indexing**.\n"
        f"Chat: `{source_chat_id}`\n"
        f"Last msg id: `{last_msg_id}`\n\n"
        "✏️ Enter number of messages to **skip from the start** (0 = none):\n"
        "Send `/cancel` to abort.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_index_text(client: Client, message: Message) -> bool:
    """Return True if message was consumed by index flow."""
    user_id = message.from_user.id
    from core.access import can_access_bot
    if not await can_access_bot(user_id):
        return False
    st = get_state(client, "index_state", user_id)
    if not st:
        return False

    step = st.get("step")
    text = (message.text or message.caption or "").strip()

    if text.lower() in ("/cancel", "cancel"):
        set_state(client, "index_state", user_id, None)
        await message.reply("Cancelled.", parse_mode=ParseMode.MARKDOWN)
        return True

    if step == "await_uri":
        ok, msg = await test_index_uri(text)
        if not ok:
            await message.reply(f"❌ {msg}\n\nSend a valid URI or /cancel.")
            return True
        await set_index_db_uri(user_id, text)
        cok, cmsg = await connect_index_db(user_id, text)
        if not cok:
            await set_index_db_uri(user_id, None)
            await message.reply(f"❌ Could not connect: {cmsg}")
            set_state(client, "index_state", user_id, None)
            return True
        set_state(client, "index_state", user_id, None)
        await message.reply(
            f"✅ Index DB connected.\nHost: `{mask_uri(text)}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return True

    if step == "await_source":
        # Prefer shared parser (forward_origin + link). Fallback to local parse.
        source_chat_id = None
        last_msg_id = None
        try:
            from handlers.source_handler import parse_source_from_message
            source_chat_id, last_msg_id, err = parse_source_from_message(message)
            if err:
                source_chat_id, last_msg_id = None, None
        except Exception:
            source_chat_id, last_msg_id = None, None

        if source_chat_id is None or last_msg_id is None:
            if text.startswith("https://t.me/") or text.startswith("http://t.me/") or "t.me/" in text:
                try:
                    parts = text.rstrip("/").split("/")
                    last_msg_id = int(parts[-1])
                    chat_part = parts[-2]
                    if chat_part.isnumeric():
                        source_chat_id = int("-100" + chat_part)
                    else:
                        source_chat_id = chat_part
                except Exception:
                    await message.reply("❌ Invalid link format.")
                    return True
            else:
                origin = getattr(message, "forward_origin", None)
                if origin is not None:
                    chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
                    msg_id = getattr(origin, "message_id", None)
                    if chat is not None and msg_id is not None:
                        source_chat_id = chat.id
                        last_msg_id = msg_id
                elif getattr(message, "forward_from_chat", None):
                    source_chat_id = message.forward_from_chat.id
                    last_msg_id = message.forward_from_message_id

        if source_chat_id is None or last_msg_id is None:
            await message.reply(
                "❌ Forward a channel/group message or send a t.me link."
            )
            return True

        st = {
            "step": "await_skip",
            "source_chat_id": source_chat_id,
            "last_msg_id": last_msg_id,
        }
        set_state(client, "index_state", user_id, st)
        await message.reply(
            f"Source set for **Indexing**.\n"
            f"Chat: `{source_chat_id}`\n"
            f"Last msg id: `{last_msg_id}`\n\n"
            "✏️ Enter number of messages to **skip from the start** (0 = none):",
            parse_mode=ParseMode.MARKDOWN,
        )
        return True

    if step == "await_skip":
        try:
            skip = int(text)
            if skip < 0:
                raise ValueError
        except ValueError:
            await message.reply("❌ Send a non-negative integer or /cancel.")
            return True
        st["skip"] = skip
        st["step"] = "confirm"
        set_state(client, "index_state", user_id, st)
        to_index = max(0, int(st["last_msg_id"]) - skip)
        await message.reply(
            f"**📚 Indexing Confirmation**\n\n"
            f"Source: `{st['source_chat_id']}`\n"
            f"Last message id: `{st['last_msg_id']}`\n"
            f"Skip first: `{skip}`\n"
            f"Approx range size: `{to_index}`\n\n"
            "Only **media** will be indexed (text skipped).",
            reply_markup=index_start_confirm_keyboard(),
            parse_mode=ParseMode.MARKDOWN,
        )
        return True

    if step == "await_fwd_count":
        try:
            count = int(text)
            if count <= 0:
                raise ValueError
        except ValueError:
            await message.reply("❌ Send a positive integer or /cancel.")
            return True
        st["count"] = count
        st["step"] = "fwd_targets"
        st["selected"] = []
        set_state(client, "index_state", user_id, st)
        targets = await get_user_targets(user_id)
        if not targets:
            await message.reply("❌ No targets. Add one first.")
            set_state(client, "index_state", user_id, None)
            return True
        await message.reply(
            f"**🎯 Select Targets**\n\nWill forward **{count:,}** indexed media.",
            reply_markup=index_fwd_targets_keyboard(targets, []),
            parse_mode=ParseMode.MARKDOWN,
        )
        return True

    return False
