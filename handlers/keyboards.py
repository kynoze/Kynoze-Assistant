from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from typing import Dict, Any, List, Optional

from handlers.ui import FEATURE_CATEGORY, PAGE_SIZE, paginate, pager_row


# ============================================================
# DASHBOARD
# ============================================================

def dashboard_keyboard(allowed: dict | None = None, *, is_owner: bool = False) -> InlineKeyboardMarkup:
    """Build dashboard. `allowed` maps feature key → bool. None = all on (admin)."""
    def ok(feat: str) -> bool:
        if allowed is None:
            return True
        return bool(allowed.get(feat, False))

    buttons = []
    # Shared identities
    row = []
    if ok("accounts"):
        row.append(InlineKeyboardButton("👤 My Accounts", callback_data="dash:accounts"))
    if ok("bots"):
        row.append(InlineKeyboardButton("🤖 My Bots", callback_data="dash:bots"))
    if row:
        buttons.append(row)

    # Existing Forward group
    if any(ok(f) for f in ("jobs", "targets", "stats", "quick_forward")):
        buttons.append([InlineKeyboardButton("📂 Existing Forward", callback_data="dash:existing")])

    row = []
    if ok("indexing"):
        row.append(InlineKeyboardButton("📦 Indexing", callback_data="dash:index"))
    if ok("wroxen"):
        row.append(InlineKeyboardButton("🔎 Wroxen Search", callback_data="dash:wroxen"))
    if row:
        buttons.append(row)

    row = []
    if ok("delete_manager"):
        row.append(InlineKeyboardButton("🗑️ Delete Manager", callback_data="dash:delete"))
    if ok("cnl"):
        row.append(InlineKeyboardButton("📡 CNL Auto-Post", callback_data="dash:cnl"))
    if row:
        buttons.append(row)

    buttons.append([InlineKeyboardButton("🗄️ My Databases", callback_data="dash:mydbs")])
    buttons.append([InlineKeyboardButton("🩺 Runtime Health", callback_data="health:home")])
    buttons.append([InlineKeyboardButton("📢 Log Chat", callback_data="log:home")])
    buttons.append([InlineKeyboardButton("🗄️ My Storage", callback_data="dash:storage")])
    if is_owner:
        buttons.append([InlineKeyboardButton("👑 Owner Control", callback_data="own:home")])
    buttons.append([
        InlineKeyboardButton("❓ Help", callback_data="dash:help"),
        InlineKeyboardButton("🔄 Refresh", callback_data="dash:refresh"),
    ])
    if ok("settings"):
        buttons.append([InlineKeyboardButton("⚙️ Settings", callback_data="dash:settings")])
    return InlineKeyboardMarkup(buttons)


def existing_forward_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Jobs", callback_data="dash:jobs")],
        [
            InlineKeyboardButton("🎯 Targets", callback_data="dash:targets"),
            InlineKeyboardButton("📊 Statistics", callback_data="dash:stats"),
        ],
        [InlineKeyboardButton("⚡ Quick Forward", callback_data="dash:quick")],
        [InlineKeyboardButton("« Dashboard", callback_data="dash:home")],
    ])


# ============================================================
# TARGETS
# ============================================================

def targets_list_keyboard(targets: List[Dict]) -> InlineKeyboardMarkup:
    buttons = []
    for t in targets:
        title = (t.get("title") or "Unknown")[:28]
        chat_id = t["chat_id"]
        buttons.append([
            InlineKeyboardButton(
                f"🎯 {title}",
                callback_data=f"tg:open:{chat_id}"
            )
        ])

    buttons.append([
        InlineKeyboardButton("➕ Add Target", callback_data="tg:add"),
        InlineKeyboardButton("🔄 Refresh", callback_data="tg:list")
    ])
    buttons.append([
        InlineKeyboardButton("« Back to Dashboard", callback_data="dash:home")
    ])
    return InlineKeyboardMarkup(buttons)


