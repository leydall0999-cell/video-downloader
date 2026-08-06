#!/usr/bin/env bash
# ==============================================================================
# 独立解说 worker 启动脚本（部署到强机后执行）
#
# 前置（系统级，需在脚本外装好）：
#   Debian/Ubuntu :  apt-get install -y ffmpeg fonts-noto-cjk
#   CentOS/RHEL   :  dnf install -y ffmpeg google-noto-sans-cjk-ttc-fonts
#                    （ffmpeg 可能要走 rpmfusion 或装静态包）
#   macOS(本地调试):  brew install ffmpeg && 字体用系统自带 PingFang 即可
#
# 用法：
#   COMMENTARY_BASE=/opt/commentary-pipeline WORKER_PORT=8100 ./start_worker.sh
#
# 环境变量：
#   COMMENTARY_PYTHON  解释器（默认 python3）；建议用 venv 内的 python
#   COMMENTARY_BASE    管线根目录（默认脚本所在目录）
#   WORKER_PORT        监听端口（默认 8100）
#   WORKER_MAX_CONCURRENCY  同时渲染数（默认 1，渲染吃 CPU，强机可调 2~3）
#   WORKER_TIMEOUT     单任务超时秒（默认 1800）
# ==============================================================================
set -euo pipefail
cd "$(dirname "$0")"

PY="${COMMENTARY_PYTHON:-$(command -v python3)}"
PIPE_BASE="${COMMENTARY_BASE:-$(pwd)}"
PORT="${WORKER_PORT:-8100}"

# 1) 建并激活 venv（首次）
if [ ! -d .venv ]; then
  echo "[worker] 创建虚拟环境 .venv ..."
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# 2) 装 Python 依赖（ffmpeg / 中文字体是系统级，见文件头注释）
echo "[worker] 安装依赖 ..."
pip install --upgrade pip -q
pip install -r requirements.txt -q

# 3) 起服务
export COMMENTARY_BASE="$PIPE_BASE"
echo "[worker] 启动 commentary_worker :$PORT (BASE=$PIPE_BASE)"
exec uvicorn commentary_worker:app --host 0.0.0.0 --port "$PORT"
