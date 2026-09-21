#!/usr/bin/env bash
# remote_setup.sh — 把 VDL 网页端一键部署到境外 VPS（Debian 12 / Ubuntu 22.04）
#
# 在【本机】运行，不是 VPS 上：
#   bash deploy/remote_setup.sh root@<VPS公网IP>
#   bash deploy/remote_setup.sh root@<IP> deploy/.env      # 附带配置（回源凭据等）
#
# 它会自动完成：
#   0) 配 2G swap（canvas 原生模块编译 + ffmpeg 合并都吃内存，小机器保底）
#   1) 装系统依赖：python3-venv / ffmpeg / git / build-essential / cairo 系库 / Node.js 22
#   2) rsync 推代码 → /opt/vdl
#   3) 建 venv + 装 requirements.txt
#   4) 编译 bgutil PO token server → /opt/bgutil（YouTube bot 检测绕过，缺了就下不了 YouTube）
#   5) 写 systemd 单元（用 start.py 启动，它会自动拉起 bgutil）→ 启动
#   6) 自检：服务状态 / bgutil:4416 监听 / YouTube 可达性
#
# 前置条件：
#   1. 已在 VPS 控制台把本机 SSH 公钥（桌面「VPS部署SSH公钥.txt」）加进机器
#   2. VPS 全新 Debian 12 / Ubuntu 22.04，能 root SSH 登录
#   3. 配置建议 2核2G / 40G 盘；**2核1G 同样可用**（脚本按实际内存自动加到 3G swap 兜底）。
#      若 1G 机型在编译或运行时仍 OOM，可在控制台**原地升配到同规格族的 2G 档**——
#      阿里云官方「升级套餐配置」：仅支持同规格族内升配（跨族如国际型↔通用型不行），
#      IP / 数据 / 系统 / 防火墙均不变，按天补差价，升级时重启一次。
set -euo pipefail

REMOTE="${1:?用法: bash deploy/remote_setup.sh root@<VPS公网IP> [deploy/.env]}"
ENV_FILE="${2:-}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 读配置（可选）
VDL_PROXY_CN=""
VDL_INSTANCE="cloud"
if [[ -n "$ENV_FILE" && -f "$ENV_FILE" ]]; then
  set -a; source "$ENV_FILE"; set +a
fi

# 默认 8080 而非 8000：Cloudflare 橙云（Proxied）回源只允许
#   HTTP  80 / 8080 / 8888 / 2052 / 2082 / 2086 / 2095
#   HTTPS 443 / 2053 / 2083 / 2087 / 2096 / 8443
# 8000 不在白名单内 —— 若以后把 hanyuxz.top 挂成橙云会连不上。
# 需要改端口：在 deploy/.env 里设 VDL_HTTP_PORT=xxxx。
APP_PORT="${VDL_HTTP_PORT:-8080}"

# SSH 公共参数：必须带 StrictHostKeyChecking=accept-new，否则**首次连接会卡在
# "Are you sure you want to continue connecting?" 交互提示**上（后台/非交互运行时直接挂死）。
# 之前漏了这一条，导致脚本在 0/6 步之后卡住 3 分钟无任何输出。
SSH_COMMON="-o StrictHostKeyChecking=accept-new -o LogLevel=ERROR -o ConnectTimeout=15"

GREEN=$'\033[0;32m'; RED=$'\033[0;31m'; YEL=$'\033[0;33m'; NC=$'\033[0m'
ok(){ echo "${GREEN}✓${NC} $*"; }
bad(){ echo "${RED}✗${NC} $*"; }
warn(){ echo "${YEL}!${NC} $*"; }

# 未显式配置回源凭据时，自动从国内 ECS(8.138.223.3) 的 cn_proxy 读取
if [[ -z "$VDL_PROXY_CN" ]]; then
  if ssh -o ConnectTimeout=8 -o BatchMode=yes -o StrictHostKeyChecking=no root@8.138.223.3 'true' 2>/dev/null; then
    # ⚠️ service 那一行是 `Environment=CN_PROXY_AUTH=user:pass`（含 3 个 `=`），
    # 用 `cut -d= -f2` 会取到变量名 `CN_PROXY_AUTH` 而不是值 → 认证必失败（407）。
    # 必须用 sed 剥掉前缀再取值。
    _auth=$(ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=no root@8.138.223.3 \
      "sed -n 's/^.*CN_PROXY_AUTH=//p' /etc/systemd/system/cn_proxy.service | head -1" 2>/dev/null | tr -d '\r\n')
    if [[ -n "$_auth" ]]; then
      VDL_PROXY_CN="http://${_auth}@8.138.223.3:18888"
      ok "已从国内 ECS 自动读取回源凭据"
    fi
  fi
fi

echo "==> 部署目标 : $REMOTE"
echo "==> 项目目录 : $PROJECT_DIR"

