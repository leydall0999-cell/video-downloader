# 项目接续快照 — 「我的音色」页内直接录制（含结束键修复）

> 生成时间：2026-09-19 16:58（GMT+8）
> 主仓：`/Users/suixindelang/WorkBuddy/video-downloader-app`（分支 **app-dev**，HEAD `19740cf`）
> **远端已同步**：`git@github.com:leydall0999-cell/video-downloader.git` → `origin/app-dev` = `19740cf`（本轮 2 个提交已 push）
> 运行态：App **v1.0.21** 运行中（127.0.0.1:8321），包内指纹 `前端热更 19740cf @ 09-19 16:49 #260919164925`
> 前序快照：`PROJECT_SNAPSHOT_2026-09-18.md`（解说页剪映式布局改版，已完成）

---

## 1. 当前项目原始需求

**用户原话**：看到「配音与音量 → 我的音色」里只有一个「🎙 选择录音」按钮，问 **「录音能在这里直接录吗」**（附图）。
第二句反馈（16:39 截图）：**「没有结束键」** —— 点了「直接录制」后按钮变成灰色「录制中…」，界面上找不到停止按钮。

**诉求本质**：用户不想再用外部录音软件，要**在页面里直接念一段、点一下就存成音色样本**；且**开始与停止必须在同一个可点位置**，不能被滚动区域藏起来。

**验收标准**：
1. 「我的音色」区出现「⏺ 直接录制」，点了就能录（麦克风链路在真机上真能采到声音）。
2. 录制中主按钮**就地**变成可点的停止键，不依赖任何会被滚出视口的控件。
3. 停止后自动上传并保存为音色，无需再点「保存音色」。
4. 一切在**已安装到 `/Applications/视频工坊.app` 的包内实例**上实测通过（不是看源码）。

---

## 2. 已经完成的全部工作成果

### 2.1 提交记录（本轮 2 个，均已 push）

| commit | 内容 |
|---|---|
| `97fba9a` | 「我的音色」支持页内直接录制（9 文件，+647/-1） |
| `19740cf` | 修「没有结束键」：主按钮就地变「⏹ 停止并保存」（3 文件，+52/-14） |

### 2.2 前端（`web/`）

- **`index.html`**：「我的音色」行加「⏺ 直接录制」按钮（与「🎙 选择录音」并排）；文字稿下方加录制条 `#comMyVoiceRecBar`（红点 / 计时 / 电平条 / 提示 / 取消）。**录制条里不再放停止键**（`comMyVoiceRecStop` 已删除）。
- **`app.js`**：
  - 采集走 **WebAudio + 自编 16bit 单声道 WAV**（不用 MediaRecorder —— Safari 出 m4a、Chrome 出 webm，格式不统一）。
  - `comRecStart()` / `comRecStop(save, auto)` / `comRecEncodeWav()` / `comRecBusy()` / `comRecTeardown()`。
  - **两态切换**：主按钮点击分支 `if (comRec.active) comRecStop(true,false); else comRecStart();`；录制中标签变「⏹ 停止并保存」且**保持 `disabled=false`**、加 `.is-recording` 转红。
  - 常量：`COM_REC_MAX_SEC=30`（到点自动停）、`COM_REC_MIN_SEC=1.0`、`COM_REC_PEAK_MIN=0.02`（峰值过低判「没采到麦克风」并报错，不存静音）。
  - 上传 `/api/commentary/voice-sample/record`，成功后自动点「保存音色」。
  - 错误提示区分 `NotAllowedError`（引导去系统设置开麦克风）、`NotFoundError`、12s 超时。
- **`styles.css`**：`.com-myvoice-recbar` 全套样式 + `comRecPulse` 动画；`.com-myvoice-rec.is-recording` 转红（**显式写 `background-color` + `background-image:none` + `transition:none`**）；窄栏下录制态 `flex: 1 1 100%` 独占一行防裁字。

### 2.3 后端（`server/`）

- **`commentary_config.py`** 新增：`_voice_rec_dir()`、`_wav_duration()`（解析 WAV 头，**时长优先信服务端解析，不信前端传值**）、`save_recorded_voice_sample()`、`_prune_voice_recordings()`。常量 `VOICE_REC_MIN_SEC=1.0 / MAX_SEC=60 / MAX_BYTES=32MB / KEEP=5`。落盘 `~/.video-downloader/voice_samples/`，**文件 0600、目录 0700**。
- **`routers/commentary.py`** 新增 `POST /api/commentary/voice-sample/record`（`UploadFile` + `Form`）。
- 修复既有 bug：文件名原用秒级时间戳，同秒多段会互相覆盖 → 改**毫秒 + 撞名自增**。