def target_settings_keyboard(target: Dict[str, Any]) -> InlineKeyboardMarkup:
    """Settings hub — categories, not one giant list."""
    chat_id = target["chat_id"]
    buttons = [
        [InlineKeyboardButton("🎨 Content", callback_data=f"st:cat:{chat_id}:content")],
        [InlineKeyboardButton("🔍 Filters", callback_data=f"st:cat:{chat_id}:filters")],
        [InlineKeyboardButton("⚡ Forwarding", callback_data=f"st:cat:{chat_id}:forward")],
        [InlineKeyboardButton("🆕 Future Posts", callback_data=f"st:cat:{chat_id}:future")],
        [InlineKeyboardButton("👁 View Configuration", callback_data=f"st:view:{chat_id}")],
        [InlineKeyboardButton("♻️ Reset Settings", callback_data=f"st:reset:{chat_id}")],
        [InlineKeyboardButton("« Target", callback_data=f"tg:open:{chat_id}")],
    ]
    return InlineKeyboardMarkup(buttons)


def target_detail_keyboard(target: Dict[str, Any]) -> InlineKeyboardMarkup:
    chat_id = target["chat_id"]
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚙️ Settings", callback_data=f"st:hub:{chat_id}"),
            InlineKeyboardButton("👁 View Config", callback_data=f"st:view:{chat_id}"),
        ],
        [InlineKeyboardButton("📊 Statistics", callback_data=f"tg:stats:{chat_id}")],
        [
            InlineKeyboardButton("🗑 Remove", callback_data=f"tg:delete:{chat_id}"),
            InlineKeyboardButton("🔄 Refresh", callback_data=f"tg:open:{chat_id}"),
        ],
        [InlineKeyboardButton("« Back", callback_data="tg:list")],
    ])


def settings_category_keyboard(target: Dict[str, Any], category: str) -> InlineKeyboardMarkup:
    s = target.get("settings") or {}
    chat_id = target["chat_id"]

    def on_off(key: str, default: bool = False) -> str:
        return "✅ ON" if s.get(key, default) else "❌ OFF"

    if category == "content":
        buttons = [
            [InlineKeyboardButton(f"📝 Caption  {on_off('caption_enabled')}", callback_data=f"st:toggle:{chat_id}:caption_enabled")],
            [InlineKeyboardButton(f"✨ Rich Message  {on_off('rich_message_enabled')}", callback_data=f"st:toggle:{chat_id}:rich_message_enabled")],
            [InlineKeyboardButton("📄 Caption Template", callback_data=f"st:menu:{chat_id}:caption_template")],
            [InlineKeyboardButton(f"🔄 Replacement  {on_off('replace_enabled')}", callback_data=f"st:toggle:{chat_id}:replace_enabled")],
            [InlineKeyboardButton("✏️ Manage Replacements", callback_data=f"st:menu:{chat_id}:replacements")],
            [InlineKeyboardButton(f"🔗 Remove Links  {on_off('remove_links')}", callback_data=f"st:toggle:{chat_id}:remove_links")],
            [InlineKeyboardButton(f"🔘 Inline Buttons  {on_off('inline_buttons_enabled', True)}", callback_data=f"st:toggle:{chat_id}:inline_buttons_enabled")],
            [InlineKeyboardButton("🛠 Manage Inline Buttons", callback_data=f"st:menu:{chat_id}:inline_buttons")],
        ]
    elif category == "filters":
        buttons = [
            [InlineKeyboardButton(f"🚫 Block Words  {on_off('block_words_enabled', True)}", callback_data=f"st:toggle:{chat_id}:block_words_enabled")],
            [InlineKeyboardButton("🛠 Manage Block List", callback_data=f"st:menu:{chat_id}:block_words")],
            [InlineKeyboardButton(f"✅ Whitelist Mode  {on_off('whitelist_mode')}", callback_data=f"st:toggle:{chat_id}:whitelist_mode")],
            [InlineKeyboardButton("📋 Manage Whitelist", callback_data=f"st:menu:{chat_id}:whitelist")],
            [InlineKeyboardButton("🎞 Media Types", callback_data=f"st:menu:{chat_id}:media_types")],
        ]
    elif category == "forward":
        buttons = [
            [InlineKeyboardButton(f"↪️ Forward Tag  {on_off('forward_tag')}", callback_data=f"st:toggle:{chat_id}:forward_tag")],
            [InlineKeyboardButton(f"⏱ Delay  [{s.get('delay', 1.0)}s]", callback_data=f"st:menu:{chat_id}:delay")],
            [InlineKeyboardButton(f"🛡 Anti-Duplicate  {on_off('anti_duplicate', True)}", callback_data=f"st:toggle:{chat_id}:anti_duplicate")],
        ]
    else:
        buttons = [
            [InlineKeyboardButton(
                f"🆕 Future New Posts  {on_off('future_new_posts')}",
                callback_data=f"st:toggle:{chat_id}:future_new_posts",
            )],
            [InlineKeyboardButton(
                "⏱ Interval is set per Job → Jobs → Monitor",
                callback_data=f"st:hub:{chat_id}",
            )],
        ]
    buttons.append([InlineKeyboardButton("« Settings", callback_data=f"st:hub:{chat_id}")])
    return InlineKeyboardMarkup(buttons)


