from pyrogram import Client, filters
from pyrogram.types import CallbackQuery

from database import (
    DEFAULT_TARGET_SETTINGS,
    get_setting,
    get_target,
    is_admin,
    update_full_settings,
    update_target_settings,
)
from handlers.keyboards import (
    view_config_keyboard,
    back_settings_keyboard,
    caption_keyboard,
    confirm_clear_keyboard,
    confirm_reset_all_keyboard,
    list_manage_keyboard,
    list_pick_keyboard,
    media_types_keyboard,
    reset_settings_keyboard,
    settings_category_keyboard,
    simple_back_keyboard,
    target_settings_keyboard,
)
from core.state import set_state
from handlers.ui import (
    FEATURE_CATEGORY,
    HR,
    PAGE_SIZE,
    TOGGLE_CATEGORY,
    on_off,
    paginate,
    safe_answer,
    safe_edit,
)
import copy
import logging

logger = logging.getLogger(__name__)

ALL_MEDIA = [
    ("photo", "Photo"),
    ("video", "Video"),
    ("document", "Document"),
    ("audio", "Audio"),
    ("sticker", "Sticker"),
    ("animation", "Animation"),
    ("voice", "Voice"),
    ("video_note", "Video Note"),
    ("text", "Text"),
]

CAT_TITLE = {
    "content": "🎨 Content",
    "filters": "🔍 Filters",
    "forward": "⚡ Forwarding",
    "future": "🆕 Future Posts",
}


def _mark(v: bool) -> str:
    return "✅" if v else "❌"


def _list_items(feature: str, settings: dict) -> list:
    if feature == "block_words":
        return list(settings.get("block_words") or [])
    if feature == "whitelist":
        return list(settings.get("whitelist") or [])
    if feature == "replacements":
        return list(settings.get("replacements") or [])
    if feature == "inline_buttons":
        return list(settings.get("inline_buttons") or [])
    return []


def _format_item(feature: str, item) -> str:
    if feature == "replacements" and isinstance(item, dict):
        return f"{item.get('from', '')} → {item.get('to', '') or '(remove)'}"
    if feature == "inline_buttons" and isinstance(item, list):
        return " | ".join(b.get("text", "") for b in item)
    return str(item)


def _labels(feature: str, settings: dict) -> list:
    return [_format_item(feature, it) for it in _list_items(feature, settings)]


def _list_text(feature: str, settings: dict, page: int = 0) -> str:
    items = _list_items(feature, settings)
    titles = {
        "block_words": "🚫 Block Words",
        "whitelist": "✅ Whitelist",
        "replacements": "🔄 Replacements",
        "inline_buttons": "🔘 Inline Buttons",
    }
    title = titles.get(feature, feature)
    extra = ""
    if feature == "whitelist":
        extra = f"\nWhitelist Mode: {_mark(settings.get('whitelist_mode'))}"
    if feature == "replacements":
        extra = f"\nReplacement: {_mark(settings.get('replace_enabled'))}"

    if not items:
        return f"**{title}**\n\nTotal: 0\n\nNo entries yet.{extra}"

    slice_, page, total_pages = paginate(items, page)
    start = page * PAGE_SIZE
    body = "\n".join(
        f"{start + i + 1}. {_format_item(feature, it)}"
        for i, it in enumerate(slice_)
    )
    page_note = f"  (page {page + 1}/{total_pages})" if total_pages > 1 else ""
    return f"**{title}**\n\nTotal: {len(items)}{page_note}\n\n{body}{extra}"


