"""B1/A1 下载预检与 PO Token 会话保持回归测试（2026-10-02）。

- _estimate_info_bytes：DASH 双流取和 / 单流 filesize / approx 兜底 / 未知=0；
- _preflight_space_check：超上限给 too_large 人话错误、磁盘不足给 disk_full、
  体积未知时磁盘 <1GB 兜底拦、正常路径不抛；
- _MAX_FILE_MB 默认 20GB（B1 解锁大文件），环境变量仍可覆盖；
- 403 降级链带 visitor_data（A1）：源码级钉死 _fb_opts 注入，防止回归丢失会话。
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("VDL_DATA_DIR", tempfile.mkdtemp())

import downloader  # noqa: E402
from downloader import (  # noqa: E402
    _estimate_info_bytes,
    _fmt_gb,
    _MAX_FILE_MB,
    ResolveError,
    _preflight_space_check,
)
from tasks import DownloadTask  # noqa: E402


def test_estimate_info_bytes():
    assert _estimate_info_bytes({}) == 0
    assert _estimate_info_bytes("not-a-dict") == 0
    assert _estimate_info_bytes({"filesize": 100}) == 100
    assert _estimate_info_bytes({"filesize_approx": 120}) == 120
    dash = {"requested_formats": [{"filesize": 90}, {"filesize_approx": 10}]}
    assert _estimate_info_bytes(dash) == 100, "DASH 双流必须取和"


def test_limit_default_unlocked():
    assert _MAX_FILE_MB >= 20480, f"默认上限应 >= 20GB（当前 {_MAX_FILE_MB}）"


def _mk_task(root: Path) -> DownloadTask:
    return DownloadTask(id="0" * 16, url="https://example.com/v", title="t",
                        platform="p", quality="best", workdir=root / "w")


def test_preflight_rejects_over_limit():
    root = Path(tempfile.mkdtemp())
    (root / "w").mkdir()
    task = _mk_task(root)
    big = {"filesize": downloader._MAX_FILE_BYTES + 1}
    try:
        _preflight_space_check(task, big)
        raise AssertionError("超上限必须抛 ResolveError")
    except ResolveError as e:
        assert "上限" in e.message and "超过" in e.message, e.message
        assert e.category == "too_large"


class _FakeUsage:
    def __init__(self, free):
        self.free = free
        self.total = free * 2
        self.used = free


def test_preflight_rejects_low_disk():
    root = Path(tempfile.mkdtemp())
    (root / "w").mkdir()
    task = _mk_task(root)
    import shutil as _sh
    orig = _sh.disk_usage
    _sh.disk_usage = lambda p: _FakeUsage(free=2 * 1024 ** 3)   # 剩 2GB
    try:
        try:
            _preflight_space_check(task, {"filesize": 10 * 1024 ** 3})   # 要 10GB → 必拦
            raise AssertionError("磁盘不足必须抛 ResolveError")
        except ResolveError as e:
            assert "磁盘空间不足" in e.message and e.category == "disk_full", e.message
        try:
            _preflight_space_check(task, {})   # 体积未知，要求 ≥1GB，剩 2GB… patch 更小再试
            # 剩 2GB > 1GB 兜底线 → 不拦（预期通过）
        except ResolveError:
            raise AssertionError("体积未知且余量 2GB 不应被拦")
        _sh.disk_usage = lambda p: _FakeUsage(free=512 * 1024 * 1024)   # 剩 512MB
        try:
            _preflight_space_check(task, {})   # est=0，余量 <1GB → 必拦
            raise AssertionError("体积未知且磁盘紧张必须拦")
        except ResolveError as e:
            assert e.category == "disk_full"
    finally:
        _sh.disk_usage = orig


def test_preflight_passes_normal_case():
    root = Path(tempfile.mkdtemp())
    (root / "w").mkdir()
    task = _mk_task(root)
    _preflight_space_check(task, {"filesize": 1024})   # 小文件 + 充足磁盘 → 不抛
    _preflight_space_check(task, {})                    # 未知体积 + 充足磁盘 → 不抛


def test_fmt_gb():
    assert _fmt_gb(0) == "0 B"
    assert _fmt_gb(2048).endswith("KB")
    assert "GB" in _fmt_gb(5 * 1024 ** 3)


def test_fallback_keeps_visitor_data_source_level():
    """A1 钉死：403 降级链必须把 visitor_data 带进 _fb_opts（源码级契约）。"""
    src = Path(downloader.__file__).read_text(encoding="utf-8")
    # 1) visitor_data 在首试注入，且降级循环里重新注入（丢失会话 = 照样 403）
    assert '"visitor_data"' in src
    assert '_ya["visitor_data"] = [_yd_vd]' in src, "降级链必须回填 visitor_data"
    # 2) _fb_opts 必须在 visitor_data 注入之前构建（顺序契约）
    assert src.index("_fb_opts = dict(_fb_base)") < src.index('_ya["visitor_data"]'), \
        "降级 options 构建必须先于 visitor_data 注入"


if __name__ == "__main__":
    print("▶ 下载预检与 PO Token 会话保持回归测试（B1/A1）")
    test_estimate_info_bytes()
    test_limit_default_unlocked()
    test_preflight_rejects_over_limit()
    test_preflight_rejects_low_disk()
    test_preflight_passes_normal_case()
    test_fmt_gb()
    test_fallback_keeps_visitor_data_source_level()
    print("🎉 下载预检与 PO Token 会话保持回归测试全部通过")
