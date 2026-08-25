"""配音/音量手动可调配置层测试。

覆盖：默认值、保存夹取、off 分支、env 注入、环境变量优先。
"""
import importlib
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
# commentary_config 内部用 `from llm_config import ...`，约定 server/ 在 sys.path 上
SERVER_DIR = REPO / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import commentary_config as cc


def _write_config(tmp_path, data):
    p = tmp_path / "commentary_config.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_defaults_when_no_file(monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "_config_path", lambda: tmp_path / "nope.json")
    cfg = cc.get_commentary_config()
    assert cfg["narration_loudness"] == cc.DEFAULT_NARRATION_LOUDNESS
    assert cfg["original_duck"] == cc.DEFAULT_ORIGINAL_DUCK
    assert cfg["narration_boost"] == cc.DEFAULT_NARRATION_BOOST


def test_off_branch_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_config_path", lambda: tmp_path / "c.json")
    norm = cc.save_commentary_config({
        "narration_loudness": "off",
        "original_duck": 0.10,
        "narration_boost": 1.0,
    })
    assert norm["narration_loudness"] == "off"
    cfg = cc.get_commentary_config()
    assert cfg["narration_loudness"] == "off"


def test_clamp_values(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_config_path", lambda: tmp_path / "c.json")
    norm = cc.save_commentary_config({
        "narration_loudness": -100,   # 应夹到 -18
        "original_duck": 0.99,        # 应夹到 0.30
        "narration_boost": 9.9,       # 应夹到 1.6
    })
    assert norm["narration_loudness"] == -18
    assert norm["original_duck"] == 0.30
    assert norm["narration_boost"] == 1.6


def test_inject_sets_env(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_config_path", lambda: tmp_path / "c.json")
    cc.save_commentary_config({
        "narration_loudness": -12,
        "original_duck": 0.08,
        "narration_boost": 1.3,
    })
    env = {}
    cc.inject_commentary_env(env)
    assert env["VDL_NARRATION_LOUDNESS"] == "-12"
    assert env["VDL_ORIGINAL_DUCK"] == "0.08"
    assert env["VDL_NARRATION_BOOST"] == "1.3"


def test_inject_off_writes_off(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_config_path", lambda: tmp_path / "c.json")
    cc.save_commentary_config({
        "narration_loudness": "off",
        "original_duck": 0.10,
        "narration_boost": 1.0,
    })
    env = {}
    cc.inject_commentary_env(env)
    assert env["VDL_NARRATION_LOUDNESS"] == "off"


def test_existing_env_not_overridden(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_config_path", lambda: tmp_path / "c.json")
    cc.save_commentary_config({
        "narration_loudness": -12,
        "original_duck": 0.08,
        "narration_boost": 1.3,
    })
    env = {"VDL_NARRATION_LOUDNESS": "-16"}  # 运维级覆盖
    cc.inject_commentary_env(env)
    assert env["VDL_NARRATION_LOUDNESS"] == "-16"  # 尊重已有 env
    assert env["VDL_ORIGINAL_DUCK"] == "0.08"     # 其余仍注入


def test_env_var_overrides_file(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_config_path", lambda: tmp_path / "c.json")
    cc.save_commentary_config({
        "narration_loudness": -12,
        "original_duck": 0.08,
        "narration_boost": 1.3,
    })
    monkeypatch.setenv("VDL_NARRATION_LOUDNESS", "off")
    cfg = cc.get_commentary_config()
    assert cfg["narration_loudness"] == "off"
