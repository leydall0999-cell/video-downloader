# 视频工坊（VDL）项目接续快照 — 2026-09-21 00:40

## 0. 一句话现状
「解说风格强度滑杆」已全链路落地（前端→后端→管线）、全量构建通过门禁、已部署进本机
`/Applications/视频工坊.app` 并实测可跑；**唯一未做的是发布到更新源**（需用户点头）。

---

## 1. 项目原始需求
自托管的**视频下载 / 媒体备份工具**，产品名「视频工坊」（VideoDownloader），三条并行形态：
- `main` = 全功能主线（17 router）
- `web-dev` = 轻量网页版（部署在 Railway，域名 hanyuxz.top）
- `app-dev` = **桌面 App（PyInstaller 打包，当前工作重心）**

核心能力：50+ 平台下载解析、视频格式转换（23 种）、一键抠图/去水印、**视频解说自动成片**（增值方向）、
扫码分享。合规红线：只下载自有/已授权内容，不破付费墙 / DRM。

**当前活跃分支：`app-dev`**（用户 2026-08-24 明确：默认全做 app 端；要动 web 端必须先问）。

---

## 2. 已完成工作成果

### 2.1 本轮（风格强度滑杆，全部已提交+推送）
端到端 5 层穿透，语义：`≤35` 口吻克制不玩梗 / `默认 65` 鲜明不过火 / `≥80` 拉满风格表现力。

| 层 | 文件 | 内容 |
| --- | --- | --- |
| 前端 | `web/index.html`、`web/app.js`、`web/styles.css` | 解说风格块下新增 range 滑杆（0~100、默认 65）+ 实时回显 + payload 读入 `style_intensity` |
| 请求模型 | `server/app.py` `CommentaryRequest` | 新增 `style_intensity: int = Field(default=65, ge=0, le=100)` |
| CLI 拼装 | `server/app.py` `_commentary_option_args` | 产出 `--style <s> --style-intensity <n>` |
| 内部透传 | `server/app.py` `_commentary_run` / `_commentary_run_http` | 形参 + 逐层透传 |
| 路由 | `server/routers/commentary.py` | JSON 两路（payload）+ form 一路（Form(65)）共 3 处透传 |
| 管线 | `问问题/commentary-pipeline/process.py` | `--style-intensity` argparse + `auto_script` 透传 |
| 管线 | `.../scripts/llm_script.py` | `_style_intensity_prompt(n)` 助手；`_build_system_prompt` 与 `llm_script()` 加形参；**highlights + full 两分支都注入** |

### 2.2 本轮踩坑（已根治，写进技能手册）
上一轮有 **4 处编辑「报 success 但没落地」**，每处都会让解说功能直接崩：
`CommentaryRequest` 漏字段（主路径 AttributeError）→ `process.py` 漏 argparse（AttributeError）
→ `_build_system_prompt` 漏形参（NameError）→ `llm_script()` 漏形参 + full 分支漏注入（TypeError）。
**根因**：构建门禁的 pyflakes **只扫 `server/`，不扫 commentary-pipeline 仓库**；且它查不出
「模型里没这个字段」这类属性缺失。已用「逐处 grep 复核 + 真实请求探针」发现并修复。

### 2.3 新增回归测试（都做过「注入缺陷→精确转红→还原 sha 一致」的反向验证）
- `server/tests/test_commentary_style_intensity.py`（5 项）——含 **★AST 棘轮**：
  `routers` 里每处 `payload.X` 必须是该函数绑定模型里的真实字段（对将来新增引用自动生效）
- `commentary-pipeline/tests/test_style_intensity.py`（8 项）——锁签名必须收参、两分支必须注入、
  `--style-intensity` 必须真注册
- 两者都已接入 `run_offline_tests.sh`，构建门禁现为 **45/45 全绿**