def media_types_keyboard(target: Dict[str, Any]) -> InlineKeyboardMarkup:
    s = target.get("settings", {})
    chat_id = target["chat_id"]
    current = set(s.get("media_types", []))

    all_types = [
        ("photo", "🖼 Photo"),
        ("video", "🎬 Video"),
        ("document", "📄 Document"),
        ("audio", "🎵 Audio"),
        ("sticker", "🏷 Sticker"),
        ("animation", "🎞 Animation"),
        ("voice", "🎤 Voice"),
        ("video_note", "⏺ Video Note"),
        ("text", "📝 Text"),
    ]

    buttons = []
    for media_key, label in all_types:
        mark = "✅" if media_key in current else "❌"
        buttons.append([
            InlineKeyboardButton(
                f"{mark} {label}",
                callback_data=f"st:media:{chat_id}:{media_key}"
            )
        ])

    buttons.append([
        InlineKeyboardButton("« Filters", callback_data=f"st:cat:{chat_id}:filters")
    ])
    return InlineKeyboardMarkup(buttons)


def simple_back_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("« Back to Settings", callback_data=f"st:hub:{chat_id}")]
    ])


def confirm_delete_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, Delete", callback_data=f"tg:confirm_delete:{chat_id}"),
            InlineKeyboardButton("❌ No", callback_data=f"tg:open:{chat_id}")
        ]
    ])


# ============================================================
# ACCOUNTS
# ============================================================

def accounts_list_keyboard(accounts: List[Dict]) -> InlineKeyboardMarkup:
    from handlers.ui import format_account_label

    buttons = []
    for acc in accounts:
        name = format_account_label(acc, short=True)[:40]
        status = acc.get("status", "active")
        status_icon = {
            "active": "🟢",
            "sleeping": "😴",
            "disabled": "🔴",
            "error": "⚠️"
        }.get(status, "⚪")

        buttons.append([
            InlineKeyboardButton(
                f"{status_icon} {name}",
                callback_data=f"acc:open:{acc['account_id']}"
            )
        ])

    buttons.append([
        InlineKeyboardButton("➕ Add Account", callback_data="acc:add"),
        InlineKeyboardButton("🔄 Refresh", callback_data="acc:list")
    ])
    buttons.append([
        InlineKeyboardButton("« Back to Dashboard", callback_data="dash:home")
    ])
    return InlineKeyboardMarkup(buttons)


def account_settings_keyboard(account: Dict[str, Any]) -> InlineKeyboardMarkup:
    account_id = account["account_id"]
    status = account.get("status", "active")
    limit = account.get("forward_limit", 500)
    sleep_min = account.get("sleep_after_limit_minutes", 30)
    forwarded = account.get("forwarded_count", 0)

    status_text = {
        "active": "🟢 Active",
        "sleeping": "😴 Sleeping",
        "disabled": "🔴 Disabled",
        "error": "⚠️ Error"
    }.get(status, status)

    buttons = [
        [InlineKeyboardButton(
            f"Status: {status_text}",
            callback_data=f"acc:toggle_status:{account_id}"
        )],
        [InlineKeyboardButton(
            f"🔢 Forward Limit: {limit}",
            callback_data=f"acc:set_limit:{account_id}"
        )],
        [InlineKeyboardButton(
            f"😴 Sleep After Limit: {sleep_min} min",
            callback_data=f"acc:set_sleep:{account_id}"
        )],
        [InlineKeyboardButton(
            f"📊 This cycle: {forwarded}/{limit}",
            callback_data=f"acc:stats:{account_id}"
        )],
        [
            InlineKeyboardButton("🔄 Test", callback_data=f"acc:test:{account_id}"),
            InlineKeyboardButton("♻️ Reset Cycle", callback_data=f"acc:reset:{account_id}"),
        ],
        [
            InlineKeyboardButton("🗑 Remove", callback_data=f"acc:delete:{account_id}"),
            InlineKeyboardButton("🔄 Refresh", callback_data=f"acc:open:{account_id}"),
        ],
        [InlineKeyboardButton("« Back", callback_data="acc:list")],
    ]
    return InlineKeyboardMarkup(buttons)


