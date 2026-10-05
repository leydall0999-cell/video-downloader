# 视频工坊（VDL）项目接续快照 — 2026-09-26 12:30

> 生成方式：全部结论均由本机/线上实测得出（git、接口、systemd、离线测试），非记忆复述。
> 新会话直接读本文件即可接手，无需回看历史对话。

---

## 0. 一句话现状

**桌面端（app-dev）本轮三项需求全部落地并已发布到线上更新源 v1.0.25；另外应你要求把「网页版与 App 的用户数据」打通了（账号/会员/积分统一到授权中心，`dde9ca8` on web-dev，已部署 ECS 并真机验证）；代码已全部分支推送到 GitHub；离线测试 app 54/54、web 5/5 全绿。**
网页版可自行注册，且「网页版注册 → App 登录」「App 注册 → 网页版登录」**双向直接登录**均已用真实公网通道实测通过。
唯一挂着的是几个**外部阻塞项**（CF 邮箱验证、香港退订等，见 §3.2）。web-dev 那条移植草稿分支已于 2026-09-26 13:54 **删除**。

---

## 1. 项目原始需求

自托管「视频下载 / 媒体备份 + 媒体处理」工具，产品名**视频工坊（VideoDownloader / VDL）**。三分支并行：

| 分支 | 形态 | 部署位置 | 当前状态 |
| --- | --- | --- | --- |
| `app-dev` | **桌面 App（PyInstaller 打包，工作重心）** | 本机 `/Applications/视频工坊.app`，127.0.0.1:8321 | 活，v1.0.25 / 64ac942 |
| `web-dev` | 轻量网页版 | ECS `8.138.223.3:8888`（nginx）+ Railway（**已死**） | 活，回源 ECS |
| `main` | 全功能主线 | — | 冻结（仅基线） |

核心能力：50+ 平台解析下载、格式转换、高效压缩、高清修复、一键抠图、图片/PDF/视频去水印、字幕提取（ASR+歌词库）、视频解说自动成片、扫码分享。
合规红线：只处理自有/已授权内容，不破付费墙与 DRM。

**本轮（2026-09-26）用户提的需求**：
1. 个人资料页**积分要分清楚「过期时间」和「永久积分」**（AI 会员积分随会员到期清零，永久积分永不过期）。
2. 「关于本应用」要能看到**每次更新的更新内容**。
3. **所有功能必须登录后才能用**（对标「点开始下载先弹登录」的行为）。
4. （追加）图片去水印的标题**只保留「图片」**，不应写「图片/PDF/视频 去水印」；并检查连带问题一并修掉。
5. （追加）确认发布 v1.0.25 到线上更新源。

---

## 2. 已完成工作成果

### 2.1 三合一：积分分池可见 + 关于面板更新内容 + 全功能登录门禁
提交：`486aa37`（主体）→ `c6a25c2`（测试运行器 + 门禁名单补 3 项）→ `84b29d7`（会员中心积分口径统一）

- **积分区分**：个人资料页积分卡与会员中心都标注 AI 会员积分「有效期至 …，到期自动清零」（≤7 天高亮告警）、永久积分「永不过期」。前端 `_renderCreditNotes()`，数据源 `/api/member/status` 已带 `ai_member.expire_at`，**无需改后端**。
- **登录门禁（双保险）**：
  - 前端 `web/app.js`：`document` **捕获阶段**事件委托 + `_LOGIN_GATED_ACTIONS` **26 个 id 白名单**，未登录时 stopPropagation 整体阻断 → 不必改 20 多处老绑定，也不会漏改一处就放行。登录成功后 `_replayGatedAction()` 自动补点原按钮。
  - 后端 `server/app.py`：`_login_gate` 中间件，能力型 POST 端点未登录 → **401 `{"code":"NO_AUTH"}`**，防改 JS / curl 直连 8321 绕过；开关 `VDL_LOGIN_GATE=0`。**暂停/恢复/取消/中止类一律放行**（token 中途失效也要能停下任务）。
  - 前端遇 `NO_AUTH` 统一弹登录框（3s 节流），兜住漏网入口。
