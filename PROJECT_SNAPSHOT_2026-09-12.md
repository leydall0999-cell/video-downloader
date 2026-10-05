# 项目接续快照 — 图片去水印「两阶段精修（refine）」改造

> 生成时间：2026-09-12 20:55（GMT+8）
> 分支：`app-dev`　工作树：`/Users/suixindelang/WorkBuddy/video-downloader-app`
> HEAD：`0318888 图片去水印传统档再优化…（VERSION=1.0.15）`
> **当前有 5 个文件未提交的改动**（见第 2 节），尚未 commit、尚未构建。

---

## 1. 原始需求

用户原话：**「图片去水印能力不行，每次都得使用 AI 效果才可以，给我优化」**

拆解后的目标：

1. 非 AI（OpenCV）去水印路径必须产出**可用**结果，不能「必须切 AI 引擎」。
2. 关键失败场景：**浅色底 + 半透明白色文字水印**（如 `SAMPLE 素材网 example.com`，α=0.5）。
   当前 1.0.15 的 `auto` 档在这类图上漏修严重（实测仅修 5,020 / 10,787 px，漏 56%）。
3. 用户对该场景**打 5/100 分**（明确拒收），后续要求「还是不够，能不能二次加工」，
   并提出「装两个处理插件，第一个处理完后第二个介入精修」→ 即**两阶段管线**。
4. 已确认落地方案（用户通过选项选择）：**内置两阶段 + 新增 `refine` 档位**，保留 `auto` 作单遍回退。

---

## 2. 已完成的工作

### 2.1 生产代码改动（5 个文件，全部**未提交**）

| 文件 | 改动 | 状态 |
|------|------|------|
| `server/dewatermark_core.py` | +174 行：新增 `_residual_map_bilateral`、`_refine_expand`、`_refine_pipeline`；`detect_watermark` 新增 `residual_fn` 参数；`plan_image_repair` 新增 `**kw` 透传；`image_inpaint_ex` 新增 `quality="refine"` 分支 | 已完成，pyflakes 零 undefined |
| `server/routers/dewatermark.py` | `quality` 白名单加 `refine`（默认已改为 `refine`）；docstring 更新 | 已完成 |
| `web/index.html` | `dwImgQuality` 下拉新增 `refine` 项并设为 `selected` | 已完成 |
| `web/app.js` | 默认提交值改 `refine`；`dwSyncEngineUi` 的 `auto` 判定扩展含 `refine` | 已完成 |
| `server/tests/test_dewatermark_core.py` | +113 行：6 项 refine 测试 | **46/46 全过** |

`git diff --stat`：`5 files changed, 289 insertions(+), 17 deletions(-)`

### 2.2 核心算法（已落库）

```python
# server/dewatermark_core.py
_residual_map_bilateral(gray_u8)        # 中值多尺度残差 ∪ 双边保边残差（d=15, σColor=100, σSpace=100）
_refine_expand(mask_full, h, w)         # 连通组件 bbox 四边各外扩 40%（且≥2px）+ 3×3 椭圆闭运算
_refine_pipeline(img, regions, dst, method, radius)
    # Stage1: plan_image_repair(img, regions, "auto", residual_fn=_residual_map_bilateral)
    #         → _inpaint_from_mask(ns, r=3) → _feather_merge → out1
    # Stage2: plan_image_repair(out1, regions, "auto", residual_fn=…,
    #                           thr_k=0.15, min_redelta=3.0, min_fill=0.002,
    #                           max_fill=0.6, sat_max=130)            # 更激进口径重检
    #         mask2 = _refine_expand(m2d) ∪ _refine_expand(m1)
    #         mask2 &= add_union & ~sub_mask                          # 尊重 subtract
    #         → out2
    # info 返回 stage1_repair_px / stage2_repair_px / method_used
```

`image_inpaint_ex` 的 `quality` 三档：`auto`（单遍，中值残差，**1.0.15 基线不动**）、
`legacy`（整块）、`refine`（两阶段）。

