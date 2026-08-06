# 视频下载站.

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
- **格式转换订阅开关**：开源默认**不开启订阅墙**，所有人免费无限使用格式转换。部署者设 `VDL_CONVERT_REQUIRE_SUB=true` 并填 `VDL_CONVERT_SUB_KEY` 后，进入「订阅墙」模式——免费用户按客户端 IP **每日限 `VDL_CONVERT_FREE_DAILY` 次**（默认 3），超出返回提示引导订阅；请求头携带正确 `X-Subscription-Key` 的订阅用户不受限。密钥由部署者生成分发，前端在右上角「🔓 订阅解锁」填入后自动携带，切换设备需重新输入。详见下方环境变量表。
- **下载订阅开关（freemium）**：开源默认免费无限下载。部署者额外设 `VDL_DOWNLOAD_REQUIRE_SUB=true` 后，免费用户按 IP **每日限 `VDL_DOWNLOAD_FREE_DAILY` 次**（默认 10，核心功能给得比转换宽松）创建下载任务，超出返回 `402` 引导订阅；持有 `VDL_CONVERT_SUB_KEY` 的订阅用户不受限。同一把订阅密钥通用于转换与下载（一个订阅解锁全部增值能力）。云盘集成、批量管理等后续增值能力复用同一套开关与限流桶，接好后自动纳入订阅墙。
- **许可证**：本项目以 [MIT](LICENSE) 许可证开源——任何人可自由使用、复制、修改、再分发（含商用），只需保留版权与许可声明。

## 代理（国内外分流）

代理按**目标站点所在地区分流**，两条线互不干扰——国内代理出不去海外，海外代理进不来国内，
所以绝不能用同一个变量兜住两边。

**海外站点**（YouTube / TikTok / X 等）按此优先级：

1. `VDL_PROXY`（手动覆盖，优先级最高）
2. **macOS 系统代理**（自动读取 `scutil --proxy`，即系统设置里开的 Clash 等客户端）
3. 标准环境变量 `https_proxy` / `http_proxy`

**国内站点**（B站 / 抖音 / 腾讯 / 优酷 / chrqj 等）：

1. `VDL_PROXY_CN`（国内出口回源代理）
2. 留空则**直连**

> - 服务跑在**本机或国内服务器**：`VDL_PROXY_CN` 留空即可，直连最快。
> - 服务跑在**海外**（Railway / Fly.io 等）：国内站会被地理围栏 403（实测 chrqj.com 在
>   Railway 欧洲节点必 403），**必须**把 `VDL_PROXY_CN` 指向一台国内机器的 HTTP 代理，
>   让请求从国内 IP 出去。注意方向是「海外服务器 → 国内出口」，普通翻墙 VPN（国内→海外）
>   方向相反，不能用。
> - WorkBuddy 注入的 `http_proxy`（通常 `127.0.0.1:57885`）实测不通海外，代码已刻意不把它
>   作为默认回退，避免"代理开了却下不动"的假象。

| 变量 | 说明 |
| --- | --- |
| `VDL_PROXY` | 海外站点用的代理，如 `http://127.0.0.1:7890` |
| `VDL_PROXY_CN` | 国内站点回源代理（仅海外部署需要），如 `http://user:pass@1.2.3.4:18888` |
| `VDL_COOKIES_FROM_BROWSER` | 从本地浏览器读取 Cookie，如 `chrome` / `edge` / `safari`；用于需要登录的内容 |

```bash
# 本机（国内）：海外走系统代理，国内直连
./run.sh

# 海外部署：两条线都配
VDL_PROXY=http://127.0.0.1:7890 \
VDL_PROXY_CN=http://user:pass@1.2.3.4:18888 ./run.sh
```

### 自建国内回源代理（海外部署必备）

在一台**国内**云服务器（腾讯云/阿里云轻量，约 ¥68/年起）上一条命令起代理：

