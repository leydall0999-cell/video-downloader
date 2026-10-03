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

# 登录门禁（2026-09-26）：生产默认开启——能力型 POST 端点（解析/下载/转换/抠图/去水印/字幕/
# 解说/订阅等）未登录一律 401 + NO_AUTH，见 server/app.py 的 _login_gate。
# 本运行器里这批脚本测试的是「功能内部逻辑」，都直接打端点且不模拟登录态，
# 因此整体关掉门禁（保持它们原本的语义）。门禁自身的拦截/放行由
# tests/test_login_gate.py 单独打开开关验证。
export VDL_LOGIN_GATE=0

# ⚠️ 数据目录隔离（2026-09-30 事故后必设）：
# credential_store 的 Keychain 是**系统级**的，只能靠 VDL_DATA_DIR 这个开关来
# 判断「当前是不是测试环境」并切到 .test 命名空间。本运行器此前只设了
# VDL_LOGIN_GATE，没有 VDL_DATA_DIR → 跑全量套件时测试桩 token（"tok-1"）
# 把真实账号 15014313254 的 Keychain 条目覆盖了，线上登录态直接 BAD_TOKEN。
# 所以这一行不是可选的：没有它，跑一次全量测试就会弄坏真实登录凭据。
export VDL_DATA_DIR="${VDL_DATA_DIR:-/tmp/vdl_offline_data}"
mkdir -p "$VDL_DATA_DIR" 2>/dev/null

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
run_one test_subtitle_preview.py
run_one test_subtitle_audio.py
run_one test_subtitle_music_fallback.py
run_one test_subtitle_lyrics.py
run_one test_quality_options.py
run_one test_quality_member_gate.py          # 清晰度会员门槛：免费档 2K/4K → 402 引导开会员（2026-10-02 用户「免费用户 1080 以上要弹会员」）
run_one test_quality_member_ui_wiring.py     # 清晰度门槛三端接线：后端两入口都挂门 + 前端弹会员中心（不弹＝用户不知道怎么解锁）
run_one test_yt_pot_starved.py
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
run_one test_desktop_copy_wiring.py            # 桌面壳「复制链接」：原生剪贴板桥优先 + 失败不静默（2026-10-01）
run_one test_clip_autodetect_wiring.py         # 剪贴板自动识别一键下载：原生桥读取 + 提示条接线（2026-10-02 用户「复制链接后粘贴下载」）
run_one test_sniffer_ext_ping.py            # 嗅探「扩展心跳」三态：未连/在线/过期离线（2026-10-01 面板简化配套）
run_one test_sniffer_ext_push.py            # 嗅探「扩展自动直推」：入库/去重/kind_hint/脏数据（2026-10-01 自动嗅探配套）
run_one test_ext_version_cmp.py             # 扩展版本比对守卫：只在「内置比已装新」时提示升级（2026-10-01 防降级）
run_one test_sniffer_quality_wiring.py      # 嗅探链路清晰度透传：后端白名单 + 桌面端用它 + 两端都有下拉（2026-10-01 用户「没法选择分辨率」）
run_one test_sniffer_badge_hide_wiring.py   # 嗅探入口徽标按状态隐藏：扩展在线才收起、离线自动回来（2026-10-02 用户「这个可以隐藏了」）
run_one test_extension_autoupdate_wiring.py # 扩展零点击自动更新：目录识别/安全同步（拒写他人目录·只增不删）+ 心跳回传 reload_to + 扩展自重载（2026-10-02 用户「扩展程序更新怎么办」）
run_one test_download_wiring.py
#  11b. test_login_gate_placement.py —— 登录门禁「挂载位置」守卫（2026-09-27 用户实测：
#                                       点「解析链接」就弹登录框 = 门禁挂错层）。
#                                       锁住「前置步骤（解析/选文件）不弹，执行动作才弹」，
#                                       并要求门禁表 id 在 index.html 真实存在、后端名单同步、
#                                       提示文案不被 openAuthModal 清空
run_one test_login_gate_placement.py
#  11c. test_page_link_wiring.py —— 「生成网页 → 在线链接」接线守卫（2026-09-30 用户实测：
#                                       「为什么别人打不开」＝ 结果区只给本机路径，用户把它当分享链接发出去）。
#                                       锁住两端：分享节点必须把 .html 分享渲染成页面原件；
#                                       前端必须给在线链接 + 二维码，且上传请求带登录 token。
run_one test_page_link_wiring.py
#  11d. test_home_entries.py —— 首页「热门功能快捷入口」接线守卫（2026-09-30「快捷入口继续完善」）：
#                              首页 12 张卡片改 5 组 17 张，补齐生成二维码/生成网页/音乐转换/
#                              图片转换/视频音频桥接。守卫钉住「卡片 data-view 必须是侧栏真实视图名」
#                              （拼错=点了没反应）与「侧栏主功能必须都在首页有入口」（防加了功能忘了首页）。
run_one test_home_entries.py
run_one test_sr.py
run_one test_selfupdate.py
run_one test_llm_local_priority.py
run_one test_quota.py
#  任务落盘持久化（2026-10-02 B2）：重启后未完成任务恢复为可续传、completed 只在
#  成品文件仍在时恢复、remove 同步删状态。此前任务表纯内存，重启全丢。
run_one test_task_persistence.py
#  下载预检与 PO Token 会话保持（2026-10-02 B1/A1）：体积估算/上限 20GB/磁盘预检、
#  YouTube 403 降级链必须带回 visitor_data（丢会话=照样 403）。
run_one test_download_preflight.py
#  下载失败矩阵重试与 aria2c 默认化（2026-10-02 A2/B4）：可重试失败换策略重试
#  （切换下载器→切 HLS 链路，最多额外 2 次）、auto 默认启用 aria2c、原生路径
#  http_chunk_size 分块并行、硬上限到点仍在推进则延长等待（B3）。
run_one test_download_matrix.py
#  可行动化报错 / YouTube 代理分流 / 完成任务历史（2026-10-02 A3/A4/B5）：
#  停滞与硬超时不再伪装成「用户取消」；VDL_PROXY_YT/proxy.json 只分流 YouTube 域；
#  到期完成任务降级为历史条目（保留 50 条元数据、可重新下载）。
run_one test_batch3_history.py
run_one test_proxy_settings.py
#  1/3/7 天下载会员档 + 套餐价格单一真源（2026-09-30）：超管在后台改价/改天数后，
#  展示（/api/member/plans 真身 store.plans()）/ 下单金额 / 发放天数三处必须同步。
#  此前下单读硬编码 PAY_PLANS、发货读硬编码 DOWNLOAD_PLANS —— 改了等于没改。
run_one test_member_plans_source.py
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
#  19. test_login_error_message.py  —— 登录提示不得张冠李戴 + 手机号账号的云端建号（2026-09-22 新增）：
#                                      本机账号表决定「账号是否存在」（本机区分 BAD_PASSWORD /
#                                      NO_ACCOUNT，公网仍模糊防枚举）、手机号老账号自愈只在
#                                      本机密码正确时补建、云端的账号主键口径与 App 一致
#                                      ⚠️ 之前漏挂在清单里 —— 加了新测试文件必须在这里登记，
#                                     否则构建门禁根本不跑它（只写文件＝没门禁）
run_one test_login_error_message.py
#  20. test_cloud_account.py        —— 账号制客户端（2026-09-22）：
#                                      同一笔云端购买不得重复落户（幂等）、登出/被挤掉不清已购权益
run_one test_cloud_account.py
#  21. test_password_sync.py        —— 🔴 两端账号库的密码一致性（2026-09-22 新增）：
#                                      账号其实是两套库（本机 auth_store 管登录与门禁 / 云端授权中心
#                                      管会员与设备位），各存一份密码哈希 —— 任何一侧单独改密都会
#                                      分叉成「这台能登、换台说密码错」。锁死三条收敛路径：
#                                      ①改密/重置后推云端（fail-open，云端失败不回滚本机）
#                                      ②云端本无此账号不算失败 ③下次登录时自愈（须本机密码对
#                                      **且**持该账号的云端 token，防越权改他人密码）
run_one test_password_sync.py
#  22. test_system_changelog.py     —— 「关于本应用」的内置更新日志（2026-09-26 新增）：
#                                      不再依赖线上更新源（线上长期停在旧版会让面板只显示
#                                      旧版说明）：结构与版本降序、仓库 VERSION 必须在日志里
#                                      命中（漏写则退回兜底文案）、未命中时优雅降级、
#                                      端点免登录可用（未登录也要能看更新内容）
run_one test_system_changelog.py
#  23. test_youtube_player_client.py —— YouTube player_client 选择（2026-09-26 写成，2026-09-27 补登记）：
#                                      不得再给 YouTube 强制 player_client（SABR 后全部失效）、
#                                      下载兜底链首位必须是 "(default)"、
#                                      带 Cookie 失败要能剥 Cookie 裸重试
#                                      ⚠️ 之前漏挂在清单里 —— 只写文件＝没门禁
run_one test_youtube_player_client.py
#  24. test_youtube_cookie_sources.py —— YouTube Cookie 源顺序 + 登录态体检（2026-09-27 新增，
#                                       对标竞品 DataTool 的会话自持机制）：
#                                      ①登录态判据只认 SID/__Secure-*PSID/LOGIN_INFO；
#                                      ②自动源缺登录态字段即跳过、显式源(user/env)永不跳过；
#                                      ③候选顺序必须是 user > env > **browser** > cache > pool
#                                       （实时解密浏览器必须早于 30 天 TTL 的缓存：Google 的
#                                       SIDCC/__Secure-*PSIDTS 是滚动的，快照落后即被判未登录，
#                                       而「有缓存」会永远挡在新鲜值前面 → 死循环）
run_one test_youtube_cookie_sources.py
#  25. test_youtube_js_challenge.py —— YouTube JS 挑战（nsig）求解链 + 打包依赖（2026-09-27 新增，
#                                     对标竞品 DataTool 的第二半机制：自带 deno + yt-dlp-ejs）：
#                                     ①_find_js_runtime_binary 按 VDL_JS_RUNTIME > _MEIPASS/bin >
#                                       venv/bin > sysconfig scripts > PATH 顺序探测 deno/node/bun/quickjs；
#                                     ②_js_challenge_options 只下发 yt_dlp.globals 真正支持的 runtime
#                                       （不支持的绝不硬塞，否则 options 校验直接抛错）；
#                                     ③_base_options 只给 YouTube 挂 js_runtimes/remote_components，
#                                       其它站点（B 站等）行为不得被改变；
#                                     ④requirements 必须含 yt-dlp-ejs + deno，两个 build 脚本必须把
#                                       deno 显式 --add-binary 进包（venv 脚本目录不会自动分发）
run_one test_youtube_js_challenge.py

