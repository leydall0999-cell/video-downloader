#!/usr/bin/env bash
# 离线测试运行器：在沙盒内验证 VideoDownloader 后端，不依赖任何外部网络。
#
# 覆盖：
#   1. test_app_smoke.py         —— FastAPI TestClient 无头冒烟装配
#   3. test_atomic_writes_and_auth.py —— 原子写契约 / 并发锁 / 验证码仅本机回传 回归
#   4. test_worker_proxy_direct.py —— 解析通道直连 daemon 的路由回归
#   5. test_cloud_link.py        —— web 版账号接授权中心（打通 web 与 App 用户数据）
#   6. test_media_proxy_range.py —— 下载用媒体中继 /api/media/proxy 的 Range/防盗链/SSRF 契约
#   7. test_direct_url_hls.py    —— m3u8 不得被当作「可直接下载的文件」透传（否则存下播放列表文本）
#   8. test_web_direct_download.js —— 网页版直链分片下载引擎（前端真源码 + 假源站，需 node）
#   9. test_web_hls_assemble.js  —— 浏览器内 HLS 合成（m3u8 解析/变体选择/初始化段/加密与直播拒绝，需 node）
#   10. test_web_route_retry.js  —— 远端失败自动换路由重试（失败分类/原地重试/换路由/耗尽集合防抖/切换上限，需 node）
#   11. test_youtube_shortlink_scope.py —— youtu.be / shorts / m. 短链必须归一化为 www 长链
#       （yt-dlp 的 http_headers Cookie 按域作用域，短链会把有效 Cookie 挡在 innertube 之外）
#   12. test_upload_body_limit.py —— 网关体积上限守卫：/api/upload-chunk 必须放得下 64MB 分片，
#       前端上传端点只允许同源（VPS 那份 nginx 曾只有 8m，把网页版全部上传 413 打死）
#
# 退出码非 0 表示有测试失败（可在 build_mac.sh 末尾调用以阻断坏构建）。
set -u

# 🔴 离线测验必须与云端解耦：默认关掉账号上云（test_cloud_link.py 自己会重新打开，
#    并把 license_client 全量替换为内存假实现）。否则测试会真的去打 8902。
export VDL_CLOUD_LINK=0

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

