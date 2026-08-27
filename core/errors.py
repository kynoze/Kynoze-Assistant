import logging
import uuid

logger = logging.getLogger(__name__)


def friendly_error(context: str, exc: Exception | None = None) -> str:
    err_id = uuid.uuid4().hex[:8]
    if exc is not None:
        logger.exception("[%s] %s", err_id, context)
    else:
        logger.error("[%s] %s", err_id, context)
    return f"❌ Something went wrong. Please try again.\n(ref: `{err_id}`)"
