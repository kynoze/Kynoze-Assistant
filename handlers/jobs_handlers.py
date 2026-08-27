# Job skip = same meaning as Quick Forward.
# Last message ID comes from the source link (never overwritten by skip).

import logging

from pyrogram import Client, filters
from pyrogram import Client as TempClient
from pyrogram.errors import QueryIdInvalid
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from config import Config
from database import (
    JobStatus,
    add_job_log,
    clear_job_logs,
    create_job,
    delete_job,
    ensure_user,
    get_bot,
    get_job,
    get_job_scoped,
    get_job_logs,
    get_next_available_account,
    get_user_accounts,
    get_user_bots,
    get_user_jobs,
    get_visible_jobs,
    get_user_targets,
    is_admin,
    job_monitor_interval,
    set_job_status,
    update_job,
)
from handlers.keyboards import (
    confirm_delete_job_keyboard,
    job_interval_keyboard,
    job_logs_keyboard,
    job_monitor_keyboard,
    jobs_list_keyboard,
    select_accounts_keyboard,
    select_bot_keyboard,
    select_method_keyboard,
    select_targets_keyboard,
)
from core.permissions import validate_job_permissions
from core.state import get_state, set_state
from handlers.ui import (
    HR,
    clamp_interval,
    fmt_dt,
    fmt_duration,
    fmt_interval,
    paginate,
    pager_row,
    pct,
    safe_answer,
    safe_edit,
    status_icon,
)

logger = logging.getLogger(__name__)


def job_controls_keyboard(job: dict) -> InlineKeyboardMarkup:
    job_id = job["job_id"]
    status = job.get("status", "pending")
    buttons = []

    if status in ["pending", "paused", "completed"]:
        buttons.append(
            [
                InlineKeyboardButton("▶️ Start", callback_data=f"job:start:{job_id}"),
            ]
        )
    if status == "running":
        buttons.append(
            [
                InlineKeyboardButton("⏸ Pause", callback_data=f"job:pause:{job_id}"),
                InlineKeyboardButton("⏹ Stop", callback_data=f"job:cancel:{job_id}"),
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton("📊 Stats", callback_data=f"job:stats:{job_id}"),
            InlineKeyboardButton("🆕 Monitor", callback_data=f"job:mon:{job_id}"),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton("📋 Logs", callback_data=f"job:logs:{job_id}"),
            InlineKeyboardButton("🔄 Refresh", callback_data=f"job:open:{job_id}"),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton("🗑 Delete", callback_data=f"job:delete:{job_id}"),
            InlineKeyboardButton("« Back", callback_data="job:list"),
        ]
    )
    return InlineKeyboardMarkup(buttons)


