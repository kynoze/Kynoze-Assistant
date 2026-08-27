"""Auto-delete monitor. Uses check_interval + message_age independently.

No Motor. Uses the same sync Mongo helpers as the rest of the project.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from database import list_due_delete_configs, update_delete_config, get_delete_config
from core.delete_manager.engine import RUNNING, run_delete_job, is_delete_running

logger = logging.getLogger(__name__)

POLL_SECONDS = 30


def _next_run(interval_seconds: int) -> datetime:
    iv = max(3600, int(interval_seconds or 86400))
    return datetime.now(timezone.utc) + timedelta(seconds=iv)


async def delete_monitor_loop(_management_client=None):
    logger.info("Delete Manager monitor started")
    while True:
        try:
            due = await list_due_delete_configs()
            for cfg in due:
                cid = cfg.get("delete_config_id")
                if not cid or is_delete_running(cid):
                    continue
                if not cfg.get("auto_delete"):
                    continue
                logger.info("Auto-delete due for %s (%s)", cid, cfg.get("target_title"))
                task = asyncio.create_task(_run_auto(cfg))
                RUNNING[cid] = task
        except Exception:
            logger.exception("Delete monitor poll failed")
        await asyncio.sleep(POLL_SECONDS)


async def _run_auto(cfg: dict):
    cid = cfg["delete_config_id"]
    user_id = cfg["user_id"]
    try:
        stats = await run_delete_job(cfg, progress_message=None, auto=True)
        interval = int(cfg.get("check_interval_seconds") or 86400)
        updates = {
            "next_run_at": _next_run(interval),
            "last_error": stats.get("error"),
        }
        if stats.get("status") in ("failed", "paused") and stats.get("error"):
            # permission / session problems already pause auto inside engine
            pass
        await update_delete_config(user_id, cid, updates)
        logger.info(
            "Auto-delete %s finished status=%s deleted=%s",
            cid,
            stats.get("status"),
            stats.get("deleted"),
        )
    except asyncio.CancelledError:
        logger.info("Auto-delete %s cancelled", cid)
    except Exception:
        logger.exception("Auto-delete %s crashed", cid)
        await update_delete_config(
            user_id,
            cid,
            {
                "auto_delete": False,
                "last_error": "Auto-delete stopped after an error.",
                "next_run_at": _next_run(int(cfg.get("check_interval_seconds") or 86400)),
            },
        )
