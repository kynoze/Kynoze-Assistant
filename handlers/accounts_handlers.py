from pyrogram import Client, filters
from pyrogram.types import CallbackQuery

from database import (
    is_admin,
    ensure_user,
    get_user_accounts,
    get_account,
    update_account,
    set_account_status,
    delete_account,
    reset_account_cycle,
    AccountStatus,
    get_account_scoped,
)
from handlers.keyboards import (
    accounts_list_keyboard,
    account_settings_keyboard,
    confirm_delete_account_keyboard
)
from core.state import set_state
from core.errors import friendly_error
from handlers.ui import (
    HR,
    load_secret,
    paginate,
    remaining,
    safe_answer,
    safe_edit,
    status_icon,
    with_pager,
)
from config import Config
import logging

logger = logging.getLogger(__name__)


def account_detail_text(account: dict) -> str:
    from handlers.ui import format_account_label

    name = account.get("name") or account.get("phone") or "Unknown"
    uname = account.get("username")
    uname_s = f"@{uname}" if uname else "—"
    tg_id = account.get("tg_user_id") or "—"
    status = account.get("status", "active")
    limit = account.get("forward_limit", 500)
    sleep_min = account.get("sleep_after_limit_minutes", 30)
    forwarded = account.get("forwarded_count", 0)
    total = account.get("total_forwarded", 0)
    icon = status_icon(status)
    flood = "None"
    err = account.get("error_message") or ""
    if "flood" in err.lower():
        flood = err[:80]

    text = (
        f"**👤 {format_account_label(account, short=True)}**\n\n"
        f"{icon} **{status.title()}**\n"
        f"{HR}\n"
        f"**Name:** {name}\n"
        f"**Username:** {uname_s}\n"
        f"**Telegram ID:** `{tg_id}`\n"
        f"**Phone:** `{account.get('phone') or '—'}`\n"
        f"**Current Cycle:** `{forwarded}/{limit}`\n"
        f"**Total Forwarded:** `{total:,}`\n"
        f"**Forward Limit:** `{limit}`\n"
        f"**Sleep After Limit:** `{sleep_min} min`\n"
        f"**FloodWait:** {flood}\n"
    )
    if status == "sleeping":
        text += (
            f"\n😴 **Sleeping**\n"
            f"Sleep remaining: `{remaining(account.get('sleep_until'))}`\n"
        )
    if err and "flood" not in err.lower():
        text += f"\n⚠️ `{err[:120]}`"
    return text


async def show_accounts_list(client: Client, query: CallbackQuery, page: int = 0):
    user_id = query.from_user.id
    try:
        from database import get_visible_accounts
        accounts = await get_visible_accounts(user_id)
    except ImportError:
        accounts = await get_user_accounts(user_id)

    if not accounts:
        text = (
            "**👤 My Accounts**\n\n"
            "You have no accounts yet.\n"
            "Click **Add Account** to authorize a new user account."
        )
        await safe_edit(query, text, accounts_list_keyboard([]))
        return await safe_answer(query)

    from handlers.ui import format_account_label

    slice_, page, total_pages = paginate(accounts, page)
    lines = [f"**👤 My Accounts** ({len(accounts)})\n"]
    for acc in slice_:
        label = format_account_label(acc, short=True)
        status = acc.get("status", "active")
        icon = status_icon(status)
        limit = acc.get("forward_limit", 500)
        cycle = acc.get("forwarded_count", 0)
        extra = ""
        if status == "sleeping":
            extra = f"  remaining `{remaining(acc.get('sleep_until'))}`"
        lines.append(f"{icon} **{label}**  `{cycle}/{limit}`{extra}")
    kb = with_pager(accounts_list_keyboard(slice_), "acc:listp:", page, total_pages)
    await safe_edit(query, "\n".join(lines), kb)
    await safe_answer(query)


async def _test_account(account: dict) -> str:
    from pyrogram import Client as TempClient

    try:
        session = load_secret(account.get("session_string") or "")
    except Exception as e:
        return f"❌ Could not read stored session.\n{e}"

    temp = TempClient(
        name=f"acc_test_{account.get('account_id')}",
        api_id=Config.API_ID,
        api_hash=Config.API_HASH,
        session_string=session,
        in_memory=True,
        no_updates=True,
    )
    try:
        await temp.start()
        me = await temp.get_me()
        uname = f"@{me.username}" if me.username else "—"
        name = " ".join(
            x for x in [(me.first_name or ""), (me.last_name or "")] if x
        ).strip() or "Account"
        # Refresh profile fields on successful test
        try:
            from database import update_account
            await update_account(
                account.get("user_id"),
                account.get("account_id"),
                {
                    "name": name,
                    "first_name": me.first_name,
                    "last_name": me.last_name,
                    "username": me.username,
                    "tg_user_id": me.id,
                },
            )
        except Exception:
            pass
        return (
            f"✅ Connected as **{name}**\n"
            f"Username: {uname}\n"
            f"Telegram ID: `{me.id}`"
        )
    except Exception as e:
        return friendly_error("account test", e)
    finally:
        try:
            await temp.stop()
        except Exception:
            pass


