# 项目接续快照 — 视频工坊（VideoDownloader）解说模块

> 生成时间：2026-09-16 00:50（GMT+8）
> 主仓：`/Users/suixindelang/WorkBuddy/video-downloader-app`（分支 **app-dev**，HEAD `f2f9569`）
> 解说管线仓：`/Users/suixindelang/WorkBuddy/问问题/commentary-pipeline`（分支 **master**，HEAD `d9bdfb3`）
> 运行态：App v1.0.21 在运行（127.0.0.1:8321），包体=**构建 14**，前端热更 `f2f9569 @ 09-16 00:44`
> 两仓**无未提交的生产代码改动**（未跟踪项仅 `.workbuddy/`、`docs/`、`deploy/*.md`、管线 `scripts/解说词*.json`）

---

## 1. 当前项目原始需求

**产品**：视频工坊 —— macOS 桌面 App（PyInstaller 打包，`/Applications/视频工坊.app`），Flask 后端 + 纯 JS 前端 + 独立解说管线子进程。

**本轮（解说模块）用户的核心诉求，按提出顺序**：

| # | 用户原话（精简） | 目标 |
|---|------------------|------|
| 1 | 「这个开关隐藏，默认打开 / 这个隐藏 / 不要，后面剪映操作」 | 精简解说设置面板：隐藏「ASR 校正」开关（默认开）、隐藏「TTS 语音克隆状态条」、删除「BGM 自动配乐」块 |
| 2 | 「字号、描边调整能否实时预览？做个固定预览文字」 | 字幕样式实时预览 |
| 3 | 「配音字幕能支持鼠标点击移动调整位置吗」 | 字幕位置可拖拽自定义 |
| 4 | 「文字点击移动不了」 | 修拖拽失效 |
| 5 | 「现在这个参数（底部/居中 radio）是不是没用了」 | 移除 radio，换「↺ 重置位置」链接 |
| 6 | 「速度能再快一点吗」 | **提速（未做，见待办）** |
| 7 | 「全片 40 多分钟，怎么解说词只有这么多」 | 解说词覆盖不全 → 尾部覆盖闸门 |
| 8 | 「剩余 20 多分钟去哪了 / 谁裁剪的 / 我看到的 45 分钟多」 | 定位为「选错素材」→ 三层防线 |
| 9 | 「这个问题很严重，必须彻底清除问题」 | 选错素材防线必须落地 |

**产品铁律（不可违背）**：
- 面向所有用户，不只为这台 8GB Mac；引擎多方案 + 硬件探测 + 自动路由 + 失败回落，绝不写死一端。
- **用户不配密钥**：引擎只暴露 `auto`（本机优先→云端配合）/ `cloud`（纯云端）两档；凭据由管理员统一配置（受管层 `~/.video-downloader/llm_managed.json`，0600），云端真实 Key 只存于 ECS 网关。
- **免费用户不降质**；免费单视频 ≤30min，终身 3 次 / 每日 auto 1 次。
- 合规红线：不破解付费墙 / DRM / 付费 VOD。
- 打包体积 <500MB；改 `server/`、`VERSION`、依赖 → **必须全量构建**（`desktop/build_mac.sh`，须设 `COMMENTARY_PIPELINE_DIR`）；只改 `web/` → 走 `desktop/refresh_web.sh --deploy`（约 7~23s 热更）。

---

## 2. 已完成的全部工作成果

### 2.1 本轮解说模块（全部已提交 + 已部署上线）

| 提交 | 仓库 | 内容 |
|------|------|------|
| `69910ac` | app | 字幕样式实时预览 + 拖拽自定义位置 |
| `875fb33` | app | 修「拖不动」：容器 `pointer-events:none` 被子元素继承，文字 span 显式开回 `auto` |
| `b13e412` | app | 移除「底部/居中」radio（拖拽已是主交互），换「↺ 重置位置」链接 |
| `f2f9569` | app | **选错素材三层防线**（热更上线，免全量构建） |
| `c75f6f8` | app | 网关 URL 支持 `direct://` 前缀，本机代理故障不再拖死国内网关 |
| `699d884` | app | 指纹改读「打包当场」的管线 revision，修虚报版本 |
| `32a9023` | app | `_CommentaryRuntime.env()` 清 `VDL_WORKER_DEPTH`，避免 worker 被重入保险丝误拦 |
| `d698146` | 管线 | 字幕位置支持 `y:<比率>` 自定义（`_render_subtitle_png` 新增 y: 分支；y: 模式跳过 SUBTITLE_RAISE） |
| `d9bdfb3` | 管线 | **尾部覆盖闸门**：段数够但全挤前半段 → 带覆盖缺口反馈重跑一次 |
| `f1140c4` | 管线 | 预检剥 `direct://` 前缀 + 多端点逐个测（failover） |

