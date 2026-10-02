"""A2/B4 下载失败矩阵重试与 aria2c 默认化回归测试（2026-10-02）。

- _retry_matrix：原始策略 + 切换下载器（有 aria2c 才有）+ HLS 链路，最多 3 项；
- _rebuild_requested_formats(protocol_filter)：只在指定协议族挑视频轨，无轨不动；
- _switch_to_hls：已有 HLS / 无 HLS 时返回 False，直链选中且存在 HLS 时重建；
- _download_options：VDL_DOWNLOADER=auto（新默认）有 aria2c 用 aria2c、无则原生 +
  http_chunk_size 分块（concurrent_fragment_downloads 才能并行拉直链）；
- B3 源码级钉死：硬上限到点时「仍在推进则延长等待」。
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("VDL_DATA_DIR", tempfile.mkdtemp())

import downloader  # noqa: E402
from downloader import (  # noqa: E402
    _retry_matrix,
    _rebuild_requested_formats,
    _switch_to_hls,
)
from tasks import DownloadTask  # noqa: E402

_A2C_PATH = "/usr/local/bin/aria2c"


def _with_aria2c(available: bool, fn) -> None:
    orig = downloader._aria2c_path
    downloader._aria2c_path = (lambda: _A2C_PATH) if available else (lambda: None)
    try:
        fn()
    finally:
        downloader._aria2c_path = orig


def test_retry_matrix_no_aria2c():
    def run():
        assert _retry_matrix("") == [("", False), ("", True)], _retry_matrix("")
        assert _retry_matrix("native") == [("native", False), ("native", True)]
    _with_aria2c(False, run)


def test_retry_matrix_with_aria2c():
    def run():
        # auto + 有 aria2c → 原策略实际用 aria2c → 变体切 native
        assert _retry_matrix("") == [("", False), ("native", False), ("", True)]
        assert _retry_matrix("aria2c") == [("aria2c", False), ("native", False), ("aria2c", True)]
        assert _retry_matrix("native") == [("native", False), ("aria2c", False), ("native", True)]
    _with_aria2c(True, run)


def _mk_formats():
    """构造 formats：https 渐进 720p(avc1)+https 音轨；m3u8 1080p(avc1)+m3u8 音轨。"""
    return {
        "formats": [
            {"format_id": "h720", "vcodec": "avc1.640028", "acodec": "none",
             "protocol": "https", "url": "https://cdn/v720.mp4", "height": 720,
             "ext": "mp4", "tbr": 2500},
            {"format_id": "a128", "vcodec": "none", "acodec": "mp4a.40.2",
             "protocol": "https", "url": "https://cdn/a128.m4a", "abr": 128},
            {"format_id": "h1080", "vcodec": "avc1.640032", "acodec": "none",
             "protocol": "m3u8_native", "url": "https://cdn/master.m3u8", "height": 1080,
             "ext": "mp4"},
            {"format_id": "ha1080", "vcodec": "none", "acodec": "mp4a.40.2",
             "protocol": "m3u8_native", "url": "https://cdn/hls_audio.m3u8", "abr": 128},
        ],
    }


def test_rebuild_requested_formats_protocol_filter():
    info = _mk_formats()
    _rebuild_requested_formats(info, "1080", protocol_filter="hls")
    rf = info["requested_formats"]
    assert rf[0]["format_id"] == "h1080", f"应选 HLS 视频轨，实际 {rf}"
    assert rf[1]["format_id"] == "ha1080", f"HLS 音轨应同族优先，实际 {rf[1]}"

    # 无 HLS 可用 → 不动原选择
    info2 = {"formats": [f for f in _mk_formats()["formats"] if f["protocol"] == "https"]}
    _rebuild_requested_formats(info2, "1080", protocol_filter="hls")
    assert "requested_formats" not in info2, "无 HLS 视频轨时不得重建"

    # 不带 filter 的原行为不回归：目标 1080 下取 ≤target 的最高高度轨（h1080）
    info3 = _mk_formats()
    _rebuild_requested_formats(info3, "1080")
    assert info3["requested_formats"][0]["format_id"] == "h1080"


def test_switch_to_hls():
    # 直链选中 + 存在 HLS → 切换成功
    info = _mk_formats()
    _rebuild_requested_formats(info, "720")
    assert info["requested_formats"][0]["protocol"] == "https"
    assert _switch_to_hls(info, "1080") is True
    assert info["requested_formats"][0]["format_id"] == "h1080"

    # 当前已是 HLS → False（不改坏）
    info_hls = _mk_formats()
    _rebuild_requested_formats(info_hls, "1080", protocol_filter="hls")
    before = [f["format_id"] for f in info_hls["requested_formats"]]
    assert _switch_to_hls(info_hls, "1080") is False
    after = [f["format_id"] for f in info_hls["requested_formats"]]
    assert before == after, "已是 HLS 时不得重建"

    # 无 HLS 可用 → False
    info_only = {"formats": [f for f in _mk_formats()["formats"] if f["protocol"] == "https"]}
    assert _switch_to_hls(info_only, "1080") is False


def _mk_task() -> DownloadTask:
    root = Path(tempfile.mkdtemp())
    (root / "w").mkdir()
    return DownloadTask(id="0" * 16, url="https://example.com/v", title="t",
                        platform="p", quality="best", workdir=root / "w")


class _FakeReporter:
    def __call__(self, d):
        pass

    def on_postprocess(self, d):
        pass


def test_download_options_auto_native_fallback():
    """auto + 无 aria2c → 原生 + http_chunk_size 分块。"""
    def run():
        task = _mk_task()
        opts = downloader._download_options(task, "best", _FakeReporter())
        assert "downloader" not in opts, f"无 aria2c 时不得启用外部下载器：{opts.get('downloader')}"
        assert opts.get("http_chunk_size") == 10 * 1024 * 1024, opts.get("http_chunk_size")
    _with_aria2c(False, run)


def test_download_options_auto_uses_aria2c():
    def run():
        task = _mk_task()
        opts = downloader._download_options(task, "best", _FakeReporter())
        assert opts.get("downloader") == "aria2c", f"auto + 有 aria2c 必须启用：{opts.get('downloader')}"
        assert "aria2c" in (opts.get("downloader_args") or {})
    _with_aria2c(True, run)


def test_download_options_explicit_native_and_chunk_env():
    def run():
        task = _mk_task()
        opts = downloader._download_options(task, "best", _FakeReporter(), downloader_type="native")
        assert "downloader" not in opts
        assert opts.get("http_chunk_size") == 10 * 1024 * 1024
        # 环境变量可调分块；=0 关闭（回滚开关）
        os.environ["VDL_HTTP_CHUNK_MB"] = "25"
        try:
            opts25 = downloader._download_options(task, "best", _FakeReporter())
            assert opts25.get("http_chunk_size") == 25 * 1024 * 1024
            os.environ["VDL_HTTP_CHUNK_MB"] = "0"
            opts0 = downloader._download_options(task, "best", _FakeReporter())
            assert "http_chunk_size" not in opts0
        finally:
            os.environ.pop("VDL_HTTP_CHUNK_MB", None)
    _with_aria2c(False, run)


def test_run_download_wiring_source_level():
    """源码级钉死：B3 硬上限延长 + A2 矩阵接线，防止回归。"""
    src = Path(downloader.__file__).read_text(encoding="utf-8")
    # B3：硬上限 join 在 while True 里，推进判定存在
    assert src.index("while True:") < src.index("th.join(timeout=DOWNLOAD_HARD_TIMEOUT)")
    assert 'task.status == "downloading" and (time.time() - last["ts"]) <= DOWNLOAD_STALL_TIMEOUT' in src
    # A2：矩阵接入 run_download，且 alt_protocol 传入 _run_once
    assert "_matrix = _retry_matrix(downloader_type)" in src
    assert "concurrent_fragments, _dt, resume, _alt" in src, "策略矩阵参数必须传进 _run_once"
    assert "if alt_protocol:" in src and "_switch_to_hls(info, quality_key)" in src
    # 总次数必须用矩阵长度兜底（否则 max_retries=0 时矩阵失效）
    assert "attempt >= _total_attempts" in src


if __name__ == "__main__":
    print("▶ A2/B4 下载失败矩阵与 aria2c 默认化回归测试")
    test_retry_matrix_no_aria2c()
    test_retry_matrix_with_aria2c()
    test_rebuild_requested_formats_protocol_filter()
    test_switch_to_hls()
    test_download_options_auto_native_fallback()
    test_download_options_auto_uses_aria2c()
    test_download_options_explicit_native_and_chunk_env()
    test_run_download_wiring_source_level()
    print("🎉 A2/B4 下载失败矩阵与 aria2c 默认化回归测试全部通过")
