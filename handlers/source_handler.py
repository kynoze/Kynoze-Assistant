# Link/forward detect.
# Create Job wizard source step is handled HERE (not silently dropped).
# Quick Forward is kept (not removed).

import logging
import re
from typing import Optional, Tuple, Union

from pyrogram import Client, filters
from pyrogram.enums import ChatType
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from database import (
    ensure_user,
    get_target,
    get_user_targets,
    is_admin,
)
from handlers.keyboards import select_targets_keyboard
from core.state import clear_all_states, get_state, set_state
from handlers.ui import safe_answer, safe_edit

logger = logging.getLogger(__name__)

CANCEL_FLAGS = {}
FORWARDING = {}

LINK_RE = re.compile(
    r"(https?://)?(t\.me|telegram\.me|telegram\.dog)/(c/)?([a-zA-Z0-9_]+|\d+)/(\d+)"
)


def build_source_options_keyboard(source_chat_id, last_msg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Create Job (Recommended)",
                    callback_data=f"src:create_job:{source_chat_id}:{last_msg_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "Quick Forward",
                    callback_data=f"src:quick:{source_chat_id}:{last_msg_id}",
                )
            ],
            [InlineKeyboardButton("Cancel", callback_data="src:cancel")],
        ]
    )


def _forward_origin_chat(message: Message):
    """Use forward_origin only — do not read deprecated forward_from_chat."""
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return None, None
    chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
    msg_id = getattr(origin, "message_id", None)
    if chat is None:
        return None, None
    return chat, msg_id


def parse_source_from_message(
    message: Message,
) -> Tuple[Optional[Union[int, str]], Optional[int], Optional[str]]:
    """Returns (source_chat_id, last_msg_id, error)."""
    origin_chat, origin_id = _forward_origin_chat(message)

    if message.text and not origin_chat:
        m = LINK_RE.search(message.text)
        if not m:
            return None, None, "Send a Telegram message link or forward a post from the source."
        chat_part = m.group(4)
        last_msg_id = int(m.group(5))
        source_chat_id = int(f"-100{chat_part}") if chat_part.isdigit() else chat_part
        return source_chat_id, last_msg_id, None

    if origin_chat:
        chat_type = getattr(origin_chat, "type", None)
        if chat_type not in [ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP]:
            return None, None, "I can only use Channels and Groups as source."
        return origin_chat.username or origin_chat.id, origin_id, None

    return None, None, "Send a Telegram message link or forward a post from the source."


async def continue_job_create_from_source(
    client: Client, message: Message, user_id: int, source_chat_id, last_msg_id: int
):
    try:
        source_chat = await client.get_chat(source_chat_id)
    except Exception as e:
        return await message.reply(
            f"Cannot access source chat.\n"
            "Make sure this bot can see that chat, then try again.",
            parse_mode=None,
        )

    if source_chat.type not in [ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP]:
        return await message.reply("Source must be a Channel or Group.")

    targets = await get_user_targets(user_id)
    if not targets:
        set_state(client, "job_create_state", user_id, None)
        return await message.reply(
            "No targets yet. Add one first (Targets → Add Target)."
        )

    set_state(client, "job_create_state", user_id, {
        "step": "select_targets",
        "source_chat_id": source_chat.id,
        "source_title": source_chat.title or "Unknown",
        "last_msg_id": last_msg_id,
        "selected_targets": [],
    })

    await message.reply(
        f"**Source:** {source_chat.title}\n"
        f"**ID:** `{source_chat.id}`\n"
        f"**Last Message ID:** `{last_msg_id}`\n\n"
        f"**Create Job – Select Targets**\n"
        f"Select one or more targets:",
        reply_markup=select_targets_keyboard(targets, []),
    )


