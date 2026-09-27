# VDL 项目接续快照 · 2026-09-27 11:17

> 复制到新会话即可接续。两份仓库：
> - web-dev（网页版）：`~/WorkBuddy/video-downloader`，分支 `web-dev`，HEAD `5e6154d`（已推送）
> - app-dev（桌面端）：`~/WorkBuddy/video-downloader-app`，分支 `app-dev`，HEAD `597c081`（已推送）
> 两台服务器：ECS 国内 `8.138.223.3`（`/opt/vdl-worker`）；HK 香港 `47.82.101.79`（`/opt/vdl`，更老的独立分支）

---

## 1. 原始需求

用户拿到竞品 **DataTool**（Mac 安装包 `DataTool-mac-2.0.0-arm64` + 官网 `datatool.vip`），要求：
1. 静态分析它的安装包，搞清楚「视频下载为什么比我们稳」；
2. 把它好的机制搬到 VDL，或另起一个全搬过来先试用；
3. 检查它网页版的下载功能，补到我们的网页版（`hanyuxz.top`）；
4. 用户下达执行指令「开工」→「做」，要求把拆解出的缺口逐个落地。

拆解后定下的落地项（★★ = 优先级最高）：
- ★★ 网页版直链下载升级为**分片并发引擎** + 媒体中继 + 画质 PRO 角标
- ★★ **浏览器内 HLS（m3u8）合成**：浏览器拉清单+分片拼 Blob 存盘，不做转码
- 顺带根治真实缺陷：m3u8 被当成「可直接下载的文件」透传
- 追加：给香港节点补 `/api/media/proxy` 端点

---

## 2. 已完成的工作成果

### 2.1 web-dev 提交链（全部已推送 origin/web-dev）

| 提交 | 内容 |
|---|---|
| `fb6d8f5` | 网页版直链下载升级为分片并发引擎（对标 DataTool）：10MB/片·3 并发·3 重试·可取消，中继 `GET /api/media/proxy`，`.quality-pro` 角标，降级链 分片→单流→浏览器直连 |
| `6fccd00` | 中继探测阶段回落主站，兼容对端节点缺端点 |
| `b60ce6a` | **浏览器内 HLS 合成** + m3u8 不再被当直链透传 |
| `35f815c` | 修正夸大文案（「省服务器流量/未占用服务器出口」不成立） |
| `5e6154d` | `deploy/` 两个香港定点补丁脚本入库（可重放） |

### 2.2 app-dev 提交链（全部已推送 origin/app-dev）

| 提交 | 内容 |
|---|---|
| `dbe37b7` | YouTube 稳定性修复 v1.0.29：①会话自持（`browser` 须早于 30 天 TTL cache）②JS 挑战（`deno`+`yt-dlp-ejs`） |
| `597c081` | m3u8 不再被当作「可直接下载的文件」透传（与 web-dev 同源修复） |

### 2.3 线上部署现状（均已核验）

**ECS 8.138.223.3**（服务 `vdl-web`，cwd `/opt/vdl-worker/server`）
- `server/downloader.py`、`web/app.js`、`web/index.html` 已下发，sha256 与本机逐字节一致
- m3u8 的 `direct_url` 已变为 `None`；页面含 `#browserHlsBtn`
- 备份在 `/opt/vdl-worker/backup/`（`*.pre-hls`、`*.pre-copy`）

**HK 47.82.101.79**（`/opt/vdl`，服务 `vdl-web`）
- `deploy/hk_patch_m3u8.py` 已上机：`_DIRECT_EXT_RE` 去掉 m3u8 + `_detect_direct_url` 加 HLS 护栏
- `deploy/hk_patch_media_proxy.py` 已上机：`core.py` 追加 `/api/media/proxy`（603→718 行），`app.py` CORS `allow_headers` 放行 `Range`

### 2.4 关键实测数据（证据，别再重测）

**浏览器内 HLS 端到端（真实浏览器）**
- jwplayer（原先 ECS 中继 502、根本下不了）→ 合成成功 15.8 MB `.ts`，TS 188 对齐零错位，请求 **hk=8 / 主站=0**
- mux（回归）→ 47.3 MB `.ts`，请求 **hk=42 / 主站=0**（补端点前是 hk=1 试探 + 41 次主站）
- 产物 `~/Downloads/Cycle Tour_ Tuscany to Umbria.ts`、`master.ts` 均校验通过

