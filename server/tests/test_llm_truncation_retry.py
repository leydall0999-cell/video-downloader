"""server/tests/test_llm_truncation_retry.py — 「输出被 max_tokens 截断」兜底回归（2026-09-15）。

背景（真实故障）：
14 分钟素材在云端 Stage2 上跑解说生成时，completion 打满 16384（reasoning token
吃掉了绝大部分预算），content 被**拦腰截断在半句**上——返回了 5281 字、以
`"note": "开` 结尾的半截 JSON。此时 raw **非空**，所以既有「content 为空 → 关思考重试」
的兜底根本不会触发，直接抛「LLM 输出非合法 JSON」，整条任务失败（额度虽已退还，但用户白等）。

修复：`_call_llm_json` 记住本次调用是否被截断（`_LAST_TRUNCATED`，由 `_log_usage`
按 completion >= max_tokens*0.95 判定）；解析失败且确属截断时，自动以
`reasoning_effort="disabled"` 整体重跑一次——关掉思考后 content 能拿到完整预算。

覆盖：
  truncated_json_retries_without_thinking   截断 + 解析失败 → 关思考重跑并成功
  truncated_json_keeps_original_error      重跑仍失败 → 抛「重跑那次」的错，不丢上下文
  no_retry_when_not_truncated              非截断导致的坏 JSON → 不重试，直接抛
  no_retry_when_thinking_already_off       已关思考仍截断 → 不无限重试
  empty_content_still_falls_back           既有「content 全空 → 关思考重试」不回归
  usage_log_sets_truncation_flag           completion 达上限时 _LAST_TRUNCATED 置位

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


def _install_fake(mod, script):
    """把 _call_llm 换成可控替身。script 为 [(返回文本, 是否截断), ...] 序列。"""
    calls = []
    it = iter(script)

    def _fake(system_prompt, user_prompt, temperature=0.1,
              reasoning_effort=None, max_tokens=0, **kwargs):
        calls.append({"reasoning_effort": reasoning_effort, "max_tokens": max_tokens})
        try:
            text, truncated = next(it)
        except StopIteration:
            text, truncated = "", False
        mod._LAST_TRUNCATED = truncated
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
    """重跑仍失败 → 抛错（抛的是重跑那次的错），不静默吞掉。"""
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


def empty_content_still_falls_back():
    """既有兜底不回归：content 全空 → 关思考重试一次并成功。"""
    calls = _install_fake(MOD, [("", False), (_GOOD_JSON, False)])
    out = MOD._call_llm_json("sys", "user", reasoning_effort="low")
    assert out.get("title") == "祖母出手", out
    assert len(calls) == 2, calls
    assert calls[1]["reasoning_effort"] == "disabled", calls[1]
    return "空 content → 关思考重试（既有兜底未回归）"


def usage_log_sets_truncation_flag():
    """completion 达到 max_tokens 时 _LAST_TRUNCATED 必须置位（重跑判定的数据源）。"""
    MOD._LAST_TRUNCATED = False
    MOD._log_usage({"prompt_tokens": 100, "completion_tokens": 16384}, 16384)
    assert MOD._LAST_TRUNCATED is True, "撞满上限应判定为截断"
    MOD._LAST_TRUNCATED = False
    MOD._log_usage({"prompt_tokens": 100, "completion_tokens": 200}, 16384)
    assert MOD._LAST_TRUNCATED is False, "远未到上限不应判为截断"
    return "截断标志按 completion/上限 正确置位"


def main():
    if MOD is None:
        print("⚠️  未找到解说管线源码（COMMENTARY_PIPELINE_DIR），跳过本用例")
        return 0
    cases = [
        truncated_json_retries_without_thinking,
        truncated_json_keeps_original_error,
        no_retry_when_not_truncated,
        no_retry_when_thinking_already_off,
        empty_content_still_falls_back,
        usage_log_sets_truncation_flag,
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