def job_detail_text(job: dict) -> str:
    from datetime import datetime, timezone

    stats = job.get("stats") or {}
    last = int(job.get("last_msg_id") or 0)
    skip = int(job.get("skip") or 0)
    cur = int(job.get("current_msg_id") or skip or 0)
    p = pct(cur, last)
    future = bool(job.get("future_new_posts"))
    status = job.get("status")
    monitoring = status == "running" and future and cur >= last
    icon = status_icon(status)

    started = job.get("started_at")
    completed = job.get("completed_at")
    now = datetime.now(timezone.utc)
    runtime = None
    speed = 0.0
    eta = None
    fwd = int(stats.get("forwarded") or 0)

    def _as_utc(dt):
        if not isinstance(dt, datetime):
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    st = _as_utc(started)
    if status in ("completed", "cancelled", "failed"):
        end_t = _as_utc(completed) or now
    else:
        end_t = now
    if st and end_t:
        runtime = max(0.0, (end_t - st).total_seconds())
        if runtime > 0 and fwd > 0:
            speed = fwd / runtime
            remain = max(0, last - cur)
            if monitoring or remain <= 0:
                eta = None
            elif speed > 0:
                eta = remain / speed

    err = job.get("error_message")
    err_block = ""
    if err and status in ("paused", "failed"):
        reason = str(err).strip().split("\n")[0][:180]
        err_block = (
            f"\n❌ **Job Error**\n"
            f"Reason: {reason}\n"
            "Check Logs for details.\n"
        )

    actor = ""
    if job.get("method") == "bot":
        actor = f"🤖 Bot: `{str(job.get('bot_id') or '-')[:8]}`\n"
    else:
        n_acc = len(job.get("account_ids") or [])
        actor = f"👤 Accounts: `{n_acc}`\n"

    speed_s = f"{speed:.2f} msg/s" if speed > 0 else "—"
    eta_s = fmt_duration(eta) if eta is not None else "—"
    runtime_s = fmt_duration(runtime) if runtime is not None else "—"

    return (
        f"**📋 Job** `{job.get('job_id', '')[:8]}`\n\n"
        f"**{job.get('name')}**\n"
        f"**Source:** {job.get('source_title')} (`{job.get('source_chat_id')}`)\n"
        f"**Targets:** {len(job.get('target_chat_ids') or [])}\n"
        f"**Method:** `{job.get('method')}`\n"
        f"{actor}"
        f"**Status:** {icon} `{status}`\n\n"
        f"**Progress:** `{cur:,} / {last:,}`  ({p}%)\n"
        f"📤 Forwarded: `{fwd:,}`\n"
        f"⏭ Skipped: `{int(stats.get('skipped_filter') or 0) + int(stats.get('skipped_deleted') or 0):,}`\n"
        f"♻️ Duplicate: `{int(stats.get('skipped_duplicate') or 0):,}`\n"
        f"❌ Errors: `{int(stats.get('errors') or 0):,}`\n\n"
        f"⚡ Speed: `{speed_s}`\n"
        f"⏱ Runtime: `{runtime_s}`\n"
        f"🕐 ETA: `{eta_s}`\n"
        f"🆕 Future Posts: `{'ON' if future else 'OFF'}`\n"
        f"👀 Monitoring: `{'🟢 Active' if monitoring else '⚪ Idle'}`\n"
        f"⏱ Interval: `{fmt_interval(job_monitor_interval(job))}`\n"
        f"{err_block}"
    )


def job_monitor_text(job: dict) -> str:
    from datetime import datetime, timezone, timedelta

    future = bool(job.get("future_new_posts"))
    status = job.get("status")
    last = int(job.get("last_msg_id") or 0)
    cur = int(job.get("current_msg_id") or job.get("skip") or 0)
    interval = job_monitor_interval(job)
    last_at = job.get("last_monitor_at")
    next_at = job.get("next_monitor_at")

    mon_label = "⚪ Idle"
    if status == "running" and future and cur >= last:
        now = datetime.now(timezone.utc)
        la = last_at
        if isinstance(la, str):
            try:
                la = datetime.fromisoformat(la.replace("Z", "+00:00"))
            except Exception:
                la = None
        if isinstance(la, datetime) and la.tzinfo is None:
            la = la.replace(tzinfo=timezone.utc)
        if la is None:
            mon_label = "🟢 Starting"
        else:
            lag = (now - la).total_seconds()
            if lag > max(interval * 2.5, interval + 30):
                mon_label = "🟡 Delayed"
            else:
                mon_label = "🟢 Active"
        if next_at is None and isinstance(la, datetime):
            next_at = la + timedelta(seconds=interval)
    elif status == "running" and future:
        mon_label = "⏳ Catching up history"
    elif status in ("failed", "cancelled"):
        mon_label = "🔴 Stopped"

    lines = [
        "**🆕 Future Post Monitoring**",
        "",
        f"**Job:** {job.get('name')}",
        f"Status: `{'🟢 Enabled' if future else '⚪ Disabled'}`",
        f"👀 Monitoring: `{mon_label}`",
        f"⏱ Interval: **{fmt_interval(interval)}**",
        f"🕐 Last Check: `{fmt_dt(last_at)}`",
        f"⏭ Next Check: `{fmt_dt(next_at)}`",
        f"Last Detected Message: `#{job.get('last_detected_msg_id') or cur}`",
        f"New Posts Forwarded: `{int(job.get('new_posts_forwarded') or 0):,}`",
    ]
    return chr(10).join(lines)


async def job_logs_text(job_id: str) -> str:
    logs = await get_job_logs(job_id, limit=25)
    if not logs:
        return "**📋 Job Logs**\n\nNo logs yet."
    lines = ["**📋 Job Logs**\n"]
    for row in reversed(logs):
        ts = fmt_dt(row.get("created_at"))
        level = (row.get("level") or "info").lower()
        icon = {"info": "✅", "warn": "⚠️", "error": "❌"}.get(level, "•")
        msg = (row.get("message") or "")[:80]
        lines.append(f"`{ts}` {icon} {msg}")
    return "\n".join(lines)