#  26. test_direct_url_hls.py —— m3u8 不得被当作「可直接下载的文件」（2026-09-27 修复）：
#                                 `.m3u8` 曾在 _DIRECT_EXT_RE 白名单里，导致 `probe()` 在
#                                 `_looks_like_direct_file()` 处短路跳过 yt-dlp，`direct_url`
#                                 直接等于那个清单地址 → 前端把播放列表当文件存下来，
#                                 用户拿到几百字节的废文本。现在清单一律走 HLS 合成路径；
#                                 单个 `.ts` 仍是可直取的完整 MPEG-TS，不得误伤。
run_one test_direct_url_hls.py

#  27. test_sniffer_extension_ingest.py —— MV3 扩展回传链路（2026-09-27，★12）：
#      /api/sniffer/send 接受 cookie/source（extension 标记）、picked 出队含全字段、
#      items 展示区不受污染、cookie 截断 8192（对齐 DownloadRequest.max_length）、
#      缺 url 400、缺省回落 manual（悬浮球兜底语义不回归）
run_one test_sniffer_extension_ingest.py

#  28. test_upload_endpoint_same_origin.py —— 上传端点必须同源（2026-09-29，★13）：
#      UC_UPLOAD_ENDPOINTS 只许 location.origin。曾有的第二项（已停服的 Railway 云上
#      备份）会让每个奇数下标分片先撞死主机；非同源端点还会把分片劈到两份存储上，
#      finish 必报「分片不完整」。同时钉住「非 JSON 的 413 = 网关拦截」的区分文案。
run_one test_upload_endpoint_same_origin.py