- 实测证据（curl 本机 8321，必须带 `--noproxy '*'`）：未登录 `POST /api/resolve` → **401 NO_AUTH**（本次回归复核仍为 401 ✅）；`/api/matting/image`、`/api/dw/video` 同样 401；无效 Bearer token 仍 401；`POST /api/dw/video/abc/cancel` → 404（说明收尾类被放行到路由了）；`GET /api/system/info` → 200（查询类未误拦）。

### 2.2 内置更新日志（不再依赖线上发布）
提交：`32b18fc`

- 起因：线上更新源长期停在 1.0.18，面板只能显示 1.0.18 的说明 → 用户「没看懂」自己电脑上这版改了什么。
- 新增 `server/changelog.py`（1.0.25 → 1.0.10 共 9 条）+ `GET /api/system/changelog`（**免登录**，返回 `current/has_current/entries`）。
- 关于面板常驻「更新内容」区块：当前版本在上、历史版本进可滚动区；「刚更新完」优先用 localStorage 缓存条目；内置日志未命中当前版本时退回线上 `latest` 兜底。
- 新增 `server/tests/test_system_changelog.py` 并**登记进 `server/tests/run_offline_tests.sh`**（不登记＝门禁不跑它）。基线由 53 → **54**。
- 👉 **维护约定：以后每次发新版，必须在 `server/changelog.py` 的 CHANGELOG 顶部加一条，version 与仓库根 `VERSION` 一致**，否则面板退回「最新发布版本 v旧版」兜底文案。

### 2.3 发布 v1.0.25 到线上更新源
- 命令：`VDL_RELEASE_NOTES_FILE=/tmp/vdl_notes_1025.txt bash desktop/publish_update.sh --yes`
- 结果：`latest.json` 1.0.18 → **1.0.25**，`notes_list` **11 条**；全量 `VideoDownloader.app.zip` **434.1MB**（sha `a69ff321…`）；增量 `patch-1.0.18_1.0.25.delta` **168.4MB**（`from_version=1.0.18`）。
- 顺手修掉长期坑：更新源首页 `/opt/vdl-update/index.html` **不随脚本更新**，长期停在 1.0.9 + DMG（新用户下到旧包）→ 已重写为 1.0.25 ZIP + 11 条更新内容，备份 `index.html.bak-20260926`。本次复核首页仍正确显示 1.0.25 ✅
- 验证：服务器 `shasum -a 256` 与清单逐字一致；全量/补丁 HEAD 均 200 且 Content-Length 相符；取回片段头 4 字节 = `PK\x03\x04`；`/api/system/latest` 返回 1.0.25 且本机 `update_available=false`。

### 2.4 去水印三入口标题各自独立 + 参数面板字段溢出真 bug
提交：`64ac942`

- **标题**：`dwView` 被「图片/PDF/视频/一键抠图」四个入口共用，大标题却写死三合一。改为 `dwSwitchPane()` 按子模式设标题，实测四个入口分别为：图片去水印 / PDF 去水印 / 视频去水印 / 一键抠图。
- **顺带查出的真 bug**：图片去水印「处理模式」字段**只剩半行「最稳）」**、label 整个不见、半径输入框只有 68px。
  **真因是横向溢出**：全局 `.uc-form{display:grid;align-items:end}`，而 `.dw-card .uc-form` 改成 flex column 时**没覆盖 `align-items`** → flex column 下 `end` 使宽度收缩到内容宽，最宽字段 385px > 卡片 316px ⇒ **左溢出 86px**。
  修：两条 flex 版 uc-form 补 `align-items: stretch`。修复后字段 282px、零溢出。
- **诊断方法论（已固化）**：看到「文字只剩半截」**先量 x 与 width，再量高**。工具 `~/.workbuddy/skills/vdl-frontend-ui/scripts/wk_dump_box.py`（PyObjC 离屏 WKWebView 批量 dump 盒子，会标 ⚠️溢出）。

### 2.5 本轮之前（09-25/09-26 早）已完成并部署（同一 app-dev 线上）
卡密通道下线（`604e19b`）、监控告警进管理后台（`a6cee8c`）、运维看板并入后台（`bf43c3f`）、入口收敛只留「后台管理」（`e40a054`/`61e04b2`）、个人资料页宽度对齐（`cbbff56`）、媒体库卡片间距三连修（`8acb8cd`/`1c8f285`/`17c6913`）、超管实时异常告警（`e7145a2`）、每日入账与充值对账（`aa60de6`）、权益云端权威化三件套（`c468c37`）、小红书接入（`134044b`/`1627554`/`eb4a6fa`）、Cookie 池超管上传（`61fff1f`）。

