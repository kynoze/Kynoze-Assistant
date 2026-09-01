# bot.py
# Management Bot — dashboard + job_worker_loop
# Handlers live in handlers/ (plugins). Do not register /start or dash: here.

import asyncio
import logging

from pyrogram import Client, idle
from pyrogram.enums import ParseMode

from config import Config
from database import db
from core.job_worker import job_worker_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("pymongo").setLevel(logging.WARNING)
logging.getLogger("pyrogram.session.session").setLevel(logging.ERROR)
logging.getLogger("pyrogram.types.messages_and_media.message").setLevel(logging.ERROR)


app = Client(
    name="ManagementBot",
    api_id=Config.API_ID,
    api_hash=Config.API_HASH,
    bot_token=Config.BOT_TOKEN,
    parse_mode=ParseMode.MARKDOWN,
    in_memory=True,
    plugins=dict(root="handlers"),
)


async def main():
    from core.dns_fix import apply_termux_dns_fix
    apply_termux_dns_fix()

    from config import validate_config
    # PRODUCTION=1 or STRICT_CONFIG=1 → hard-fail on SESSION_ENC_KEY etc.
    import os
    strict = (os.environ.get("PRODUCTION", "").strip() in ("1", "true", "yes")
              or os.environ.get("STRICT_CONFIG", "").strip() in ("1", "true", "yes"))
    cfg_errors = validate_config(strict=strict)
    for e in cfg_errors:
        logger.error("CONFIG: %s", e)
    if cfg_errors and strict:
        raise SystemExit(
            "Fatal config errors (PRODUCTION/STRICT_CONFIG mode). Fix env and restart.\n"
            + "\n".join(f"  - {e}" for e in cfg_errors)
        )
    if not Config.SESSION_ENC_KEY:
        logger.warning(
            "SESSION_ENC_KEY is empty — sessions/tokens cannot be stored safely. "
            "Set SESSION_ENC_KEY and PRODUCTION=1 for production."
        )

    logger.info("Connecting to MongoDB...")
    await db.connect()
    logger.info("MongoDB connected")

    await app.start()
    try:
        from core.log_chat import set_mgmt_bot, install_owner_log_handler, report_owner
        set_mgmt_bot(app)
        install_owner_log_handler()
        if not Config.SESSION_ENC_KEY:
            await report_owner(
                "WARNING",
                "SESSION_ENC_KEY is empty",
                "Sessions and bot tokens cannot be stored safely until SESSION_ENC_KEY is set.",
            )
    except Exception:
        logger.exception("log-chat init failed")
    asyncio.create_task(job_worker_loop(app))
    try:
        from core.job_worker import progress_ui_refresh_loop
        asyncio.create_task(progress_ui_refresh_loop(app))
        logger.info("Progress UI refresh loop started")
    except Exception:
        logger.exception("Progress UI loop start skipped")
    try:
        from core.delete_manager.worker import delete_monitor_loop
        asyncio.create_task(delete_monitor_loop(app))
        logger.info("Delete Manager monitor started")
    except Exception:
        logger.exception("Delete Manager monitor start skipped")
        try:
            from core.log_chat import report_owner
            await report_owner("ERROR", "Delete Manager failed to start", "See bot logs.")
        except Exception:
            pass
    try:
        from core.wroxen.runtime import refresh_routing
        await refresh_routing()
        logger.info("Wroxen runtime routing loaded")
    except Exception:
        logger.exception("Wroxen runtime start skipped")
        try:
            from core.log_chat import report_owner
            await report_owner("ERROR", "Wroxen failed to start", "See bot logs.")
        except Exception:
            pass
    try:
        from core.cnl.runtime import start_cnl_runtime
        await start_cnl_runtime()
        logger.info("CNL runtime started")
    except Exception:
        logger.exception("CNL runtime start skipped")
        try:
            from core.log_chat import report_owner
            await report_owner("ERROR", "CNL failed to start", "See bot logs.")
        except Exception:
            pass
    logger.info("Job worker started")
    try:
        ver = open("VERSION.txt").read().strip()
    except Exception:
        ver = "unknown"
    logger.info("Bot is up · version=%s · Press Ctrl+C to stop.", ver)
    try:
        from core.log_chat import report_owner
        await report_owner(
            "INFO",
            f"Management bot started (v{ver})",
            f"strict_config={'on' if strict else 'off'} · session_enc={'yes' if Config.SESSION_ENC_KEY else 'NO'}",
        )
    except Exception:
        pass
    await idle()
    logger.info("Stopping bot (graceful)...")
    # Cancel in-memory job tasks — DB status stays RUNNING so they resume on next boot
    try:
        from core.job_worker import RUNNING_JOB_TASKS
        tasks = list(RUNNING_JOB_TASKS.items())
        for jid, task in tasks:
            if task and not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(
                *[t for _, t in tasks if t], return_exceptions=True
            )
            RUNNING_JOB_TASKS.clear()
            logger.info("Cancelled %s in-memory job task(s)", len(tasks))
    except Exception:
        logger.exception("job task cancel on shutdown")
    try:
        from core.lifecycle import shutdown_lifecycle
        await shutdown_lifecycle()
    except Exception:
        logger.exception("lifecycle shutdown")
    try:
        from core.cnl.runtime import stop_cnl_runtime
        await stop_cnl_runtime()
    except Exception:
        pass
    try:
        await db.close()
    except Exception:
        pass
    try:
        await app.stop()
    except Exception:
        logger.exception("app.stop")


if __name__ == "__main__":
    logger.info("Starting Management Bot...")
    try:
        app.loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Interrupted")
    except SystemExit as e:
        logger.error("%s", e)
        raise
