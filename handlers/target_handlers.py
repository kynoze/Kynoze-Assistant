from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery
from pyrogram.enums import ChatType

from database import (
    ensure_user,
    is_admin,
    add_target,
    get_user_targets,
    get_target,
    delete_target,
    get_entity_stats,
    get_duplicate_count,
)
from handlers.keyboards import (
    targets_list_keyboard,
    target_detail_keyboard,
    confirm_delete_keyboard,
)
from core.state import set_state, get_state
from handlers.ui import (
    HR,
    on_off,
    paginate,
    safe_answer,
    safe_edit,
    with_pager,
)
import logging

logger = logging.getLogger(__name__)

def _active_bots_only(bots):
    out = []
    for b in bots or []:
        st = (b.get("status") or "active").lower()
        if st in ("disabled", "inactive", "error"):
            continue
        out.append(b)
    return out


def _executor_pick_keyboard(bots, accounts, selected_bots, selected_accs):
    from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    from handlers.ui import format_account_label, format_bot_label, active_accounts_only
    rows = []
    for b in _active_bots_only(bots)[:20]:
        bid = str(b.get("bot_id") or "")
        mark = "✅" if bid in selected_bots else "⬜"
        name = format_bot_label(b, short=True)[:32] if "format_bot_label" in dir() else (
            b.get("bot_username") or b.get("name") or bid
        )
        try:
            from handlers.ui import format_bot_label as _fb
            name = _fb(b, short=True)[:32]
        except Exception:
            name = (b.get("bot_username") or b.get("name") or bid)[:32]
        rows.append([InlineKeyboardButton(
            f"{mark} 🤖 {name}", callback_data=f"tg:exec:tog:bot:{bid}"
        )])
    for a in active_accounts_only(accounts)[:20]:
        aid = str(a.get("account_id") or "")
        mark = "✅" if aid in selected_accs else "⬜"
        try:
            from handlers.ui import format_account_label as _fa
            name = _fa(a, short=True)[:32]
        except Exception:
            name = (a.get("name") or a.get("phone") or aid)[:32]
        rows.append([InlineKeyboardButton(
            f"{mark} 👤 {name}", callback_data=f"tg:exec:tog:acc:{aid}"
        )])
    if not rows:
        rows.append([InlineKeyboardButton(
            "No active bots/accounts", callback_data="tg:list"
        )])
    rows.append([InlineKeyboardButton(
        "✅ Done — check permissions", callback_data="tg:exec:done"
    )])
    rows.append([InlineKeyboardButton("« Cancel", callback_data="tg:list")])
    return InlineKeyboardMarkup(rows)




def _target_detail_text(target: dict, user_id: int) -> str:
    s = target.get("settings") or {}
    chat_id = target["chat_id"]
    uname = target.get("username")
    uname_s = f"@{uname}" if uname else "-"
    reps = s.get("replacements") or []
    blocks = s.get("block_words") or []
    white = s.get("whitelist") or []
    buttons = s.get("inline_buttons") or []
    return (
        f"**🎯 {target.get('title') or 'Target'}**\n\n"
        f"{HR}\n"
        f"**Name:** {target.get('title')}\n"
        f"**Chat ID:** `{chat_id}`\n"
        f"**Username:** {uname_s}\n"
        f"{HR}\n"
        f"📝 Caption: {on_off(bool(s.get('caption_enabled')))}\n"
        f"🔄 Replacements: `{len(reps)}`\n"
        f"🚫 Block words: `{len(blocks)}`\n"
        f"✅ Whitelist: `{len(white)}`  mode {on_off(bool(s.get('whitelist_mode')))}\n"
        f"🔗 Remove links: {on_off(bool(s.get('remove_links')))}\n"
        f"🔘 Inline buttons: `{len(buttons)}` rows\n"
        f"⏱ Delay: `{s.get('delay', 1.0)}s`\n"
        f"♻️ Anti-duplicate: {on_off(bool(s.get('anti_duplicate', True)))}\n"
        f"🏷 Forward tag: {on_off(bool(s.get('forward_tag')))}\n"
        f"🆕 Future posts: {on_off(bool(s.get('future_new_posts')))}"
    )


