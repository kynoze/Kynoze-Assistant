import logging
from datetime import datetime, timezone

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from database import (
    ensure_user,
    get_duplicate_count,
    get_entity_stats,
    get_stats_overview,
    get_user_accounts,
    get_user_bots,
    get_user_jobs,
    get_user_targets,
    is_admin,
)
from handlers.ui import HR, fmt_dt, remaining, safe_answer, safe_edit, status_icon

logger = logging.getLogger(__name__)


def _pct(done, total):
    try:
        if not total:
            return 0
        return min(100, int(done * 100 / total))
    except Exception:
        return 0


def fmt_dt(value):
    if not value:
        return "-"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)[:16]


def stats_sub_keyboard(refresh_data: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data=refresh_data)],
        [InlineKeyboardButton("« Statistics", callback_data="stats:overall")],
        [InlineKeyboardButton("Dashboard", callback_data="dash:home")],
    ])


def stats_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 Jobs", callback_data="stats:jobs"),
            InlineKeyboardButton("👤 Accounts", callback_data="stats:accounts"),
        ],
        [
            InlineKeyboardButton("🤖 Bots", callback_data="stats:bots"),
            InlineKeyboardButton("🎯 Targets", callback_data="stats:targets"),
        ],
        [InlineKeyboardButton("🌐 Overall", callback_data="stats:overall")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="stats:overall")],
        [InlineKeyboardButton("« Back", callback_data="dash:home")],
    ])


async def build_overview_text(user_id: int) -> str:
    from datetime import datetime, timezone
    o = await get_stats_overview(user_id)
    return (
        "**📊 Statistics — Overall**\n\n"
        f"Updated: `{fmt_dt(datetime.now(timezone.utc))}`\n\n"
        f"Total Jobs: `{o['total_jobs']}`\n"
        f"Running: `{o['running_jobs']}`  Paused: `{o['paused_jobs']}`\n"
        f"Completed: `{o['completed_jobs']}`  Failed: `{o['failed_jobs']}`\n"
        f"Pending: `{o['pending_jobs']}`\n\n"
        f"Forwarded (job deliveries): `{o['total_forwarded']:,}`\n"
        f"Fetched (source msgs seen): `{o['total_fetched']:,}`\n"
        f"Skipped: `{o['total_skipped']:,}`\n"
        f"Duplicates (job skips): `{o['total_duplicates']:,}`\n"
        f"Duplicates stored: `{o['stored_duplicates']:,}`\n"
        f"Errors: `{o['total_errors']:,}`\n\n"
        f"Active Accounts: `{o['active_accounts']}`\n"
        f"Sleeping: `{o['sleeping_accounts']}`  Disabled: `{o['disabled_accounts']}`\n"
        f"Active Forward Bots: `{o['active_bots']}`\n"
        f"Targets: `{o['targets']}`\n\n"
        "_Forwarded = successful sends to targets (1 source × N targets = N)._"
    )