#  29. test_payment_core.py —— 支付内核（2026-09-30）：套餐价表 / 下单返回 PENDING+二维码 /
#      未付款查询仍 PENDING 且不发放权益 / simulate_paid 翻转 PAID 且 grant 回调收到正确
#      plan_code / 未知套餐拒绝 / 真实通道未接入时 NotImplementedError / 订单可持久化复查。
run_one test_payment_core.py

#  30. test_payment_router.py —— 支付 REST 层（2026-09-30，★仓库健康）：app.py 无条件
#      include routers/payment.py，故该文件与四个 /api/cloud/pay/* 路由必须在位，
#      否则全新 clone 直接 ImportError 起不来。同时钉死订单目录走 _base_dir()
#      （~/.video-downloader + VDL_DATA_DIR 隔离），不得回落 ~/.videodownloader
#      （无短横线，是 cookie/cloud_sync 的历史目录，写错不报错但会污染家目录）。
run_one test_payment_router.py

#  31. test_credential_store.py —— 账号 token 凭据存储（2026-09-30，P2 加固）：
#      token 不再以明文落在 ~/.video-downloader/membership*.json（拷走文件即可
#      冒充用户调云端）。钉死三点：Keychain 存取往返、磁盘 JSON 无明文但内存态
#      仍有 token（否则 6 处 acc.get("token") 会集体掉登录）、重新加载能注回。
#      测试用一次性 Keychain 账号并在结尾清理；数据目录走 VDL_DATA_DIR 隔离。
run_one test_credential_store.py

