#!/usr/bin/env bash
# 测速：提交腾讯视频下载，轮询打印速度，观察并发提升效果
BASE="http://localhost:8321"
URL="${1:-https://v.qq.com/x/cover/mcv8hkc8zk8lnov/t0043wtgjyt.html}"
SECS="${2:-60}"
TID=$(curl -s -m 30 -X POST "$BASE/api/download" -H 'Content-Type: application/json' -d "{\"url\":\"$URL\",\"quality\":\"best\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["task_id"])')
echo "task_id=$TID  (测速 $SECS 秒)"
END=$((SECONDS+SECS))
MAX=0
while [ $SECONDS -lt $END ]; do
  T=$(curl -s -m 10 "$BASE/api/tasks/$TID")
  echo "$T" | python3 -c '
import sys,json
d=json.load(sys.stdin)
st=d.get("status","?")
sp=d.get("speed",0) or 0
prog=d.get("progress",0) or 0
b=d.get("downloaded_bytes",0) or 0
tb=d.get("total_bytes",0) or 0
err=d.get("error","")
print(f"status={st} prog={prog:.1f}% speed={sp/1024:.1f}KB/s bytes={b}/{tb} err={err}")
'
  sleep 5
done
echo "=== final ==="
curl -s -m 10 "$BASE/api/tasks/$TID" | python3 -c '
import sys,json
d=json.load(sys.stdin)
print("status:", d.get("status"))
print("speed:", round((d.get("speed",0) or 0)/1024,1), "KB/s")
print("progress:", d.get("progress"), "%")
print("error:", d.get("error",""))
'
