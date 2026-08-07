# 桌面版功能规划（DESKTOP_FEATURES）

> 定位（见 MEMORY.md「产品定位决策」）：桌面版 = 万能下载器（yt-dlp + 后续 libtorrent 种子）+ 视频渲染/解说管线 + 其他功能。
> 本文件把"其他功能"第一层（最该先做、与下载内核耦合最紧、成本最低）展开为具体需求与技术方案。
> 落地节奏：阶段1 平台下载（已完成）→ **阶段2 媒体库管理（本次实现）+ 批量队列 + 订阅监控** → 阶段3 渲染/解说管线。

---

## 0. 设计约束（必须遵守）

- 桌面版后端 = FastAPI 单进程，前端 = 零依赖原生 HTML/JS（同 `web/`），打包后跑在 `127.0.0.1:随机端口`。
- 下载产物目录：桌面版 `~/Downloads/VideoDownloader/<task_id>/<标题>.<ext>`；转换产物 `~/Downloads/VideoDownloader/conversions/`。
- 任务状态**只存内存，重启即丢**；所以"媒体库"必须**以磁盘文件为准**（扫目录），不能依赖内存任务表。
- 前端所有动态文本用 `textContent` 写入（防 XSS，沿用 app.js 约定）。
- 媒体库功能仅在桌面版（frozen）或显式 `VDL_LIBRARY_ENABLED=true` 时暴露给前端；网页版（Railway）文件 TTL 1 小时、目录临时，默认不开。

---

## 1. 媒体库管理（本次实现）

### 需求
- 用户下完视频/音频后，能在同一个 App 里看到**已下载内容的缩略图墙**，按时间倒序排列。
- 支持：搜索（标题/作者/文件名）、按平台筛选、按类型（视频/音频）筛选。
- 点开任意一项可**本地播放/预览**、**保存到本机**（再次下载到默认下载目录）、**删除**（从磁盘移除，含元数据侧车与缓存缩略图）。
- 展示元信息：标题、平台、作者、时长、大小、下载日期。

### 数据模型
- 每个下载文件旁边写一个**元数据侧车** `<标题>.vdlmeta.json`（下载完成时由 `downloader.run_download` 写入）：
  `{ title, platform, uploader, duration, source_url, thumbnail, completed_at }`。
- 媒体库条目 id = 相对路径的 **base64urlsafe 编码**（无斜杠，URL 安全，可逆；避免重启丢映射）。
- 缩略图**懒生成**：首次请求 `/api/library/thumb/{id}` 时用捆绑的 ffmpeg 抽首帧到 `~/Downloads/VideoDownloader/.thumbs/<hash>.jpg` 并缓存；音频无缩略图（前端显示音符图标）。

### 后端（新增 `server/library.py` + `app.py` 路由）
- `scan_library(download_dir)` → 递归扫媒体扩展名；跳过 `.thumbs/`、`conversions/` 内的 `.part` 临时文件、下载进行中（存在 `.part` 兄弟文件）的项；读取侧车补全元信息。
- `GET  /api/library?q=&platform=&kind=` → `{ items, total }`，按 mtime 倒序。
- `GET  /api/library/file/{id}` → `FileResponse`（播放+保存用）。
- `GET  /api/library/thumb/{id}` → 懒生成缩略图 `image/jpeg`；音频或生成失败返回 404（前端回退图标）。
- `DELETE /api/library/{id}` → 删文件 + 侧车 + 缩略图，返回 204。
- 安全：`id` 解码后必须 resolve 在 `DOWNLOAD_DIR` 内，否则 404（防目录穿越）。
- `/api/nodes` 增加 `library.enabled`。

### 前端（`index.html` + `app.js` + `styles.css`）
- 顶部增加 Tab 导航：**下载 / 媒体库**（仅 `library.enabled` 时显示 Tab）。
- 媒体库视图：搜索框 + 平台下拉 + 类型下拉 + 卡片网格（缩略图 / 标题 / 平台徽标 / 大小 / 日期）。
- 点卡片弹出 `<dialog>`：HTML5 `<video>`/`<audio>` 播放器 + 元信息 + 「保存到本机」「删除」按钮。

---

## 2. 批量任务队列（待实现，复用现有框架）

### 需求
- 用户贴多个链接时**已支持逐条创建任务**（`runBatch`）；本功能补上：并发上限、断点续传、失败自动重试、限速/闲时下载、全局进度汇总。
- 任务列表支持：拖拽排序优先级、暂停/继续单条、一键全部重试失败项。

