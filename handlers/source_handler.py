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
PAUSE_FLAGS = {}
QF_FILTERS = {}  # user_id -> filters snapshot
QF_PROGRESS = {}  # user_id -> live progress dict
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
    """Source resolved → method → executors → perm check → targets."""
    from core.chat_resolve import resolve_source_chat_id
    from handlers.keyboards import select_method_keyboard

    source_chat, err = await resolve_source_chat_id(client, user_id, source_chat_id)
    if not source_chat:
        msg = (
            "Cannot access source chat.\n\n"
            + str(err)
            + "\n\nManagement Bot does not need access.\n"
            "Join the source with a linked My Account, then send the link again\n"
            "or forward a message from the source."
        )
        return await message.reply(msg, parse_mode=None)

    if source_chat.type not in [ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP]:
        return await message.reply("Source must be a Channel or Group.")

    set_state(client, "job_create_state", user_id, {
        "step": "method",
        "source_chat_id": source_chat.id,
        "source_title": source_chat.title or "Unknown",
        "last_msg_id": last_msg_id,
        "selected_targets": [],
        "selected_accounts": [],
        "future_new_posts": False,
        "pre_index_target_duplicates": False,
        "skip": 0,
    })

    await message.reply(
        f"**Source:** {source_chat.title}\n"
        f"**ID:** `{source_chat.id}`\n"
        f"**Last Message ID:** `{last_msg_id}`\n\n"
        f"**Create Job – Choose method**\n"
        f"Next: pick bot/accounts (only those are permission-checked).",
        reply_markup=select_method_keyboard(),
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
        from core.op_filters import default_op_filters
        QF_FILTERS[user_id] = default_op_filters()

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




@Client.on_callback_query(filters.regex(r"^qf:"))
async def qf_controls(client: Client, query: CallbackQuery):
    """Quick Forward controls + filters — fully independent from Job filters."""
    user_id = query.from_user.id
    from core.access import can_access_bot
    if not await can_access_bot(user_id):
        return
    data = query.data or ""
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    from core.op_filters import (
        normalize_op_filters,
        ALL_MEDIA_TYPES,
        default_op_filters,
    )
    from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    def _ensure_qf_filters():
        if user_id not in QF_FILTERS or not isinstance(QF_FILTERS.get(user_id), dict):
            QF_FILTERS[user_id] = default_op_filters()
        else:
            QF_FILTERS[user_id] = normalize_op_filters(QF_FILTERS[user_id])
        return QF_FILTERS[user_id]

    def _qf_filters_text(f: dict) -> str:
        types = f.get("media_types") or []
        lines = [
            "**Quick Forward Filters**",
            "_Independent of Jobs and Target settings_",
            "",
            "**Message types:**",
        ]
        for mt in ALL_MEDIA_TYPES:
            mark = "ON" if mt in types else "off"
            lines.append(f"- `{mt}`: {mark}")
        lines.append("")
        lines.append(
            "Block list: **%s** · `%s` words"
            % ("ON" if f.get("block_enabled") else "OFF", len(f.get("block_words") or []))
        )
        lines.append(
            "Whitelist: **%s** · `%s` words"
            % ("ON" if f.get("whitelist_enabled") else "OFF", len(f.get("whitelist_words") or []))
        )
        return chr(10).join(lines)

    def _qf_filters_kb(f: dict) -> InlineKeyboardMarkup:
        types = set(f.get("media_types") or [])
        rows = []
        row = []
        for mt in ALL_MEDIA_TYPES:
            mark = "✅" if mt in types else "⬜"
            row.append(InlineKeyboardButton(
                f"{mark} {mt}", callback_data=f"qf:ft:mt:{mt}"
            ))
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        be = "ON" if f.get("block_enabled") else "OFF"
        we = "ON" if f.get("whitelist_enabled") else "OFF"
        rows.append([
            InlineKeyboardButton(f"Block {be}", callback_data="qf:ft:btog"),
            InlineKeyboardButton(f"White {we}", callback_data="qf:ft:wtog"),
        ])
        rows.append([
            InlineKeyboardButton("+ Block word", callback_data="qf:ft:badd"),
            InlineKeyboardButton("Block list", callback_data="qf:ft:bview"),
        ])
        rows.append([
            InlineKeyboardButton("+ White word", callback_data="qf:ft:wadd"),
            InlineKeyboardButton("White list", callback_data="qf:ft:wview"),
        ])
        rows.append([
            InlineKeyboardButton("Clear block", callback_data="qf:ft:bclr"),
            InlineKeyboardButton("Clear white", callback_data="qf:ft:wclr"),
        ])
        rows.append([InlineKeyboardButton("« Back", callback_data="qf:back")])
        return InlineKeyboardMarkup(rows)

    def _ready_kb() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Start", callback_data="qf:start")],
            [InlineKeyboardButton("🔍 Filters", callback_data="qf:filters")],
            [InlineKeyboardButton("❌ Cancel", callback_data="src:cancel")],
        ])

    # ── runtime controls ──
    def _fmt_qf_status() -> str:
        p = QF_PROGRESS.get(user_id) or {}
        st = p.get("status") or (
            "paused" if PAUSE_FLAGS.get(user_id)
            else ("running" if FORWARDING.get(user_id) else "idle")
        )
        title = p.get("title") or "—"
        cur = p.get("current_id") or "—"
        last = p.get("last_msg_id") or "—"
        skip = p.get("skip")
        stats = p.get("stats") or {}
        icon = {
            "running": "🟢 Running",
            "paused": "⏸ Paused",
            "cancelled": "⏹ Cancelled",
            "done": "✅ Done",
            "failed": "❌ Failed",
        }.get(st, st)
        lines = [
            f"**Quick Forward** — {icon}",
            "",
            f"Target: **{title}**",
            f"Cursor: `{cur}` / `{last}`",
        ]
        if skip is not None:
            lines.append(f"Skip: `{skip}`")
        lines.append("")
        lines.append(f"Fetched: `{stats.get('fetched', 0)}`")
        lines.append(f"Forwarded: `{stats.get('forwarded', 0)}`")
        lines.append(f"Skipped (filter): `{stats.get('skipped_filter', 0)}`")
        lines.append(f"Duplicates: `{stats.get('skipped_duplicate', 0)}`")
        lines.append(f"Errors: `{stats.get('errors', 0)}`")
        return chr(10).join(lines)

    def _qf_live_kb(status: str):
        if status in ("done", "cancelled", "failed"):
            return None
        from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⏸ Pause", callback_data="qf:pause"),
                InlineKeyboardButton("▶️ Resume", callback_data="qf:resume"),
            ],
            [
                InlineKeyboardButton("⏹ Cancel", callback_data="qf:cancel"),
                InlineKeyboardButton("🔄 Refresh", callback_data="qf:refresh"),
            ],
        ])

    if action == "pause":
        PAUSE_FLAGS[user_id] = True
        if user_id in QF_PROGRESS:
            QF_PROGRESS[user_id]["status"] = "paused"
        try:
            await query.message.edit_text(
                _fmt_qf_status(),
                reply_markup=_qf_live_kb("paused"),
            )
        except Exception:
            pass
        return await query.answer("Paused")
    if action == "resume":
        PAUSE_FLAGS[user_id] = False
        if user_id in QF_PROGRESS:
            QF_PROGRESS[user_id]["status"] = "running"
        try:
            await query.message.edit_text(
                _fmt_qf_status(),
                reply_markup=_qf_live_kb("running"),
            )
        except Exception:
            pass
        return await query.answer("Resumed")
    if action == "cancel":
        CANCEL_FLAGS[user_id] = True
        PAUSE_FLAGS[user_id] = False
        if user_id in QF_PROGRESS:
            QF_PROGRESS[user_id]["status"] = "cancelled"
        set_state(client, "forward_state", user_id, None)
        try:
            await query.message.edit_text(
                _fmt_qf_status(),
                reply_markup=None,
            )
        except Exception:
            pass
        return await query.answer("Cancelled")
    if action == "refresh":
        try:
            st = (QF_PROGRESS.get(user_id) or {}).get("status") or (
                "paused" if PAUSE_FLAGS.get(user_id)
                else ("running" if FORWARDING.get(user_id) else "idle")
            )
            await query.message.edit_text(
                _fmt_qf_status(),
                reply_markup=_qf_live_kb(st),
            )
        except Exception:
            pass
        return await query.answer("Refreshed")

    # ── filters screen ──
    if action == "filters":
        f = _ensure_qf_filters()
        await safe_edit(query, _qf_filters_text(f), _qf_filters_kb(f))
        return await safe_answer(query)

    # ── back to ready panel ──
    if action == "back":
        st = get_state(client, "forward_state", user_id) or {}
        skip = st.get("skip", 0)
        last = st.get("last_msg_id", 0)
        f = _ensure_qf_filters()
        types = ", ".join(f.get("media_types") or [])
        text = (
            "**Quick Forward — ready**" + chr(10) + chr(10)
            + f"Skip: `{skip}` · Last: `{last}`" + chr(10)
            + f"Filters: `{types}`" + chr(10)
            + f"Block: {'ON' if f.get('block_enabled') else 'OFF'} · "
            + f"White: {'ON' if f.get('whitelist_enabled') else 'OFF'}" + chr(10) + chr(10)
            + "Tap **Start** to begin."
        )
        await safe_edit(query, text, _ready_kb())
        return await safe_answer(query)

    # ── filter toggles / lists / add ──
    if action == "ft":
        f = _ensure_qf_filters()
        sub = parts[2] if len(parts) > 2 else ""
        if sub == "mt":
            mt = parts[3] if len(parts) > 3 else ""
            types = list(f.get("media_types") or [])
            if mt in types:
                types.remove(mt)
            else:
                types.append(mt)
            if not types:
                types = ["video", "document"]
            f["media_types"] = types
        elif sub == "btog":
            f["block_enabled"] = not bool(f.get("block_enabled"))
        elif sub == "wtog":
            f["whitelist_enabled"] = not bool(f.get("whitelist_enabled"))
        elif sub == "bclr":
            f["block_words"] = []
        elif sub == "wclr":
            f["whitelist_words"] = []
        elif sub == "bview":
            words = f.get("block_words") or []
            await query.answer(
                (", ".join(words)[:180] if words else "Block list empty"),
                show_alert=True,
            )
            return
        elif sub == "wview":
            words = f.get("whitelist_words") or []
            await query.answer(
                (", ".join(words)[:180] if words else "Whitelist empty"),
                show_alert=True,
            )
            return
        elif sub in ("badd", "wadd"):
            set_state(client, "qf_filter_state", user_id, {
                "kind": "block" if sub == "badd" else "white",
            })
            await safe_edit(
                query,
                "Send the word/phrase to add for **Quick Forward** filters.\n"
                "/cancel to abort.",
            )
            return await safe_answer(query)
        QF_FILTERS[user_id] = normalize_op_filters(f)
        f = QF_FILTERS[user_id]
        await safe_edit(query, _qf_filters_text(f), _qf_filters_kb(f))
        return await safe_answer(query)

    # ── start forwarding ──
    if action == "start":
        if FORWARDING.get(user_id):
            return await query.answer("Already running", show_alert=True)
        st = get_state(client, "forward_state", user_id) or {}
        if st.get("action") != "ready":
            return await query.answer(
                "Session expired. Start Quick Forward again.", show_alert=True
            )
        source_chat_id = st.get("source_chat_id")
        last_msg_id = int(st.get("last_msg_id") or 0)
        skip = int(st.get("skip") or 0)
        mode = st.get("mode") or "single"
        target_chat_id = st.get("target_chat_id")
        # Keep filters snapshot at start time
        op_snap = normalize_op_filters(_ensure_qf_filters())
        set_state(client, "forward_state", user_id, None)

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⏸ Pause", callback_data="qf:pause"),
                InlineKeyboardButton("▶️ Resume", callback_data="qf:resume"),
            ],
            [
                InlineKeyboardButton("⏹ Cancel", callback_data="qf:cancel"),
                InlineKeyboardButton("🔄 Refresh", callback_data="qf:refresh"),
            ],
        ])
        try:
            await query.message.edit_text(
                f"**Quick Forward starting…**\nSkip: `{skip}` · Last: `{last_msg_id}`",
                reply_markup=kb,
            )
        except Exception:
            pass
        await query.answer("Starting…")

        from core.forwarder import forward_messages
        from database import get_target, get_user_targets

        FORWARDING[user_id] = True
        CANCEL_FLAGS[user_id] = False
        PAUSE_FLAGS[user_id] = False
        msg = query.message
        try:
            if mode == "single":
                target = await get_target(user_id, int(target_chat_id))
                if not target:
                    await msg.edit_text("Target not found.")
                    return
                await forward_messages(
                    client=client,
                    user_id=user_id,
                    source_chat_id=source_chat_id,
                    target=target,
                    last_msg_id=last_msg_id,
                    skip=skip,
                    progress_message=msg,
                    cancel_flag=CANCEL_FLAGS,
                    pause_flag=PAUSE_FLAGS,
                    auto_progress=False,
                    op_filters=op_snap,
                )
            else:
                targets = await get_user_targets(user_id)
                for idx, target in enumerate(targets or [], 1):
                    if CANCEL_FLAGS.get(user_id):
                        await msg.edit_text("Cancelled.")
                        break
                    try:
                        await msg.edit_text(
                            f"**Target {idx}/{len(targets)}:** {target.get('title')}",
                            reply_markup=kb,
                        )
                    except Exception:
                        pass
                    await forward_messages(
                        client=client,
                        user_id=user_id,
                        source_chat_id=source_chat_id,
                        target=target,
                        last_msg_id=last_msg_id,
                        skip=skip,
                        progress_message=msg,
                        cancel_flag=CANCEL_FLAGS,
                        pause_flag=PAUSE_FLAGS,
                        auto_progress=False,
                        op_filters=op_snap,
                    )
                if not CANCEL_FLAGS.get(user_id):
                    try:
                        await msg.edit_text(
                            f"Done for {len(targets or [])} target(s)."
                        )
                    except Exception:
                        pass
        except Exception as e:
            try:
                await msg.edit_text(f"Error: {type(e).__name__}")
            except Exception:
                pass
        finally:
            FORWARDING[user_id] = False
            CANCEL_FLAGS[user_id] = False
            PAUSE_FLAGS[user_id] = False
        return

    return await safe_answer(query)
