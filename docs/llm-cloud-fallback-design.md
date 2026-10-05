# 解说 LLM 引擎：本机优先 → 云端自动回落 设计方案

> 状态：设计稿（未改代码）。对应产品铁律「这是给所有人用的产品，不能只考虑本机，要有多套方案」。
> 关联文件：`commentary-pipeline/scripts/llm_script.py`、`server/llm_config.py`、`server/app.py`。

## 1. 目标与原则
- 落实产品铁律：解说 LLM 必须**多方案 + 自动路由 + 失败回落**，弱机/无 GPU 用户不能被本机 OOM 卡死。
- 默认**本机优先**（零成本、隐私好）；本机确定性失败（OOM/没装/没起）时**自动**转云端。
- 用户可显式选择模式（本机优先 / 仅本机 / 仅云端）。
- 成本护栏：云端回落**只在「本机确实不行」时触发**，默认用最便宜模型 + 一次性费用提示（因为云端按量计费，用户明确在意成本）。

## 2. 什么叫「本机失败」——回落触发条件
在 `_call_llm` 内对**本机 profile** 分级判定：
- **确定性失败（直接跳云端，不浪费重试）**：
  - OOM 崩溃：响应体/日志命中 `_OLLAMA_CRASH_MARKERS`（`GGML_ASSERT` / `out of memory` / `llama-server process has terminated` 等）。
  - 模型不存在：`/api/tags` 无配置的本机模型（model not found / 404）。
  - Ollama 没起：`http://localhost:11434` 连接被拒（ConnectionRefused）。
  - 内存闸门：`_guard_local_model()` 判定 RAM 装不下（`ratio>0.55`）→ RuntimeError。
- **瞬时失败（本机内部退避重试，耗尽再跳）**：
  - 网络抖动、超时、5xx（非崩溃指纹）、429。走现有 `_BACKOFF` 退避。

## 3. 配置模型
`~/.video-downloader/llm_config.json` 新增 `mode` 与 `fallback` 段（与现有字段平级）：
```json
{
  "provider": "ollama",
  "local_priority": true,
  "local_model": "qwen2.5vl:3b",
  "mode": "auto",                 // auto | local_only | cloud_only
  "fallback": {
    "enabled": true,
    "provider": "deepseek",       // 走 PROVIDER_PRESETS，默认最便宜
    "model": "deepseek-v4-flash", // 成本护栏：默认最廉价的文本模型
    "api_key": "",                // VDL 托管 Key（服务端注入，用户不可见/不可填）
    "notice": true                // 切云端时弹一次性费用提示
  }
}
```
环境变量覆盖（运维/容器最终裁决，沿用现有风格）：
`VDL_LLM_MODE`、`VDL_LLM_FALLBACK_ENABLED`、`VDL_LLM_FALLBACK_PROVIDER`、
`VDL_LLM_FALLBACK_BASE_URL`、`VDL_LLM_FALLBACK_MODEL`、`VDL_LLM_FALLBACK_API_KEY`。

成本建议：默认 `deepseek-v4-flash`（峰时外 ~$0.44/1M in，一段 10 分钟视频的解说脚本通常 <$0.01）。
`qwen`(dashscope) / `openai` 备选；`PROVIDER_PRESETS` 已含这些，**无需新增预设**。

## 4. 三种模式（满足「多方案 + 用户选择」）
- `auto`（默认）：本机优先，本机确定性失败 → 云端回落；云端也失败 → 人话报错。
- `local_only`：等价于当前写死行为，绝不碰云端（给拒绝云端的用户）。
- `cloud_only`：弱机/无 GPU 用户一键走云端，根本不试本机。