# 0. swap（编译 canvas/tsc、ffmpeg 合并、node+uvicorn 常驻都吃内存）
#    按物理内存自适应：≤1.5G 的机型给 3G swap 兜底，其余 2G。
ok "0/6 按内存自适应配置 swap…"
ssh $SSH_COMMON "$REMOTE" 'set -e
  MEM_KB=$(grep -m1 MemTotal /proc/meminfo | tr -dc "0-9")
  if [ "${MEM_KB:-0}" -lt 1500000 ]; then SWAP_MB=3072; else SWAP_MB=2048; fi
  if ! swapon --show 2>/dev/null | grep -q "/swapfile"; then
    (fallocate -l ${SWAP_MB}M /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=$SWAP_MB status=none)
    chmod 600 /swapfile
    mkswap -q /swapfile >/dev/null
    swapon /swapfile
    grep -q "^/swapfile" /etc/fstab || echo "/swapfile none swap sw 0 0" >> /etc/fstab
  fi
  # 小内存机型更早借用 swap，降低被 OOM killer 杀掉的概率
  sysctl -w vm.swappiness=70 >/dev/null 2>&1 || true
  grep -q "^vm.swappiness" /etc/sysctl.conf || echo "vm.swappiness=70" >> /etc/sysctl.conf
  echo "内存 ${MEM_KB}KB → swap ${SWAP_MB}MB"
  free -h | head -2
  if [ "${MEM_KB:-0}" -lt 1500000 ]; then
    echo "[提示] 小内存机型（<1.5G）：已加大 swap。若编译或运行时仍 OOM，可在控制台原地升配到同规格族 2G 档（IP/数据不变，补差价）。"
  fi'

# 1. 系统依赖（含 bgutil 编译所需的 cairo 系开发库 + Node >= 22）
ok "1/6 安装系统依赖 (python / ffmpeg / Node22 / canvas 编译库)…"
ssh $SSH_COMMON "$REMOTE" 'set -e
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y -q python3-venv python3-pip python3-dev ffmpeg curl rsync ca-certificates gnupg git \
    build-essential pkg-config \
    libcairo2-dev libpango1.0-dev libjpeg-dev libgif-dev librsvg2-dev libpixman-1-dev \
    libcairo2 libpango1.0-0 libjpeg62-turbo libgif7 librsvg2-2 unzip
  # Node >= 22：bgutil PO token server 的运行要求（Debian 12 自带 nodejs 仅 18，不够）
  if ! command -v node >/dev/null 2>&1 || [ "$(node -v | tr -d v | cut -d. -f1)" -lt 22 ]; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
    apt-get install -y -q nodejs
  fi
  # deno：yt-dlp 解 YouTube n 签名/JS 挑战所需的 JS runtime。
  # 缺了会打 "Signature solving failed" / "n challenge solving failed"
  # → 拿到的下载地址签名无效 → googlevideo 分片 403（2026-09-21 实测）。
  if ! command -v deno >/dev/null 2>&1; then
    curl -fsSL --max-time 180 -o /tmp/deno.zip \
      https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip
    unzip -o -q /tmp/deno.zip -d /usr/local/bin
    chmod +x /usr/local/bin/deno
    rm -f /tmp/deno.zip
  fi
  echo "python: $(python3 --version 2>&1) | ffmpeg: $(ffmpeg -version | head -1) | node: $(node -v) | deno: $(deno --version | head -1)"'

# 2. 推送代码（排除大目录/缓存，避免传几 GB 的 build/dist）
ok "2/6 推送代码到 VPS (/opt/vdl)…"
ssh $SSH_COMMON "$REMOTE" 'mkdir -p /opt/vdl'
#   ⚠️ .build_venv / .build_tmp 是本机 macOS 的构建产物（含 .dylib/.so 二进制，~400MB），
#   推到 Linux 服务器毫无用处 —— 必须排除，否则白传几百 MB。
rsync -az --delete -e "ssh $SSH_COMMON" \
  --exclude '.git' --exclude '.venv' --exclude '.build_venv' \
  --exclude '.build_tmp' --exclude '.pytest_cache' \
  --exclude '__pycache__' \
  --exclude '*.pyc' --exclude 'build' --exclude 'dist' \
  --exclude 'backup' --exclude 'downloads' --exclude 'commentary_out' \
  --exclude 'node_modules' --exclude '.workbuddy' --exclude '.idea' \
  --exclude 'tests' --exclude '.github' \
  "$PROJECT_DIR/" "$REMOTE:/opt/vdl/"

# 3. venv + 依赖
ok "3/6 创建 venv 并安装依赖 (首次约几分钟)…"
ssh $SSH_COMMON "$REMOTE" 'set -e
  cd /opt/vdl
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt'

