"""server/tests/test_llm_local_priority.py — 本地优先开关 + 字幕翻译分片/Ollama 兼容。

覆盖：
1. detect_ollama 探测（运行中 / 未运行）。
2. local_priority 开启 → 引擎回落本机优先（auto=MLX 本地优先→云端回落），无需 api_key 即注入 LLM_ENGINE；
   显式选 cloud 时本地优先开关应覆盖回落 auto。
3. engine=ollama 且 Ollama 在跑时，get_llm_config 整体切到 ollama 预设、清空 api_key，
   已保存 local_model 优先于探测列表第一项；失效模型回落并标记 local_model_stale。
4. 字幕翻译 Ollama 模式（base_url 含 11434）免 Key；云端模式无 Key 必须抛 ValueError。
5. 长字幕按块边界分片为多请求，重组后序号/时间轴不丢。
6. _split_srt_blocks 纯函数边界正确。
"""
import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
SERVER = REPO / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import llm_config as L
import subtitles as S


def _fake_resp(payload_bytes):
    class _R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return payload_bytes

    return _R()


def _reset_cache():
    L._ollama_cache = {"ts": 0.0, "val": None}


@contextmanager
def isolated_config_dir():
    """把配置目录临时指向系统临时目录，隔离用户真实的 ~/.video-downloader/llm_config.json。

    为什么必须隔离（本次踩中的坑）：llm_config.py:182 的规则是
        local_model = cfg.get("local_model") or 检测到的第一个模型
    **已保存的 local_model 优先于本次探测结果**。本机真实配置里存着 `qwen2.5vl:7b`，
    于是「断言应采用检测到的 qwen2.5:7b」必然失败——测试读到了家目录里的真实状态。
    隔离配置目录同时守住「离线测试不读不写家目录」的约定。
    """
    tmp = Path(tempfile.mkdtemp(prefix="vdl_llmcfg_"))
    orig = L._config_dir
    L._config_dir = lambda: tmp
    try:
        yield tmp
    finally:
        L._config_dir = orig
        shutil.rmtree(tmp, ignore_errors=True)


def test_detect_ollama_running():
    _reset_cache()
    data = json.dumps({"models": [{"name": "qwen2.5:7b"}, {"name": "llama3"}]}).encode()
    with mock.patch("urllib.request.urlopen", return_value=_fake_resp(data)):
        det = L.detect_ollama()
    assert det["running"] is True
    assert set(det["models"]) == {"qwen2.5:7b", "llama3"}


def test_detect_ollama_not_running():
    _reset_cache()
    with mock.patch("urllib.request.urlopen", side_effect=ConnectionRefusedError("refused")):
        det = L.detect_ollama()
    assert det["running"] is False
    assert det["models"] == []


def test_local_priority_override():
    """local_priority 开启 → 引擎回落本机优先（auto=MLX 本地优先→云端回落），无需 api_key 即可注入 LLM_ENGINE。

    注：新引擎模型下「本地优先」不再强切 Ollama（本机 Ollama Metal 后端已弃用），
    本地引擎统一走 MLX（engine=auto/mlx）；Ollama 仅在用户显式 engine=ollama 时启用。
    """
    _reset_cache()
    os.environ["VDL_LLM_LOCAL_PRIORITY"] = "1"
    try:
        with isolated_config_dir():   # 不读家目录
            cfg = L.get_llm_config()
        assert cfg["local_priority"] is True
        assert cfg["engine"] in ("auto", "mlx"), cfg["engine"]
        # 本地优先下即使无云端 key，inject_llm_env 也应注入 LLM_ENGINE（本地引擎可独立工作，零成本）
        env = {}
        L.inject_llm_env(env)
        assert env.get("LLM_ENGINE") in ("auto", "mlx"), env
    finally:
        os.environ.pop("VDL_LLM_LOCAL_PRIORITY", None)


