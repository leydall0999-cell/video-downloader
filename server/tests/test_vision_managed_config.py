"""server/tests/test_vision_managed_config.py — 视觉模型「受管凭据」回归测试（2026-09-15）。

背景（产品决策）：
云端视觉 Key 与 LLM Key 同理，**不该由终端用户持有/填写**——由超级管理员通过
受管配置文件（或环境变量）统一下发。界面只保留「自动（本机离线 OCR）」这类不
依赖 Key 的路径，并汇报「云端视觉服务是否已就绪」。

此前 `vision_config` 只有「环境变量 > 用户 JSON」两层，没有受管层；而保存端点
以 `get_vision_config()`（已叠加受管与环境变量）为基底写回用户文件，一次保存就会
把管理员凭据写进用户文件。这里锁定修复后的语义：

  managed_overrides_user_config          受管配置优先于用户配置
  env_wins_over_managed                  环境变量仍是最终裁决
  fallback_to_user_config_without_managed 无受管时回落用户配置
  save_never_writes_managed_credentials  保存不得把受管凭据写进用户文件
  managed_status_masks_api_key           状态接口绝不返回明文 Key
  save_does_not_add_empty_api_key_field  用户文件不出现空的 api_key 字段
  save_preserves_fields_when_not_submitted 不提交凭据时原样保留

设计约束：**绝不读写用户真实 ~/.video-downloader**。所有用例在 `fake_home()` 下
运行，并清空 VDL_VISION_* 环境变量，避免污染。

运行：
    cd server && python tests/test_vision_managed_config.py
    cd server && python -m pytest tests/test_vision_managed_config.py -v
"""
import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402,F401  先完成 app 初始化，避免 routers 循环导入
import vision_config  # noqa: E402
from routers import vision as vision_router  # noqa: E402

_ENV_KEYS = ("VDL_VISION_API_KEY", "VDL_VISION_BASE_URL", "VDL_VISION_MODEL", "VDL_VISION_PROVIDER")


@contextmanager
def fake_home():
    """把 Path.home 临时指向临时目录，隔离真实家目录（还原＝删除实例属性覆盖）。"""
    import pathlib
    fake = Path(tempfile.mkdtemp(prefix="vdl_vision_fakehome_")).resolve()
    pathlib.Path.home = staticmethod(lambda: fake)
    try:
        yield fake
    finally:
        try:
            del pathlib.Path.home
        except AttributeError:
            pass
        shutil.rmtree(fake, ignore_errors=True)


@contextmanager
def no_vision_env():
    """清空 VDL_VISION_* 环境变量（它们优先级最高，不清理会干扰用例）。"""
    saved = {k: os.environ.pop(k, None) for k in _ENV_KEYS}
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _user_path(home: Path) -> Path:
    return home / ".video-downloader" / "vision_config.json"


def _managed_path(home: Path) -> Path:
    return home / ".video-downloader" / "vision_managed.json"


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _read(path: Path) -> dict:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


# ── 1. 读取优先级 ─────────────────────────────────────────────────────── #
def test_managed_overrides_user_config():
    with fake_home() as home, no_vision_env():
        _write(_user_path(home), {"provider": "auto", "api_key": ""})
        _write(_managed_path(home), {
            "provider": "dashscope",
            "api_key": "sk-managed-secret-1234",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model": "qwen-vl-max",
        })
        cfg = vision_config.get_vision_config()
        assert cfg["provider"] == "dashscope", cfg
        assert cfg["api_key"] == "sk-managed-secret-1234", cfg
        assert cfg["model"] == "qwen-vl-max", cfg


def test_env_wins_over_managed():
    """服务端部署用环境变量下发，必须是最终裁决。"""
    with fake_home() as home, no_vision_env():
        _write(_managed_path(home), {"provider": "dashscope", "api_key": "sk-managed-1234"})
        os.environ["VDL_VISION_API_KEY"] = "sk-from-env-5678"
        try:
            cfg = vision_config.get_vision_config()
            assert cfg["api_key"] == "sk-from-env-5678", cfg
        finally:
            os.environ.pop("VDL_VISION_API_KEY", None)