def _config_text(target: dict) -> str:
    s = target.get("settings") or {}
    media = set(s.get("media_types") or [])
    media_lines = "\n".join(
        f"{label} {_mark(key in media)}" for key, label in ALL_MEDIA
    )
    reps = s.get("replacements") or []
    blocks = s.get("block_words") or []
    white = s.get("whitelist") or []
    buttons = s.get("inline_buttons") or []
    return (
        f"**🎯 Target Configuration**\n\n"
        f"**Name:** {target.get('title')}\n"
        f"**Chat ID:** `{target.get('chat_id')}`\n\n"
        f"Caption: {_mark(s.get('caption_enabled'))}\n"
        f"Rich Message: {_mark(s.get('rich_message_enabled'))}\n"
        f"Template: `{s.get('caption_template') or '{caption}'}`\n"
        f"Replacement: {_mark(s.get('replace_enabled'))} ({len(reps)} rules)\n"
        f"Block Words: {_mark(s.get('block_words_enabled', True))} ({len(blocks)} stored)\n"
        f"Whitelist: {_mark(s.get('whitelist_mode'))} ({len(white)})\n"
        f"Remove Links: {_mark(s.get('remove_links'))}\n"
        f"Inline Buttons: {_mark(s.get('inline_buttons_enabled', True))} ({len(buttons)} rows stored)\n\n"
        f"**Media Types:**\n{media_lines}\n\n"
        f"Forward Tag: {_mark(s.get('forward_tag'))}\n"
        f"Delay: `{s.get('delay', 1.0)}s`\n"
        f"Anti-Duplicate: {_mark(s.get('anti_duplicate', True))}\n"
        f"Future New Posts: {_mark(s.get('future_new_posts'))}"
    )

def _caption_text(target: dict) -> str:
    s = target.get("settings") or {}
    status = on_off(bool(s.get("caption_enabled")))
    rich = on_off(bool(s.get("rich_message_enabled")))
    template = s.get("caption_template") or "{caption}"
    return (
        f"**📝 Caption Settings**\n\n"
        f"Caption: {status}\n"
        f"Rich Message: {rich}\n\n"
        f"Template:\n`{template}`\n\n"
        "Rich Message (text posts only): headings, lists, tables via HTML.\n"
        "Media captions still use classic HTML (1024 limit)."
    )


def _hub_text(target: dict) -> str:
    return (
        f"**⚙️ Target Settings**\n\n"
        f"**Name:** {target.get('title', 'Unknown')}\n"
        f"**Chat ID:** `{target.get('chat_id')}`\n"
        f"{HR}\n"
        "Choose a category:"
    )


def _category_text(target: dict, category: str) -> str:
    title = CAT_TITLE.get(category, category)
    extra = ""
    if category == "future":
        extra = (
            "\n\nMonitoring interval is set **per Job**.\n"
            "Open Jobs → job → **Monitor** to change how often new posts are checked."
        )
    return (
        f"**{title}**\n\n"
        f"Target: {target.get('title')}\n"
        f"{HR}{extra}"
    )


async def _show_target_settings(query: CallbackQuery, target: dict):
    await safe_edit(query, _hub_text(target), target_settings_keyboard(target))


async def _show_category(query: CallbackQuery, target: dict, category: str):
    await safe_edit(
        query,
        _category_text(target, category),
        settings_category_keyboard(target, category),
    )


async def _show_list(query: CallbackQuery, target: dict, feature: str, page: int = 0):
    s = target.get("settings") or {}
    labels = _labels(feature, s)
    slice_, page, _total = paginate(labels, page)
    await safe_edit(
        query,
        _list_text(feature, s, page),
        list_manage_keyboard(target["chat_id"], feature, len(labels), page),
    )