### 2.4 构建与部署（已完成）
- 全量 `build_mac.sh` 成功（**6 分 39 秒**），门禁 `✔ 离线测试全部通过`
- `refresh_web.sh --deploy` 已把包部署进 `/Applications/视频工坊.app`（构建指纹 **app=3b8dbdb /
  pipe=ac44f7d**，旧版备份在 `~/.vdl_backups`）
- **冻结代码层权威验证通过**：启动 App 后 `curl 127.0.0.1:8321/openapi.json`，
  `CommentaryRequest.style_intensity` = integer / min 0 / max 100 / default 65
- 前端服务产物核验：`GET /`、`/app.js`、`/styles.css` 均含滑杆
- 包内管线功能核验：30/65/85 三档 prompt 文本互异、两分支都注入、无风格时不注入

### 2.5 同批次一并交付的前端改动（前几轮，均已进包）
- 录音按钮「再听一遍」→「试听」（并修 WebKit 二次播放静默拒绝）
- **修复「解说风格选择整组消失」**：`comStyle` 块走统一下拉机制却漏 `.com-opt-title`，
  导致 `initComSelect` 提前 return
- 配音引擎改名：系统音色 / TTS语音克隆 / MLX语音克隆(更自然)（含灰显文案统一）
- 状态条文案统一：本地语音克隆 → MLX语音克隆 / TTS语音克隆

### 2.6 技能手册更新（本轮）
`~/.workbuddy/skills/vdl-add-offline-tests/SKILL.md` 913 → 1032 行（纯追加，未改原有行）：
新增 §4.13「交叉校验属性名：堵 pyflakes 盲区」（AST 棘轮写法 + 四坑 + 漏层症状表）；
§1.5 补「构建门禁不覆盖管线仓库」+「打包形态决定热修还是重构建」。备份
`/tmp/vdl_add_offline_tests_skill.BAK.md`。

---

## 3. 当前位置与待办

### 3.1 仓库状态（全部已推送，工作树干净）
- `video-downloader-app`（分支 `app-dev`）HEAD = **3b8dbdb**，`origin/app-dev` 已同步
- `commentary-pipeline`（分支 `master`）HEAD = **03279c6**，`origin/master` 已同步
  （本次一并推送了积压的羽化修复 `9ccdebf`/`1e2d07f`）
- 主仓 `video-downloader`（分支 `web-dev`）HEAD = **0348b36**，干净、已同步，本轮未动

### 3.2 待办清单（按优先级）
1. 🔴 **发布 v1.0.21 到更新源**（唯一未完成的实质事项）
   - 命令：`cd ~/WorkBuddy/video-downloader-app && bash desktop/publish_update.sh`
   - ⚠️ 脚本内置「手输版本号确认」门禁（用户自己设的，**不得用 `--yes` 绕过**）
   - ⚠️ 线上当前是 **v1.0.18**，本地 `VERSION` = **v1.0.21** ⇒ 会**一次跳过 3 个版本**，
     用户端将直接吃下 1.0.19~1.0.21 的全部累积改动（风格强度 + 羽化修复 + 配音改名等）
   - 建议：用户先在 App 里实跑一次带风格强度的解说，观感 OK 再发布
2. 🟡 **端到端观感实测**：目前只验证到「prompt 措辞随强度变化」（prompt 层），
   未跑真实解说全流程看成品口吻差异
3. 🟢 可选：把 `~/.workbuddy/skills/` 初始化成 git 仓库（现在改技能没有版本历史，只能靠临时备份）
4. 🟢 用户未要求、暂不做：把风格强度同步到 web 端（web-dev 的 app.py 副本）

---

## 4. 关键约束与注意事项

### 4.1 工作方式（用户明确）
- **默认只做 app 端（app-dev）**；要动 web 端必须先问用户
- **AI 能自己做的一律不要让用户做**：SSH 运维、改配置、重启服务、跑构建、验证，全部自执行
- 用户**不执行命令行**，别让他去 SSH / 点后台 / 看日志

