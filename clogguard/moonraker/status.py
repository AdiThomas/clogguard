"""Helpers for Moonraker printer-object status dictionaries."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any


def merge_status(status: dict[str, Any], diff: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-merge a ``notify_status_update`` diff into ``status`` in place and return it.

    Nested dicts merge key by key; any other value (lists included) replaces the old one, which
    is how Moonraker reports changes (only changed fields are sent; lists are sent whole).
    The diff is copied so later mutation of the merged status cannot alter the caller's diff.
    """
    for key, value in diff.items():
        old = status.get(key)
        if isinstance(value, Mapping) and isinstance(old, dict):
            merge_status(old, value)
        else:
            status[key] = copy.deepcopy(value) if isinstance(value, Mapping | list) else value
    return status


def get_path(status: Mapping[str, Any], *path: str | int) -> Any:
    """Return ``status[p0][p1]...`` or None if any step is missing."""
    cur: Any = status
    for p in path:
        if isinstance(p, int):
            if not isinstance(cur, list | tuple) or not -len(cur) <= p < len(cur):
                return None
            cur = cur[p]
        else:
            if not isinstance(cur, Mapping) or p not in cur:
                return None
            cur = cur[p]
    return cur