def job_confirm_keyboard(state: dict) -> InlineKeyboardMarkup:
    future = bool(state.get("future_new_posts"))
    skip = int(state.get("skip") or 0)
    last = int(state.get("last_msg_id") or 0)
    method = state.get("method") or "-"
    n_targets = len(state.get("selected_targets") or [])
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"Last message ID: {last}", callback_data="jobcreate:noop")],
            [
                InlineKeyboardButton(
                    f"Skip: {skip}  (tap to change)",
                    callback_data="jobcreate:set_skip",
                )
            ],
            [
                InlineKeyboardButton(
                    f"Future New Posts: {'ON' if future else 'OFF'}",
                    callback_data="jobcreate:toggle_future",
                )
            ],
            [
                InlineKeyboardButton(
                    f"Create Job  ({method}, {n_targets} target)",
                    callback_data="jobcreate:confirm",
                )
            ],
            [InlineKeyboardButton("Cancel", callback_data="job:list")],
        ]
    )


def job_confirm_text(state: dict) -> str:
    last = int(state.get("last_msg_id") or 0)
    skip = int(state.get("skip") or 0)
    start_at = skip + 1 if last else 0
    return (
        "**Create Job – Confirm**\n\n"
        f"**Source:** {state.get('source_title')}\n"
        f"**Source ID:** `{state.get('source_chat_id')}`\n"
        f"**Last message ID:** `{last}`  (from source link — not skip)\n"
        f"**Skip:** `{skip}`  → forwarding starts at `{start_at}`\n"
        f"**Targets:** `{len(state.get('selected_targets') or [])}`\n"
        f"**Method:** `{state.get('method')}`\n"
        f"**Future New Posts:** `{'ON' if state.get('future_new_posts') else 'OFF'}`\n\n"
        "Skip ka matlab Quick Forward jaisa: pehle N messages skip, uske baad last ID tak.\n"
        "Tap **Skip** to change. Send only a number like `0` or `200`."
    )


def show_confirm(state: dict):
    state["step"] = "confirm"
    state.setdefault("skip", 0)
    state.setdefault("future_new_posts", False)


async def show_jobs_list(client: Client, query: CallbackQuery, page: int = 0):
    user_id = query.from_user.id
    jobs = await get_user_jobs(user_id, limit=80)
    if not jobs:
        text = (
            "**📋 Your Jobs**\n\n"
            "No jobs yet.\n"
            "Create a job or use **Quick Forward** from the dashboard."
        )
        kb = jobs_list_keyboard([])
        await safe_edit(query, text, kb)
        return await safe_answer(query)

    slice_, page, total_pages = paginate(jobs, page)
    lines = [f"**📋 Your Jobs** ({len(jobs)})\n"]
    for j in slice_:
        s = j.get("stats") or {}
        last = int(j.get("last_msg_id") or 0)
        cur = int(j.get("current_msg_id") or j.get("skip") or 0)
        icon = status_icon(j.get("status"))
        future = "ON" if j.get("future_new_posts") else "OFF"
        name = (j.get("name") or "Job")[:32]
        lines.append(
            f"{icon} **{name}**\n"
            f"Progress: `{cur:,} / {last:,}`  Method: `{j.get('method')}`  🆕 {future}\n"
            f"📤 `{int(s.get('forwarded') or 0):,}`  ❌ `{int(s.get('errors') or 0):,}`"
        )
    text = "\n".join(lines)
    kb = jobs_list_keyboard(slice_)
    # inject pager
    rows = list(kb.inline_keyboard)
    pager = pager_row("job:listp:", page, total_pages)
    if pager:
        rows.insert(-2, pager)
        kb = InlineKeyboardMarkup(rows)
    await safe_edit(query, text, kb)
    await safe_answer(query)