### 技术方案
- 后端 `TaskStore` 增 `priority`/`paused` 字段；新增调度器（现有 `MAX_CONCURRENT_DOWNLOADS=3` 已是信号量式并发，升级为带优先级的线程池）。
- 断点续传：yt-dlp 原生支持 `-c/--continue`（默认开），`.part` 续传；任务被取消后重下可复用 `.part`。
- 失败重试：`run_download` 包一层指数退避（最多 N 次），区分"可重试网络错"与"硬错（受限/不支持）"。
- 前端：任务卡片加「暂停/继续/重试」按钮；顶部加汇总条（X 进行中 / Y 完成 / Z 失败）。

---

## 3. 订阅监控（待实现）

### 需求
- 用户填入 UP 主 / 频道主页，App 定期（如每小时）**自动检测新视频并后台下载**到媒体库。
- 支持：频道列表管理、每个频道选清晰度/格式、只下最新 N 条或全量增量、跳过已下。

### 技术方案
- 复用 yt-dlp 的 playlist/Channel 提取：对频道 URL 设 `noplaylist=False`，取 entries 的 `id`/`url`；用本地 `seen.json`（频道 → 已下视频 id 集合）做增量。
- 后台调度：`threading.Timer` 或独立线程循环；仅桌面版（常驻进程）有意义。
- 新视频直接走现有 `run_download`，落盘到媒体库 → 与功能 1 天然打通。
- 前端：媒体库页加「订阅」Tab，管理频道与抓取状态。

---

## 2.5 格式 / 片段增强（已实现）

### 需求
- 对已下载的媒体（视频/音频）做本地 ffmpeg 加工，无需重新下载：转音频、生成 GIF、时间裁剪、画面裁剪、压缩、放大/轻量超分。
- 产物自动落回媒体库（带元信息侧车），用户可立即在「媒体库」看到/播放/管理/删除。

### 技术方案
- 新增 `server/ffmpeg_tools.py`（纯 ffmpeg，无外部依赖）：`extract_audio / make_gif / trim_video / crop_video / compress_video / upscale_video`，统一 `_unique_out`（中文后缀安全，不用 `Path.with_suffix`）+ `_write_sidecar`。
- 新增 `POST /api/process/run` + `GET /api/process/{job_id}`（基于 lib_id，复用下载线程池 `executor` 异步跑）；产物写源目录并写侧车，`encode_id` 回传新 lib_id。
- 开关同媒体库：`VDL_LIBRARY_ENABLED=true` 或桌面 frozen 才暴露（前端「🛠 处理」按钮 + 媒体库弹窗加工面板）。
- 前端 `index.html`/`app.js`/`styles.css`：媒体库弹窗加「🛠 处理」按钮与加工面板；按媒体类型（video/audio/image）过滤可用操作；动态参数表单；提交后轮询进度，完成自动关闭弹窗并刷新媒体库。
- 媒体库识别增强：`library.py` 的 `MEDIA_EXTS` 增加 `.gif/.webp`（kind=image），前端 `openLibModal` 增加 image 分支用 `<img>` 预览。
- 「超分」说明：实为 lanczos 放大 + unsharp 锐化（非 AI 超分，无模型依赖），UI 标注「轻量超分」。
- 修复：字幕烧录 `burn_subtitle` 与 `ffmpeg_tools._unique_out` 的中文后缀被 `Path.with_suffix` 吞掉的 bug（改用字符串拼接）。
- 测试：ffmpeg 端到端（6 函数全过）+ TestClient 路由集成（op 校验 400 / 404 / 真实加工 completed / 产物入媒体库识别）。

## 2.6 抽帧封面 / 抽音频铃声（已实现）

### 需求
- **抽帧封面**：从视频任意时间点抽一张图做封面/配图（jpg/png/webp，可指定宽度）。
- **批量抽帧**：按固定间隔批量抽帧（做解说素材、逐帧分析），带帧数上限防止长视频抽出上万张。
- **预览图**：把视频均匀采样成一张九宫格拼图（contact sheet），一眼看完整片内容。
- **铃声**：从视频/音频截一段做手机铃声，带淡入淡出，支持 m4r（iPhone）/ m4a / mp3。

### 技术方案
- `ffmpeg_tools.py` 新增 4 个函数 + 1 个工具：
  - `snapshot(video, at, fmt, width)` → `<标题>.封面.jpg`；**seek 超出时长自动回退**（先试 at，再试 0，最后不带 `-ss`）。
  - `extract_frames(video, start, end, interval, limit, fmt, width)` → 输出到子目录 `<标题>.抽帧[.n]/frame_0001.jpg`，返回 `(目录, 帧数)`；`fps=1/interval` + `-frames:v limit` 封顶（默认 200，硬上限 2000）；部分成功也按实际产出算，零产出则删空目录返回 None。
  - `contact_sheet(video, rows, cols, width, duration)` → `<标题>.预览图.jpg`；用 `fps=n/时长` 均匀采样 + `tile=colsxrows`；时长优先取侧车 `duration`，缺失则 `probe_duration` 探测，再缺失退化为 `thumbnail=100`。
  - `make_ringtone(src, start, duration, fmt, fade)` → `<标题>.铃声.m4r`；`afade` 淡入淡出；**m4r 必须显式 `-f ipod`**，否则 ffmpeg 不认该扩展名。
  - `probe_duration(video)` → 不依赖 ffprobe，直接正则解析 `ffmpeg -i` 的 stderr `Duration:` 行。