### 2.3 1.0.15 已发布基线（对照）

- `_SMART` 常量：`thr_k=0.40, sat_max=95, max_fill=0.30, solid_inner=0.02,
  solid_inset=(0.30,6,18), cc_frac=0.0004, min_fill=0.004,
  scales=(3,5,9,15,25,35), min_redelta=12.0, min_stroke_px=24, stroke_grow=2, max_solid_rect=0.5`
- 30 样本基准：PSNR 24.16→34.02，Δavg +3.25→+13.11，变差 14→5/30，最差损伤 -10.13→-4.05。

### 2.4 实验资产（`/tmp/dwbench/`，**重启会丢，必要时先备份**）

- 测试图：`real_wm.png`（900px，半透明白字「SAMPLE 素材网 example.com」α=0.5）+ `real_clean.png`
- 基准台：`mkbench.py`（`build_suite(photo_path)` → 30 样本：3 底图 × (3 形态 × 3 强度 + solid)；
  底图 = photo / gradient / texture；形态 = diag / tile / badge / solid）
  依赖真实照片：`/Users/suixindelang/Downloads/[已压缩]落地式展架副本.jpg`
- 关键脚本：`bench_refine.py`（三档对比）、`diag_refine.py`（回归隔离）、
  `sweep_expand.py`（外扩幅度扫描）、`gate_stage2.py`（复杂度闸门）、
  `mask2_variants.py`（stage2 mask 组成对比，最新）、`twopass_v2/v3.py`（策略可视化）
- 对比图：`refine_vs_auto.png`、`before_after_v4.png`、`v3_strategies.png`
- 结果记录：`/tmp/dwbench/mask2_result.txt`

---

## 3. 当前进展与**未完成的待办**

### 3.1 阻塞点（最重要，接手先读这段）

**`refine` 在 30 样本基准上净回归，不能作为默认档位。**

30 样本水印区 PSNR（越高越好）：

| 档位 | mean | Δvs auto | worse(<-0.5dB) | better |
|------|------|----------|----------------|--------|
| auto（基线） | **25.94** | — | 0 | 0 |
| refine（当前实现） | 24.35 | **-1.59** | **12/30** | 6/30 |

最差样本：`photo-tile` -11.5dB、`photo-badge` -9.5dB（**真实照片底图**翻车最狠）；
`gradient` 底图反而 +19.7dB。

**根因隔离（`diag_refine.py` 结论，已验证）：**
- 双边残差（stage1 换 `residual_fn`）与 auto **完全一致** → 双边在这 30 样本上零贡献（并集被中值主导）。
- **回归 100% 来自 stage2**：真实照片的纹理细节被 stage2 的激进阈值当成「残影」误检，inpaint 一填就毁画面。

**stage2 mask 组成扫描（`mask2_variants.py`，2026-09-12 20:51 跑完）：**

| 组成 | mean | Δauto | worse | better |
|------|------|-------|-------|--------|
| OFF（只 stage1） | 25.94 | +0.00 | 0 | 0 |
| M1 只重检残影·不扩张 | 25.57 | -0.37 | 9 | 6 |
| M2 重检 + 2px 膨胀 | 25.15 | -0.79 | 11 | 4 |
| M3 只 m1 bbox 扩张 | 23.87 | **-2.07** | 13 | 4 |
| **M4 当前实现**（m2d 扩张 ∪ m1 扩张） | 24.35 | -1.59 | 12 | 6 |
| M5 m2d 不扩张 ∪ m1 扩张 | 24.21 | -1.73 | 12 | 6 |

**两条硬结论：**
1. **bbox 扩张（`_refine_expand`）是回归主元凶**（M3 最差；M1 去掉扩张后回归从 -1.59 收窄到 -0.37）。
2. 但即使最保守的 M1 仍 9/30 变差 → **stage2 无条件开启必然净亏**。

### 3.2 待办清单（按优先级）