### 2.6 本次盘点顺带完成的保存动作
- **推送**：`origin/app-dev` = `64ac942`（`5961c98 → 64ac942`，**61 个提交全部上云**，含 09-25 以来全部桌面端工作）；`origin/web-dev` = `dde9ca8`（本轮 5 个提交：4 个观测/小红书 + 1 个「打通 web 与 App 用户数据」）；早期移植草稿分支 `origin/wip/2026-09-26-web-dev-ops-ai-port`（`5b537a7`）已于 13:54 **删除**（本地 + 远端）。
- **工作区清理**：`video-downloader` 检出目录里 09-25 遗留的未提交移植改动（干净地存进 WIP 分支，主工作区已还原干净）。
- **测试复核**：`bash server/tests/run_offline_tests.sh` → **通过 54 / 失败 0** ✅（在 app-dev HEAD `64ac942` 上）。
- **线上健康复核**（本次实测）：`127.0.0.1:8321/api/system/info` → v1.0.25 / 构建 `64ac942 (app) / 03279c6 (pipe)`；ECS `8.138.223.3:8888` → 200；`POST /api/resolve` 云端 → **200 真实解析成功**；`hanyuxz.top` → 200 / 2.1s；更新源 `latest.json` → 1.0.25。
- ECS systemd 实测在跑：`vdl-web` / `vdl-license` / `vdl-pay` / `vdl-share` / `vdl-update` / `vdl-gateway`；另有 18731 `vdl_cookie_daemon.py`、18890 worker 进程（**无独立 `vdl-worker.service`**）。

### 2.7 「打通网页版与 App 的用户数据」（13:15–13:50，`dde9ca8` on **web-dev**，已部署 ECS）
- **背景（实测）**：网页版此前是**另一套账号库** —— 全库只有一个 `pr***@example.com` 占位号，`auth.py` 里 0 处 license/cloud 引用。App 注册的号在网页版登不上，网页版注册的号 App 也不认，会员更谈不上。
- **新分工**：授权中心（8902）= 账号 + 会员 + 积分唯一权威；本机 `auth_store` 退化为「镜像 + 离线兜底 + 功能门禁 bearer 签发方」；云端 `authority` 快照**覆盖**本机 `memberships/{uid}.json`（防篡改）。
- **实现**（纯服务端，**前端零改动** —— 关键取舍）：
  - 新增 `server/license_client.py`（自 app-dev 原样移植，纯 stdlib）、`server/cloud_link.py`（账号桥）。
  - 浏览器设备号 = 服务端签发的 **cookie `vdl_dev`**（首次响应 Set-Cookie）→ 授权中心 `device.fp`；网页版照旧占 1 个设备位（不豁免，否则账号共享能绕开 2 台限制）。
  - `routers/auth.py`：register/login **云端优先**，成功后落本机同号同密镜像；云端 EXISTS → 用同密码试登录；云端不可达 → 退回纯本机（fail-open + `cloud_notice`）。改密 / 忘记密码重置后用上次登录留下的云端 token 把新密码推给授权中心。
  - `membership.py`：移植 `save_account / apply_cloud_authoritative / account_view / cloud_session`；`status()` 增加 `account` 与账号级锁；`spend_credits` 扣减后异步上报云端。`/api/member/status`、`/api/account/profile` 前做 60s 节流刷新（兼心跳保活 + 挤出检测）。
  - 关闭开关 `VDL_CLOUD_LINK=0`；新测试 `server/tests/test_cloud_link.py`（7 例，已登记；web 基线 4 → **5/5**）。
  - **部署要求**：`vdl-web` 必须注入 `VDL_LICENSE_BASE=http://127.0.0.1:8902`（drop-in 已建），否则回落到 hanyuxz.top 走公网 CF。
