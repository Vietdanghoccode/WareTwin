"""Task location rules, matching frontend/src/simulation/rules.ts:
source and destination must exist in the layout, cannot be chargers or identical, and are restricted by TaskType. (Quy tắc vị trí tác vụ, khớp với frontend/src/simulation/rules.ts: source và destination phải tồn tại trong layout, không phải trạm sạc, không trùng nhau và bị giới hạn theo TaskType.)"""
from __future__ import annotations

from typing import Any, Optional

TASK_RULES: dict[str, tuple[set[str], set[str]]] = {
    "PICK": ({"SHELF"}, {"PACKING", "SORTING", "OUTBOUND"}),
    "REPLENISH": ({"INBOUND"}, {"SHELF"}),
    "TRANSPORT": ({"PACKING", "SORTING", "INBOUND", "OUTBOUND"}, {"PACKING", "SORTING", "INBOUND", "OUTBOUND"}),
    "RETURN": ({"PACKING", "SORTING", "OUTBOUND"}, {"SHELF"}),
}


def task_error(loc: dict[str, dict[str, Any]], type_: str, source: str, destination: str) -> Optional[str]:
    """Return an error message; None means valid. (Trả về thông báo lỗi; None nghĩa là hợp lệ.)"""
    rule = TASK_RULES.get(type_)
    if not rule:
        return f"unknown task type {type_!r}"
    s, d = loc.get(source), loc.get(destination)
    if not s:
        return f"unknown source location {source!r}"
    if not d:
        return f"unknown destination location {destination!r}"
    if source == destination:
        return "source and destination must differ"
    if s["kind"] == "CHARGING" or d["kind"] == "CHARGING":
        return "charging stations cannot be task locations"
    if s["kind"] not in rule[0]:
        return f"{type_} source must be one of {sorted(rule[0])}, got {s['kind']}"
    if d["kind"] not in rule[1]:
        return f"{type_} destination must be one of {sorted(rule[1])}, got {d['kind']}"
    return None