### 2.4 桌面端（**两个硬依赖，缺一即「点了没反应/永远卡在获取麦克风」**）

- **`desktop/desktop_launcher.py`**：🔴 pywebview 6.x **没实现** WKWebView 的采集权限回调 ⇒ `getUserMedia` 永远 pending（**不报错，就是不落定**）。启动时用 PyObjC `objc.classAddMethods` 给 `BrowserView.BrowserDelegate` 补 `webView:requestMediaCapturePermissionForOrigin:initiatedByFrame:type:decisionHandler:` 并放行（`signature=b'v@:@@@Q@?'`，**必须调用 handler(1)**）。
- **`desktop/build_mac.sh`**：🔴 包内 `Info.plist` 必须声明 `NSMicrophoneUsageDescription`，否则系统**静默拒绝且根本不弹窗**（极难查）。已用 `plutil -replace`（失败回退 `-insert`）写入。

### 2.5 测试

- **`server/tests/test_voice_sample_record.py`**（新增 7 例）：WAV 头解析、保存成功（0600/0700/字节精确/WAV 时长优先）、拒绝空/超大/非法扩展名/时长越界、非 WAV 回退前端时长、prune 只留 5 且不碰 `voice_sample.json`。已做**变异验证**（故意注入缺陷→转红，已还原）。
- 已挂进 `server/tests/run_offline_tests.sh`。**全量离线套件 43 通过 / 0 失败**。

### 2.6 验证证据（对**包内实例**实测，不是看源码）

- `/api/version` = `v1.0.21 · 前端热更 19740cf @ 09-19 16:49`；包内 `app.js`/`styles.css` 与仓库 **sha256 逐字节一致**，`index.html` 除注入指纹外 diff 为空；服务出来的三件套 11 项改动全部命中。
- 路由在 openapi 里；真 WAV POST 返回 **200**（正确解析 1.5s）；过短返回 **400**；目录/文件权限 0700/0600。
- 启动日志出现「**已放行 WebView 媒体采集权限（我的音色·直接录制）**」。
- **真 WKWebView 跑完整链路**（脚本 `/tmp/wk_rec_toggle_test.py`，支持 `VDL_PROBE_W/H`）：三态断言全绿 —— 闲置 / 录制中（`disabled:false`、`is-recording`、`rgb(225,29,72)`、163px 不裁字、录制条可见、计时 2.0s、电平 6%、文字稿 readOnly）/ 停止后（还原、录制条 hidden、状态「音色已保存」）；fetch 恰好 1 次，`fileSize=49196=44+24576×2`（WAV 编码逐字节对得上）。
- **真机录音已落盘证明麦克风链路可用**：`~/.video-downloader/voice_samples/voice_rec_20260919_163944_024.wav`，29.95s / 48kHz / 单声道（用户被 30 秒自动停兜住），另有 16:47 两次 8.19s / 6.66s。

---

## 3. 当前进行到哪一步 / 未完成待办

**状态：本轮需求已交付完毕并部署，App 正在运行。无阻塞项。**

| # | 待办 | 优先级 | 说明 |
|---|---|---|---|
| 1 | 清理用户被卡住那次的残留录音 | 低（**需用户点头**） | `~/.video-downloader/voice_samples/voice_rec_20260919_163944_024.wav`（2.8MB，29.95s）。已告知用户，等他决定是否删。**不要自作主张删。** |
| 2 | 真机麦克风端到端再确认一次 | 低 | 已有 29.95s 落盘作证；若用户下次仍报「录不到」，先查系统设置 → 隐私与安全性 → 麦克风 → 视频工坊是否被关。 |
| 3 | 工作区未跟踪文件 | 信息 | `PROJECT_SNAPSHOT_*.md`、`docs/`、`deploy/`、`web_backup_改版前_2026-09-17/`、`解说模块_剪映式布局_v2原型.html`、`server/quota.py.bak` —— **均为有意保留，勿删、勿提交**。 |
| 4 | 未回滚的验证脚本 | 信息 | `/tmp/wk_rec_toggle_test.py`、`/tmp/wk_myvoice_shot.py` 在 /tmp，未入库，不影响仓库。 |

---

## 4. 关键约束 / 修改要求 / 注意事项

