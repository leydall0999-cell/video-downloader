#!/usr/bin/env bash
# ==============================================================================
# 独立解说 worker 一键部署（在目标强机上以 root 运行）
# ------------------------------------------------------------------------------
# 前置：Ubuntu / Debian，已联网；本脚本会自动安装 Docker（如未装）。
# 用法：
#   WORKER_TOKEN=$(openssl rand -hex 16) bash deploy.sh
#   或先 export WORKER_TOKEN=你的强随机串 再 bash deploy.sh
# 可选环境变量：
#   WORKER_PORT              对外端口（默认 8100）
#   WORKER_MAX_CONCURRENCY  同时渲染数（默认 1；强机可 2~3）
#   WORKER_TIMEOUT          单任务超时秒（默认 1800）
#
# 部署后：把「http://<本机公网IP>:端口」和 WORKER_TOKEN 发给主站运维，
#        由主站设置 VDL_COMMENTARY_MODE=http / VDL_COMMENTARY_ENDPOINT=<地址>
#        / VDL_COMMENTARY_TOKEN=<token> 并 redeploy，解说功能即上线。
# ==============================================================================
set -euo pipefail

WORKER_PORT="${WORKER_PORT:-8100}"
WORKER_TOKEN="${WORKER_TOKEN:-}"   # 强烈建议设置，否则公网无鉴权
MAX_CONC="${WORKER_MAX_CONCURRENCY:-1}"
TIMEOUT="${WORKER_TIMEOUT:-1800}"

SRC="$(cd "$(dirname "$0")" && pwd)"

echo "[deploy] 端口=${WORKER_PORT} 并发=${MAX_CONC} 鉴权=${WORKER_TOKEN:+已设}${WORKER_TOKEN:-未设}"

# 1) 安装 Docker（如缺失）
if ! command -v docker >/dev/null 2>&1; then
  echo "[deploy] 安装 Docker ..."
  curl -fsSL https://get.docker.com | sh
fi

# 2) 构建镜像（构建上下文 = 本目录）
echo "[deploy] 构建镜像 commentary-worker ..."
docker build -t commentary-worker "$SRC"

# 3) 启动容器
echo "[deploy] 启动容器 ..."
docker rm -f commentary-worker 2>/dev/null || true
docker run -d --restart unless-stopped \
  --name commentary-worker \
  -p "${WORKER_PORT}:8100" \
  -e WORKER_TOKEN="${WORKER_TOKEN}" \
  -e WORKER_MAX_CONCURRENCY="${MAX_CONC}" \
  -e WORKER_TIMEOUT="${TIMEOUT}" \
  commentary-worker

# 4) 健康检查
sleep 4
if curl -fsS "http://127.0.0.1:${WORKER_PORT}/health" >/dev/null; then
  echo "[deploy] 健康检查通过 ✅"
else
  echo "[deploy] 健康检查失败，查看日志: docker logs commentary-worker"
fi

echo "=========================================================="
echo "worker 地址: http://<本机公网IP>:${WORKER_PORT}"
echo "WORKER_TOKEN(主站 VDL_COMMENTARY_TOKEN 填这个): ${WORKER_TOKEN:-<未设置, 请重跑并设置>}"
echo "查看状态: docker logs -f commentary-worker"
echo "=========================================================="