@Client.on_message(
    filters.private
    & filters.incoming
    & (filters.forwarded | filters.regex(LINK_RE))
)
async def source_detector(client: Client, message: Message):
    user_id = message.from_user.id
    from core.access import can_access_bot
    if not await can_access_bot(user_id):
        return await message.reply("You are not allowed to use this bot.")

    await ensure_user(user_id)

    source_chat_id, last_msg_id, err = parse_source_from_message(message)
    if err:
        return await message.reply(err)

    from core.state import get_state

    # CNL flows own text (e.g. URL button labels contain t.me links).
    cnl_state = get_state(client, "cnl_state", user_id)
    if isinstance(cnl_state, dict) and cnl_state.get("step"):
        from handlers.cnl_handlers import handle_cnl_text
        if await handle_cnl_text(client, message):
            return

    # Wroxen / Indexing flows own the next source message.
    wroxen_state = get_state(client, "wroxen_state", user_id) or {}
    if isinstance(wroxen_state, dict) and wroxen_state.get("step") in (
        "await_source", "await_target", "await_reindex_last"
    ):
        from handlers.wroxen_handlers import handle_wroxen_text
        await handle_wroxen_text(client, message)
        return

    delete_state = get_state(client, "delete_state", user_id) or {}
    if isinstance(delete_state, dict) and delete_state.get("step") == "await_group":
        from handlers.delete_handlers import continue_delete_from_source
        await continue_delete_from_source(
            client, message, user_id, source_chat_id, last_msg_id
        )
        return

    # Indexing flow owns the next source message — do not show Job/Quick Forward.
    index_state = get_state(client, "index_state", user_id) or {}
    if isinstance(index_state, dict) and index_state.get("step") == "await_source":
        from handlers.indexing_handlers import continue_index_from_source
        await continue_index_from_source(
            client, message, user_id, source_chat_id, last_msg_id
        )
        return

    job_state = get_state(client, "job_create_state", user_id) or {}
    if isinstance(job_state, dict) and job_state.get("step") == "source":
        await continue_job_create_from_source(
            client, message, user_id, source_chat_id, last_msg_id
        )
        return

    if FORWARDING.get(user_id):
        return await message.reply(
            "Please wait until the current forwarding process finishes.\n"
            "Send `cancel` to stop it."
        )

    try:
        source_chat = await client.get_chat(source_chat_id)
    except Exception as e:
        return await message.reply(
            f"Cannot access source chat.\n"
            "Make sure this bot can see that chat, then try again.",
            parse_mode=None,
        )

    if source_chat.type not in [ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP]:
        return await message.reply("Source must be a Channel or Group.")

    await message.reply(
        f"**Source Detected**\n\n"
        f"**Chat:** {source_chat.title}\n"
        f"**ID:** `{source_chat.id}`\n"
        f"**Last Message ID:** `{last_msg_id}`\n\n"
        f"How do you want to forward?",
        reply_markup=build_source_options_keyboard(source_chat.id, last_msg_id),
    )