# 4. bgutil PO token server（YouTube bot 检测绕过）
#    没有它 → YouTube 数据中心 IP 匿名请求会被 "Sign in to confirm you're not a bot" 拦死。
#    start.py 启动时会在 /opt/bgutil/server/build/main.js 拉起它（监听 127.0.0.1:4416）。
#    ⚠️ 这里的版本必须与 requirements.txt 里 bgutil-ytdlp-pot-provider 的大版本一致，
#       否则插件与 HTTP server 不匹配 → PO token 失效 → YouTube 分片一律 403。
ok "4/6 编译 bgutil PO token server (npm ci + tsc，约 2-5 分钟)…"
ssh $SSH_COMMON "$REMOTE" 'set -e
  rm -rf /opt/bgutil
  git clone --depth 1 --single-branch --branch 2.0.0 \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil
  cd /opt/bgutil/server
  npm ci --silent
  npx tsc
  test -f /opt/bgutil/server/build/main.js
  echo "bgutil: /opt/bgutil/server/build/main.js OK"'

# 5. systemd 单元（heredoc 经 stdin 写入，变量在本地展开）
#    用 start.py 而非裸 uvicorn：start.py 会拉起 bgutil 并放宽 WS keepalive（跨境链路必需）。
ok "5/6 写 systemd 单元并启用…"
ssh $SSH_COMMON "$REMOTE" 'cat > /etc/systemd/system/vdl-web.service' <<UNIT
[Unit]
Description=VDL 网页端 (uvicorn via start.py)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/vdl
Environment=HOST=0.0.0.0
Environment=PORT=${APP_PORT}
Environment=VDL_INSTANCE=${VDL_INSTANCE}
Environment=VDL_REGION=global
Environment=VDL_FFMPEG_BIN=/usr/bin/ffmpeg
Environment=VDL_PROXY_CN=${VDL_PROXY_CN}
ExecStart=/opt/vdl/.venv/bin/python start.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
ssh $SSH_COMMON "$REMOTE" 'systemctl daemon-reload; systemctl enable --now vdl-web; systemctl restart vdl-web'

# 6. 自检
ok "6/6 等待启动并自检…"
if ssh $SSH_COMMON "$REMOTE" 'sleep 5; systemctl is-active --quiet vdl-web'; then
  echo "    vdl-web: active"
else
  bad "服务未起来，查看日志:"
  ssh $SSH_COMMON "$REMOTE" 'journalctl -u vdl-web -n 40 --no-pager' || true
  exit 1
fi

# bgutil / YouTube 自检
ssh $SSH_COMMON "$REMOTE" 'echo "--- bgutil PO token server ---"
  grep -m2 "\[bgutil\]" /tmp/bgutil-pot-server.log 2>/dev/null || echo "(暂无 bgutil 日志)"
  (ss -tln 2>/dev/null | grep -q ":4416" && echo "✓ 4416 正在监听") || echo "✗ 4416 未监听 —— YouTube 可能被 bot 检测拦截"
  (command -v deno >/dev/null 2>&1 && echo "✓ deno $(deno --version | head -1)") || echo "✗ deno 未安装 —— n 签名解不出，YouTube 下载必 403"
  echo "--- YouTube 真实下载自检（不是只看 HTTP 可达）---"
  # ⚠️ 必须用 yt-dlp 原生下载器整体下载一个小格式：
  #    --download-sections 会走 ffmpeg，而 ffmpeg 不带 yt-dlp 的请求头 → 必然 403，
  #    会把「链路正常」误判为失败（2026-09-21 踩过）。
  cd /opt/vdl
  if timeout 240 .venv/bin/python -m yt_dlp --no-progress --newline -f 18 \
       -o /tmp/_vdl_selftest.%(ext)s "https://www.youtube.com/watch?v=dQw4w9WgXcQ" \
       >/tmp/_vdl_selftest.log 2>&1 && ls /tmp/_vdl_selftest.* >/dev/null 2>&1; then
    echo "✓ YouTube 下载链路正常（PO token + JS runtime 均生效）"
    rm -f /tmp/_vdl_selftest.*
  else
    echo "✗ YouTube 下载失败！排查顺序："
    echo "   ① deno 是否安装（n 签名）"
    echo "   ② requirements.txt 的 bgutil-ytdlp-pot-provider 版本是否 == /opt/bgutil 的 git 分支"
    echo "   ③ yt-dlp 是否 >= 2026.08，且 downloader.py 的 player_client 为 web_safari"
    tail -12 /tmp/_vdl_selftest.log 2>/dev/null
  fi'

PUB_IP=$(ssh $SSH_COMMON "$REMOTE" 'curl -fsS --max-time 8 https://api.ipify.org || hostname -I' | awk '{print $1}')
echo
ok "部署完成！浏览器打开:  http://${PUB_IP}:${APP_PORT}"
echo "    （去云厂商控制台安全组放行 TCP ${APP_PORT}；仅自己用时可限制来源 IP）"
if [[ -z "$VDL_PROXY_CN" ]]; then
  warn "未配置 VDL_PROXY_CN：B站/抖音等国内平台无法回源。把国内 ECS 的 cn_proxy 凭据填入 deploy/.env 后重跑本脚本即可。"
fi
