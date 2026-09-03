"""Conversation state with TTL so flows cannot stick forever."""

from __future__ import annotations

import time
from typing import Any, Optional

TTL_SECONDS = 600  # 10 minutes
STATE_TTL_SECONDS = 600
STATE_NAMES = (
    "settings_state",
    "target_add_state",
    "account_add_state",
    "account_edit_state",
    "bot_add_state",
    "job_create_state",
    "forward_state",
    "index_state",
    "wroxen_state",
    "delete_state",
    "cnl_state",
    "log_chat_state",
    "cnl_rvia_state",
    "job_progress_ui_state",
    "job_interval_state",
    "jobs_log_channel_state",
    "job_filter_state",
    "qf_filter_state",
    "job_acc_state",
)


class StateMap(dict):
    """dict[user_id] -> value, auto-wrapped with a timestamp."""

    def __setitem__(self, user_id, value):
        if value is None:
            super().__setitem__(user_id, None)
            return
        if isinstance(value, dict) and value.get("_state_ts") is not None and "_state_v" in value:
            super().__setitem__(user_id, value)
            return
        super().__setitem__(user_id, {"_state_v": value, "_state_ts": time.time()})

    def __getitem__(self, user_id):
        if not dict.__contains__(self, user_id):
            raise KeyError(user_id)
        return self._unwrap(user_id, dict.get(self, user_id))

    def get(self, user_id, default=None):
        if not dict.__contains__(self, user_id):
            return default
        return self._unwrap(user_id, dict.get(self, user_id))

    def _unwrap(self, user_id, item):
        if item is None:
            return None
        if isinstance(item, dict) and "_state_ts" in item and "_state_v" in item:
            if time.time() - float(item["_state_ts"]) > TTL_SECONDS:
                dict.__setitem__(self, user_id, None)
                return None
            return item["_state_v"]
        return item


def attach_states(client) -> None:
    for name in STATE_NAMES:
        current = getattr(client, name, None)
        if isinstance(current, StateMap):
            continue
        sm = StateMap()
        if isinstance(current, dict):
            for k, v in current.items():
                sm[k] = v
        setattr(client, name, sm)


def set_state(client, name: str, user_id: int, value) -> None:
    store = getattr(client, name, None)
    if not isinstance(store, StateMap):
        sm = StateMap()
        if isinstance(store, dict):
            for k, v in store.items():
                sm[k] = v
        setattr(client, name, sm)
        store = sm
    store[user_id] = value


def get_state(client, name: str, user_id: int, default=None):
    current = getattr(client, name, None)
    if current is None:
        return default
    if isinstance(current, dict):
        return current.get(user_id, default)
    return default


def clear_user_states(client, user_id: int) -> Optional[Any]:
    """Clear every flow for this user. Returns account temp_client if any."""
    temp = None
    for name in STATE_NAMES:
        store = getattr(client, name, None)
        if not isinstance(store, dict) or user_id not in store:
            continue
        val = store.get(user_id)
        if name == "account_add_state" and isinstance(val, dict):
            temp = val.get("temp_client")
        store[user_id] = None
    return temp


async def clear_all_states(client, user_id: int) -> None:
    temp = clear_user_states(client, user_id)
    if not temp:
        return
    for meth in ("disconnect", "stop"):
        fn = getattr(temp, meth, None)
        if not fn:
            continue
        try:
            await fn()
            return
        except Exception:
            continue
