"""SenseVoice 模型懒加载单测（2026-10-15，安装包瘦身 228MB）。

背景：model.int8.onnx 占安装包 49.5%，改为首次使用本地字幕时从 R2 下载
（server/routers/subtitle.py::_ensure_sensevoice_model）。本文件覆盖：
  1. 包内已带模型（旧安装包）→ 直接返回 True，零下载
  2. 缓存标记命中（.model-ok + sha 相符）→ True，零下载
  3. 正常下载路径：本地 HTTP 服务小归档 → sha 校验 → 解压 → 写标记 → True，
     且下载进度写入 job["stage"]/["progress"]（前端轮询可见）
  4. sha 不符 → False，且不解压、不写标记（半途/被篡改的包永不被当成模型）
  5. 下载失败（拒连）→ False（重试后仍失败，不抛异常阻断字幕任务）
  6. 接线守卫：_run_subtitle 确有以下发下载调用；build_mac.sh 默认不打包模型；
     下载 URL 指向 R2 自定义域

隔离：整个进程 HOME 指向临时目录（subtitle 用 _Path.home()/.cache/vdl-sensevoice），
HTTP 服务只听 127.0.0.1 随机端口，不碰外网、不碰真实模型缓存。

运行（独立进程）：
    cd server && .build_venv/bin/python tests/test_sensevoice_lazy_model.py
"""
import hashlib
import http.server
import os
import sys
import tarfile
import tempfile
import threading
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="vdl_sb_svmodel_")
os.environ["HOME"] = _TMP
os.environ["VDL_DATA_DIR"] = os.path.join(_TMP, ".video-downloader")
os.makedirs(os.environ["VDL_DATA_DIR"], exist_ok=True)

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402
import routers.subtitle as sb  # noqa: E402

server_app  # 保持 import 顺序（app 必须先于 routers 加载）

_CACHE = Path(_TMP) / ".cache" / "vdl-sensevoice"
_DEAD_PORT = 1                      # 拒连端口（tcpmux/端口分类服务，必拒）


def _mk_archive(payload: bytes = b"fake-onnx-bytes") -> bytes:
    """构造一个与真实归档同构的小 tar.xz（平级两个文件）。"""
    import io
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tf:
        for name in ("model.int8.onnx", "tokens.txt"):
            data = payload if name.endswith(".onnx") else b"<blk>\n"
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = self.server.payload  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/x-xz")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):      # 静音
        pass


def _serve(payload: bytes):
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    srv.payload = payload          # type: ignore[attr-defined]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/sensevoice-v1.tar.xz"


def _reset(sb_url=None, sb_sha=None):
    """清缓存现场 + 重置模块常量（monkeypatch 手写版，无 pytest 依赖）。"""
    import shutil
    if _CACHE.exists():
        shutil.rmtree(_CACHE)
    sb._SV_MODEL_URL = sb_url if sb_url is not None else f"http://127.0.0.1:{_DEAD_PORT}/x"
    sb._SV_MODEL_SHA = sb_sha if sb_sha is not None else "0" * 64


def test_bundled_model_skips_download():
    """① 包内自带（旧安装包）：_sensevoice_dir 命中 → True，且绝不发请求。"""
    _reset()
    orig = sb._sensevoice_dir
    sb._sensevoice_dir = lambda: Path(_TMP) / "fake-bundle"
    try:
        assert sb._ensure_sensevoice_model() is True
        assert not (_CACHE / ".model-ok").exists()
        print("✅ 1. 包内已带模型：直接命中，零下载")
    finally:
        sb._sensevoice_dir = orig


def test_marker_hit_skips_download():
    """② 缓存标记命中：True，零下载。"""
    _reset()
    _CACHE.mkdir(parents=True, exist_ok=True)
    good_sha = hashlib.sha256(b"x").hexdigest()
    (_CACHE / ".model-ok").write_text(good_sha)
    sb._SV_MODEL_SHA = good_sha
    assert sb._ensure_sensevoice_model() is True
    print("✅ 2. 缓存标记命中：直接返回，零下载")


def test_happy_path():
    """③ 正常下载：解压 + 标记 + 进度回报。"""
    _reset()
    payload = _mk_archive()
    srv, url = _serve(payload)
    try:
        sb._SV_MODEL_URL = url
        sb._SV_MODEL_SHA = hashlib.sha256(payload).hexdigest()
        job = {"stage": "", "progress": 0}
        assert sb._ensure_sensevoice_model(job) is True
        assert (_CACHE / "model.int8.onnx").read_bytes() == b"fake-onnx-bytes"
        assert (_CACHE / "tokens.txt").read_bytes() == b"<blk>\n"
        assert (_CACHE / ".model-ok").read_text().strip() == sb._SV_MODEL_SHA
        assert not (_CACHE / "sensevoice-v1.tar.xz.part").exists()
        assert "下载语音识别模型" in job["stage"], job
        assert 5 <= job["progress"] <= 15, job
        # 幂等：第二次调用零请求（走标记）
        sb._SV_MODEL_URL = f"http://127.0.0.1:{_DEAD_PORT}/x"
        assert sb._ensure_sensevoice_model() is True
        print("✅ 3. 正常下载：解压/标记/进度回报/幂等全过")
    finally:
        srv.shutdown()


def test_sha_mismatch():
    """④ sha 不符：False，不解压不写标记。"""
    _reset()
    payload = _mk_archive()
    srv, url = _serve(payload)
    try:
        sb._SV_MODEL_URL = url
        sb._SV_MODEL_SHA = "f" * 64          # 故意错
        assert sb._ensure_sensevoice_model() is False
        assert not (_CACHE / "model.int8.onnx").exists()
        assert not (_CACHE / ".model-ok").exists()
        assert not (_CACHE / "sensevoice-v1.tar.xz.part").exists()
        print("✅ 4. sha 不符：拒收并清理现场，不污染缓存")
    finally:
        srv.shutdown()


def test_download_failure_degrades():
    """⑤ 下载失败（拒连 x3）：False，不抛异常（字幕任务继续走纯 Whisper）。"""
    _reset()
    assert sb._ensure_sensevoice_model() is False
    assert not (_CACHE / "model.int8.onnx").exists()
    print("✅ 5. 下载失败：静默降级返回 False，不阻断字幕任务")


def test_wiring():
    """⑥ 接线守卫：源码级断言，防「函数写了却没接上/构建侧没改」。"""
    src = Path(_SERVER_DIR) / "routers" / "subtitle.py"
    text = src.read_text(encoding="utf-8")
    assert "_ensure_sensevoice_model(job)" in text, "_run_subtitle 未下发下载调用"
    assert "download.hanyuxz.top/models/sensevoice-v1.tar.xz" in text, "URL 未指向 R2"
    build = Path(_SERVER_DIR).parent / "desktop" / "build_mac.sh"
    btext = build.read_text(encoding="utf-8")
    assert 'VDL_BUNDLE_SENSEVOICE:-0' in btext, "构建侧未改为默认不打包模型"
    print("✅ 6. 接线守卫：运行时分发/URL/构建侧默认跳过全部在位")


if __name__ == "__main__":
    test_bundled_model_skips_download()
    test_marker_hit_skips_download()
    test_happy_path()
    test_sha_mismatch()
    test_download_failure_degrades()
    test_wiring()
    print("\n全部通过 ✅")