def test_local_priority_overrides_cloud():
    """本地优先开关开启时，即便配置显式选了纯云端(engine=cloud)，也应回落本机优先(auto)。"""
    _reset_cache()
    os.environ["VDL_LLM_LOCAL_PRIORITY"] = "1"
    os.environ["VDL_LLM_ENGINE"] = "cloud"
    try:
        with isolated_config_dir():
            cfg = L.get_llm_config()
        assert cfg["engine"] == "auto", f"本地优先应覆盖 cloud，实际 {cfg['engine']}"
    finally:
        os.environ.pop("VDL_LLM_LOCAL_PRIORITY", None)
        os.environ.pop("VDL_LLM_ENGINE", None)


def test_saved_local_model_takes_precedence():
    """钉住规则（Ollama 引擎下）：已保存的 local_model **优先于**探测列表的第一个（前提：它还在本机）。

    用 detected=["llama3","qwen2.5:7b"] + saved="qwen2.5:7b" 来区分：
    若实现退化成「无脑取第一个」，这里会得到 llama3，从而暴露回归。
    （新引擎模型下 local_model 优先级仅在 engine=ollama 时生效；MLX 引擎用 MLX_MODEL_PATH 指定权重。）
    """
    _reset_cache()
    os.environ["VDL_LLM_ENGINE"] = "ollama"
    try:
        with isolated_config_dir() as d:
            (d / "llm_config.json").write_text(json.dumps({
                "provider": "deepseek", "api_key": "sk-test", "model": "deepseek-chat",
                "local_model": "qwen2.5:7b",
            }), encoding="utf-8")
            data = json.dumps({"models": [{"name": "llama3"}, {"name": "qwen2.5:7b"}]}).encode()
            with mock.patch("urllib.request.urlopen", return_value=_fake_resp(data)):
                cfg = L.get_llm_config()
        assert cfg["provider"] == "ollama", cfg["provider"]
        assert cfg["model"] == "qwen2.5:7b", f"已保存的选择应优先，实际 {cfg['model']}"
        assert "local_model_stale" not in cfg, "模型在本机就不该被判失效"
    finally:
        os.environ.pop("VDL_LLM_ENGINE", None)


# ------------------------------------------- 本机模型解析：模型被卸载/改名时的回落
#
# 前端「本机模型」是个 <select>，选项直接来自 detect_ollama 的结果，
# 所以保存值只在"该模型还装着"时才有意义。模型一旦被卸载/改名，
# 死守它只会让后续 LLM 调用拿到 Ollama 晦涩的 model not found 报错。

def test_resolve_local_model_exact_match():
    m, stale = L.resolve_local_model("qwen2.5:7b", ["llama3", "qwen2.5:7b"])
    assert m == "qwen2.5:7b" and stale is None
    print("✅ 精确命中时原样使用")


def test_resolve_local_model_base_name_alias():
    """同模型不同 tag（qwen2.5vl:7b vs :latest）→ 用探测到的变体，不误报失效。"""
    m, stale = L.resolve_local_model("qwen2.5vl:7b", ["qwen2.5vl:latest"])
    assert m == "qwen2.5vl:latest", m
    assert stale is None, "同基名不同 tag 不应判为失效"
    print("✅ 同模型不同 tag 时改用可用变体")


def test_resolve_local_model_stale_falls_back():
    m, stale = L.resolve_local_model("gone-model", ["qwen2.5:7b"])
    assert m == "qwen2.5:7b" and stale == "gone-model"
    print("✅ 模型失效时回落并回传原值")


def test_resolve_local_model_empty_inputs():
    assert L.resolve_local_model("x", []) == ("x", None), "无探测结果时保留原值，不擅自清空"
    assert L.resolve_local_model("", ["a", "b"]) == ("a", None), "无偏好时用第一个"
    print("✅ 边界输入不炸")


