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
    logger.info("Connecting to MongoDB...")
    await db.connect()
    logger.info("MongoDB connected")

    if not Config.SESSION_ENC_KEY:
        logger.warning("SESSION_ENC_KEY is empty — sessions/tokens cannot be stored safely")

    await app.start()
    asyncio.create_task(job_worker_loop(app))
    try:
        from core.delete_manager.worker import delete_monitor_loop
        asyncio.create_task(delete_monitor_loop(app))
        logger.info("Delete Manager monitor started")
    except Exception:
        logger.exception("Delete Manager monitor start skipped")
    try:
        from core.wroxen.runtime import refresh_routing
        await refresh_routing()
        logger.info("Wroxen runtime routing loaded")
    except Exception:
        logger.exception("Wroxen runtime start skipped")
    try:
        from core.cnl.runtime import start_cnl_runtime
        await start_cnl_runtime()
        logger.info("CNL runtime started")
    except Exception:
        logger.exception("CNL runtime start skipped")
    logger.info("Job worker started")
    logger.info("Bot is up. Press Ctrl+C to stop.")
    await idle()
    logger.info("Stopping bot...")
    try:
        from core.cnl.runtime import stop_cnl_runtime
        await stop_cnl_runtime()
    except Exception:
        pass
    try:
        await db.close()
    except Exception:
        pass
    await app.stop()


if __name__ == "__main__":
    logger.info("Starting Management Bot... (build wroxen-exact-rank)")
    app.loop.run_until_complete(main())