- **真机端到端证据**：① 网页版注册 → 账号进授权中心；② 模拟 App 注册的账号 → **网页版登录成功 `cloud_synced:true`**（核心判据）；③ 云端 grant + adjust → 网页版 `/api/member/status` 显示会员与积分；④ 第 3 台设备登录 → 挤出最久未用设备并提示；⑤ 网页版花 5 积分 → 云端 20→15。
- **双向登录复核（13:40，全走真实公网通道）**：
  - 通道：App → `https://hanyuxz.top/api/license/*`（nginx 分流）；网页版 → `http://8.138.223.3:8888`（内部 `127.0.0.1:8902`）。**8902 本身不对外**。
  - ①网页版注册 → 用同账号打 `.../api/license/login`（＝ App 登录路径）→ `ok:true`；②`.../api/license/register`（＝ App 注册路径）→ 网页版登录 → `ok:true, registered:false, cloud_synced:true`。
  - 两向的 `account.devices` **同时列出「网页版 · 未知系统/浏览器」与 App 设备** ⇒ 同一账号；云端 grant 会员 + 加积分后网页版 `active / 积分 / expire_at` 与云端**逐位一致**。
  - 结论：**「网页版能注册」= 能；「网页版注册→App 登录」= 能；「App 注册→网页版登录」= 能。**
  - 会话姿势坑：网页版登录/注册返回 **Bearer token**（字段 `token`），后续接口要 `Authorization: Bearer <token>`；**不是** cookie 会话。请求体字段名 `identifier` + `password`。
- **测试账号清理（13:45 已真删，非 setstate）**：云端 `cards.json` 12 → 8、网页版本地 `/root/.video-downloader/users.json` 5 → 0、对应 `memberships/*.json` 一并删；备份 `.bak-cleanup-<ts>`。被删账号再登录 = `BAD_CREDENTIALS`，已确认。云端仍残留 6 个**历史**测试号（`e2e*` / `anticrack_*` / `monitor_*`），非本次产生，未擅自删。
- **仍未打通（如实记录）**：下载记录 / 媒体库 / 任务队列各机各存（只共享 Cookie 池）；**网页版没有在线支付入口**（卡密已下线、云端 redeem 已 410）→ 网页版用户要充值得先在 App 里买。
- 小瑕疵：云端快照不带套餐名 ⇒ 权威覆盖后 `download_member.plan` 为 None（App 亦然，非回归）。

---

## 3. 当前进度与待办

### 3.1 无进行中的代码改动
`video-downloader-app`（app-dev）与 `video-downloader`（web-dev）工作区**均干净**，改动都已提交并推送。桌面端运行版本 **v1.0.25 · 构建 64ac942**；网页版 = web-dev `dde9ca8`（已部署 ECS 8888）。

### 3.2 待办（按优先级）

| # | 待办 | 说明 / 卡点 | 谁做 |
| --- | --- | --- | --- |
| 1 | ~~决定 web-dev 移植草稿的归属~~ **✅ 已完成** | 分支 `wip/2026-09-26-web-dev-ops-ai-port`（`5b537a7`，把 App 的「AI 大模型账户 + 运维看板」缩水回移到网页版）已于 **2026-09-26 13:54 删除**（本地 + GitHub）。删除依据（实测）：①远程访问 `/ops-board`＝**403「仅限本机访问」**（`_require_local`），公网网页版永远进不去；②web-dev 的 `web/ops/board.html` 本就不存在 → 本机打开是 500；③它只是 App 版缩水页（145 行 vs App 297 行，缺时间窗/告警/对账/鉴权），而 App 侧看板**当天已并入管理后台「运维监控」tab**，方向已过时。 | 已完成 |
| 2 | CF 命名隧道（域名入口） | 卡在 **CF 账号邮箱未验证**（Gmail 收不到验证信）→ CF 服务端硬拦隧道授权，无技术绕过。唯一解：换 QQ 邮箱完成验证（你的操作）。ECS `~/.cloudflared/` 仍空、`cert.pem` 未落地。当前 `hanyuxz.top` 靠 **CF 橙云回源香港机**在撑（200/2.1s）。 | 需要你操作邮箱 |
| 3 | 香港节点 `47.82.101.79` 退订 | 一直待退订；退订前需确认命名隧道能接管 `hanyuxz.top`，否则域名会断。 | 你决定时机 |
| 4 | **网页版在线支付入口** | 账号已打通，但网页版没有买会员的入口（`/api/member/activate` 卡密通道已下线、云端 `redeem` 已 410）→ 网页版用户要充值得先在 App 里买。要补就得把 `vdl-pay`（支付宝）接到网页版。 | 你定要不要做 |
| 5 | web 版前端未随本次重发 | 线上 `web/app.js` 仍比 web-dev **旧**（缺 `applyWebTabs` 导航栏兜底修复）。本次打通**只动服务端**，所以没顺手覆盖；要顺手修就单独发一次前端。 | 低优 |
| 6 | 逆向客户端风险 | 纯逆向客户端（yt-dlp 系）随时可能被平台改版打断；**终极解是把高价值能力放云端扣费**。已有 `vdl-gateway` 打底。 | 战略项 |
| 7 | 微信支付 | 目前只接支付宝；充值页卡密通道已下线。 | 待定 |
| 8 | 存量测试债 | `test_web_contract::test_nodes_web_contract` 类失败为**改动前就有**（缺 cloud/archive 组），与近期改动无关，用 `git worktree` 基线对比法已确认。 | 低优 |

