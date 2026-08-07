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

## 4. 后续（阶段3 及以后，仅列思路）
- 字幕提取/翻译/烧录；格式片段增强（GIF/裁剪/超分）；抽帧封面/抽音频铃声；一键归档网盘；本地媒体库加密。
- 详见 MEMORY.md 桌面其他功能思路。
