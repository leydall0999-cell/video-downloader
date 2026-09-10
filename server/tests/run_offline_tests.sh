#!/usr/bin/env bash
# 离线测试运行器：在沙盒内验证 VideoDownloader 后端，不依赖任何外部网络。
#
# 覆盖：
#   1. test_app_smoke.py              —— FastAPI TestClient 无头冒烟核心路由
#   2. test_static_undefined_names.py —— 静态守卫：全量扫描 server/ 的「未定义名」
#                                        （py_compile 查不到、只在运行时炸的漏 import）
#   3. test_downloader_url_parsing.py —— 下载器链接解析/归一化（B站短链、追踪参数）
#   3. test_matting_core.py           —— 抠图前处理/选区/边缘柔化 纯函数
#   4. test_dewatermark_core.py       —— 去水印选区归一化/mask 合成/瓦片羽化权重
#   5. test_convert_guards.py         —— 转换入口守卫（分片 id/排序、本地路径白名单）
#   6. test_commentary_routes.py      —— 解说路由层（试听路径守卫、sidecar 推导、kind 白名单）
#   7. test_codec_utils.py            —— LGPL 编码器选择（H.264/HEVC 降级链、GPL 红线）
#   8. test_convert_pipeline.py       —— 转码管线决策 + 参数构造 + 真实转码端到端
#   9. test_ffmpeg_tools.py           —— 媒体加工（裁剪注入防护、抽音频/封面/铃声/去水印）
#
# 注：7~9 含调用真实 ffmpeg 的端到端用例（合成素材，无需网络）；
#     若构建机没有 ffmpeg，这些用例会打印「⚠️ 跳过」而非失败。
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
run_one test_static_undefined_names.py
run_one test_membership.py
run_one test_member_quota_e2e.py
run_one test_quality_options.py
run_one test_downloader_url_parsing.py
run_one test_matting_core.py
run_one test_dewatermark_core.py
run_one test_convert_guards.py
run_one test_commentary_routes.py
run_one test_codec_utils.py
run_one test_convert_pipeline.py
run_one test_ffmpeg_tools.py

echo ""
echo "========================================="
echo "  通过: $PASS   失败: $FAIL"
echo "========================================="
if [ "$FAIL" -gt 0 ]; then
  echo "❌ 存在失败用例，构建不应发布"
  exit 1
fi
echo "✅ 全部离线测试通过"