```bash
docker run -d --name gost --restart always -p 18888:18888 \
  gogost/gost -L "http://用户名:强密码@:18888"
```

然后到云厂商控制台的**防火墙/安全组**放行 TCP `18888`，Railway 里加环境变量
`VDL_PROXY_CN=http://用户名:强密码@服务器公网IP:18888` 即可。

> ⚠️ **必须设账号密码**。裸奔的公开 HTTP 代理会在几小时内被全网扫到并滥用，
> 流量跑爆、IP 被封，甚至被拿去干违法的事，责任在机主。
> 代理端口用高位端口（非 80/443）不涉及备案。
> 该代理仅供本服务回源国内站点使用，不要对外提供跨境代理服务。

## 双节点部署（国内外都跑一份，免回源代理）

比回源代理更快的方案：国内、海外**各部署一个完整实例**，前端按链接域名把请求
自动发给离目标站点更近的那个，各自直连，不跨境。

```
用户粘贴链接 ──┬─ 国内站 → 国内节点 → 直连 B站/抖音/chrqj
               └─ 海外站 → 海外节点 → 直连 YouTube/X
```

两个节点用同一份代码，靠环境变量区分身份：

| 变量 | 说明 |
| --- | --- |
| `VDL_REGION` | 本节点区域：`cn` 或 `global`（默认 `global`） |
| `VDL_PEER_ENDPOINT` | 对端节点完整地址，如 `https://cn.example.com`；留空则退化为单节点 |
| `VDL_ALLOW_ORIGINS` | 允许跨域调本节点 API 的来源，逗号分隔；不填则默认只允许 `VDL_PEER_ENDPOINT` |
| `VDL_RATE_LIMIT_PER_HOUR` | 每 IP 每小时下载 / 解析次数上限，默认 `30`（`0`=不限） |
| `VDL_MAX_FILE_MB` | 单文件下载体积上限（MB），默认 `2048`（`0`=不限），防磁盘 / 带宽被撑爆 |
| `VDL_ADS_ENABLED` | 广告位开关，默认 `false`（关闭）。下载站属广告平台高风险类目，默认不挂广告；流量稳定并确定合规广告源后再设为 `true` 显示广告位 |
| `VDL_CONVERT_REQUIRE_SUB` | 格式转换订阅墙开关，默认 `false`。与 `VDL_CONVERT_SUB_KEY` 同时设置后才生效 |
| `VDL_CONVERT_SUB_KEY` | **订阅主密钥**（格式转换、下载、云盘集成等全部增值能力共用同一把）。请求头 `X-Subscription-Key` 与之相等即视为已订阅（不限次） |
| `VDL_CONVERT_FREE_DAILY` | 免费用户每日格式转换次数上限（按 IP 计），默认 `3`；仅订阅墙开启时有效 |
| `VDL_DOWNLOAD_REQUIRE_SUB` | 下载订阅墙开关，默认 `false`。与 `VDL_CONVERT_SUB_KEY` 同时设置后才生效；开启后免费用户每日下载限次 |
| `VDL_DOWNLOAD_FREE_DAILY` | 免费用户每日下载任务数上限（按 IP 计），默认 `10`；仅下载订阅墙开启时有效 |

```bash
# 国内节点
VDL_REGION=cn VDL_PEER_ENDPOINT=https://global.example.com ./run.sh

# 海外节点
VDL_REGION=global VDL_PEER_ENDPOINT=https://cn.example.com ./run.sh
```

前端会在输入框下方显示当前走哪条线路，自动判断不准时可手动切换。
任务创建后会锁定在所属节点，进度查询、取消、取文件都打到同一个节点，不会串。

> **注意**：海外节点抓完的文件仍要跨境传回国内用户，这段快不了。
> 双节点解决的是「国内站下载」和「解析速度」，不是「海外文件回传」。

### 公开部署（对不特定用户开放）的额外注意

如果这个实例要给陌生人用，先想清楚下面几条：