@Client.on_callback_query(filters.regex(r"^job:"))
async def jobs_callbacks(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    from core.access import can_access_bot
    if not await can_access_bot(user_id):
        return await safe_answer(query, "Not allowed", True)

    data = query.data
    await ensure_user(user_id)

    if data == "job:list":
        await show_jobs_list(client, query)
        return

    if data.startswith("job:listp:"):
        try:
            page = int(data.split(":")[2])
        except Exception:
            page = 0
        await show_jobs_list(client, query, page)
        return

    if data == "job:create":
        set_state(client, "forward_state", user_id, None)
        set_state(client, "job_create_state", user_id, {"step": "source"})
        await safe_edit(
            query,
            "**Create New Job – Step 1**\n\n"
            "Send the **Source Channel/Group** link or forward a message from it.\n\n"
            "Example:\n`https://t.me/c/1234567890/100`\n\n"
            "Type /cancel to cancel.",
        )
        return await safe_answer(query)

    if data.startswith("job:open:"):
        job_id = data.split(":")[2]
        job = await get_job_scoped(user_id, job_id)
        if not job:
            return await safe_answer(query, "Job not found", True)
        await safe_edit(query, job_detail_text(job), job_controls_keyboard(job))
        return await safe_answer(query)

    if data.startswith("job:mon:"):
        job_id = data.split(":")[2]
        job = await get_job_scoped(user_id, job_id)
        if not job:
            return await safe_answer(query, "Job not found", True)
        await safe_edit(query, job_monitor_text(job), job_monitor_keyboard(job))
        return await safe_answer(query)

    if data.startswith("job:intcustom:"):
        job_id = data.split(":")[2]
        set_state(client, "job_interval_state", user_id, {"job_id": job_id})
        await safe_edit(
            query,
            "**⏱ Custom interval**\n\n"
            "Send monitoring interval in **seconds**.\n"
            "Example: `15`\n\n"
            f"Allowed: `{5}` – `{864000}` seconds (max 10 days).\n"
            "/cancel to go back.",
        )
        return await safe_answer(query)

    if data.startswith("job:intset:"):
        parts = data.split(":")
        job_id = parts[2]
        try:
            seconds = clamp_interval(parts[3])
        except Exception:
            return await safe_answer(query, "Invalid interval", True)
        await update_job(user_id, job_id, {"monitor_interval_seconds": seconds})
        job = await get_job_scoped(user_id, job_id)
        await safe_answer(query, f"Interval → {fmt_interval(seconds)}", True)
        await safe_edit(query, job_monitor_text(job), job_monitor_keyboard(job))
        return

    if data.startswith("job:int:"):
        job_id = data.split(":")[2]
        job = await get_job_scoped(user_id, job_id)
        if not job:
            return await safe_answer(query, "Job not found", True)
        cur = job_monitor_interval(job)
        await safe_edit(
            query,
            f"**⏱ Monitoring Interval**\n\nCurrent: **{fmt_interval(cur)}**\n\n"
            "How often should the bot check for new posts?",
            job_interval_keyboard(job),
        )
        return await safe_answer(query)

    if data.startswith("job:logs:"):
        job_id = data.split(":")[2]
        await safe_edit(query, await job_logs_text(job_id), job_logs_keyboard(job_id))
        return await safe_answer(query)

    if data.startswith("job:logsclear:"):
        job_id = data.split(":")[2]
        await clear_job_logs(job_id)
        await safe_answer(query, "Logs cleared", True)
        await safe_edit(query, await job_logs_text(job_id), job_logs_keyboard(job_id))
        return

    if data.startswith("job:stats:"):
        job_id = data.split(":")[2]
        job = await get_job_scoped(user_id, job_id)
        if not job:
            return await safe_answer(query, "Job not found", True)
        s = job.get("stats") or {}
        last = int(job.get("last_msg_id") or 0)
        skip = int(job.get("skip") or 0)
        cur = int(job.get("current_msg_id") or 0)
        pct = min(100, int(cur * 100 / last)) if last else 0
        future = "ON" if job.get("future_new_posts") else "OFF"
        monitoring = (
            job.get("status") == "running"
            and job.get("future_new_posts")
            and cur >= last
        )
        watch = "👀 Monitoring new posts\n" if monitoring else ""
        text = (
            f"**Job Stats** — {job.get('name')}\n\n"
            f"Status: `{job.get('status')}`\n"
            f"Skip: `{skip}` (start at `{skip + 1}`)\n"
            f"Progress: `{cur}/{last}` ({pct}%)\n"
            f"🆕 Future Posts: `{future}`\n"
            f"{watch}"
            f"Runtime start: `{job.get('started_at') or '-'}`\n"
            f"Last update: `{job.get('updated_at') or '-'}`\n\n"
            f"Fetched: `{s.get('fetched', 0)}`\n"
            f"Forwarded: `{s.get('forwarded', 0)}`\n"
            f"Filter skipped: `{s.get('skipped_filter', 0)}`\n"
            f"Duplicates: `{s.get('skipped_duplicate', 0)}`\n"
            f"Deleted skipped: `{s.get('skipped_deleted', 0)}`\n"
            f"Errors: `{s.get('errors', 0)}`"
        )
        await safe_edit(
            query,
            text,
            InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔄 Refresh", callback_data=f"job:stats:{job_id}")],
                    [InlineKeyboardButton("📋 Logs", callback_data=f"job:logs:{job_id}")],
                    [InlineKeyboardButton("« Back", callback_data=f"job:open:{job_id}")],
                ]
            ),
        )
        return await safe_answer(query)

    if data.startswith("job:toggle_future:"):
        job_id = data.split(":")[2]
        job = await get_job_scoped(user_id, job_id)
        if not job:
            return await safe_answer(query, "Job not found", True)
        new_val = not bool(job.get("future_new_posts"))
        await update_job(user_id, job_id, {"future_new_posts": new_val})
        job = await get_job_scoped(user_id, job_id)
        await safe_edit(query, job_monitor_text(job), job_monitor_keyboard(job))
        return await safe_answer(
            query, f"Future New Posts → {'ON' if new_val else 'OFF'}"
        )

    if data.startswith("job:start:"):
        job_id = data.split(":")[2]
        job = await get_job_scoped(user_id, job_id)
        if not job:
            return await safe_answer(query, "Job not found", True)
        if job.get("status") == JobStatus.RUNNING.value:
            return await safe_answer(query, "Job is already running", True)

        await safe_answer(query, "Starting job...")

        method = job.get("method")
        source_chat_id = job.get("source_chat_id")
        target_chat_ids = job.get("target_chat_ids", [])
        check_client = None
        try:
            if method == "bot":
                bot = await get_bot(user_id, job.get("bot_id"))
                if not bot or bot.get("status") != "active":
                    await query.message.reply("Bot not available or disabled.")
                    return
                token = bot["bot_token"]
                try:
                    from handlers.ui import load_secret
                    token = load_secret(token)
                except Exception:
                    await query.message.reply("❌ Could not read stored bot token. Check SESSION_ENC_KEY.")
                    return
                check_client = TempClient(
                    name=f"perm_check_{job_id}",
                    api_id=Config.API_ID,
                    api_hash=Config.API_HASH,
                    bot_token=token,
                    in_memory=True,
                    no_updates=True,
                )
                await check_client.start()
            elif method == "user":
                account = await get_next_available_account(user_id, job.get("account_ids", []))
                if not account:
                    await query.message.reply("No available account.")
                    return
                session = account["session_string"]
                try:
                    from handlers.ui import load_secret
                    session = load_secret(session)
                except Exception:
                    await query.message.reply("❌ Could not read stored account session. Check SESSION_ENC_KEY.")
                    return
                check_client = TempClient(
                    name=f"perm_check_{job_id}",
                    api_id=Config.API_ID,
                    api_hash=Config.API_HASH,
                    session_string=session,
                    in_memory=True,
                    no_updates=True,
                )
                await check_client.start()
            else:
                await query.message.reply("Unknown method.")
                return

            is_valid, msg = await validate_job_permissions(
                client=check_client,
                method=method,
                source_chat_id=source_chat_id,
                target_chat_ids=target_chat_ids,
            )
            await check_client.stop()
            check_client = None
            if not is_valid:
                await query.message.reply(
                    f"**Permission Check Failed**\n\n"
                    f"`{msg}`\n\n"
                    f"• Private Source + Bot → Bot must be Admin\n"
                    f"• User Account → must be Member of source\n"
                    f"• Target → Bot/Account must be Admin"
                )
                return
        except Exception as e:
            if check_client:
                try:
                    await check_client.stop()
                except Exception:
                    pass
            logger.exception("Permission check failed")
            await query.message.reply("❌ Permission check failed. Please try again.")
            return

        start_fields = {
            "status": JobStatus.RUNNING.value,
            "pause_reason": None,
            "error_message": None,
        }
        if not job.get("started_at"):
            from datetime import datetime, timezone
            start_fields["started_at"] = datetime.now(timezone.utc)
        await update_job(user_id, job_id, start_fields)
        try:
            await add_job_log(job_id, "info", "Job started")
        except Exception:
            pass
        job = await get_job_scoped(user_id, job_id)
        await safe_edit(query, job_detail_text(job), job_controls_keyboard(job))
        return

    if data.startswith("job:pause:"):
        job_id = data.split(":")[2]
        await update_job(
            user_id,
            job_id,
            {
                "status": JobStatus.PAUSED.value,
                "pause_reason": "user",
            },
        )
        await safe_answer(query, "Job paused")
        job = await get_job_scoped(user_id, job_id)
        await safe_edit(query, job_detail_text(job), job_controls_keyboard(job))
        return
        

    if data.startswith("job:cancel:"):
        job_id = data.split(":")[2]
        await set_job_status(user_id, job_id, JobStatus.CANCELLED.value)
        await safe_answer(query, "Job cancelled")
        job = await get_job_scoped(user_id, job_id)
        await safe_edit(query, job_detail_text(job), job_controls_keyboard(job))
        return

    if data.startswith("job:delete:"):
        job_id = data.split(":")[2]
        job = await get_job_scoped(user_id, job_id)
        if not job:
            return await safe_answer(query, "Job not found", True)
        await safe_edit(
            query,
            f"**Delete Job?**\n\n**{job.get('name')}**\n\nThis cannot be undone.",
            confirm_delete_job_keyboard(job_id),
        )
        return await safe_answer(query)

    if data.startswith("job:confirm_delete:"):
        job_id = data.split(":")[2]
        try:
            from core.job_worker import cancel_running_job
            await cancel_running_job(user_id, job_id)
        except Exception:
            logger.exception("cancel_running_job failed for %s", job_id)
        success = await delete_job(user_id, job_id)
        if success:
            await safe_answer(query, "Job deleted", True)
            await show_jobs_list(client, query)
        else:
            await safe_answer(query, "Failed to delete", True)
        return