### 3.3 下次发版流程（照抄）
1. `server/changelog.py` 顶部加一条（version = 新 VERSION）。
2. 改仓库根 `VERSION`；提交（**先 commit 再构建**，不要 `-dirty`）。
3. `bash desktop/deploy_mac.sh`（冷启动 6~13 分钟，**不带 `--no-build` 会重建**；被杀会留 `.build.lock` + 半成品 dist）。
4. 真机复验（包内 `Resources/web/*` 与 `server/*` 是明文，可直接 grep；或打 8321 真接口）。
5. 用户确认后：`VDL_RELEASE_NOTES_FILE=/tmp/notes.txt bash desktop/publish_update.sh --yes`，并**顺手同步 ECS `/opt/vdl-update/index.html`**。

---

## 4. 关键约束 / 修改要求 / 注意事项

### 4.1 工作方式（用户明确要求，必须遵守）
- **默认只做 app 端（app-dev）**；要动 web-dev 必须先得到用户明说。
- **用户不做任何命令行运维**（SSH / systemctl / VPS 文件编辑都要 AI 自己做，SSH key 已可用、沙盒可直连 `root@8.138.223.3`）。
- 用户报「缺陷」≠ 偏好：**定位根因、给量化证据、改代码根治**；不要列一堆选项让他选。
- 用户说「已做某事」先实测再信。用户说「慢」先读 `~/.video-downloader/stats.json`、`~/.vdl_launch.log`。
- 发布=对外可见，**必须用户确认**。

### 4.2 硬技术铁律（踩过坑的）
- 🔴 **Edit 报成功 ≠ 已落盘**（已出现 4 次，多行/含制表符/长中文最易中招）→ 每处 Edit 后**立刻用单 pattern 复核**。**BSD grep 不支持 `\|` 交替**，用交替会误判成「Edit 丢失」。
- 🔴 长中文 commit message 一律写文件再 `git commit -F`；`-m "…"` 里的反引号会被 bash 当命令替换吃掉内容。
- 🔴 **App 的 WKWebView 不保证 `gap`（grid/flex）与 `aspect-ratio` 生效** → 卡片网格用品格：**margin + min-height** 兜底；**flex column 必须显式 `align-items:stretch`**（否则宽度收缩、横向溢出）。
- 🔴 「文字只剩半截」**先量 x/width 再量高**；量法用 `wk_dump_box.py`（PyObjC 离屏 WKWebView），比 Chromium 猜内核差异可靠；也可 `osascript activate + screencapture -x` 抓真实渲染逐像素量。
- 测试：`bash server/tests/run_offline_tests.sh`，**基线 54/54**；**新增测试文件必须在脚本里 `run_one` 登记**，否则门禁根本不跑它。加全局中间件/鉴权前，先摸清哪些测试直连端点（否则 401 会打爆老测试）。pytest 需 `PYTHONPATH=server:tests`，`--basetemp=tests/_wd_tmp/xxx`。
- 改判断逻辑必跑**基线对比**：`git worktree add --detach /tmp/vdl-base-app HEAD` 跑同一条命令比失败集合。
- 新包 adhoc 签名的 cdhash 变化可能重弹 TCC「访问下载文件夹」授权，无人点击会让 `open()` 永久阻塞（症状：240s 超时且 pydump 卡 `retention._load`）。
- 沙盒内 curl 必须 `--noproxy '*'`（否则回环被代理劫走）。

