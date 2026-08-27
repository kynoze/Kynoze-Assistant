"""
CNL forward engine — ported from original post.py
Kurigram/Pyrogram | async CnlDatabase

- Management bot does NOT forward.
- forward_via user_bot / user_account clients do the work.
- forward_tag ON  → pure forward()
- forward_tag OFF → full processing (caption/filters/buttons)

Caption priority:
1. custom_caption (HTML template with {caption})
2. add_caption + caption_position (start / end / end_with_gap)
3. Original text with replacements / remove_links / remove_old_caption

Anti-dupe: media file_unique_id only (pure text never hashed).
Albums: buffer ALBUM_WAIT_SECONDS then send_media_group.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple, Union

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import (
    AccessTokenExpired, AccessTokenInvalid, AuthKeyUnregistered,
    ChatWriteForbidden, FloodWait, MessageIdInvalid, PeerIdInvalid,
    RPCError, SessionRevoked, UserDeactivated,
)
from pyrogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaAnimation, InputMediaAudio, InputMediaDocument,
    InputMediaPhoto, InputMediaVideo, Message,
)

from core.cnl.clean import clean_file_name, remove_links_and_usernames
from core.cnl.constants import ALBUM_WAIT_SECONDS, FORWARD_CONCURRENCY
from core.cnl.db import get_cnl

logger = logging.getLogger(__name__)

_album_buffers: Dict[Tuple[int, str, int], List[Message]] = defaultdict(list)
_album_tasks: Dict[Tuple[int, str, int], asyncio.Task] = {}
_album_lock = asyncio.Lock()
_forward_sem = asyncio.Semaphore(FORWARD_CONCURRENCY)


# ── replacements / filters ─────────────────────────────────────────────────

def replace_whole_word(text: str, old: str, new: str) -> str:
    if not old:
        return text
    pattern = r"(?<![@\w])" + re.escape(old) + r"(?!\w)"
    try:
        return re.sub(pattern, new, text, flags=re.IGNORECASE)
    except re.error:
        return text.replace(old, new)


def apply_replacements(text: Optional[str], replacements: list) -> Optional[str]:
    if not text or not replacements:
        return text
    result = text
    for item in replacements:
        if isinstance(item, dict):
            old = item.get("from") or item.get("old") or ""
            new = item.get("to") if "to" in item else item.get("new", "")
            is_regex = bool(item.get("is_regex", False))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            old, new, is_regex = item[0], item[1], False
        else:
            continue
        if not old:
            continue
        try:
            if is_regex:
                result = re.sub(str(old), str(new), result, flags=re.IGNORECASE)
            else:
                result = replace_whole_word(result, str(old), str(new))
        except re.error as e:
            logger.warning("Invalid regex replacement %r: %s", old, e)
            result = replace_whole_word(result, str(old), str(new))
    return result


def process_original_text(original: Optional[str], rule: dict) -> Optional[str]:
    if rule.get("remove_old_caption"):
        return None
    processed = apply_replacements(original, rule.get("replacements") or [])
    if rule.get("remove_links") and processed:
        processed = remove_links_and_usernames(processed)
        processed = clean_file_name(processed) if processed else processed
    return processed


def build_final_caption_and_entities(message: Message, rule: dict) -> Tuple[Optional[str], None]:
    original = message.caption or message.text

    template = (rule.get("custom_caption") or "").strip()
    if template:
        processed = process_original_text(original, rule)
        caption_value = processed if processed is not None else ""
        return template.replace("{caption}", caption_value), None

    processed = process_original_text(original, rule)
    if processed is None:
        processed = ""

    add = (rule.get("add_caption") or "").strip()
    pos = (rule.get("caption_position") or "end").lower()
    if add:
        if not processed:
            return add, None
        if pos == "start":
            return f"{add}\n{processed}", None
        if pos == "end_with_gap":
            return f"{processed}\n\n{add}", None
        return f"{processed}\n{add}", None
    return processed or None, None


def _plain_word_match(word: str, text: str) -> bool:
    if not word:
        return False
    pattern = r"(?<![@\w])" + re.escape(word) + r"(?!\w)"
    try:
        return re.search(pattern, text, flags=re.IGNORECASE) is not None
    except re.error:
        return word.lower() in text.lower()


def _match_word_list(text: Optional[str], words: list) -> bool:
    if not text or not words:
        return False
    for item in words:
        if isinstance(item, str):
            if _plain_word_match(item, text):
                return True
        elif isinstance(item, dict):
            pattern = item.get("pattern") or item.get("word") or ""
            is_regex = bool(item.get("is_regex", False))
            if not pattern:
                continue
            try:
                if is_regex:
                    if re.search(pattern, text, flags=re.IGNORECASE):
                        return True
                else:
                    if _plain_word_match(pattern, text):
                        return True
            except re.error:
                if _plain_word_match(pattern, text):
                    return True
    return False


def is_blocked(text: Optional[str], block_words: list) -> bool:
    return _match_word_list(text, block_words)


def is_whitelisted(text: Optional[str], whitelist_words: list) -> bool:
    if not whitelist_words:
        return True
    return _match_word_list(text, whitelist_words)


def build_keyboard(buttons) -> Optional[InlineKeyboardMarkup]:
    if not buttons:
        return None
    try:
        rows = []
        for row in buttons:
            btns = []
            for b in row:
                if isinstance(b, dict) and b.get("text") and b.get("url"):
                    btns.append(InlineKeyboardButton(b["text"], url=b["url"]))
            if btns:
                rows.append(btns)
        return InlineKeyboardMarkup(rows) if rows else None
    except Exception:
        return None


def get_message_type(message: Message) -> str:
    if message.photo: return "photo"
    if message.video: return "video"
    if message.document: return "document"
    if message.animation: return "animation"
    if message.audio: return "audio"
    if message.voice: return "voice"
    if message.sticker: return "sticker"
    if message.poll: return "poll"
    if message.contact: return "contact"
    if message.location: return "location"
    if message.venue: return "venue"
    if message.text: return "text"
    return "other"


def is_type_allowed(msg_type: str, allowed_types: list) -> bool:
    if not allowed_types or "all" in allowed_types:
        return True
    return msg_type in allowed_types


def get_content_hash(message: Message) -> Optional[str]:
    """Only media with file_unique_id — pure text is never treated as duplicate."""
    for attr in ("photo", "video", "document", "animation", "audio", "voice", "sticker"):
        media = getattr(message, attr, None)
        if media and getattr(media, "file_unique_id", None):
            return f"file:{media.file_unique_id}"
    return None


def get_album_hash(messages: List[Message]) -> Optional[str]:
    ids = sorted(h for m in messages if (h := get_content_hash(m)))
    if not ids:
        return None
    return "album:" + hashlib.sha256("|".join(ids).encode()).hexdigest()


# ── send helpers ────────────────────────────────────────────────────────────

def _to_input_media(message: Message, caption: Optional[str] = None):
    if message.photo:
        return InputMediaPhoto(message.photo.file_id, caption=caption)
    if message.video:
        return InputMediaVideo(message.video.file_id, caption=caption)
    if message.document:
        return InputMediaDocument(message.document.file_id, caption=caption)
    if message.animation:
        return InputMediaAnimation(message.animation.file_id, caption=caption)
    if message.audio:
        return InputMediaAudio(message.audio.file_id, caption=caption)
    return None


async def _send_single(
    client: Client, message: Message, rule: dict, target_id: int,
    source_id: int, owner_id: int,
):
    cnl = await get_cnl(owner_id)
    delay = float(rule.get("delay") or 0)
    if delay > 0:
        await asyncio.sleep(min(delay, 300))

    caption, _ = build_final_caption_and_entities(message, rule)
    markup = build_keyboard(rule.get("buttons"))

    try:
        if message.photo:
            await client.send_photo(target_id, message.photo.file_id, caption=caption, reply_markup=markup)
        elif message.video:
            await client.send_video(target_id, message.video.file_id, caption=caption, reply_markup=markup)
        elif message.document:
            await client.send_document(target_id, message.document.file_id, caption=caption, reply_markup=markup)
        elif message.animation:
            await client.send_animation(target_id, message.animation.file_id, caption=caption, reply_markup=markup)
        elif message.audio:
            await client.send_audio(target_id, message.audio.file_id, caption=caption, reply_markup=markup)
        elif message.voice:
            await client.send_voice(target_id, message.voice.file_id, caption=caption, reply_markup=markup)
        elif message.sticker:
            await client.send_sticker(target_id, message.sticker.file_id)
        elif message.text:
            await client.send_message(target_id, caption or message.text, reply_markup=markup)
        else:
            await client.copy_message(target_id, message.chat.id, message.id)
        if cnl:
            await cnl.record_forward_success(source_id, target_id, owner_id)
    except Exception:
        if cnl:
            await cnl.record_failed(source_id, target_id)
        raise


async def _send_album(
    client: Client, messages: List[Message], rule: dict, target_id: int,
    source_id: int, owner_id: int,
):
    cnl = await get_cnl(owner_id)
    delay = float(rule.get("delay") or 0)
    if delay > 0:
        await asyncio.sleep(min(delay, 300))

    if rule.get("forward_tag"):
        ids = [m.id for m in messages]
        await client.forward_messages(target_id, messages[0].chat.id, ids)
        if cnl:
            await cnl.record_forward_success(source_id, target_id, owner_id)
        return

    media = []
    for i, msg in enumerate(sorted(messages, key=lambda m: m.id)):
        cap = None
        if i == 0:
            cap, _ = build_final_caption_and_entities(msg, rule)
        item = _to_input_media(msg, caption=cap)
        if item:
            media.append(item)
    if not media:
        for msg in messages:
            await _send_single(client, msg, rule, target_id, source_id, owner_id)
        return
    try:
        await client.send_media_group(target_id, media)
        if cnl:
            await cnl.record_forward_success(source_id, target_id, owner_id)
    except Exception:
        if cnl:
            await cnl.record_failed(source_id, target_id)
        raise


# ── main entry ──────────────────────────────────────────────────────────────

async def process_and_forward(client: Client, message: Message, rule: dict, owner_id: int):
    """Process one incoming message against one rule using the given client."""
    cnl = await get_cnl(owner_id)
    if not cnl:
        return

    source_id = message.chat.id
    target_id = int(rule["target_chat_id"])
    msg_type = get_message_type(message)

    if not is_type_allowed(msg_type, rule.get("allowed_types") or ["all"]):
        return

    text = message.caption or message.text
    if is_blocked(text, rule.get("block_words") or []):
        await cnl.record_blocked(source_id, target_id)
        return
    if not is_whitelisted(text, rule.get("whitelist_words") or []):
        await cnl.record_blocked(source_id, target_id)
        return

    # album path
    if message.media_group_id:
        await _handle_album_message(client, message, rule, owner_id)
        return

    # anti-dupe (media only)
    if rule.get("anti_dupe"):
        h = get_content_hash(message)
        if h:
            claimed = await cnl.try_claim_hash_for_owner(
                owner_id, h, target_id, source_id, message.id
            )
            if not claimed:
                await cnl.record_duplicate_skipped(source_id, target_id)
                return

    if not await cnl.try_consume_quota(owner_id):
        logger.info("CNL quota exhausted owner=%s", owner_id)
        return

    async def _do():
        if rule.get("forward_tag"):
            delay = float(rule.get("delay") or 0)
            if delay > 0:
                await asyncio.sleep(min(delay, 300))
            await message.forward(target_id)
            await cnl.record_forward_success(source_id, target_id, owner_id)
        else:
            await _send_single(client, message, rule, target_id, source_id, owner_id)

    async with _forward_sem:
        for attempt in range(3):
            try:
                await _do()
                return
            except (AuthKeyUnregistered, SessionRevoked, UserDeactivated,
                    AccessTokenInvalid, AccessTokenExpired) as e:
                logger.warning("CNL auth dead owner=%s via=%s: %s", owner_id, rule.get("forward_via"), type(e).__name__)
                await cnl.record_failed(source_id, target_id)
                return
            except FloodWait as e:
                if attempt < 2:
                    await asyncio.sleep(e.value + 1)
                    continue
                await cnl.record_failed(source_id, target_id)
                return
            except (ChatWriteForbidden, PeerIdInvalid, MessageIdInvalid) as e:
                logger.error("CNL cannot write %s: %s", target_id, e)
                await cnl.record_failed(source_id, target_id)
                return
            except RPCError as e:
                logger.error("CNL RPCError → %s: %s", target_id, e)
                await cnl.record_failed(source_id, target_id)
                return
            except Exception:
                logger.exception("CNL unexpected → %s", target_id)
                await cnl.record_failed(source_id, target_id)
                return


async def _handle_album_message(client: Client, message: Message, rule: dict, owner_id: int):
    key = (owner_id, str(message.media_group_id), int(rule["target_chat_id"]))
    async with _album_lock:
        _album_buffers[key].append(message)
        if key in _album_tasks:
            _album_tasks[key].cancel()

        async def _flush():
            await asyncio.sleep(ALBUM_WAIT_SECONDS)
            async with _album_lock:
                msgs = sorted(_album_buffers.pop(key, []), key=lambda m: m.id)
                _album_tasks.pop(key, None)
            if not msgs:
                return
            cnl = await get_cnl(owner_id)
            if not cnl:
                return
            source_id = msgs[0].chat.id
            target_id = int(rule["target_chat_id"])
            text = msgs[0].caption or msgs[0].text
            if is_blocked(text, rule.get("block_words") or []):
                await cnl.record_blocked(source_id, target_id)
                return
            if not is_whitelisted(text, rule.get("whitelist_words") or []):
                await cnl.record_blocked(source_id, target_id)
                return
            if rule.get("anti_dupe"):
                h = get_album_hash(msgs)
                if h:
                    claimed = await cnl.try_claim_hash_for_owner(
                        owner_id, h, target_id, source_id, msgs[0].id
                    )
                    if not claimed:
                        await cnl.record_duplicate_skipped(source_id, target_id)
                        return
            if not await cnl.try_consume_quota(owner_id):
                return
            try:
                async with _forward_sem:
                    await _send_album(client, msgs, rule, target_id, source_id, owner_id)
            except Exception:
                logger.exception("CNL album send fail")

        _album_tasks[key] = asyncio.create_task(_flush())


async def process_global_copy(client: Client, message: Message, owner_id: int):
    cnl = await get_cnl(owner_id)
    if not cnl:
        return
    gc = await cnl.get_global_copy(owner_id)
    if not gc or not gc.get("enabled") or not gc.get("target_chat_id"):
        return
    rule = {
        "target_chat_id": int(gc["target_chat_id"]),
        "owner_id": owner_id,
        "enabled": True,
        "allowed_types": gc.get("allowed_types") or ["all"],
        "block_words": gc.get("block_words") or [],
        "whitelist_words": gc.get("whitelist_words") or [],
        "replacements": gc.get("replacements") or [],
        "add_caption": gc.get("add_caption"),
        "caption_position": gc.get("caption_position") or "end",
        "custom_caption": gc.get("custom_caption"),
        "remove_old_caption": gc.get("remove_old_caption", False),
        "remove_links": gc.get("remove_links", False),
        "buttons": gc.get("buttons"),
        "delay": gc.get("delay") or 0,
        "anti_dupe": gc.get("anti_dupe", False),
        "forward_tag": gc.get("forward_tag", False),
        "forward_via": "user_account",
    }
    await process_and_forward(client, message, rule, owner_id)
