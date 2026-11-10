"""
MQTT → Eclipse Ditto bridge choWareTwin. (Cầu nối MQTT → Eclipse Ditto.)

Backend là nguồn chân lý (authoritative). Lớp này chỉ là adapter ĐẦU RA: sau mỗi lần engine
tiến tick, nó so sánh trạng thái hiện tại với snapshot đã publish lần trước và chỉ gửi
những trường THAY ĐỔI sang MQTT theo Ditto Protocol JSON. Ditto (qua connectivity source)
sẽ ghi lại thành các Thing — tức là nơi lưu trạng thái hiện tại của robot/conveyor/...

Nguyên tắc:
- Chỉ publish khi có thay đổi; entity mới xuất hiện → create (modify), biến mất → delete.
- Coalesce theo `interval_s` để không bắn mỗi tick khi robot di chuyển (speed 10×).
- paho chạy thread riêng, `publish()` không block vòng lặp mô phỏng.
- Module import được kể cả khi chưa cài paho (paho chỉ được import lười trong MqttTransport).
- Thuần logic tách khỏi I/O: `build_things` / `DittoPublisher` nhận transport giả được trong test.

Topic & payload theo Ditto Protocol: `waretwin/<type>-<id>/things/twin/commands/{modify|merge|delete}`.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional
from urllib.parse import urlparse

log = logging.getLogger("twin.mqtt")

# section trong TwinState → prefix tên Thing. (Khu vực state → tiền tố tên Thing.)
SECTION_PREFIX = {
    "robots": "robot",
    "conveyors": "conveyor",
    "zones": "zone",
    "lifts": "lift",
    "cameras": "camera",
    "sensors": "sensor",
    "tasks": "task",
    "people": "person",
    "alerts": "alert",
}
WAREHOUSE_THING = "warehouse"

# Ánh xạ section → root Goal trong goal knowledge graph (Neo4j, xem WoDT-pattern/cypher/waretwin_*_kg.md).
# Dùng để nối Thing trên Ditto với cây mục tiêu của nó; ghi vào attributes tĩnh nên không bị diff mỗi tick.
# `goalRootId` là khoá chính mà WoDT đọc; `goalAgentId` ghi kèm để tương thích quy ước cũ.
SECTION_GOAL_ROOT = {
    "robots": "G_WT_TRANSPORT",
    "conveyors": "G_CV_PROVIDE_MATERIAL_FLOW",
    "zones": "G_Z_MAINTAIN_SAFE_TRAFFIC",
    "lifts": "G_WT_CROSSFLOOR",
    "cameras": "G_P_PROVIDE_SITUATIONAL_AWARENESS",
    "sensors": "G_P_PROVIDE_SITUATIONAL_AWARENESS",
    "tasks": "G_WT_ALLOC",
}
WAREHOUSE_GOAL_ROOT = "G_WT_FULFILL_OPERATIONS"

_MISSING = object()


# ─────────────────────────────────────────────────────────────
# Tiện ích build payload (thuần, không I/O — dùng được trong test)
# ─────────────────────────────────────────────────────────────

def thing_name(section: str, entity_id: str) -> str:
    """Tên Thing trong Ditto (namespace tách riêng khi tạo topic)."""
    return f"{SECTION_PREFIX.get(section, section)}-{entity_id}"


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    """Bỏ các giá trị None ở tầng trên cùng: property vắng mặt = không có giá trị.
    Khi một giá trị chuyển thành None, diff sẽ gửi null để Ditto xoá property đó."""
    return {k: v for k, v in d.items() if v is not None}


def _round(x: Any, n: int) -> Any:
    try:
        return round(float(x), n)
    except (TypeError, ValueError):
        return x


def _build_robot(r: dict[str, Any]) -> dict[str, Any]:
    pos = r.get("position") or [0, 0, 0]
    props: dict[str, Any] = {
        "status": r.get("status"), "fsm": r.get("fsm"), "health": r.get("health"),
        "floor": r.get("floor"), "lift_id": r.get("lift_id"), "lift_stage": r.get("lift_stage"),
        "position": {"x": _round(pos[0], 3), "z": _round(pos[2], 3)},
        "heading": _round(r.get("heading"), 3), "velocity": _round(r.get("velocity"), 2),
        "battery": _round(r.get("battery"), 2),
    }
    if r.get("load") is not None:
        props["load"] = r["load"]
    # Task hiện tại: khi không có task thì BỎ property để diff gửi null (xoá trên Ditto).
    if r.get("current_task_id"):
        props["task"] = _compact({
            "id": r.get("current_task_id"), "destination": r.get("destination"),
            "zone": r.get("zone"), "eta_s": _round(r.get("eta_s"), 1),
        })
    if r.get("perception") is not None:
        props["perception"] = r["perception"]
    if r.get("stats") is not None:
        props["stats"] = {k: _round(v, 1) if isinstance(v, float) else v for k, v in r["stats"].items()}
    return _compact(props)


def _build_conveyor(c: dict[str, Any]) -> dict[str, Any]:
    return _compact({
        "status": c.get("status"), "speed_mps": _round(c.get("speed_mps"), 2),
        "items_on_belt": c.get("items_on_belt"), "throughput_per_min": _round(c.get("throughput_per_min"), 2),
    })


def _build_zone(z: dict[str, Any]) -> dict[str, Any]:
    return _compact({
        "status": z.get("status"), "robot_count": z.get("robot_count"),
        "congestion": _round(z.get("congestion"), 3), "blocked_reason": z.get("blocked_reason"),
        "blocked_since_tick": z.get("blocked_since_tick"),
    })


def _build_lift(l: dict[str, Any]) -> dict[str, Any]:
    return _compact({
        "state": l.get("state"), "floor": l.get("floor"), "target_floor": l.get("target_floor"),
        "y": _round(l.get("y"), 2), "door_f1": l.get("door_f1"), "door_f2": l.get("door_f2"),
        "occupant": l.get("occupant"), "reserved_by": l.get("reserved_by"), "queue": l.get("queue"),
        "fault": l.get("fault"), "trips": l.get("trips"),
    })


def _build_camera(c: dict[str, Any]) -> dict[str, Any]:
    return _compact({"status": c.get("status"), "last_observation": c.get("last_observation")})


def _build_sensor(s: dict[str, Any]) -> dict[str, Any]:
    return _compact({
        "status": s.get("status"), "kind": s.get("kind"),
        "value": _round(s.get("value"), 3), "unit": s.get("unit"),
    })


def _build_task(t: dict[str, Any]) -> dict[str, Any]:
    return _compact({
        "type": t.get("type"), "priority": t.get("priority"), "status": t.get("status"),
        "source": t.get("source"), "destination": t.get("destination"),
        "assigned_robot": t.get("assigned_robot"), "load_units": t.get("load_units"),
        "created_tick": t.get("created_tick"), "assigned_tick": t.get("assigned_tick"),
        "started_tick": t.get("started_tick"), "completed_tick": t.get("completed_tick"),
        "deadline_tick": t.get("deadline_tick"), "eta_s": _round(t.get("eta_s"), 1),
    })


def _build_person(p: dict[str, Any]) -> dict[str, Any]:
    pos = p.get("position") or [0, 0, 0]
    return _compact({
        "kind": p.get("kind"), "position": {"x": _round(pos[0], 3), "z": _round(pos[2], 3)},
        "heading": _round(p.get("heading"), 3), "zone": p.get("zone"), "floor": p.get("floor"),
        "expires_tick": p.get("expires_tick"),
    })


def _build_alert(a: dict[str, Any]) -> dict[str, Any]:
    return _compact({
        "severity": a.get("severity"), "title": a.get("title"), "detail": a.get("detail"),
        "zone_id": a.get("zone_id"), "robot_id": a.get("robot_id"), "conveyor_id": a.get("conveyor_id"),
        "source_event_id": a.get("source_event_id"), "acknowledged": a.get("acknowledged"),
        "created_tick": a.get("created_tick"), "resolved_tick": a.get("resolved_tick"),
    })


_BUILDERS = {
    "robots": _build_robot, "conveyors": _build_conveyor, "zones": _build_zone, "lifts": _build_lift,
    "cameras": _build_camera, "sensors": _build_sensor, "tasks": _build_task,
    "people": _build_person, "alerts": _build_alert,
}

# Thuộc tính tĩnh (attributes) lấy từ layout. (Tĩnh → chỉ gửi lúc tạo Thing.)
def _static_maps(layout: Optional[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    L = layout or {}
    return {
        "conveyors": {c["id"]: {"zone": c.get("zone"), "feeds": c.get("feeds"), "direction": c.get("direction")} for c in L.get("conveyors", [])},
        "zones": {z["id"]: {"floor": z.get("floor", 1), "name": z.get("name")} for z in L.get("zones", [])},
        "cameras": {c["id"]: {"zone": c.get("zone"), "floor": c.get("floor", 1), "fov_deg": c.get("fov_deg"), "range_m": c.get("range_m")} for c in L.get("cameras", [])},
        "lifts": {l["id"]: {"floors": l.get("floors")} for l in L.get("lifts", [])},
        "sensors": {s["id"]: {"zone": s.get("zone")} for s in L.get("sensors", [])},
    }


def build_things(state: dict[str, Any], layout: Optional[dict[str, Any]] = None) -> dict[str, dict[str, Any]]:
    """TwinState → {thing_name: {"attributes": {...}, "features": {...}}}.

    Mỗi entity là một feature `state`; warehouse có `subsystems` + `kpi`.
    Attribute tĩnh lấy từ layout khi có (nếu không thì bỏ qua)."""
    static = _static_maps(layout)
    out: dict[str, dict[str, Any]] = {}

    for section, builder in _BUILDERS.items():
        entities = state.get(section) or {}
        attrs_by_id = static.get(section, {})
        for eid, entity in entities.items():
            name = thing_name(section, eid)
            attrs = _compact(dict(attrs_by_id.get(eid) or {}))
            root = SECTION_GOAL_ROOT.get(section)
            if root:   # nối Thing với root Goal của nó trong goal graph
                attrs = _compact({**attrs, "goalRootId": root, "goalAgentId": root})
            if section == "robots":   # model/max_speed là tĩnh nhưng nằm trong state
                attrs = _compact({**attrs, "model": entity.get("model"), "max_speed": entity.get("max_speed")})
            out[name] = {"attributes": attrs, "features": {"state": {"properties": builder(entity)}}}

    # Warehouse tổng hợp: subsystems + kpi.
    subsys = state.get("subsystems") or {}
    kpi = state.get("kpi") or {}
    kpi_props = _compact({
        "tick": kpi.get("tick"), "fleet": kpi.get("fleet"),
        "operation": kpi.get("operation"), "efficiency": kpi.get("efficiency"),
    })
    out[WAREHOUSE_THING] = {
        "attributes": _compact({"layout_id": state.get("layout_id"), "schema_version": state.get("schema_version"),
                                "goalRootId": WAREHOUSE_GOAL_ROOT, "goalAgentId": WAREHOUSE_GOAL_ROOT}),
        "features": {
            "subsystems": {"properties": dict(subsys)},
            "kpi": {"properties": kpi_props},
        },
    }
    return out


def _diff_props(prev: dict[str, Any], cur: dict[str, Any]) -> dict[str, Any]:
    """So sánh property tầng một. Key mất đi → None (JSON merge patch = xoá)."""
    d: dict[str, Any] = {}
    for k, v in cur.items():
        if prev.get(k, _MISSING) != v:
            d[k] = v
    for k in prev:
        if k not in cur:
            d[k] = None
    return d


def _diff_features(prev: dict[str, Any], cur: dict[str, Any]) -> dict[str, Any]:
    d: dict[str, Any] = {}
    for fname, fval in cur.items():
        got = _diff_props((prev.get(fname) or {}).get("properties", {}), fval.get("properties", {}))
        if got:
            d[fname] = {"properties": got}
    for fname in prev:
        if fname not in cur:
            d[fname] = None
    return d


def diff_thing(prev: dict[str, Any], cur: dict[str, Any]) -> dict[str, Any]:
    """Phần thân cần gửi cho lệnh merge (chỉ feature/attribute thay đổi)."""
    d: dict[str, Any] = {}
    fd = _diff_features(prev.get("features", {}), cur.get("features", {}))
    if fd:
        d["features"] = fd
    ad = _diff_props(prev.get("attributes", {}), cur.get("attributes", {}))
    if ad:
        d["attributes"] = ad
    return d


def _topic(namespace: str, name: str, command: str) -> str:
    return f"{namespace}/{name}/things/twin/commands/{command}"


def _envelope(topic: str, path: str, value: Any = _MISSING, content_type: str = "application/json") -> dict[str, Any]:
    msg: dict[str, Any] = {"topic": topic, "headers": {"content-type": content_type}, "path": path}
    if value is not _MISSING:
        msg["value"] = value
    return msg


# ─────────────────────────────────────────────────────────────
# Transport: paho MQTT (chỉ import paho khi thực sự kết nối)
# ─────────────────────────────────────────────────────────────

class MqttTransport:
    """Kết nối Mosquitto và publish Ditto Protocol JSON. paho chạy loop riêng nên không block asyncio."""

    def __init__(self, url: str = "mqtt://localhost:1883", qos: int = 1,
                 username: str = "", password: str = "", client_id: str = "waretwin-backend") -> None:
        u = urlparse(url)
        self.scheme = u.scheme or "mqtt"
        self.host = u.hostname or "localhost"
        self.port = u.port or (8883 if self.scheme in ("mqtts", "ssl") else 1883)
        self.tls = self.scheme in ("mqtts", "ssl")
        self.qos = qos
        self.username = username
        self.password = password
        self.client_id = client_id
        self.connected = False
        self._client: Any = None

    @classmethod
    def from_env(cls) -> "MqttTransport":
        return cls(
            url=os.environ.get("TWIN_MQTT_URL", "mqtt://localhost:1883"),
            qos=int(os.environ.get("TWIN_MQTT_QOS", "1") or "1"),
            username=os.environ.get("TWIN_MQTT_USERNAME", ""),
            password=os.environ.get("TWIN_MQTT_PASSWORD", ""),
        )

    def connect(self) -> bool:
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            log.warning("paho-mqtt chưa cài — bỏ qua MQTT (pip install paho-mqtt)")
            return False
        try:
            try:  # paho ≥2 dùng callback API v2; bản cũ không có enum này
                client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.client_id)
            except (AttributeError, TypeError):
                client = mqtt.Client(client_id=self.client_id)
            if self.username:
                client.username_pw_set(self.username, self.password)
            if self.tls:
                client.tls_set()
            client.connect(self.host, self.port, keepalive=60)
            client.loop_start()
            self._client = client
            self.connected = True
            log.info("MQTT connected: %s:%s (qos=%s)", self.host, self.port, self.qos)
            return True
        except Exception as e:  # broker chưa lên cũng không làm chết sim
            self._client = None
            self.connected = False
            log.warning("không kết nối được MQTT %s:%s — %s", self.host, self.port, e)
            return False

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        if not self._client:
            return
        try:
            self._client.publish(topic, json.dumps(payload, separators=(",", ":"), ensure_ascii=False), qos=self.qos)
        except Exception as e:
            log.warning("MQTT publish lỗi trên %s: %s", topic, e)

    def disconnect(self) -> None:
        self.connected = False
        if not self._client:
            return
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass
        finally:
            self._client = None


# ─────────────────────────────────────────────────────────────
# Publisher: diff + điều phối create/merge/delete
# ─────────────────────────────────────────────────────────────

class DittoPublisher:
    """Giữ snapshot đã publish và chỉ gửi phần thay đổi lên Ditto qua MQTT.

    `transport` chỉ cần có `.connected` (bool) và `.publish(topic, payload)` → test dễ dàng
    bằng transport giả, không cần broker."""

    def __init__(self, transport: Optional[MqttTransport], *, namespace: str = "waretwin",
                 policy_id: str = "waretwin:waretwin-policy", interval_s: float = 0.5,
                 enabled: bool = True, layout: Optional[dict[str, Any]] = None) -> None:
        self.transport = transport
        self.namespace = namespace
        self.policy_id = policy_id
        self.interval_s = max(0.0, interval_s)
        self.enabled = enabled
        self.layout = layout
        self._known: dict[str, dict[str, Any]] = {}
        self._last = 0.0

    @classmethod
    def from_env(cls, layout: Optional[dict[str, Any]] = None) -> "DittoPublisher":
        enabled = os.environ.get("TWIN_MQTT_ENABLED", "0") == "1"
        return cls(
            MqttTransport.from_env() if enabled else None,
            namespace=os.environ.get("TWIN_MQTT_NAMESPACE", "waretwin"),
            policy_id=os.environ.get("TWIN_MQTT_POLICY_ID", "waretwin:waretwin-policy"),
            interval_s=float(os.environ.get("TWIN_MQTT_INTERVAL_S", "0.5") or "0.5"),
            enabled=enabled,
            layout=layout,
        )

    def reset(self) -> None:
        """Quên snapshot → lần publish kế tiếp tạo/ghi đè lại toàn bộ (dùng khi RESET sim)."""
        self._known = {}
        self._last = 0.0

    def _active(self) -> bool:
        return bool(self.enabled and self.transport is not None and self.transport.connected)

    def publish(self, state: dict[str, Any], *, dispatch: Optional[dict[str, Any]] = None, force: bool = False, now: Optional[float] = None) -> list[str]:
        """Publish phần thay đổi; trả về danh sách Thing đã gửi (rỗng nếu throttle/tắt)."""
        if not self._active():
            return []
        now = time.monotonic() if now is None else now
        if not force and now - self._last < self.interval_s:
            return []
        self._last = now

        things = build_things(state, self.layout)
        sent: list[str] = []

        for name in [n for n in self._known if n not in things]:   # entity biến mất → delete
            topic = _topic(self.namespace, name, "delete")
            self.transport.publish(topic, _envelope(topic, "/"))
            self._known.pop(name, None)
            sent.append(name)

        for name, payload in things.items():
            prev = self._known.get(name)
            if prev is None:   # chưa từng publish → create (modify kèm policyId)
                topic = _topic(self.namespace, name, "modify")
                value = {"policyId": self.policy_id}
                if payload.get("attributes"):
                    value["attributes"] = payload["attributes"]
                if payload.get("features"):
                    value["features"] = payload["features"]
                self.transport.publish(topic, _envelope(topic, "/", value))
                self._known[name] = payload
                sent.append(name)
            else:
                d = diff_thing(prev, payload)
                if d:   # chỉ gửi khi có thay đổi
                    topic = _topic(self.namespace, name, "merge")
                    # Ditto merge dùng JSON Merge Patch (RFC 7386), không phải application/json.
                    self.transport.publish(topic, _envelope(topic, "/", d, content_type="application/merge-patch+json"))
                    self._known[name] = payload
                    sent.append(name)
        if dispatch is not None:
            # Replace the complete feature in one command. Merge-patch would retain
            # removed waiting tasks and erase explicit nulls inside this snapshot.
            topic = _topic(self.namespace, WAREHOUSE_THING, "modify")
            self.transport.publish(topic, _envelope(topic, "/features/dispatch",
                                   {"properties": {"snapshot": dispatch}}))
            if WAREHOUSE_THING not in sent:
                sent.append(WAREHOUSE_THING)
        return sent