@Client.on_callback_query(filters.regex(r"^st:"))
async def settings_callbacks(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    from core.access import can_access_bot
    if not await can_access_bot(user_id):
        return await safe_answer(query, "Not allowed", True)

    data = query.data
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "hub":
        chat_id = int(parts[2])
        set_state(client, "settings_state", user_id, None)
        target = await get_target(user_id, chat_id)
        if not target:
            return await safe_answer(query, "Target not found", True)
        await _show_target_settings(query, target)
        return await safe_answer(query)

    if action == "cat":
        chat_id = int(parts[2])
        category = parts[3] if len(parts) > 3 else "content"
        set_state(client, "settings_state", user_id, None)
        target = await get_target(user_id, chat_id)
        if not target:
            return await safe_answer(query, "Target not found", True)
        await _show_category(query, target, category)
        return await safe_answer(query)

    if action == "toggle":
        chat_id = int(parts[2])
        key = parts[3]
        target = await get_target(user_id, chat_id)
        if not target:
            return await safe_answer(query, "Target not found", True)
        default_on = {
            "block_words_enabled": True,
            "inline_buttons_enabled": True,
            "anti_duplicate": True,
        }.get(key, False)
        current = bool(get_setting(target, key, default_on))
        await update_target_settings(user_id, chat_id, {key: not current})
        target = await get_target(user_id, chat_id)
        cat = TOGGLE_CATEGORY.get(key, "content")
        await _show_category(query, target, cat)
        await safe_answer(query, f"{key.replace('_', ' ').title()} → {'ON' if not current else 'OFF'}")
        return

    if action == "view":
        chat_id = int(parts[2])
        target = await get_target(user_id, chat_id)
        if not target:
            return await safe_answer(query, "Target not found", True)
        await safe_edit(query, _config_text(target), view_config_keyboard(chat_id))
        return await safe_answer(query)

    if action == "reset":
        chat_id = int(parts[2])
        await safe_edit(
            query,
            "**♻️ Reset Settings**\n\nChoose what to reset. Reset All needs confirmation.",
            reset_settings_keyboard(chat_id),
        )
        return await safe_answer(query)

    if action == "resetdo":
        chat_id = int(parts[2])
        group = parts[3]
        target = await get_target(user_id, chat_id)
        if not target:
            return await safe_answer(query, "Target not found", True)
        s = dict(target.get("settings") or {})
        if group == "caption":
            s["caption_enabled"] = DEFAULT_TARGET_SETTINGS["caption_enabled"]
            s["rich_message_enabled"] = DEFAULT_TARGET_SETTINGS.get("rich_message_enabled", False)
            s["caption_template"] = DEFAULT_TARGET_SETTINGS["caption_template"]
        elif group == "filters":
            s["block_words"] = []
            s["block_words_enabled"] = True
            s["whitelist"] = []
            s["whitelist_mode"] = False
            s["remove_links"] = False
            s["media_types"] = list(DEFAULT_TARGET_SETTINGS["media_types"])
            s["anti_duplicate"] = True
        elif group == "buttons":
            s["inline_buttons"] = []
            s["inline_buttons_enabled"] = True
        elif group == "replacements":
            s["replacements"] = []
            s["replace_enabled"] = False
        await update_full_settings(user_id, chat_id, s)
        await safe_answer(query, "Reset done", True)
        target = await get_target(user_id, chat_id)
        await _show_target_settings(query, target)
        return

    if action == "resetall":
        chat_id = int(parts[2])
        await safe_edit(
            query,
            "**⚠️ Reset ALL settings?**\n\nThis restores every target setting to defaults. Cannot be undone.",
            confirm_reset_all_keyboard(chat_id),
        )
        return await safe_answer(query)

    if action == "resetallok":
        chat_id = int(parts[2])
        await update_full_settings(user_id, chat_id, copy.deepcopy(DEFAULT_TARGET_SETTINGS))
        await safe_answer(query, "All settings reset", True)
        target = await get_target(user_id, chat_id)
        await _show_target_settings(query, target)
        return

    if action in ("capedit", "capprev", "capreset", "capclear") or (
        action == "menu" and len(parts) > 3 and parts[3] == "caption_template"
    ):
        chat_id = int(parts[2])
        target = await get_target(user_id, chat_id)
        if not target:
            return await safe_answer(query, "Target not found", True)
        if action == "menu":
            await safe_edit(query, _caption_text(target), caption_keyboard(chat_id))
            return await safe_answer(query)
        if action == "capreset":
            await update_target_settings(user_id, chat_id, {
                "caption_template": DEFAULT_TARGET_SETTINGS["caption_template"],
            })
            target = await get_target(user_id, chat_id)
            await safe_edit(query, _caption_text(target), caption_keyboard(chat_id))
            return await safe_answer(query, "Template reset")
        if action == "capclear":
            await update_target_settings(user_id, chat_id, {
                "caption_template": "{caption}",
                "caption_enabled": False,
            })
            target = await get_target(user_id, chat_id)
            await safe_edit(query, _caption_text(target), caption_keyboard(chat_id))
            return await safe_answer(query, "Caption disabled")
        if action == "capedit":
            await safe_edit(
                query,
                "**✏️ Edit Caption Template**\n\n"
                "Send the new template. Use `{caption}` as the original text.\n\n"
                "With **Rich Message ON** (text posts), HTML works e.g. "
                "`<h1>{caption}</h1>`, `<b>bold</b>`.\n"
                "Lists/tables apply to pure text forwards only.\n\n"
                "Type /cancel to go back.",
                simple_back_keyboard(chat_id),
            )
            set_state(client, "settings_state", user_id, {
                "action": "set_caption_template",
                "chat_id": chat_id,
                "category": "content",
            })
            return await safe_answer(query)
        if action == "capprev":
            await safe_edit(
                query,
                "**👁 Caption Preview**\n\n"
                "Send a sample original caption (it will NOT be forwarded).\n\n"
                "Example: `Movie Name 1080p`\n\n"
                "Type /cancel to go back.",
                simple_back_keyboard(chat_id),
            )
            set_state(client, "settings_state", user_id, {
                "action": "caption_preview",
                "chat_id": chat_id,
                "category": "content",
            })
            return await safe_answer(query)

    if action == "media":
        chat_id = int(parts[2])
        media_key = parts[3]
        target = await get_target(user_id, chat_id)
        if not target:
            return await safe_answer(query, "Target not found", True)
        current_list = list(get_setting(target, "media_types", []))
        if media_key in current_list:
            current_list.remove(media_key)
        else:
            current_list.append(media_key)
        await update_target_settings(user_id, chat_id, {"media_types": current_list})
        target = await get_target(user_id, chat_id)
        await safe_edit(
            query,
            "**🎞 Media Types Filter**\n\nSelect which media types should be forwarded.\nEmpty list = allow all.",
            media_types_keyboard(target),
        )
        return await safe_answer(query, f"{media_key} updated")

    if action == "menup":
        chat_id = int(parts[2])
        feature = parts[3]
        try:
            page = int(parts[4])
        except Exception:
            page = 0
        target = await get_target(user_id, chat_id)
        if not target:
            return await safe_answer(query, "Target not found", True)
        await _show_list(query, target, feature, page)
        return await safe_answer(query)

    if action == "pickp":
        chat_id = int(parts[2])
        feature = parts[3]
        mode = parts[4]
        try:
            page = int(parts[5])
        except Exception:
            page = 0
        target = await get_target(user_id, chat_id)
        if not target:
            return await safe_answer(query, "Target not found", True)
        labels = _labels(feature, target.get("settings") or {})
        title = "Select item to edit:" if mode == "e" else "Select item to delete:"
        await safe_edit(
            query,
            f"**{title}**",
            list_pick_keyboard(chat_id, feature, labels, mode, page),
        )
        return await safe_answer(query)

    if action == "menu":
        chat_id = int(parts[2])
        feature = parts[3]
        target = await get_target(user_id, chat_id)
        if not target:
            return await safe_answer(query, "Target not found", True)
        s = target.get("settings") or {}

        if feature == "media_types":
            await safe_edit(
                query,
                "**🎞 Media Types Filter**\n\nSelect which media types should be forwarded.\nEmpty list = allow all.",
                media_types_keyboard(target),
            )
            return await safe_answer(query)

        if feature == "delay":
            current = s.get("delay", 1.0)
            await safe_edit(
                query,
                f"**⏱ Delay**\n\nCurrent: **{current} seconds**\n\n"
                "Send a new delay in seconds (example: `1.5` or `3`).\n"
                "Type /cancel to go back.",
                simple_back_keyboard(chat_id),
            )
            set_state(client, "settings_state", user_id, {
                "action": "set_delay",
                "chat_id": chat_id,
                "category": "forward",
            })
            return await safe_answer(query)

        if feature in ("block_words", "whitelist", "replacements", "inline_buttons"):
            await _show_list(query, target, feature, 0)
            return await safe_answer(query)

        await safe_answer(query, "Unknown menu", True)
        return

    if action in ("add", "edit", "del", "delall", "delallok", "item"):
        chat_id = int(parts[2])
        feature = parts[3]
        target = await get_target(user_id, chat_id)
        if not target:
            return await safe_answer(query, "Target not found", True)
        s = target.get("settings") or {}
        labels = _labels(feature, s)
        category = FEATURE_CATEGORY.get(feature, "content")

        if action == "add":
            prompts = {
                "block_words": (
                    "**➕ Add Block Words**\n\n"
                    "Send words separated by comma or new lines.\n"
                    "Example: `cam, sample`\n\nDuplicates are reported, not ignored."
                ),
                "whitelist": (
                    "**➕ Add Whitelist Words**\n\n"
                    "Send words separated by comma or new lines.\n"
                    "Example: `1080p, WEB-DL`"
                ),
                "replacements": (
                    "**➕ Add Replacement Rules**\n\n"
                    "One rule per line:\n`old => new`\n\n"
                    "Invalid lines are listed. Valid ones still save."
                ),
                "inline_buttons": (
                    "**➕ Add Inline Buttons**\n\n"
                    "`Button Text - https://example.com`\n"
                    "Same row: `||`   New row: new line."
                ),
            }
            await safe_edit(
                query,
                prompts[feature] + "\n\nType /cancel to go back.",
                simple_back_keyboard(chat_id),
            )
            set_state(client, "settings_state", user_id, {
                "action": f"add_{feature}",
                "chat_id": chat_id,
                "category": category,
            })
            return await safe_answer(query)

        if action in ("edit", "del"):
            if not labels:
                return await safe_answer(query, "List is empty", True)
            mode = "e" if action == "edit" else "d"
            title = "Select item to edit:" if action == "edit" else "Select item to delete:"
            await safe_edit(
                query,
                f"**{title}**",
                list_pick_keyboard(chat_id, feature, labels, mode, 0),
            )
            return await safe_answer(query)

        if action == "delall":
            if not labels:
                return await safe_answer(query, "List is empty", True)
            names = {
                "block_words": "Block Words",
                "whitelist": "Whitelist",
                "replacements": "Replacements",
                "inline_buttons": "Inline Buttons",
            }
            await safe_edit(
                query,
                f"**⚠️ Delete All {names.get(feature, feature)}?**\n\n"
                f"This will remove **{len(labels)}** item(s).",
                confirm_clear_keyboard(chat_id, feature),
            )
            return await safe_answer(query)

        if action == "delallok":
            field = {
                "block_words": "block_words",
                "whitelist": "whitelist",
                "replacements": "replacements",
                "inline_buttons": "inline_buttons",
            }[feature]
            await update_target_settings(user_id, chat_id, {field: []})
            target = await get_target(user_id, chat_id)
            await _show_list(query, target, feature, 0)
            return await safe_answer(query, "Cleared")

        if action == "item":
            idx = int(parts[4])
            mode = parts[5]
            if idx < 0 or idx >= len(labels):
                return await safe_answer(query, "Item not found", True)
            if mode == "d":
                if feature == "block_words":
                    items = list(s.get("block_words") or [])
                    items.pop(idx)
                    await update_target_settings(user_id, chat_id, {"block_words": items})
                elif feature == "whitelist":
                    items = list(s.get("whitelist") or [])
                    items.pop(idx)
                    await update_target_settings(user_id, chat_id, {"whitelist": items})
                elif feature == "replacements":
                    items = list(s.get("replacements") or [])
                    items.pop(idx)
                    await update_target_settings(user_id, chat_id, {"replacements": items})
                elif feature == "inline_buttons":
                    items = list(s.get("inline_buttons") or [])
                    items.pop(idx)
                    await update_target_settings(user_id, chat_id, {"inline_buttons": items})
                target = await get_target(user_id, chat_id)
                await _show_list(query, target, feature, 0)
                return await safe_answer(query, "Deleted")

            prompts = {
                "block_words": "Send the new word to replace this item.",
                "whitelist": "Send the new whitelist word.",
                "replacements": "Send one rule: `old => new`",
                "inline_buttons": "Send one row: `Text - https://url` (use `||` for extra buttons on the same row).",
            }
            await safe_edit(
                query,
                f"**✏️ Edit item {idx + 1}**\n\nCurrent: `{labels[idx]}`\n\n{prompts[feature]}\n\nType /cancel to go back.",
                simple_back_keyboard(chat_id),
            )
            set_state(client, "settings_state", user_id, {
                "action": f"edit_{feature}",
                "chat_id": chat_id,
                "index": idx,
                "category": category,
            })
            return await safe_answer(query)

    await safe_answer(query)
