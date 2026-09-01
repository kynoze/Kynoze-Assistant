import logging
import uuid

logger = logging.getLogger(__name__)


def friendly_error(context: str, exc: Exception | None = None) -> str:
    err_id = uuid.uuid4().hex[:8]
    if exc is not None:
        logger.exception("[%s] %s", err_id, context)
        detail = f"{type(exc).__name__}: {exc}"
    else:
        logger.error("[%s] %s", err_id, context)
        detail = context
    try:
        import asyncio
        from core.log_chat import report_owner
        loop = asyncio.get_running_loop()
        loop.create_task(
            report_owner(
                "ERROR",
                f"App error: {context}",
                f"ref=`{err_id}`\n{detail}",
            )
        )
    except Exception:
        pass
    return f"❌ Something went wrong. Please try again.\n(ref: `{err_id}`)"
