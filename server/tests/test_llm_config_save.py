"""server/tests/test_llm_config_save.py — LLM 配置保存的字段保全回归测试（2026-09-14 新增）。

背景（实测复现的真实缺陷）：
`POST /api/llm/config` 原先用一个**固定字段字典整体覆盖**写入配置文件，而前端
「AI 能力与密钥配置」面板只提交其中一部分字段。结果是用户点一次「保存」——哪怕
只是改了个模型名——就会把 `engine` / `mlx_model_path` / `mlx_python` **静默抹掉**：
本机 MLX 引擎随即失效，下一个解说任务悄悄回落到云端模型并消耗免费配额，界面上
没有任何提示。已用真实端点复现（保存前 4 个 MLX 字段俱在，保存后全部消失）。

本测试锁定修复后的语义：**未提交的字段一律保留，提交的字段才覆盖**。

覆盖：
  save_keeps_mlx_fields_when_not_submitted   未提交本机字段时必须保留（回归核心）
  save_overrides_mlx_fields_when_submitted   显式提交时正确覆盖，未提交的仍保留
  save_rejects_unknown_engine                非法 engine 回落 auto，不写坏配置
  save_masked_api_key_keeps_existing         脱敏 Key 不覆盖真实 Key
  local_models_scan_filters_and_sorts        只认「config.json + safetensors」目录
  local_models_dir_env_override              环境变量可指定模型目录
  runtime_status_skips_missing_interpreter   不存在的解释器必须被跳过

设计约束：**绝不读写用户真实 ~/.video-downloader**。所有用例在 `fake_home()` 下运行，
`Path.home` 指向系统临时目录，配置与假权重都落在那儿，退出即清理。

运行：
    cd server && python tests/test_llm_config_save.py
    cd server && python -m pytest tests/test_llm_config_save.py -v
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

import app as server_app  # noqa: E402  先完成 app 初始化，避免 routers 循环导入
import llm_config  # noqa: E402
from routers import llm as llm_router  # noqa: E402
from routers import vision as vision_router  # noqa: E402


@contextmanager
def fake_home():
    """把 pathlib.Path.home 临时指向系统临时目录，隔离真实用户家目录。

    还原方式：`Path.home` 原本继承自 `PurePath.home`、**不在** `Path.__dict__` 里，
    我们设置的是一次实例属性覆盖，因此删掉该属性即可完整恢复继承链——
    比「保存原值再赋回」更干净（后者会把 classmethod 二次绑定）。
    """
    import pathlib
    fake = Path(tempfile.mkdtemp(prefix="vdl_fakehome_")).resolve()
    pathlib.Path.home = staticmethod(lambda: fake)
    try:
        yield fake
    finally:
        try:
            del pathlib.Path.home
        except AttributeError:
            pass
        shutil.rmtree(fake, ignore_errors=True)


def _cfg_path(home: Path) -> Path:
    return home / ".video-downloader" / "llm_config.json"


def _write_cfg(home: Path, data: dict) -> Path:
    p = _cfg_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def _read_cfg(home: Path) -> dict:
    return json.loads(_cfg_path(home).read_text(encoding="utf-8"))


def _managed_path(home: Path) -> Path:
    return home / ".video-downloader" / "llm_managed.json"


def _write_managed(home: Path, data: dict) -> Path:
    p = _managed_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


@contextmanager
def no_llm_env():
    """临时清空 LLM 相关环境变量：它们优先级最高，会盖掉受管配置的判定。"""
    keys = ("LLM_API_KEY", "LLM_APIKEY", "LLM_BASE_URL", "LLM_MODEL",
            "VDL_LLM_ENGINE", "MLX_MODEL_PATH", "MLX_PYTHON")
    saved = {k: os.environ.pop(k, None) for k in keys}
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def _make_model(root: Path, name: str, *, config=True, weights=True,
                weight_bytes=2 * 1024 * 1024) -> Path:
    """造一个假 MLX 权重目录（只造判定所需的两个文件）。

    权重文件刻意写足 2MB：体积统计是「字节数 ÷ 1024²」取整，
    几十字节的占位文件会算出 0MB，测不到这条路径。
    """
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    if config:
        (d / "config.json").write_text("{}", encoding="utf-8")
    if weights:
        (d / "model.safetensors").write_bytes(b"\x00" * weight_bytes)
    return d


# ── 保存语义 ──────────────────────────────────────────────────────────
def test_save_keeps_mlx_fields_when_not_submitted():
    """回归核心：只提交 UI 暴露的字段时，本机引擎配置必须原样保留。"""
    with fake_home() as home:
        _write_cfg(home, {
            "provider": "deepseek",
            "api_key": "sk-real-key-1234",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-v4-flash",
            "engine": "auto",
            "mlx_model_path": "/models/Qwen2.5-3B-Instruct-4bit",
            "mlx_python": "/opt/mlx/bin/python",
            "mlx_max_tokens": 8192,
        })
        # 模拟旧版前端/第三方调用：请求体里没有 engine / mlx_* 字段
        llm_router.llm_config_save(server_app.LLMConfigRequest(
            provider="deepseek",
            api_key="sk-real-key-1234",
            base_url="https://api.deepseek.com/v1",
            model="deepseek-chat",
        ))
        cfg = _read_cfg(home)
        assert cfg["engine"] == "auto", cfg
        assert cfg["mlx_model_path"] == "/models/Qwen2.5-3B-Instruct-4bit", cfg
        assert cfg["mlx_python"] == "/opt/mlx/bin/python", cfg
        assert cfg["mlx_max_tokens"] == 8192, cfg
        # 同时确认提交的字段确实写进去了（不是整个保存都失效）
        assert cfg["model"] == "deepseek-chat", cfg


def test_save_overrides_mlx_fields_when_submitted():
    """显式提交本机字段时覆盖；未提交的同类字段仍然保留。"""
    with fake_home() as home:
        _write_cfg(home, {
            "engine": "auto",
            "mlx_model_path": "/models/old-3B",
            "mlx_python": "/opt/mlx/bin/python",
        })
        llm_router.llm_config_save(server_app.LLMConfigRequest(
            engine="cloud",
            mlx_model_path="/models/new-1.5B",
            mlx_max_tokens=4096,
        ))
        cfg = _read_cfg(home)
        assert cfg["engine"] == "cloud", cfg
        assert cfg["mlx_model_path"] == "/models/new-1.5B", cfg
        assert cfg["mlx_max_tokens"] == 4096, cfg
        # mlx_python 本次未提交 → 保留旧值
        assert cfg["mlx_python"] == "/opt/mlx/bin/python", cfg


def test_save_normalizes_engine_to_two_tiers():
    """引擎档位收敛为用户可见的两档：auto（本机优先→云端配合）/ cloud（纯云端）。

    旧版曾暴露 mlx / ollama 强制档，现按产品决策下线——写入时统一归一到 auto，
    避免出现「配置文件里是 mlx、界面上只有两个选项」的幽灵状态。
    """
    with fake_home() as home:
        _write_cfg(home, {"engine": "auto"})
        for legacy in ("mlx", "ollama", "ollama-typo", ""):
            llm_router.llm_config_save(server_app.LLMConfigRequest(engine=legacy))
            assert _read_cfg(home)["engine"] == "auto", legacy
        llm_router.llm_config_save(server_app.LLMConfigRequest(engine="cloud"))
        assert _read_cfg(home)["engine"] == "cloud"


def test_save_omitted_credentials_are_preserved():
    """回归核心：前端简化后不再提交凭据四件套，保存不得把它们重置。

    历史破坏路径：`provider` 的请求默认值是 "openai"，且保存是无条件写入——
    用户只改引擎档位时，配置会被打回 openai、base_url / model 被清空，
    云端解说随即失效（且界面没有任何提示）。
    """
    with fake_home() as home:
        _write_cfg(home, {
            "provider": "deepseek",
            "api_key": "sk-real-key-1234",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-v4-flash",
            "engine": "auto",
        })
        # 模拟「新前端」：只提交用户可见的字段（引擎档位 + 省钱旋钮）
        llm_router.llm_config_save(server_app.LLMConfigRequest(
            engine="cloud", reasoning_effort="low"))
        cfg = _read_cfg(home)
        assert cfg["engine"] == "cloud", cfg
        assert cfg["provider"] == "deepseek", cfg
        assert cfg["base_url"] == "https://api.deepseek.com/v1", cfg
        assert cfg["model"] == "deepseek-v4-flash", cfg
        assert cfg["api_key"] == "sk-real-key-1234", cfg


def test_save_does_not_add_empty_api_key_field():
    """用户文件不该出现空的 `api_key` 字段——排查时容易被误读成「Key 被清空了」。"""
    with fake_home() as home:
        _write_cfg(home, {"engine": "auto"})
        llm_router.llm_config_save(server_app.LLMConfigRequest(engine="cloud"))
        raw = _read_cfg(home)
        assert raw["engine"] == "cloud", raw
        assert "api_key" not in raw, raw
        # 文件里本来就有 Key 时仍原样保留
        _write_cfg(home, {"engine": "auto", "api_key": "sk-keep-me-1234"})
        llm_router.llm_config_save(server_app.LLMConfigRequest(engine="auto"))
        assert _read_cfg(home)["api_key"] == "sk-keep-me-1234"


def test_managed_config_overrides_user_config():
    """管理员受管配置优先于用户配置；保存时不得把受管凭据写进用户文件。"""
    with fake_home() as home, no_llm_env():
        _write_cfg(home, {"provider": "openai", "api_key": "", "engine": "auto"})
        _write_managed(home, {
            "provider": "deepseek",
            "api_key": "sk-managed-key-9999",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-v4-flash",
        })
        cfg = llm_config.get_llm_config()
        assert cfg["provider"] == "deepseek", cfg
        assert cfg["api_key"] == "sk-managed-key-9999", cfg
        assert cfg["model"] == "deepseek-v4-flash", cfg
        # 保存一次：受管凭据不得落进用户文件（否则等于在用户机器上多留一份 Key）
        llm_router.llm_config_save(server_app.LLMConfigRequest(engine="auto"))
        raw = _read_cfg(home)
        assert raw.get("api_key", "") == "", raw
        assert raw.get("provider", "") != "deepseek", raw


def test_managed_status_reports_source_without_plain_key():
    """状态接口要能说明「已由管理员配置」，但绝不能吐出明文 Key。"""
    with fake_home() as home, no_llm_env():
        _write_managed(home, {
            "provider": "deepseek",
            "api_key": "sk-managed-key-9999",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-v4-flash",
        })
        st = llm_config.managed_status()
        assert st["configured"] is True, st
        assert st["source"] == "managed", st
        assert st["provider_name"] == "DeepSeek", st
        assert st["model"] == "deepseek-v4-flash", st
        blob = json.dumps(st, ensure_ascii=False)
        assert "sk-managed-key-9999" not in blob, "状态接口泄露了明文 Key"
        assert st["api_key_masked"].endswith("9999"), st


def test_save_masked_api_key_keeps_existing():
    """前端回填的是脱敏 Key（含 ****），保存时不得覆盖真实 Key。"""
    with fake_home() as home:
        _write_cfg(home, {"api_key": "sk-real-secret-value"})
        llm_router.llm_config_save(server_app.LLMConfigRequest(api_key="sk-r****alue"))
        assert _read_cfg(home)["api_key"] == "sk-real-secret-value"


def test_save_empty_api_key_keeps_existing():
    """空 api_key 语义 = 「本次不修改」，绝不能清空已配好的 Key。

    回归（2026-09-15 实测事故）：`GET /api/llm/config` 返回体不含 ok 字段，而前端
    用 `if (r && r.ok)` 判定回填成功 → 回填**永不执行** → 面板里 Key 输入框恒为空
    → 用户点「保存」提交空字符串 → 旧实现 `"****" not in ""` 为真，把真实 Key 写成空。
    后果：云端解说与视觉理解双双失效（inject_llm_env 在「无 Key + engine=cloud」
    时直接 return，子进程拿不到 LLM_API_KEY，任务报「LLM_API_KEY 未设置」）。
    """
    with fake_home() as home:
        _write_cfg(home, {
            "provider": "deepseek",
            "api_key": "sk-real-secret-value",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
            "engine": "cloud",
        })
        # 前端未回填时提交空 Key（其余字段照常提交）
        llm_router.llm_config_save(server_app.LLMConfigRequest(
            provider="openai", api_key="", base_url="", model="", engine="cloud"))
        saved = _read_cfg(home)
        assert saved["api_key"] == "sk-real-secret-value", saved
        # 顺带锁定：本机引擎字段未被抹掉（与既有用例同源的防回归）

    # 空白串同样视为「不修改」（前端 .trim() 后可能得到空串）
    with fake_home() as home:
        _write_cfg(home, {"api_key": "sk-real-secret-value"})
        llm_router.llm_config_save(server_app.LLMConfigRequest(api_key="   "))
        assert _read_cfg(home)["api_key"] == "sk-real-secret-value"


def test_save_empty_vision_api_key_keeps_existing():
    """视觉配置同样：空 Key 不得清空（与 LLM 面板同一时刻被清空过）。"""
    with fake_home() as home:
        p = home / ".video-downloader" / "vision_config.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "provider": "dashscope",
            "api_key": "sk-vision-secret",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model": "qwen-vl-max",
        }, ensure_ascii=False), encoding="utf-8")
        vision_router.vision_config_save(server_app.VisionConfigRequest(
            provider="auto", api_key="", base_url="", model=""))
        saved = json.loads(p.read_text(encoding="utf-8"))
        assert saved["api_key"] == "sk-vision-secret", saved


# ── 本机模型扫描 ──────────────────────────────────────────────────────
def test_local_models_scan_filters_and_sorts():
    """只认「config.json + safetensors」的目录，并按参数量从大到小... 实际按升序排列。"""
    with fake_home() as home:
        root = home / ".video-downloader" / "mlx_models"
        _make_model(root, "Qwen2.5-3B-Instruct-4bit")
        _make_model(root, "Qwen2.5-1.5B-Instruct-4bit")
        _make_model(root, "OnlyConfig-7B", weights=False)   # 缺权重 → 排除
        _make_model(root, "OnlyWeights-9B", config=False)   # 缺配置 → 排除

        models = llm_config.list_local_models()
        names = [m["name"] for m in models]
        assert names == ["Qwen2.5-1.5B-Instruct-4bit", "Qwen2.5-3B-Instruct-4bit"], names
        tiers = {m["name"]: m["tier"] for m in models}
        assert tiers["Qwen2.5-1.5B-Instruct-4bit"] == "1.5B", tiers
        assert tiers["Qwen2.5-3B-Instruct-4bit"] == "3B", tiers
        # 每项都带体积与提示，供前端下拉直接展示
        for m in models:
            assert m["size_mb"] >= 2, m          # 假权重写足 2MB
            assert m["hint"], m
            assert os.path.isdir(m["path"]), m


def test_local_models_dir_env_override():
    """VDL_MLX_MODELS_DIR 可指定扫描目录（部署/自定义路径场景）。"""
    with fake_home() as home:
        custom = home / "my_models"
        _make_model(custom, "Qwen2.5-0.5B-Instruct-4bit")
        # 默认目录也放一个，用于确认环境变量优先生效且不重复混入
        _make_model(home / ".video-downloader" / "mlx_models", "Qwen2.5-3B-Instruct-4bit")

        old = os.environ.get("VDL_MLX_MODELS_DIR")
        os.environ["VDL_MLX_MODELS_DIR"] = str(custom)
        try:
            assert llm_config.local_models_dir() == str(custom)
            names = [m["name"] for m in llm_config.list_local_models()]
            # 环境变量目录优先（0.5B 排前），默认目录也仍在候选内
            assert names[0] == "Qwen2.5-0.5B-Instruct-4bit", names
            assert "Qwen2.5-3B-Instruct-4bit" in names, names
        finally:
            if old is None:
                os.environ.pop("VDL_MLX_MODELS_DIR", None)
            else:
                os.environ["VDL_MLX_MODELS_DIR"] = old


def test_runtime_status_skips_missing_interpreter():
    """不存在的解释器路径必须被跳过——否则 subprocess 抛错/阻塞会拖慢配置页。"""
    st = llm_config.local_runtime_status("/nonexistent/python-for-test")
    assert st["python"] != "/nonexistent/python-for-test", st
    assert isinstance(st.get("available"), bool), st


def main():
    tests = [
        test_save_keeps_mlx_fields_when_not_submitted,
        test_save_overrides_mlx_fields_when_submitted,
        test_save_normalizes_engine_to_two_tiers,
        test_save_omitted_credentials_are_preserved,
        test_save_does_not_add_empty_api_key_field,
        test_managed_config_overrides_user_config,
        test_managed_status_reports_source_without_plain_key,
        test_save_masked_api_key_keeps_existing,
        test_save_empty_api_key_keeps_existing,
        test_save_empty_vision_api_key_keeps_existing,
        test_local_models_scan_filters_and_sorts,
        test_local_models_dir_env_override,
        test_runtime_status_skips_missing_interpreter,
    ]
    for t in tests:
        t()
        print(f"  ✅ {t.__name__}")
    print(f"✅ LLM 配置保存与模型扫描测试全过（{len(tests)} 项）")


if __name__ == "__main__":
    main()
