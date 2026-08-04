# 视频下载站

粘贴链接即可下载主流平台视频的自托管小工具。后端 FastAPI + yt-dlp，前端零依赖（原生 HTML/CSS/JS）。

> **定位**：个人媒体备份与管理工具，仅供下载**自己拥有或已授权**的内容。详见下方「合规与商业化」。

## 快速开始

```bash
./run.sh                  # 默认 http://127.0.0.1:8000
./run.sh 8321             # 指定端口
PORT=9000 HOST=0.0.0.0 ./run.sh   # 用环境变量控制端口/监听地址（部署常用）
```

脚本会自动复用已有虚拟环境；没有时会创建 `.venv` 并安装 `requirements.txt`。

**依赖 ffmpeg**：合并高清视频的音视频流、提取 MP3 都需要它。
macOS `brew install ffmpeg`，Ubuntu `sudo apt install ffmpeg`。

## 功能

- 粘贴链接（支持直接粘贴带文案的分享内容，会自动提取其中的 URL）
- 自动识别平台、拉取标题/封面/时长/作者
- 清晰度可选：最佳画质、4K/2K/1080P/720P/480P/360P、仅音频 MP3，并给出预估体积
- 实时进度：SSE 推送百分比、已下载/总大小、速度、剩余时间；断流自动回退轮询
- 支持取消任务；完成后自动触发浏览器保存，也可手动点「保存文件」
- 桌面 / 移动端响应式，点击目标 ≥44px

## 支持的平台

白名单已覆盖 50+ 主流站（国内 + 海外），包括但不限于：

- **国内**：哔哩哔哩、抖音、快手、小红书、微博、西瓜视频、AcFun、腾讯视频、优酷、爱奇艺、芒果TV、搜狐视频、知乎、喜马拉雅、网易云音乐、梨视频、好看视频、皮皮虾、微视、虎牙直播、斗鱼直播、央视频 等
- **海外**：YouTube、TikTok、X/Twitter、Vimeo、Facebook、Instagram、Dailymotion、SoundCloud、Twitch、Rumble、Reddit、LinkedIn、Pinterest、VK、Odnoklassniki、Naver TV、KakaoTV、ニコニコ、TED、BBC、Rutube、Bandcamp、Mixcloud、Streamable、BitChute、Odysee、Weverse、LINE TV 等

**通用兜底**：不在白名单内、但属于合法 `http(s)` 域名的链接，也会交给 yt-dlp 尝试解析——yt-dlp 原生支持上千个站点，长尾站点大多能直接下。明显非视频的站点（如 google / github / baidu / 淘宝 等）会被直接拦截并提示「该站点暂不支持视频下载」。

可在浏览器首页实时查看当前支持的平台清单与数量（`/api/platforms`）。

> **已知限制**：爱奇艺等极少数站点的提取器受 yt-dlp 上游能力所限（反爬/加密），
> 即便直连也可能返回"无法找到视频"，属上游限制而非本工具缺陷；腾讯/优酷/芒果TV/搜狐/央视频等主流站均可正常下载。

## 合规与商业化

- **用途定位**：本工具定位为「个人媒体备份与管理」小工具，仅供下载**你自己拥有或已获授权**的内容（个人学习、备份留存）。请勿用于下载或传播未获授权的版权内容。
- **明确红线**：本工具**不提供、不支持破解会员 / 付费专享 / 地区限制**内容的手段（相关链接会明确提示「暂不支持」）；服务端 `downloads/` 仅作临时中转，**不长期留存**他人版权内容。
- **责任归属**：使用者须遵守各平台服务条款与所在地法律法规，违规用途的责任由使用者自行承担。
- **商业化方向**：本项目以 **开源** 形式发布；未来若提供订阅，仅围绕**合法媒体管理的增值能力**（格式转换、云盘集成、批量管理、API 额度等），绝不把盈利建立在「绕过付费墙」之上。

## 代理（访问海外站点）

代理按以下优先级自动决定，**无需手动配置即可用**：

1. 显式 `VDL_PROXY` 环境变量（手动覆盖，例如 `VDL_PROXY=http://127.0.0.1:7890 ./run.sh`）
  2. **macOS 系统代理**（自动读取 `scutil --proxy`，即你在系统设置里开的代理客户端，如 Clash）
  3. 标准环境变量 `https_proxy` / `http_proxy`

> 国内站点（腾讯/优酷/爱奇艺/B站/抖音等）会**强制直连、不走代理**——它们本就面向国内网络，
> 走海外代理节点反而会因跨境/节点问题超时（实测优酷走代理必超时，直连正常）。
> 海外站点（YouTube/TikTok 等）才走代理。
> 注意：WorkBuddy 注入的 `http_proxy`（通常为 `127.0.0.1:57885`）实测无法访问海外站点，
> 代码已刻意**不**把它作为默认回退，而是优先用 macOS 系统代理，避免"代理开了却下不动"的假象。
> 手动指定 `VDL_PROXY` 时仍以你的指定为准（可强制让国内站也走代理）。

