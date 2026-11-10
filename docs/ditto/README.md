# WareTwin + Eclipse Ditto + WoDT allocator

Hướng dẫn Linux/Bash cho bridge MQTT và bộ phân công robot theo goal model.
Chạy WareTwin độc lập theo [README chính](../../README.md).

```text
WareTwin UI :5174 ↔ backend :8000 → MQTT :1883 → Ditto :8080
                        ↑                         ↓
                POST /api/fleet/assignments ← task_allocator ← Neo4j :7687
```

MQTT đưa trạng thái lên Ditto; allocator gửi quyết định về backend qua HTTP.
Backend là nguồn trạng thái gốc và kiểm tra lại điều kiện trước khi giao task.
Mặc định bridge tắt, Fleet Manager nội bộ tự phân công robot.

## 1. Chuẩn bị

- Python 3.11+, Node 20+, Docker Compose v2 và `curl`.
- Hai repo `WareTwin` và `WoDT-pattern` nằm cạnh nhau. `WoDT-pattern` cung cấp
  Docker stack, goal model, `task_allocator` và Ditto Studio.
- Cài backend/frontend theo README chính. Trong `WoDT-pattern`, tạo môi trường
  Python và cài `task_allocator/requirements.txt` vào `task_allocator/.venv`.

Đặt đường dẫn trong **mỗi terminal**, sửa `LAB` theo máy của bạn:

```bash
export LAB="$HOME/Desktop/SME Lab"
export WT="$LAB/WareTwin"
export WODT="$LAB/WoDT-pattern"
```

Tạo `WoDT-pattern/task_allocator/.env` từ file mẫu nếu chưa có. Cấu hình chính:

```dotenv
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD='MAT_KHAU_NEO4J_CUA_BAN'
NEO4J_DATABASE=neo4j
DITTO_URL=http://localhost:8080
DITTO_USER=ditto
DITTO_PASSWORD='MAT_KHAU_DITTO_CUA_BAN'
WARETWIN_URL=http://localhost:8000
FLEET_API_KEY='KHOA_DUNG_CHUNG_CHO_BACKEND_VA_ALLOCATOR'
TWIN_MQTT_NAMESPACE=waretwin
ALLOCATOR_INTERVAL_S=0.5
ALLOCATOR_MAX_AGE_S=5
```

Thay các giá trị mẫu bằng cấu hình hiện có. Backend và allocator cần cùng
`FLEET_API_KEY`. File phải có cú pháp Bash hợp lệ để nạp bằng `source`.

## 2. Khởi động hạ tầng và provision Ditto

```bash
cd "$WODT"
docker network inspect wodt >/dev/null 2>&1 || docker network create wodt
docker compose -f mqtt/docker-compose.yml up -d
DITTO_VERSION=3.9 docker compose -f ditto/docker/docker-compose.yml up -d
docker compose -f docker-compose.neo4j.yml up -d
```

Chờ Ditto healthy tại `http://localhost:8080/status/health`. Neo4j Browser dùng
`http://localhost:7474`. Mosquitto và Ditto connectivity cần cùng network `wodt`:
connection dùng `tcp://mosquitto:1883`, backend trên host dùng `mqtt://localhost:1883`.

```bash
cd "$WT/backend"
./infra/ditto/provision.sh
```

Script tạo/cập nhật policy `waretwin:waretwin-policy` và connection
**WareTwin local MQTT connection**, source `waretwin/#`, QoS 1.
Policy mặc định dùng `ditto:ditto`; connection dùng `devops:foobar`, cùng gateway `:8080`.
Nếu tài khoản/địa chỉ khác, truyền `DITTO_URL`, `DITTO_AUTH`, `DITTO_AUTH_DEVOPS`;
`DITTO_CONN_URL` dùng khi có gateway connection riêng. Policy và connection JSON
nằm trong [`backend/infra/ditto/`](../../backend/infra/ditto/).

## 3. Chạy backend và frontend

**Terminal backend:**