## 5. 执行流程（伪代码）
```python
profiles = _build_profiles()   # [local] (+ [cloud] if mode!=local_only and fallback ok)
_SESSION_FELL_BACK = False

for each script segment:
    if _SESSION_FELL_BACK:
        use cloud profile directly
    else:
        try:
            _guard_local_model(local)        # 内存闸门；失败=确定性
            resp = _try_request(local)       # 内部对瞬时错误退避重试
        except DeterministicLocalError:
            if cloud available:
                switch_to_cloud(); _SESSION_FELL_BACK = True; _emit_fallback_notice()
            else:
                raise human_error()
        except TransientExhausted:
            if cloud available:
                switch_to_cloud(); _SESSION_FELL_BACK = True; _emit_fallback_notice()
            else:
                raise
    # cloud profile attempt（同结构，无进一步回落）
```
- **Sticky**：一旦回落，本次进程内后续所有 segment 直走云端（避免每段都重试本机、省时省钱）。
- **Per-run 重置**：每次 `process.py` 启动重置 `_SESSION_FELL_BACK`，下轮重新先试本机（用户可能已装好模型）。

## 6. `llm_script.py` 改动点（仅设计，未改）
- 新增 `Profile(kind, base_url, model, api_key)` 结构。
- `_build_profiles()`：读 `LLM_BASE_URL`(本机) + `LLM_FALLBACK_*`，按 mode 组装**有序**列表。
- `_call_llm()`：把现有「`_attempt % len(base_urls)` 轮询」升级为「按 profile 顺序 + 确定性/瞬时分流」。
- `_classify_error(e, base_url)`：返回 `deterministic_local` / `transient` / `cloud_error`。
- `_guard_local_model` 抛出的 RuntimeError 归类为 `deterministic_local`，触发回落而非直接失败。
- 新增模块级 `_SESSION_FELL_BACK` 标志 + `_emit_fallback_notice()`（打印一次
  `⚠️ 本机模型失败，已自动切换云端，本次解说将产生少量云端费用`）。

## 7. `server/llm_config.py` 改动点
- `get_llm_config()`：读取 `mode` + `fallback.*` 字段（JSON→环境变量覆盖，沿用现有风格）。
- `inject_llm_env(env)`：当 `mode != local_only` 且 fallback 有 key 时，额外注入
  `LLM_FALLBACK_BASE_URL / MODEL / API_KEY / ENABLED`。
- `PROVIDER_PRESETS` 已含 deepseek/qwen/openai，fallback 直接复用。

## 8. 前端 UI（让「所有人」能选）
- 解说 LLM 设置新增「运行模式」下拉：本机优先(自动回落) / 仅本机 / 仅云端。
- 选「本机优先/仅云端」时显示「回落/云端服务商 + Key + 模型」输入（复用现有 provider 预设 UI）。
- 状态条：回落发生时显示「本机不可用，已切云端」徽标。

## 9. 成本护栏
- 默认回落模型 = 最便宜（deepseek-v4-flash / qwen-plus）。
- 一次性提示：切云端时打印/显示费用提示，不在后台静默烧钱。
- 可选 `fallback.max_tokens` 上限（默认沿用解说脚本既有 max_tokens）。
- 透明：日志标明每段用的是本机还是云端，便于审计。

## 10. 边界与坑
- **别把瞬时错误当确定性**：429/5xx 退避后可能成功，先本机重试，别一上来就烧云端。
- **OOM 是确定性的**：7B 在 8GB 上 retry 4 次必 4 次崩，必须一次判定就跳云端，否则白等 + 误导。
- **Key 缺失**：fallback 无 Key 时不注入、不报错，等同于 local_only（符合「无 Key 不污染环境」现有约定）。
- **视觉模块不受影响**：视觉理解本就走 `vision_config.json`(云端 qwen-vl-max)，与本次解说 LLM 回落是两套独立配置，互不干扰。
- **Sticky 防抖**：进程内 sticky 避免反复横跳；跨进程重置，保证装好模型后能回到本机。

## 11. 验证计划
- 单测：① 本机 7B 在 8GB → 内存闸门触发 → 回落云端成功；② Ollama 未起 → 连接拒 → 回落；
  ③ 云端 Key 无效 → 人话报错；④ local_only 模式本机失败直接报错不烧云端；
  ⑤ cloud_only 根本不试本机。