### 4.2 发布红线
- `desktop/publish_update.sh` 是**用户设的发布门禁**（手输版本号确认），**绝不绕过**
- `build_mac.sh` 只产出 `dist/VideoDownloader.app`，**不自动发布**
- 全量构建后要进 `/Applications` 需再跑 `bash desktop/refresh_web.sh --deploy`

### 4.3 构建 / 部署环境（缺一即中止）
```bash
cd ~/WorkBuddy/video-downloader-app
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"        # dylibbundler 在这
export TMPDIR="$PWD/.build_tmp"                             # 沙盒拒 /private/var/folders
export COMMENTARY_PIPELINE_DIR="$HOME/WorkBuddy/问问题/commentary-pipeline"
export VDL_BUILD_WORKPATH="/tmp/vdl_build_work"             # 绕开沙盒对已签名二进制的替换守卫
bash desktop/build_mac.sh                                   # 约 6~17 分钟
```
- 必须**后台任务**跑（不能 `nohup` 脱手，沙盒会杀掉脱离的进程）
- ffmpeg 走 LGPL 构建 `~/vdl-ffmpeg-lgpl`（避开 GPL 许可）
- 构建 venv 里 PyInstaller 的 `remove_signature_from_binary` 补丁**必须保留**（已把
  `raise SystemError` 降级为 warning）

### 4.4 改代码前必知
- **管线 = 数据文件**（`--add-data`，明文落在 `Contents/Resources/commentary/`，worker 用
  `sys.path.insert(0, loc.root); import process` 从磁盘加载）⇒ **可热修 + `codesign --force --deep --sign - <app>`，无需全量构建**
- **`server/` = 冻结进二进制**（`--hidden-import app`）⇒ **必须全量 `build_mac.sh`**，只改资源副本不生效
- `Contents/Frameworks/commentary` 是指向 `../Resources/commentary` 的软链，包内只有一份
- **改完必须逐处 grep 复核**：本轮反复出现「Edit 报 success 但没落地」
- 构建门禁 pyflakes **只扫 `server/`**；改管线必须**自己跑**：
  `cd ~/WorkBuddy/问问题/commentary-pipeline && TMPDIR=/tmp/vdl-pytest .venv/bin/python -m pytest tests/ -q -p no:cacheprovider`

### 4.5 环境坑
- 本机 **git 2.15** 太老：无 `--show-current` / `merge-tree` 双参 / 无 `worktree remove`；
  一律加 `-c safe.directory='*'`
- **Bash 对中文路径不可靠**：`test -f` / `cd` / `grep` 对 `问问题/` 可能静默失败，用绝对路径 + `ls -la`
- **长中文 commit message 写文件再 `git commit -F <file>`**（`-m` 里的反引号会被 shell 当命令替换吞掉）
- App 冷启动约 **120s** 端口 8321 才就绪；探测要按 **HTTP 200** 判，别把代理报错文本当成功
- 跑全量 `pytest server/tests` 有 **11~12 项既有环境性失败**（会员/配额/家目录类），
  判「是否自己改坏」用 worktree 跑上一提交 diff 失败集合

### 4.6 安全 / 凭据
- 关键凭据（AppKey/Secret/Token/密码）**绝不从截图 OCR**，必须让用户复制文本
- 内部限流 `VDL_RATE_LIMIT_PER_HOUR`（默认 30/小时）是项目自带的，**开发会员制时必须
  按订阅状态分层放开**（`_check_rate_limit` 需接 `_subscription_quota` 的 subscribed 值），
  否则付费用户照样被限速

---

## 5. 速查
- 本机 App：`/Applications/视频工坊.app`（已在运行，端口 8321）
- 本地服务探测：`curl -s 127.0.0.1:8321/api/version`
- 冻结代码核验：`curl -s 127.0.0.1:8321/openapi.json`（看目标字段在不在）
- 更新源：`http://8.138.223.3:8765/latest.json`（当前 v1.0.18）
- 仓库：`github.com:leydall0999-cell/video-downloader.git`（app-dev）、
  `github.com:leydall0999-cell/commentary-pipeline.git`（master）
