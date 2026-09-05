#!/usr/bin/env bash
# 离线测试运行器：在沙盒内验证 VideoDownloader 后端，不依赖任何外部网络。
#
# 覆盖：
#   1. test_app_smoke.py  —— FastAPI TestClient 无头冒烟核心路由
#
# 退出码非 0 表示有测试失败（可在 build_mac.sh 末尾调用以阻断坏构建）。
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVER="$REPO/server"

# 优先用项目 build venv（含 requests / fastapi / httpx），否则回退 managed python
PY=""
for cand in "$REPO/.build_venv/bin/python" "/Users/suixindelang/.workbuddy/binaries/python/versions/3.13.12/bin/python3"; do
  if [ -x "$cand" ]; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
  echo "❌ 找不到可用的 python（需 requests + fastapi + httpx）"
  exit 1
fi

echo "▶ 使用解释器: $PY"
echo "▶ 运行离线测试（无外部网络依赖）..."
cd "$SERVER" || exit 1

PASS=0
FAIL=0

run_one() {
  local f="$1"
  echo ""
  echo "=== $f ==="
  if "$PY" "tests/$f" 2>&1; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1))
  fi
}

run_one test_app_smoke.py
run_one test_membership.py
run_one test_member_quota_e2e.py

echo ""
echo "========================================="
echo "  通过: $PASS   失败: $FAIL"
echo "========================================="
if [ "$FAIL" -gt 0 ]; then
  echo "❌ 存在失败用例，构建不应发布"
  exit 1
fi
echo "✅ 全部离线测试通过"
