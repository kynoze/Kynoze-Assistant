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
    get_account,
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
    job_progress_ui_interval,
    clamp_progress_ui_interval,
    DEFAULT_PROGRESS_UI_INTERVAL,
    MIN_PROGRESS_UI_INTERVAL,
    MAX_PROGRESS_UI_INTERVAL,
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

    # Pre-index running: Cancel + Delete + Refresh
    if _is_preindexing(job):
        buttons.append(
            [
                InlineKeyboardButton("⏹ Cancel Pre-Index", callback_data=f"job:cancel:{job_id}"),
            ]
        )
        buttons.append(
            [
                InlineKeyboardButton("🗑 Delete Job", callback_data=f"job:delete:{job_id}"),
            ]
        )
        buttons.append(
            [
                InlineKeyboardButton("🔄 Refresh", callback_data=f"job:open:{job_id}"),
                InlineKeyboardButton("« Back", callback_data="job:list"),
            ]
        )
        return InlineKeyboardMarkup(buttons)

    pause_reason = (job.get("pause_reason") or "").strip()
    # Account sleep: job is still "active" — waiting for accounts, will auto-resume.
    # Do NOT show Start (that is only for manual user pause / idle states).
    if status == "paused" and pause_reason == "accounts_unavailable":
        buttons.append(
            [
                InlineKeyboardButton(
                    "⏳ Waiting for accounts (auto-resume)",
                    callback_data="job:noop",
                ),
            ]
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    "🔒 Keep paused (no auto-resume)",
                    callback_data=f"job:pause:{job_id}",
                ),
                InlineKeyboardButton("⏹ Stop", callback_data=f"job:cancel:{job_id}"),
            ]
        )
    elif status == "paused" and pause_reason == "user":
        # Manual pause only → Start
        buttons.append(
            [
                InlineKeyboardButton("▶️ Start", callback_data=f"job:start:{job_id}"),
            ]
        )
        buttons.append(
            [
                InlineKeyboardButton("⏹ Stop", callback_data=f"job:cancel:{job_id}"),
            ]
        )
    elif status in ["pending", "paused", "completed", "failed"]:
        # Other paused reasons / idle: allow Start
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
            InlineKeyboardButton("🎯 Targets", callback_data=f"job:targets:{job_id}"),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton("🆕 Monitor", callback_data=f"job:mon:{job_id}"),
            InlineKeyboardButton("👤 Executor", callback_data=f"job:exec:{job_id}"),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton("📋 Logs", callback_data=f"job:logs:{job_id}"),
        ]
    )
    if status == "running":
        buttons.append(
            [
                InlineKeyboardButton(
                    "⏱ Progress auto-update",
                    callback_data=f"job:pui:{job_id}",
                ),
            ]
        )
    buttons.append(
        [
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

    # While pre-index runs: ONLY show index progress
    if _is_preindexing(job):
        return _preindex_detail_text(job)

    stats = job.get("stats") or {}
    last = int(job.get("last_msg_id") or 0)
    skip = int(job.get("skip") or 0)
    # Cursor = high-water of progress (never below job creation skip)
    cur = max(
        int(job.get("high_water_msg_id") or 0),
        int(job.get("current_msg_id") or 0),
        skip,
    )
    # Always measure % from the skip the user set at job creation → last_msg_id
    # (same formula for historical forward and new-post monitoring)
    range_base = skip
    range_span = max(0, last - skip)
    cursor_done = max(0, cur - skip)
    if range_span > 0:
        cursor_pct = int(round(100.0 * cursor_done / range_span))
    else:
        cursor_pct = 100 if last and cur >= last else 0
    cursor_pct = max(0, min(100, cursor_pct))

    future = bool(job.get("future_new_posts"))
    status = (job.get("status") or "pending").lower()
    try:
        from core.job_worker import historical_range_complete
        hist_done = historical_range_complete(job)
    except Exception:
        hist_done = cur >= last and last > 0
    monitoring = status == "running" and future and hist_done and cur >= last
    icon = status_icon(status)

    fetched = int(stats.get("fetched") or 0)
    fwd = int(stats.get("forwarded") or 0)
    skip_f = int(stats.get("skipped_filter") or 0)
    skip_d = int(stats.get("skipped_deleted") or 0)
    skip_dup = int(stats.get("skipped_duplicate") or 0)
    errs = int(stats.get("errors") or 0)

    # Accurate speed + ETA (current window / active-time average / peak)
    try:
        from database import compute_job_speed_eta
        sp = compute_job_speed_eta(job)
    except Exception:
        sp = {
            "current_mpm": 0.0, "avg_mpm": 0.0, "peak_mpm": 0.0,
            "runtime": 0.0, "eta_seconds": None, "eta_label": "—",
            "monitoring": monitoring,
        }

    # Pause / error reason
    pause_block = ""
    if status == "paused":
        pr = (job.get("pause_reason") or "").strip()
        reason = (
            job.get("error_message")
            or job.get("pause_reason")
            or "Paused"
        )
        reason = str(reason).strip().split("\n")[0][:160]
        if pr == "accounts_unavailable":
            pause_block = (
                "\n⏳ **WAITING FOR ACCOUNTS**\n"
                f"{reason}\n"
                "Job is still active — will **auto-resume** when an account wakes.\n"
                "_Not a manual pause — Start is hidden on purpose._\n"
            )
        elif pr == "user":
            pause_block = (
                "\n⏸ **PAUSED BY YOU**\n"
                "Will **not** auto-resume. Press **Start** when ready.\n"
            )
        else:
            pause_block = (
                "\n⏸ **PAUSED**\n"
                f"Reason: {reason}\n"
            )
    elif status == "failed":
        reason = str(job.get("error_message") or "Failed").strip().split("\n")[0][:160]
        pause_block = f"\n❌ **FAILED**\nReason: {reason}\n"

    # Executor line (labels filled async in open handler when possible;
    # here keep lightweight — detail uses job_executor_detail_text)
    if (job.get("method") or "").lower() == "bot":
        actor = f"🤖 Bot selected"
    else:
        n_acc = len(job.get("account_ids") or [])
        actor = f"👤 Accounts: `{n_acc}`"

    # Current target
    targets = list(job.get("target_chat_ids") or [])
    t_idx = int(job.get("current_target_index") or 0)
    n_t = len(targets)
    if n_t == 0:
        target_line = "🎯 Target: `—`"
    elif status == "completed" or t_idx >= n_t:
        target_line = f"🎯 Targets: `{n_t}` · all done"
    else:
        target_line = f"🎯 Target: `{t_idx + 1} / {n_t}` (active)"

    bar = _progress_bar(cursor_pct)
    runtime = float(sp.get("runtime") or 0)
    runtime_s = fmt_duration(runtime) if runtime > 0 else "—"
    cur_s = f"{sp['current_mpm']:.1f}" if sp.get("current_mpm") else "—"
    avg_s = f"{sp['avg_mpm']:.1f}" if sp.get("avg_mpm") else "—"
    peak_s = f"{sp['peak_mpm']:.1f}" if sp.get("peak_mpm") else "—"
    if sp.get("eta_label") == "Calculating…":
        eta_s = "Calculating…"
    elif sp.get("eta_seconds") is not None:
        eta_s = f"~{fmt_duration(sp['eta_seconds'])}"
    else:
        eta_s = sp.get("eta_label") or "—"

    if monitoring:
        mon_s = "🟢 LIVE"
    elif future and status == "running" and hist_done and cur < last:
        mon_s = "⏳ New posts"
    elif future and status == "running" and not hist_done:
        mon_s = "⏳ Historical"
    elif future and status == "running":
        mon_s = "⏳ Catching up"
    else:
        mon_s = "⚪ Idle"
    # Heartbeat: when this progress view was last refreshed
    updated_s = "—"
    try:
        from datetime import datetime, timezone
        ts = job.get("progress_ui_last_at") or job.get("updated_at") or job.get("progress_ui_bound_at")
        if ts is not None:
            if getattr(ts, "tzinfo", None) is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = int((datetime.now(timezone.utc) - ts).total_seconds())
            if age < 5:
                updated_s = "just now"
            elif age < 60:
                updated_s = f"{age}s ago"
            elif age < 3600:
                updated_s = f"{age // 60}m ago"
            else:
                updated_s = f"{age // 3600}h ago"
    except Exception:
        updated_s = "—"
    pre_line = ""
    if job.get("pre_index_target_duplicates"):
        pre_line = _preindex_progress_line(job)

    start_id = (skip + 1) if last else skip
    return (
        f"**📋 JOB** `{str(job.get('job_id') or '')[:8]}`\n"
        f"{'━' * 24}\n"
        f"**{job.get('name') or 'Job'}**\n\n"
        f"📤 **Source:** {job.get('source_title')}\n"
        f"`{job.get('source_chat_id')}`\n"
        f"{target_line}\n"
        f"⚙️ **Method:** `{job.get('method')}` · {actor}\n"
        f"**Status:** {icon} `{status}`\n"
        f"🔄 Updated: `{updated_s}`\n"
        f"{pause_block}"
        f"{pre_line}"
        f"\n📊 **CURSOR PROGRESS** (message IDs)\n"
        f"Range: `#{start_id:,}` → `#{last:,}`\n"
        f"Cursor: `#{cur:,}`\n"
        f"`{bar}` **≈ {cursor_pct}%**\n"
        f"_ID range progress — not exact message count_\n\n"
        f"📨 Fetched: `{fetched:,}`\n"
        f"📤 Forwarded: `{fwd:,}`\n"
        f"⏭ Skipped: `{skip_f + skip_d:,}` · ♻️ Dup: `{skip_dup:,}` · ❌ `{errs:,}`\n\n"
        f"⚡ **Speed** (msg/min)\n"
        f"Current: `{cur_s}` · Avg: `{avg_s}` · Peak: `{peak_s}`\n"
        f"_Current = last 60s · Avg = active forward time (sleeps excluded)_\n\n"
        f"⏱ Runtime: `{runtime_s}`\n"
        f"🕐 ETA: `{eta_s}`\n"
        f"_ETA uses current rate when stable, else average · remaining ID units_\n\n"
        f"🆕 Future: `{'ON' if future else 'OFF'}` · Monitor: `{mon_s}`\n"
        f"⏱ Interval: `{fmt_interval(job_monitor_interval(job))}`"
    )



def job_monitor_text(job: dict) -> str:
    from datetime import datetime, timezone, timedelta
    from core.job_worker import historical_range_complete

    future = bool(job.get("future_new_posts"))
    status = (job.get("status") or "").lower()
    last = int(job.get("last_msg_id") or 0)
    cur = int(job.get("current_msg_id") or job.get("skip") or 0)
    interval = job_monitor_interval(job)
    last_at = job.get("last_monitor_at")
    next_at = job.get("next_monitor_at")
    detected = job.get("last_detected_msg_id")
    new_fwd = int(job.get("new_posts_forwarded") or 0)
    hist_done = historical_range_complete(job)

    mon_label = "⚪ Idle"
    phase = "Off"
    now = datetime.now(timezone.utc)
    la = _as_utc_dt(last_at)

    if not future:
        mon_label = "⚪ Disabled"
        phase = "Future posts OFF — monitoring will not run"
    elif status in ("failed", "cancelled"):
        mon_label = "🔴 Stopped"
        phase = status
    elif status == "paused":
        mon_label = "⏸ Paused"
        phase = "Paused — monitoring paused until you Start again"
    elif status == "running" and not hist_done:
        mon_label = "⏳ Historical"
        phase = "Historical range still in progress — monitoring starts only after it finishes"
    elif status == "running" and future and hist_done:
        phase = "LIVE — watching for new posts (job stays Running)"
        if la is None:
            mon_label = "🟢 Starting"
        else:
            lag = (now - la).total_seconds()
            if lag > max(interval * 2.5, interval + 30):
                mon_label = "🟡 Delayed"
                phase = f"Last check {fmt_duration(lag)} ago (expected ≤ {fmt_interval(interval)})"
            else:
                mon_label = "🟢 LIVE"
        if next_at is None and la is not None:
            next_at = la + timedelta(seconds=interval)
    elif status in ("pending", "completed"):
        mon_label = "⚪ Idle"
        phase = (
            "Completed (monitoring was OFF)"
            if status == "completed"
            else "Not running — start the job first"
        )

    lines = [
        "**🆕 FUTURE POST MONITORING**",
        "━" * 24,
        f"**Job:** {job.get('name')}",
        f"**Enabled:** `{'🟢 ON' if future else '⚪ OFF'}`",
        f"**State:** {mon_label}",
        f"**Phase:** {phase}",
        "",
        f"⏱ Interval: **{fmt_interval(interval)}**",
        f"🕐 Last check: `{fmt_dt(last_at)}`",
        f"⏭ Next check: `{fmt_dt(next_at)}`",
        f"📍 Cursor: `#{cur:,}` · Range end: `#{last:,}`",
        f"📡 Last detected: `#{detected if detected is not None else '—'}`",
        f"📤 New posts forwarded: `{new_fwd:,}`",
        "",
        "_Rules: job must be **Running**, monitoring **ON**, and historical range **finished**._",
    ]
    return chr(10).join(lines)



async def job_executor_detail_text(user_id: int, job: dict) -> str:
    """Bot or account executors for this job — status from DB."""
    method = (job.get("method") or "").lower()
    lines = [
        "**👤 EXECUTOR**",
        "━" * 24,
        f"**Job:** {job.get('name')}",
        f"**Method:** `{method or '—'}`",
        "",
    ]
    from handlers.ui import format_account_label, format_bot_label

    if method == "bot":
        bot_id = job.get("bot_id")
        bot = await get_bot(user_id, bot_id) if bot_id else None
        if not bot:
            lines.append(f"🤖 Bot `{str(bot_id or '-')[:12]}`")
            lines.append("🔴 Missing or not found")
        else:
            st = (bot.get("status") or "unknown").lower()
            icon = "🟢" if st == "active" else ("⚪" if st in ("disabled", "inactive") else "🟡")
            lines.append(f"🤖 **{format_bot_label(bot)}**")
            uname = bot.get("bot_username")
            if uname:
                lines.append(f"Username: `@{str(uname).lstrip('@')}`")
            lines.append(f"Status: {icon} `{st}`")
            if bot.get("error_message"):
                lines.append(f"Error: {str(bot.get('error_message'))[:120]}")
            if bot.get("last_used_at"):
                lines.append(f"Last used: `{fmt_dt(bot.get('last_used_at'))}`")
    else:
        account_ids = list(job.get("account_ids") or [])
        lines.append(f"**Linked accounts:** `{len(account_ids)}` (min 1)")
        lines.append("_Add/remove anytime — even while the job is running._")
        lines.append("")
        if not account_ids:
            lines.append("_No accounts linked to this job._")
        else:
            for i, aid in enumerate(account_ids, 1):
                acc = await get_account(user_id, aid)
                if not acc:
                    lines.append(f"{i}. `{str(aid)[:10]}` — 🔴 not found")
                    continue
                st = (acc.get("status") or "unknown").lower()
                if st == "active":
                    icon = "🟢"
                elif st == "sleeping":
                    icon = "🟡"
                elif st in ("disabled", "inactive", "error"):
                    icon = "⚪"
                else:
                    icon = "·"
                lines.append(f"{i}. {icon} **{format_account_label(acc, short=True)}**")
                lines.append(f"   Status: `{st}`")
                lines.append(
                    f"   Cycle: `{int(acc.get('forwarded_count') or 0):,}` / "
                    f"limit `{int(acc.get('forward_limit') or 0):,}`"
                )
                lines.append(f"   Total forwarded: `{int(acc.get('total_forwarded') or 0):,}`")
                if st == "sleeping" and acc.get("sleep_until"):
                    lines.append(f"   Sleep until: `{fmt_dt(acc.get('sleep_until'))}`")
                if acc.get("error_message"):
                    lines.append(f"   Error: {str(acc.get('error_message'))[:100]}")
                if acc.get("last_used_at"):
                    lines.append(f"   Last used: `{fmt_dt(acc.get('last_used_at'))}`")
                lines.append("")
    lines.append("_Status is from DB (not live Telegram ping)._")
    return chr(10).join(lines)


async def job_logs_text(
    job_id: str,
    *,
    level: str = "all",
    page: int = 0,
    page_size: int = 15,
) -> tuple:
    """Returns (text, has_next)."""
    from database import count_job_logs

    lvl = (level or "all").lower()
    if lvl in ("warn",):
        lvl = "warning"
    skip = max(0, int(page)) * page_size
    filt = None if lvl in ("all", "") else lvl
    total = await count_job_logs(job_id, filt)
    logs = await get_job_logs(job_id, limit=page_size + 1, level=filt, skip=skip)
    has_next = len(logs) > page_size
    logs = logs[:page_size]
    title = f"**📋 Job Logs** · `{lvl}` · page {page + 1}"
    if total:
        title += f" · `{total}` total"
    if not logs:
        return f"{title}\n\nNo logs for this filter.", False
    lines = [title, ""]
    for row in reversed(logs):
        ts = fmt_dt(row.get("created_at"))
        level_r = (row.get("level") or "info").lower()
        icon = {"info": "✅", "warning": "⚠️", "warn": "⚠️", "error": "❌"}.get(level_r, "•")
        msg = (row.get("message") or "")[:100]
        lines.append(f"`{ts}` {icon} {msg}")
    return "\n".join(lines), has_next



def job_stats_detail_text(job: dict) -> str:
    """Detailed counters — separate from cursor progress."""
    if _is_preindexing(job):
        return _preindex_detail_text(job)

    stats = job.get("stats") or {}
    last = int(job.get("last_msg_id") or 0)
    skip = int(job.get("skip") or 0)
    cur = int(job.get("current_msg_id") or skip or 0)
    fetched = int(stats.get("fetched") or 0)
    fwd = int(stats.get("forwarded") or 0)
    skip_f = int(stats.get("skipped_filter") or 0)
    skip_d = int(stats.get("skipped_deleted") or 0)
    skip_dup = int(stats.get("skipped_duplicate") or 0)
    errs = int(stats.get("errors") or 0)
    success_rate = (100.0 * fwd / fetched) if fetched > 0 else 0.0
    status = (job.get("status") or "").lower()

    try:
        from database import compute_job_speed_eta
        sp = compute_job_speed_eta(job)
    except Exception:
        sp = {
            "current_mpm": 0, "avg_mpm": 0, "peak_mpm": 0,
            "runtime": 0, "eta_seconds": None, "eta_label": "—",
            "remain_units": max(0, last - cur), "rate_source": "none",
        }

    if sp.get("eta_label") == "Calculating…":
        eta_s = "Calculating…"
    elif sp.get("eta_seconds") is not None:
        eta_s = f"~{fmt_duration(sp['eta_seconds'])}"
    else:
        eta_s = sp.get("eta_label") or "—"

    cur_s = f"{sp['current_mpm']:.1f}" if sp.get("current_mpm") else "—"
    avg_s = f"{sp['avg_mpm']:.1f}" if sp.get("avg_mpm") else "—"
    peak_s = f"{sp['peak_mpm']:.1f}" if sp.get("peak_mpm") else "—"
    runtime = float(sp.get("runtime") or 0)

    return (
        f"**📊 JOB STATISTICS**\n"
        f"{'━' * 24}\n"
        f"**{job.get('name')}** · `{status}`\n\n"
        f"**SOURCE RANGE (IDs)**\n"
        f"Start: `#{skip + 1 if last else skip:,}`\n"
        f"End: `#{last:,}`\n"
        f"Cursor: `#{cur:,}`\n"
        f"Remaining ID units: `{int(sp.get('remain_units') or 0):,}` "
        f"(current target + queued targets)\n\n"
        f"**COUNTERS**\n"
        f"Fetched: `{fetched:,}`\n"
        f"Forwarded: `{fwd:,}`\n"
        f"Filter skipped: `{skip_f:,}`\n"
        f"Deleted skipped: `{skip_d:,}`\n"
        f"Duplicate: `{skip_dup:,}`\n"
        f"Errors: `{errs:,}`\n"
        f"Success rate: `{success_rate:.1f}%` (fwd/fetched)\n\n"
        f"**PERFORMANCE** (msg/min)\n"
        f"Current (60s): `{cur_s}`\n"
        f"Average (active time): `{avg_s}`\n"
        f"Peak: `{peak_s}`\n"
        f"ETA source: `{sp.get('rate_source') or '—'}`\n"
        f"Runtime: `{fmt_duration(runtime) if runtime else '—'}`\n"
        f"ETA: `{eta_s}`\n"
        f"Started: `{fmt_dt(job.get('started_at'))}`\n"
        f"Last forward: `{fmt_dt(job.get('last_forward_at'))}`\n"
    )


def job_targets_detail_text(job: dict) -> str:
    """Per-target sequencing from current_target_index (shared cursor window)."""
    targets = list(job.get("target_chat_ids") or [])
    t_idx = int(job.get("current_target_index") or 0)
    last = int(job.get("last_msg_id") or 0)
    skip = int(job.get("skip") or 0)
    cur = int(job.get("current_msg_id") or skip or 0)
    status = (job.get("status") or "pending").lower()
    stats = job.get("stats") or {}
    fwd = int(stats.get("forwarded") or 0)
    skip_dup = int(stats.get("skipped_duplicate") or 0)
    errs = int(stats.get("errors") or 0)

    lines = [
        f"**🎯 TARGETS** ({len(targets)})",
        "━" * 24,
        f"Job: **{job.get('name')}**",
        f"Shared range: `#{skip + 1 if last else 0:,}` → `#{last:,}`",
        f"Cursor: `#{cur:,}`",
        "",
    ]
    if not targets:
        lines.append("_No targets configured._")
    else:
        for i, tid in enumerate(targets):
            if status == "completed" or (status != "pending" and i < t_idx):
                label = "✅ COMPLETED"
            elif status in ("running", "indexing", "paused") and i == t_idx:
                label = "🟢 RUNNING" if status == "running" else (
                    "🔍 INDEXING" if status == "indexing" else "⏸ PAUSED"
                )
            elif i > t_idx or status in ("pending",):
                label = "⏳ WAITING"
            else:
                label = f"`{status}`"
            # Per-target counters not stored separately — show shared stats only on active
            extra = ""
            if i == t_idx and status in ("running", "paused", "indexing"):
                extra = f"\nCursor `#{cur:,}` / `#{last:,}` · 📤 {fwd:,} · ♻️ {skip_dup:,} · ❌ {errs:,}"
            elif i < t_idx:
                extra = "\n_Finished this target window_"
            lines.append(f"**{i + 1}.** `{tid}`\n{label}{extra}\n")

    lines.append(
        "_Note: one cursor window is applied per target in sequence._"
    )
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
                    f"🔍 Pre-Index Target Dupes: {'ON' if bool(state.get('pre_index_target_duplicates')) else 'OFF'}",
                    callback_data="jobcreate:toggle_preindex",
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




def _progress_bar(pct: int, width: int = 12) -> str:
    pct = max(0, min(100, int(pct or 0)))
    filled = int(round(width * pct / 100))
    return "█" * filled + "░" * (width - filled)


def _as_utc_dt(dt):
    from datetime import datetime, timezone
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except Exception:
            return None
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _is_preindexing(job: dict) -> bool:
    st = (job.get("status") or "").lower()
    # Cancelled / failed / completed must never look like active pre-index
    if st in ("cancelled", "failed", "completed", "paused"):
        return False
    pst = (job.get("pre_index_status") or "").lower()
    if pst in ("cancelled", "failed", "done"):
        return False
    return bool(
        job.get("pre_index_target_duplicates")
        and (st == "indexing" or pst == "running")
    )


def _preindex_progress_line(job: dict) -> str:
    if not job.get("pre_index_target_duplicates"):
        return "🔍 Pre-Index: `OFF`\n"
    st = job.get("pre_index_status") or "—"
    n = int(job.get("pre_index_count") or 0)
    if st == "running" or (job.get("status") or "") == "indexing":
        pct = int(job.get("pre_index_progress_pct") or 0)
        return f"🔍 Pre-Index: `running` **{pct}%** · ids `{n}`\n"
    if st == "done":
        return f"🔍 Pre-Index: `done` · **{n}** media IDs\n"
    if st == "failed":
        err = (job.get("pre_index_error") or job.get("error_message") or "")[:80]
        return f"🔍 Pre-Index: `failed` · {err}\n"
    return f"🔍 Pre-Index: `{st}` · ids `{n}`\n"


def _preindex_detail_text(job: dict) -> str:
    """Only pre-index progress — used while indexing is active."""
    from datetime import datetime, timezone

    pct = int(job.get("pre_index_progress_pct") or 0)
    scanned = int(job.get("pre_index_scanned") or 0)
    total = int(job.get("pre_index_total_estimate") or 0)
    rem = int(job.get("pre_index_remaining") or max(0, total - scanned))
    td = int(job.get("pre_index_target_done") or 0)
    tt = int(job.get("pre_index_target_total") or max(1, len(job.get("target_chat_ids") or [])))
    n_ids = int(job.get("pre_index_count") or 0)
    cur_tid = job.get("pre_index_current_target_id")
    method = (job.get("method") or "?").lower()
    actor = (
        "🤖 Bot"
        if method == "bot"
        else f"👤 User account ({len(job.get('account_ids') or [])})"
    )

    now = datetime.now(timezone.utc)
    st = _as_utc_dt(job.get("pre_index_started_at")) or _as_utc_dt(job.get("started_at"))
    runtime = max(0.0, (now - st).total_seconds()) if st else 0.0
    speed = (scanned / runtime) if runtime > 1 and scanned > 0 else 0.0
    eta = (rem / speed) if speed > 0 and rem > 0 else None

    bar = _progress_bar(pct)
    speed_s = f"{speed:.1f} msg/s" if speed > 0 else "—"
    runtime_s = fmt_duration(runtime) if runtime else "—"
    if eta is not None:
        eta_s = fmt_duration(eta)
    elif scanned > 0:
        eta_s = "Calculating…"
    else:
        eta_s = "—"
    cur_disp = min(tt, td + 1) if td < tt else tt
    tline = f"**Targets:** `{cur_disp} / {tt}` completed-or-active"
    if cur_tid:
        tline += f"\n**Current chat:** `{cur_tid}`"

    return (
        f"**🔍 Pre-Index Target Duplicates**\n\n"
        f"**Job:** {job.get('name')}\n"
        f"**Via:** {actor}\n"
        f"**Status:** `indexing`\n\n"
        f"`{bar}` **{pct}%**\n\n"
        f"{tline}\n"
        f"**Scanned msgs:** `{scanned:,}` / `{total:,}`\n"
        f"**Remaining:** `{rem:,}`\n"
        f"**Unique media IDs:** `{n_ids:,}`\n\n"
        f"⚡ Speed: `{speed_s}`\n"
        f"⏱ Runtime: `{runtime_s}`\n"
        f"🕐 ETA: `{eta_s}`\n\n"
        f"_Indexes **every** selected target, one after another._\n"
        f"_Forwarding starts after all targets finish._\n"
        f"_Tap Refresh to update progress._"
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
        f"**Future New Posts:** `{'ON' if state.get('future_new_posts') else 'OFF'}`\n**Pre-Index Target Dupes:** `{'ON' if state.get('pre_index_target_duplicates') else 'OFF'}`\n\n"
        "Skip ka matlab Quick Forward jaisa: pehle N messages skip, uske baad last ID tak.\n"
        "Tap **Skip** to change. Send only a number like `0` or `200`."
    )


def show_confirm(state: dict):
    state["step"] = "confirm"
    state.setdefault("skip", 0)
    state.setdefault("future_new_posts", False)
    state.setdefault("pre_index_target_duplicates", False)


async def show_jobs_list(
    client: Client,
    query: CallbackQuery,
    page: int = 0,
    status_filter: str = "all",
):
    user_id = query.from_user.id
    sf = (status_filter or "all").lower()
    if sf in ("all", ""):
        jobs = await get_user_jobs(user_id, limit=80)
    else:
        jobs = await get_user_jobs(user_id, status=sf, limit=80)
    if not jobs:
        text = (
            f"**📋 Your Jobs** (`{sf}`)\n\n"
            "No jobs in this filter.\n"
            "Create a job or use **Quick Forward** from the dashboard."
        )
        kb = jobs_list_keyboard([], status_filter=sf)
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
        st = (j.get("status") or "").lower()
        if st == "indexing":
            pct = int(j.get("pre_index_pct") or 0)
            extra = f"🔍 Pre-index `{pct}%`"
        else:
            extra = f"Cursor `#{cur:,}` / `#{last:,}`"
        lines.append(
            f"{icon} **{name}**\n"
            f"{extra} · `{j.get('method')}` · 🆕 {future}\n"
            f"📤 `{int(s.get('forwarded') or 0):,}` · ❌ `{int(s.get('errors') or 0):,}`"
        )
    text = "\n".join(lines)
    kb = jobs_list_keyboard(slice_, status_filter=sf)
    # inject pager
    rows = list(kb.inline_keyboard)
    pager = pager_row(f"job:listp:{sf}:", page, total_pages)
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

    if data == "job:list" or data.startswith("job:list:"):
        parts = data.split(":")
        sf = parts[2] if len(parts) > 2 else "all"
        await show_jobs_list(client, query, status_filter=sf)
        return

    if data.startswith("job:listp:"):
        parts = data.split(":")
        # job:listp:{filter}:{page} or job:listp:{page}
        try:
            if len(parts) >= 4:
                sf = parts[2]
                page = int(parts[3])
            else:
                sf = "all"
                page = int(parts[2])
        except Exception:
            sf, page = "all", 0
        await show_jobs_list(client, query, page=page, status_filter=sf)
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
        # Bind this message for periodic progress auto-update while running
        if (job.get("status") or "").lower() == "running":
            try:
                msg = query.message
                await update_job(user_id, job_id, {
                    "progress_chat_id": msg.chat.id,
                    "progress_message_id": msg.id,
                    "progress_ui_bound_at": __import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc
                    ),
                })
                job = await get_job_scoped(user_id, job_id) or job
            except Exception:
                pass
        text = job_detail_text(job)
        if (job.get("status") or "").lower() == "running":
            from handlers.ui import fmt_interval
            text += (
                f"\n\n⏱ **Progress auto-update:** "
                f"`{fmt_interval(job_progress_ui_interval(job))}` "
                f"(while this screen is bound · 5m–1d)"
            )
        await safe_edit(query, text, job_controls_keyboard(job))
        return await safe_answer(query)

    if data.startswith("job:mon:"):
        job_id = data.split(":")[2]
        job = await get_job_scoped(user_id, job_id)
        if not job:
            return await safe_answer(query, "Job not found", True)
        await safe_edit(query, job_monitor_text(job), job_monitor_keyboard(job))
        return await safe_answer(query)

    if data.startswith("job:exec:"):
        job_id = data.split(":")[2]
        job = await get_job_scoped(user_id, job_id)
        if not job:
            return await safe_answer(query, "Job not found", True)
        text = await job_executor_detail_text(user_id, job)
        rows = []
        method = (job.get("method") or "").lower()
        if method in ("user", "user_account"):
            rows.append([InlineKeyboardButton(
                "➕ Add account", callback_data=f"job:accadd:{job_id}",
            )])
            account_ids = list(job.get("account_ids") or [])
            if len(account_ids) > 1:
                rows.append([InlineKeyboardButton(
                    "➖ Remove account", callback_data=f"job:accrmlist:{job_id}",
                )])
            rows.append([InlineKeyboardButton(
                "ℹ️ Min 1 account required", callback_data="job:noop",
            )])
        rows.append([InlineKeyboardButton("🔄 Refresh", callback_data=f"job:exec:{job_id}")])
        rows.append([InlineKeyboardButton("« Back", callback_data=f"job:open:{job_id}")])
        await safe_edit(query, text, InlineKeyboardMarkup(rows))
        return await safe_answer(query)

    if data == "job:noop":
        return await safe_answer(query, "At least 1 user account must stay on the job", True)

    # ── Add account to job (user method only) ──
    if data.startswith("job:accadd:"):
        job_id = data.split(":")[2]
        job = await get_job_scoped(user_id, job_id)
        if not job:
            return await safe_answer(query, "Job not found", True)
        if (job.get("method") or "").lower() not in ("user", "user_account"):
            return await safe_answer(query, "Only for user-account jobs", True)
        from database import get_user_accounts
        from handlers.ui import active_accounts_only, format_account_label
        linked = {str(a) for a in (job.get("account_ids") or [])}
        pool = [
            a for a in active_accounts_only(await get_user_accounts(user_id))
            if str(a.get("account_id") or "") not in linked
        ]
        if not pool:
            return await safe_answer(query, "No more active accounts to add", True)
        ids = [str(a.get("account_id")) for a in pool[:25]]
        set_state(client, "job_acc_state", user_id, {
            "job_id": job_id, "mode": "add", "ids": ids,
        })
        rows = []
        for i, a in enumerate(pool[:25]):
            rows.append([InlineKeyboardButton(
                f"➕ {format_account_label(a, short=True)}",
                callback_data=f"job:accpick:{i}",
            )])
        rows.append([InlineKeyboardButton("« Back", callback_data=f"job:exec:{job_id}")])
        await safe_edit(
            query,
            f"**➕ Add account to job**\n\n"
            f"**{job.get('name') or job_id}**\n"
            f"Currently linked: `{len(linked)}`\n\n"
            "Tap an account to add it (works while job is running).",
            InlineKeyboardMarkup(rows),
        )
        return await safe_answer(query)

    if data.startswith("job:accrmlist:"):
        job_id = data.split(":")[2]
        job = await get_job_scoped(user_id, job_id)
        if not job:
            return await safe_answer(query, "Job not found", True)
        if (job.get("method") or "").lower() not in ("user", "user_account"):
            return await safe_answer(query, "Only for user-account jobs", True)
        account_ids = [str(a) for a in (job.get("account_ids") or [])]
        if len(account_ids) <= 1:
            return await safe_answer(query, "At least 1 account must remain", True)
        from database import get_account
        from handlers.ui import format_account_label
        set_state(client, "job_acc_state", user_id, {
            "job_id": job_id, "mode": "rm", "ids": account_ids,
        })
        rows = []
        for i, aid in enumerate(account_ids):
            acc = await get_account(user_id, aid)
            label = format_account_label(acc, short=True) if acc else aid[:10]
            # cannot remove if only 1 left — list still shows all; pick enforces
            rows.append([InlineKeyboardButton(
                f"➖ {label}",
                callback_data=f"job:accpick:{i}",
            )])
        rows.append([InlineKeyboardButton("« Back", callback_data=f"job:exec:{job_id}")])
        await safe_edit(
            query,
            f"**➖ Remove account from job**\n\n"
            f"**{job.get('name') or job_id}**\n"
            f"Linked: `{len(account_ids)}` (minimum 1)\n\n"
            "Tap to remove. Job can keep running with the remaining accounts.",
            InlineKeyboardMarkup(rows),
        )
        return await safe_answer(query)

    if data.startswith("job:accpick:"):
        try:
            idx = int(data.split(":")[2])
        except Exception:
            return await safe_answer(query, "Invalid", True)
        st = get_state(client, "job_acc_state", user_id) or {}
        job_id = st.get("job_id")
        mode = st.get("mode")
        ids = list(st.get("ids") or [])
        if not job_id or mode not in ("add", "rm") or idx < 0 or idx >= len(ids):
            return await safe_answer(query, "Selection expired — open Executor again", show_alert=True)
        job = await get_job_scoped(user_id, job_id)
        if not job:
            return await safe_answer(query, "Job not found", True)
        if (job.get("method") or "").lower() not in ("user", "user_account"):
            return await safe_answer(query, "Only for user-account jobs", True)
        chosen = str(ids[idx])
        current = [str(a) for a in (job.get("account_ids") or [])]
        if mode == "add":
            if chosen in current:
                return await safe_answer(query, "Already on this job", True)
            current.append(chosen)
            await update_job(user_id, job_id, {"account_ids": current})
            set_state(client, "job_acc_state", user_id, None)
            await safe_answer(query, f"Account added ({len(current)} total)", True)
        else:
            if len(current) <= 1:
                return await safe_answer(query, "At least 1 account must remain", True)
            if chosen not in current:
                return await safe_answer(query, "Not on this job", True)
            current = [a for a in current if a != chosen]
            if not current:
                return await safe_answer(query, "At least 1 account must remain", True)
            await update_job(user_id, job_id, {"account_ids": current})
            set_state(client, "job_acc_state", user_id, None)
            await safe_answer(query, f"Account removed ({len(current)} left)", True)
        # Refresh executor screen
        job = await get_job_scoped(user_id, job_id)
        text = await job_executor_detail_text(user_id, job)
        rows = []
        rows.append([InlineKeyboardButton("➕ Add account", callback_data=f"job:accadd:{job_id}")])
        if len(job.get("account_ids") or []) > 1:
            rows.append([InlineKeyboardButton("➖ Remove account", callback_data=f"job:accrmlist:{job_id}")])
        rows.append([InlineKeyboardButton("🔄 Refresh", callback_data=f"job:exec:{job_id}")])
        rows.append([InlineKeyboardButton("« Back", callback_data=f"job:open:{job_id}")])
        await safe_edit(query, text, InlineKeyboardMarkup(rows))
        return

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


    if data.startswith("job:pui:"):
        parts = data.split(":")
        # job:pui:set:<job_id>:<seconds>
        if len(parts) >= 5 and parts[2] == "set":
            job_id = parts[3]
            from database import OWNER_ONLY_PROGRESS_UI
            from core.access import is_owner, is_admin as access_is_admin
            _priv = is_owner(user_id) or await access_is_admin(user_id)
            try:
                raw_sec = int(str(parts[4]).strip())
            except Exception:
                return await safe_answer(query, "Invalid", True)
            if raw_sec in OWNER_ONLY_PROGRESS_UI and not _priv:
                return await safe_answer(
                    query,
                    "1 min / 2 min only for owner & admins",
                    True,
                )
            try:
                seconds = clamp_progress_ui_interval(raw_sec, allow_fast=_priv)
            except Exception:
                return await safe_answer(query, "Invalid", True)
            await update_job(user_id, job_id, {"progress_ui_interval_seconds": seconds})
            from handlers.ui import fmt_interval
            await safe_answer(query, f"Progress update → {fmt_interval(seconds)}", True)
            job = await get_job_scoped(user_id, job_id)
            if not job:
                return
            # show settings again
            data = f"job:pui:{job_id}"
            parts = data.split(":")
        # job:pui:custom:<job_id>
        if len(parts) >= 4 and parts[2] == "custom":
            job_id = parts[3]
            set_state(client, "job_progress_ui_state", user_id, {"job_id": job_id})
            await safe_edit(
                query,
                "**⏱ Custom progress auto-update**\n\n"
                "Send interval in **minutes**.\n"
                "Example: `90` = 1.5 hours\n\n"
                f"Allowed: **5 minutes** – **1 day** for normal users.\n"
                "Owner/admin may use **1** or **2** minutes.\n"
                "/cancel to go back.",
            )
            return await safe_answer(query)
        # job:pui:<job_id>
        job_id = parts[2]
        job = await get_job_scoped(user_id, job_id)
        if not job:
            return await safe_answer(query, "Job not found", True)
        from handlers.ui import fmt_interval
        cur = job_progress_ui_interval(job)
        from core.access import is_owner, is_admin as access_is_admin
        is_priv = is_owner(user_id) or await access_is_admin(user_id)
        presets = []
        if is_priv:
            presets += [(60, "1m"), (120, "2m")]
        presets += [
            (5 * 60, "5m"),
            (10 * 60, "10m"),
            (30 * 60, "30m"),
            (60 * 60, "1h"),
            (3 * 3600, "3h"),
            (6 * 3600, "6h"),
            (12 * 3600, "12h"),
            (24 * 3600, "24h"),
        ]
        rows = []
        row = []
        for sec, lab in presets:
            mark = "✅ " if int(cur) == int(sec) else ""
            row.append(InlineKeyboardButton(
                f"{mark}{lab}",
                callback_data=f"job:pui:set:{job_id}:{sec}",
            ))
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton(
            "✏️ Custom (minutes)",
            callback_data=f"job:pui:custom:{job_id}",
        )])
        rows.append([InlineKeyboardButton("« Back", callback_data=f"job:open:{job_id}")])
        await safe_edit(
            query,
            f"**⏱ Progress auto-update**\n\n"
            f"Job: **{job.get('name') or job_id}**\n"
            f"Current: **{fmt_interval(cur)}**\n\n"
            "While the job is **running** and you opened its progress screen, "
            "the bot edits that message on this interval.\n\n"
            "Min **30 min** (owner/admin: **5/10 min**) · Max **1 day** · Default **30 min**.\n"
            "Works only while this job progress screen stays open in the management bot.",
            InlineKeyboardMarkup(rows),
        )
        return await safe_answer(query)

    if data.startswith("job:logs:"):
        parts = data.split(":")
        job_id = parts[2]
        level = parts[3] if len(parts) > 3 else "all"
        try:
            page = int(parts[4]) if len(parts) > 4 else 0
        except ValueError:
            page = 0
        text, has_next = await job_logs_text(job_id, level=level, page=page)
        await safe_edit(
            query,
            text,
            job_logs_keyboard(job_id, level=level, page=page, has_next=has_next),
        )
        return await safe_answer(query)

    if data.startswith("job:logsclear:"):
        job_id = data.split(":")[2]
        await clear_job_logs(job_id)
        await safe_answer(query, "Logs cleared", True)
        text, has_next = await job_logs_text(job_id, level="all", page=0)
        await safe_edit(
            query, text, job_logs_keyboard(job_id, level="all", page=0, has_next=has_next)
        )
        return

    if data.startswith("job:stats:"):
        job_id = data.split(":")[2]
        job = await get_job_scoped(user_id, job_id)
        if not job:
            return await safe_answer(query, "Job not found", True)
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔄 Refresh", callback_data=f"job:stats:{job_id}")],
                [InlineKeyboardButton("« Back", callback_data=f"job:open:{job_id}")],
            ]
        )
        await safe_edit(query, job_stats_detail_text(job), kb)
        return await safe_answer(query)

    if data.startswith("job:targets:"):
        job_id = data.split(":")[2]
        job = await get_job_scoped(user_id, job_id)
        if not job:
            return await safe_answer(query, "Job not found", True)
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔄 Refresh", callback_data=f"job:targets:{job_id}")],
                [InlineKeyboardButton("« Back", callback_data=f"job:open:{job_id}")],
            ]
        )
        await safe_edit(query, job_targets_detail_text(job), kb)
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
        msg = f"Future New Posts → {'ON' if new_val else 'OFF'}"
        if new_val:
            msg += " (active only while Running + after historical done)"
        return await safe_answer(query, msg, True)

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
        from core.job_worker import pause_running_job
        await pause_running_job(user_id, job_id, reason="user")
        try:
            await add_job_log(job_id, "info", "Paused by user (no auto-resume)")
        except Exception:
            pass
        await safe_answer(query, "Paused — will not auto-resume", True)
        job = await get_job_scoped(user_id, job_id)
        await safe_edit(query, job_detail_text(job), job_controls_keyboard(job))
        return
        

    if data.startswith("job:cancel:"):
        job_id = data.split(":")[2]
        # Stop worker + clear pre-index "running" so UI is not stuck in index-only mode
        try:
            from core.job_worker import cancel_running_job
            await cancel_running_job(user_id, job_id)
        except Exception:
            await set_job_status(user_id, job_id, JobStatus.CANCELLED.value)
        await update_job(
            user_id,
            job_id,
            {
                "status": JobStatus.CANCELLED.value,
                "pre_index_status": "cancelled",
                "pause_reason": None,
            },
        )
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
            from handlers.ui import active_accounts_only
            accounts = active_accounts_only(await get_user_accounts(user_id))
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

    if action == "toggle_preindex":
        state["pre_index_target_duplicates"] = not bool(
            state.get("pre_index_target_duplicates")
        )
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
                pre_index_target_duplicates=bool(state.get("pre_index_target_duplicates")),
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