| 变量 | 说明 |
| --- | --- |
| `VDL_PROXY` | 访问海外站点用的代理（手动覆盖，优先级最高） |
| `VDL_COOKIES_FROM_BROWSER` | 从本地浏览器读取 Cookie，如 `chrome` / `edge` / `safari`；用于需要登录的内容 |

```bash
VDL_PROXY=http://127.0.0.1:7890 ./run.sh
```

## 目录结构

```
video-downloader/
├── run.sh                启动脚本（支持 PORT / HOST 环境变量）
├── requirements.txt      已验证的依赖版本（已钉版）
├── Dockerfile            容器镜像（含 ffmpeg）
├── Procfile              Render / Heroku / Railway 启动命令
├── runtime.txt           Heroku Python 版本
├── .gitignore
├── server/
│   ├── app.py            FastAPI 路由与生命周期
│   ├── platforms.py      链接校验 + 平台识别
│   ├── downloader.py     yt-dlp 封装：解析 / 清晰度 / 下载 / 进度
│   └── tasks.py          任务状态仓库（线程安全，内存态）
├── web/
│   ├── index.html
│   ├── styles.css
│   └── app.js
└── downloads/            下载产物，按任务 ID 分目录，1 小时后自动清理（.gitkeep 占位）
```

## 部署到线上（做成可访问的网站）

本项目是**带后端**的应用（解析 / 下载依赖服务端运行 yt-dlp + ffmpeg），
需要能跑 Python + 安装 ffmpeg 的托管环境，不能只部署静态前端。

### 方式一：Docker（任意支持容器的平台：Render / Railway / Fly.io / 自有服务器）

```bash
docker build -t video-downloader .
docker run -d -p 8000:8000 -e PORT=8000 video-downloader
# 随后访问 http://<你的域名或IP>:8000
```

### 方式二：Render / Railway / Heroku（一键）

仓库根目录已备好 `Procfile` 与 `runtime.txt`，直接用 Git 连接仓库即可：

- **Build Command**：`pip install -r requirements.txt`（平台通常会自动执行）
- **Start Command**：`Procfile` 已写 `uvicorn app:app --app-dir server --host 0.0.0.0 --port $PORT`
- **需额外安装 ffmpeg**：Render 可在 Build 脚本里加 `apt-get install -y ffmpeg`；
  Railway 用 nixpacks 时需在 `nixpacks.toml` 声明 ffmpeg。缺少 ffmpeg 会导致高清视频无法合并音轨。

### 上线前注意

- 默认监听地址由 `HOST` 环境变量控制，部署平台通常需 `0.0.0.0` + 平台注入的 `PORT`。
- 建议加一层反向代理（Nginx / Caddy 或平台自带的），启用 HTTPS 并限制上传/下载体积，避免被滥用。
- 任务状态在内存中、成品文件 1 小时后自动清理；多实例部署时各实例状态不共享（适合小规模自用）。
- 跨国下载（YouTube 等）所需的代理，见上方「代理」一节，通过 `VDL_PROXY` 在部署环境中配置。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/platforms` | 支持的平台清单 |
| POST | `/api/resolve` | `{url}` → 视频信息 + 清晰度选项 |
| POST | `/api/download` | `{url, quality}` → `{task_id}` |
| GET | `/api/tasks/{id}` | 任务状态（轮询兜底） |
| GET | `/api/tasks/{id}/events` | SSE 实时进度 |
| GET | `/api/tasks/{id}/file` | 下载成品文件 |
| DELETE | `/api/tasks/{id}` | 取消任务并清理临时文件 |

错误响应统一为 `{"error": "面向用户的说明", "hint": "补充建议"}`，HTTP 状态码：
400 链接非法 / 415 平台不支持 / 404 任务不存在 / 409 文件未就绪 / 504 解析超时。

## 已知限制

- 默认监听 `127.0.0.1`，仅本机可访问。局域网 / 线上部署通过 `HOST=0.0.0.0`（或部署平台注入）开放，并请加访问控制与 HTTPS。
- 任务状态存在内存中，重启即清空；成品文件保留 1 小时后自动删除。
- 直播流、DRM 加密内容不支持。
- 部分平台的高清晰度需要登录态，请配合 `VDL_COOKIES_FROM_BROWSER` 使用。

## 合规提示

本项目仅供个人学习与内容备份。请遵守目标平台的服务条款与著作权法律，
不要下载、传播未获授权的内容。
