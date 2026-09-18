"""本地语音克隆运行环境按需安装（server/clone_env.py）的离线回归测试。

为什么值得单独测：这条链路会**在用户机器上建 venv、跑 pip、下 2.4GB 权重**，
是所有功能里副作用最重的一个。它一旦判错（把「已装好」判成「要重装」，或反过来
把「没装」判成「就绪」），用户看到的是「凭空多下一遍 500MB」或「点了安装却什么都没发生」，
而且都要等几分钟才发现。

覆盖：
  status()            状态形状 + 不抛异常 + 不向真实家目录写入
  find_venv()         候选优先级（数据目录 > 开发机路径）、torch/mlx 类型判定
  venv_ok/weights_ok  半成品目录不得判成就绪
  start_install()     非 Apple Silicon 拒绝 / 已就绪短路 / 磁盘不足拦截 / 幂等
  cancel()            空闲时取消返回 ok=False
  _mlx_pin()          macOS 13 钉 0.29.3、14+ 不钉（决定能不能装上）
  _sub_env()          默认摘代理、显式 KEEP 时保留
  🔴 隔离纪律        全程不得改写进程级 HF_ENDPOINT（对齐 test_engine_isolation 的要求）

设计约束（沿用 test_commentary_routes.py 的约定）：**绝不向真实家目录写任何文件**。
需要「目录存在性」的用例一律把模块的路径常量指向 tempfile 临时目录。

运行：
    cd server && python tests/test_clone_env.py
    cd server && python -m pytest tests/test_clone_env.py -v
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import clone_env as C  # noqa: E402


# ───────────────────────── 工具 ─────────────────────────

class _Sandbox:
    """把 clone_env 的落点常量临时指到临时目录，退出即还原。"""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self._saved: dict = {}

    def __enter__(self):
        venv = self.tmp / "venvs" / "qwen3tts_mlx"
        weights = self.tmp / "qwen3tts" / C.MODEL_DIRNAME
        self._saved = {
            "VENV_DIR": C.VENV_DIR, "WEIGHTS_ROOT": C.WEIGHTS_ROOT, "WEIGHTS_DIR": C.WEIGHTS_DIR,
            "_MAIN_WEIGHTS": C._MAIN_WEIGHTS, "_TOK_WEIGHTS": C._TOK_WEIGHTS,
            "_venv_dirs": C._venv_dirs,
        }
        C.VENV_DIR = venv
        C.WEIGHTS_ROOT = weights.parent
        C.WEIGHTS_DIR = weights
        C._MAIN_WEIGHTS = weights / "model.safetensors"
        C._TOK_WEIGHTS = weights / "speech_tokenizer" / "model.safetensors"
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            setattr(C, k, v)
        return False


def _pretend_venv(d: Path, kind: str = "mlx") -> None:
    """造一个「看起来装好了」的 venv 目录。

    kind="mlx" → site-packages 下有 mlx + mlx_audio；kind="torch" → 只有 qwen_tts。
    注意目录名要与 clone_env._venv_kind() 实际检查的名字一致，别只造个"像"的。
    """
    py = d / "bin" / "python"
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    py.chmod(0o755)
    sp = d / "lib" / "python3.13" / "site-packages"
    sp.mkdir(parents=True, exist_ok=True)
    if kind == "mlx":
        (sp / "mlx").mkdir(exist_ok=True)
        (sp / "mlx_audio").mkdir(exist_ok=True)
    else:
        (sp / kind).mkdir(exist_ok=True)


def _pretend_weights(d: Path) -> None:
    for p, size in ((d / "model.safetensors", 1_100_000_000),
                    (d / "speech_tokenizer" / "model.safetensors", 600_000_000)):
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as f:
            f.truncate(size)          # 稀疏文件：占位不占盘


# ───────────────────────── 用例 ─────────────────────────

def test_status_shape_and_no_home_writes():
    """status() 必须秒回、形状稳定，且不往真实家目录写东西。"""
    before = {p: p.stat().st_mtime for p in (
        Path.home() / ".video-downloader", Path.home() / ".cache" / "qwen3tts") if p.exists()}
    s = C.status()
    for key in ("supported", "ready", "needed_mb", "disk_free_mb", "note",
                "python", "venv", "weights", "install"):
        assert key in s, f"status() 缺字段 {key}"
    assert isinstance(s["supported"], bool) and isinstance(s["ready"], bool)
    assert s["venv"]["kind"] in ("", "mlx", "torch")
    assert s["install"]["phase"] in ("idle", "python", "venv", "pip", "weights",
                                     "done", "error", "cancelled")
    for p, mt in before.items():
        assert p.stat().st_mtime == mt, f"status() 不该改到家目录：{p}"


def test_find_venv_prefers_install_target_over_dev_path():
    """数据目录里装好的 MLX 环境，必须优先于开发机路径（否则用户装了也用不上）。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        with _Sandbox(tmp):
            dev = tmp / "devproj" / "scripts" / ".venv_qwen3tts_mlx"
            _pretend_venv(C.VENV_DIR)
            _pretend_venv(dev)
            C._venv_dirs = lambda: [C.VENV_DIR, dev]      # type: ignore[assignment]
            got, kind = C.find_venv()
            assert got == C.VENV_DIR and kind == "mlx", f"应优先数据目录，实际 {got} / {kind}"


