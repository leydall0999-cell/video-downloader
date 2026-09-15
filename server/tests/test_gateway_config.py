"""server/tests/test_gateway_config.py — 云端网关（Key 不落地）回归测试。

背景
----
原先管理员 Key 明文躺在 `~/.video-downloader/llm_managed.json`（0600）。自用无碍，
但**把 App 分发给他人 = 把 Key 一起发出去**（0600 挡不住，App 自己就能读）。

改为服务端代理后：本机只持有「网关地址 + 可吊销令牌」，真实 Key 只在网关服务上。
本测试锁定这条链路的语义，防止哪次改动又把真实 Key 塞回子进程环境。

覆盖：
  gateway_prefers_env_over_managed_over_user      三级优先级
  gateway_disabled_when_url_or_token_missing      半配置必须视为未接入
  gateway_status_never_returns_full_token         状态接口绝不吐完整令牌
  gateway_resolve_model_prefers_whitelist_hit     模型名命中白名单就沿用
  gateway_resolve_model_falls_back_to_first       没命中就退到白名单第一个（避免撞 403）
  gateway_env_models_bypasses_network             环境变量指定白名单时零网络
  inject_env_uses_gateway_token_not_real_key      核心：注入的是令牌，不是 sk-
  inject_env_falls_back_to_direct_key             未配网关时回退直连（行为不回归）

设计约束：**不读写真实 ~/.video-downloader，不发起任何网络请求**。
配置目录用 VDL_HOME 指到临时目录；模型白名单用 VDL_GATEWAY_MODELS 显式给出，
因此 resolve_model 全程零网络。

运行：
    cd server && python tests/test_gateway_config.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402,F401  先完成 app 初始化，避免 routers 循环导入
import gateway_config  # noqa: E402
import llm_config  # noqa: E402

_ENV_KEYS = (
    "VDL_HOME",
    "VDL_GATEWAY_URL",
    "VDL_GATEWAY_TOKEN",
    "VDL_GATEWAY_ENABLED",
    "VDL_GATEWAY_MODELS",
    "LLM_API_KEY",
    "LLM_APIKEY",
)

TMPDIR = Path(tempfile.gettempdir())
GW_TOKEN = "vdlt_TESTTOKEN0123456789abcdef"
REAL_KEY = "sk-REALKEY0000000000000000000000"


def run(fn):
    fn()
    print(f"  ✔ {fn.__name__}")


class _EnvGuard:
    """隔离家目录与网关相关环境变量，退出即还原。

    关键：gateway_config 与 llm_config **必须落到同一个配置目录**，否则
    「走网关 / 回退直连」两条分支会各自读到不同文件，测出来的结论是假的。
    llm_config 只认 `Path.home()/.video-downloader`，所以这里也用 fake home
    而不是 VDL_HOME（VDL_HOME 仅作覆盖口保留，测试期间一律清空）。
    """

    def __enter__(self):
        import pathlib

        self._saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        self.home = Path(tempfile.mkdtemp(prefix="vdl_gwhome_")).resolve()
        self._pathlib = pathlib
        pathlib.Path.home = staticmethod(lambda: self.home)
        self.cfg_dir = self.home / ".video-downloader"
        self.cfg_dir.mkdir(parents=True, exist_ok=True)
        gateway_config._MODELS_CACHE["ts"] = 0.0
        gateway_config._MODELS_CACHE["models"] = []
        return self

    def __exit__(self, *exc):
        # Path.home 原本继承自 PurePath.home、不在 Path.__dict__ 里，删属性即恢复继承链
        try:
            del self._pathlib.Path.home
        except AttributeError:
            pass
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False

    def write_managed(self, **kw):
        p = self.cfg_dir / "gateway_managed.json"
        p.write_text(json.dumps(kw, ensure_ascii=False), encoding="utf-8")
        p.chmod(0o600)
        return p

    def write_user(self, **kw):
        p = self.cfg_dir / "gateway.json"
        p.write_text(json.dumps(kw, ensure_ascii=False), encoding="utf-8")
        return p


def test_gateway_prefers_env_over_managed_over_user():
    with _EnvGuard() as g:
        g.write_user(url="http://user.invalid/gw", token="user-token", enabled=True)
        assert gateway_config.get_gateway_config()["source"] == "user"

        g.write_managed(url="http://managed.invalid/gw", token="managed-token", enabled=True)
        cfg = gateway_config.get_gateway_config()
        assert cfg["source"] == "managed"
        assert cfg["token"] == "managed-token"

        os.environ["VDL_GATEWAY_URL"] = "http://env.invalid/gw"
        os.environ["VDL_GATEWAY_TOKEN"] = "env-token"
        cfg = gateway_config.get_gateway_config()
        assert cfg["source"] == "env"
        assert cfg["url"] == "http://env.invalid/gw"
        assert cfg["token"] == "env-token"


def test_gateway_disabled_when_url_or_token_missing():
    """半配置必须视为未接入——否则会让人以为在走网关，实际云端根本不可用。"""
    with _EnvGuard() as g:
        g.write_managed(url="http://gw.invalid/gw", token="", enabled=True)
        assert gateway_config.get_gateway_config()["enabled"] is False

        g.write_managed(url="", token=GW_TOKEN, enabled=True)
        assert gateway_config.get_gateway_config()["enabled"] is False

        g.write_managed(url="http://gw.invalid/gw", token=GW_TOKEN, enabled=False)
        assert gateway_config.get_gateway_config()["enabled"] is False

        g.write_managed(url="http://gw.invalid/gw", token=GW_TOKEN, enabled=True)
        assert gateway_config.get_gateway_config()["enabled"] is True


def test_gateway_status_never_returns_full_token():
    with _EnvGuard() as g:
        g.write_managed(url="http://gw.invalid/gw", token=GW_TOKEN, enabled=True)
        st = gateway_config.gateway_status()
        assert st["enabled"] is True
        assert st["has_token"] is True
        assert GW_TOKEN not in json.dumps(st, ensure_ascii=False)
        # 脱敏串仍能让人认出是哪一枚令牌
        assert st["token_masked"].startswith("vdlt_")
        assert st["token_masked"].endswith("cdef")


def test_gateway_resolve_model_prefers_whitelist_hit():
    with _EnvGuard() as g:
        g.write_managed(url="http://gw.invalid/gw", token=GW_TOKEN, enabled=True)
        os.environ["VDL_GATEWAY_MODELS"] = "deepseek-v4-flash,deepseek-chat"
        assert gateway_config.resolve_model("deepseek-chat") == "deepseek-chat"
        assert gateway_config.upstream_models() == ["deepseek-v4-flash", "deepseek-chat"]


def test_gateway_resolve_model_falls_back_to_first():
    """客户端拿着旧模型名去撞服务端白名单会 403，这里就地校正。"""
    with _EnvGuard() as g:
        g.write_managed(url="http://gw.invalid/gw", token=GW_TOKEN, enabled=True)
        os.environ["VDL_GATEWAY_MODELS"] = "deepseek-v4-flash,deepseek-chat"
        assert gateway_config.resolve_model("deepseek-v3-old") == "deepseek-v4-flash"
        assert gateway_config.resolve_model("") == "deepseek-v4-flash"


def test_gateway_env_models_bypasses_network():
    """白名单来自环境变量时，不应对网关发起任何请求（离线环境也要能跑）。"""
    with _EnvGuard() as g:
        g.write_managed(url="http://gw.invalid/gw", token=GW_TOKEN, enabled=True)
        os.environ["VDL_GATEWAY_MODELS"] = "only-model"
        assert gateway_config.upstream_models() == ["only-model"]
        # 缓存不应被写入，避免环境变量与缓存打架
        assert gateway_config._MODELS_CACHE["models"] == []


def test_gateway_cloud_env_returns_token_not_key():
    with _EnvGuard() as g:
        g.write_managed(url="http://gw.invalid/gw", token=GW_TOKEN, enabled=True)
        os.environ["VDL_GATEWAY_MODELS"] = "deepseek-v4-flash"
        env = gateway_config.cloud_env("deepseek-v4-flash")
        assert env is not None
        assert env["base_url"] == "http://gw.invalid/gw/v1"
        assert env["api_key"] == GW_TOKEN
        assert not env["api_key"].startswith("sk-")


def test_inject_env_uses_gateway_token_not_real_key():
    """核心回归：即使本机仍存着真实 Key，走网关时注入的也必须是令牌。"""
    with _EnvGuard() as g:
        g.write_managed(url="http://gw.invalid/gw", token=GW_TOKEN, enabled=True)
        os.environ["VDL_GATEWAY_MODELS"] = "deepseek-v4-flash"
        # 故意在用户文件里塞一把真实 Key，模拟「历史遗留 Key 还没清干净」
        user_cfg = g.cfg_dir / "llm_config.json"
        user_cfg.write_text(
            json.dumps({"engine": "cloud", "provider": "deepseek", "api_key": REAL_KEY}),
            encoding="utf-8",
        )
        env: dict[str, str] = {}
        llm_config.inject_llm_env(env)
        assert env.get("VDL_LLM_VIA_GATEWAY") == "1"
        assert env["LLM_API_KEY"] == GW_TOKEN
        assert REAL_KEY not in "".join(env.values())
        assert env["LLM_BASE_URL"] == "http://gw.invalid/gw/v1"


def test_inject_env_falls_back_to_direct_key():
    """未配网关时的行为必须与改造前一致，不能因为加了网关就把直连路径弄坏。"""
    with _EnvGuard() as g:
        user_cfg = g.cfg_dir / "llm_config.json"
        user_cfg.write_text(
            json.dumps(
                {
                    "engine": "cloud",
                    "provider": "deepseek",
                    "api_key": REAL_KEY,
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-v4-flash",
                }
            ),
            encoding="utf-8",
        )
        env: dict[str, str] = {}
        llm_config.inject_llm_env(env)
        assert "VDL_LLM_VIA_GATEWAY" not in env
        assert env["LLM_API_KEY"] == REAL_KEY
        assert env["LLM_BASE_URL"] == "https://api.deepseek.com/v1"


TESTS = [
    test_gateway_prefers_env_over_managed_over_user,
    test_gateway_disabled_when_url_or_token_missing,
    test_gateway_status_never_returns_full_token,
    test_gateway_resolve_model_prefers_whitelist_hit,
    test_gateway_resolve_model_falls_back_to_first,
    test_gateway_env_models_bypasses_network,
    test_gateway_cloud_env_returns_token_not_key,
    test_inject_env_uses_gateway_token_not_real_key,
    test_inject_env_falls_back_to_direct_key,
]


def main():
    print("=" * 78)
    print("test_gateway_config.py — 云端网关（Key 不落地）")
    print("=" * 78)
    for t in TESTS:
        run(t)
    print("-" * 78)
    print(f"✅ {len(TESTS)}/{len(TESTS)} 通过")


if __name__ == "__main__":
    main()