def confirm_delete_account_keyboard(account_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, Delete", callback_data=f"acc:confirm_delete:{account_id}"),
            InlineKeyboardButton("❌ No", callback_data=f"acc:open:{account_id}")
        ]
    ])


# ============================================================
# FORWARD BOTS
# ============================================================

def bots_list_keyboard(bots: List[Dict]) -> InlineKeyboardMarkup:
    from handlers.ui import format_bot_label

    buttons = []
    for b in bots:
        name = format_bot_label(b, short=True)[:40]
        status = b.get("status", "active")
        icon = "🟢" if status == "active" else "🔴"

        buttons.append([
            InlineKeyboardButton(
                f"{icon} {name}",
                callback_data=f"bot:open:{b['bot_id']}"
            )
        ])

    buttons.append([
        InlineKeyboardButton("➕ Add Bot", callback_data="bot:add"),
        InlineKeyboardButton("🔄 Refresh", callback_data="bot:list")
    ])
    buttons.append([
        InlineKeyboardButton("« Back to Dashboard", callback_data="dash:home")
    ])
    return InlineKeyboardMarkup(buttons)


def bot_settings_keyboard(bot: Dict[str, Any]) -> InlineKeyboardMarkup:
    bot_id = bot["bot_id"]
    status = bot.get("status", "active")
    total = bot.get("total_forwarded", 0)

    status_text = "🟢 Active" if status == "active" else "🔴 Disabled"

    buttons = [
        [InlineKeyboardButton(
            f"Status: {status_text}",
            callback_data=f"bot:toggle_status:{bot_id}"
        )],
        [InlineKeyboardButton(
            f"📊 Total Forwarded: {total}",
            callback_data=f"bot:stats:{bot_id}"
        )],
        [InlineKeyboardButton("🔄 Test Connection", callback_data=f"bot:test:{bot_id}")],
        [
            InlineKeyboardButton("🗑 Remove", callback_data=f"bot:delete:{bot_id}"),
            InlineKeyboardButton("🔄 Refresh", callback_data=f"bot:open:{bot_id}"),
        ],
        [InlineKeyboardButton("« Back", callback_data="bot:list")],
    ]
    return InlineKeyboardMarkup(buttons)


def confirm_delete_bot_keyboard(bot_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, Delete", callback_data=f"bot:confirm_delete:{bot_id}"),
            InlineKeyboardButton("❌ No", callback_data=f"bot:open:{bot_id}")
        ]
    ])


# ============================================================
# JOBS
# ============================================================

def jobs_list_keyboard(
    jobs: List[Dict],
    *,
    status_filter: str = "all",
) -> InlineKeyboardMarkup:
    buttons = []
    for j in jobs:
        name = (j.get("name") or f"Job {j['job_id'][:6]}")[:28]
        status = j.get("status", "pending")
        icon = {
            "pending": "⏳",
            "running": "🟢",
            "paused": "⏸",
            "completed": "✅",
            "cancelled": "🛑",
            "failed": "❌",
            "indexing": "🔍",
        }.get(status, "⚪")

        buttons.append([
            InlineKeyboardButton(
                f"{icon} {name}",
                callback_data=f"job:open:{j['job_id']}"
            )
        ])

    sf = (status_filter or "all").lower()
    buttons.append([
        InlineKeyboardButton("•All" if sf == "all" else "All", callback_data="job:list:all"),
        InlineKeyboardButton("•Run" if sf == "running" else "Run", callback_data="job:list:running"),
        InlineKeyboardButton("•Pause" if sf == "paused" else "Pause", callback_data="job:list:paused"),
        InlineKeyboardButton("•Done" if sf == "completed" else "Done", callback_data="job:list:completed"),
    ])
    buttons.append([
        InlineKeyboardButton("➕ Create Job", callback_data="job:create"),
        InlineKeyboardButton("🔄 Refresh", callback_data=f"job:list:{sf}"),
    ])
    buttons.append([
        InlineKeyboardButton("📢 Jobs Log Channel", callback_data="jlog:cfg"),
    ])
    buttons.append([
        InlineKeyboardButton("« Back to Dashboard", callback_data="dash:home")
    ])
    return InlineKeyboardMarkup(buttons)