```bash
cd "$WT/backend"
set -a
source "$WODT/task_allocator/.env"
set +a
TWIN_ALLOCATION_MODE=external TWIN_MQTT_ENABLED=1 \
TWIN_MQTT_URL=mqtt://localhost:1883 TWIN_RATE_LIMIT=0 TWIN_ALLOW_NO_ORIGIN=1 \
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`http://localhost:8000/api/health` cần có `ok=true`, `mqtt_connected=true`.
Chế độ `external` giữ task chờ cho đến khi allocator giao robot.
Nếu chỉ cần telemetry Ditto, dùng `TWIN_ALLOCATION_MODE=internal`.

**Terminal frontend:**

```bash
cd "$WT/frontend"
VITE_WS_URL=ws://localhost:8000/ws npm run dev -- --host 127.0.0.1 --port 5174 --strictPort
```

Mở `http://localhost:5174`, kiểm tra badge **BACKEND**, bấm **Play**, chọn **1×**.
Chế độ **LOCAL** chỉ chạy engine trong trình duyệt và không cập nhật Ditto.

## 4. Seed goal model và chạy allocator

Với database mới, nạp topology và contract phân công:

```bash
cd "$WODT"
set -a
source task_allocator/.env
set +a
task_allocator/.venv/bin/python cypher/seed_neo4j.py --waretwin-only
task_allocator/.venv/bin/python -m task_allocator.seed --apply
```

Nếu graph và contract đã có, dùng dữ liệu hiện tại. Nếu chỉ thiếu `T_ORDER_ASSIGN`,
chạy lệnh seed thứ hai. Seed áp dụng điều kiện mẫu, có thể đặt lại tùy chỉnh cũ.

**Terminal allocator:**

```bash
cd "$WODT"
set -a
source task_allocator/.env
set +a
task_allocator/.venv/bin/python -m task_allocator
```

Kỳ vọng log `Loaded goal=G_WT_TRANSPORT agent=A_ROBOT shadow=False`, sau đó
`Assigned task=... robot=... decision=...`. Sau khi sửa graph, khởi động lại allocator
để đọc contract mới. `--shadow` chỉ quan sát, không gửi phân công.

Trên WareTwin: **Task Queue → View All Tasks →** tạo task `PICK`, source `Shelf A12`,
destination `PACK 01`. Theo dõi cột Robot, trạng thái `IN_PROGRESS` và
**AI Ops → Fleet Manager Decisions** để xem điểm/lý do chọn robot.

`A_ROBOT` là vai trò trong graph ánh xạ tới R01–R20; allocator chọn một robot cụ thể
thuộc vai trò đó. Goal xác định trách nhiệm và điều kiện; công thức chấm điểm nằm
trong `WoDT-pattern/task_allocator/scoring.py`.

## 5. Ditto Studio (tùy chọn)

Cài dependencies trong `WoDT-pattern/client-aplication` bằng `npm ci`.
Cấu hình `ditto-ambassador/.env` theo file mẫu: upstream Docker
`DITTO_BASE_URL=http://nginx:80`, tài khoản Ditto và CORS cho `http://localhost:5173`.

```bash
cd "$WODT"
docker compose -f ditto-ambassador/docker-compose.yml up -d
cd client-aplication
npm run dev -- --host 127.0.0.1 --port 5173 --strictPort
```

Mở `http://localhost:5173 → Digital Twins`, tìm `waretwin:warehouse`:
- **Features → dispatch:** snapshot allocator đang đọc; `mode=external`,
  `paused=false`, `tick` tăng, `run_id` khớp backend.
- **Panel E → Đầy đủ:** goal model và điều kiện của từng node.
- `waretwin:task-<Task ID>` → **Features → state:** robot được giao và trạng thái task.

## 6. Ánh xạ và cấu hình bridge

Mỗi entity có feature `state` và các `attributes` tĩnh. Thing tổng hợp
`waretwin:warehouse` chứa `subsystems`, `kpi` và snapshot `dispatch` khi được bật.

