#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# install.sh —— 在广州 ECS 上部署 VDL LLM 网关（真实 Key 只留在服务端）
#
# 用法（在目标机上执行，或 ssh root@host 'bash -s' < install.sh）：
#   VDL_UPSTREAM_KEY=sk-xxx bash install.sh
#
# 环境变量：
#   VDL_UPSTREAM_KEY   上游 API Key（必填，只在首次部署用于生成 upstream.json；
#                      脚本不会把它写进任何日志或 stdout）
#   VDL_UPSTREAM_BASE  上游 base_url，默认 https://api.deepseek.com/v1
#   VDL_UPSTREAM_MODELS 逗号分隔的模型白名单，默认 deepseek-v4-flash,deepseek-chat
#   VDL_TOKEN_NAME     首枚令牌的备注名，默认 mac
#
# 幂等：重跑只补缺失文件，不会覆盖已有 upstream.json / tokens.json。
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

HOME_DIR=/opt/vdl-gateway
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

KEY="${VDL_UPSTREAM_KEY:-}"
BASE_URL="${VDL_UPSTREAM_BASE:-https://api.deepseek.com/v1}"
MODELS="${VDL_UPSTREAM_MODELS:-deepseek-v4-flash,deepseek-chat}"
TOKEN_NAME="${VDL_TOKEN_NAME:-mac}"

echo "▶ 准备目录 $HOME_DIR"
mkdir -p "$HOME_DIR"

echo "▶ 同步代码"
cp "$SRC_DIR/gateway.py" "$HOME_DIR/gateway.py"

echo "▶ 准备 venv"
if [ ! -x "$HOME_DIR/.venv/bin/python" ]; then
  if ! python3 -c "import venv" 2>/dev/null; then
    apt-get -qq update && apt-get -qq install -y python3-venv
  fi
  python3 -m venv "$HOME_DIR/.venv"
fi
"$HOME_DIR/.venv/bin/pip" -q install --disable-pip-version-check fastapi "uvicorn[standard]" httpx

echo "▶ 写入配置"
if [ ! -f "$HOME_DIR/upstream.json" ]; then
  [ -n "$KEY" ] || { echo "❌ 缺少 VDL_UPSTREAM_KEY（首次部署必须提供上游 Key）" >&2; exit 1; }
  python3 - "$HOME_DIR" "$BASE_URL" "$MODELS" <<'PY'
import json, sys, os
from pathlib import Path
home, base, models = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
data = {
    "provider": "deepseek",
    "base_url": base,
    "api_key": os.environ["VDL_UPSTREAM_KEY"],
    "models": [m.strip() for m in models.split(",") if m.strip()],
}
p = home / "upstream.json"
p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
p.chmod(0o600)
print("   upstream.json 已写入（0600）")
PY
else
  echo "   upstream.json 已存在，跳过（如需轮换请手动改）"
fi

if [ ! -f "$HOME_DIR/tokens.json" ]; then
  python3 - "$HOME_DIR" "$TOKEN_NAME" <<'PY'
import json, secrets, sys
from pathlib import Path
home, name = Path(sys.argv[1]), sys.argv[2]
tok = "vdlt_" + secrets.token_urlsafe(24)
data = {"tokens": {tok: {"name": name, "enabled": True, "created": ""}}}
p = home / "tokens.json"
p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
p.chmod(0o600)
print("   已签发令牌:", tok)
PY
else
  echo "   tokens.json 已存在，跳过（如需签发新令牌请手动追加）"
fi

echo "▶ 安装 systemd 单元"
cp "$SRC_DIR/vdl-gateway.service" /etc/systemd/system/vdl-gateway.service
systemctl daemon-reload
systemctl enable vdl-gateway >/dev/null 2>&1 || true
systemctl restart vdl-gateway
sleep 2
systemctl is-active --quiet vdl-gateway && echo "   vdl-gateway: active" || { echo "❌ 网关未启动"; systemctl status vdl-gateway --no-pager | tail -10; exit 1; }

echo "▶ 自检"
curl -s -m 5 http://127.0.0.1:8890/gw/health | head -c 300
echo
echo "✅ 网关部署完成。下一步：配置 nginx 在 8888 上把 /gw/ 转发到 8890。"
