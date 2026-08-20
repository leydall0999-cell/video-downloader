#!/usr/bin/env bash
# 部署爱奇艺 Playwright worker 到 VPS 并重启 cookie daemon。
# 用法: bash deploy_iqiyi.sh <user@host> [ssh端口，默认22]
# 示例: bash deploy_iqiyi.sh root@8.138.223.3
set -euo pipefail

VPS_HOST="${1:?用法: bash deploy_iqiyi.sh <user@host> [ssh端口]}"
SSH_PORT="${2:-22}"
DIR="/opt/vdl-worker"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "==> [1/3] 上传 iqiyi_resolve.py + vdl_cookie_daemon.py 到 $VPS_HOST:$DIR/"
scp -P "$SSH_PORT" "$HERE/iqiyi_resolve.py" "$HERE/vdl_cookie_daemon.py" "$VPS_HOST:$DIR/"

echo "==> [2/3] VPS 上验证模块可导入（Playwright 环境检查）"
ssh -p "$SSH_PORT" "$VPS_HOST" "cd $DIR && python3 -c 'import iqiyi_resolve, vdl_cookie_daemon; print(\"import OK: iqiyi_resolve + vdl_cookie_daemon\")'"

echo "==> [3/3] 重启 daemon（强杀所有 daemon + 释端口 + 起新 daemon 加载最新代码）"
ssh -p "$SSH_PORT" "$VPS_HOST" "cd $DIR && for pid in \$(ls /proc 2>/dev/null | grep -E '^[0-9]+\$'); do c=\$(cat /proc/\$pid/cmdline 2>/dev/null | tr '\0' ' '); echo \"\$c\" | grep -q 'vdl_cookie_daemon.py' && [ \"\$pid\" != \"\$\$\" ] && kill -9 \$pid 2>/dev/null; done; sleep 1; fuser -k -n tcp 18731 2>/dev/null; sleep 1; nohup .venv/bin/python vdl_cookie_daemon.py >> daemon.log 2>\&1 & disown; sleep 3; echo '--- 新 daemon ---'; for pid in \$(ls /proc 2>/dev/null | grep -E '^[0-9]+\$'); do c=\$(cat /proc/\$pid/cmdline 2>/dev/null | tr '\0' ' '); echo \"\$c\" | grep -q 'vdl_cookie_daemon.py' && echo \"PID \$pid 启动 \$(stat -c %y /proc/\$pid 2>/dev/null | cut -d. -f1)\"; done; echo '--- healthz ---'; curl -s http://127.0.0.1:18731/healthz; echo"

echo "==> 完成。验证 daemon 加载的是最新代码（启动时间应 > 部署时间）："
echo "  端到端测：curl -s --max-time 120 'http://127.0.0.1:18731/v1/resolve?token=<VDL_COOKIE_API_TOKEN>&platform=iqiyi&url=<爱奇艺分享链接>'"
echo "  （若 daemon 实际由 systemd/supervisor 管理，请改用其 restart 命令，勿用 nohup 方式）"