### 4.3 环境与凭据（速查）
- 桌面源目录：`/Users/suixindelang/WorkBuddy/video-downloader-app`（**app-dev，当前工作仓库**）
- web-dev 检出目录：`/Users/suixindelang/WorkBuddy/video-downloader`
- 本机 App：`/Applications/视频工坊.app` ← 127.0.0.1:8321（重启 `open -a "/Applications/视频工坊.app"`）
- 远端：`origin git@github.com:leydall0999-cell/video-downloader.git`
- ECS `8.138.223.3`：`8888` 网站/解析（nginx）、`8765` 更新源（`/opt/vdl-update`）、`8902` 授权中心（仅 127.0.0.1，走 nginx `/api/license/`）、`18731` cookie daemon；`/opt/vdl-pay` 支付宝。
- **网页版账号打通的相关部署点**：`vdl-web`（跑 `/opt/vdl-worker`）有 drop-in `/etc/systemd/system/vdl-web.service.d/10-license-base.conf` 注入 `VDL_LICENSE_BASE=http://127.0.0.1:8902`；改服务端文件前的备份在 `/opt/vdl-worker/_backup_cloud_link_20260926-132352/`。⚠️ 部署网页版**别整目录覆盖** `server/routers/core.py`（线上多了段未入库的「授权中心告警桥」）。
- 香港 `47.82.101.79`：待退订（现仍在为 `hanyuxz.top` 回源）。
- 超管 `1812804606@qq.com`；`X-Admin-Key` / 运维看板密钥 `be2b20a6…`（同 ECS `VDL_ADMIN_KEY`）；license admin token `a0771c21…`。
- ⚠️ `hanyuxz.top` 的 Railway 应用**已不存在**（报 `Application not found`），**不要再拿「轮询 git_sha 确认部署」当手段**；对外入口只有 `8.138.223.3:8888` 与 `8765`。

### 4.4 相关技能（细则都在技能里，别现场重推）
`vdl-build-release`（构建/发布/验证清单）·`vdl-frontend-ui`（改 web/ 与不重构建自查）·`vdl-pending-audit`（实测盘点）·`vdl-resolve-cookie-diag`（解析失败）·`vdl-account-login-diag`（登录/会员）·`vdl-subtitle-asr-diagnosis`（字幕/ASR）·`vdl-matting`（抠图）·`vdl-dw-refine-gate`（去水印精修）·`vdl-feather-band`（羽化带）·`vdl-commentary-llm-diagnosis`（解说）·`vdl-scan-share`（扫码分享）·`vdl-perf-diagnosis`（慢）·`vdl-add-offline-tests`（补测试）·`vdl-vps-worker-add`（加平台）。
记忆：项目日志 `/Users/suixindelang/WorkBuddy/video-downloader/.workbuddy/memory/YYYY-MM-DD.md`、长期笔记同目录 `MEMORY.md`。

---

## 5. 给新会话的第一句话（可直接粘贴）

> 读 `/Users/suixindelang/WorkBuddy/video-downloader-app/PROJECT_SNAPSHOT_2026-09-26.md`。
> 桌面源在 `video-downloader-app`（app-dev，已 push 到 64ac942，本机跑 v1.0.25）；
> 网页版在 `video-downloader`（web-dev，已 push 到 dde9ca8，部署在 ECS /opt/vdl-worker，
> 账号/会员/积分已与 App 统一到授权中心 8902；网页版可自助注册，且与 App **双向直接登录**已实测通过）。
> 历史草稿分支 `wip/2026-09-26-web-dev-ops-ai-port` 已于 2026-09-26 13:54 删除，不必再管。
> 待办见 §3.2：前面几项是外部阻塞（CF 邮箱验证、香港退订）；下一个可动的开发项是「网页版在线支付入口」。
