"""server/tests/test_llm_truncation_retry.py — 云端输出异常兜底回归（2026-09-15）。

背景（两次真实故障）：
1) 14 分钟素材在云端 Stage2 跑解说生成时，completion 打满 16384（reasoning token
   吃掉绝大部分预算），content 被**拦腰截断在半句**上——5281 字、以 `"note": "开` 结尾
   的半截 JSON。raw **非空**，既有「content 为空 → 关思考重试」兜底不触发 → 任务失败。
2) 把兜底加完后做真实链路验证，发现**两个兜底其实全是死代码**：
   `_call_llm` 在 content 为空时是**抛 RuntimeError**（非流式分支与 _read_sse 尾部
   各一处），并不返回空串。原先直接 `raw = _call_llm(...)` 会让异常穿透
   `_call_llm_json`，后面那句 `if not (raw or "").strip()` 永远不可达。
   ——此前本文件的桩用「返回空串」模拟，正好绕开了真实行为，测试全绿但线上是死的。
   故本次把桩改成「抛异常」为主，并额外保留「返回空串」分支做防御性覆盖。

修复（管线侧）：
  * `_call_llm_json` 用 `_call_capture()` 包住调用，把「LLM 返回空内容」的抛错转成空串；
  * 兜底 1：content 全空 → `reasoning_effort="disabled"` 整体重跑一次；
  * 兜底 2：raw 非空但 `_LAST_TRUNCATED`（completion >= max_tokens*0.95）→ 同样重跑一次；
  * 两个兜底共用「最多重跑一次」计数，避免同一请求被重跑两遍白烧 token；
  * 重跑后仍空 → 抛带准确措辞的人话错误（不再出现「非合法 JSON」这种误导信息）。

覆盖：
  truncated_json_retries_without_thinking    截断 + 解析失败 → 关思考重跑并成功
  truncated_json_keeps_original_error       重跑仍失败 → 抛错，不丢上下文
  no_retry_when_not_truncated               非截断坏 JSON → 不重试
  no_retry_when_thinking_already_off        已关思考仍截断 → 不无限重试
  empty_content_raised_by_call_llm          【核心】_call_llm 抛「返回空内容」→ 兜底 1 救回
  empty_content_returned_blank             _call_llm 返回空串（防御性）→ 兜底 1 救回
  no_second_retry_after_empty_retry         空内容重跑后仍截断 → 不再跑第三次
  no_retry_when_effort_none                 不传 effort → 不重试，且报错措辞准确
  usage_log_sets_truncation_flag            completion 达上限时 _LAST_TRUNCATED 置位

设计约束：管线源码在仓库外（COMMENTARY_PIPELINE_DIR），找不到时**跳过**而非失败，
避免在没有管线源码的环境（如纯后端 CI）里误报红灯。

运行：
    cd server && python tests/test_llm_truncation_retry.py
"""
import importlib.util
import os
import sys

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

_REPO_DIR = os.path.dirname(_SERVER_DIR)

# 让「本机非法 JSON → 回落云端」那一段不参与本文件（避免环境差异导致多一次调用）
os.environ["LLM_ENGINE"] = "cloud"


