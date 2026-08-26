"""tests/test_narrato_manager.py — NarratoManager 逻辑（mock 子进程/目录）。

覆盖：目录解析、config.toml 预填（DeepSeek+edge_tts）、key 写入/读取、
ensure_start 的状态分支（missing_dir / need_key / starting / ready）。
"""
import importlib
import sys
import types

import pytest


@pytest.fixture
def narrato(monkeypatch, tmp_path):
    # 独立临时目录当 NarratoAI 根；放一个最小 config.example.toml
    example = tmp_path / "config.example.toml"
    example.write_text(
        '[app]\n'
        'text_llm_provider = "openai"\n'
        'text_openai_model_name = "Pro/zai-org/GLM-5"\n'
        'text_openai_api_key = ""\n'
        'text_openai_base_url = "https://api.siliconflow.cn/v1"\n'
        '[ui]\n'
        'tts_engine = "indextts"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("VDL_NARRATOAI_DIR", str(tmp_path))
    mod = importlib.import_module("server.routers.narrato")
    importlib.reload(mod)
    # 重置单例
    mod._state["proc"] = None
    mod._state["launched"] = False
    monkeypatch.setattr(mod, "_resolve_dir", lambda: tmp_path)
    return mod


def test_resolve_dir_default(monkeypatch, tmp_path):
    monkeypatch.delenv("VDL_NARRATOAI_DIR", raising=False)
    monkeypatch.setattr(
        "server.routers.narrato.NARRATO_DEFAULT_DIR", tmp_path / "NarratoAI-0.8.7"
    )
    mod = importlib.import_module("server.routers.narrato")
    assert mod._resolve_dir() == tmp_path / "NarratoAI-0.8.7"


def test_ensure_config_prefills_deepseek_and_edge_tts(narrato, tmp_path):
    narrato._ensure_config(tmp_path)
    cfg = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert 'text_openai_base_url = "https://api.deepseek.com/v1"' in cfg
    assert 'text_openai_model_name = "deepseek-chat"' in cfg
    assert 'tts_engine = "edge_tts"' in cfg
    # key 留空
    assert 'text_openai_api_key = ""' in cfg


def test_ensure_config_handles_indented_toml(narrato, tmp_path):
    # NarratoAI 真实 config.example.toml 字段带缩进，必须能预填
    (tmp_path / "config.example.toml").write_text(
        "[text]\n"
        '    text_openai_model_name = "Pro/zai-org/GLM-5"\n'
        '    text_openai_api_key = ""\n'
        '    text_openai_base_url = "https://api.siliconflow.cn/v1"\n'
        "[ui]\n"
        '    tts_engine = "indextts"\n',
        encoding="utf-8",
    )
    (tmp_path / "config.toml").unlink(missing_ok=True)
    narrato._ensure_config(tmp_path)
    cfg = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert 'text_openai_base_url = "https://api.deepseek.com/v1"' in cfg
    assert 'text_openai_model_name = "deepseek-chat"' in cfg
    assert 'tts_engine = "edge_tts"' in cfg


def test_has_key_and_set_key(narrato, tmp_path):
    # 用带缩进的真实 TOML 风格测试
    (tmp_path / "config.toml").write_text(
        "[text]\n    text_openai_api_key = \"\"\n", encoding="utf-8"
    )
    assert narrato._has_key(tmp_path) is False
    narrato._set_key_in_config(tmp_path, "sk-test-123")
    assert narrato._has_key(tmp_path) is True
    cfg = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert '    text_openai_api_key = "sk-test-123"' in cfg


def test_port_constant(narrato):
    assert narrato.NARRATO_PORT == 8510
    assert narrato.NARRATO_HOST == "127.0.0.1"


def test_ensure_start_missing_dir(monkeypatch, tmp_path):
    mod = importlib.import_module("server.routers.narrato")
    importlib.reload(mod)
    missing = tmp_path / "does_not_exist"
    monkeypatch.setattr(mod, "_resolve_dir", lambda: missing)
    res = mod.ensure_start()
    assert res["status"] == "missing_dir"


def test_ensure_start_need_key(narrato, tmp_path):
    # 没写 key
    res = narrato.ensure_start()
    assert res["status"] == "need_key"


def test_ensure_start_starting_and_ready(narrato, tmp_path, monkeypatch):
    narrato._set_key_in_config(tmp_path, "sk-x")
    # 假启动器 + 假 Popen + 假就绪
    monkeypatch.setattr(narrato, "_resolve_launcher", lambda d: ["echo", "x"])
    fake_proc = types.SimpleNamespace(pid=12345, returncode=None, poll=lambda: None, terminate=lambda: None, wait=lambda *a, **k: None, kill=lambda: None, stdout=None)
    monkeypatch.setattr(narrato.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(narrato, "_is_ready", lambda: True)
    res = narrato.ensure_start()
    assert res["status"] == "starting"
    # 再次查询应 ready
    assert narrato.status()["status"] == "ready"
    narrato.stop()
    assert narrato.status()["status"] == "stopped"
