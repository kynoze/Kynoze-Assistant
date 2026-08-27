from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.enums import ChatType
from pyrogram.errors import (
    PhoneCodeInvalid, PhoneCodeExpired, SessionPasswordNeeded,
    PhoneNumberInvalid, FloodWait
)
import logging

from config import Config
from database import (
    is_admin, update_target_settings, get_target, get_user_targets,
    add_target, add_forward_bot, add_forward_account, update_account,
    get_user_accounts, get_user_bots, get_account,
)
from handlers.keyboards import (
    target_settings_keyboard, targets_list_keyboard,
    accounts_list_keyboard, bots_list_keyboard,
    account_settings_keyboard, caption_keyboard, list_manage_keyboard,
)
from core.state import clear_all_states, get_state, set_state
from core.errors import friendly_error
from core.caption import apply_caption_text
from core.validate import (
    format_result,
    parse_delay,
    parse_inline_buttons,
    parse_replacements,
    parse_word_list,
)

logger = logging.getLogger(__name__)


async def _cancel_flow(client: Client, message: Message):
    user_id = message.from_user.id
    await clear_all_states(client, user_id)
    from handlers.source_handler import FORWARDING, CANCEL_FLAGS
    if FORWARDING.get(user_id):
        CANCEL_FLAGS[user_id] = True
        await message.reply("Cancellation requested...")
    else:
        await message.reply("✅ Cancelled.")


@Client.on_message(filters.private & filters.command("cancel"))
async def cmd_cancel(client: Client, message: Message):
    from core.access import can_access_bot
    if not await can_access_bot(message.from_user.id):
        return
    await _cancel_flow(client, message)


def _unwrap_job(state):
    if isinstance(state, dict) and "step" in state:
        return state
    return state