| Entity | Thing | `attributes.goalRootId` |
|---|---|---|
| Robot | `waretwin:robot-R01` | `G_WT_TRANSPORT` |
| Lift | `waretwin:lift-LIFT-1` | `G_WT_CROSSFLOOR` |
| Conveyor | `waretwin:conveyor-CV01` | `G_CV_PROVIDE_MATERIAL_FLOW` |
| Zone | `waretwin:zone-A` | `G_Z_MAINTAIN_SAFE_TRAFFIC` |
| Camera / sensor | `waretwin:camera-CAM-A01`, `waretwin:sensor-S-A01` | `G_P_PROVIDE_SITUATIONAL_AWARENESS` |
| Task | `waretwin:task-A3820` | `G_WT_ALLOC` |
| Person / alert | `waretwin:person-...`, `waretwin:alert-...` | — |
| Warehouse | `waretwin:warehouse` | `G_WT_FULFILL_OPERATIONS` |

`goalAgentId` giữ cùng Goal ID để tương thích tên trường cũ.

- Entity mới / full-sync: `modify` kèm `policyId`, attributes và features.
- Đổi field: `merge`, content-type `application/merge-patch+json`; `null` xóa property.
- Entity biến mất: `delete`. Entity mất khi backend offline có thể để lại Thing cũ.
- Bridge gộp telemetry theo chu kỳ mặc định 0,5 giây.
- `dispatch` được thay thế nguyên feature bằng một lệnh `modify`, giữ task, robot,
  lift và zone trong cùng snapshot với `run_id`, `tick`, `published_at`.

| Biến backend | Mặc định / ý nghĩa |
|---|---|
| `TWIN_MQTT_ENABLED` | `0`; đặt `1` để publish |
| `TWIN_MQTT_URL` | `mqtt://localhost:1883` |
| `TWIN_MQTT_NAMESPACE` | `waretwin` |
| `TWIN_MQTT_POLICY_ID` | `waretwin:waretwin-policy` |
| `TWIN_MQTT_INTERVAL_S` | `0.5` |
| `TWIN_MQTT_QOS` | `1` |
| `TWIN_MQTT_USERNAME` / `TWIN_MQTT_PASSWORD` | Rỗng; dùng khi broker yêu cầu auth |
| `TWIN_ALLOCATION_MODE` | `internal`; `external` nhận quyết định từ allocator |
| `FLEET_API_KEY` | Khóa chung, bắt buộc cho external; gửi bằng header `X-Fleet-Key` |
| `TWIN_DISPATCH_SNAPSHOT` | `1` để publish dispatch cả ở internal, phục vụ shadow |

File mẫu: [`backend/.env.example`](../../backend/.env.example).

## 7. Kiểm tra và xử lý lỗi

```bash
curl -fsS http://localhost:8000/api/health | python3 -m json.tool
curl -fsS -u ditto:ditto http://localhost:8080/api/2/things/waretwin:robot-R01 | python3 -m json.tool
curl -fsS -u devops:foobar http://localhost:8080/api/2/connections | python3 -m json.tool
mosquitto_sub -h localhost -t 'waretwin/#' -v
```

Thay tài khoản trong lệnh nếu stack dùng credentials khác. `mqtt_connected=true`
xác nhận backend kết nối broker; Thing thay đổi trên Ditto mới xác nhận cả luồng.

| Triệu chứng | Kiểm tra |
|---|---|
| Ditto không cập nhật | Backend đang chạy, bridge bật, connection `open`, UI dùng **BACKEND** |
| Provision báo `401` | Policy dùng user `ditto`, connection dùng user `devops` |
| Broker không kết nối | Cổng 1883 và `TWIN_MQTT_URL`; connectivity cùng Docker network với broker |
| Task luôn `WAITING` | Allocator chạy không có `--shadow`; backend không pause; contract có robot hợp lệ |
| Fleet API báo `401` / `409` | Khóa chung / snapshot quá hạn hoặc khác `run_id`, task/robot đã đổi trạng thái |
| Đổi graph chưa có tác dụng | Khởi động lại allocator để tải contract |

Dừng hệ thống: Ctrl+C ở allocator trước, sau đó backend và các frontend.