@Client.on_callback_query(filters.regex(r"^src:"))
async def source_options_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    from core.access import can_access_bot
    if not await can_access_bot(user_id):
        return await query.answer("Not allowed", show_alert=True)

    data = query.data

    if data == "src:cancel":
        set_state(client, "job_create_state", user_id, None)
        await query.message.edit_text("Cancelled.")
        return await query.answer()

    if data.startswith("src:create_job:"):
        parts = data.split(":")
        try:
            source_chat_id = int(parts[2]) if parts[2].lstrip("-").isdigit() else parts[2]
            last_msg_id = int(parts[3])
        except Exception:
            return await query.answer("Invalid data", show_alert=True)

        targets = await get_user_targets(user_id)
        if not targets:
            return await query.answer("Add a target first.", show_alert=True)

        source_title = "Detected Source"
        try:
            chat = await client.get_chat(source_chat_id)
            source_title = chat.title or source_title
            source_chat_id = chat.id
        except Exception:
            pass

        set_state(client, "job_create_state", user_id, {
            "step": "select_targets",
            "source_chat_id": source_chat_id,
            "source_title": source_title,
            "last_msg_id": last_msg_id,
            "selected_targets": [],
        })

        await query.message.edit_text(
            "**Create Job – Select Targets**\n\n"
            "Select one or more targets:",
            reply_markup=select_targets_keyboard(targets, []),
        )
        return await query.answer()

    if data.startswith("src:quick:"):
        parts = data.split(":")
        try:
            source_chat_id = int(parts[2]) if parts[2].lstrip("-").isdigit() else parts[2]
            last_msg_id = int(parts[3])
        except Exception:
            return await query.answer("Invalid data", show_alert=True)

        targets = await get_user_targets(user_id)
        if not targets:
            return await query.answer("Add a target first.", show_alert=True)

        buttons = []
        for t in targets:
            title = (t.get("title") or "Unknown")[:25]
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"{title}",
                        callback_data=f"fwd:to:{t['chat_id']}:{source_chat_id}:{last_msg_id}",
                    )
                ]
            )
        if len(targets) > 1:
            buttons.append(
                [
                    InlineKeyboardButton(
                        "Send to All",
                        callback_data=f"fwd:all:{source_chat_id}:{last_msg_id}",
                    )
                ]
            )
        buttons.append([InlineKeyboardButton("Cancel", callback_data="src:cancel")])
        await query.message.edit_text(
            "**Quick Forward**\n\n"
            "One-time forward (no Job). Select target:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return await query.answer()


@Client.on_callback_query(filters.regex(r"^fwd:"))
async def forward_callbacks(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    from core.access import can_access_bot
    if not await can_access_bot(user_id):
        return await query.answer("Not allowed", show_alert=True)

    data = query.data

    if data == "fwd:cancel":
        await query.message.edit_text("Cancelled.")
        return await query.answer()

    if FORWARDING.get(user_id):
        return await query.answer("Already running. Send cancel first.", show_alert=True)

    if data.startswith("fwd:to:"):
        parts = data.split(":")
        try:
            target_chat_id = int(parts[2])
            source_chat_id = int(parts[3]) if parts[3].lstrip("-").isdigit() else parts[3]
            last_msg_id = int(parts[4])
        except Exception:
            return await query.answer("Invalid data", show_alert=True)

        target = await get_target(user_id, target_chat_id)
        if not target:
            return await query.answer("Target not found", show_alert=True)

        await query.message.edit_text(
            f"**Quick Forward – Skip**\n\n"
            f"**Target:** {target.get('title')}\n"
            f"**Source:** `{source_chat_id}`\n"
            f"**Last Message ID:** `{last_msg_id}`\n\n"
            f"How many messages to skip from the start?\n"
            f"Send a number (example: `0` or `100`)."
        )
        set_state(client, "forward_state", user_id, {
            "action": "waiting_skip",
            "target_chat_id": target_chat_id,
            "source_chat_id": source_chat_id,
            "last_msg_id": last_msg_id,
        })
        return await query.answer()

    if data.startswith("fwd:all:"):
        parts = data.split(":")
        try:
            source_chat_id = int(parts[2]) if parts[2].lstrip("-").isdigit() else parts[2]
            last_msg_id = int(parts[3])
        except Exception:
            return await query.answer("Invalid data", show_alert=True)

        await query.message.edit_text(
            f"**Quick Forward – Skip (All Targets)**\n\n"
            f"How many messages to skip?\n"
            f"Send a number (`0` = no skip):"
        )
        set_state(client, "forward_state", user_id, {
            "action": "waiting_skip_all",
            "source_chat_id": source_chat_id,
            "last_msg_id": last_msg_id,
        })
        return await query.answer()


@Client.on_message(filters.private & filters.regex(r"(?i)^cancel$"))
async def cancel_forward(client: Client, message: Message):
    user_id = message.from_user.id
    from core.access import can_access_bot
    if not await can_access_bot(user_id):
        return
    if FORWARDING.get(user_id):
        CANCEL_FLAGS[user_id] = True
        # text_input_handlers also clears states and replies