def _persist_create(client: Client, user_id: int, state: dict) -> None:
    set_state(client, "job_create_state", user_id, state)


@Client.on_callback_query(filters.regex(r"^jobcreate:"))
async def job_create_callbacks(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    from core.access import can_access_bot
    if not await can_access_bot(user_id):
        return await safe_answer(query, "Not allowed", True)

    state = get_state(client, "job_create_state", user_id)
    if not state:
        return await safe_answer(
            query, "Session expired. Start again with Create Job.", True
        )

    parts = query.data.split(":")
    action = parts[1]

    if action == "toggle_target":
        chat_id = int(parts[2])
        selected = state.setdefault("selected_targets", [])
        if chat_id in selected:
            selected.remove(chat_id)
        else:
            selected.append(chat_id)
        _persist_create(client, user_id, state)
        targets = await get_user_targets(user_id)
        try:
            await query.message.edit_reply_markup(select_targets_keyboard(targets, selected))
        except Exception:
            pass
        return await safe_answer(query)

    if action == "next_method":
        if not state.get("selected_targets"):
            return await safe_answer(query, "Select at least one target first.", True)
        state["step"] = "method"
        _persist_create(client, user_id, state)
        await safe_edit(
            query,
            "**Create Job – Step 3**\n\nChoose the forwarding method:",
            select_method_keyboard(),
        )
        return await safe_answer(query)

    if action == "method":
        method = parts[2]
        state["method"] = method
        if method == "user":
            accounts = await get_user_accounts(user_id)
            if not accounts:
                return await safe_answer(
                    query, "No accounts added yet. Add one first.", True
                )
            state["step"] = "accounts"
            state["selected_accounts"] = []
            _persist_create(client, user_id, state)
            await safe_edit(
                query,
                "**Create Job – Step 4**\n\nSelect account(s) to use:",
                select_accounts_keyboard(accounts, []),
            )
        else:
            bots = await get_user_bots(user_id)
            if not bots:
                return await safe_answer(
                    query, "No forward bots added yet. Add one first.", True
                )
            state["step"] = "bot"
            _persist_create(client, user_id, state)
            await safe_edit(
                query,
                "**Create Job – Step 4**\n\nSelect the forward bot to use:",
                select_bot_keyboard(bots),
            )
        return await safe_answer(query)

    if action == "toggle_account":
        acc_id = parts[2]
        selected = state.setdefault("selected_accounts", [])
        if acc_id in selected:
            selected.remove(acc_id)
        else:
            selected.append(acc_id)
        _persist_create(client, user_id, state)
        accounts = await get_user_accounts(user_id)
        try:
            await query.message.edit_reply_markup(
                select_accounts_keyboard(accounts, selected)
            )
        except Exception:
            pass
        return await safe_answer(query)

    if action == "next_options":
        if not state.get("selected_accounts"):
            return await safe_answer(query, "Select at least one account.", True)
        show_confirm(state)
        _persist_create(client, user_id, state)
        await safe_edit(query, job_confirm_text(state), job_confirm_keyboard(state))
        return await safe_answer(query)

    if action == "select_bot":
        state["bot_id"] = parts[2]
        show_confirm(state)
        _persist_create(client, user_id, state)
        await safe_edit(query, job_confirm_text(state), job_confirm_keyboard(state))
        return await safe_answer(query)

    if action == "noop":
        return await safe_answer(query, "This is the source last message ID, not skip.")

    if action == "toggle_future":
        state["future_new_posts"] = not bool(state.get("future_new_posts"))
        _persist_create(client, user_id, state)
        await safe_edit(query, job_confirm_text(state), job_confirm_keyboard(state))
        return await safe_answer(query)

    if action == "set_skip":
        state["step"] = "waiting_skip"
        _persist_create(client, user_id, state)
        last = int(state.get("last_msg_id") or 0)
        await safe_edit(
            query,
            "**Skip (same as Quick Forward)**\n\n"
            f"Source last message ID is `{last}` — that does **not** change.\n\n"
            "Kitne messages skip karne hain start se?\n"
            "Example: `0` (start from 1)  ya  `200` (start from 201)\n\n"
            "/cancel to go back.",
        )
        return await safe_answer(query)

    if action == "confirm":
        last_msg_id = int(state.get("last_msg_id") or 0)
        skip = int(state.get("skip") or 0)
        if last_msg_id <= 0:
            return await safe_answer(
                query, "Last message ID missing. Send a source link again.", True
            )
        if skip >= last_msg_id:
            return await safe_answer(
                query,
                f"Skip ({skip}) must be less than last message ID ({last_msg_id}).",
                True,
            )
        try:
            from core.access import check_limit
            from database import get_user_jobs as _guj
            _err = await check_limit(user_id, "jobs", len(await _guj(user_id, limit=500)))
            if _err:
                return await safe_answer(query, _err, True)

            job = await create_job(
                user_id=user_id,
                source_chat_id=state.get("source_chat_id"),
                source_title=state.get("source_title", "Unknown"),
                target_chat_ids=state.get("selected_targets", []),
                method=state.get("method"),
                account_ids=state.get("selected_accounts"),
                bot_id=state.get("bot_id"),
                last_msg_id=last_msg_id,
                skip=skip,
                future_new_posts=bool(state.get("future_new_posts")),
                name=f"Job {(state.get('source_title') or '')[:20]}",
            )
        except Exception:
            logger.exception("create_job failed")
            return await safe_answer(query, "Create failed. Please try again.", True)

        set_state(client, "job_create_state", user_id, None)
        await safe_edit(
            query,
            f"**Job Created**\n\n"
            f"**ID:** `{job['job_id']}`\n"
            f"**Source:** {job.get('source_title')}\n"
            f"**Last ID:** `{last_msg_id}`\n"
            f"**Skip:** `{skip}` → starts at `{skip + 1}`\n"
            f"**Future posts:** `{'ON' if job.get('future_new_posts') else 'OFF'}`\n"
            f"**Method:** `{job.get('method')}`\n\n"
            f"Open Jobs → Start.",
            jobs_list_keyboard(await get_user_jobs(user_id, limit=30)),
        )
        return await safe_answer(query, "Job created")