- [x] **P0｜撤销 `refine` 默认值**（2026-09-12 20:53 已完成，46/46 测试仍全过）：
      `web/index.html` 的 `selected` 移回 `auto`；`web/app.js:6150` 兜底值改回 `'auto'`；
      `server/routers/dewatermark.py:123` `Form("refine")` → `Form("auto")` + docstring 补齐风险说明。
      *理由：基准证明 refine 均值低于 auto 且 12/30 变差，默认开启等于给用户埋雷。*
- [ ] **P0｜给 stage2 加自适应闸门**（推荐方向）：只在「stage1 后残影仍强」时才跑 stage2。
      候选判据：stage1 输出在 add 区内的残差能量 / 重检 mask 占比 / 底图复杂度（见 4.4）。
      目标：难例（浅底半透明白字）拿到 refine 的收益，简单/照片底图自动退回 stage1（零回归）。
- [ ] **P1｜按闸门结论收缩 `_refine_expand` 外扩幅度**（当前 40% 太激进；sweep 显示 0% 仍回归 →
      大概率应改为「仅在闸门放行时才允许扩张，且幅度降到 10~20%」）。
- [ ] **P1｜重跑 30 样本基准**，验收标准：**refine 的 worse 数必须 ≤ auto（=0），且难例（real_wm.png）明显优于 auto**。
- [ ] **P1｜变异测试**：改完参数后必须让断言变红，确认测试有效（见 4.2）。
- [ ] **P2｜VERSION → 1.0.16**，构建 `bash desktop/build_mac.sh`（~12.5min，注意环境变量，见 4.3）、
      `bash desktop/deploy_mac.sh --no-build` 部署、处理 TCC 弹窗、真机 curl 核验 `/api/dw/image`。
- [ ] **P2｜commit + push app-dev**（当前 5 文件改动未提交；**先解决 P0 再提交**）。
- [ ] **P3｜UI 提示**：对「浅底+半透明白字」这类传统算法天花板场景，主动提示切 AI 引擎（LaMa）。
- [ ] **P3｜发布**：线上仍 1.0.13，问用户是否 `publish_update.sh` 发增量更新。

---

## 4. 关键约束 / 修改要求 / 注意事项

### 4.1 分支与工作树铁律
- **默认做 app-dev（桌面端）**；转 web-dev 必须先问用户。两分支不 merge，只 cherry-pick。
- 桌面构建必须在 `/Users/suixindelang/WorkBuddy/video-downloader-app`（app-dev 工作树）。
- 改完代码先跑：`.build_venv/bin/python -m pyflakes server/` → undefined name 归零，再构建。
- 发版收尾必查：`git status --short` 空 → `git log @{u}..HEAD` 空。
- **发布绝不自动**（`publish_update.sh` 需手工二次确认）。

### 4.2 测试铁律
- 跑测试：`.build_venv/bin/python server/tests/test_dewatermark_core.py`（当前 46/46 过）。
- 全量离线：`bash server/tests/run_offline_tests.sh`（需 `VDL_DATA_DIR`；**硬编码列表**，新增 `test_*.py` 必须手动加 `run_one`，否则静默不跑）。
- **改参数必须做变异测试确认断言会红**，否则测试是摆设。
- 测试绝不向用户家目录写（用 fake_home / tmp）。

### 4.3 构建 / 部署 / 真机
- 全量构建：`COMMENTARY_PIPELINE_DIR="/Users/suixindelang/WorkBuddy/问问题/commentary-pipeline" bash desktop/build_mac.sh`（~12.5min / 683MB）。**绝不加 `--collect-all onnx`**。
- 纯前端热更 14s：`bash desktop/refresh_web.sh --deploy`（重注入 `var fp` 2 处必须 `/g`）。
- 部署：`bash desktop/deploy_mac.sh`（ditto，勿 `cp -R` / `rm -rf`）。别用 `--version` 验证（会启动应用）。
- ⚠️ **TCC 弹窗**：改名/重签后首次启动会弹「视频工坊.app 想要访问『下载』文件夹」，
  **未点掉时后端线程卡在 `open()` 不绑端口**（易误判启动失败）。自动化点击会被安全策略拒绝，需用户手动点「好」。
