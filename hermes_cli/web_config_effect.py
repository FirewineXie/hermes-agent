"""Describe when a saved config document reaches the running Hermes process."""

from __future__ import annotations

from typing import Any


def _changed_paths(before: Any, after: Any, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    if isinstance(before, dict) and isinstance(after, dict):
        changed = set()
        for key in before.keys() | after.keys():
            if key not in before or key not in after:
                changed.add((*prefix, str(key)))
            else:
                changed.update(_changed_paths(before[key], after[key], (*prefix, str(key))))
        return changed
    return {prefix} if before != after else set()


def config_save_effect(before: dict, after: dict) -> dict[str, str]:
    """Use the session-policy follow contract; never call a disk write a live reload."""
    from gateway.session_policy import FOLLOW_AGENT_KEYS, FOLLOW_SECTIONS

    changed = _changed_paths(before, after)
    if not changed:
        return {"status": "applied"}
    if any(path[0] in {"gateway", "platforms"} for path in changed):
        return {"status": "gateway_restart_required"}
    if all(
        path[0] in FOLLOW_SECTIONS
        or (len(path) >= 2 and path[0] == "agent" and path[1] in FOLLOW_AGENT_KEYS)
        for path in changed
    ):
        return {"status": "next_turn"}
    return {"status": "next_session"}