**离线守卫**
- `test_web_hls_assemble.js` **68/68**（含对端回落用例）
- `test_direct_url_hls.py` **22/22**（两棵树各一遍）
- app-dev `test_downloader_url_parsing.py` **20/20**

**出海矩阵（curl 实测）**
- ECS 直连：mux 200/0.84s、B站 200；**YT / google / googlevideo / cdn.jwplayer.com 全 000**
- `VDL_PROXY`→HK:8080：被墙站**仍 000**（代理不解锁，只值「借香港 IP 过地域封锁」，非墙站反而更慢）
- HK 直连：全部 200

**架构结论（09-27 实测）**
- 🔴 **两台是「双出口」，不是冗余，任一台都砍不得**
  - ECS＝国内 IP 出口：HK 靠 `VDL_PROXY_CN`→ECS:18888 改出口（实测香港→广东广州阿里云）；砍 ECS＝国内站地理围栏 403 + 支付宝/授权/分享/Cookie池/自更新全断
  - HK＝海外 IP 出口：砍 HK＝海外站全断
- 🔴 **主站入口其实已出海**：厦门电信访问 `hanyuxz.top` → `cf-ray:…-AMS`（阿姆斯特丹）。大陆首屏瓶颈在 **CF 节点分配**，不在服务器放哪

---

## 3. 当前进度与未完成待办

**当前状态：本轮任务链已全部落地并验证，无进行中的代码改动。两仓库工作区干净（已跟踪文件无未提交修改）。**

### 待办清单（按优先级）

| # | 事项 | 状态 | 说明 |
|---|---|---|---|
| 1 | **桌面端 v1.0.30 重建** | ⏸ 未做 | `downloader.py` 的 m3u8 护栏已提交 `597c081`，但 v1.0.29 安装包是旧的。重建约 10 分钟 |
| 2 | **香港换更便宜的 VPS** | ⏰ 2026-10-21 | 已建一次性提醒（10:00）+ 项目日志 TODO。HK 只承担「海外 IP 出口 + media/proxy」，可降级；**ECS 不可动** |
| 3 | 大陆首屏优化 | 💡 建议 | 需 ICP 备案 + 国内 CDN，或换能分亚洲节点的 CDN。当前绕阿姆斯特丹 |
| 4 | 桌面端是否要加浏览器 HLS 合成 | ❌ 建议不做 | 桌面端后端在本机，无需中继/对端回落，`save_direct_url` 原生桥即可 |

### 换机（待办 2）执行清单与验收判据
1. 新机部署 `/opt/vdl` 同版本（老独立分支，**禁整文件覆盖**，只能定点 patch）
2. 重放 `deploy/hk_patch_m3u8.py` + `deploy/hk_patch_media_proxy.py`（幂等，锚点命中须=1，写前备份、写后 py_compile）
3. DNS `hk.hanyuxz.top` 改指新 IP（DNS-only）；证书走 `/root/vdl-hk-cert/deploy.sh` 重签
4. ECS `40-peer.conf` 的 `VDL_PEER_ENDPOINT`/`VDL_ALLOW_ORIGINS` 同步改址并重启 `vdl-web`
- **验收**：① jwplayer 清单经 HK 中继取 → 200（ECS 走是 502）② HK 直连 YT+google → 200 ③ 浏览器内合成 mux 回归，请求 hk=N / 主站=0，产物可播

---

## 4. 关键约束、修改要求、注意事项

### 4.1 节点与部署
- **ECS 现网 ≠ 本机 HEAD**（`core.py` 多线上超管告警代码）⇒ **定点 patch，禁整文件覆盖**；下发前先 `diff` 确认 0 行意外差异，写前备份
- **HK 是老独立分支**，与 web-dev 不同源 ⇒ 补丁只追加不改既有函数，**必须入库**（只留 /tmp 重装即永久丢失）
- **HK 那份「解析类」修复必须同步**：海外 `direct_url` 由 HK 决定，只改 ECS 会复发（第一轮线上复验就是这么被打回来的）
- 🔴 **新链路必须自带「对端→主站」回落**，且只在**第一条请求**做（HK 缺新端点＝常态）
- 别给桌面端设 `VDL_PEER_ENDPOINT`（`resolve` 是 `async def`，同步转发会冻结整站）

