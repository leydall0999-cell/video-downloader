# 视频解说（Commentary）功能规划备忘

> 本文记录 video-downloader 的视频解说增值方向的架构选型与成本。代码**已落地**（后端桥接 + 前端入口 + 独立 worker），但默认 `VDL_COMMENTARY_ENABLED=false` 处于暂停/可选状态；启用方式与 A/B 选择见文末「实现状态」。

## 背景

- 视频下载 / 媒体备份是项目底座；**视频解说（自动成片）**是已规划的增值方向之一。
- 解说管线 = 素材 → 转写(ASR) → 解说词提炼(LLM) → 剪辑/压字幕(ffmpeg) → 旁白(TTS) → 成片。
- 当前 `server/app.py` 里解说 worker 处于**暂停**状态（`VDL_COMMENTARY_ENABLED` 待设为 `false` 隐藏，避免半成品暴露）。
- 用户另有独立项目 `/Users/suixindelang/WorkBuddy/问问题/commentary-pipeline`，已在本机跑通过"素材→转写→解说词→成片"的本地管线。

## 关键澄清：已购阿里云 VPS 不用于解说

- 阿里云 ECS 99 计划（2核2G / 3M 不限流量 / 国内节点）**专用于 `VDL_PROXY_CN` 回源国内站**（B站/抖音等），让海外部署的 Railway 实例能绕过地域封锁。
- 该机 **2核2G 算力太弱，跑不了 whisper 本地大模型转写**，不能复用为解说算力机。
- 即：这台 VPS 是"代理管子"，不是"算力机"，两者职责分离。

## 三方案对比

| 方案 | 算力在哪 | 转写(ASR) | 解说词(LLM) | 旁白(TTS) | 剪辑(ffmpeg) | 额外服务器费 | 适用阶段 |
|---|---|---|---|---|---|---|---|
| **A. 本机 Mac** | 用户 Mac | whisper 本地 | 云端 LLM API | 本地/云端 | 本机 ffmpeg | **0** | 先验证效果、小批量 |
| **B. Railway + API** | Railway 复用 | 云端 ASR API | 云端 LLM API | 云端 TTS API | Railway ffmpeg | Railway 付费档 ~$5/月 | 做成网站在线自动成片 |
| **C. 本地大模型算力机** | GPU/大内存服务器 | whisper 本地大模型 | 本地/云端 LLM | 本地 TTS | 服务器 ffmpeg | **另购**（一笔钱） | 隐私优先、不依赖外部 API |

### 方案 A —— 本机 Mac（推荐先试）
- 复用已有 `commentary-pipeline` 项目：本机跑 whisper 转写 + WorkBuddy 提炼解说词 + ffmpeg 成片。
- 下载好的视频从 Railway / 本地下载后拉回本机处理。
- **零额外服务器费用**，最省，适合先验证成片质量。

### 方案 B —— Railway + API（做成在线服务）
- Railway 上海外部署本就有 ffmpeg，解说管线只**调 API、不自己跑模型**：
  - ASR → OpenAI Whisper API / 国内大模型 ASR
  - 解说词 → LLM API
  - TTS → 云端 TTS API
- 常驻 worker 可能超出 Railway 免费档，需升付费档（约 $5/月）。
- **不另买 VPS**（已购那台只管代理回源）。

### 方案 C —— 本地大模型算力机（后话）
- 仅当要求"不依赖外部 API、隐私优先、本地大模型 + 本地 TTS"时才需要。
- 需另购 **GPU / 大内存服务器**，与已购 2核2G VPS 无关、不冲突也不复用。
- 初期不做。

## 结论 / 决策

1. 已购阿里云 VPS **只用于视频下载的国内站回源**，别想着复用它跑解说。
2. 解说功能先走 **A（本机）或 B（Railway + API）**，**不额外买服务器**。
3. 只有"重度本地模型"才需第二台算力机 —— 那是后话，当前不采购。
4. 落地顺序建议：先打通 VPS 回源（当前进行中）→ 再决定解说走 A 还是 B。

## 待办

- [x] 解说桥接代码落地：后端 `server/app.py`（local / http 双模式）+ 前端「生成解说成片」入口（`web/`）+ 独立 worker `commentary-pipeline/commentary_worker.py`（复用 `process.py`）。默认 `VDL_COMMENTARY_ENABLED=false` 暂停。
- [x] 测试覆盖：`tests/test_commentary_routes.py`（12 项，mock 掉 whisper/ffmpeg，覆盖禁用/缺配置/任务校验/创建/状态/下载全分支 + local 成片定位）。
- [ ] **部署时决定运行模式**（二者代码均已支持，无需二选一重写）：
  - 桌面版（本机跑 video-downloader）→ `VDL_COMMENTARY_MODE=local` + `VDL_COMMENTARY_DIR=<commentary-pipeline 路径>` + `VDL_COMMENTARY_PYTHON=<装了 faster_whisper 的解释器>`。
  - 在线版（Railway）→ `VDL_COMMENTARY_MODE=http` + `VDL_COMMENTARY_ENDPOINT=<本机 worker 的 tunnel 地址>` + `VDL_COMMENTARY_TOKEN`，算力在本机 Mac（零额外服务器费）。
- [ ] 真机端到端验证一次（dev 机 macOS 13 受限：whisper 模型需联网下载、且访问不了 Google/huggingface，暂只能靠测试保证逻辑正确）。

## 实现状态（2026-08-08 更新）

- 后端 `server/app.py`：`COMMENTARY_ENABLED` / `COMMENTARY_DIR` / `COMMENTARY_PYTHON` / `COMMENTARY_VOICE` / `COMMENTARY_TIMEOUT_SECONDS` / `COMMENTARY_MODE`(local|http) / `COMMENTARY_ENDPOINT` / `COMMENTARY_TOKEN` / `COMMENTARY_LOCAL_OUTPUT` 九个开关；路由 `POST /api/commentary`、`GET /api/commentary/{id}`、`GET /api/commentary/{id}/file`；`/api/nodes` 暴露 `commentary_enabled`。
- 前端：下载完成的任务 + 媒体库弹窗里的现成视频（非加密文件）均出现「生成解说成片」按钮（受 `node.commentaryEnabled` 控制），点击后轮询状态并在完成后提供成片下载。
- local 模式已修复：voice 为空时不传 `--voice`（避免 process.py 收到空串报错）。
- 启用示例（桌面版）：在 run.sh / 桌面配置里加 `VDL_COMMENTARY_ENABLED=true VDL_COMMENTARY_MODE=local VDL_COMMENTARY_DIR=/abs/path/to/commentary-pipeline VDL_COMMENTARY_PYTHON=/path/to/python3.13`。