async def _target_stats_text(user_id: int, target: dict) -> str:
    chat_id = target["chat_id"]
    try:
        dups = await get_duplicate_count(user_id, chat_id)
    except Exception:
        dups = 0
    st = await get_entity_stats(user_id, "target", str(chat_id)) or {}
    return (
        f"**📊 Target Statistics**\n\n"
        f"**{target.get('title')}**\n"
        f"{HR}\n"
        f"📤 Forwarded: `{int(st.get('forwarded') or 0):,}`\n"
        f"♻️ Duplicates: `{dups:,}`\n"
        f"🚫 Blocked: `{int(st.get('blocked') or 0):,}`\n"
        f"❌ Errors: `{int(st.get('errors') or 0):,}`"
    )


async def show_targets_list(client: Client, query: CallbackQuery, page: int = 0):
    user_id = query.from_user.id
    try:
        from database import get_visible_targets
        targets = await get_visible_targets(user_id)
    except ImportError:
        targets = await get_user_targets(user_id)
    if not targets:
        text = (
            "**🎯 My Targets**\n\n"
            "You have no targets yet.\n"
            "Click **Add Target** to add your first channel/group."
        )
        await safe_edit(query, text, targets_list_keyboard([]))
        return await safe_answer(query)

    slice_, page, total_pages = paginate(targets, page)
    lines = [f"**🎯 My Targets** ({len(targets)})\n"]
    for t in slice_:
        uname = f" @{t['username']}" if t.get("username") else ""
        lines.append(f"🎯 **{t.get('title') or 'Unknown'}**{uname}")
    kb = with_pager(targets_list_keyboard(slice_), "tg:listp:", page, total_pages)
    await safe_edit(query, "\n".join(lines), kb)
    await safe_answer(query)


async def cmd_targets_internal(client: Client, query: CallbackQuery):
    await show_targets_list(client, query, 0)


@Client.on_message(filters.private & filters.command("targets"))
async def cmd_targets(client: Client, message: Message):
    from core.access import can_access_bot
    if not await can_access_bot(message.from_user.id):
        return await message.reply("❌ You are not allowed to use this bot.")

    await ensure_user(message.from_user.id)
    targets = await get_user_targets(message.from_user.id)
    if not targets:
        text = (
            "**🎯 My Targets**\n\n"
            "You have no targets yet.\n"
            "Click **Add Target** to add your first channel/group."
        )
    else:
        text = f"**🎯 My Targets** ({len(targets)})\n\nSelect a target:"
    await message.reply(text, reply_markup=targets_list_keyboard(targets[:8]))