#  32. test_engine_idle.py —— 引擎空闲自动卸载（2026-09-30，方案 A）：
#      空闲超时默认 180s 释放模型权重；开关关闭时不释放；释放后能重建；
#      配置走 VDL_DATA_DIR 隔离（绝不写用户家目录）。
run_one test_engine_idle.py

#  33. test_support_image.py —— 客服「发图片 + 搜历史消息」（2026-10-03）：
#      图片解码白名单/大小上限/魔数校验、附件路径防穿越、历史消息搜索
#      （超管搜全部 / 用户只搜自己的、按时间倒序、带定位下标）。
#      附件必须落 VDL_DATA_DIR（绝不写 Downloads —— TCC 会重新索要授权并永久阻塞）。
run_one test_support_image.py

echo ""
echo "=== extension/tests/test_sniff_core.js（MV3 扩展判定核心，node） ==="
NODE_BIN=""
for cand in /Users/suixindelang/.workbuddy/binaries/node/versions/22.22.2-3/bin/node "$(command -v node 2>/dev/null)"; do
  if [ -n "$cand" ] && [ -x "$cand" ]; then NODE_BIN="$cand"; break; fi
done
if [ -n "$NODE_BIN" ]; then
  if "$NODE_BIN" "$REPO/extension/tests/test_sniff_core.js"; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1))
  fi
else
  echo "⚠️ 跳过（无 node）"
fi

echo ""
echo "=== extension/tests/test_background_page_scope.js（扩展「只保存当前页」，node + chrome 桩） ==="
if [ -n "$NODE_BIN" ]; then
  if "$NODE_BIN" "$REPO/extension/tests/test_background_page_scope.js"; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1))
  fi
else
  echo "⚠️ 跳过（无 node）"
fi

echo ""
echo "=== extension/tests/test_pagewatch.js（页面侧哨兵：SPA 换页不丢事件，node + chrome 桩） ==="
# 2026-10-01 补登记：该文件随扩展 1.0.40 一起写就，但当时漏挂进运行器（只写文件＝没门禁）。
if [ -n "$NODE_BIN" ]; then
  if "$NODE_BIN" "$REPO/extension/tests/test_pagewatch.js"; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1))
  fi
else
  echo "⚠️ 跳过（无 node）"
fi

echo ""
echo "=== extension/tests/test_popup_video_page.js（视频页空状态必须有「解析并下载/复制链接」，node） ==="
# 2026-10-01 用户反馈「这里也要可以操作」：钉住 popup 空状态的两个操作按钮 + 复制失败不静默。
if [ -n "$NODE_BIN" ]; then
  if "$NODE_BIN" "$REPO/extension/tests/test_popup_video_page.js"; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1))
  fi
else
  echo "⚠️ 跳过（无 node）"
fi

echo ""
echo "=== extension/tests/test_ext_autoreload.js（零点击自动更新：只在真更新时自重载，防死循环/防降级，node + chrome 桩） ==="
# 2026-10-02 用户问「扩展程序更新怎么办」→ 心跳回传 reload_to + 扩展 chrome.runtime.reload()。
# 三个致命分支都在「不该动」的一侧（同版本/旧版本/同目标重复），故用行为测试而非源码断言。
if [ -n "$NODE_BIN" ]; then
  if "$NODE_BIN" "$REPO/extension/tests/test_ext_autoreload.js"; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1))
  fi
else
  echo "⚠️ 跳过（无 node）"
fi

echo ""
echo "=== extension/tests/test_popup_quality_state.js（清晰度「回显 = 实际发送」，node + DOM/chrome 桩） ==="
# 2026-10-02 用户截图：下拉选的 2K 1440P，回显却写「最佳画质（自动）」，实际也按 best 下载。
# 根因是 render(st) 的 `state = st` 抹掉了 quality（快照里没有该字段）。要害全在「两次渲染
# 之间」，源码断言 grep 不出来，故写成行为测试：改选后必须三处一致，且再渲染一次不许丢。
if [ -n "$NODE_BIN" ]; then
  if "$NODE_BIN" "$REPO/extension/tests/test_popup_quality_state.js"; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1))
  fi