def test_stale_local_model_falls_back_in_config():
    """端到端（Ollama 引擎下）：保存的模型已不在本机 → 回落到可用模型，并标记 local_model_stale。"""
    _reset_cache()
    os.environ["VDL_LLM_ENGINE"] = "ollama"
    try:
        with isolated_config_dir() as d:
            (d / "llm_config.json").write_text(json.dumps({
                "provider": "deepseek", "api_key": "sk-test", "model": "deepseek-chat",
                "local_model": "gone-model",
            }), encoding="utf-8")
            data = json.dumps({"models": [{"name": "qwen2.5:7b"}]}).encode()
            with mock.patch("urllib.request.urlopen", return_value=_fake_resp(data)):
                cfg = L.get_llm_config()
        assert cfg["model"] == "qwen2.5:7b", cfg["model"]
        assert cfg["local_model_stale"] == "gone-model", cfg
        assert cfg["api_key"] == "", "切到 ollama 分支应清空 api_key"
    finally:
        os.environ.pop("VDL_LLM_ENGINE", None)
    print("✅ 失效模型端到端回落")


def test_translate_ollama_no_key():
    captured = {}

    def fake_open(req, timeout=120):
        captured["url"] = req.full_url
        return _fake_resp(json.dumps({"choices": [{"message": {"content": "translated"}}]}).encode())

    with mock.patch("urllib.request.urlopen", side_effect=fake_open):
        out = S.translate_srt(
            "1\n00:00:01,000 --> 00:00:02,000\nhello\n\n2\n00:00:03,000 --> 00:00:04,000\nworld",
            api_key="", base_url="http://localhost:11434/v1", model="qwen2.5:7b",
        )
    assert out == "translated"
    assert "11434" in captured["url"]


def test_translate_cloud_requires_key():
    raised = False
    try:
        S.translate_srt(
            "1\n00:00:01,000 --> 00:00:02,000\nhi",
            api_key="", base_url="https://api.openai.com/v1", model="gpt-4o-mini",
        )
    except ValueError:
        raised = True
    assert raised, "云端无 key 应抛 ValueError"


def test_translate_chunking():
    block = "1\n00:00:01,000 --> 00:00:02,000\n" + ("这是一行需要翻译的字幕内容。" * 20) + "\n\n"
    text = block * 40  # 远 > 4000 字符
    calls = []

    def fake_open(req, timeout=120):
        body = json.loads(req.data.decode())
        content = body["messages"][0]["content"]
        # 模拟模型只回翻译后的字幕块（即 prompt 中 "\n\n" 之后的部分），不回指令
        chunk = content.split("\n\n", 1)[1] if "\n\n" in content else content
        calls.append(content)
        return _fake_resp(json.dumps({"choices": [{"message": {"content": chunk}}]}).encode())

    with mock.patch("urllib.request.urlopen", side_effect=fake_open):
        out = S.translate_srt(text, api_key="sk-test1234567890", base_url="https://api.openai.com/v1", model="gpt-4o-mini")
    assert len(calls) > 1, f"长字幕应分片为多请求，实际 {len(calls)}"
    assert all("字幕翻译" in c for c in calls)
    assert out.count("-->") == 40, f"重组后应保留 40 条时间轴，实际 {out.count('-->')}"


def test_split_srt_blocks():
    text = "1\n00:00:01,000 --> 00:00:02,000\nhi\n\n2\n00:00:03,000 --> 00:00:04,000\nyo"
    # max_chars 设小以强制分片（两个短块无法并入同一片）
    chunks = S._split_srt_blocks(text, 40)
    assert len(chunks) == 2, f"应分成 2 片，实际 {len(chunks)}"
    assert chunks[0].startswith("1")
    assert chunks[1].startswith("2")


if __name__ == "__main__":
    test_detect_ollama_running()
    test_detect_ollama_not_running()
    test_local_priority_override()
    test_saved_local_model_takes_precedence()
    test_resolve_local_model_exact_match()
    test_resolve_local_model_base_name_alias()
    test_resolve_local_model_stale_falls_back()
    test_resolve_local_model_empty_inputs()
    test_stale_local_model_falls_back_in_config()
    test_translate_ollama_no_key()
    test_translate_cloud_requires_key()
    test_translate_chunking()
    test_split_srt_blocks()
    print("✅ test_llm_local_priority 全部通过")