- `app.py`：op 白名单加 `frame / frames / sheet / ringtone`；`frames` 产物是**目录**不是文件，单独收尾（`is_dir=True` + `count`，不写侧车、不编 lib_id），`/api/process/{job_id}` 增返 `count` / `is_dir`。
- `library.py`：
  - `AUDIO_EXTS` 加 `.m4r`；`IMAGE_EXTS` 加 `.jpg/.jpeg/.png`（封面、预览图能进媒体库）。
  - 新增 `FRAMES_DIR_MARK = ".抽帧"`，扫描时跳过抽帧子目录内的所有帧图，**避免上百张帧刷屏媒体库**。
  - 缩略图生成修复：**静态图片带任何 `-ss`（含 `-ss 0`）都会把唯一一帧 seek 掉、输出为空**；改为图片不加 `-ss`，视频先试 `-ss 1` 失败再回退无 seek。
- 前端 `app.js`：`PROCESS_OPS` 加 4 项（含 iPhone 铃声 >40 秒的提示）；`frames` 完成后不关弹窗、不刷媒体库，直接提示「已抽 N 帧 → 下载目录/xxx.抽帧/」。

### 已知取舍
- 抽帧目录不进媒体库列表，因此也**无法在 App 内删除**，需用户去下载目录手动清理（避免误删整目录的风险）。

## 2.7 时效自动清理（已实现）

### 需求
长期用下来磁盘会无声爆掉，尤其是「批量抽帧」一次上百张图、中断下载留下的 `.part` 碎片 —— 用户既看不见也删不掉。需要按保留期自动清，但**媒体本体是用户资产，绝不能默认删**。

### 分档策略（核心）
按「可再生程度」分五档，各自独立开关与保留期：

| 档位 | 默认 | 保留期 | 删除方式 |
|---|---|---|---|
| 临时碎片 `.part`/`.ytdl` | 开 | 2 天 | 直接删（进回收站反而白占空间） |
| 批量抽帧目录 `xxx.抽帧/` | 开 | 7 天 | 直接删（可随时重抽） |
| 缩略图缓存 `.thumbs/` | 开 | 30 天 | 直接删（下次浏览自动重建） |
| **媒体本体** | **关** | 30 天 | **强制进系统回收站** |
| **磁盘容量上限** | **关** | 20 GB | **最旧优先，进回收站** |

### 安全设计（改代码前必读）
1. **媒体删除强制走回收站**：回收站不可用时**整档跳过并记录原因**，绝不静默硬删。后端 `POST /api/retention/config` 在回收站不可用时直接拒绝开启媒体/容量两档（400）。
2. **穿越防护**：每个候选路径 `resolve()` 后必须仍在 `download_dir` 内，否则跳过（防符号链接把清理引到目录外）。
3. **保护名单**：`.subscriptions.json` / `.retention.json` 等配置文件永不删；`download_dir` 本身永不删。
4. **dry-run 优先**：`scan()` 只算不删，前端必须先「预览将清理什么」才能点「立即清理」，且危险档二次 confirm。
5. **时间基准用 mtime**；目录取「目录内最新文件的 mtime」，避免误删刚生成的抽帧目录。
6. **伴生文件联动**：删媒体时 `.vdlmeta.json` / `.srt` 等同名伴生文件一起走，不留孤儿。

### 回收站实现（踩过坑，别退回去）
macOS 上 `tell application "Finder" to delete` 需要「自动化 → Finder」授权，**未授权报 -10004，而 `which osascript` 依然为 True** —— 典型假阳性。现为四级兜底：

`PyObjC NSFileManager.trashItemAtURL` → `trash` CLI → Finder AppleScript → **手动移入 `~/.Trash`（零依赖，永远可用）**

`trash_available()` 检测的是「回收站目录真的可写」，不是查命令存在。跨卷时走 `<卷>/.Trashes/<uid>`，避免把几十 GB 复制回系统盘。Linux 手动兜底会补写 `.trashinfo` 以支持「还原」。