else
  echo "⚠️ 跳过（无 node）"
fi

echo ""
echo "=== extension/tests/test_page_title_lag.js（嗅探标题滞后：SPA 换页「地址先到、标题后到」必须补推，node + chrome 桩） ==="
# 2026-10-02 用户实测「app上嗅探到的比视频慢一步，最新嗅探到的是上一个视频，
# 想要看的当前视频的嗅探结果得打开另一个视频」。根因：SPA 换页那一刻 Chrome 只给得到
# changeInfo.url，tab.title 还是**上一页**的；只推那一拍 → 条目带旧标题，且同页 5 分钟
# 冷却把重推挡死。要害全在「两拍之间的状态」，源码 grep 不出来，故跑真实 background.js
# 做行为测试（服务端配套见 server/tests/test_sniffer_ext_push.py 的标题覆盖用例）。
if [ -n "$NODE_BIN" ]; then
  if "$NODE_BIN" "$REPO/extension/tests/test_page_title_lag.js"; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1))
  fi
else
  echo "⚠️ 跳过（无 node）"
fi

echo ""
echo "=== extension/tests/test_sniff_meta.js（嗅探条目元信息：标题/时长/大小，node + chrome 桩） ==="
# 2026-10-02 用户「这里把嗅探到的视频信息也加上，比如：标题、时长、大小」。
# 三样来源各不相同：标题=tab.title；时长=页面侧 <video>.duration（只有 pagewatch.js 读得到，
# SABR 站后台既抓不到媒体流也没 Content-Duration 头）；大小=响应头 Content-Length
# （sniff-core.pickHeaders 早已解析，但必须**传进 store.add**、再**带进推送载荷**才有用）。
# 要害在「时长是后到值」：refreshPageTitle 的去重口径必须连 duration 一起比，
# 否则「标题同、时长刚拿到」的补推会被当成重复丢掉 —— 列表里的时长永远空着。
if [ -n "$NODE_BIN" ]; then
  if "$NODE_BIN" "$REPO/extension/tests/test_sniff_meta.js"; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1))
  fi
else
  echo "⚠️ 跳过（无 node）"
fi

echo ""
echo "=== server/tests/test_platform_count_label.mjs（平台数对外口径：两端统一 116→100+） ==="
if [ -n "$NODE_BIN" ]; then
  if "$NODE_BIN" "$SERVER/tests/test_platform_count_label.mjs"; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1))
  fi
else
  echo "⚠️ 跳过（无 node）"
fi

echo ""
echo "========================================="
echo "  通过: $PASS   失败: $FAIL"
echo "========================================="
if [ "$FAIL" -gt 0 ]; then
  echo "❌ 存在失败用例，构建不应发布"
  exit 1
fi
echo "✅ 全部离线测试通过"

#  34. test_membership_benefits.py —— 下载会员权益与配额表一致性（2026-10-03）：
#      权益文案改为从 DAILY_QUOTA_LIMITS + FEATURE_USAGE_DEFS 自动生成，
#      守卫钉住「有配额必有文案」+「下载会员不送积分」，以后加配额不会再漏文案。
run_one test_membership_benefits.py

#  35. test_plan_sales_state.py —— 档位售卖状态（2026-10-03）：
#      模式（普通/秒杀/限量/活动）、秒杀价与窗口、限量份数、上下架与活动时间；
#      激活入口强制校验（售罄/未开始/已结束/下架拒发），限量档发放后 sold 自动 +1。
run_one test_plan_sales_state.py

#  36. test_admin_user_usage.py —— 后台「用户使用详情」聚合（2026-10-03）：
#      _usage_bundle 聚合结构（按天倒序/只留>0/激活历史倒序）、store 读失败降级不崩、
#      路由挂载与 require_admin 门禁。daily_usage 里的 date 字段非数值，必须跳过。
run_one test_admin_user_usage.py

#  37. test_plan_mkt_matrix.py —— 营销面板「模式×分组」联动（2026-10-03）：
#      直接解析 web/app.js 的 PLAN_MODES / _MKT_BY_MODE / data-mkt 三处命名做交叉核对，
#      并模拟 4 模式 × 3 分组显隐矩阵。防止「模式 value 与分组标识不同名」
#      （limited vs stock）导致选了该模式却什么都不显示。
run_one test_plan_mkt_matrix.py