@Client.on_callback_query(filters.regex(r"^acc:"))
async def accounts_callbacks(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    from core.access import can_access_bot
    if not await can_access_bot(user_id):
        return await safe_answer(query, "Not allowed", True)

    data = query.data
    await ensure_user(user_id)

    if data == "acc:list":
        await show_accounts_list(client, query, 0)
        return

    if data.startswith("acc:listp:"):
        try:
            page = int(data.split(":")[2])
        except Exception:
            page = 0
        await show_accounts_list(client, query, page)
        return

    if data == "acc:add":
        await safe_edit(
            query,
            "**➕ Add New Account**\n\n"
            "Send the **phone number** in international format.\n\n"
            "Example: `+919876543210`\n\n"
            "Type /cancel to cancel.",
        )
        set_state(client, "account_add_state", user_id, {"step": "phone"})
        return await safe_answer(query)

    if data.startswith("acc:open:") or data.startswith("acc:stats:"):
        account_id = data.split(":")[2]
        account = await get_account_scoped(user_id, account_id)
        if not account:
            return await safe_answer(query, "Account not found", True)
        await safe_edit(query, account_detail_text(account), account_settings_keyboard(account))
        return await safe_answer(query)

    if data.startswith("acc:test:"):
        account_id = data.split(":")[2]
        account = await get_account_scoped(user_id, account_id)
        if not account:
            return await safe_answer(query, "Account not found", True)
        await safe_answer(query, "Testing connection...")
        result = await _test_account(account)
        account = await get_account_scoped(user_id, account_id)
        text = account_detail_text(account) + f"\n\n{HR}\n{result}"
        await safe_edit(query, text, account_settings_keyboard(account))
        return

    if data.startswith("acc:toggle_status:"):
        account_id = data.split(":")[2]
        account = await get_account_scoped(user_id, account_id)
        if not account:
            return await safe_answer(query, "Account not found", True)

        current = account.get("status", "active")
        new_status = AccountStatus.DISABLED.value if current == AccountStatus.ACTIVE.value else AccountStatus.ACTIVE.value
        await set_account_status(user_id, account_id, new_status)
        if new_status == AccountStatus.DISABLED.value:
            # Fully offline — stop any CNL client using this account
            try:
                from core.cnl.clients import get_user_client_manager
                await get_user_client_manager().stop_user_client(user_id, account_id=str(account_id))
            except Exception:
                logger.debug("stop disabled account client failed", exc_info=True)
            try:
                from core.lifecycle import release_my_account
                # drop CNL deps for this account
                from core import lifecycle as lc
                key = f"acc:{int(user_id)}:{account_id}"
                async with lc._lock:
                    lc._acc_deps.pop(key, None)
            except Exception:
                pass

        account = await get_account_scoped(user_id, account_id)
        await safe_edit(query, account_detail_text(account), account_settings_keyboard(account))
        return await safe_answer(query, f"Status → {new_status}")

    if data.startswith("acc:set_limit:"):
        account_id = data.split(":")[2]
        await safe_edit(
            query,
            "**🔢 Set Forward Limit**\n\n"
            "Send the maximum number of messages this account can forward **in one cycle** before sleeping.\n\n"
            "Example: `500`\n\nType /cancel to go back.",
        )
        set_state(client, "account_edit_state", user_id, {"action": "set_limit", "account_id": account_id})
        return await safe_answer(query)

    if data.startswith("acc:set_sleep:"):
        account_id = data.split(":")[2]
        await safe_edit(
            query,
            "**😴 Set Sleep Time**\n\n"
            "Send how many **minutes** the account should sleep after reaching limit.\n\n"
            "Example: `30`\n\nType /cancel to go back.",
        )
        set_state(client, "account_edit_state", user_id, {"action": "set_sleep", "account_id": account_id})
        return await safe_answer(query)

    if data.startswith("acc:reset:"):
        account_id = data.split(":")[2]
        await reset_account_cycle(user_id, account_id)
        account = await get_account_scoped(user_id, account_id)
        await safe_answer(query, "✅ Cycle reset to 0", True)
        await safe_edit(query, account_detail_text(account), account_settings_keyboard(account))
        return

    if data.startswith("acc:delete:"):
        account_id = data.split(":")[2]
        account = await get_account_scoped(user_id, account_id)
        if not account:
            return await safe_answer(query, "Account not found", True)

        await safe_edit(
            query,
            f"**⚠️ Delete Account?**\n\n"
            f"**{account.get('name')}** (`{account.get('phone')}`)\n\n"
            f"This cannot be undone.",
            confirm_delete_account_keyboard(account_id),
        )
        return await safe_answer(query)

    if data.startswith("acc:confirm_delete:"):
        account_id = data.split(":")[2]
        success = await delete_account(user_id, account_id)
        if success:
            await safe_answer(query, "✅ Account deleted", True)
            await show_accounts_list(client, query, 0)
        else:
            await safe_answer(query, "Failed to delete", True)
        return