- 🚨 **沙盒 curl 本机必须 `--noproxy '*'`**，否则 `upstream connect failed`。

### 4.4 算法侧已验证的结论（别重复踩）
- **双边滤波保边**是突破点：`cv2.bilateralFilter(g, d=15, sigmaColor=100, sigmaSpace=100)` 作背景估计，
  笔画不被吃进背景 → 残差可测，难例召回 68%→95.6%。但它只在**难例**上有贡献，30 样本均值上被中值主导。
- **bbox 外扩 40% 在复杂照片底图上必然毁图**（吃掉真实画面），是 refine 回归主因。
- **迭代 / 过膨胀 / bbox 填充**均验证无效或反伤：极限阈值召回仅 74% 且 PSNR 掉到 29；
  3 遍迭代是甜点（24.2）超 3 遍毁图；bbox 填充 20.7 更差。
- **复杂度闸门初测区分度不足**（很多样本测得 0.00，因为水印 mask 盖满了区域）→
  换判据时应基于「stage1 输出 vs 原图在 add 区内的残差能量」而非底图方差。
- **cv2.inpaint 天花板**：浅底 + 半透明粗笔画汉字（如「素材网」）彻底清零**必须走 AI（LaMa）**，
  传统路径最多做到「极淡残影」。

### 4.5 本机环境坑
- macOS 无 `timeout`；grep 匹配不到中文 pattern；zsh glob 无匹配会中断整条命令；禁 `ps aux`（用 lsof/pgrep）。
- bash `${VAR}` 后紧跟全角字符必须写 braces；ssh 不加 `-n` 会吃 stdin。
- 后台进程活不过工具调用；长任务需前台跑或 `run_in_background`。
- 8GB 内存：构建期间别启动 App。
- 前端 UI 验证别用本机 Chrome 无头（常年不可用），用 PyObjC + WKWebView 离屏快照
  （脚本见 skill `vdl-web-ui-visual-verify` 的 `scripts/wk_shot.py`）。

---

## 5. 接手第一步（建议）

1. 先 `git diff` 确认 5 个文件改动在位（`git status --short` 应列出 5 个 M）。
2. ~~P0 撤销 refine 默认值~~ **已完成**（20:53），下一步直接进入闸门。
3. **P0 实现自适应闸门**：在 `_refine_pipeline` 的 stage2 前加判据，只在「stage1 后 add 区内残差能量仍高」
   时才执行 stage2，否则直接返回 out1。
   建议判据（按可行性排序）：
   a. stage2 重检 mask 占 add 区面积比例 > 阈值（如 8%）；
   b. stage1 输出 vs 原图在 add 区内的残差能量（推荐，比底图方差区分度高）；
   c. 底图纹理复杂度（**已验证区分度不足**，不要用）。
4. 用 `bench_refine.py`（30 样本，要求 worse=0）+ `real_wm.png`（难例，要求明显优于 auto）双轨验收。
5. 通过后 commit → VERSION 1.0.16 → 构建 → 部署 → 真机核验。

## 6. 资产备份位置（防 `/tmp` 被清）

`/Users/suixindelang/WorkBuddy/.workbuddy/dwbench/` 已备份：
`mkbench.py`（30 样本基准台）、`bench_refine.py`、`diag_refine.py`、`sweep_expand.py`、
`gate_stage2.py`、`mask2_variants.py`、`real_wm.png` + `real_clean.png`（难例 + 真值）、
`refine_vs_auto.png`、`mask2_result.txt`。

⚠️ 基准台依赖真实照片 `/Users/suixindelang/Downloads/[已压缩]落地式展架副本.jpg`，
若该文件不存在需换一张真实照片作为 `photo` 底图。