### 技术方案
- 新增 `server/retention.py`：`RetentionConfig`（dataclass）+ `RetentionStore`（JSON 持久化到 `.retention.json`，RLock + 损坏降级）+ `scan()` / `run()` + 跨平台回收站 + `disk_usage()` / `human_size()`。
- `app.py`：`GET/POST /api/retention/config`、`POST /api/retention/scan`、`POST /api/retention/run`；`_retention_watchdog` 后台守护线程按 `interval_hours` 周期跑；`/api/nodes` 暴露 `retention.enabled` / `trash_available`。
- 开关同媒体库：桌面 frozen 或 `VDL_RETENTION_ENABLED=true` 才暴露。
- 前端：媒体库头部「🧹 自动清理」按钮 + 独立弹窗（磁盘占用条、总开关+检查周期、五档规则、预览清单、危险档二次确认）。

## 2.8 一键归档网盘（已实现）

### 需求
本机下完 / 加工完的文件，按规则**批量 / 定时上传到用户自己的网盘**（WebDAV / 百度网盘），把本地媒体库兜底备份到云端。只搬用户自己的文件，服务端不留存、不中转。

### 设计要点（核心）
- **与 `clouddrive.py` 解耦**：`clouddrive.py` 负责「怎么传」（WebDAV / 百度网盘协议），`archive.py` 负责「传什么 / 传到哪 / 传过没有」（选取规则、路径模板、去重记录、自动巡检）。归档层独立可单测。
- **去重指纹** `fingerprint = "{rel}|{size}|{mtime}"`：文件没变不重传；被重新加工（大小/时间变了）会自动再传。
- **凭据安全**：明文凭据单独存本机配置文件 `~/.video-downloader/archive.json`（**不放下载目录**，避免把下载目录同步到网盘时泄露密码），权限 `0600`；接口返回一律脱敏（首尾各 2 字符），前端改密码留空则沿用旧值。
- **路径模板**：`VideoDownloader/{platform}/{date}/{filename}`，占位符 `{filename}{title}{ext}{platform}{uploader}{kind}{date}{year}{month}`；`..` 段直接丢弃、非法字符清洗，防穿越与怪目录。
- **归档后删本地**：可选且默认关，开启也强制走系统回收站（复用 `retention` 四级兜底）；回收站不可用则只归档不删，**绝不静默硬删用户资产**。
- **单文件失败不中断整批**；支持取消（`should_stop`）；前端必须 `scan` 预览再 `run`。

### 安全设计
- WebDAV 地址用 **`_assert_archive_url`**（区别于下载的 `_assert_safe_url`）：桌面版里用户指向自己的 NAS/网盘是正当场景，故**放行私网 / 环回 / 链路本地**（如 `https://192.168.1.100:5006/dav`、`my-nas.local`），只拦截非 `http(s)` 与缺主机名。这是特意与下载 SSRF 护栏区分的——归档是用户显式填自己的目标。
- 归档记录按指纹去重；配置损坏降级默认；守护线程只在校验 `auto_enabled and has_creds` 后才跑。
- 跨平台回站兜底与 `retention.py` 同源：回收站不可用 → 删除动作被禁用并提示，绝不硬删。

### 技术方案
- 新增 `server/archive.py`：`ArchiveConfig`(dataclass) + `ArchiveStore`(JSON 持久化，RLock+损坏降级+0600) + `fingerprint` / `render_dest` / `pending_items` / `run_archive` / `human_size`。
- `app.py`：`GET/POST /api/archive/config`（含凭据脱敏返回、set_creds 写入、delete_after 无回收站拒 400）、`POST /api/archive/scan`（只算不传，最多 200 条预览）、`POST /api/archive/run`（按 lib_ids 过滤、线程池提交 `_run_archive_job`）、`GET /api/archive/status/{job_id}`、`POST /api/archive/cancel/{job_id}`、`POST /api/archive/forget` 清记录；`_archive_watchdog` 静默 180s 后按 interval 周期跑（仅 `auto_enabled and has_creds`）。
- 开关同媒体库：桌面 frozen 或 `VDL_ARCHIVE_ENABLED=true` 才暴露；`/api/nodes` 暴露 `archive.enabled` / `baidu_available` / `configured`。
- 前端：媒体库头部「☁️ 归档网盘」按钮 + 独立弹窗（provider 单选、WebDAV/百度凭据、路径模板+占位符提示、类型/静置/上限/删本地开关、定时自动开关、扫描预览清单+勾选、进度轮询、最近归档记录、忘记记录）。
- 测试覆盖：`/tmp/test_archive.py`（12 组单测：模板渲染、`..` 穿越、筛选、去重、凭据脱敏/留空沿用/0600、正常上传、单条失败继续、删本地回收站不可用只传不删、回收站可用本体+侧车一起走、取消、human_size）；`/tmp/test_archive_routes.py`（TestClient 路由集成：nodes 开关、config 脱敏/私网 WebDAV 放行/非法 scheme 拒/删本地回收站不可用拒、scan、run 去重、cancel、forget、关闭 404）。

## 4. 后续（阶段3 及以后，仅列思路）
- 本地媒体库加密。
- 详见 MEMORY.md 桌面其他功能思路。