def test_fallback_to_user_config_without_managed():
    with fake_home() as home, no_vision_env():
        _write(_user_path(home), {
            "provider": "ollama",
            "base_url": "http://localhost:11434/v1",
            "model": "qwen2.5vl:7b",
        })
        cfg = vision_config.get_vision_config()
        assert cfg["provider"] == "ollama", cfg
        assert cfg["model"] == "qwen2.5vl:7b", cfg
        st = vision_config.managed_status()
        assert st["configured"] is False and st["source"] == "user", st


# ── 2. 保存语义 ───────────────────────────────────────────────────────── #
def test_save_never_writes_managed_credentials():
    """保存不得把管理员凭据写进用户文件——否则「凭据只由管理员持有」被破坏。"""
    with fake_home() as home, no_vision_env():
        _write(_user_path(home), {"provider": "auto"})
        _write(_managed_path(home), {"provider": "dashscope", "api_key": "sk-managed-secret-1234"})
        vision_router.vision_config_save(server_app.VisionConfigRequest())
        raw = _read(_user_path(home))
        assert "sk-managed-secret-1234" not in json.dumps(raw), raw
        assert raw.get("provider") == "auto", raw


def test_save_preserves_fields_when_not_submitted():
    with fake_home() as home, no_vision_env():
        _write(_user_path(home), {
            "provider": "gemini",
            "api_key": "sk-user-keep-1234",
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "model": "gemini-2.5-flash",
        })
        vision_router.vision_config_save(server_app.VisionConfigRequest())
        raw = _read(_user_path(home))
        assert raw["api_key"] == "sk-user-keep-1234", raw
        assert raw["provider"] == "gemini", raw
        assert raw["model"] == "gemini-2.5-flash", raw


def test_save_does_not_add_empty_api_key_field():
    """用户文件不该出现空的 api_key（排查时极易被误读成「Key 被清空了」）。"""
    with fake_home() as home, no_vision_env():
        _write(_user_path(home), {"provider": "auto"})
        vision_router.vision_config_save(server_app.VisionConfigRequest())
        assert "api_key" not in _read(_user_path(home)), _read(_user_path(home))


def test_save_overrides_only_submitted_fields():
    with fake_home() as home, no_vision_env():
        _write(_user_path(home), {
            "provider": "auto",
            "base_url": "",
            "model": "",
        })
        vision_router.vision_config_save(server_app.VisionConfigRequest(model="qwen-vl-max"))
        raw = _read(_user_path(home))
        assert raw["model"] == "qwen-vl-max", raw
        assert raw["provider"] == "auto", raw   # 未提交 → 保留


# ── 3. 状态接口不泄明文 ───────────────────────────────────────────────── #
def test_managed_status_masks_api_key():
    with fake_home() as home, no_vision_env():
        _write(_managed_path(home), {
            "provider": "dashscope",
            "api_key": "sk-managed-secret-1234",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model": "qwen-vl-max",
        })
        st = vision_config.managed_status()
        assert st["configured"] is True and st["source"] == "managed", st
        assert st["api_key_masked"] and "****" in st["api_key_masked"], st
        blob = json.dumps(st, ensure_ascii=False)
        assert "sk-managed-secret-1234" not in blob, blob
        assert st["provider_name"], "要给出可读的服务商名供界面展示"


def test_config_get_masks_api_key_and_reports_managed():
    """面板回填接口：Key 脱敏，并附带受管状态供界面显示。"""
    with fake_home() as home, no_vision_env():
        _write(_managed_path(home), {"provider": "dashscope", "api_key": "sk-managed-secret-1234"})
        cfg = vision_router.vision_config_get()
        assert "sk-managed-secret-1234" not in json.dumps(cfg, ensure_ascii=False), cfg
        assert "****" in (cfg.get("api_key") or ""), cfg
        assert cfg.get("managed", {}).get("source") == "managed", cfg


_TESTS = [
    test_managed_overrides_user_config,
    test_env_wins_over_managed,
    test_fallback_to_user_config_without_managed,
    test_save_never_writes_managed_credentials,
    test_save_preserves_fields_when_not_submitted,
    test_save_does_not_add_empty_api_key_field,
    test_save_overrides_only_submitted_fields,
    test_managed_status_masks_api_key,
    test_config_get_masks_api_key_and_reports_managed,
]


if __name__ == "__main__":
    failed = 0
    for t in _TESTS:
        try:
            t()
            print(f"  ✓ {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ✗ {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(_TESTS) - failed}/{len(_TESTS)} 通过")
    raise SystemExit(1 if failed else 0)