@Client.on_message(filters.private & filters.text & ~filters.command(["start", "targets", "cancel"]))
async def handle_all_text_input(client: Client, message: Message):
    from handlers.cnl_handlers import handle_cnl_text
    if await handle_cnl_text(client, message):
        return

    from handlers.db_settings_handlers import handle_db_config_text
    if await handle_db_config_text(client, message):
        return

    from handlers.indexing_handlers import handle_index_text
    if await handle_index_text(client, message):
        return

    from handlers.wroxen_handlers import handle_wroxen_text
    if await handle_wroxen_text(client, message):
        return

    from handlers.delete_handlers import handle_delete_text
    if await handle_delete_text(client, message):
        return

    user_id = message.from_user.id
    from core.access import can_access_bot
    if not await can_access_bot(user_id):
        return

    text = message.text.strip()

    if text.lower() in ("/cancel", "cancel"):
        await _cancel_flow(client, message)
        return

    interval_state = get_state(client, "job_interval_state", user_id)
    if interval_state:
        from handlers.ui import (
            MIN_MONITOR_INTERVAL,
            MAX_MONITOR_INTERVAL,
            clamp_interval,
            fmt_interval,
        )
        from database import update_job, get_job
        try:
            raw = int(text)
        except ValueError:
            return await message.reply(
                "Send monitoring interval in **seconds**.\nExample: `15`"
            )
        if raw < MIN_MONITOR_INTERVAL or raw > MAX_MONITOR_INTERVAL:
            return await message.reply(
                f"❌ Interval must be between `{MIN_MONITOR_INTERVAL}` and "
                f"`{MAX_MONITOR_INTERVAL}` seconds.\n"
                "Smaller values can cause FloodWait."
            )
        seconds = clamp_interval(raw)
        job_id = interval_state.get("job_id")
        await update_job(user_id, job_id, {"monitor_interval_seconds": seconds})
        set_state(client, "job_interval_state", user_id, None)
        job = await get_job(user_id, job_id)
        from handlers.jobs_handlers import job_monitor_text
        from handlers.keyboards import job_monitor_keyboard
        await message.reply(
            f"✅ Monitoring interval updated.\n\n"
            f"Future Post Monitoring:\n⏱ Every {fmt_interval(seconds)}"
        )
        if job:
            await message.reply(
                job_monitor_text(job),
                reply_markup=job_monitor_keyboard(job),
            )
        return

    job_state = get_state(client, "job_create_state", user_id)
    if job_state and job_state.get("step") == "waiting_skip":
        try:
            skip = int(text)
            if skip < 0:
                return await message.reply("Skip cannot be negative.")
        except ValueError:
            return await message.reply("Send a number. Example: `0` or `200`")

        last = int(job_state.get("last_msg_id") or 0)
        if last and skip >= last:
            return await message.reply(f"Skip must be less than last message ID `{last}`.")

        job_state["skip"] = skip
        job_state["step"] = "confirm"
        set_state(client, "job_create_state", user_id, job_state)

        from handlers.jobs_handlers import job_confirm_keyboard, job_confirm_text
        await message.reply(
            job_confirm_text(job_state),
            reply_markup=job_confirm_keyboard(job_state),
        )
        return

    forward_state = get_state(client, "forward_state", user_id)
    if forward_state and forward_state.get("action") in ("waiting_skip", "waiting_skip_all"):
        try:
            skip = int(text)
            if skip < 0:
                return await message.reply("Skip cannot be negative.")
        except ValueError:
            return await message.reply("Send a number. Example: `0` or `100`")

        source_chat_id = forward_state["source_chat_id"]
        last_msg_id = int(forward_state["last_msg_id"])
        if skip >= last_msg_id:
            return await message.reply(
                f"Skip must be less than last message id `{last_msg_id}`."
            )

        set_state(client, "forward_state", user_id, None)

        from handlers.source_handler import FORWARDING, CANCEL_FLAGS
        from core.forwarder import forward_messages

        if forward_state["action"] == "waiting_skip":
            target = await get_target(user_id, forward_state["target_chat_id"])
            if not target:
                return await message.reply("Target not found.")

            msg = await message.reply(
                f"**Quick Forward starting**\n\n"
                f"Target: {target.get('title')}\n"
                f"Skip: `{skip}`  Last: `{last_msg_id}`\n\n"
                f"Send `cancel` to stop."
            )
            FORWARDING[user_id] = True
            CANCEL_FLAGS[user_id] = False
            try:
                await forward_messages(
                    client=client,
                    user_id=user_id,
                    source_chat_id=source_chat_id,
                    target=target,
                    last_msg_id=last_msg_id,
                    skip=skip,
                    progress_message=msg,
                    cancel_flag=CANCEL_FLAGS,
                )
            except Exception as e:
                await msg.edit_text(friendly_error("quick forward (single target)", e))
            finally:
                FORWARDING[user_id] = False
                CANCEL_FLAGS[user_id] = False
            return

        targets = await get_user_targets(user_id)
        if not targets:
            return await message.reply("No targets found.")

        msg = await message.reply(
            f"**Quick Forward to ALL targets** ({len(targets)})\nSkip: `{skip}`"
        )
        FORWARDING[user_id] = True
        CANCEL_FLAGS[user_id] = False
        try:
            for idx, target in enumerate(targets, 1):
                if CANCEL_FLAGS.get(user_id):
                    await msg.edit_text("Cancelled.")
                    break
                await msg.edit_text(
                    f"**Target {idx}/{len(targets)}:** {target.get('title')}"
                )
                await forward_messages(
                    client=client,
                    user_id=user_id,
                    source_chat_id=source_chat_id,
                    target=target,
                    last_msg_id=last_msg_id,
                    skip=skip,
                    progress_message=msg,
                    cancel_flag=CANCEL_FLAGS,
                )
            if not CANCEL_FLAGS.get(user_id):
                await msg.edit_text(f"Done for {len(targets)} target(s).")
        except Exception as e:
            await msg.edit_text(friendly_error("quick forward (all targets)", e))
        finally:
            FORWARDING[user_id] = False
            CANCEL_FLAGS[user_id] = False
        return

    account_state = get_state(client, "account_add_state", user_id)
    if account_state:
        step = account_state.get("step")

        if step == "phone":
            phone = text.strip()
            if not phone.startswith("+") or not phone[1:].isdigit():
                return await message.reply(
                    "❌ Invalid phone number.\n"
                    "Please send in international format.\n"
                    "Example: `+919876543210`"
                )
            try:
                temp_client = Client(
                    name=f"login_{user_id}",
                    api_id=Config.API_ID,
                    api_hash=Config.API_HASH,
                    in_memory=True
                )
                await temp_client.connect()
                sent_code = await temp_client.send_code(phone)
                set_state(client, "account_add_state", user_id, {
                    "step": "otp",
                    "phone": phone,
                    "phone_code_hash": sent_code.phone_code_hash,
                    "temp_client": temp_client,
                })
                await message.reply(
                    f"**📱 Code Sent!**\n\n"
                    f"A login code has been sent to `{phone}`.\n\n"
                    f"Please send the **OTP** code now.\n\n"
                    f"Type /cancel to cancel."
                )
            except PhoneNumberInvalid:
                await message.reply("❌ Invalid phone number.")
            except FloodWait as e:
                await message.reply(f"⏳ FloodWait: Please wait `{e.value}` seconds.")
            except Exception as e:
                await message.reply(friendly_error("account login: send code", e))
            return

        if step == "otp":
            otp = text.strip().replace(" ", "")
            phone = account_state["phone"]
            phone_code_hash = account_state["phone_code_hash"]
            temp_client: Client = account_state["temp_client"]
            try:
                await temp_client.sign_in(
                    phone_number=phone,
                    phone_code_hash=phone_code_hash,
                    phone_code=otp
                )
                session_string = await temp_client.export_session_string()
                await temp_client.disconnect()
                from core.access import check_limit
                from database import get_user_accounts
                err = await check_limit(user_id, "accounts", len(await get_user_accounts(user_id)))
                if err:
                    set_state(client, "account_add_state", user_id, None)
                    return await message.reply(err)

                result = await add_forward_account(
                    user_id=user_id,
                    phone=phone,
                    session_string=session_string,
                    name=phone
                )
                set_state(client, "account_add_state", user_id, None)
                if result is None:
                    return await message.reply("⚠️ This phone number is already added.")
                await message.reply(
                    f"✅ **Account Added Successfully!**\n\n"
                    f"**Phone:** `{phone}`\n"
                    f"**Account ID:** `{result['account_id']}`\n\n"
                    f"You can now use this account for forwarding jobs."
                )
                accounts = await get_user_accounts(user_id)
                await message.reply(
                    f"**👤 My Accounts** ({len(accounts)})",
                    reply_markup=accounts_list_keyboard(accounts)
                )
            except PhoneCodeInvalid:
                await message.reply("❌ Invalid OTP. Please try again.")
            except PhoneCodeExpired:
                await message.reply("❌ OTP expired. Please start again with /cancel and add account.")
                set_state(client, "account_add_state", user_id, None)
                try:
                    await temp_client.disconnect()
                except Exception:
                    pass
            except SessionPasswordNeeded:
                set_state(client, "account_add_state", user_id, {
                    "step": "2fa",
                    "phone": phone,
                    "temp_client": temp_client
                })
                await message.reply(
                    "**🔐 Two-Step Verification Enabled**\n\n"
                    "Please send your **2FA password** now."
                )
            except Exception as e:
                await message.reply(friendly_error("account login: sign in", e))
                set_state(client, "account_add_state", user_id, None)
                try:
                    await temp_client.disconnect()
                except Exception:
                    pass
            return

        if step == "2fa":
            password = text.strip()
            temp_client: Client = account_state["temp_client"]
            phone = account_state["phone"]
            try:
                await temp_client.check_password(password)
                session_string = await temp_client.export_session_string()
                await temp_client.disconnect()
                from core.access import check_limit
                from database import get_user_accounts
                err = await check_limit(user_id, "accounts", len(await get_user_accounts(user_id)))
                if err:
                    set_state(client, "account_add_state", user_id, None)
                    return await message.reply(err)

                result = await add_forward_account(
                    user_id=user_id,
                    phone=phone,
                    session_string=session_string,
                    name=phone
                )
                set_state(client, "account_add_state", user_id, None)
                if result is None:
                    return await message.reply("⚠️ This phone number is already added.")
                await message.reply(
                    f"✅ **Account Added Successfully (with 2FA)!**\n\n"
                    f"**Phone:** `{phone}`\n"
                    f"**Account ID:** `{result['account_id']}`"
                )
                accounts = await get_user_accounts(user_id)
                await message.reply(
                    f"**👤 My Accounts** ({len(accounts)})",
                    reply_markup=accounts_list_keyboard(accounts)
                )
            except Exception as e:
                await message.reply(friendly_error("account login: 2FA", e) + "\n\nOr /cancel.")
            return

    add_state = get_state(client, "target_add_state", user_id)
    if add_state:
        try:
            if text.startswith("@"):
                chat = await client.get_chat(text)
            else:
                chat_id = int(text)
                chat = await client.get_chat(chat_id)

            if chat.type not in [ChatType.CHANNEL, ChatType.SUPERGROUP, ChatType.GROUP]:
                return await message.reply("❌ Only Channels and Groups are supported.")

            from core.access import check_limit
            from database import get_user_targets
            err = await check_limit(user_id, "targets", len(await get_user_targets(user_id)))
            if err:
                set_state(client, "target_add_state", user_id, None)
                return await message.reply(err)

            result = await add_target(
                user_id=user_id,
                chat_id=chat.id,
                title=chat.title or "Unknown",
                username=getattr(chat, "username", None)
            )
            set_state(client, "target_add_state", user_id, None)
            if result is None:
                return await message.reply("⚠️ This target is already added.")
            await message.reply(
                f"✅ **Target Added Successfully!**\n\n"
                f"**Name:** {chat.title}\n"
                f"**ID:** `{chat.id}`"
            )
            targets = await get_user_targets(user_id)
            await message.reply(
                f"**🎯 Your Targets** ({len(targets)})",
                reply_markup=targets_list_keyboard(targets)
            )
        except ValueError:
            await message.reply("❌ Invalid Chat ID. Please send a valid number or @username.")
        except Exception as e:
            await message.reply(
                friendly_error("add target", e) + "\n\n"
                "Make sure:\n"
                "• Bot is **Admin** in that channel/group\n"
                "• You sent correct Chat ID or @username"
            )
        return

    bot_add_state = get_state(client, "bot_add_state", user_id)
    if bot_add_state:
        token = text.strip()
        if ":" not in token or len(token) < 40:
            return await message.reply(
                "❌ Invalid bot token format.\n\n"
                "Token looks like this:\n"
                "`123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw`"
            )
        try:
            from pyrogram import Client as TempClient
            temp_bot = TempClient(
                name=f"validate_{user_id}_{int(message.id)}",
                api_id=Config.API_ID,
                api_hash=Config.API_HASH,
                bot_token=token,
                in_memory=True,
                no_updates=True
            )
            await temp_bot.start()
            me = await temp_bot.get_me()
            await temp_bot.stop()
            bot_username = me.username
            bot_name = me.first_name or "Forward Bot"
            from core.access import check_limit
            from database import get_user_bots
            err = await check_limit(user_id, "bots", len(await get_user_bots(user_id)))
            if err:
                set_state(client, "bot_add_state", user_id, None)
                return await message.reply(err)

            result = await add_forward_bot(
                user_id=user_id,
                bot_token=token,
                bot_username=bot_username,
                name=bot_name
            )
            set_state(client, "bot_add_state", user_id, None)
            if result is None:
                return await message.reply("⚠️ This bot token is already added.")
            await message.reply(
                f"✅ **Forward Bot Added Successfully!**\n\n"
                f"**Name:** {bot_name}\n"
                f"**Username:** @{bot_username}\n"
                f"**Bot ID:** `{result.get('bot_id')}`"
            )
            bots = await get_user_bots(user_id)
            await message.reply(
                f"**🤖 Forward Bots** ({len(bots)})",
                reply_markup=bots_list_keyboard(bots)
            )
        except Exception as e:
            error_msg = str(e).lower()
            if "auth_key_unregistered" in error_msg or "401" in error_msg:
                await message.reply("❌ **Invalid Bot Token**\n\nGet a new token from @BotFather.")
            elif "unauthorized" in error_msg or "token" in error_msg:
                await message.reply("❌ Invalid Bot Token. Please check and try again.")
            elif "flood" in error_msg:
                await message.reply("⏳ FloodWait. Try again later.")
            else:
                await message.reply(friendly_error("add forward bot", e))
        return

    account_edit = get_state(client, "account_edit_state", user_id)
    if account_edit:
        action = account_edit.get("action")
        account_id = account_edit.get("account_id")
        try:
            if action == "set_limit":
                limit = int(text)
                if limit < 1:
                    return await message.reply("Limit must be at least 1.")
                await update_account(user_id, account_id, {"forward_limit": limit})
                await message.reply(f"✅ Forward limit set to **{limit}** messages per cycle")
            elif action == "set_sleep":
                minutes = int(text)
                if minutes < 1:
                    return await message.reply("Sleep time must be at least 1 minute.")
                await update_account(user_id, account_id, {"sleep_after_limit_minutes": minutes})
                await message.reply(f"✅ Sleep after limit set to **{minutes} minutes**")
            set_state(client, "account_edit_state", user_id, None)
            account = await get_account(user_id, account_id)
            if account:
                from handlers.accounts_handlers import account_detail_text
                await message.reply(
                    account_detail_text(account),
                    reply_markup=account_settings_keyboard(account)
                )
        except ValueError:
            await message.reply("❌ Please send a valid number.")
        except Exception as e:
            await message.reply(friendly_error("account edit", e))
        return

    state = get_state(client, "settings_state", user_id)
    if state:
        action = state.get("action")
        chat_id = state.get("chat_id")
        index = state.get("index")
        target = await get_target(user_id, chat_id)
        if not target:
            set_state(client, "settings_state", user_id, None)
            return await message.reply("Target not found.")
        s = target.get("settings") or {}

        try:
            if action == "set_delay":
                delay, err = parse_delay(text)
                if err:
                    return await message.reply(f"❌ {err}")
                await update_target_settings(user_id, chat_id, {"delay": delay})
                await message.reply(f"✅ Delay set to **{delay}s**")

            elif action == "set_caption_template":
                if not text.strip():
                    return await message.reply("❌ Template cannot be empty.")
                await update_target_settings(user_id, chat_id, {"caption_template": text})
                await message.reply("✅ Caption template updated.")
                set_state(client, "settings_state", user_id, None)
                target = await get_target(user_id, chat_id)
                from handlers.settings_handlers import _caption_text
                await message.reply(_caption_text(target), reply_markup=caption_keyboard(chat_id))
                return

            elif action == "caption_preview":
                preview = apply_caption_text(text, s) or "(empty)"
                await message.reply(
                    f"**👁 Caption Preview** (not forwarded)\n\n"
                    f"**Original:**\n{text}\n\n"
                    f"**Template:**\n`{s.get('caption_template', '{caption}')}`\n\n"
                    f"**Preview:**\n{preview}",
                    reply_markup=caption_keyboard(chat_id),
                )
                set_state(client, "settings_state", user_id, None)
                return

            elif action == "add_block_words":
                existing = list(s.get("block_words") or [])
                items, errors = parse_word_list(text, existing)
                await update_target_settings(user_id, chat_id, {"block_words": existing + items})
                await message.reply(format_result(errors, len(items), "words"))

            elif action == "edit_block_words":
                items, errors = parse_word_list(text)
                if not items:
                    return await message.reply(format_result(errors, 0, "words") or "❌ Empty value.")
                cur = list(s.get("block_words") or [])
                if index is None or index >= len(cur):
                    return await message.reply("Item not found.")
                new_word = items[0]
                others = [w.lower() for i, w in enumerate(cur) if i != index]
                if new_word.lower() in others:
                    return await message.reply("❌ Duplicate block word.")
                cur[index] = new_word
                await update_target_settings(user_id, chat_id, {"block_words": cur})
                await message.reply("✅ Word updated.")

            elif action == "add_whitelist":
                existing = list(s.get("whitelist") or [])
                items, errors = parse_word_list(text, existing)
                await update_target_settings(user_id, chat_id, {"whitelist": existing + items})
                await message.reply(format_result(errors, len(items), "words"))

            elif action == "edit_whitelist":
                items, errors = parse_word_list(text)
                if not items:
                    return await message.reply(format_result(errors, 0, "words") or "❌ Empty value.")
                cur = list(s.get("whitelist") or [])
                if index is None or index >= len(cur):
                    return await message.reply("Item not found.")
                new_word = items[0]
                others = [w.lower() for i, w in enumerate(cur) if i != index]
                if new_word.lower() in others:
                    return await message.reply("❌ Duplicate whitelist word.")
                cur[index] = new_word
                await update_target_settings(user_id, chat_id, {"whitelist": cur})
                await message.reply("✅ Word updated.")

            elif action == "add_replacements":
                existing_from = [r.get("from", "") for r in (s.get("replacements") or [])]
                items, errors = parse_replacements(text, existing_from)
                if items:
                    await update_target_settings(
                        user_id, chat_id, {"replacements": (s.get("replacements") or []) + items}
                    )
                await message.reply(format_result(errors, len(items), "rules"))

            elif action == "edit_replacements":
                items, errors = parse_replacements(text)
                if errors and not items:
                    return await message.reply(format_result(errors, 0, "rules"))
                cur = list(s.get("replacements") or [])
                if index is None or index >= len(cur):
                    return await message.reply("Item not found.")
                if not items:
                    return await message.reply("❌ Send one rule: `old => new`")
                rule = items[0]
                others = [r.get("from", "").lower() for i, r in enumerate(cur) if i != index]
                if rule["from"].lower() in others:
                    return await message.reply("❌ Duplicate rule.")
                cur[index] = rule
                await update_target_settings(user_id, chat_id, {"replacements": cur})
                extra = format_result(errors, 1, "rules") if errors else "✅ Rule updated."
                await message.reply(extra)

            elif action == "add_inline_buttons":
                items, errors = parse_inline_buttons(text)
                if items:
                    await update_target_settings(
                        user_id, chat_id, {"inline_buttons": (s.get("inline_buttons") or []) + items}
                    )
                await message.reply(format_result(errors, len(items), "rows"))

            elif action == "edit_inline_buttons":
                items, errors = parse_inline_buttons(text)
                if errors and not items:
                    return await message.reply(format_result(errors, 0, "rows"))
                cur = list(s.get("inline_buttons") or [])
                if index is None or index >= len(cur):
                    return await message.reply("Item not found.")
                if not items:
                    return await message.reply("❌ Invalid button row.")
                cur[index] = items[0]
                await update_target_settings(user_id, chat_id, {"inline_buttons": cur})
                await message.reply("✅ Button row updated." if not errors else format_result(errors, 1, "rows"))

            set_state(client, "settings_state", user_id, None)
            target = await get_target(user_id, chat_id)
            if target:
                from handlers.keyboards import settings_category_keyboard, target_settings_keyboard
                cat = state.get("category")
                title = target.get("title", "Unknown")
                if cat:
                    from handlers.settings_handlers import _category_text
                    await message.reply(
                        _category_text(target, cat),
                        reply_markup=settings_category_keyboard(target, cat),
                    )
                else:
                    await message.reply(
                        f"**🎯 Target Settings**\n\n"
                        f"**Name:** {title}\n"
                        f"**Chat ID:** `{chat_id}`",
                        reply_markup=target_settings_keyboard(target)
                    )
        except Exception as e:
            await message.reply(friendly_error("target settings update", e))
        return

    job_state = get_state(client, "job_create_state", user_id)

    if job_state and job_state.get("step") == "final_options":
        try:
            parts = text.split()
            n1 = int(parts[0])
            n2 = int(parts[1]) if len(parts) > 1 else None
        except ValueError:
            return await message.reply(
                "Send a number.\n"
                "Skip example: `0` or `200`\n"
                "(Last message ID already comes from the source link.)"
            )

        known_last = int(job_state.get("last_msg_id") or 0)
        if n2 is None:
            skip = n1
            last_msg_id = known_last
        else:
            last_msg_id = n1
            skip = n2

        if last_msg_id <= 0:
            return await message.reply(
                "Source last message ID missing. /cancel then send the source link again."
            )
        if skip < 0:
            return await message.reply("Skip cannot be negative.")
        if skip >= last_msg_id:
            return await message.reply(
                f"Skip `{skip}` must be less than last message ID `{last_msg_id}`."
            )

        job_state["last_msg_id"] = last_msg_id
        job_state["skip"] = skip
        job_state["step"] = "confirm"
        job_state.setdefault("future_new_posts", False)
        set_state(client, "job_create_state", user_id, job_state)

        from handlers.jobs_handlers import job_confirm_keyboard, job_confirm_text
        await message.reply(
            job_confirm_text(job_state),
            reply_markup=job_confirm_keyboard(job_state),
        )
        return

    if job_state and job_state.get("step") == "source":
        from handlers.source_handler import continue_job_create_from_source, parse_source_from_message
        source_chat_id, last_msg_id, err = parse_source_from_message(message)
        if err:
            return await message.reply(
                "Couldn't read a source from that.\n\n"
                "Forward a message from the source, or send a link like:\n"
                "`https://t.me/c/1234567890/100`"
            )
        await continue_job_create_from_source(
            client, message, user_id, source_chat_id, last_msg_id
        )
        return