### 4.2 网页版 vs 桌面端（别混为一谈）
- 🔴 **「媒体中继 / 对端回落 / 省不省服务器出口」全是网页版专有**：桌面端无 `media_proxy`、无 `_dlRunHls`，直链走本机桥 `save_direct_url`，`peer` 恒空
- 桌面端后端是 `127.0.0.1:8321`，页面与接口同源、出海靠用户自己的 VPN，不存在跨域/出海/出口归属问题

### 4.3 浏览器内 HLS 合成（web-dev）
- 容器**不转码**：fMP4/CMAF → `.mp4`，MPEG-TS → `.ts`（ffmpeg.wasm 30MB+ 不值当）
- 加密流 / 直播流 / 超 4000 片 / 空清单 **一律抛错**，绝不静默产出坏文件
- 相对地址必须相对清单自身 URL 解析，解析失败须抛错；`#EXT-X-MAP` 初始化段必须排最前
- 🔴 **`m3u8` 不得进 `_DIRECT_EXT_RE`**（否则存下几百字节的播放列表废文本），ECS + HK 都要
- ⚠️ 文案别夸大：分片经中继 ⇒ 服务端出口**进出各一趟（≈2N）**，与「开始下载」相同；省的是落盘 + ffmpeg 转码 + 「先下完再发」的等待
- 前端是 IIFE ⇒ 测试须 `new Function` 注入依赖才能导出引擎函数

### 4.4 Cookie
- 🔴「带 Cookie」≠「已登录」：抓 YT 首页看 `"LOGGED_IN"`；快照字段过期 ⇒ false，**用前现取**
- 候选序 `user>env>cache>pool`，cache 在 pool 前会遮蔽新鲜值 ⇒ 失效即 `drop_cached_cookie()`
- `/api/cookie/sync` 限流按 (IP,域) 计，**ECS 与 HK 两处都要打**

### 4.5 工具与常见坑
- 🔴 **验 HTTPS 一律用 `/usr/bin/curl`**：本机默认 curl 是 anaconda 版，CA 库太旧，会误报 `unable to get local issuer certificate` / `http=000`，看着跟服务器挂了一模一样
- 同文件**禁并行 Edit**（都报成功但后覆盖前）⇒ 串行 + 立刻 Grep 复核；**Edit 报成功 ≠ 落盘**
- BSD grep 不支持 `a\|b` ⇒ 用 Grep 工具；沙盒截 glob、拦出网；macOS 无 `timeout`
- zsh：glob 无匹配会中断整条命令；**长中文 commit message 写文件再 `-F`**（`-m` 里反引号会被吞）
- `pkill -f` 会连自己杀 ⇒ 先取 PID；内存紧时 Node 测试放后台（会 OOM exit 137）
- 带 yt_dlp 的解释器：`~/.workbuddy/binaries/python/envs/vdl/bin/python`（HK 上是 `/opt/vdl/.venv/bin/python`）
- Cookie 解密需 `pycryptodome`；构建用 `VDL_BUILD_WORKPATH` 绕沙盒守卫；**先 commit 再构建**
- 测试红先 `diff` 另一棵树同名文件（会漂移）；**别按旧断言改产品代码**

### 4.6 用户工作方式约定
- **AI 能自己做的运维绝不让用户做**：SSH 改文件/重启/看日志、`curl` 线上自查，都自己跑
- 报「缺陷」≠ 偏好：找根因、量化证据、改代码根治；**要单一答案，反感列选项**
- 用户说「已做某事」先实测再信
- 输出简洁中文，不要多余闲聊

---

## 5. 常用指针

- 记忆：`.workbuddy/memory/MEMORY.md`（长期，≤3000 字符）+ `.workbuddy/memory/2026-09-27.md`（今日详细日志）
- 技能：`vdl-web-dev-ship`（网页版改动+浏览器侧下载，含 §8 HLS 合成、§9 收尾清单）｜`vdl-build-release`｜`vdl-youtube-sabr-diag`｜`vdl-resolve-cookie-diag`｜`vdl-add-offline-tests`｜`mac-app-static-analysis`（竞品分析）
- 香港补丁：`deploy/hk_patch_m3u8.py`、`deploy/hk_patch_media_proxy.py`