def test_find_venv_detects_torch_only_env():
    """只有 torch 版（无 mlx_audio）时，类型要报 torch —— 前端据此提示「建议换 MLX」。"""
    with tempfile.TemporaryDirectory() as td:
        with _Sandbox(Path(td)):
            d = Path(td) / "v"
            _pretend_venv(d, kind="qwen_tts")
            C._venv_dirs = lambda: [d]                    # type: ignore[assignment]
            assert C.find_venv() == (d, "torch")


def test_venv_ok_rejects_half_built():
    """半成品 venv（有 bin/python 但没装依赖）不得判成就绪。"""
    with tempfile.TemporaryDirectory() as td:
        with _Sandbox(Path(td)):
            d = Path(td) / "v"
            (d / "bin").mkdir(parents=True)
            (d / "bin" / "python").write_text("", encoding="utf-8")
            C._venv_dirs = lambda: [d]                    # type: ignore[assignment]
            assert C.find_venv() == (None, ""), "没有依赖的 venv 不该算就绪"


def test_weights_ok_rejects_truncated():
    """权重被截断（体积不达标）不得判成就绪，否则会在合成时才炸。"""
    with tempfile.TemporaryDirectory() as td:
        with _Sandbox(Path(td)):
            assert C.weights_ok() is False
            p = C.WEIGHTS_DIR / "model.safetensors"
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "wb") as f:
                f.truncate(10_000)          # 远小于下限
            assert C.weights_ok() is False, "截断的权重不该算就绪"
            _pretend_weights(C.WEIGHTS_DIR)
            assert C.weights_ok() is True


def test_start_install_refuses_on_non_apple_silicon():
    """Intel Mac / Linux 上必须明确拒绝，而不是装到一半才失败。"""
    saved = C._is_apple_silicon
    C._is_apple_silicon = lambda: False                # type: ignore[assignment]
    try:
        r = C.start_install()
        assert r["ok"] is False and "Apple Silicon" in r["msg"], r
    finally:
        C._is_apple_silicon = saved                        # type: ignore[assignment]


def test_start_install_short_circuits_when_ready():
    """已就绪时不得重复开线程下载。"""
    with tempfile.TemporaryDirectory() as td:
        with _Sandbox(Path(td)):
            _pretend_venv(C.VENV_DIR)
            _pretend_weights(C.WEIGHTS_DIR)
            C._venv_dirs = lambda: [C.VENV_DIR]        # type: ignore[assignment]
            assert C.venv_ok() and C.weights_ok()
            r = C.start_install()
            assert r["ok"] is True and "就绪" in r["msg"], r
            assert C.progress()["active"] is False, "不该真的起安装线程"


def test_start_install_blocks_on_low_disk():
    """磁盘不够时必须提前拦住——下到 1.8GB 才失败是最糟的体验。"""
    with tempfile.TemporaryDirectory() as td:
        with _Sandbox(Path(td)):
            saved = C._disk_free_mb
            C._disk_free_mb = lambda p: 100            # type: ignore[assignment]
            try:
                r = C.start_install()
                assert r["ok"] is False and "磁盘" in r["msg"], r
            finally:
                C._disk_free_mb = saved                 # type: ignore[assignment]


def test_cancel_when_idle():
    """空闲时取消应如实返回「没有进行中的安装」，别假装成功。"""
    saved = C._STATE["active"]
    C._STATE["active"] = False
    try:
        r = C.cancel()
        assert r["ok"] is False, r
    finally:
        C._STATE["active"] = saved


def test_mlx_pin_by_macos_version():
    """macOS 13 必须钉 0.29.3（0.30+ 只有 14+ 的 wheel），14+ 不钉（拿最新）。"""
    saved_mac, saved_env = C.platform.mac_ver, os.environ.pop("VDL_CLONE_MLX_VERSION", None)
    try:
        C.platform.mac_ver = lambda: ("13.7.8", ("", "", ""), "")   # type: ignore[assignment]
        assert C._mlx_pin() == "0.29.3"
        C.platform.mac_ver = lambda: ("14.5.0", ("", "", ""), "")   # type: ignore[assignment]
        assert C._mlx_pin() == "", "macOS 14+ 不该钉旧版"
        os.environ["VDL_CLONE_MLX_VERSION"] = "9.9.9"
        assert C._mlx_pin() == "9.9.9", "显式指定最优先"
    finally:
        C.platform.mac_ver = saved_mac                        # type: ignore[assignment]
        os.environ.pop("VDL_CLONE_MLX_VERSION", None)
        if saved_env is not None:
            os.environ["VDL_CLONE_MLX_VERSION"] = saved_env