async def show_stats_home(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    await safe_edit(query, await build_overview_text(user_id), stats_menu_keyboard())
    await safe_answer(query)


async def jobs_stats_text(user_id: int) -> str:
    jobs = await get_user_jobs(user_id, limit=20)
    if not jobs:
        return "**📋 Job statistics**\n\nNo jobs yet."
    lines = ["**📋 Job statistics** (latest 20)\n"]
    for j in jobs:
        s = j.get("stats") or {}
        last = int(j.get("last_msg_id") or 0)
        cur = int(j.get("current_msg_id") or j.get("skip") or 0)
        pct = _pct(cur, last)
        name = (j.get("name") or j.get("job_id", "")[:8])[:28]
        future = "ON" if j.get("future_new_posts") else "OFF"
        icon = status_icon(j.get("status"))
        monitoring = (
            j.get("status") == "running" and j.get("future_new_posts") and cur >= last
        )
        watch = "  👀 Monitoring" if monitoring else ""
        lines.append(
            f"{icon} **{name}** `{j.get('status')}`{watch}\n"
            f"  {cur}/{last} ({pct}%)  fwd `{s.get('forwarded', 0)}`  "
            f"dup `{s.get('skipped_duplicate', 0)}`  err `{s.get('errors', 0)}`\n"
            f"  🆕 Future Posts: {future}  updated `{fmt_dt(j.get('updated_at'))}`"
        )
    return "\n".join(lines)


async def accounts_stats_text(user_id: int) -> str:
    accounts = await get_user_accounts(user_id)
    if not accounts:
        return "**👤 Account statistics**\n\nNo accounts yet."
    lines = [
        "**👤 Account statistics**\n",
        "Cycle = messages forwarded this cycle / forward limit.\n",
    ]
    for a in accounts:
        name = a.get("name") or a.get("phone") or a.get("account_id")
        limit = int(a.get("forward_limit") or 0)
        cycle = int(a.get("forwarded_count") or 0)
        total = int(a.get("total_forwarded") or 0)
        status = a.get("status") or "-"
        icon = status_icon(status)
        extra = ""
        if status == "sleeping":
            extra = (
                f"\n  😴 Sleep remaining: `{remaining(a.get('sleep_until'))}`"
            )
        lines.append(
            f"{icon} **{name}** `{status}`\n"
            f"  This cycle: `{cycle}/{limit}`  Total: `{total}`"
            f"{extra}"
        )
    return "\n".join(lines)


async def targets_stats_text(user_id: int) -> str:
    targets = await get_user_targets(user_id)
    if not targets:
        return "**🎯 Target statistics**\n\nNo targets yet."
    lines = ["**🎯 Target statistics**\n"]
    for t in targets:
        title = t.get("title") or str(t.get("chat_id"))
        chat_id = t["chat_id"]
        try:
            dups = await get_duplicate_count(user_id, chat_id)
        except Exception:
            dups = 0
        st = await get_entity_stats(user_id, "target", str(chat_id)) or {}
        lines.append(
            f"**{title}**\n"
            f"  Forwarded: `{int(st.get('forwarded') or 0):,}`  "
            f"Duplicates: `{dups:,}`  "
            f"Blocked: `{int(st.get('blocked') or 0):,}`  "
            f"Errors: `{int(st.get('errors') or 0):,}`"
        )
    return "\n".join(lines)


async def bots_stats_text(user_id: int) -> str:
    bots = await get_user_bots(user_id)
    if not bots:
        return "**🤖 Bot statistics**\n\nNo forward bots yet."
    lines = ["**🤖 Bot statistics**\n"]
    for b in bots:
        name = b.get("name") or b.get("bot_username") or b.get("bot_id")
        icon = status_icon("active" if b.get("status") == "active" else "disabled")
        lines.append(
            f"{icon} **{name}** `{b.get('status')}`\n"
            f"  Total forwarded: `{int(b.get('total_forwarded') or 0):,}`"
        )
    return "\n".join(lines)


@Client.on_callback_query(filters.regex(r"^stats:"))
async def stats_callbacks(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    from core.access import can_access_bot
    if not await can_access_bot(user_id):
        return await safe_answer(query, "Not allowed", True)

    await ensure_user(user_id)
    data = query.data

    if data in ("stats:home", "stats:overall", "stats:refresh"):
        await safe_edit(
            query,
            await build_overview_text(user_id),
            stats_menu_keyboard(),
        )
        return await safe_answer(query)

    mapping = {
        "stats:jobs": jobs_stats_text,
        "stats:accounts": accounts_stats_text,
        "stats:targets": targets_stats_text,
        "stats:bots": bots_stats_text,
    }
    builder = mapping.get(data)
    if not builder:
        return await safe_answer(query)

    await safe_edit(query, builder(user_id), stats_sub_keyboard(data))
    await safe_answer(query)
