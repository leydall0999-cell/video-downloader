"""守卫测试：物理内存探测必须在 Windows 上真的取到值。

2026-10-20 Win 版实测发现：`/api/dw/capability` 返回 `ram_bytes: 0 / ram_gb: 0.0`。
根因是 `_total_ram_bytes()` 只有三个分支——POSIX `os.sysconf`、macOS `sysctl`、
Linux `/proc/meminfo`——而 Windows 三者全落空（`os.sysconf` 是 POSIX 专有属性）。

后果不只是 UI 写着「检测到内存较小（约 0.0 GB）」：
  - `tier` 恒判成 `low`
  - 扩散档（`supported = ram_gb >= 16`）在 Windows 上**永久不可选**，32GB 机器也用不了
  - 低内存降级文案失去真实数值

运行：cd server && python -m pytest tests/test_capability_ram.py -q
"""
import inspect
import os
import sys

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import capability  # noqa: E402


def test_total_ram_is_positive_on_this_host():
    """真机上必须取到一个像样的值（>= 1GB），否则等于没探测。"""
    ram = capability._total_ram_bytes()
    assert ram >= 1024 ** 3, f"物理内存探测异常：{ram} 字节"


def test_windows_branch_uses_globalmemorystatusex():
    """Windows 分支必须存在：os.sysconf 在 Windows 上不可用。

    用源码守卫代替真的跳平台——回退成「只有 POSIX 分支」时立刻红。
    """
    src = inspect.getsource(capability._total_ram_bytes)
    if sys.platform == "win32":
        assert "GlobalMemoryStatusEx" in src, "Windows 分支丢了 GlobalMemoryStatusEx"
        assert "ullTotalPhys" in src
    # 任何平台都不该在 win32 上依赖 sysconf（这条只锁顺序意图，不锁具体写法）
    assert src.index("win32") < src.index("SC_PHYS_PAGES"), "win32 分支应排在 POSIX sysconf 之前"


def test_tier_consistent_with_ram():
    """tier 必须由真实 ram_gb 推出来，不能因为 ram=0 就恒定 low。"""
    ram_gb = capability._total_ram_bytes() / (1024 ** 3)
    tier = capability.probe_capability().get("tier")
    expect = ("extreme" if ram_gb >= 32 else "high" if ram_gb >= 16
              else "mid" if ram_gb >= 8 else "low")
    assert tier == expect, f"ram_gb={ram_gb:.1f} 应判 {expect}，实际 {tier}"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("全部通过")
