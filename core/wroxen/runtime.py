"""Run Wroxen bot clients: event-driven auto-index + group search.

Each unique bot_token used by active Wroxen configs gets one Client.
No userbot. Handlers attached when the client starts.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Set

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import CallbackQuery, Message

from config import Config
from core.security import decrypt_session
from core.wroxen import db as wxdb
from core.wroxen.indexer import index_message_to_db
from core.wroxen.search import (
    RESULTS_PER_PAGE,
    build_results_text,
    get_cached,
    pagination_keyboard,
    recall_query,
    remember_query,
    set_cached,
)

logger = logging.getLogger(__name__)

# bot_id -> Client
_CLIENTS: Dict[str, Client] = {}
# bot_id -> owner_user_id (management user who owns configs)
_BOT_OWNER: Dict[str, int] = {}
# target_chat_id -> list of (owner_user_id, wroxen_id, bot_id)
_TARGET_MAP: Dict[int, List[tuple]] = {}
# source_chat_id -> list of (owner_user_id, wroxen_id, bot_id)
_SOURCE_MAP: Dict[int, List[tuple]] = {}
_STARTED: Set[str] = set()
_lock = asyncio.Lock()


def _rebuild_maps(configs: List[Dict[str, Any]]) -> None:
    _TARGET_MAP.clear()
    _SOURCE_MAP.clear()
    for c in configs:
        if not c.get("enabled", True):
            continue
        wid = c["wroxen_id"]
        owner = c["user_id"]
        bot_id = c["bot_id"]
        src = int(c["source_chat_id"])
        tgt = int(c["target_chat_id"])
        _SOURCE_MAP.setdefault(src, []).append((owner, wid, bot_id))
        _TARGET_MAP.setdefault(tgt, []).append((owner, wid, bot_id))


async def refresh_routing() -> None:
    """Reload active configs from main DB and ensure bot clients running."""
    from database import list_all_enabled_wroxen

    configs = await list_all_enabled_wroxen()
    _rebuild_maps(configs)

    needed_bots: Dict[str, Dict] = {}
    for c in configs:
        if not c.get("enabled", True):
            continue
        needed_bots[c["bot_id"]] = c

    # stop unused
    for bot_id in list(_CLIENTS.keys()):
        if bot_id not in needed_bots:
            await stop_bot(bot_id)

    for bot_id, cfg in needed_bots.items():
        if bot_id not in _CLIENTS:
            await start_bot_for_config(cfg)


async def start_bot_for_config(cfg: Dict[str, Any]) -> Optional[Client]:
    bot_id = cfg["bot_id"]
    owner = cfg["user_id"]
    if bot_id in _CLIENTS:
        _BOT_OWNER[bot_id] = owner
        return _CLIENTS[bot_id]

    from database import get_bot

    bot_doc = await get_bot(owner, bot_id)
    if not bot_doc:
        logger.error("Wroxen bot doc missing %s", bot_id)
        return None

    token = bot_doc.get("bot_token")
    try:
        token = decrypt_session(token)
    except Exception:
        logger.exception("decrypt wroxen bot token")
        return None
    if not token:
        return None

    client = Client(
        name=f"wroxen_{bot_id}",
        api_id=Config.API_ID,
        api_hash=Config.API_HASH,
        bot_token=token,
        in_memory=True,
        parse_mode=ParseMode.HTML,
    )

    # Auto-index: media in source chats
    @client.on_message(
        (filters.channel | filters.group)
        & (filters.video | filters.document | filters.photo | filters.audio | filters.animation)
    )
    async def _auto_index(c: Client, message: Message):
        try:
            chat_id = message.chat.id
            entries = _SOURCE_MAP.get(chat_id) or []
            for owner_uid, wroxen_id, bid in entries:
                if bid != bot_id:
                    continue
                # ensure DB
                from core.db_resolver import resolve_feature_db
                resolved = await resolve_feature_db(owner_uid, "wroxen")
                uri = resolved.get("uri")

                if not uri:
                    continue
                ok, _ = await wxdb.ensure_connected(owner_uid, uri)
                if not ok:
                    continue
                result = await index_message_to_db(owner_uid, wroxen_id, chat_id, message)
                if result == "saved":
                    logger.info("Wroxen auto-index %s msg %s", wroxen_id, message.id)
        except Exception:
            logger.exception("wroxen auto-index")

    # Search: text in target groups
    @client.on_message(filters.group & filters.text & ~filters.command(["start"]))
    async def _search(c: Client, message: Message):
        if not message.from_user:
            return
        query = (message.text or "").strip()
        if not query or query.startswith(("/", ".", "!", ",")):
            return
        chat_id = message.chat.id
        entries = _TARGET_MAP.get(chat_id) or []
        # Prefer config whose bot_id matches this client
        matched = [(o, w, b) for o, w, b in entries if b == bot_id]
        if not matched:
            return
        owner_uid, wroxen_id, _ = matched[0]
        try:
            from core.db_resolver import resolve_feature_db
            resolved = await resolve_feature_db(owner_uid, "wroxen")
            uri = resolved.get("uri")

            if not uri:
                return
            ok, _ = await wxdb.ensure_connected(owner_uid, uri)
            if not ok:
                return

            cached = get_cached(wroxen_id, query)
            if cached:
                results, total = cached
            else:
                data = await wxdb.search_media(owner_uid, wroxen_id, query, limit=200)
                results = data["results"]
                total = data["total"]
                set_cached(wroxen_id, query, results, total)

            if not results:
                return

            remember_query(query)
            pages = max(1, (len(results) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE)
            # build page 1 text
            from core.wroxen.search import format_result_line
            from html import escape

            text = (
                f"<b>🔎 Results for:</b> <code>{escape(query)}</code>\n"
                f"📄 Page 1/{pages} • Total: {total}\n\n"
            )
            for i, movie in enumerate(results[:RESULTS_PER_PAGE], start=1):
                text += format_result_line(i, movie)

            kb = pagination_keyboard(wroxen_id, query, 1, pages, message.from_user.id)
            await message.reply_text(
                text,
                reply_markup=kb,
                disable_web_page_preview=True,
            )
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except Exception:
            logger.exception("wroxen search")

    @client.on_callback_query(filters.regex(r"^wxpage:"))
    async def _page(c: Client, cq: CallbackQuery):
        try:
            # wxpage:wroxen_id:page:owner_id:qhash
            parts = cq.data.split(":")
            wroxen_id = parts[1]
            page = int(parts[2])
            owner_id = int(parts[3])
            qhash = parts[4]
        except Exception:
            return await cq.answer("Invalid", show_alert=True)

        if cq.from_user and cq.from_user.id != owner_id:
            return await cq.answer("Not for you!", show_alert=True)

        query = recall_query(qhash)
        if not query:
            return await cq.answer("Cache expired — search again", show_alert=True)

        # find owner from target map for this wroxen
        owner_uid = None
        for entries in _TARGET_MAP.values():
            for o, w, b in entries:
                if w == wroxen_id and b == bot_id:
                    owner_uid = o
                    break
            if owner_uid:
                break
        if owner_uid is None:
            return await cq.answer("Config not found", show_alert=True)

        cached = get_cached(wroxen_id, query)
        if not cached:
            return await cq.answer("Cache expired — search again", show_alert=True)
        results, total = cached
        pages = max(1, (len(results) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE)
        page = max(1, min(page, pages))
        start = (page - 1) * RESULTS_PER_PAGE
        end = start + RESULTS_PER_PAGE
        from core.wroxen.search import format_result_line
        from html import escape

        text = (
            f"<b>🔎 Results for:</b> <code>{escape(query)}</code>\n"
            f"📄 Page {page}/{pages} • Total: {total}\n\n"
        )
        for i, movie in enumerate(results[start:end], start=start + 1):
            text += format_result_line(i, movie)
        kb = pagination_keyboard(wroxen_id, query, page, pages, owner_id)
        try:
            await cq.message.edit_text(
                text, reply_markup=kb, disable_web_page_preview=True
            )
            await cq.answer()
        except MessageNotModified:
            await cq.answer()
        except Exception as e:
            await cq.answer(str(e)[:100], show_alert=True)

    try:
        await client.start()
        _CLIENTS[bot_id] = client
        _BOT_OWNER[bot_id] = owner
        _STARTED.add(bot_id)
        me = await client.get_me()
        logger.info("Wroxen bot started: @%s (%s)", me.username, bot_id)
        return client
    except Exception:
        logger.exception("Failed to start Wroxen bot %s", bot_id)
        try:
            from core.log_chat import report_user_auto_stop
            await report_user_auto_stop(
                owner,
                feature="Wroxen Search",
                title=cfg.get("name") or bot_id,
                reason="Wroxen bot client failed to start. Search/auto-index is stopped.",
                error="client.start failed — see bot logs",
            )
        except Exception:
            pass
        return None


async def stop_bot(bot_id: str) -> None:
    client = _CLIENTS.pop(bot_id, None)
    _BOT_OWNER.pop(bot_id, None)
    _STARTED.discard(bot_id)
    if client:
        try:
            await client.stop()
        except Exception:
            pass


async def stop_all() -> None:
    for bot_id in list(_CLIENTS.keys()):
        await stop_bot(bot_id)


async def get_client(bot_id: str) -> Optional[Client]:
    return _CLIENTS.get(bot_id)