- 集成：断网本机 7B + 填 DeepSeek Key → 跑一段解说，确认输出正常、日志有「已切云端」提示、费用 <$0.01。
- 加入 `server/tests/run_offline_tests.sh` 的 `run_one` 硬列表（沿用项目测试约定）。

---

## 12. 会员 / 配额模式（云端按量计费必须配套闸门）

### 12.1 为什么需要配额
云端按量计费（见 §12.5 费用测算）。若免费用户无限走云端，会被刷爆。会员/配额是「多方案铁律」在商业层的落地：本机优先省钱，但云端保底必须有闸门。

### 12.2 云端供给模型：**仅 VDL 托管（无 BYOK）**
> ⚠️ **用户明确：不要 BYOK（用户自填 Key）功能**。所有云端调用一律走 **VDL 托管云端（共享 Key，VDL 出钱）**，因此**配额对所有用户生效**（没有「自付无限」的旁路）。
- 好处：计费统一、配额可管、免费送 3 次才成立；避免用户乱填 Key 导致的「503/超支/调试地狱」。
- 代价：VDL 承担全部云端成本 → 更凸显配额闸门与「视觉分档」的必要性（见 §12.3.1）。

### 12.3 计量单位：云端计费事件
一次解说任务中，**实际打到 VDL 托管云端 LLM** = 1 个「云端事件」：
- `auto`：本机成功 → 0；本机失败回落云端 → 1。
- `cloud_only`：→ 1（强制走云端）。
- `local_only`：→ 0（永不）。
- ⚠️ **视觉理解**（vision_config）是独立 always-cloud 模块，**免费与会员同模型、不降档**（见 §12.3.1），**不计入本「文本 LLM 云端事件」配额**。

#### 12.3.1 视觉理解：免费与会员同档、绝不降档（用户明确规则）
| 档位 | 模型 | 输出质量 | 提示 |
|---|---|---|---|
| **会员** | `qwen-vl-max` | 高（画面/物体/场景理解精准） | 无 |
| **免费** | `qwen-vl-max`（**不降档**） | 高（与会员完全一致） | 无降质提示 |

- **规则（用户明确）**：免费用户与会员**视觉质量完全一致、绝不降档**。理由：**试用体验差会直接影响付费转化**，降档牺牲质量等于自毁转化漏斗。免费与会员的唯一差异是**用量配额（见 §12.4），不是质量**。
- **不消耗**终身 3 次文本云端配额（视觉是独立廉价通道，但本次按全质量计）。
- **成本靠配额闸门控，不靠降质**：视觉全质量约 ¥0.15/半小时；免费用户通过「3 次终身云端事件 + 1 次每日 auto」限制**用量**来压成本。
- **实现**：`vision_client.py` **不再按会员切换模型**，统一 `qwen-vl-max`；配额只控用量。免费用户可在**用完云端配额时**弹「升级会员享无限云端」，而非在使用中降质。
- **freemium 原则**：限「量」不限「质」——免费给满血体验，只限制能跑几次，避免坏体验劝退付费。

### 12.4 免费用户配额（按用户要求）
| 配额 | 数量 | 重置 | 消耗场景 / 说明 |
|---|---|---|---|
| 终身云端事件 | **3 次** | 不可恢复（跨重装/换机需账号绑定） | auto 回落云端 / cloud_only 各 1 次 |
| 每日 auto 运行 | **1 次** | 自然日重置 | 每天第 1 次 auto 运行（不论本机成败） |
| **单条上传视频时长** | **≤ 30 分钟** | 每次上传校验 | 免费用户单个解说视频时长上限；超限在前端/解析阶段拦截并提示升级会员 |

> 假设（待确认）：「每日 auto 1 次」指**每天 1 次免费 auto 运行额度**；该次若本机成功则不耗云端额度，若回落云端则同时扣 1 次终身云端额度。

