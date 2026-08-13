#!/usr/bin/env bash
# 端到端：解析→下载→取文件→校验
set -u
BASE="http://localhost:8321"
URL="https://v.qq.com/x/page/q326831cny0.html"
QUALITY="best"
OUTDIR="/Users/suixindelang/WorkBuddy/2026-08-09-09-38-52/video-downloader/e2e_out"
mkdir -p "$OUTDIR"

echo "== [1/4] resolve =="
RESOLVE=$(curl -s -m 60 -X POST "$BASE/api/resolve" -H 'Content-Type: application/json' -d "{\"url\":\"$URL\"}")
echo "$RESOLVE" | python3 -m json.tool 2>/dev/null | head -20
echo

echo "== [2/4] submit download (quality=$QUALITY) =="
SUB=$(curl -s -m 30 -X POST "$BASE/api/download" -H 'Content-Type: application/json' -d "{\"url\":\"$URL\",\"quality\":\"$QUALITY\"}")
echo "$SUB"
TASK_ID=$(echo "$SUB" | python3 -c 'import sys,json;print(json.load(sys.stdin)["task_id"])')
echo "task_id=$TASK_ID"
echo

echo "== [3/4] poll task =="
STATUS=""
for i in $(seq 1 120); do
  T=$(curl -s -m 15 "$BASE/api/tasks/$TASK_ID")
  STATUS=$(echo "$T" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("status","?"))' 2>/dev/null)
  PROG=$(echo "$T" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("progress",0),d.get("speed",0),d.get("filesize",0),d.get("error",""))' 2>/dev/null)
  echo "[$i] status=$STATUS progress=$PROG"
  if [ "$STATUS" = "completed" ] || [ "$STATUS" = "failed" ]; then
    break
  fi
  sleep 5
done
echo

if [ "$STATUS" != "completed" ]; then
  echo "!! task did not complete: $STATUS"
  echo "$T" | python3 -m json.tool 2>/dev/null
  exit 2
fi

echo "== [4/4] fetch file =="
FNAME=$(echo "$T" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("filename",""))' 2>/dev/null)
curl -s -m 300 -o "$OUTDIR/${TASK_ID}.mp4" "$BASE/api/tasks/$TASK_ID/file"
echo "saved -> $OUTDIR/${TASK_ID}.mp4"
ls -lh "$OUTDIR/${TASK_ID}.mp4"
echo "$T" | python3 -m json.tool 2>/dev/null