- **国内节点不要直接对外**。域名对公众提供服务需 ICP 备案，「视频下载」类目
  容易被驳；且版权投诉会直接发到云厂商，服务器可能被直接停机。
  更稳的做法是国内机器只做 `VDL_PROXY_CN` 回源代理（不跑 web、不暴露端口给公网）。
- **必开限流**。设 `VDL_RATE_LIMIT_PER_HOUR`，否则实例会被脚本当免费下载器薅，
  轻量服务器 3~5 Mbps 带宽两人同时下就打满，超额流量按 GB 计费。
  （默认已内置 30 次/小时基础限流，可按需调高 / 调低。）
- **SSRF 已内置防护**。解析 / 下载入口会校验链接主机名，拒绝指向内网、环回、链路本地
  （含云元数据 `169.254.169.254`）、私有网段的地址，普通部署无需额外配置。
- **广告位默认关闭**。前端已预留广告容器（`index.html` 的 `#adsSlot`），由后端
  `VDL_ADS_ENABLED` 控制显隐，**默认 `false` 不展示**——下载站类目在广告平台属高风险，
  贸然挂广告极易被封号；待流量稳定、确定合规广告源后再开，且接广告与订阅增值并行（订阅为主）。
- **公开实例定位成"体验版"**。重度用户引导去自托管（一条 `docker run` 的事），
  这既是开源项目的正常用法，也让你对外提供的始终是「有限速的工具」而非「高速下载服务」。
- **加 `robots.txt` 禁止索引**，不做 SEO 引流。
- 只支持公开可访问、无 DRM、无付费墙的内容——这是底线，别碰。

### 自动解说（增值功能）· 独立解说 Worker 部署

「生成解说成片」吃 CPU（whisper 转写 + ffmpeg 渲染），**不应和下载服务挤同一台机器**。
推荐把解说管线部署成独立 HTTP worker（强机），主站通过 HTTP 调用，互不干扰。

**机器配置建议（海外强机，免备案、YouTube 可达）**
- CPU 4 核以上（转写是 CPU 密集；用 GPU 更快但非必需）
- 内存 8 GB+（whisper base + ffmpeg 中间文件）
- 磁盘 20 GB+（模型 + 中间文件 + 成片）
- 系统：Ubuntu 22.04 / Debian 12（apt 直接装 ffmpeg 与中文字体最方便）

**解说管线仓库**（本仓库不含，需单独部署）：含 `process.py`（一键管线）、
`commentary_worker.py`（独立 HTTP 服务）、`requirements.txt`、`start_worker.sh`、`Dockerfile`。
部署前请先把它纳入你自己的版本库或打包传到目标机器。

**部署解说 worker（二选一）**

方式 A · 脚本（Debian/Ubuntu）：
```bash
apt-get install -y ffmpeg fonts-noto-cjk            # 系统级依赖：ffmpeg + 中文字幕字体
git clone <你的 commentary-pipeline 仓库> /opt/commentary-pipeline
cd /opt/commentary-pipeline
COMMENTARY_BASE=/opt/commentary-pipeline WORKER_PORT=8100 ./start_worker.sh
```

方式 B · Docker：
```bash
docker build -t commentary-worker .
docker run -d -p 8100:8100 -e WORKER_MAX_CONCURRENCY=2 commentary-worker
```

> **安全**：worker 端口（8100）只对内网 / 主站开放，不要直接暴露公网。
> 主站用内网地址（如 `http://10.x.x.x:8100` 或同机 `http://127.0.0.1:8100`）调用。

**主站开启解说（HTTP 模式）**，在 Railway / 主站环境变量加：

