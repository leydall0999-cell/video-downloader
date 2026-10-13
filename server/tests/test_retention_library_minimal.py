"""安全审计 2026-10-13（I7/I8）：app 时效清理(retention) 与媒体库(library) 零测试覆盖补最小回归。

聚焦**安全相关纯逻辑**（不依赖 ffmpeg / 真实磁盘扫描）：
- library.encode_id / decode_id 往返稳定（媒体库项 id 是路径的编码，须可还原且无歧义）；
- library._resolve_safe 拒绝路径穿越（lib_id 含 `../` 或绝对路径越界 → None）；
- retention._safe_inside 正确判定「目标是否仍在根目录内」（防清空/越界删除）；
- retention._age_days 基本量纲正确。
"""
import os
import sys
import tempfile
from pathlib import Path

_SERVER = Path(__file__).resolve().parent.parent
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

import library as lib  # noqa: E402
import retention as ret  # noqa: E402

_failures = []


def check(cond, label, detail=""):
    if cond:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}  {detail}")
        _failures.append(label)


def test_library_id_roundtrip():
    rel = "2026/10/clip.mp4"
    enc = lib.encode_id(rel)
    check(enc != rel, "encode_id 不是明文路径", enc)
    check(lib.decode_id(enc) == rel, "decode_id 往返一致", f"{enc!r} -> {lib.decode_id(enc)!r}")


def test_library_resolve_safe_rejects_traversal():
    root = Path(tempfile.mkdtemp(prefix="vdl_lib_"))
    # 合法 lib_id 必须对应真实文件才会返回路径（_resolve_safe 内部 is_file 门禁）
    inside_file = root / "a" / "b.mp4"
    inside_file.parent.mkdir(parents=True, exist_ok=True)
    inside_file.write_text("x")
    inside = lib.encode_id("a/b.mp4")
    check(lib._resolve_safe(root, inside) is not None, "合法 lib_id（真实文件）可解析")
    # 穿越：编码里塞入向上路径
    traversal = lib.encode_id("../etc/passwd")
    check(lib._resolve_safe(root, traversal) is None, "向上穿越被拒")
    # 绝对路径越界
    abs_id = lib.encode_id("/etc/passwd")
    check(lib._resolve_safe(root, abs_id) is None, "绝对路径越界被拒")


def test_retention_safe_inside():
    root = Path(tempfile.mkdtemp(prefix="vdl_ret_"))
    inside = root / "sub" / "x.mp4"
    check(ret._safe_inside(root, inside) is True, "根内目标判定为内")
    outside = root.parent / "escape.mp4"
    check(ret._safe_inside(root, outside) is False, "根外目标判定为外（防越界删除）")


def test_retention_age_days():
    import time
    now = time.time()
    old = now - 86400 * 3.5
    check(abs(ret._age_days(old, now) - 3.5) < 0.01, "_age_days 量纲正确（3.5 天）")


def main():
    print("=== I7/I8 app retention/library 最小回归 ===")
    test_library_id_roundtrip()
    test_library_resolve_safe_rejects_traversal()
    test_retention_safe_inside()
    test_retention_age_days()
    if _failures:
        print(f"\n❌ {len(_failures)} 项未通过")
        sys.exit(1)
    print("\n✅ app retention/library 最小回归全过")


if __name__ == "__main__":
    main()
