#!/usr/bin/env bash
# Tạo policy + MQTT connection cho WareTwin trên Eclipse Ditto local. (Idempotent: chạy lại được.)
#
#   ./provision.sh
#   DITTO_URL=http://localhost:8080 DITTO_AUTH=ditto:ditto ./provision.sh
#
# Lưu ý xác thực: policy đi qua gateway thường (:8080, user `ditto`), connection đi qua
# gateway devops (:8082, user `devops`) — giống setup Ditto local của WoDT-pattern.
# Yêu cầu: Ditto đang chạy và Mosquitto ở cùng docker network (`tcp://mosquitto:1883`).
set -euo pipefail

DITTO="${DITTO_URL:-http://localhost:8080}"          # gateway thường
DITTO_AUTH="${DITTO_AUTH:-ditto:ditto}"
CONN_URL="${DITTO_CONN_URL:-http://localhost:8082}"  # gateway devops
CONN_AUTH="${DITTO_AUTH_DEVOPS:-devops:foobar}"
CONN_NAME="WareTwin local MQTT connection"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "→ Policy waretwin:waretwin-policy ($DITTO)"
curl --fail-with-body -sS -u "$DITTO_AUTH" -X PUT -H 'Content-Type: application/json' \
  --data @"$DIR/policy.json" \
  "$DITTO/api/2/policies/waretwin:waretwin-policy" >/dev/null

echo "→ Connection '$CONN_NAME' ($CONN_URL)"
CONN_ID="$(curl -sS -u "$CONN_AUTH" "$CONN_URL/api/2/connections" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(next((c['id'] for c in d if c.get('name')=='$CONN_NAME'),''))")"

if [ -n "$CONN_ID" ]; then
  echo "  đã có ($CONN_ID) — cập nhật"
  TMP="$(mktemp)"
  python3 -c "import json;d=json.load(open('$DIR/waretwin_connection.json'));d['id']='$CONN_ID';json.dump(d,open('$TMP','w'))"
  curl --fail-with-body -sS -u "$CONN_AUTH" -X PUT -H 'Content-Type: application/json' \
    --data @"$TMP" "$CONN_URL/api/2/connections/$CONN_ID" >/dev/null
  rm -f "$TMP"
else
  echo "  chưa có — tạo mới"
  curl --fail-with-body -sS -u "$CONN_AUTH" -X POST -H 'Content-Type: application/json' \
    --data @"$DIR/waretwin_connection.json" "$CONN_URL/api/2/connections" >/dev/null
fi

echo "→ Trạng thái kết nối"
curl -sS -u "$CONN_AUTH" "$CONN_URL/api/2/connections" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);[print(' ',c['id'],c['connectionStatus'],c.get('name')) for c in d if c.get('name')=='$CONN_NAME']"

echo "✔ Xong. Bật backend với TWIN_MQTT_ENABLED=1 để bắt đầu đẩy trạng thái."
