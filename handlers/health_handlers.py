
"""Runtime Health UI."""
from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from core.access import can_access_bot
from core.health import build_user_health, list_dead_items
from handlers.ui import safe_answer, safe_edit


@Client.on_callback_query(filters.regex(r"^health:"))
async def health_callbacks(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    if not await can_access_bot(user_id):
        return await query.answer("Not allowed", show_alert=True)
    data = query.data
    if data in ("health:home", "health:refresh"):
        text = await build_user_health(user_id)
        dead = await list_dead_items(user_id)
        if dead:
            text += "\n\n**⚠️ Needs attention**\n"
            for d in dead[:8]:
                text += f"• {d.get('feature')}: `{d.get('title')}` — {str(d.get('reason') or '')[:60]}\n"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="health:refresh")],
            [InlineKeyboardButton("« Dashboard", callback_data="dash:home")],
        ])
        await safe_edit(query, text[:3900], kb)
        return await safe_answer(query)
    await safe_answer(query)
