"""Unit test cho cầu nối MQTT→Ditto: build payload, diff, create/merge/delete, throttle. (Không cần broker.)"""
from __future__ import annotations

import json

from app.mqtt import DittoPublisher, build_things, diff_thing, thing_name
from app.sim.engine import SimEngine
from app.sim.navgrid import load_layout


class FakeTransport:
    """Transport giả ghi lại mọi message thay vì bắn MQTT."""

    def __init__(self) -> None:
        self.connected = True
        self.messages: list[tuple[str, dict]] = []

    def publish(self, topic: str, payload: dict) -> None:
        self.messages.append((topic, payload))

    def topics(self) -> list[str]:
        return [t for t, _ in self.messages]


def _engine() -> SimEngine:
    return SimEngine(load_layout(), seed=42)


def _make(eng: SimEngine, interval_s: float = 0.5) -> tuple[DittoPublisher, FakeTransport]:
    t = FakeTransport()
    p = DittoPublisher(t, interval_s=interval_s, enabled=True)
    return p, t


def test_build_things_covers_all_sections_and_warehouse():
    eng = _engine()
    things = build_things(eng.state, eng.layout)
    # Hạ tầng tĩnh theo layout
    assert "robot-R01" in things and "robot-R20" in things
    assert "conveyor-CV01" in things and "zone-A" in things
    assert "lift-LIFT-1" in things and "camera-CAM-A01" in things and "sensor-S-A01" in things
    assert "warehouse" in things
    # Mỗi entity có feature `state`; robot có model/max_speed ở attributes.
    r = things["robot-R01"]
    assert "state" in r["features"] and r["features"]["state"]["properties"]["battery"] is not None
    assert r["attributes"]["max_speed"] > 0
    # Thuộc tính tĩnh lấy từ layout.
    assert things["conveyor-CV01"]["attributes"]["feeds"] == "PACK-02"
    assert "subsystems" in things["warehouse"]["features"] and "kpi" in things["warehouse"]["features"]


def test_create_then_merge_only_sends_changes():
    eng = _engine()
    p, t = _make(eng)

    # Lần đầu: create toàn bộ (modify) kèm policyId.
    sent = p.publish(eng.state, force=True)
    assert "robot-R01" in sent and "warehouse" in sent
    assert all(topic.endswith("/things/twin/commands/modify") for topic in t.topics())
    assert t.messages[0][1]["value"]["policyId"] == "waretwin:waretwin-policy"
    t.messages.clear()

    # Không đổi gì → không gửi.
    assert p.publish(eng.state, force=True) == []
    assert t.messages == []

    # Chỉ đổi battery → merge duy nhất property battery.
    eng.state["robots"]["R01"]["battery"] = 12.34
    sent = p.publish(eng.state, force=True)
    assert sent == ["robot-R01"]
    topic, payload = t.messages[0]
    assert topic == f"waretwin/robot-R01/things/twin/commands/merge"
    assert payload["headers"]["content-type"] == "application/merge-patch+json"
    props = payload["value"]["features"]["state"]["properties"]
    assert props == {"battery": 12.34}


def test_null_transition_deletes_property_on_ditto():
    eng = _engine()
    p, t = _make(eng)
    p.publish(eng.state, force=True)
    # Gán task rồi bỏ task → property `task` phải được gửi null (JSON merge patch xoá).
    eng.state["robots"]["R01"]["current_task_id"] = "A9999"
    eng.state["robots"]["R01"]["destination"] = "PACK-01"
    p.publish(eng.state, force=True)
    t.messages.clear()
    eng.state["robots"]["R01"]["current_task_id"] = None
    p.publish(eng.state, force=True)
    topic, payload = t.messages[0]
    assert topic.endswith("/merge")
    assert payload["value"]["features"]["state"]["properties"]["task"] is None


def test_entity_disappearance_sends_delete():
    eng = _engine()
    p, t = _make(eng)
    task = eng.create_task("PICK", "NORMAL", "SHELF-A12", "PACK-01")
    p.publish(eng.state, force=True)
    assert f"task-{task['id']}" in p._known
    t.messages.clear()
    del eng.state["tasks"][task["id"]]
    sent = p.publish(eng.state, force=True)
    assert sent == [f"task-{task['id']}"]
    topic, payload = t.messages[0]
    assert topic == f"waretwin/task-{task['id']}/things/twin/commands/delete"
    assert "value" not in payload


def test_throttle_coalesces_ticks():
    eng = _engine()
    p, t = _make(eng, interval_s=0.5)
    assert p.publish(eng.state, now=100.0)  # lần đầu → create
    assert p.publish(eng.state, now=100.1) == []  # trong khoảng → bỏ qua
    eng.state["subsystems"]["CONVEYORS"] = "WARNING"
    assert p.publish(eng.state, now=100.2) == []  # vẫn trong khoảng
    sent = p.publish(eng.state, now=100.6)  # qua khoảng → gửi thay đổi đã tích luỹ
    assert sent == ["warehouse"]


def test_disabled_or_disconnected_is_noop():
    t = FakeTransport()
    t.connected = False
    p = DittoPublisher(t, enabled=True)
    assert p.publish({}, force=True) == []
    assert t.messages == []

    t2 = FakeTransport()
    p2 = DittoPublisher(t2, enabled=False)
    assert p2.publish({}, force=True) == []
    assert t2.messages == []


def test_diff_thing_attributes():
    prev = {"attributes": {"zone": "A", "feeds": "PACK-02"}, "features": {}}
    cur = {"attributes": {"zone": "A", "feeds": "PACK-02", "direction": "FORWARD"}, "features": {}}
    assert diff_thing(prev, cur) == {"attributes": {"direction": "FORWARD"}}
    assert diff_thing(cur, cur) == {}


def test_names_are_stable_and_topic_safe():
    assert thing_name("robots", "R01") == "robot-R01"
    assert thing_name("cameras", "CAM-A01") == "camera-CAM-A01"
    assert ":" not in thing_name("tasks", "A3813")


def test_reset_forces_full_recreate():
    eng = _engine()
    p, t = _make(eng)
    p.publish(eng.state, force=True)
    t.messages.clear()
    p.reset()
    sent = p.publish(eng.state, force=True)
    assert "robot-R01" in sent
    assert all(topic.endswith("/modify") for topic in t.topics())
    # Payload create hợp lệ JSON (đảm bảo serialize được).
    json.dumps(t.messages[0][1])