def job_detail_keyboard(job: Dict[str, Any]) -> InlineKeyboardMarkup:
    job_id = job["job_id"]
    status = job.get("status", "pending")

    buttons = []

    if status in ["pending", "paused"]:
        buttons.append([
            InlineKeyboardButton("▶️ Start / Resume", callback_data=f"job:start:{job_id}")
        ])
    if status == "running":
        buttons.append([
            InlineKeyboardButton("⏸ Pause", callback_data=f"job:pause:{job_id}"),
            InlineKeyboardButton("🛑 Cancel", callback_data=f"job:cancel:{job_id}")
        ])

    buttons.append([
        InlineKeyboardButton("📊 Detailed Stats", callback_data=f"job:stats:{job_id}")
    ])
    buttons.append([
        InlineKeyboardButton("🗑 Delete Job", callback_data=f"job:delete:{job_id}"),
        InlineKeyboardButton("« Back", callback_data="job:list")
    ])
    return InlineKeyboardMarkup(buttons)


def confirm_delete_job_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, Delete", callback_data=f"job:confirm_delete:{job_id}"),
            InlineKeyboardButton("❌ No", callback_data=f"job:open:{job_id}")
        ]
    ])


# ============================================================
# JOB CREATION HELPERS
# ============================================================

def select_targets_keyboard(targets: List[Dict], selected: List[int]) -> InlineKeyboardMarkup:
    buttons = []
    for t in targets:
        title = (t.get("title") or "Unknown")[:25]
        chat_id = t["chat_id"]
        mark = "✅" if chat_id in selected else "⬜"
        buttons.append([
            InlineKeyboardButton(
                f"{mark} {title}",
                callback_data=f"jobcreate:toggle_target:{chat_id}"
            )
        ])

    buttons.append([
        InlineKeyboardButton("➡️ Next: Options", callback_data="jobcreate:next_confirm")
    ])
    buttons.append([
        InlineKeyboardButton("❌ Cancel", callback_data="job:list")
    ])
    return InlineKeyboardMarkup(buttons)


def select_method_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 User Accounts", callback_data="jobcreate:method:user")],
        [InlineKeyboardButton("🤖 Forward Bot", callback_data="jobcreate:method:bot")],
        [InlineKeyboardButton("❌ Cancel", callback_data="job:list")]
    ])


def select_accounts_keyboard(accounts: List[Dict], selected: List[str]) -> InlineKeyboardMarkup:
    from handlers.ui import format_account_label, active_accounts_only

    buttons = []
    for acc in active_accounts_only(accounts):
        name = format_account_label(acc, short=True)[:40]
        acc_id = acc["account_id"]
        mark = "✅" if acc_id in selected else "⬜"
        buttons.append([
            InlineKeyboardButton(
                f"{mark} {name}",
                callback_data=f"jobcreate:toggle_account:{acc_id}"
            )
        ])
    if not any(
        (b[0].callback_data or "").startswith("jobcreate:toggle_account:")
        for b in buttons
        if b
    ):
        buttons.append([
            InlineKeyboardButton(
                "No active accounts — enable in My Accounts",
                callback_data="acc:list",
            )
        ])

    buttons.append([
        InlineKeyboardButton("➡️ Next: Select Targets", callback_data="jobcreate:next_targets")
    ])
    buttons.append([
        InlineKeyboardButton("❌ Cancel", callback_data="job:list")
    ])
    return InlineKeyboardMarkup(buttons)