def test_sub_env_strips_dead_proxy():
    """默认摘掉代理（坏代理会让 pip/下载报「连不上镜像」这种误导错），KEEP 时保留。"""
    saved = {k: os.environ.get(k) for k in ("HTTP_PROXY", "HTTPS_PROXY", "VDL_CLONE_KEEP_PROXY")}
    try:
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:53372"
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:53372"
        os.environ.pop("VDL_CLONE_KEEP_PROXY", None)
        env = C._sub_env()
        assert "HTTP_PROXY" not in env and "HTTPS_PROXY" not in env
        assert env.get("PIP_DISABLE_PIP_VERSION_CHECK") == "1"
        os.environ["VDL_CLONE_KEEP_PROXY"] = "1"
        env2 = C._sub_env()
        assert env2.get("HTTP_PROXY") == "http://127.0.0.1:53372", "显式 KEEP 时必须保留"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_never_mutates_process_hf_endpoint():
    """🔴 隔离纪律：本模块不得改写进程级 HF_ENDPOINT（镜像只走 snapshot_download 的 endpoint= 参数）。

    对齐 test_engine_isolation.py：历史上字幕模块在请求期改这个变量，
    把扩散去水印等其它功能的下载端点一起带偏了。
    """
    src = (Path(__file__).resolve().parent.parent / "clone_env.py").read_text(encoding="utf-8")
    for bad in ('os.environ["HF_ENDPOINT"]', "os.environ['HF_ENDPOINT']",
                'environ.setdefault("HF_ENDPOINT"', "HF_ENDPOINT\"] ="):
        assert bad not in src, f"clone_env.py 不得写进程级 HF_ENDPOINT（发现 {bad}）"
    assert "endpoint=HF_ENDPOINT" in src, "镜像必须显式作为 snapshot_download(endpoint=) 传入"

    os.environ.pop("HF_ENDPOINT", None)
    C.status()
    assert "HF_ENDPOINT" not in os.environ, "status() 不得顺手设 HF_ENDPOINT"


def test_routes_wired():
    """三个接口必须真的挂在 app 上（404 detail=="Not Found" 就是路由没 include）。

    ⚠️ 绝不让安装真的跑起来：把磁盘余量压到 1MB 让 start_install 提前拒绝，
    这样既验证了「路由 + Form 解析 + 拒绝分支返回 JSON」这条链路，又不会去下 2.4GB。
    """
    from fastapi.testclient import TestClient
    import app as server_app

    c = TestClient(server_app.app)

    r = c.get("/api/commentary/clone-env")
    assert r.status_code == 200, f"状态接口非 200：{r.status_code}"
    body = r.json()
    assert "ready" in body and "install" in body, f"状态接口字段不对：{list(body)[:8]}"

    # 把「已就绪短路」与「磁盘充足」都按下去，确定性走到磁盘闸门分支：
    # 既验证路由 + Form 解析 + 拒绝分支返回 JSON，又保证绝不会真去下 2.4GB
    # （本机若环境已就绪，start_install 会在磁盘检查之前就返回「已就绪」，
    #   那样这条断言就变成测环境而不是测接口了）。
    saved = (C._disk_free_mb, C.venv_ok, C.weights_ok)
    C._disk_free_mb = lambda p: 1                       # type: ignore[assignment]
    C.venv_ok = lambda: False                           # type: ignore[assignment]
    C.weights_ok = lambda: False                        # type: ignore[assignment]
    try:
        r2 = c.post("/api/commentary/clone-env/install", data={})
        assert r2.status_code == 200, f"安装接口非 200：{r2.status_code}"
        b2 = r2.json()
        assert b2.get("ok") is False and "磁盘" in (b2.get("msg") or ""), f"应被磁盘闸门拦下：{b2}"
    finally:
        C._disk_free_mb, C.venv_ok, C.weights_ok = saved   # type: ignore[assignment]

    r3 = c.post("/api/commentary/clone-env/cancel", data={})
    assert r3.status_code == 200, f"取消接口非 200：{r3.status_code}"
    assert "ok" in r3.json(), r3.json()


def test_route_order_before_job_id():
    """🔴 固定路径路由必须排在 /api/commentary/{job_id} 之前（仓库既有铁律）。

    否则 `/api/commentary/clone-env` 会被当成 job_id 匹配进详情接口，返回「任务不存在」，
    现象是「接口明明写了却 404」，极难查。
    """
    src = (Path(__file__).resolve().parent.parent / "routers" / "commentary.py").read_text(encoding="utf-8")
    i_env = src.find('"/api/commentary/clone-env"')
    i_job = src.find('"/api/commentary/{job_id}"')
    assert i_env != -1 and i_job != -1, "没找到路由定义"
    assert i_env < i_job, "clone-env 路由必须在 {job_id} 之前，否则会被当成 job_id 吞掉"


_TESTS = [
    test_status_shape_and_no_home_writes,
    test_find_venv_prefers_install_target_over_dev_path,
    test_find_venv_detects_torch_only_env,
    test_venv_ok_rejects_half_built,
    test_weights_ok_rejects_truncated,
    test_start_install_refuses_on_non_apple_silicon,
    test_start_install_short_circuits_when_ready,
    test_start_install_blocks_on_low_disk,
    test_cancel_when_idle,
    test_mlx_pin_by_macos_version,
    test_sub_env_strips_dead_proxy,
    test_never_mutates_process_hf_endpoint,
    test_routes_wired,
    test_route_order_before_job_id,
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
