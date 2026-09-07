"""Shared message ID iteration for bot-safe range scans.

Used by Jobs forward, Pre-Index, Index-Forward, Wroxen bot fallback.
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncGenerator, List, Optional, Union

from pyrogram import Client
from pyrogram.errors import FloodWait
from pyrogram.types import Message

logger = logging.getLogger(__name__)

DEFAULT_BATCH = 200


async def custom_iter_messages(
    client: Client,
    chat_id: Union[int, str],
    limit: int,
    offset: int = 0,
    *,
    batch_size: int = DEFAULT_BATCH,
    on_batch=None,
) -> AsyncGenerator[Message, None]:
    """
    Iterate message IDs (offset+1) .. limit inclusive via get_messages batches.

    - Batches of `batch_size` (default 200)
    - FloodWait: sleep and retry same batch
    - Other errors: skip batch after log; stop after 5 consecutive failures
    - Yields non-None messages (empty placeholders skipped by caller if needed)

    `on_batch(scanned_through_id, limit)` optional progress callback after each batch.
    """
    current = int(offset or 0)
    limit = int(limit or 0)
    if limit <= current:
        return

    consecutive_failures = 0
    while current < limit:
        bs = min(int(batch_size or DEFAULT_BATCH), limit - current)
        message_ids = list(range(current + 1, current + bs + 1))
        try:
            messages = await client.get_messages(chat_id, message_ids)
            consecutive_failures = 0
        except FloodWait as e:
            wait = int(getattr(e, "value", 1) or 1) + 1
            logger.warning("custom_iter_messages FloodWait %ss @%s", wait, current + 1)
            await asyncio.sleep(min(wait, 120))
            continue
        except Exception as e:
            consecutive_failures += 1
            logger.exception(
                "get_messages failed at id~%s (failure %s): %s",
                current + 1,
                consecutive_failures,
                e,
            )
            current += bs
            if on_batch:
                try:
                    await on_batch(current, limit)
                except Exception:
                    pass
            if consecutive_failures >= 5:
                logger.error("Too many get_messages failures — stopping iteration")
                return
            await asyncio.sleep(0.5)
            continue

        if not isinstance(messages, list):
            messages = [messages]

        for msg in messages:
            current += 1
            if msg is None:
                continue
            yield msg

        if on_batch:
            try:
                await on_batch(current, limit)
            except Exception:
                pass





async def _message_exists(client: Client, chat_id: Union[int, str], mid: int) -> bool:
    try:
        msgs = await client.get_messages(chat_id, int(mid))
        m = msgs[0] if isinstance(msgs, list) else msgs
        return bool(m and not getattr(m, "empty", False))
    except FloodWait as e:
        await asyncio.sleep(min(int(getattr(e, "value", 1) or 1) + 1, 120))
        return await _message_exists(client, chat_id, mid)
    except Exception:
        return False


async def latest_message_id_probe(
    client: Client,
    chat_id: Union[int, str],
    *,
    on_progress=None,
) -> int:
    """Highest reachable message id via get_messages only (bots: no history/search).

    Telegram bots cannot call messages.GetHistory. We must discover max id by
    probing IDs. Empty slots (deleted) are normal — never stop at first empty.

    Strategy:
      1) pinned_message id (if any)
      2) dense candidate set (geometric + linear grids)
      3) batch get_messages (up to 100 ids) — keep max non-empty id
      4) climb from best with expanding jumps
    """
    best = 0

    # 1) pinned
    try:
        chat = await client.get_chat(chat_id)
        pin = getattr(chat, "pinned_message", None)
        if pin and getattr(pin, "id", None):
            best = max(best, int(pin.id))
    except Exception:
        pass

    # 2) candidate ids
    candidates = set()
    x = 1
    while x <= 5_000_000:
        candidates.add(x)
        x = max(x + 1, int(x * 1.6))
    for base in (10, 50, 100, 500, 1000, 5000):
        for k in range(0, 200):
            v = base * (k + 1)
            if v > 5_000_000:
                break
            candidates.add(v)
    if best:
        for d in range(0, 5000, 25):
            candidates.add(best + d)
    ordered = sorted(candidates)

    # 3) batch probe
    BATCH = 100
    for i in range(0, len(ordered), BATCH):
        batch = ordered[i : i + BATCH]
        try:
            msgs = await client.get_messages(chat_id, batch)
            if not isinstance(msgs, list):
                msgs = [msgs]
            for m in msgs:
                if m and not getattr(m, "empty", False) and getattr(m, "id", None):
                    best = max(best, int(m.id))
        except FloodWait as e:
            await asyncio.sleep(min(int(getattr(e, "value", 1) or 1) + 1, 120))
            try:
                msgs = await client.get_messages(chat_id, batch)
                if not isinstance(msgs, list):
                    msgs = [msgs]
                for m in msgs:
                    if m and not getattr(m, "empty", False) and getattr(m, "id", None):
                        best = max(best, int(m.id))
            except Exception:
                pass
        except Exception:
            # try one-by-one for this batch
            for mid in batch:
                if await _message_exists(client, chat_id, mid):
                    best = max(best, mid)
        if on_progress:
            try:
                await on_progress(scanned=i + len(batch), total=len(ordered), phase="probe")
            except Exception:
                pass

    if best <= 0:
        return 0

    # 4) climb
    pos = best
    empty_streak = 0
    jumps = (1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000)
    while empty_streak < 30 and pos < 50_000_000:
        found = False
        for j in jumps:
            trial = pos + j
            if trial > 50_000_000:
                break
            if await _message_exists(client, chat_id, trial):
                pos = trial
                best = trial
                empty_streak = 0
                found = True
                break
        if not found:
            empty_streak += 1
            pos = min(pos + jumps[min(empty_streak, len(jumps) - 1)], 50_000_000)

    return int(best or 0)
