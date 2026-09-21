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
#   3b. test_matting_forward.py       —— 抠图 AI 推理前向链路（假 session 注入，不下载权重）
#   4b. test_dewatermark_forward.py   —— 去水印 AI 推理前向链路（假 session 注入，不下载权重）
#   5. test_convert_guards.py         —— 转换入口守卫（分片 id/排序、本地路径白名单）
#   6. test_commentary_routes.py      —— 解说路由层（试听路径守卫、sidecar 推导、kind 白名单、
#                                         解说词字数/时长预检（防原速渲染静默截断旁白））
#   7. test_codec_utils.py            —— LGPL 编码器选择（H.264/HEVC 降级链、GPL 红线）
#   8. test_convert_pipeline.py       —— 转码管线决策 + 参数构造 + 真实转码端到端
#   9. test_ffmpeg_tools.py           —— 媒体加工（裁剪注入防护、抽音频/封面/铃声/去水印）
#  10. test_selfupdate.py             —— 自更新链路（全量 sha/校验闸门、增量版本门槛、
#                                        套用后的权限保持、发布脚本排序守卫）
#  11. test_sr.py                    —— 高清修复（档位/倍率校验、AI 输入上限、
#                                        轮询端点免限流、CoreML 不得指定计算单元）
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
run_one test_matting_member.py
run_one test_subtitle_quota.py
run_one test_quality_options.py
run_one test_downloader_url_parsing.py
run_one test_matting_core.py
run_one test_dewatermark_core.py
run_one test_matting_forward.py
run_one test_dewatermark_forward.py
run_one test_dewatermark_diffusion.py
#   4c. test_dewatermark_routes.py     —— 去水印路由层（扩散档）：capability 暴露 / diffusion 503 / engine·model 白名单 400
run_one test_dewatermark_routes.py
#   3c. test_matting_engines_forward.py —— 其余本地 ONNX 引擎前向（modnet/isnet/birefnet-matting/portrait）
#   3d. test_matting_chroma_key.py      —— chroma-key 纯算法前向（背景透明/主体不透明/选区硬边界）
#   3e. test_matting_sam_forward.py     —— sam-matting 前向（假 _sam_session 注入 enc/dec）
run_one test_matting_engines_forward.py
run_one test_matting_chroma_key.py
run_one test_matting_sam_forward.py
run_one test_convert_guards.py
run_one test_commentary_routes.py
#   6a. test_commentary_range_trim.py —— 解说「正剧范围 → 实际输入」裁剪判据（2026-09-20 用户实测）：
#                                        「正剧开始」留空 + 「片尾开始」15 分钟 → 成片仍是 46 分钟整片
#                                        （ts=0 被 `0.0 < ts < te` 静默漏掉）。含 fold 与裁剪判据必须一致
#                                        的不变量 + 「调用方不许再抄一份判据」的静态守卫
run_one test_commentary_range_trim.py
#   6b. test_clone_env.py             —— 本地语音克隆「按需安装」环境判定（候选优先级 / 半成品驳回 /
#                                        磁盘不足拦截 / 非 Apple Silicon 拒绝 / 全程不得改进程级 HF_ENDPOINT）
run_one test_clone_env.py
run_one test_codec_utils.py
run_one test_convert_pipeline.py
run_one test_ffmpeg_tools.py
run_one test_compress.py
run_one test_desktop_bridge.py
run_one test_download_wiring.py
run_one test_sr.py
run_one test_selfupdate.py
run_one test_llm_local_priority.py
run_one test_quota.py
run_one test_llm_config_save.py
run_one test_vision_managed_config.py
run_one test_gateway_config.py
run_one test_quota_refund.py
run_one test_commentary_output_naming.py
run_one test_commentary_precheck.py
run_one test_subtitle_band.py
run_one test_llm_truncation_retry.py
#  12. test_engine_isolation.py      —— 跨功能「共享工具」隔离性：抠图/去水印/扩散 各自独立
#                                       session 缓存与模型全局；VDL_MODELS_DIR 下模型目录必须一致
#                                       （同模型不下载两份）；字幕不得在请求期污染进程级 HF 端点
run_one test_engine_isolation.py
#  13. test_users_store_concurrency.py —— 账号表并发写安全：所有写入口必须整段持锁
#                                        （注册/改密/注销/提权/头像/后台禁用/后台重置/超管引导），
#                                        并发注册不丢号、同名只成功一次、跨进程锁生效、
#                                        写盘用唯一临时名（固定名会写出半截 JSON）
run_one test_users_store_concurrency.py
#  14. test_config_atomic_write.py  —— 配置/状态文件的「原子落盘 + 读改写串行」：
#                                       固定名临时文件并发下会互相截断（有可复现反证）、
#                                       quota.json 跨进程读写会「额度复原」；
#                                       含全仓 AST 棘轮（不得再出现固定名 .tmp）
run_one test_config_atomic_write.py
#  15. test_auth_reset_code_leak.py  —— 🔴 账号接管防护：dev 模式不得把验证码回传给公网调用方
#                                        （缺 smtp.json 时生产会自动落到 dev，原实现＝公网可改任意账号密码）；
#                                        dev+本机仍回传（桌面调试不退化）、dev+公网为 None、
#                                        拿不到码即改不动密码、未知账号不回传、smtp 不回传
run_one test_auth_reset_code_leak.py
#  16. test_voice_sample_record.py —— 「我的音色 · 直接录制」落盘守卫：
#                                      WAV 头时长解析、空/过大/格式/太短/过长 五类拒绝、
#                                      非 WAV 回落前端报的时长、文件 0600 目录 0700、
#                                      只保留最近 5 段且不碰配置目录里的其它文件
run_one test_voice_sample_record.py
#  17. test_commentary_style_intensity.py —— 解说「风格强度」链路（2026-09-21 新增）：
#                                        CommentaryRequest.style_intensity 字段必须存在（漏加则
#                                        router 的 payload.style_intensity 在 JSON 主路径 AttributeError，
#                                        pyflakes 查不到这类字段缺失）；CLI 按强度产出
#                                        `--style <s> --style-intensity <n>`；★AST 棘轮：
#                                        routers 里每处 payload.X 都必须是该函数绑定模型里的真实字段
run_one test_commentary_style_intensity.py
#  18. test_share_history.py        —— 扫码分享「我的分享 / 删除 / 有效期」（2026-09-21 新增）：
#                                        历史落盘（去重/新在前/上限裁剪/坏文件容错）、
#                                        删除端点**只允许删本机历史里的 sid**（否则成任意删除代理）、
#                                        节点删失败必须保留记录、通道根推导（direct 只能上传，
#                                        删除/探活须用源站根）、上传须透传 X-Expire 且成功后落历史
run_one test_share_history.py

echo ""
echo "========================================="
echo "  通过: $PASS   失败: $FAIL"
echo "========================================="
if [ "$FAIL" -gt 0 ]; then
  echo "❌ 存在失败用例，构建不应发布"
  exit 1
fi
echo "✅ 全部离线测试通过"
