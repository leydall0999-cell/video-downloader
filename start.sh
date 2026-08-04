#!/bin/sh
# 启动脚本：由部署平台（Railway/Render/Fly）把 PORT 作为真实环境变量注入，
# 运行时由本脚本读取，避免 Dockerfile CMD 层面的 $PORT 变量替换坑。
set -e

PORT="${PORT:-8080}"
HOST="${HOST:-0.0.0.0}"

echo "▶ starting uvicorn on host=$HOST port=$PORT"
exec uvicorn app:app --app-dir server --host "$HOST" --port "$PORT"
