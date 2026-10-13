"""安全审计 2026-10-13（I8）：web AI 字幕(subtitle) 零测试覆盖补最小回归。

聚焦**路径安全护栏**与基础格式化（不触发 faster-whisper 推理）：
- subtitle._resolve_safe_local_path 契约：
  · 用户目录（/Users/、/home/、/Volumes/、C:\\）下真实文件 → 返回 Path；
  · 越界路径（如 /etc/passwd，存在但不属允许前缀）→ 抛 HTTPException(400)；
  · 不存在的文件 → 抛 HTTPException(400)。
- subtitle._fmt_ts 时间戳格式化基本正确。
"""
import os
import sys
import tempfile
from pathlib import Path

_SERVER = Path(__file__).resolve().parent.parent
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

import app  # noqa: E402  共享内核（subtitle 在 import 时依赖 app）
from routers import subtitle as st  # noqa: E402

_failures = []


def check(cond, label, detail=""):
    if cond:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}  {detail}")
        _failures.append(label)


def _raises_400(fn):
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        return getattr(e, "status_code", None) == 400
    return False


def test_resolve_safe_local_path_contract():
    # 合法：/Users/ 下真实文件 → 返回 Path
    allowed_dir = Path("/Users/suixindelang/WorkBuddy AI/2026-10-11-11-23-04")
    allowed_dir.mkdir(parents=True, exist_ok=True)
    f = allowed_dir / ".vdl_sub_test_tmp"
    f.write_text("ok")
    try:
        res = st._resolve_safe_local_path(str(f))
        check(isinstance(res, Path), "用户目录下真实文件返回 Path", str(res))
    finally:
        f.unlink(missing_ok=True)
    # 越界：/etc/passwd 存在但不属允许前缀 → 抛 400
    check(_raises_400(lambda: st._resolve_safe_local_path("/etc/passwd")),
          "越界路径（/etc/passwd）抛 400")
    # 不存在的文件 → 抛 400
    check(_raises_400(lambda: st._resolve_safe_local_path(
          "/Users/suixindelang/does_not_exist_xyz.mp4")),
          "不存在的文件抛 400")


def test_fmt_ts():
    check(st._fmt_ts(0) == "00:00:00,000", "_fmt_ts(0) == 00:00:00,000", st._fmt_ts(0))
    check(st._fmt_ts(3661) == "01:01:01,000", "_fmt_ts(3661) == 01:01:01,000", st._fmt_ts(3661))
    check(st._fmt_ts(60) == "00:01:00,000", "_fmt_ts(60) == 00:01:00,000", st._fmt_ts(60))


def main():
    print("=== I8 web subtitle 最小回归 ===")
    test_resolve_safe_local_path_contract()
    test_fmt_ts()
    if _failures:
        print(f"\n❌ {len(_failures)} 项未通过")
        sys.exit(1)
    print("\n✅ web subtitle 最小回归全过")


if __name__ == "__main__":
    main()
