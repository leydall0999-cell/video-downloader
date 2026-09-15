#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# enable_nginx.sh —— 让网关复用唯一放行的公网端口 8888
#
# 为什么必须绕这一层：阿里云安全组只放行 8888，网关新起端口（8889/8080/8443 等）
# 实测全部不通。所以只能在 8888 上按路径分流：
#     /gw/  → 127.0.0.1:8890   (vdl-gateway)
#     /     → 127.0.0.1:<新端口> (vdl-web，原本独占 8888)
#
# 回滚：
#    sed -i 's/--host 127.0.0.1 --port 18891/--host 0.0.0.0 --port 8888/' \
#        /etc/systemd/system/vdl-web.service && systemctl daemon-reload && \
#        systemctl restart vdl-web && systemctl stop nginx
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

WEB_SERVICE=/etc/systemd/system/vdl-web.service
CONF_SRC=/tmp/vdl-gw-install/nginx-vdl-gateway.conf
BACKUP_DIR=/opt/vdl-gateway/backup

echo "▶ 0) 备份现有 unit"
mkdir -p "$BACKUP_DIR"
cp "$WEB_SERVICE" "$BACKUP_DIR/vdl-web.service.bak-$(date +%Y%m%d-%H%M%S)"
echo "   已备份到 $BACKUP_DIR"

echo "▶ 1) 为 vdl-web 挑一个空闲本机端口（18888/18889 已被占用）"
NEW_PORT=$(python3 - <<'PY'
import socket
for p in range(18890, 18920):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", p))
    except OSError:
        continue
    finally:
        s.close()
    print(p)
    break
PY
)
[ -n "$NEW_PORT" ] || { echo "❌ 找不到空闲端口" >&2; exit 1; }
echo "   选用 127.0.0.1:$NEW_PORT"

echo "▶ 2) 安装 nginx"
if ! command -v nginx >/dev/null 2>&1; then
  apt-get -qq update
  DEBIAN_FRONTEND=noninteractive apt-get -qq install -y nginx
fi
nginx -v

echo "▶ 3) 把 vdl-web 改到本机端口 $NEW_PORT"
sed -i "s|--host 0\.0\.0\.0 --port 8888|--host 127.0.0.1 --port $NEW_PORT|" "$WEB_SERVICE"
grep -n "ExecStart" "$WEB_SERVICE"
systemctl daemon-reload
systemctl restart vdl-web
sleep 3
systemctl is-active --quiet vdl-web && echo "   vdl-web: active (127.0.0.1:$NEW_PORT)" || { echo "❌ vdl-web 未启动"; exit 1; }
curl -s -m 5 "http://127.0.0.1:$NEW_PORT/api/version" | head -c 120 || true
echo

echo "▶ 4) 写入 nginx 配置"
sed "s|127\.0\.0\.1:18888|127.0.0.1:$NEW_PORT|" "$CONF_SRC" > /etc/nginx/sites-available/vdl-gateway
ln -sf /etc/nginx/sites-available/vdl-gateway /etc/nginx/sites-enabled/vdl-gateway
rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true
nginx -t

echo "▶ 5) 启动 nginx"
systemctl enable nginx >/dev/null 2>&1 || true
systemctl restart nginx
sleep 2
systemctl is-active --quiet nginx && echo "   nginx: active" || { echo "❌ nginx 未启动"; exit 1; }

echo "▶ 6) 公网路径验证"
echo -n "   /gw/health        → "; curl -s -m 8 http://127.0.0.1:8888/gw/health | head -c 160; echo
echo -n "   /api/version      → "; curl -s -m 8 http://127.0.0.1:8888/api/version | head -c 120; echo
echo
echo "✅ 完成。公网 http://8.138.223.3:8888/gw/ 即网关，其余路径仍是原服务。"