| 变量 | 说明 |
| --- | --- |
| `VDL_COMMENTARY_ENABLED` | `true` 启用解说功能 |
| `VDL_COMMENTARY_MODE` | `http`（独立 worker，推荐）或 `local`（同机 subprocess，仅自托管测试用） |
| `VDL_COMMENTARY_ENDPOINT` | worker 地址，如 `http://127.0.0.1:8100`（mode=http 必填） |
| `VDL_COMMENTARY_VOICE` | 默认配音嗓音，如 `zh-CN-YunxiNeural` |
| `VDL_COMMENTARY_TIMEOUT` | 单任务超时秒，默认 `1800` |
| `VDL_COMMENTARY_LOCAL_OUTPUT` | HTTP 模式成片落盘目录（主站本地），默认 `<仓库>/commentary_out` |

同机测试：主站与 worker 同一台机器时，`VDL_COMMENTARY_MODE=http` +
`VDL_COMMENTARY_ENDPOINT=http://127.0.0.1:8100` 即可；也可设 `VDL_COMMENTARY_MODE=local` +
`VDL_COMMENTARY_DIR` 直接同机 subprocess（最简单，但会挤占下载 CPU）。

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
- 代理配置见上方「代理（国内外分流）」一节：海外部署时 `VDL_PROXY` 管海外站，
  `VDL_PROXY_CN` 管国内站回源，两个都要配才能"国内外网站都能用"。

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
| POST | `/api/commentary` | `{task_id, vertical, voice}` → `{job_id}`（需先启用并配置解说管线） |
| GET | `/api/commentary/{job_id}` | 解说任务状态（轮询） |
| GET | `/api/commentary/{job_id}/file` | 下载解说成片 |

错误响应统一为 `{"error": "面向用户的说明", "hint": "补充建议"}`，HTTP 状态码：
400 链接非法 / 415 平台不支持 / 404 任务不存在 / 409 文件未就绪 / 504 解析超时。

## 已知限制

- 默认监听 `127.0.0.1`，仅本机可访问。局域网 / 线上部署通过 `HOST=0.0.0.0`（或部署平台注入）开放，并请加访问控制与 HTTPS。
- 任务状态存在内存中，重启即清空；成品文件保留 1 小时后自动删除。
- 直播流、DRM 加密内容不支持。
- 部分平台的高清晰度需要登录态，请配合 `VDL_COOKIES_FROM_BROWSER` 使用。

## 自动解说（增值功能，可选）

下载完一个视频后，可一键「生成解说成片」——后端把视频喂给你自己独立的
commentary-pipeline（转写 → 配音 → 出片，输入 `input/`、跑 `process.py`、输出 `output/` 的本地脚本管线），
跑完回传成片。本服务只做**文件桥接，不重写解说逻辑**，解说管线在你自己的机器上独立运行。

> 适合做订阅增值点：算力可控（本地 whisper-base + 免费 edge-tts，无 LLM/TTS API 费），
> 但渲染吃 CPU，建议放在独立 worker，别和下载服务抢资源。

**启用方式**（默认关闭，开源版不显示该入口）：

| 环境变量 | 说明 |
| --- | --- |
| `VDL_COMMENTARY_ENABLED` | 设为 `true` 才启用 |
| `VDL_COMMENTARY_DIR` | commentary-pipeline 项目根目录（需含 `process.py` 及 `input/`、`output/`） |
| `VDL_COMMENTARY_PYTHON` | 跑 `process.py` 的 Python 解释器（需装 faster_whisper 等依赖），默认同进程解释器 |
| `VDL_COMMENTARY_VOICE` | 默认配音嗓音，默认 `zh-CN-YunxiNeural` |
| `VDL_COMMENTARY_TIMEOUT` | 单任务超时秒数，默认 `1800` |

前端在下载完成的任务卡片上自动出现「生成解说成片」按钮（仅当本节点启用该功能）。

> **版权边界**：下载他人视频 + 自动解说二创并公开发布，属于你自己的发布行为，
> 需注意各平台（YouTube/B站）的二创政策；本工具不破解付费墙、不替代原作者的授权。

## 合规提示

本项目仅供个人学习与内容备份。请遵守目标平台的服务条款与著作权法律，
不要下载、传播未获授权的内容。