更早（同为当前线上能力，勿回退）：`9198258` 解说前置闸门 + 引擎两档 + 管理员凭据；`ca61724` 云端 Key 不落地走服务端网关；`d5b8154` 任务失败不扣额度 + 空内容降级；`efd4049` SSE 修复；`68e27e5` 启动等待窗 60s→240s；`e41f5c2`/`b03d458` 兜底回归测试；去水印 `refine` 两阶段精修档（`auto`/`refine`/`legacy`）。

### 2.2 关键实现细节（接续必读）

- **字幕实时预览**：`#comPreview` 外包 `.com-preview-wrap`，覆盖层 `#comSubPreview`（固定示例文字，`pointer-events:none` 不挡播放器，内部 `span` 显式 `auto`）；`app.js comUpdateSubPreview()` 按 letterbox 后实际渲染高算字号（`contentH×4.8%×size`），8 向 `text-shadow` 模拟 ASS 黑描边 + 投影；触发 = 控件 `input` + `ResizeObserver` + `loadedmetadata` + `resize`；视频高 <40px 自动隐藏。
- **拖拽定位**：拖示例文字 → `comSubPosCustom = 'y:' + ratio`（文字中心距画面顶部比例，clamp 6%~94%）；提交 `subtitle_pos: comSubPosCustom || 'bottom'`；管线侧 `y:` 中心 = h×ratio（clamp 0.02~0.98），非法值兜底 `bottom`（**旧包收到 y: 会安全降级回 bottom**）。
- **尾部覆盖闸门**（`llm_script.py`，退化兜底之后）：最后一段 `end < 全片 75%` 且缺口 >90s → 把覆盖缺口写进 user_prompt 重跑一次，取覆盖更好者（仅触发时多一次 LLM 调用）。实测：37 段/止于 12:40 → **55 段/止于 19:48**，覆盖 ~100%。
- **选错素材三层防线**（`web/app.js`）：① `comSource` 下拉选项附 `duration`（接口字段早就有，前端从没展示）；② `comGenerateScript` 提交前用**自定义 `showConfirm`** 摆文件名 + 时长二次确认（pywebview 下 `window.confirm` 无效）；③ 文件名匹配 `_CLIP_NAME_RE`（前N分钟/片段/节选/clip/preview…）→ danger 弹窗强警示。
- **`direct://` 前缀**（三处消费均已剥）：`server/gateway_config.py`（`_normalize_url` + health 探测 + `gateway_status()` 返回 `direct:true`）、管线 `process._check_llm_endpoint`、`llm_script.py` 请求层。`~/.video-downloader/gateway_managed.json` url = `direct://http://8.138.223.3:8888/gw`。
- **网关架构**：广州 ECS `8.138.223.3:8888`，nginx 按路径分流 `/gw/`→8890（网关）、`/`→18890（vdl-web）；**安全组只放行 8888**；本机只持 `vdlt_` 可吊销令牌，真实 Key 只在 `/opt/vdl-gateway/upstream.json`。

### 2.3 验收记录（已通过的端到端）

- 验收成片：`少帅第8集前20分钟-fade擦除+定向重写验收-解说完成202609152258.mp4`（121 段，249MB）。
- 闸门验证：`少帅-尾部覆盖闸门验证-解说完成202609160025.mp4`（55 段，铺满 19:48，220MB）。
- 防线回归任务 `ecca1bdd4e7c` 全流程完成；App 在线校验 `comSelectedSourceLabel`、`_CLIP_NAME_RE` 各 2 处。
- 指纹铁证：任务一直用 `Downloads/少帅_第8集_前20分钟.mp4`（20.0min，sha16 `5cbe31dc2a5f9f44`）；45.6min 完整版 `少帅_国产剧_第8集.mp4`（sha16 `9ed0296049404d34`）**从未进过解说任务**。

---

## 3. 当前进行到哪一步 / 未完成待办

**当前状态**：本轮所有需求（1~5、7~9）已提交、已构建、已热更上线并通过端到端验证。处于**收尾交接**状态，无正在执行的构建或任务。

**待办（按优先级）**：

| P | 事项 | 说明 |
|---|------|------|
| P0 | **解说提速**（用户原话「速度能再快一点吗」） | 未做。当前瓶颈：prep 裁剪（VideoToolbox，实测 84s）+ Whisper 转写 + LLM 两阶段 + TTS + 渲染。建议先量化各阶段耗时再定优化点（TTS 并发 / 转写档位 / 渲染预设）。 |
| P1 | **45min 完整集解说** | 免费 30min 前置闸门会拦 `少帅_国产剧_第8集.mp4`（45.6min）。需用户裁剪进 30min 或走会员/付费通道，尚未与用户确认方向。 |
| P1 | **任务日志落盘** | 环形缓冲只留 200 条，把「照抄检测 / usage 统计」挤掉；后续要统计须在任务结束瞬间拉取或改为落盘。 |
| P2 | 旧包 `y:` 降级提示 | 旧构建收到 `y:` 静默回 `bottom`，可考虑提示「需升级」。 |
| P2 | 遗留架构债（MEMORY.md 已记） | 公网入口仍 http 明文；配额仍在客户端记账；网关 2C2G 单点。 |
| P2 | 体验文案 | 「剩余 XX:XX」是整任务 ETA 不是当前阶段耗时，用户多次误读（见 prep 0% + 剩余 24min 误判）。 |

