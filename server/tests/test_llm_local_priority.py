"""server/tests/test_llm_local_priority.py — 本地优先开关 + 字幕翻译分片/Ollama 兼容。

覆盖：
1. detect_ollama 探测（运行中 / 未运行）。
2. local_priority 开启且 Ollama 在跑时，get_llm_config 整体切到 ollama 预设、清空 api_key。
3. 字幕翻译 Ollama 模式（base_url 含 11434）免 Key；云端模式无 Key 必须抛 ValueError。
4. 长字幕按块边界分片为多请求，重组后序号/时间轴不丢。
5. _split_srt_blocks 纯函数边界正确。
"""
import json
import os
import sys
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
    _reset_cache()
    os.environ["VDL_LLM_LOCAL_PRIORITY"] = "1"
    try:
        data = json.dumps({"models": [{"name": "qwen2.5:7b"}]}).encode()
        with mock.patch("urllib.request.urlopen", return_value=_fake_resp(data)):
            cfg = L.get_llm_config()
        assert cfg["provider"] == "ollama", cfg["provider"]
        assert cfg["api_key"] == ""
        assert "11434" in cfg["base_url"], cfg["base_url"]
        assert cfg["model"] == "qwen2.5:7b"
        assert cfg["local_priority"] is True
    finally:
        os.environ.pop("VDL_LLM_LOCAL_PRIORITY", None)


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
    test_translate_ollama_no_key()
    test_translate_cloud_requires_key()
    test_translate_chunking()
    test_split_srt_blocks()
    print("✅ test_llm_local_priority 全部通过")
