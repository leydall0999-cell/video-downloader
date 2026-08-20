#!/usr/bin/env bash
# 从 VPS 自动取 API token，端到端测试 iqiyi Playwright worker。
# 用法: bash test_iqiyi.sh [user@host] [token_name]
#   默认 root@8.138.223.3 / VDL_COOKIE_API_TOKEN
#   token 必须与 Railway VDL_COOKIE_REFILL_TOKEN 一致
set -euo pipefail
VPS="${1:-root@8.138.223.3}"
TOK_NAME="${2:-VDL_COOKIE_API_TOKEN}"
DIR="/opt/vdl-worker"

TOKEN=$(ssh "$VPS" "grep ^${TOK_NAME}= $DIR/.cookie_sync.env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\n'")
if [ -z "$TOKEN" ]; then
  echo "❌ 没拿到 token。请确认 $VPS 上的 $DIR/.cookie_sync.env 存在且含 ${TOK_NAME}=..."
  exit 1
fi
echo "✅ 已取到 token（长度 ${#TOKEN}，前 6 位: ${TOKEN:0:6}...）"

# 默认测当前用户问题里的分享链接（base64 shareId 已 URL encode）
SHARE_URL="https://www.iqiyi.com/playShare.html?shareId=NTA0MTMxMTU0Mzg5MDcwMA%3D%3D"
ENC_URL=$(python3 -c "import urllib.parse, sys; print(urllib.parse.quote(sys.argv[1], safe=''))" "$SHARE_URL")
ENC_TOKEN=$(python3 -c "import urllib.parse, sys; print(urllib.parse.quote(sys.argv[1], safe=''))" "$TOKEN")

echo ""
echo "==> 调用 /v1/resolve?platform=iqiyi（超时 90s：起 Chromium + 跳转 + API）"
ssh "$VPS" "curl -s --max-time 90 'http://127.0.0.1:18731/v1/resolve?platform=iqiyi&token=${ENC_TOKEN}&url=${ENC_URL}'"
echo ""