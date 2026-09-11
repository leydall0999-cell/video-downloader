#!/usr/bin/env bash
# VDL 海外(香港)节点一键部署 —— 在本机(Mac)运行。
# 目标机: 阿里云/腾讯云 香港轻量, Ubuntu 22.04, 2核2G 起步。
#
# 用法:
#   bash desktop/deploy_hk.sh root@<IP>                 # 仅部署服务 (http://IP:8888)
#   bash desktop/deploy_hk.sh root@<IP> hanyuxz.top     # 同时装 Caddy + 自动 HTTPS
#
# 做法: 从 web-dev 分支 git archive 源码 → 目标机 Docker 构建镜像(含 bgutil
# PO token server, 绕 YouTube 数据中心 IP bot 检测) → docker run 启动 → 冒烟。
# 前提: 本机已能 SSH 免密登录目标机 (ssh-copy-id root@<IP>)。
#
# 2026-09-11 备货预案: 阿里香港轻量补货后, 下单 → ssh-copy-id → 跑本脚本 →
# 按 desktop/HK_NODE_RUNBOOK.md 切域名。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${1:?用法: bash desktop/deploy_hk.sh root@<IP> [域名]}"
DOMAIN="${2:-}"
SRC_REF="web-dev"          # 部署来源分支(勿改: 网页版主干)
APP_DIR="/opt/vdl-src"     # 目标机源码目录
PORT=8888

echo "==> [1/7] 检查 SSH 连通性: ${HOST}"
ssh -o ConnectTimeout=10 -o BatchMode=yes "$HOST" 'echo "  ssh ok"' >/dev/null

echo "==> [2/7] 创建 2G swap (2G 内存机构建镜像防 OOM)"
ssh "$HOST" 'if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo "/swapfile none swap sw 0 0" >> /etc/fstab
  echo "  swap 已创建"; else echo "  swap 已存在"; fi'

echo "==> [3/7] 上传 ${SRC_REF} 源码 → ${HOST}:${APP_DIR} (git archive, 不受本地工作树影响)"
git -C "$REPO" archive "$SRC_REF" \
    server web requirements.txt Dockerfile start.py run.sh yt_dlp_plugins \
  | ssh "$HOST" "mkdir -p '${APP_DIR}' && tar -xf - -C '${APP_DIR}'"

echo "==> [4/7] 安装 Docker (香港机直连官方源, 无需镜像)"
ssh "$HOST" 'command -v docker >/dev/null 2>&1 || curl -fsSL https://get.docker.com | sh'

echo "==> [5/7] 构建镜像 vdl-web (含 bgutil PO token 编译, 首次约 5-10 分钟)"
ssh "$HOST" "cd '${APP_DIR}' && docker build -t vdl-web ."

echo "==> [6/7] 启动容器 (VDL_REGION=global, 端口 ${PORT})"
ssh "$HOST" "docker rm -f vdl-web 2>/dev/null || true
docker run -d --name vdl-web --restart unless-stopped \
  -p ${PORT}:${PORT} -e PORT=${PORT} -e VDL_REGION=global vdl-web"

echo "==> [7/7] 冒烟: 容器内 /api/version"
sleep 3
ssh "$HOST" "curl -fsS http://127.0.0.1:${PORT}/api/version && echo"

if [ -n "$DOMAIN" ]; then
  echo "==> 附加: 安装 Caddy + 自动 HTTPS (${DOMAIN})"
  ssh "$HOST" "apt-get update -qq
apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl gnupg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
apt-get update -qq && apt-get install -y -qq caddy
printf '%s {\n  reverse_proxy 127.0.0.1:${PORT}\n}\n' '${DOMAIN}' > /etc/caddy/Caddyfile
systemctl reload caddy 2>/dev/null || systemctl restart caddy"
  echo "  前提: ${DOMAIN} 的 A 记录已指向本机 IP 且为灰云(DNS only); 证书由 Caddy 自动签发"
  echo "==> 外部验收: curl https://${DOMAIN}/api/version"
else
  echo "==> 未提供域名, 跳过 Caddy。入口 = http://<服务器IP>:${PORT}"
fi

echo "完成。后续步骤见 desktop/HK_NODE_RUNBOOK.md"
