# VideoDownloader 桌面版

把整套「视频下载站」打包成**双击即用的桌面 App**，用户无需安装 Python、ffmpeg 或任何依赖。

## 用户体验
1. 双击 `VideoDownloader.app`（macOS）/ `VideoDownloader.exe`（Windows），或把 `.dmg` 里的 App 拖进「应用程序」
2. 自动打开浏览器到 `http://127.0.0.1:8321`（端口被占用会自动顺延到 8322…）
3. 粘贴视频链接 → 直接下载

**用户零配置**：抓视频用的是用户自己电脑的网络出口（家庭宽带 IP），因此 B站 / 抖音等国内站也能下，且不需要任何代理设置。
ffmpeg 已随包捆绑，下载/转码用的就是包内的 ffmpeg，用户机器无需预装。

## 退出方式
- **macOS**：Dock 栏右键 App 图标 →「退出」；或 `Cmd+Q`。
- **Windows**：系统托盘右键 → 退出。
- 启动时会弹一次系统通知，告知访问地址。

## 原理
- 启动器（`desktop_launcher.py`）把 `server/`（FastAPI 后端）+ `web/`（前端）+ 捆绑 ffmpeg + yt-dlp 全部打进一个 App。
- 启动流程：定位资源 → 自动挑空闲端口 → 起本地 uvicorn → 自动开浏览器 + 系统通知。
- `server/app.py` 支持「冻结模式」：打包后从 `Contents/Resources`（macOS）读取前端，下载产物写入 `~/Downloads/VideoDownloader`（按任务分子目录）。
- 启动器优先用**捆绑的 ffmpeg**（`VDL_FFMPEG_BIN` 指向包内二进制），用户机器没装 ffmpeg 也能正常合并/转码。

## 构建（开发者 / 发布用）
> 跨平台编译做不到，需在对应系统上构建。构建脚本会自建独立 venv 并安装依赖，不影响系统环境。

### macOS
```bash
bash desktop/build_mac.sh
# 产物：dist/VideoDownloader.app（双击即用）
#       dist/VideoDownloader.dmg（分发用，拖到应用程序即可）
# 自动生成圆角图标并打包进 App；ffmpeg 自动捆绑
```

### Windows
在 Windows（Git Bash / WSL）中：
```bash
bash desktop/build_win.sh
# 产物：dist/VideoDownloader/VideoDownloader.exe（双击即用）
# 需构建机已装 ffmpeg 且可在 PATH 找到，或把 ffmpeg.exe 放仓库根（脚本会拷进 bin/）
```

## 与线上版的关系
- **线上版（Railway）**：面向海外站（YouTube 等），零安装但国内站受机房 IP 限制。
- **桌面版**：面向「要下国内站且不愿配环境」的用户，用各自家庭宽带 IP，零月费、零代理。
- 两者代码同源，桌面版只是把服务端跑在了用户本机。

## 已验证
- macOS `.app` 端到端：双击启动 → 前端 200 → 真实下载 B站视频（带用户 cookie）→ ffmpeg 合并生成有效 mp4（含音视频双流）。
- 国内站走用户本机宽带 IP，不经过任何服务器/代理。

## 合规提醒
桌面版下载用的是用户自己的网络与（可选）登录 cookie，属于个人媒体备份范畴，符合项目红线（不破解会员、不绕过付费墙）。请勿用于大规模抓取他人版权内容。
