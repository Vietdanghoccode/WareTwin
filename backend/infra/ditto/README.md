# WareTwin → Eclipse Ditto (qua MQTT)

Ánh xạ trạng thái hiện tại của các thực thể trong mô phỏng lên Ditto Things. Backend chỉ
**publish phần thay đổi** sang MQTT; Ditto connectivity (source `waretwin/#`) ghi lại thành Thing.

```
backend (SimEngine) ──publish──▶ Mosquitto :1883 ──subscribe──▶ Ditto connectivity ──▶ Things
   only-on-change                Ditto Protocol JSON              payloadMapping: Ditto
```

## 1. Provisioning (chạy 1 lần)

```bash
# Ditto gateway mặc định ở :8082, devops:foobar
./provision.sh
# hoặc: DITTO_URL=http://localhost:8082 DITTO_AUTH=devops:foobar ./provision.sh
```

Script tạo:
- Policy `waretwin:waretwin-policy` (subject `nginx:ditto`, READ/WRITE trên `thing:/`, `policy:/`, `message:/`).
- MQTT connection `WareTwin local MQTT connection` → `tcp://mosquitto:1883`, source `waretwin/#`, qos 1.

Kiểm tra thủ công:

```bash
curl -s -u devops:foobar http://localhost:8082/api/2/connections | python3 -m json.tool
```

## 2. Bật backend

```bash
cd backend
TWIN_MQTT_ENABLED=1 uvicorn app.main:app --reload --port 8000
```

Biến môi trường (xem `backend/.env.example`):

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `TWIN_MQTT_ENABLED` | `0` | `1` = bật publish |
| `TWIN_MQTT_URL` | `mqtt://localhost:1883` | broker từ host (backend chạy ngoài docker) |
| `TWIN_MQTT_NAMESPACE` | `waretwin` | namespace Ditto |
| `TWIN_MQTT_POLICY_ID` | `waretwin:waretwin-policy` | policy gán cho Thing |
| `TWIN_MQTT_INTERVAL_S` | `0.5` | coalesce — không publish mỗi tick |
| `TWIN_MQTT_QOS` | `1` | QoS MQTT |
| `TWIN_MQTT_USERNAME` / `TWIN_MQTT_PASSWORD` | rỗng | nếu broker yêu cầu auth |

## 3. Ánh xạ entity → Thing

Mỗi Thing có một feature `state` (property = các trường động) và `attributes` tĩnh.

| State | Thing | Ghi chú |
|---|---|---|
| `robots` | `waretwin:robot-R01…R20` | status, fsm, pose, battery, task, perception, stats |
| `conveyors` | `waretwin:conveyor-CV01…CV04` | status, speed, items, throughput |
| `zones` | `waretwin:zone-A…M` | status, robot_count, congestion |
| `lifts` | `waretwin:lift-LIFT-1,2` | state, floor, doors, occupant, fault |
| `cameras` | `waretwin:camera-CAM-…` | status, last_observation |
| `sensors` | `waretwin:sensor-S-…` | status, kind, value, unit |
| `tasks` | `waretwin:task-A…` | create khi xuất hiện, delete khi bị prune |
| `people` | `waretwin:person-…` | create/delete động |
| `alerts` | `waretwin:alert-…` | severity, ack, resolved |
| tổng hợp | `waretwin:warehouse` | subsystems + KPI |

Semantics: entity mới → `modify` (kèm `policyId`); đổi field → `merge` (property mất đi gửi
`null` để Ditto xoá); entity biến mất → `delete`.

## 4. Kiểm chứng

```bash
# Xem message thô trên broker
mosquitto_sub -h localhost -t 'waretwin/#' -v

# Xem Thing đã được Ditto ghi lại
curl -s -u ditto:ditto http://localhost:8080/api/2/things/waretwin:robot-R01 | python3 -m json.tool

# Health backend có cờ mqtt
curl -s http://localhost:8000/api/health | python3 -m json.tool
```
