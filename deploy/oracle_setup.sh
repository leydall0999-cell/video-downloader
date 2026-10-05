#!/usr/bin/env bash
# =============================================================
# VDL · Oracle Cloud Free Tier（Ubuntu 22.04, aarch64）一键部署
# 用法（AI 拿到 Oracle 公网 IP + 你的 SSH 公钥已注入后）：
#   Oracle Ubuntu 镜像默认登录用户是 ubuntu（不是 root），用 sudo 跑：
#   scp deploy/oracle_setup.sh ubuntu@<ORACLE_IP>:/tmp/ && ssh ubuntu@<ORACLE_IP> 'sudo bash /tmp/oracle_setup.sh'
#   或：ssh ubuntu@<ORACLE_IP> 'sudo bash -s' < deploy/oracle_setup.sh
#
# 环境变量（可选）：
#   VDL_REPO       git 仓库地址（默认下方占位，AI 部署时按本地 origin 填真实 URL）
#   VDL_BRANCH     app-dev
#   CF_TUNNEL_TOKEN Cloudflare Tunnel token（不传则只起 vdl-web，Tunnel 后续手动接）
#   VDL_PORT       8888
# =============================================================
set -euo pipefail

VDL_REPO="${VDL_REPO:-https://github.com/OWNER/video-downloader.git}"
VDL_BRANCH="${VDL_BRANCH:-app-dev}"
VDL_PORT="${VDL_PORT:-8888}"
INSTALL_DIR="/opt/vdl"
SVC_USER="vdl"

echo "==> [1/7] 系统更新 + 基础依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip python3-dev \
  ffmpeg git curl ca-certificates ufw openssh-server

echo "==> [2/7] 基线加固（非 root 用户 + ufw 只放 22）"
id -u "$SVC_USER" &>/dev/null || useradd -m -s /bin/bash "$SVC_USER"
# 允许 vdl 用 sudo（后续维护）；如不需要可去掉
echo "$SVC_USER ALL=(ALL) NOPASSWD:ALL" >/etc/sudoers.d/$SVC_USER
chmod 440 /etc/sudoers.d/$SVC_USER
# SSH：禁 root 密码登录（key 已注入），保留 root 密钥登录以便 AI 接入
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw --force enable

echo "==> [3/7] 拉代码（$VDL_BRANCH）"
if [ ! -d "$INSTALL_DIR" ]; then
  git clone --branch "$VDL_BRANCH" --depth 1 "$VDL_REPO" "$INSTALL_DIR"
else
  git -C "$INSTALL_DIR" fetch origin "$VDL_BRANCH"
  git -C "$INSTALL_DIR" checkout -f "origin/$VDL_BRANCH"
fi
chown -R "$SVC_USER:$SVC_USER" "$INSTALL_DIR"

echo "==> [4/7] Python venv + 依赖（torch 在 requirements.txt 为注释态，不装）"
python3 -m venv "$INSTALL_DIR/.venv"
# 升级 pip 并装依赖；ARM aarch64 下 onnxruntime/pymupdf 均有官方 manylinux wheel
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"
# yt-dlp 由 app 内 ydlp_update.bootstrap() 自动更新；这里先装一份基线
"$INSTALL_DIR/.venv/bin/pip" install --upgrade yt-dlp

echo "==> [5/7] systemd 单元 vdl-web.service（127.0.0.1:$VDL_PORT）"
cat >/etc/systemd/system/vdl-web.service <<EOF
[Unit]
Description=VideoDownloader Web (FastAPI)
After=network-online.target
Wants=network-online.target

[Service]
User=$SVC_USER
WorkingDirectory=$INSTALL_DIR/server
Environment=VDL_DOWNLOAD_DIR=$INSTALL_DIR/downloads
Environment=VDL_FFMPEG_BIN=/usr/bin/ffmpeg
ExecStart=$INSTALL_DIR/.venv/bin/uvicorn app:app --host 127.0.0.1 --port $VDL_PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
mkdir -p "$INSTALL_DIR/downloads"
chown -R "$SVC_USER:$SVC_USER" "$INSTALL_DIR/downloads"
systemctl daemon-reload
systemctl enable --now vdl-web.service
sleep 3
echo "    vdl-web 状态："; systemctl is-active vdl-web.service || systemctl status vdl-web.service --no-pager | tail -20

echo "==> [6/7] 本地自检（不经过 Tunnel）"
curl -s -o /dev/null -w "local /api/version http=%{http_code}\n" "http://127.0.0.1:$VDL_PORT/api/version" || echo "本地自检失败，查看 journalctl -u vdl-web"

echo "==> [7/7] Cloudflare Tunnel（如提供 token）"
if [ -n "${CF_TUNNEL_TOKEN:-}" ]; then
  echo "    安装 cloudflared (arm64) ..."
  curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb -o /tmp/cf.deb
  dpkg -i /tmp/cf.deb || apt-get install -f -y
  cloudflared service install "$CF_TUNNEL_TOKEN"
  systemctl enable --now cloudflared
  echo "    Tunnel 已装；去 Cloudflare 控制台给 hanyuxz.top 加 Public Hostname -> http://localhost:$VDL_PORT"
else
  echo "    未传 CF_TUNNEL_TOKEN：vdl-web 已起，Tunnel 后续手动接（见 ORACLE_DEPLOY_GUIDE.md 第 4 节）"
fi

echo "============================================================"
echo "部署完成。下一步："
echo "  1) 实测出网国外：ssh root@<IP> 后 curl -4 https://www.youtube.com 确认 http=200"
echo "  2) 提供 CF_TUNNEL_TOKEN 接 hanyuxz.top（或手动在 Cloudflare 控制台加 Public Hostname）"
echo "  3) 验证 https://hanyuxz.top/api/version 返回 200（不再是 Railway 404）"
echo "============================================================"