def select_bot_keyboard(bots: List[Dict]) -> InlineKeyboardMarkup:
    from handlers.ui import format_bot_label

    buttons = []
    for b in bots:
        name = format_bot_label(b, short=True)[:40]
        buttons.append([
            InlineKeyboardButton(
                f"🤖 {name}",
                callback_data=f"jobcreate:select_bot:{b['bot_id']}"
            )
        ])

    buttons.append([
        InlineKeyboardButton("❌ Cancel", callback_data="job:list")
    ])
    return InlineKeyboardMarkup(buttons)


def list_manage_keyboard(chat_id: int, feature: str, count: int, page: int = 0) -> InlineKeyboardMarkup:
    cat = FEATURE_CATEGORY.get(feature, "content")
    buttons = [
        [InlineKeyboardButton("➕ Add", callback_data=f"st:add:{chat_id}:{feature}")],
        [
            InlineKeyboardButton("✏️ Edit", callback_data=f"st:edit:{chat_id}:{feature}"),
            InlineKeyboardButton("🗑 Delete", callback_data=f"st:del:{chat_id}:{feature}"),
        ],
        [InlineKeyboardButton("🗑 Delete All", callback_data=f"st:delall:{chat_id}:{feature}")],
    ]
    total_pages = max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE) if count else 1
    pager = pager_row(f"st:menup:{chat_id}:{feature}:", page, total_pages)
    if pager:
        buttons.append(pager)
    buttons.append([InlineKeyboardButton("« Back", callback_data=f"st:cat:{chat_id}:{cat}")])
    return InlineKeyboardMarkup(buttons)


def list_pick_keyboard(chat_id: int, feature: str, labels: list, mode: str, page: int = 0) -> InlineKeyboardMarkup:
    slice_, page, total_pages = paginate(labels, page)
    start = page * PAGE_SIZE
    buttons = []
    for i, label in enumerate(slice_):
        idx = start + i
        shown = (label or "")[:40]
        buttons.append([
            InlineKeyboardButton(
                f"{idx + 1}. {shown}",
                callback_data=f"st:item:{chat_id}:{feature}:{idx}:{mode}",
            )
        ])
    pager = pager_row(f"st:pickp:{chat_id}:{feature}:{mode}:", page, total_pages)
    if pager:
        buttons.append(pager)
    buttons.append([InlineKeyboardButton("« Back", callback_data=f"st:menu:{chat_id}:{feature}")])
    return InlineKeyboardMarkup(buttons)


def confirm_clear_keyboard(chat_id: int, feature: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("❌ Cancel", callback_data=f"st:menu:{chat_id}:{feature}"),
            InlineKeyboardButton("🗑 Confirm", callback_data=f"st:delallok:{chat_id}:{feature}"),
        ]
    ])


def caption_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Edit Template", callback_data=f"st:capedit:{chat_id}")],
        [InlineKeyboardButton("👁 Preview", callback_data=f"st:capprev:{chat_id}")],
        [
            InlineKeyboardButton("♻️ Reset", callback_data=f"st:capreset:{chat_id}"),
            InlineKeyboardButton("❌ Disable", callback_data=f"st:capclear:{chat_id}"),
        ],
        [InlineKeyboardButton("« Content", callback_data=f"st:cat:{chat_id}:content")],
    ])


def reset_settings_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Reset Caption", callback_data=f"st:resetdo:{chat_id}:caption")],
        [InlineKeyboardButton("Reset Filters", callback_data=f"st:resetdo:{chat_id}:filters")],
        [InlineKeyboardButton("Reset Buttons", callback_data=f"st:resetdo:{chat_id}:buttons")],
        [InlineKeyboardButton("Reset Replacements", callback_data=f"st:resetdo:{chat_id}:replacements")],
        [InlineKeyboardButton("⚠️ Reset All Settings", callback_data=f"st:resetall:{chat_id}")],
        [InlineKeyboardButton("« Back", callback_data=f"st:hub:{chat_id}")],
    ])


def confirm_reset_all_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, reset all", callback_data=f"st:resetallok:{chat_id}"),
            InlineKeyboardButton("❌ No", callback_data=f"st:reset:{chat_id}"),
        ]
    ])


