#!/usr/bin/env bash
# 启动视频下载站：./run.sh [端口]
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 端口 / 监听地址：命令行参数优先，其次环境变量（部署平台通常注入 PORT），默认 8000 / 127.0.0.1
PORT="${PORT:-${1:-8000}}"
HOST="${HOST:-127.0.0.1}"
MANAGED_VENV="$HOME/.workbuddy/binaries/python/envs/vdl"

pick_python() {
  if [[ -x "$PROJECT_DIR/.venv/bin/python" ]]; then
    echo "$PROJECT_DIR/.venv/bin/python"
  elif [[ -x "$MANAGED_VENV/bin/python" ]]; then
    echo "$MANAGED_VENV/bin/python"
  else
    python3 -m venv "$PROJECT_DIR/.venv"
    "$PROJECT_DIR/.venv/bin/pip" install -q -r "$PROJECT_DIR/requirements.txt"
    echo "$PROJECT_DIR/.venv/bin/python"
  fi
}

PYTHON="$(pick_python)"

command -v ffmpeg >/dev/null 2>&1 || \
  echo "提示：未检测到 ffmpeg，高清视频将无法合并音轨（macOS: brew install ffmpeg）"

echo "服务地址 → http://${HOST}:${PORT}"
exec "$PYTHON" -m uvicorn app:app \
  --app-dir "$PROJECT_DIR/server" \
  --host "$HOST" --port "$PORT"