# --- Node 解释器（前端下载引擎测试用）---
# 找不到就只跳过那一条，不让「本机没装 node」把整份套件染红。
NODE=""
for cand in /Users/suixindelang/.workbuddy/binaries/node/versions/*/bin/node \
            "$(command -v node 2>/dev/null)"; do
  if [ -n "$cand" ] && [ -x "$cand" ]; then NODE="$cand"; break; fi
done

run_node() {
  local f="$1"
  echo ""
  echo "=== $f (node) ==="
  if [ -z "$NODE" ]; then
    echo "⏭ 跳过：未找到 node 解释器（前端下载引擎测试需要 node）"
    return 0
  fi
  if "$NODE" "tests/$f" 2>&1; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1))
  fi
}

run_one test_app_smoke.py
run_one test_index_cache_invalidation.py   # 首页内存缓存失效判据：只改 index.html 也必须重读（2026-09-30 线上吐旧页事故）
run_one test_atomic_writes_and_auth.py
run_one test_worker_proxy_direct.py
run_one test_peer_overseas_fallback.py
run_one test_member_download_quota.py
run_one test_youtube_unavailable_vs_bot.py
run_one test_youtube_shortlink_scope.py
run_one test_yt_warp_proxy.py              # YouTube 专属出口代理：VDL_PROXY_YT(WARP) 分流、googlevideo 同代理、国内站不受影响（2026-10-01）
run_one test_yt_pot_starved.py             # YouTube SABR「PO Token 饥饿」检测 + web_safari/HLS 补救 + 雪碧图假档排除（2026-10-01）
run_one test_upload_body_limit.py
run_one test_reconvert_source_reuse.py      # 免重传重转：finish 留源 2h / reconvert 复用 / 设备隔离 / 410 回退 / TTL 清理（2026-09-29）
run_one test_feature_auth_gate.py           # 功能级登录门禁（服务端）：18 个执行类端点未登录 403+引导登录、带 token 放行（2026-09-29）
run_one test_cloud_quota.py                # 云端算力账号级配额：免费 3 次/日（转码/拼接/去水印/字幕），gate/count/relay/fail-open（2026-09-29）
run_one test_cloud_link.py
run_one test_media_proxy_range.py
run_one test_direct_url_hls.py
run_node test_web_direct_download.js
run_node test_web_hls_assemble.js
run_node test_web_route_retry.js
run_node test_convert_poll_lifecycle.mjs   # 音乐/图片转换轮询定时器自杀停表回归（2026-09-29 线上卡 30% 事故）
run_node test_dw_preview_zoom.mjs          # 去水印捏合缩放(只放大图片)+结果预览灯箱+重新加工（2026-09-29 用户反馈）
run_node test_view_top_gap.mjs             # 顶层视图卡片与吸顶顶栏间距：main > .panel 3rem，防「顶栏+卡片」粘连（2026-10-04 用户反馈）
run_node test_mobile_header_tabs.mjs       # 手机网页版顶栏功能导航：≤760px 换行让 tabs 不被挤成 0 宽而整排消失（2026-10-07 用户反馈）
run_node test_activate_gate.mjs            # 激活码通道收口：/api/member/activate 仅超管 + 前端入口已移除（2026-10-04 白嫖后门）
run_node test_feature_usage_gate.mjs        # 「今日使用」功能配额表逐行须有真实 use_daily 拦截点（2026-10-04 清占位行）
run_node test_deploy_surface_parity.mjs     # cn/hk 双部署面：activate 超管门禁必须两端同步（2026-10-04 hk 实测被白嫖）
run_node test_benefit_copy_hide.mjs          # 会员页权益文案不得含「云端/算力/AI/本地」字眼，但配额与限流原样（2026-10-04 用户定档）
run_node test_ai_features_honest.mjs         # AI 会员权益：每条须有实现落点 + 前端必须真的渲染（2026-10-04 用户反馈「AI会员补充权益」）
run_node test_concat_flow.mjs              # 视频拼接面板状态机：防重复结果/停表/清源片段/输出分区（2026-09-29 用户反馈）
run_node test_app_intro_entries.mjs        # 网页版「更多功能」页：24 项能力/5 组/标签与入口一致（2026-09-30）
run_node test_platform_count_label.mjs     # 平台数对外口径：两端徽章走 fmtPlatformCount，116→100+（2026-09-30）
run_node test_member_page.mjs              # 网页版会员购买页：免登录可见价目/三轨/登录后自动续单/弹窗在顶层（2026-09-30）
run_node test_profile_login_single.mjs     # 个人中心未登录：页内登录表单优先，禁止与全局登录弹窗同屏（2026-10-08 用户反馈）
run_node test_auth_modal_width.mjs         # 登录弹窗定宽 400 无右侧留白 + 登录方式文案「电话号码/QQ邮箱」（2026-10-08 用户反馈）
run_node test_notice_modal.mjs             # 下单失败/活动限购改用弹窗：禁回退成「只写页面底部状态行」（2026-10-09 用户反馈）
run_node test_cookie_contribute_hidden.mjs # 「贡献 Cookie 到公共池」勾选框隐藏：只 display:none 不删元素/仍默认勾选/不留悬空指引（2026-10-09 用户反馈）
run_node test_placeholder_contrast.mjs     # 输入框 placeholder 灰度统一：全局兜底须与 #urlInput 基准 #a2acbd 同值，防个别框吃浏览器默认深灰 #757575（2026-10-09 用户反馈）
run_one test_member_plans_source.py        # 1/3/7 天档 + 套餐价格单一真源：超管改价/改天数在展示/下单/发放三处同步（2026-09-30）
run_one test_pay_gateway_web.py            # 网页版支付真通道：后端带云端令牌转发 VPS 支付服务 / 无令牌必拒 / PAID 拉权益 / 离线回落 mock / 前端按通道切文案（2026-10-09）
run_one test_purchase_history_sync.py       # 购买记录落户：apply_cloud_authoritative 幂等并入云端 purchases（按 id 去重） + 显示端 1/3/7 天档 + 到期精确到分 + 来源映射（cloud/ui_test/admin_direct）（2026-10-09）
run_one test_flash_phase.py                # 秒杀窗口三态：窗口过了必须自动收起「限时秒杀」角标与「秒杀至…」（2026-10-03）
run_one test_share_token.py                # 网页版分享凭据端点：登录门禁/SHARE_UNAVAILABLE/每日限次/计数/挂载（2026-09-30 受限版分享）
run_one test_web_stats.py                  # 网页访客统计：PV/UV 去重、不落 IP 明文、跨天哈希不可关联、事件白名单、来源归一、90 天裁剪（2026-10-01）
#  任务落盘持久化（2026-10-02 B2 移植）：重启后未完成任务恢复为可续传、completed 只在成品文件仍在时恢复。
run_one test_task_persistence.py
#  下载预检与 PO Token 会话保持（2026-10-02 B1/A1 移植）：体积估算/上限 20GB/磁盘预检、
#  YouTube 403 降级链必须带回 visitor_data（丢会话=照样 403）。
run_one test_download_preflight.py
run_node test_share_web.mjs                # 网页版「生成二维码/生成网页」：登录门禁/凭据三头/计数/受限上限/合成器红线/本地二维码（2026-09-30）
#  下载失败矩阵重试与 aria2c 默认化（2026-10-02 A2/B4 移植）：可重试失败换策略重试
#  （切换下载器→切 HLS 链路，最多额外 2 次）、auto 默认启用 aria2c、硬上限到点仍在推进则延长等待（B3）。
run_one test_download_matrix.py
#  可行动化报错 / 完成任务历史（2026-10-02 A3/B5 移植）：停滞与硬超时不再伪装成
#  「用户取消」；到期完成任务降级为历史条目（保留 50 条元数据、可重新下载）。
#  （A4 的 proxy.json 分流：web 端验证见 test_yt_warp_proxy.py 的 env 分支。）
run_one test_batch3_history.py
run_one test_ai_free_trial_web.py
run_one e2e_ai_trial_check.py

#  test_cloud_quota_unified.py —— 两端云端额度口径统一（2026-06-06 用户定档
#    「两端统一成终身 3 次」）：此前桌面记 quota.json 的终身 3 次、网页记
#    use_daily("cloud") 的每日 3 次/日 ⇒ 换端就白拿。现网页版也走授权中心原子
#    扣减（App / 网页共享一份）。守卫钉住：中心扣减是主路径、本地每日配额仅作
#    fail-open 兜底（AST 判「不许出现在 try 体的无条件单条语句里」）、上限读
#    quota.py 常量、取 token 走 store.cloud_session()、会员判定用网页版**实际
#    存在**的接口（它没有桌面端的 is_download_active / _cloud_token）。
run_one test_cloud_quota_unified.py

echo ""
echo "========================================="
echo "  通过: $PASS   失败: $FAIL"
echo "========================================="
if [ "$FAIL" -gt 0 ]; then
  echo "❌ 存在失败用例，构建不应发布"
  exit 1
fi
echo "✅ 全部离线测试通过"
