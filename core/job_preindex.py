"""Pre-index existing media file_unique_ids from job target chats.

Job-scoped `job_duplicate_index` — independent of target anti-dupe.
method=bot → message-id walk.
method=user → get_chat_history with progress from newest msg id.
Progress written to job doc for UI (% / scanned / remaining / target i of n).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from pyrogram import Client
from pyrogram.errors import FloodWait

from database import (
    bulk_mark_job_preindex,
    update_job,
    add_job_log,
    get_job,
)
from core.filters import get_unique_file_id
from core.message_iter import custom_iter_messages, latest_message_id_probe

logger = logging.getLogger(__name__)

UPSERT_CHUNK = 200
PROBE_BATCH = 50
PROGRESS_EVERY_MSGS = 40  # force progress write every N messages (user path)
PROGRESS_EVERY_SEC = 2.0


def _status_indexing() -> str:
    try:
        from database import JobStatus
        if hasattr(JobStatus, "INDEXING"):
            return JobStatus.INDEXING.value
    except Exception:
        pass
    return "indexing"


def _status_failed() -> str:
    try:
        from database import JobStatus
        return JobStatus.FAILED.value
    except Exception:
        return "failed"


def _status_cancelled() -> str:
    try:
        from database import JobStatus
        return JobStatus.CANCELLED.value
    except Exception:
        return "cancelled"


def _status_paused() -> str:
    try:
        from database import JobStatus
        return JobStatus.PAUSED.value
    except Exception:
        return "paused"


async def _sleep_flood(e: FloodWait):
    wait = int(getattr(e, "value", 1) or 1) + 1
    logger.warning("Pre-index FloodWait %ss", wait)
    await asyncio.sleep(min(wait, 120))


async def _latest_id_via_probe(client: Client, chat_id: int) -> int:
    """Find highest message id in chat (bot-safe)."""
    lo, hi = 0, 1
    for _ in range(28):
        try:
            msgs = await client.get_messages(chat_id, hi)
            m = msgs[0] if isinstance(msgs, list) else msgs
            if m and not getattr(m, "empty", False):
                lo = hi
                hi = hi * 2
            else:
                break
        except FloodWait as e:
            await _sleep_flood(e)
        except Exception:
            break
        if hi > 50_000_000:
            break
    left, right = lo, max(lo + 1, hi)
    best = lo
    while left <= right:
        mid = (left + right) // 2
        if mid <= 0:
            left = mid + 1
            continue
        try:
            msgs = await client.get_messages(chat_id, mid)
            m = msgs[0] if isinstance(msgs, list) else msgs
            if m and not getattr(m, "empty", False):
                best = mid
                left = mid + 1
            else:
                right = mid - 1
        except FloodWait as e:
            await _sleep_flood(e)
        except Exception:
            right = mid - 1
    return int(best or 0)


async def _latest_id_user(client: Client, chat_id: int) -> int:
    """Newest message id via history (user accounts)."""
    try:
        async for m in client.get_chat_history(chat_id, limit=1):
            if m and getattr(m, "id", None):
                return int(m.id)
    except FloodWait as e:
        await _sleep_flood(e)
        try:
            async for m in client.get_chat_history(chat_id, limit=1):
                if m and getattr(m, "id", None):
                    return int(m.id)
        except Exception:
            pass
    except Exception as e:
        logger.warning("user latest id %s: %s", chat_id, e)
    return 0


async def _resolve_target_chat(client: Client, chat_id: int) -> int:
    """Force selected bot/account to resolve peer (required for private channels)."""
    try:
        chat = await client.get_chat(chat_id)
        return int(getattr(chat, "id", chat_id) or chat_id)
    except FloodWait as e:
        await _sleep_flood(e)
        chat = await client.get_chat(chat_id)
        return int(getattr(chat, "id", chat_id) or chat_id)
    except Exception as e:
        raise RuntimeError(
            f"Selected bot/account cannot access target `{chat_id}`: {type(e).__name__}: {e}. "
            "Add this exact bot as admin in the target (with read messages), then retry."
        ) from e


async def _bot_probe_latest_id(client: Client, chat_id: int) -> int:
    """Discover last message id for bots (no GetHistory).

    1) Send a short marker message in the target
    2) That message's id is the current last id
    3) Delete the marker immediately

    Requires the selected Forward Bot to be admin with Post + Delete in target.
    Works independently for each target chat.
    """
    marker = "🔍"  # short; deleted right away
    sent = None
    try:
        sent = await client.send_message(chat_id, marker)
        latest = int(getattr(sent, "id", 0) or 0)
        if latest <= 0:
            raise RuntimeError("send_message returned no id")
        logger.info(
            "bot last-id probe chat=%s marker_id=%s (will delete)",
            chat_id,
            latest,
        )
        return latest
    except FloodWait as e:
        await _sleep_flood(e)
        sent = await client.send_message(chat_id, marker)
        latest = int(getattr(sent, "id", 0) or 0)
        if latest <= 0:
            raise RuntimeError("send_message returned no id after FloodWait")
        return latest
    except Exception as e:
        raise RuntimeError(
            f"Cannot probe last message id in target `{chat_id}`: {type(e).__name__}: {e}. "
            "Selected Forward Bot must be admin with Post Messages permission."
        ) from e
    finally:
        if sent is not None and getattr(sent, "id", None):
            try:
                await client.delete_messages(chat_id, sent.id)
            except FloodWait as e:
                try:
                    await _sleep_flood(e)
                    await client.delete_messages(chat_id, sent.id)
                except Exception:
                    logger.warning(
                        "Could not delete probe msg %s in %s", sent.id, chat_id
                    )
            except Exception:
                logger.warning(
                    "Could not delete probe msg %s in %s",
                    getattr(sent, "id", None),
                    chat_id,
                )


async def _iter_bot_id_walk(
    client: Client,
    chat_id: int,
    on_progress=None,
) -> AsyncIterator[Any]:
    """Bot pre-index — Index-Forward style:

    * Discover last id by send → read id → delete (bot-safe)
    * Walk 1..last with custom_iter_messages only (get_messages batches)
    * No get_chat_history, no search_messages, no user-account assist

    Each target is probed separately (multi-target safe).
    """
    chat_id = await _resolve_target_chat(client, int(chat_id))

    from core.message_iter import custom_iter_messages

    latest = await _bot_probe_latest_id(client, chat_id)
    # Marker is deleted; its id is still the high-water for existing history
    # (new posts after this use higher ids — fine for pre-index snapshot).

    if latest <= 0:
        raise RuntimeError(
            f"Selected bot got invalid last id for target `{chat_id}`."
        )

    if on_progress:
        await on_progress(scanned=0, total=latest, phase="probe_done")

    async def _batch(scanned, total):
        if on_progress:
            await on_progress(scanned=scanned, total=total, phase="walk")

    saw = 0
    # Walk up to latest-1 so we skip the deleted marker id if it left a hole;
    # include latest in range anyway — empty placeholder is fine.
    async for message in custom_iter_messages(
        client,
        chat_id,
        limit=latest,
        offset=0,
        batch_size=100,
        on_batch=_batch,
    ):
        if message is None or getattr(message, "empty", False):
            continue
        saw += 1
        yield message

    if on_progress:
        await on_progress(scanned=latest, total=latest, phase="walk_done")
    logger.info(
        "bot pre-index chat=%s custom_iter_messages non-empty=%s last_id=%s",
        chat_id,
        saw,
        latest,
    )


async def _iter_user_history(
    client: Client,
    chat_id: int,
    on_progress=None,
) -> AsyncIterator[Any]:
    """User account: stream history; progress from newest id estimate."""
    latest = await _latest_id_user(client, chat_id)
    if on_progress:
        await on_progress(scanned=0, total=max(latest, 1) if latest else 0, phase="probe_done")
    count = 0
    try:
        async for message in client.get_chat_history(chat_id):
            count += 1
            # Approximate progress: how far down from newest id we are
            mid = int(getattr(message, "id", 0) or 0)
            if latest > 0 and mid > 0:
                scanned_est = max(0, latest - mid + 1)
                scanned_est = max(scanned_est, count)
            else:
                scanned_est = count
                if latest <= 0:
                    latest = max(latest, mid, count)
            if on_progress and (count == 1 or count % PROGRESS_EVERY_MSGS == 0):
                await on_progress(
                    scanned=scanned_est,
                    total=max(latest, scanned_est, 1),
                    phase="history",
                )
            yield message
    except FloodWait as e:
        await _sleep_flood(e)
        async for message in client.get_chat_history(chat_id):
            count += 1
            mid = int(getattr(message, "id", 0) or 0)
            if latest > 0 and mid > 0:
                scanned_est = max(latest - mid + 1, count)
            else:
                scanned_est = count
            if on_progress and count % PROGRESS_EVERY_MSGS == 0:
                await on_progress(
                    scanned=scanned_est,
                    total=max(latest, scanned_est, 1),
                    phase="history",
                )
            yield message



async def preindex_job_targets(
    client: Client,
    user_id: int,
    job: Dict[str, Any],
) -> Tuple[bool, str, int]:
    """Scan all targets; store file_unique_id under (job_id, target, uid)."""
    job_id = job["job_id"]
    targets = list(job.get("target_chat_ids") or [])
    method = (job.get("method") or "user").lower()
    if not targets:
        return False, "No target chats on job", 0

    n_t = len(targets)
    await update_job(
        user_id,
        job_id,
        {
            "status": _status_indexing(),
            "pre_index_status": "running",
            "pre_index_error": None,
            "pre_index_count": 0,
            "pre_index_started_at": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ),
            "pre_index_progress_pct": 0,
            "pre_index_scanned": 0,
            "pre_index_total_estimate": 0,
            "pre_index_remaining": 0,
            "pre_index_target_done": 0,
            "pre_index_target_total": n_t,
            "pre_index_current_target_id": int(targets[0]) if targets else None,
            "pre_index_current_target_index": 0,
        },
    )
    try:
        await add_job_log(
            job_id,
            "info",
            f"Pre-index started ({method}, {n_t} target(s))",
        )
    except Exception:
        pass

    # Identify executor (selected forward bot / user account client)
    try:
        me = await client.get_me()
        logger.info(
            "Pre-index job=%s using client id=%s username=@%s method=%s targets=%s",
            job_id,
            getattr(me, "id", None),
            getattr(me, "username", None),
            method,
            targets,
        )
    except Exception:
        logger.info("Pre-index job=%s method=%s targets=%s (get_me failed)", job_id, method, targets)

    total_ids = 0
    pending: List[dict] = []
    last_progress_ts = 0.0
    messages_seen = 0

    async def _flush():
        nonlocal total_ids, pending
        if not pending:
            return
        chunk = list(pending)
        pending = []
        n = await bulk_mark_job_preindex(job_id, chunk)
        # Count every media id we attempted (not only Mongo upserted_count)
        total_ids += len(chunk) if n >= 0 else 0
        if n == 0 and chunk:
            logger.warning(
                "Pre-index bulk_mark returned 0 for %s items (job=%s) — check Mongo index",
                len(chunk),
                job_id,
            )

    async def _write_progress(
        *,
        target_i: int,
        scanned: int,
        total: int,
        target_id: int = 0,
        force: bool = False,
    ):
        nonlocal last_progress_ts
        now = time.monotonic()
        if not force and (now - last_progress_ts) < PROGRESS_EVERY_SEC:
            return
        last_progress_ts = now
        done_t = max(0, min(target_i, n_t))
        cur_frac = (scanned / total) if total and total > 0 else 0.0
        cur_frac = min(1.0, max(0.0, cur_frac))
        # completed targets fully counted + current target fraction
        pct = int(round(((done_t + cur_frac) / max(1, n_t)) * 100))
        pct = min(99, max(0, pct))
        remaining = max(0, (total or 0) - (scanned or 0))
        try:
            await update_job(
                user_id,
                job_id,
                {
                    "pre_index_count": total_ids,
                    "pre_index_progress_pct": pct,
                    "pre_index_scanned": int(scanned or 0),
                    "pre_index_total_estimate": int(total or 0),
                    "pre_index_remaining": int(remaining),
                    "pre_index_target_done": done_t,
                    "pre_index_target_total": n_t,
                    "pre_index_current_target_id": int(target_id) if target_id else None,
                    "pre_index_current_target_index": done_t,
                    "pre_index_status": "running",
                    "status": _status_indexing(),
                },
            )
        except Exception:
            logger.exception("pre-index progress write failed")

    try:
        for ti, tid in enumerate(targets):
            tid = int(tid)
            logger.info(
                "Pre-index job=%s target=%s (%s/%s) method=%s",
                job_id,
                tid,
                ti + 1,
                n_t,
                method,
            )
            await _write_progress(
                target_i=ti, scanned=0, total=0, target_id=tid, force=True
            )

            if method == "bot":
                async def _on_prog(scanned=0, total=0, phase="walk", _ti=ti, _tid=tid):
                    await _write_progress(
                        target_i=_ti, scanned=scanned, total=total, target_id=_tid
                    )

                msg_iter = _iter_bot_id_walk(client, tid, on_progress=_on_prog)
            else:
                async def _on_prog_u(scanned=0, total=0, phase="history", _ti=ti, _tid=tid):
                    await _write_progress(
                        target_i=_ti, scanned=scanned, total=total, target_id=_tid
                    )

                msg_iter = _iter_user_history(client, tid, on_progress=_on_prog_u)

            scanned_local = 0
            async for message in msg_iter:
                # Lightweight cancel check every 25 msgs (not every message)
                if scanned_local % 25 == 0:
                    fresh = await get_job(user_id, job_id)
                    st = (fresh or {}).get("status")
                    if st == _status_cancelled():
                        await _flush()
                        return False, "Job cancelled during pre-index", total_ids
                    if st == _status_paused():
                        await _flush()
                        return False, "Job paused during pre-index", total_ids

                messages_seen += 1
                uid = get_unique_file_id(message)
                scanned_local += 1
                if uid:
                    pending.append({
                        "file_unique_id": uid,
                        "target_chat_id": tid,
                        "source_msg_id": getattr(message, "id", None),
                    })
                    if len(pending) >= UPSERT_CHUNK:
                        await _flush()

                # Extra progress pulse for user path if iterator is slow to callback
                if method != "bot" and scanned_local % PROGRESS_EVERY_MSGS == 0:
                    await _write_progress(
                        target_i=ti,
                        scanned=scanned_local,
                        total=max(scanned_local, 1),
                        target_id=tid,
                    )

            await _flush()

            # Target complete
            await _write_progress(
                target_i=ti + 1, scanned=0, total=0, target_id=tid, force=True
            )

        logger.info(
            "Pre-index job=%s method=%s done: messages_seen=%s media_ids=%s targets=%s",
            job_id,
            method,
            messages_seen,
            total_ids,
            n_t,
        )
        note = None
        if total_ids == 0:
            note = (
                f"0 media IDs (scanned non-empty msgs≈{messages_seen}). "
                "Target may have no media, or bot cannot read media in that chat."
            )
            logger.warning("Pre-index job=%s %s", job_id, note)

        await update_job(
            user_id,
            job_id,
            {
                "pre_index_status": "done",
                "pre_index_count": total_ids,
                "pre_index_progress_pct": 100,
                "pre_index_remaining": 0,
                "pre_index_error": note,
                "pre_index_messages_seen": int(messages_seen),
                "pre_index_target_done": n_t,
                "pre_index_target_total": n_t,
            },
        )
        try:
            await add_job_log(
                job_id, "info", f"Pre-index complete — {total_ids} unique media IDs"
            )
        except Exception:
            pass
        return True, f"Indexed {total_ids} media IDs", total_ids

    except FloodWait as e:
        msg = f"FloodWait {e.value}s during pre-index"
        logger.warning("Job %s %s", job_id, msg)
        await update_job(
            user_id,
            job_id,
            {
                "pre_index_status": "failed",
                "pre_index_error": msg,
                "status": _status_failed(),
                "error_message": f"Target indexing failed: {msg}",
            },
        )
        try:
            await add_job_log(job_id, "error", msg)
        except Exception:
            pass
        return False, msg, total_ids
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        logger.exception("Pre-index failed job=%s", job_id)
        await update_job(
            user_id,
            job_id,
            {
                "pre_index_status": "failed",
                "pre_index_error": msg,
                "status": _status_failed(),
                "error_message": f"Target indexing failed: {msg}",
            },
        )
        try:
            await add_job_log(job_id, "error", f"Pre-index failed: {msg}")
        except Exception:
            pass
        return False, msg, total_ids