def back_settings_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("« Target", callback_data=f"tg:open:{chat_id}")],
        [InlineKeyboardButton("« Settings", callback_data=f"st:hub:{chat_id}")],
    ])


def view_config_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("« Target", callback_data=f"tg:open:{chat_id}")],
        [InlineKeyboardButton("« Settings", callback_data=f"st:hub:{chat_id}")],
    ])


def stats_refresh_keyboard(*extra_rows):
    rows = list(extra_rows)
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="stats:home")])
    rows.append([InlineKeyboardButton("« Back to Dashboard", callback_data="dash:home")])
    return InlineKeyboardMarkup(rows)


def job_monitor_keyboard(job: Dict[str, Any]) -> InlineKeyboardMarkup:
    job_id = job["job_id"]
    future = bool(job.get("future_new_posts"))
    toggle = "⏸ Disable" if future else "▶️ Enable"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏱ Monitoring Interval", callback_data=f"job:int:{job_id}")],
        [InlineKeyboardButton(toggle, callback_data=f"job:toggle_future:{job_id}")],
        [InlineKeyboardButton("🔄 Refresh", callback_data=f"job:mon:{job_id}")],
        [InlineKeyboardButton("« Job", callback_data=f"job:open:{job_id}")],
    ])


def job_interval_keyboard(job: Dict[str, Any]) -> InlineKeyboardMarkup:
    from database import job_monitor_interval
    from handlers.ui import INTERVAL_LABELS, INTERVAL_PRESETS
    job_id = job["job_id"]
    current = job_monitor_interval(job)
    rows = []
    row = []
    for sec in INTERVAL_PRESETS:
        mark = "✅ " if sec == current else ""
        label = INTERVAL_LABELS.get(sec, str(sec) + "s")
        row.append(InlineKeyboardButton(
            f"{mark}{label}",
            callback_data=f"job:intset:{job_id}:{sec}",
        ))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⚙️ Custom", callback_data=f"job:intcustom:{job_id}")])
    rows.append([InlineKeyboardButton("« Back", callback_data=f"job:mon:{job_id}")])
    return InlineKeyboardMarkup(rows)


def job_logs_keyboard(
    job_id: str,
    *,
    level: str = "all",
    page: int = 0,
    has_next: bool = False,
) -> InlineKeyboardMarkup:
    lvl = (level or "all").lower()
    rows = [
        [
            InlineKeyboardButton(
                ("• ALL" if lvl == "all" else "ALL"),
                callback_data=f"job:logs:{job_id}:all:0",
            ),
            InlineKeyboardButton(
                ("• ERR" if lvl == "error" else "ERR"),
                callback_data=f"job:logs:{job_id}:error:0",
            ),
            InlineKeyboardButton(
                ("• INFO" if lvl == "info" else "INFO"),
                callback_data=f"job:logs:{job_id}:info:0",
            ),
            InlineKeyboardButton(
                ("• WARN" if lvl == "warning" else "WARN"),
                callback_data=f"job:logs:{job_id}:warning:0",
            ),
        ]
    ]
    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton("⬅️ Prev", callback_data=f"job:logs:{job_id}:{lvl}:{page - 1}")
        )
    if has_next:
        nav.append(
            InlineKeyboardButton("Next ➡️", callback_data=f"job:logs:{job_id}:{lvl}:{page + 1}")
        )
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data=f"job:logs:{job_id}:{lvl}:{page}")])
    rows.append([InlineKeyboardButton("🗑 Clear Logs", callback_data=f"job:logsclear:{job_id}")])
    rows.append([InlineKeyboardButton("« Job", callback_data=f"job:open:{job_id}")])
    return InlineKeyboardMarkup(rows)


# ============================================================
# INDEXING
# ============================================================