def _load_llm_script():
    """按 COMMENTARY_PIPELINE_DIR → 已知本机路径 → 仓库旁 的顺序找管线并加载。"""
    cands = [
        os.environ.get("COMMENTARY_PIPELINE_DIR", "").strip(),
        "/Users/suixindelang/WorkBuddy/问问题/commentary-pipeline",
        os.path.join(_REPO_DIR, "commentary-pipeline"),
        os.path.join(os.path.dirname(_REPO_DIR), "commentary-pipeline"),
    ]
    for c in cands:
        if not c:
            continue
        p = os.path.join(c, "scripts", "llm_script.py")
        if os.path.isfile(p):
            # 管线内部用「同目录裸 import」（import commentary_options），
            # 必须先把 scripts/ 挂进 sys.path，否则 exec_module 阶段就 ModuleNotFound。
            _sd = os.path.dirname(p)
            if _sd not in sys.path:
                sys.path.insert(0, _sd)
            spec = importlib.util.spec_from_file_location("_vdl_llm_script_under_test", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    return None


MOD = _load_llm_script()

_TRUNCATED_JSON = (
    '{\n  "title": "祖母出手",\n  "segments": [\n'
    '    {"start": 0.0, "end": 8.0, "narration": "明兰一开口就把伤疤亮了出来", '
    '"note": "开'
)
_GOOD_JSON = '{"title": "祖母出手", "segments": [{"start": 0.0, "end": 8.0, "narration": "完整"}]}'
# 标记：模拟 _call_llm 真实行为——content 为空时**抛** RuntimeError（而非返回空串）
_RAISE_EMPTY = "«RAISE_EMPTY»"


def _install_fake(mod, script):
    """把 _call_llm 换成可控替身。

    script 为 [(返回文本或 _RAISE_EMPTY, 是否截断), ...] 序列；
    退化为字符串时视为 (文本, False)。
    """
    calls = []
    it = iter(script)

    def _fake(system_prompt, user_prompt, temperature=0.1,
              reasoning_effort=None, max_tokens=0, **kwargs):
        calls.append({"reasoning_effort": reasoning_effort, "max_tokens": max_tokens})
        try:
            item = next(it)
        except StopIteration:
            item = ("", False)
        text, truncated = item if isinstance(item, tuple) else (item, False)
        mod._LAST_TRUNCATED = truncated
        if text == _RAISE_EMPTY:
            # 🔴 还原真实行为：_call_llm 在 content 为空时抛异常。
            # 上一版测试这里返回 ""，正好绕开了 `_call_llm_json` 的死代码缺陷。
            raise RuntimeError(
                f"{mod._EMPTY_CONTENT_MARK}（可能输出被截断或思考占满 token 预算）。"
                "请降低推理强度后重试。"
            )
        return text

    mod._call_llm = _fake
    return calls


def truncated_json_retries_without_thinking():
    """截断 + 解析失败 → 自动以「关闭思考」整体重跑一次，并采用重跑结果。"""
    calls = _install_fake(MOD, [(_TRUNCATED_JSON, True), (_GOOD_JSON, False)])
    out = MOD._call_llm_json("sys", "user", reasoning_effort="low")
    assert out.get("title") == "祖母出手", out
    assert len(calls) == 2, f"应重跑一次，实际调用 {len(calls)} 次"
    assert calls[0]["reasoning_effort"] == "low", calls[0]
    assert calls[1]["reasoning_effort"] == "disabled", f"重跑必须关闭思考：{calls[1]}"
    return "截断后关思考重跑，成功救回"


def truncated_json_keeps_original_error():
    """重跑仍失败 → 抛错，不静默吞掉，且只重试一次。"""
    calls = _install_fake(MOD, [(_TRUNCATED_JSON, True), (_TRUNCATED_JSON, True)])
    try:
        MOD._call_llm_json("sys", "user", reasoning_effort="low")
    except RuntimeError:
        assert len(calls) == 2, f"应只重试一次，实际 {len(calls)} 次"
        return "重跑仍坏 → 明确报错，不无限重试"
    raise AssertionError("重跑仍失败时应抛 RuntimeError")


def no_retry_when_not_truncated():
    """非截断导致的坏 JSON（如本机小模型乱写）→ 不重试，直接抛。"""
    calls = _install_fake(MOD, [("这不是 JSON", False)])
    try:
        MOD._call_llm_json("sys", "user", reasoning_effort="low")
    except RuntimeError:
        assert len(calls) == 1, f"非截断不应重试，实际 {len(calls)} 次"
        return "非截断坏 JSON → 不重试"
    raise AssertionError("坏 JSON 应抛 RuntimeError")


def no_retry_when_thinking_already_off():
    """已经关了思考还截断 → 不再重试（重试也无意义，避免白烧 token）。"""
    calls = _install_fake(MOD, [(_TRUNCATED_JSON, True)])
    try:
        MOD._call_llm_json("sys", "user", reasoning_effort="disabled")
    except RuntimeError:
        assert len(calls) == 1, f"已关思考不应重试，实际 {len(calls)} 次"
        return "已关思考仍截断 → 不重试"
    raise AssertionError("坏 JSON 应抛 RuntimeError")


def empty_content_raised_by_call_llm():
    """【核心回归】_call_llm 抛「返回空内容」→ 兜底 1 必须能接住并重跑成功。

    这正是 2026-09-15 真实链路实测发现的死代码场景：异常穿透导致兜底永不触发。
    """
    calls = _install_fake(MOD, [(_RAISE_EMPTY, True), (_GOOD_JSON, False)])
    out = MOD._call_llm_json("sys", "user", reasoning_effort="low")
    assert out.get("title") == "祖母出手", out
    assert len(calls) == 2, f"抛空内容异常后应关思考重跑，实际 {len(calls)} 次"
    assert calls[1]["reasoning_effort"] == "disabled", calls[1]
    return "_call_llm 抛「空内容」→ 兜底 1 接住并救回（死代码已消除）"


def empty_content_returned_blank():
    """防御性覆盖：若 _call_llm 改成返回空串，兜底 1 仍要工作。"""
    calls = _install_fake(MOD, [("", False), (_GOOD_JSON, False)])
    out = MOD._call_llm_json("sys", "user", reasoning_effort="low")
    assert out.get("title") == "祖母出手", out
    assert len(calls) == 2, calls
    assert calls[1]["reasoning_effort"] == "disabled", calls[1]
    return "空串返回路径同样被兜底"


def no_second_retry_after_empty_retry():
    """空内容已重跑一次后仍截断 → 不得再跑第三次（防白烧 token）。"""
    calls = _install_fake(MOD, [(_RAISE_EMPTY, True), (_TRUNCATED_JSON, True)])
    try:
        MOD._call_llm_json("sys", "user", reasoning_effort="low")
    except RuntimeError:
        assert len(calls) == 2, f"最多重跑一次，实际 {len(calls)} 次"
        return "重跑后仍坏 → 不跑第三次"
    raise AssertionError("两次都坏时应抛 RuntimeError")


def no_retry_when_effort_none():
    """不传 reasoning_effort（不加 thinking 字段）→ 不重试，且报错措辞不得谎称「已重跑」。"""
    calls = _install_fake(MOD, [(_RAISE_EMPTY, True)])
    try:
        MOD._call_llm_json("sys", "user", reasoning_effort=None)
    except RuntimeError as e:
        assert len(calls) == 1, f"未开启思考不应重试，实际 {len(calls)} 次"
        msg = str(e)
        assert "未开启思考" in msg, f"措辞应说明未重跑，实际：{msg[:80]}"
        assert "已自动关闭思考重跑一次" not in msg, f"不应谎称已重跑：{msg[:80]}"
        return "不传 effort → 不重试，措辞准确"
    raise AssertionError("应抛 RuntimeError")


def usage_log_sets_truncation_flag():
    """completion 达到 max_tokens 时 _LAST_TRUNCATED 必须置位（重跑判定的数据源）。"""
    MOD._LAST_TRUNCATED = False
    MOD._log_usage({"prompt_tokens": 100, "completion_tokens": 16384}, 16384)
    assert MOD._LAST_TRUNCATED is True, "撞满上限应判定为截断"
    MOD._LAST_TRUNCATED = False
    MOD._log_usage({"prompt_tokens": 100, "completion_tokens": 200}, 16384)
    assert MOD._LAST_TRUNCATED is False, "远未到上限不应判为截断"
    return "截断标志按 completion/上限 正确置位"


def empty_content_mark_matches_call_llm():
    """`_EMPTY_CONTENT_MARK` 必须真的出现在 _call_llm 源码里的抛错文案中。

    否则 `_call_capture` 的字符串匹配会失效、兜底重新变回死代码——这是本文件
    最该守住的一条：文案与匹配常量一旦漂移，测试锤不到、线上又静默失效。
    """
    assert getattr(MOD, "_EMPTY_CONTENT_MARK", ""), "缺少 _EMPTY_CONTENT_MARK 常量"
    here = os.path.dirname(os.path.abspath(MOD.__file__))
    src = open(os.path.join(here, "llm_script.py"), encoding="utf-8").read()
    # 抛错文案在源码里是字符串字面量，收紧到「出现且至少两处」（非流式 + SSE 各一处）
    n = src.count(MOD._EMPTY_CONTENT_MARK)
    assert n >= 3, f"「{MOD._EMPTY_CONTENT_MARK}」在 llm_script.py 仅出现 {n} 次，疑文案已改但常量未同步"
    return f"空内容文案与匹配常量一致（源码中 {n} 处）"


def main():
    if MOD is None:
        print("⚠️  未找到解说管线源码（COMMENTARY_PIPELINE_DIR），跳过本用例")
        return 0
    cases = [
        truncated_json_retries_without_thinking,
        truncated_json_keeps_original_error,
        no_retry_when_not_truncated,
        no_retry_when_thinking_already_off,
        empty_content_raised_by_call_llm,
        empty_content_returned_blank,
        no_second_retry_after_empty_retry,
        no_retry_when_effort_none,
        usage_log_sets_truncation_flag,
        empty_content_mark_matches_call_llm,
    ]
    failed = 0
    for fn in cases:
        try:
            print(f"  ✅ {fn.__name__}: {fn()}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ❌ {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n通过 {len(cases) - failed}/{len(cases)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
