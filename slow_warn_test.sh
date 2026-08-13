#!/usr/bin/env bash
# 验证慢速告警：提交已知慢速腾讯视频，轮询任务状态，确认 slow_warning 出现
set -u
BASE="http://localhost:8321"
URL="https://v.qq.com/x/cover/mcv8hkc8zk8lnov/t0043wtgjyt.html"

echo "=== submit download (slow video, best) ==="
SUB=$(curl -s -m 40 -X POST "$BASE/api/download" -H 'Content-Type: application/json' \
  -d "{\"url\":\"$URL\",\"quality\":\"best\"}")
echo "$SUB"
TID=$(echo "$SUB" | python3 -c 'import sys,json;print(json.load(sys.stdin)["task_id"])')
echo "TID=$TID"

echo "=== poll up to 50s for slow_warning ==="
for i in $(seq 1 17); do
  T=$(curl -s -m 10 "$BASE/api/tasks/$TID")
  echo "$T" | python3 -c '
import sys,json
d=json.load(sys.stdin)
sw=d.get("slow_warning") or {}
print(f"[{d[\"status\"]}] prog={d[\"progress\"]:.1f}% speed={d[\"speed\"]/1024:.1f}KB/s warn={bool(sw)} msg={sw.get(\"message\",\"\")[:40]} sug={sw.get(\"suggested_quality_keys\")}")
'
  if [ -n "$(echo "$T" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(1 if d.get(\"slow_warning\") else \"\")')" ]; then
    echo ">>> SLOW WARNING TRIGGERED"
    break
  fi
  sleep 3
done

echo "=== cleanup: cancel ==="
curl -s -m 10 -X DELETE "$BASE/api/tasks/$TID" >/dev/null 2>&1 || true
echo done