---

## 4. 关键约束、修改要求、注意事项

### 4.1 构建与部署（血泪坑）

- 全量构建：`COMMENTARY_PIPELINE_DIR=/Users/suixindelang/WorkBuddy/问问题/commentary-pipeline bash desktop/build_mac.sh`（改 `server/`、依赖、`VERSION`、打包配置时必须跑）。**纯 `web/` 改动绝不构建**，走 `desktop/refresh_web.sh --deploy`。
- ⚠️ **`refresh_web.sh --deploy` 会杀掉运行中的 App**（内存态任务丢失）；dist/ 被移走时它会报错 —— 那时只能原地修 `/Applications/视频工坊.app/Contents/Resources/web/`（server 按磁盘 serve + 动态注入 `?v=`）。
- ⚠️ **部署 /Applications 必须 `dangerouslyDisableSandbox:true` 且前台运行**；绝不 `rm -rf` 旧包（删除闸门）→ `mv` 移走 + `ditto` 换入；校验 `version.txt` + `/api/version`。
- ⚠️ **绝不要把 ditto 部署放进后台任务**：不带沙盒开关 → 全文件 `Operation not permitted` → 留半截包（`Contents/MacOS/` 出现 `.BC.T_xxxxxx`），Dock 启动报 "executable is missing"。诊断看 `~/.vdl_launch.log`。
- ⚠️ **PyInstaller FileExistsError**（`os.symlink av/codec/context.pyi`）= dist 半残留 → `mv dist/VideoDownloader.app dist/.stale_<ts>` 后重跑。
- ⚠️ **启动 App 必须用 run_in_background 方式**（后台任务）；`nohup` 启动会**静默死亡**（活几分钟就退、无日志）。
- 冷启动：换包后 97~120s（等待窗已 240s，热启动 ~2s 无感）；`open -a` 换包后常瞬时失败 `-600/-609`，等 5~10s 重试。
- 包内管线真实路径：**`Contents/Resources/commentary/`**（不是 Frameworks/）。

### 4.2 代码约定

- 引擎两档 `auto` / `cloud`；`mlx`/`ollama` 已下线。Mac 禁用 Ollama/llama.cpp（Metal SIGABRT），本地一律 MLX。
- 前端新增控件必须在 `web/app.js` 的 `el` 登记；`POST /api/llm/config` 必须**增量合并**。
- 新增 `server/tests/test_*.py` **必须加进 `run_offline_tests.sh`**，否则静默不跑。
- 两份 `quota.py`（`server/`、`scripts/`）**必须同源 + diff 校验**。
- 预检闸门：`quota.precheck_commentary()` 返回键是 **`allowed` 不是 `ok`**；时长按 `_effective_duration()`（总时长 − trim 头尾）判；所有解说入口（`/api/commentary`、`/script-only`、两个 upload）**都要过闸门**。
- pywebview 下 `window.confirm/alert` 无效 → 一律用自定义 `showConfirm`。
- 改 `deploy/gateway/*.py` 后必须 `scp` + `systemctl restart vdl-gateway`；ECS 排错 `journalctl -u vdl-gateway -n 30`。

### 4.3 用户协作偏好

- **用户不做命令行运维**（SSH / systemctl / 后台点击）——凡 AI 能自己完成的，绝不让用户做；线上验证自己 `curl` 自查。
- 指令极简（「推」「继续」「你定就行」），小决策授权 AI 自决，仅在**重大范围变更**时介入。
- 偏好结构化输出（表格 / 问题列表 / 直接结论），讨厌多余闲聊；交付前**先自测再交用户**。
- 交付后要求主动自查未打通项并修复；后台任务停滞要主动回报。

### 4.4 常用命令速查

```bash
# App 版本 / 在线校验
curl -s --noproxy '*' http://127.0.0.1:8321/api/version
# 在线校验前端热更是否生效
curl -s --noproxy '*' http://127.0.0.1:8321/ | grep -c "comSelectedSourceLabel"
# 全量构建（前台 + 免沙盒，需 Agent 模式 dangerouslyDisableSandbox）
COMMENTARY_PIPELINE_DIR=/Users/suixindelang/WorkBuddy/问问题/commentary-pipeline bash desktop/build_mac.sh
# 纯 web 热更
bash desktop/refresh_web.sh --deploy
# 离线测试
bash run_offline_tests.sh
```