### 🔴 打包与部署铁律
- **`server/*.py`、`desktop/desktop_launcher.py`、`Info.plist` 改动 ⇒ 必须全量构建**：`COMMENTARY_PIPELINE_DIR=/Users/suixindelang/WorkBuddy/问问题/commentary-pipeline bash desktop/build_mac.sh`（包内 `Resources/server/*.py` 是**死数据**，手工 cp 不生效；PYZ 赢明文）。
- **纯 `web/` 改动 ⇒ 走热更**：`bash desktop/refresh_web.sh --deploy`（约 30 秒，含 `node --check` 门禁 + 重签名 + 指纹校验）。⚠️ 它会 quit App 但**不自动重启** ⇒ 自己 `open -n "/Applications/视频工坊.app"`。
- **证「真进包」＝直接打请求看响应**（`curl` 包内实例的 `/openapi.json`、`/app.js`），grep 源码或包内明文都**不算证据**。
- 包内 App 名是中文 `/Applications/视频工坊.app`；构建产物是 `dist/VideoDownloader.app`。
- 包内 ffmpeg 是 LGPL（无 `delogo`/`libx264`）；包内**无可执行 python**。
- `Edit` 报成功 ≠ 真改：同文件多处编辑会互相覆盖，**改完必 grep 复验**。

### 🔴 WKWebView 麦克风（本轮核心新知识）
- 两个依赖**必须同时满足**：① delegate 实现采集权限回调（pywebview 没有，已补）；② `Info.plist` 声明 `NSMicrophoneUsageDescription`。缺 ② 时系统**静默拒绝且不弹窗**。
- **PyObjC `classAddMethods` 签名规则**：`v@:` 之后的每个编码对应函数**除 self 外**的一个参数。5 参 → `b'v@:@@@Q@?'`（Q=NSUInteger，@?=block）。**必须调用 handler**，否则 WebKit 崩 `NSInternalInconsistencyException: Completion handler ... was not called`。
- **沙盒限制**：WorkBuddy 沙盒内 WKWebView **加载不了 http**（网络进程被封，只走 `file://`）、**没有音频设备**（`getUserMedia` 永不落定，delegate 根本不被调用）。所以真机录制只能在用户机器上验证。

### 🔴 前端验证手法（可复用）
- 沙盒内验证：用「假 `getUserMedia` + 假 `AudioContext`（`Proc.connect()` 时同步灌 6×4096 个 0.35 幅度样本）+ **只截要测 URL 的 fetch 桩**」。⚠️ 桩若拦截**所有** fetch，`voice-sample` 的 GET 会返回假数据把文字稿冲空 ⇒ 点击撞在「请先写好内容」守卫上，白跑一轮。
- **离屏 WKWebView 不推进 CSS 过渡/动画**（冻在起点）→ 量 `getComputedStyle` 会读到过渡起点值，看着像「样式没生效」。探针侧注入 `*{transition:none!important;animation:none!important}` 让拍到的是终态。
- `.btn` 自带 `transition: background .18s`，`.btn-primary` 底是 `linear-gradient` ⇒ 背景色过渡起点是 **transparent**，切换那 0.18s 是**白底白字**。产品代码须写 `background-color` + `background-image:none` + `transition:none`。

### 🔴 其他沿用铁律（详见 `.workbuddy/memory/MEMORY.md`）
- 配置落盘读改写**整段持锁**（`server/atomic_io.py`），别改回 `write_text`。
- 前端新参数走**一个 JSON 字符串字段**且**必进 cache tag**。
- 数据目录两处：`~/.video-downloader`（配置/配额/成片）、`~/.videodownloader`（cookie 池 + yt-dlp）。
- bash `grep` 无输出 → 用 `/usr/bin/grep`；长中文 commit 用 `-F <file>`（`-m` 里反引号会被吞）。
- **凡是 AI 能自己完成的操作绝不让用户做**（SSH/VPS/curl 自查等）；仅私人后台登录、真实密钥粘贴才问用户。

---

## 5. 下一步建议（若继续开发）

1. 等用户真机再录一次确认按钮手感；若 OK，本轮闭环。
2. 顺手可做的增强（**未做，等用户提**）：录制条电平改成实时频谱、录制前 3 秒倒计时、样本列表可选/试听、录制条常驻吸底（防再次被滚出视口）。
3. 若要动 `server/*.py` 或桌面端，**务必全量构建 + 重新部署**，别只热更 `web/`。
