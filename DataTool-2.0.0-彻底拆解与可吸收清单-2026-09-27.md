# DataTool 2.0.0（mac arm64）彻底拆解 · 可吸收清单

> 分析对象：`/Applications/DataTool.app`（`vip.datatool.datatool`，CFBundleVersion 2.0.0，732 MB）
> 方法：静态解包只读，不运行目标程序。asar 结构解析 + obfuscator 字符串表还原 + 二进制 `strings`
> 产出路径：本文所有结论均落到**具体文件路径 + 字符串**，可复核

---

## 0. 一句话结论

**内核层我们没落后——yt-dlp 版本完全相同（都是 2026.8.19），他们也没改 extractor。**
真正的差距在三个地方：① **HLS 用专业下载器（N_m3u8DL-RE）而不是自己拼**；② **代理链路做了完整适配（系统代理探测 + SOCKS→HTTP 桥）**；③ **下载失败有"换路由重试"的自愈机制**。
另外他们有一整块**浏览器内嗅探 + 悬浮球 + 扩展**的产品形态，这是我们完全没有的。

---

## 1. 技术栈与自带武器库

| 项 | DataTool 2.0.0 | VDL 桌面端 | 差距 |
|---|---|---|---|
| 框架 | Electron（`NSPrincipalClass=AtomApplication`）+ `ee-core@4.1.3`（electron-egg）+ Vue3 | FastAPI + PyWebView/原生壳 | 路线不同 |
| yt-dlp | **2026.8.19**（官方原版，943 个 extractor，无 `--subtitles-probe-only` 等私有参数） | **2026.8.19**（`dist/.../yt_dlp-2026.8.19.dist-info`） | **持平** |
| JS 挑战 | 自带 **deno 2.9.6**（实测 `--version`）+ `yt_dlp_ejs 0.8.0` | 已修通（v1.0.29 `dbe37b7`） | 持平 |
| HTTP 分片下载 | **aria2c 改名 `boost-core`**（4.9 MB，`strings` 命中 aria2 帮助文本全文） | 桌面端 aria2c 已随包（88 MB，`desktop/bundle_aria2.py`）；普通下载**有开关但未默认启用**（`downloader.py:4421` `VDL_DOWNLOADER=aria2c`），实测未突破单 IP 限速顶 | ~~我们有但没用在正地方~~ → **已落地，无增量** |
| HLS 下载 | **N_m3u8DL-RE 改名 `m3u8-dl`**（20 MB，.NET AOT；`strings` 命中 `N_m3u8DL-RE.Common`、`LoadM3u8FromUrlAsync`、`MergeByFFmpeg`、`ParseHLSCustomKey`） | 桌面端走 yt-dlp；网页版是我们自己写的浏览器内拼接 | **明显落后** |
| ffmpeg | 自带 `ffmpeg` + `ffprobe`（各 ~50 MB） | 自带 | 持平 |
| Python | 自带 Python **3.12** 运行环境（`extraResources/python/bin/python3.12` + `yt-dlp`） | 打包进 app | 持平 |
| 浏览器扩展 | 自带 MV3 扩展 `datatool-ext/chrome-mv3-prod`（plasmo 构建），覆盖 tiktok / 快手 / 小红书 / X / onlyfans / 新片场 / vimeo / facebook / bilibili | **无** | **我们没有** |
| 本地库 | typeorm + better-sqlite3（任务持久化） | SQLite | 持平 |
| 云/AI | ali-oss + @alicloud/credentials；openai + @langchain/* + js-tiktoken | 自建 LLM 网关 | 路线不同 |

关键源码位置：`app.asar` → `public/electron/`（289 个 js，3.91 MB，全部 obfuscator.io 混淆）
`extraResources/yt_dlp_bridge.py`（**1811 行，明文未混淆**，是他们 yt-dlp 调用的完整实现）

---

## 2. 下载内核：四条路，各自的参数

### 2.1 aria2c（普通 HTTP 直链）— `service/aria2c/aria2c-rpc.js`

启动参数（原文字符串）：
```
--enable-rpc --rpc-listen-port=<N> --rpc-listen-all=true
--rpc-allow-origin-all=true --rpc-max-request-size=200M
--split=4 --min-split-size=8M --max-connection-per-server=4
--retry-wait=5 --max-file-not-found=10
--seed-time=0 --seed-ratio=0.0
--bt-tracker-connect-timeout=10 --bt-tracker-timeout=30
```
二进制文件名为 `boost-core` / `boost-core.exe` / `boost-core-linux`（**改名防特征识别**）。

**进程治理做得比我们细**（字符串实证）：
- `[aria2c] Port <N> is in use, switching to port …` → 端口冲突自动换端口
- `[aria2c] netstat found lines for port …` / `Found PIDs via lsof on port …` → 多手段查占用
- `[aria2c] Exceeded maximum restart attempts (…)` / `Too many restart attempts in a short period. Waiting before retrying.` → 重启次数 + 冷却窗口双限流
- 退出时按平台清进程（SIGTERM / `taskkill /F /PID`）

### 2.2 N_m3u8DL-RE（HLS/m3u8）— `service/m3u8.js`

```
m3u8-dl <url> --header "User-Agent: …" --header "Referer: …"
  --save-dir <dir> --save-name <name> --tmp-dir /tmp/m3u8dl-re
  --ffmpeg-binary-path <ffmpeg> --auto-select
  --write-meta-json --log-level … --no-ansi-color --no-log
```
这是**目前开源界最强的 HLS 下载器**：支持 `#EXT-X-KEY` 解密、master 自动选档、分片并发、ffmpeg 合并、断点续传、直播录制。

对照我们自己刚做的「浏览器内 HLS 合成」：**加密流直接抛错、不转码、超 4000 片抛错**。差距是实打实的。

### 2.3 yt-dlp（YouTube / 通用站点）— `extraResources/yt_dlp_bridge.py`

传给 yt-dlp 的参数全集：
```
--cookies --js-runtimes --player-client --proxy
--ffmpeg-location --format --socket-timeout --skip-audio-tracks --output
```

**几个值得直接抄的细节**（都是 bridge 里的注释/代码原文）：

1. **默认客户端就用 yt-dlp 主线默认**
   ```python
   def resolve_youtube_player_clients(player_client):
       """未指定 client 时使用 yt-dlp 主线默认组合（当前为 visionos + web）。"""
       if player_client: return [player_client]
       return ["default"]
   ```
   → 他们**没在客户端选择上搞特殊**，别神化竞品。

2. **解析阶段刻意不 skip HLS/DASH**（`build_info_opts`）
   ```python
   # 勿 skip hls/dash：登录态 web client 的高清在 HLS/DASH（web_safari HLS 可无 PO Token）
   "skip": ["translated_subs", "chapters"],
   "max_comments": ["0"],
   "player_skip": ["configs"],     # ← 跳过 player configs 请求，提速
   ```
   → `player_skip: configs` 是**提速项**；我们记忆里有「YT 解析慢 ~2min」，这条很可能直接有效。

3. **多音轨发现阶段参数相反**（`build_audio_track_opts`）
   ```python
   """多音轨发现专用 opts：… 避免提速用的 hls/dash skip 把 audioTrack 元数据裁掉。"""
   "skip": ["translated_subs","chapters","dash_manifest","hls_manifest"],
   "player_client": [client], "max_comments": 0, "innodata": False
   ```
   → 同一站点、不同目的用**两套 extractor_args**，这个思路我们完全没有。

4. **cookie 每次复制新副本**（`ensure_pristine_cookies` / `cookiefile_for_ydl`）
   ```python
   """缓存 Electron 传入的原始 Netscape 内容；yt-dlp 会回写 cookiefile，不能直接复用同一路径。"""
   """每次 YoutubeDL 使用一份全新 cookie 副本，避免上一次回写破坏 Netscape 头。"""
   fd, path = tempfile.mkstemp(prefix="yt_cookies_ydl_", suffix=".txt")
   ```
   → **这是他们比我们稳的一个具体原因**。我们记忆里有「快照滚动字段过期 ⇒ false」「失效即 `drop_cached_cookie()`」，属于同一类坑，但我们是"事后发现失效再丢"，他们是"每次给一份干净副本，从结构上避免污染"。

5. **自带音频 n-challenge solver**（不依赖 yt-dlp 之外的网络服务）
   - 脚本：`extraResources/youtube_audio_solver/0.7.0/yt.solver.lib.min.js` + `yt.solver.core.min.js`（来自 `yt-dlp/ejs` release）
   - 执行：`run_audio_n_solver()` 用 **node**（不是 deno）跑，`node <23.5` 加 `--experimental-permission`，否则 `--permission`
   - 手法：stdin 注入 `globalThis.self = {location:{origin:"https://www.youtube.com"}}` shim → 拼脚本 → `console.log(JSON.stringify(jsc(payload)))`
   - 用途：解 `signatureCipher`、`fmt_url` 的 `n` 参数（`decrypt_signature_cipher` / `decrypt_fmt_url_n` / `resolve_audio_stream_url`）
   - 缺失时才去 GitHub 下载，且**优先带代理尝试、失败再直连**（`for request_proxy in [proxy, None] if proxy else [None]`）

6. **会话来源**：`utils/youtube-session-cookies.js` + `utils/bilibili-session-cookies.js`
   Electron 分区 `persist:analyzer` / `persist:default` → 导出 Netscape txt → 喂 yt-dlp。
   日志串：`[youtube-cookies] using … cookies for yt-dlp login=… login_names=…`、`LOGIN_INFO`
   → 与我们从 DataTool 得出的旧结论一致：**会话在用户自己浏览器里出生**。

### 2.4 CDP 嗅探 + 页面悬浮球 — `lib/cdp.js` / `utils/sniffer-inject.js`

- `CdpSession`：`Target.setAutoAttach` → `Page.enable` → `Page.addScriptToEvaluateOnNewDocument`
- 识别目标：`mpegurl`、`audio/x-mpegurl`、`isM3u8`、`googlevideo.com`、`youtubei.googleapis.com`、`mime_type=audio`、`segments`
- 页面注入：`overlay` + `fab`（悬浮球，图标路径 `M12 4v10m0 0l4-4m-4 4l-4-4M5 19h14`）+ `drawer`（抽屉面板），在你看视频的页面上直接点下载
- `blockedHosts = [youtu.be, googlevideo.com, youtubei.googleapis.com, onlyfans.com]` —— 这些域名不注入 UI
- `plugin_batch_download_media`（批量下载媒体）
- 配合 `utils/stealth.js` 做反检测

---

## 3. 代理链路：这才是"稳"的隐性基础

| 能力 | 文件 | 实证 |
|---|---|---|
| **系统代理自动探测** | `utils/detect-system-proxy.js` | macOS `scutil --proxy`、`networksetup -getwebproxy/-getsecurewebproxy "AirPort"/"Ethernet"`；Windows `reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyServer`；`route -n get default`；日志 `[system-proxy] scutil http/https/socks proxy: …`、`detected windows proxy:` |
| **SOCKS→HTTP 本地桥** | `utils/socks-http-bridge.js` | 起本地 HTTP 服务器，`CONNECT` 隧道转发到 socks5/socks4；日志 `[socks-http-bridge] Started bridge …`、`CONNECT tunnel failed:`；失败回 `HTTP/1.1 502 Bad Gateway`。**解决"yt-dlp/aria2 只认 http 代理，用户开的是 socks5 VPN"** |
| **三模式代理** | `utils/network-proxy.js` | `direct` / `system` / `fixed_servers`；导出 `getAxiosProxyRuntimeConfig`、`getAria2ProxyOptionsFromUrl`（映射 aria2 的 `all-proxy` / `all-proxy-user` / `all-proxy-passwd`）、`getElectronSessionProxyConfig`、`getBridgeCompatibleProxyUrl` |
| **失败换路由重试** | `service/aria2c/aria2c-service.js` | `[Aria2cService] Route switch retry started remote->local. failed_gid=…`、`Auto TLS retry started. failed_gid=…`、`buildDirectRetryKey`、`remoteToLocalRetryExhaustedKeys`、`proxyFallbackUrlSet` → **远端（代理/CDN）失败自动切本地直连重试，且有重试耗尽集合防抖动** |

→ 最后一条跟我们网页版做的「对端→主站回落」是同一思路，但他们做得更细：区分 TLS 重试 / 路由重试、各自独立 key、有耗尽集合。

---

## 4. 产品面：他们有、我们没有的

前端 8 个路由：`/home` `/downloader` `/transcriber` `/subtitle-editor` `/transcription-summary` `/media-tools` `/image-tools` `/settings`

| 模块 | 实证 | 我们 |
|---|---|---|
| 转录 + 说话人分离 + 字幕编辑器 | `service/transcriber/`（400 KB，含 `transcribeProcessor`/`parseProcessor`/`transcodeProcessor`）；`说话人{n}`；`subtitle_token_budget` / `subtitle_char_budget` | 有 ASR，无说话人分离/字幕编辑器 |
| 多音轨下载 | `--audio-tracks-only` / `--skip-audio-tracks` / `--subtitles-probe-only`；`AUDIO_TRACK_LANGUAGE_ZH_MAP`（21 种语言中文名） | 无 |
| 视频擦除水印 | 火山引擎 MediaKit / 阿里云 ICE 双后端 | 有自研去水印 |
| 图片工具 | 抠图、超清修复、转动漫、改字、翻译（走阿里云百炼） | 有抠图/去水印 |
| 抖音作者批量获源 | `service/douyinAuthor/` + 扩展 + 窗口内登录 + 滚动翻页 | 无 |
| 云存储 | ali-oss 直传 | 有扫码分享 |
| 命名规则模板 | `utils/naming-rule-utils.js`：变量 `{platform}{domain}{title}{quality}{index}{date_published}{content}{link_id}{filename}{random}` | 无 |
| 文件名唯一化 | `utils/filename-utils.js`：`ensureUniqueFilename` + `reusableBaseNames`/`occupiedBaseNames` 复用池，从 `content-disposition` 探测文件名 | 无 |
| 响应头透传白名单 | `utils/download-request-headers.js`：`accept-ranges`/`content-range`/`content-disposition`/`x-oss-*`/`ali-swift-*`/`x-datatool-platform`… | 无 |
| 统一 HTTP 重试 | `lib/httpRetry.js`：`withHttpRetry(maxRetries, retryDelayMs)` | 有零散重试 |

---

## 5. 可吸收清单（按性价比排序）

### ★★★ 立刻能做、成本低

| # | 吸收项 | 证据 | 工作量 | 复核结论（2026-09-27） |
|---|---|---|---|---|
| 1 | **cookie 每次用 `tempfile.mkstemp` 新副本**喂 yt-dlp，不复用同一路径 | `yt_dlp_bridge.py:114-144` 注释原文「yt-dlp 会回写 cookiefile」 | 极小 | ❌ **不适用**：我们从不写 cookiefile，cookie 一律以 `http_headers["Cookie"]` 注入（全仓 grep `cookiefile` 零命中），结构上不存在「回写污染」 |
| 2 | **`player_skip: ["configs"]`**（+`max_comments:0`、+`skip: translated_subs,chapters`） | `yt_dlp_bridge.py:187-210` | 极小 | ✅ **已落地**（只取 player_skip，理由见下） |
| 3 | **SOCKS5→HTTP 本地桥** | `utils/socks-http-bridge.js` | 小 | ❌ **不必要**：yt-dlp 自带 `yt_dlp/socks.py`（Public Domain 实现，无需 PySocks），原生支持 `socks5://`；aria2 同样原生支持 |
| 4 | **系统代理自动探测**（macOS `scutil --proxy`） | `utils/detect-system-proxy.js` | 小 | ❌ **已有**：`_macos_system_proxy()`（scutil）+ `_probe_local_proxy_ports()`（常见端口兜底） |
| 5 | **文件名唯一化 + 命名模板变量** | `utils/filename-utils.js`、`naming-rule-utils.js` | 小 | ⏸ 待做 |

> 复核方法：先 grep 自己代码再决定抄不抄。**竞品的解法服务于它的实现路径**（它用 cookiefile + Electron 无 socks 支持），
> 不等于我们的缺口。4 条里只有第 2 条是真缺口。

### 第 2 条落地记录（2026-09-27）

只取 `player_skip`，**不取** `max_comments` / `skip: translated_subs,chapters`：
- `max_comments`：yt-dlp 默认 `getcomments=False`，传了是空转；
- `skip:`：会裁掉字幕/章节元数据，收益（省一次请求）不抵风险，我们前端后续要用。

实测（香港节点 47.82.101.79，yt-dlp 2026.8.19，同一视频跑两遍、交替顺序抵消预热偏差）：

| 客户端链 | 格式数 | 协议分布 | 耗时 baseline → skip_configs |
|---|---|---|---|
| `web_safari`（web-dev） | 11 → 11 | mhtml×4 + m3u8_native×6 + https×1（**不变**） | 5.63→4.52s、4.83→4.49s |
| 默认链（app-dev） | 49 → 49 | mhtml×4 + m3u8_native×17 + https×28（**不变**） | 5.54→4.18s、4.11→4.43s |

⇒ **省一次 ytcfg 请求、耗时不劣化，且不裁掉 HLS/DASH**（skip 类参数最容易误伤格式列表，这条必须实测而非照抄）。

落地：`server/downloader.py` 的 `_base_options()` YouTube 分支，带 `VDL_YT_PLAYER_SKIP=0` 紧急回滚开关；
离线用例 `tests/test_youtube_player_skip.py`（web-dev + app-dev 各一份）。

### ★★ 值得做、但要评估

| # | 吸收项 | 说明 | 回本仓复核（2026-09-27） |
|---|---|---|---|
| 6 | **HLS 换 N_m3u8DL-RE**（桌面端） | 补齐加密流解密、自动选档、并发合并。二进制 20 MB 需随包。网页版无法直接用（要落盘到服务器） | ⏸ **不优先**：见下方「6 vs 7 决策」 |
| 7 | **普通直链下载接 aria2 RPC**（桌面端） | 我们 aria2 已随包但只服务种子。`--split=4 --min-split-size=8M --max-connection-per-server=4 --retry-wait=5` 这套参数可直接抄 | ❌ **已经做完了**：`downloader.py:4421-4429` 早有 `VDL_DOWNLOADER=aria2c` / 请求字段 `downloader_type` 开关 + `_build_aria2c_args()`；`desktop/bundle_aria2.py` 已把 aria2c+7 dylib（88 MB）打进包。报告第 24 行「普通下载不用」表述不准，应为「已实现开关、默认不启用」 |
| 8 | **远端失败自动换路由重试**（网页版） | 把现有「对端→主站回落」升级成：区分 TLS 重试/路由重试、独立 key、耗尽集合防抖 | ⏸ 网页版可考虑 |
| 9 | **双套 extractor_args**（解析用 / 多音轨发现用） | 不同目的用不同 skip 组合 | ⏸ |
| 10 | **aria2 端口冲突自愈 + 重启限流** | 换端口、netstat/lsof 查占用、重启次数+冷却双限流 | ❌ 我们用 yt-dlp 的 external downloader 接口调 aria2c，**不起 RPC 守护进程**，无端口冲突问题 |

#### 6 vs 7 决策：两个都不做，真正的瓶颈是单 IP 限速

**7（aria2）已经落地且实测无收益** —— `downloader.py:1081-1086` 的注释就是当年实测原文：

```
# 腾讯等平台实测：单连接限速 ~1KB/s，但**单 IP 总带宽硬顶 ~18KB/s**（与并发数无关）。
# 16 并发已吃满该上限（VPS 实测：5并发=5KB/s, 16并发=18KB/s, 32/64/aria2c 均未突破）。
```

⇒ 换下载器（aria2c 也好、N_m3u8DL-RE 也好）**在单 IP 上都不可能突破这个顶**。

**6（N_m3u8DL-RE）唯一真增量是加密方式覆盖**，但 yt-dlp 的实际边界是
`downloader/hls.py:63`：`r'#EXT-X-KEY:METHOD=(?!NONE|AES-128)'` → 只有 **SAMPLE-AES / 私有 METHOD** 会被判 unsupported；
AES-128 由 HlsFD 自己解（或交给 ffmpeg），明文更不用说。而我们目标站群（优酷/腾讯/爱奇艺/B站/YouTube）
基本落在「明文 + AES-128」区间，SAMPLE-AES 命中率极低。

并且它**不是 yt-dlp 的 external downloader** —— aria2c 能接是因为 yt-dlp 有 `options["downloader"]` 接口，
N_m3u8DL-RE 没有，**必须完全旁路 yt-dlp 重写 HLS 子系统**（进程管理、进度解析、失败重试、合并、任务状态全重建）。

| 项 | 6 N_m3u8DL-RE | 7 aria2 接直链 |
|---|---|---|
| 现状 | 未做 | **已做完**（开关+随包+实测） |
| 增量 | SAMPLE-AES/私有加密、自动选轨、直播录制 | 无（实测未突破限速顶） |
| 工作量 | 大（旁路重写 HLS 子系统） | 无 |
| 随包 | +20~40 MB（现有包体 1.0 G，占比可忽略） | 已含 88 MB |

⇒ **结论：先不做这两个。** 剩下唯一可能的路线是「换 IP」—— 我们已有香港（47.82.101.79）+
ECS（8.138.223.3）两个出口，把同一任务的分片/字节段调度到多出口并行拉取，理论上能绕开单 IP 限速。

#### 「多出口并行」实测：机制可行，但**不值得做**（2026-09-27）

对象：B 站 `BV1GJ411x7h7` 的 30080 码率 m4s 直链（69.76 MB，`upos-sz-mirror08h.bilivideo.com`）。
用 HTTP Range 分段，每端 32 MB。

| 组 | 配置 | ECS | HK | **总吞吐** |
|---|---|---|---|---|
| A2 | ECS 单连接 32 MB | **15.29 MB/s** | — | **15.29 MB/s** ⭐ |
| B2 | HK 单连接 32 MB | — | 2.22 MB/s | 2.22 MB/s |
| C | **跨 IP 并行**（ECS 前半 + HK 后半） | 9.76 ↓36% | 2.06 ↓7% | **11.82 MB/s** ❌ |
| D | **ECS 单 IP 2 并发** 64 MB | 10.74 + 6.46 | — | **12.74 MB/s** ❌ |

**三条硬结论：**

1. ✅ **机制可行**：B 站 `?e=` 签名**不绑 IP**（HK 的 HEAD 同样 200、Content-Length 一致），
   且 `accept-ranges: bytes`，跨 IP 拉不同段全部返回 **206**。技术障碍为零。
2. ❌ **但吞吐不叠加，还倒退**：跨 IP 并行 11.82 **低于** ECS 单连接 15.29；
   连 ECS 单 IP 内 2 并发（12.74）也低于单连接。⇒ **B 站的限速维度是「资源 token」而非 IP**，
   加连接、加 IP 都突破不了那个 ~12~15 MB/s 的顶。
3. ❌ **HK 是国内站的劣质出口**：拉同一 CDN 只有 2.22 MB/s，比 ECS 慢 **6.9 倍**（跨境 + 线路）。
   把它拉进来只会拖慢整体。

⇒ **多出口并行对国内站（B 站这类）是负收益，不工程化。**
（注：腾讯那 18 KB/s 顶按注释是「单 IP 总带宽」维度，与 B 站的 token 维度不同，
若将来真要做，需**先用腾讯源单独复验**，不能拿 B 站结论直接套。）

**副产品（可直接用）**：B 站签名不绑 IP ⇒ **ECS 解析出的直链可被 HK 直接使用**，
说明「对端→主站回落」里复用主站解析结果这条路是安全的，不必担心 token 与出口 IP 绑定。

### ★ 战略级、成本高

| # | 吸收项 | 说明 |
|---|---|---|
| 11 | **CDP 嗅探 + 页面悬浮球** | 这是「在真实浏览器上下文里拿流」的产品化，技术难度中高；但它是竞品"看起来很稳"的重要观感来源 |
| 12 | **MV3 浏览器扩展**（站点覆盖） | 他们覆盖了 tiktok/快手/小红书/X/onlyfans/新片场/vimeo/facebook/bilibili，我们靠 yt-dlp extractor，扩展能拿 yt-dlp 拿不到的站 |

### ✗ 不建议吸收

- **自带完整 Python 3.12 运行时**（80 MB+）：我们是 Python 项目，本来就有
- **在客户端选择上做特殊优化**：实测他们 `resolve_youtube_player_clients()` 直接返回 `["default"]`，跟主线一致——**别神化，他们也没做**
- **改 yt-dlp 源码**：他们是官方原版 943 extractor，一个都没改
- **混淆源码**：他们 obfuscator 是防抄，不是技术手段

---

## 6. 复核方法（下次可直接复用）

```bash
# 1. 解 asar（asar 不压缩，自解比装工具快）
python3 - <<'PY'
import struct,json
d=open("/Applications/DataTool.app/Contents/Resources/app.asar",'rb').read()
_,_,_,jsz=struct.unpack('<IIII',d[:16])
hdr=json.loads(d[16:16+jsz].decode('utf-8'))   # offset 在 JSON 里是字符串，取数据前要 int()
PY

# 2. obfuscator 字符串表还原（关键一步，否则关键词全 0 命中）
#    \xNN / \uNNNN 全局还原成明文后再 grep
python3 -c "
import re,sys
s=open(sys.argv[1],encoding='utf-8',errors='replace').read()
s=re.sub(r'\\\\x([0-9a-fA-F]{2})',lambda m:chr(int(m.group(1),16)),s)
print(s)" file.js

# 3. 自带二进制真身识别
strings -n 6 <binary> | grep -iE 'aria2|yt-dlp|ffmpeg|deno|N_m3u8DL'
#   boost-core → aria2c（命中 aria2 帮助文本全文）
#   m3u8-dl    → N_m3u8DL-RE（命中 N_m3u8DL-RE.Common / MergeByFFmpeg / ParseHLSCustomKey）

# 4. 明文宝藏：extraResources/yt_dlp_bridge.py（1811 行未混淆）
```

---

## 7. 本次分析的边界

- 全程**静态只读**，未运行 DataTool，未读取用户 Cookie 值（只统计字段名）
- `public/dist/` 前端 38 MB 未逐行读（不做 UI 抄袭，只看路由与语言包还原功能面）
- 混淆代码只做了**字符串表还原 + 关键词上下文**，未做完整反混淆；结论均基于明文字符串与未混淆的 Python bridge