def indexing_home_keyboard(
    db_ok: bool,
    bot_ok: bool,
    can_start: bool,
) -> InlineKeyboardMarkup:
    rows = []
    if not db_ok:
        rows.append([InlineKeyboardButton("🔗 Setup Index DB", callback_data="idx:setup_db")])
    else:
        rows.append([InlineKeyboardButton("🔗 Change / Remove Index DB", callback_data="idx:setup_db")])
    rows.append([InlineKeyboardButton("🤖 Select Index Bot", callback_data="idx:select_bot")])
    if can_start:
        rows.append([InlineKeyboardButton("📥 Start Indexing", callback_data="idx:start")])
        rows.append([InlineKeyboardButton("📤 Forward Indexed Media", callback_data="idx:fwd")])
    rows.append([InlineKeyboardButton("📊 Statistics", callback_data="idx:stats")])
    if db_ok:
        rows.append([InlineKeyboardButton("🗑 Clear Index Database", callback_data="idx:clear")])
    rows.append([InlineKeyboardButton("« Back to Dashboard", callback_data="dash:home")])
    return InlineKeyboardMarkup(rows)


def index_bot_select_keyboard(bots: List[Dict], current_bot_id: Optional[str] = None) -> InlineKeyboardMarkup:
    rows = []
    for b in bots:
        bid = b.get("bot_id")
        name = (b.get("name") or b.get("bot_username") or bid or "?")[:28]
        mark = "✅ " if bid == current_bot_id else ""
        rows.append([InlineKeyboardButton(f"{mark}🤖 {name}", callback_data=f"idx:setbot:{bid}")])
    if current_bot_id:
        rows.append([InlineKeyboardButton("❌ Clear Index Bot", callback_data="idx:setbot:__clear__")])
    rows.append([InlineKeyboardButton("« Back", callback_data="idx:home")])
    return InlineKeyboardMarkup(rows)


def index_db_setup_keyboard(has_uri: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("✏️ Enter / Replace URI", callback_data="idx:db_prompt")],
    ]
    if has_uri:
        rows.append([InlineKeyboardButton("🗑 Remove Index DB", callback_data="idx:db_remove")])
    rows.append([InlineKeyboardButton("« Back", callback_data="idx:home")])
    return InlineKeyboardMarkup(rows)


def index_progress_keyboard(user_id: int, paused: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="idx:prog_refresh"),
            InlineKeyboardButton("⏸ Pause" if not paused else "▶️ Resume", callback_data="idx:prog_pause" if not paused else "idx:prog_resume"),
        ],
        [InlineKeyboardButton("❌ Stop", callback_data="idx:prog_stop")],
    ]
    return InlineKeyboardMarkup(rows)


def index_fwd_count_keyboard(available: int) -> InlineKeyboardMarkup:
    rows = []
    for n in (100, 500, 1000):
        if available >= n:
            rows.append([InlineKeyboardButton(str(n), callback_data=f"idx:fwd_count:{n}")])
    rows.append([InlineKeyboardButton("Custom", callback_data="idx:fwd_custom")])
    if available > 0:
        rows.append([InlineKeyboardButton(f"All ({available:,})", callback_data=f"idx:fwd_count:{available}")])
    rows.append([InlineKeyboardButton("« Back", callback_data="idx:home")])
    return InlineKeyboardMarkup(rows)


def index_fwd_targets_keyboard(
    targets: List[Dict],
    selected: List[int],
) -> InlineKeyboardMarkup:
    rows = []
    for t in targets:
        cid = t["chat_id"]
        title = (t.get("title") or str(cid))[:28]
        mark = "☑️" if cid in selected else "☐"
        rows.append([InlineKeyboardButton(f"{mark} {title}", callback_data=f"idx:fwd_tg:{cid}")])
    rows.append([
        InlineKeyboardButton("✅ Continue", callback_data="idx:fwd_continue"),
        InlineKeyboardButton("« Back", callback_data="idx:fwd"),
    ])
    return InlineKeyboardMarkup(rows)


def index_fwd_delete_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, Delete", callback_data="idx:fwd_del:yes")],
        [InlineKeyboardButton("❌ No, Keep", callback_data="idx:fwd_del:no")],
        [InlineKeyboardButton("« Cancel", callback_data="idx:home")],
    ])


def index_clear_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Clear", callback_data="idx:clear_yes")],
        [InlineKeyboardButton("❌ Cancel", callback_data="idx:home")],
    ])


def index_start_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ START", callback_data="idx:do_start")],
        [InlineKeyboardButton("❌ Cancel", callback_data="idx:home")],
    ])