@Client.on_callback_query(filters.regex(r"^tg:"))
async def target_callbacks(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    from core.access import can_access_bot
    if not await can_access_bot(user_id):
        return await safe_answer(query, "Not allowed", True)

    data = query.data
    await ensure_user(user_id)

    if data == "tg:list":
        await show_targets_list(client, query, 0)
        return

    if data.startswith("tg:listp:"):
        try:
            page = int(data.split(":")[2])
        except Exception:
            page = 0
        await show_targets_list(client, query, page)
        return

    if data == "tg:add":
        from database import get_user_bots, get_user_accounts
        bots = await get_user_bots(user_id)
        from handlers.ui import active_accounts_only
        accs = active_accounts_only(await get_user_accounts(user_id))
        if not bots and not accs:
            return await query.answer(
                "❌ Add at least one Bot or User Account before adding a Target Chat.",
                show_alert=True,
            )
        await safe_edit(
            query,
            "**➕ Add New Target**\n\n"
            "Send the **Channel / Group ID** or **Username**.\n\n"
            "Management Bot does **not** need to be admin.\n"
            "After the chat ID, you will select a **Bot** or **Account** to verify permissions.\n\n"
            "Example:\n`-1001234567890`  or  `@mychannel`\n\n"
            "Type /cancel to cancel.",
        )
        set_state(client, "target_add_state", user_id, {"step": "await_chat"})
        return await safe_answer(query)


    if data.startswith("tg:exec:tog:bot:") or data.startswith("tg:exec:tog:acc:"):
        st = get_state(client, "target_add_state", user_id) or {}
        if not isinstance(st, dict) or st.get("step") != "pick_executor":
            return await query.answer("Session expired — start Add Target again", show_alert=True)
        parts = data.split(":")
        # tg:exec:tog:bot:ID or tg:exec:tog:acc:ID
        kind = parts[3] if len(parts) > 3 else ""
        eid = parts[4] if len(parts) > 4 else ""
        sel_bots = list(st.get("selected_bots") or [])
        sel_accs = list(st.get("selected_accounts") or [])
        if kind == "bot":
            if eid in sel_bots:
                sel_bots.remove(eid)
            else:
                sel_bots.append(eid)
        elif kind == "acc":
            if eid in sel_accs:
                sel_accs.remove(eid)
            else:
                sel_accs.append(eid)
        st["selected_bots"] = sel_bots
        st["selected_accounts"] = sel_accs
        set_state(client, "target_add_state", user_id, st)
        from database import get_user_bots, get_user_accounts
        bots = await get_user_bots(user_id)
        accs = await get_user_accounts(user_id)
        try:
            await query.message.edit_reply_markup(
                _executor_pick_keyboard(bots, accs, sel_bots, sel_accs)
            )
        except Exception:
            pass
        n = len(sel_bots) + len(sel_accs)
        return await safe_answer(query, f"Selected: {n}")

    if data == "tg:exec:done":
        st = get_state(client, "target_add_state", user_id) or {}
        if not isinstance(st, dict) or st.get("step") != "pick_executor":
            return await query.answer("Session expired — start Add Target again", show_alert=True)
        chat_id = st.get("chat_id")
        username = st.get("username")
        title = st.get("title") or str(chat_id or username)
        sel_bots = list(st.get("selected_bots") or [])
        sel_accs = list(st.get("selected_accounts") or [])
        if not sel_bots and not sel_accs:
            return await query.answer("Select at least one bot or account.", show_alert=True)
        from core.permissions import verify_target_executor
        from database import add_target, get_user_targets, get_account, get_bot
        from core.job_worker import get_user_client, get_bot_client

        # Resolve peer using ONLY selected executors (first success wins for title/id)
        ref = chat_id if chat_id else username
        resolved_id = chat_id
        resolved_title = title
        resolved_username = username
        for aid in sel_accs:
            acc = await get_account(user_id, str(aid))
            if not acc:
                continue
            uc = await get_user_client(acc)
            if not uc:
                continue
            try:
                r = ref
                if isinstance(r, str) and r.lstrip("-").isdigit():
                    r = int(r)
                ch = await uc.get_chat(r)
                resolved_id = int(ch.id)
                resolved_title = ch.title or resolved_title
                resolved_username = getattr(ch, "username", None) or resolved_username
                break
            except Exception:
                try:
                    async for d in uc.get_dialogs(limit=200):
                        c = getattr(d, "chat", None)
                        if c and chat_id and int(c.id) == int(chat_id):
                            resolved_id = int(c.id)
                            resolved_title = c.title or resolved_title
                            resolved_username = getattr(c, "username", None) or resolved_username
                            break
                except Exception:
                    pass
        # Always try selected bots for real title (not only when id is missing)
        need_title = (
            not resolved_title
            or str(resolved_title).startswith("Chat ")
            or (resolved_id is not None and str(resolved_title) == str(resolved_id))
            or str(resolved_title).lstrip("-").isdigit()
        )
        if sel_bots and (resolved_id is None or need_title):
            for bid in sel_bots:
                bot = await get_bot(user_id, str(bid))
                if not bot:
                    continue
                bc = await get_bot_client(bot)
                if not bc:
                    continue
                try:
                    r = ref if ref is not None else resolved_id
                    if isinstance(r, str) and r.lstrip("-").isdigit():
                        r = int(r)
                    ch = await bc.get_chat(r)
                    resolved_id = int(ch.id)
                    if getattr(ch, "title", None):
                        resolved_title = ch.title
                    resolved_username = getattr(ch, "username", None) or resolved_username
                    if resolved_title and not str(resolved_title).startswith("Chat "):
                        break
                except Exception:
                    continue

        if resolved_id is None:
            return await query.answer(
                "Selected bot/account could not resolve this chat. "
                "Join the chat with that account, or use @username / forward.",
                show_alert=True,
            )

        fails = []
        ok_names = []
        for bid in sel_bots:
            err = await verify_target_executor(user_id, int(resolved_id), bot_id=str(bid))
            if err:
                fails.append("bot %s: %s" % (bid, err))
            else:
                ok_names.append("bot:%s" % bid)
        for aid in sel_accs:
            err = await verify_target_executor(user_id, int(resolved_id), account_id=str(aid))
            if err:
                fails.append("acc %s: %s" % (aid, err))
            else:
                ok_names.append("acc:%s" % aid)
        if fails:
            from database import get_user_bots, get_user_accounts
            bots = await get_user_bots(user_id)
            accs = await get_user_accounts(user_id)
            text = (
                "**Permission check failed** for `%s`" % resolved_id
                + chr(10) + chr(10)
                + chr(10).join(fails[:8])
                + chr(10) + chr(10)
                + "Only selected bots/accounts were checked."
            )
            await safe_edit(
                query,
                text,
                _executor_pick_keyboard(bots, accs, sel_bots, sel_accs),
            )
            return await safe_answer(query, "Permission failed", True)
        result = await add_target(
            user_id=user_id,
            chat_id=int(resolved_id),
            title=resolved_title,
            username=resolved_username,
        )
        set_state(client, "target_add_state", user_id, None)
        if result is None:
            return await query.answer("Already added", show_alert=True)
        verified = ", ".join(ok_names[:6])
        if len(ok_names) > 6:
            verified += " +%s" % (len(ok_names) - 6)
        await safe_edit(
            query,
            "✅ **Target Added**" + chr(10) + chr(10)
            + "**Name:** %s" % resolved_title + chr(10)
            + "**ID:** `%s`" % resolved_id + chr(10)
            + "**Verified via:** %s" % verified,
            targets_list_keyboard(await get_user_targets(user_id)),
        )
        return await safe_answer(query)

    # legacy single-click (old messages)
    if data.startswith("tg:exec:bot:") or data.startswith("tg:exec:acc:"):
        return await query.answer(
            "Use the new multi-select list (toggle + Done).", show_alert=True
        )

    if data.startswith("tg:open:"):
        chat_id = int(data.split(":")[2])
        target = await get_target(user_id, chat_id)
        if not target:
            return await safe_answer(query, "Target not found", True)
        await safe_edit(query, _target_detail_text(target, user_id), target_detail_keyboard(target))
        return await safe_answer(query)

    if data.startswith("tg:stats:"):
        chat_id = int(data.split(":")[2])
        target = await get_target(user_id, chat_id)
        if not target:
            return await safe_answer(query, "Target not found", True)
        from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        await safe_edit(
            query,
            await _target_stats_text(user_id, target),
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data=f"tg:stats:{chat_id}")],
                [InlineKeyboardButton("« Target", callback_data=f"tg:open:{chat_id}")],
            ]),
        )
        return await safe_answer(query)

    if data.startswith("tg:delete:"):
        chat_id = int(data.split(":")[2])
        target = await get_target(user_id, chat_id)
        if not target:
            return await safe_answer(query, "Target not found", True)
        await safe_edit(
            query,
            f"**⚠️ Delete Target?**\n\n"
            f"**{target.get('title')}** (`{chat_id}`)\n\n"
            f"This will also delete all duplicate records of this target.\n"
            f"This action cannot be undone.",
            confirm_delete_keyboard(chat_id),
        )
        return await safe_answer(query)

    if data.startswith("tg:confirm_delete:"):
        chat_id = int(data.split(":")[2])
        success = await delete_target(user_id, chat_id)
        if success:
            await safe_answer(query, "✅ Target deleted", True)
            await show_targets_list(client, query, 0)
        else:
            await safe_answer(query, "Failed to delete", True)
        return