### 12.5 超限闸门的三种情形
- **终身云端 = 0**：拦截所有需云端操作。`cloud_only` 直接拒绝；`auto` 回落云端被拦 → 改走 `local_only` 行为。提示：「免费云端额度已用完，请开通会员或自配本机模型」。
- **每日 auto = 0**（已用满 1 次）：当天第 2 次起 `auto` 仍可用，但**强制 local_only**（不回落云端）；本机也失败 → 人话报错，不烧钱。
- **免费上传视频 > 30 分钟**：前端上传/解析阶段即拦截，提示「免费版单条视频上限 30 分钟，升级会员解锁长视频」；会员不限时长。
- **会员（付费）**：解除上述全部限制，云端按套餐无限，保留一次性费用提示。

### 12.6 费用测算（半小时典型用量）★用户最关心的
单次 10 分钟解说视频用量估算：文本 LLM（ASR 校正+脚本）~8K tokens，视觉理解（~20 帧）~10K tokens。半小时 ≈ 2 个视频 → 文本 ~16K + 视觉 ~20K tokens。

| 层级 | 文本 LLM | 视觉模型 | 半小时成本 | 说明 |
|---|---|---|---|---|
| **免费层** | deepseek-v4-flash | qwen-vl-max（满血，**不降档**） | **≈¥0.2** | 质量与会员完全一致，差异仅配额（3 次终身 + 1 次每日 auto） |
| **会员层** | deepseek-v4-flash | qwen-vl-max（满血） | **≈¥0.2** | 视觉拉满，解说画面贴合度高；用量无限 |

> 三套候选模型组合的优缺点对比见 §12.6.1。

#### 12.6.1 选定模型组合（仅 B；A / C 已废弃，不采用）
**B. 最终选定：deepseek-v4-flash + qwen-vl-max（≈¥0.2/半小时）** ← 免费与会员统一采用
- ✅ 文本便宜 + 视觉最强，质量/成本平衡最佳；vl-max 视觉精准，解说画面贴合度高。
- ✅ **免费与会员同质量（不降档）**，保障试用转化；成本靠配额闸门（3 次终身 + 1 次每日）控「量」而非降「质」。
- ❌ 视觉占成本 75%（¥0.15/¥0.2），但免费配额（终身 3 次 + 每日 1 次）已把总支出压到可忽略。
- ⚠️ **A（deepseek-v4-flash + qwen3-vl-flash，降质伤转化）与 C（qwen-plus + qwen-vl-plus，最贵无优势）已废弃，不再保留**。

**结论：单用户半小时云端成本 ≈ ¥0.2（免费与会员同价）。** 免费用户受「3 次终身 + 每日 1 次 auto」配额闸门限制**用量**，而非降质；即便 1000 名免费用户各用满 3 次终身额度，总支出也仅数百元。**配额的核心是「防单用户无限刷」，质量对所有人都满血**。

### 12.7 持久化与开放问题（需拍板）
1. **配额存哪**：本地 `~/.video-downloader/quota.json` 即可；跨设备/防作弊需账号系统（更大范围，后续迭代）。
2. **会员档位与定价**：待定（1+3 变现模型，订阅为主）；建议「会员 = 云端无限 + 视觉 qwen-vl-max 满血 + 多本机模型档位」。
3. **freemium 红线（用户明确）**：免费与会员**质量一致、绝不降档**；只允许限「量」（配额），不允许限「质」（降模型）。理由：试用体验差会劝退付费。

### 12.8 实现落点（设计，未改代码）
- 新增 `quota.json` + `quota.py`：`check_and_consume(event_type)` / `daily_auto_remaining()` / `lifetime_cloud_remaining()`。
- `llm_script.py` 回落前调用 `quota.check_and_consume("cloud_event")`：返回 `denied` 则走 local_only 行为并弹提示。
- `vision_client.py` **统一** `qwen-vl-max`（不按会员切换模型）；配额只控用量，不控质量。
- 前端设置页：显示「剩余免费云端 X/3、今日 auto Y/1」+ 会员开通入口；**无「自填 Key」输入框**（已取消 BYOK）。
- **免费上传时长闸门（用户明确）**：前端上传前读取视频元数据获取时长，免费用户 > 30 分钟直接拦截并提示升级；服务端二次校验（防前端绕过）。会员不限。
