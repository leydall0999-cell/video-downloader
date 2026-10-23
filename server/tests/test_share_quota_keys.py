#!/usr/bin/env python3
"""分享两个子功能并入账号级每日配额的离线回归测试（2026-10-20 新增）。

背景
----
2026-10-19 给**网页版**加了「生成二维码 / 生成网页」的每日配额统计
（web-dev 的 share_token.py + membership.FEATURE_USAGE_DEFS），但**从没
cherry-pick 到 app-dev**（两分支只 cherry-pick 不 merge）。后果：

  * App 会员中心「使用统计」与管理后台配额面板**完全没有这两行** ——
    用户 2026-10-20 截图对比网页版后发现并反馈；
  * App 侧分享上传**从不计数** —— 等于网页版在限次、桌面端无限用。

本文件锁住本次补齐的行为：

  _share_resource        src 分派（pagegen → share_page；其余 → share_qr）
  _share_quota_gate      配额闸门：放行 / 429 拒绝 / fail-open / 按 src 分派键
  share_upload_path      闸门顺序：**路径校验之后**才计数（文件不存在不扣额度）
  feature_usage_status   会员中心两行必须出现（免费 20/日、会员 200/日）

设计约束
--------
* **绝不联网**：闸门里的 member store 一律打桩（sys.modules["app"] 注入假模块）。
* **绝不写用户真实家目录**：MembershipStore 的 path 指向系统临时目录。

运行：
    cd server && python tests/test_share_quota_keys.py
    cd server && python -m pytest tests/test_share_quota_keys.py -v
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import traceback
import types
from pathlib import Path

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from routers import share as sh  # noqa: E402
import membership as M  # noqa: E402

from fastapi import HTTPException  # noqa: E402


class _FakeStore:
    """记录每次 use_daily 调用的假 member store。"""

    def __init__(self, ok: bool = True, exc: Exception | None = None):
        self.ok = ok
        self.exc = exc
        self.calls: list[tuple[str, int]] = []

    def use_daily(self, resource: str, n: int = 1, **kw):
        self.calls.append((resource, n))
        if self.exc is not None:
            raise self.exc
        if self.ok:
            return {"ok": True, "used": 1, "remaining": 19, "ref": "ref-fake"}
        return {"ok": False,
                "error": "今日免费处理额度已用尽（20/日）— 开通会员可解锁 200 次/日",
                "resource": resource, "code": "MEMBER_QUOTA"}


@contextlib.contextmanager
def stub_app(store: _FakeStore, *, path_raises: bool = False):
    """把 `import app` 换成假模块（闸门与端点都在函数体内局部 import app）。"""
    if path_raises:
        def _raise(p):
            raise HTTPException(status_code=400, detail="file_not_found")
        _assert = _raise
    else:
        _assert = lambda p: p  # noqa: E731
    fake = types.SimpleNamespace(
        current_member_store=lambda request: store,
        _assert_shareable_local_path=_assert,
        HTTPException=HTTPException,
    )
    old = sys.modules.get("app")
    sys.modules["app"] = fake
    try:
        yield fake
    finally:
        if old is None:
            sys.modules.pop("app", None)
        else:
            sys.modules["app"] = old


# ---------------------------------------------------------------- src 分派

def test_share_resource_dispatch():
    """pagegen（大小写/空白不敏感）→ share_page；其余一律 share_qr。"""
    for v in ("pagegen", "PAGEGEN", " PageGen ", "pagegen\n"):
        assert sh._share_resource(v) == "share_page", v
    for v in ("shareqr", "", None, "other", "PAGEGENX"):
        assert sh._share_resource(v) == "share_qr", repr(v)


# ---------------------------------------------------------------- 闸门行为

def test_gate_allows_when_quota_ok():
    st = _FakeStore(ok=True)
    with stub_app(st):
        assert sh._share_quota_gate(None, "shareqr") is None
        assert st.calls == [("share_qr", 1)], st.calls


def test_gate_denies_when_exhausted():
    st = _FakeStore(ok=False)
    with stub_app(st):
        r = sh._share_quota_gate(None, "pagegen")
    assert getattr(r, "status_code", None) == 429, r
    body = json.loads(r.body)
    assert body["code"] == "DAILY_LIMIT", body
    assert body["resource"] == "share_page", body
    assert "已用尽" in body["error"], body


def test_gate_fail_open_on_store_error():
    """配额引擎异常绝不阻断上传（与既有闸门一致的 fail-open）。"""
    st = _FakeStore(exc=RuntimeError("boom"))
    with stub_app(st):
        assert sh._share_quota_gate(None, "shareqr") is None


def test_gate_dispatches_key_by_src():
    st = _FakeStore(ok=True)
    with stub_app(st):
        sh._share_quota_gate(None, "pagegen")
        sh._share_quota_gate(None, "shareqr")
        sh._share_quota_gate(None, None)
    assert st.calls == [("share_page", 1), ("share_qr", 1), ("share_qr", 1)], st.calls


# ---------------------------------------------------------------- 顺序回归

def test_upload_path_missing_file_does_not_count():
    """🔴 顺序回归：闸门在路径校验**之后** —— 文件不存在/超限的请求不扣额度。

    2026-10-20 第一版曾把闸门放在路径校验之前（与自己的注释相反），
    用户选错文件也会被计一次。本用例钉住「先校验后计数」。
    """
    st = _FakeStore(ok=True)
    with stub_app(st, path_raises=True):
        r = sh.share_upload_path({"path": "/definitely/not/here.png", "src": "pagegen"})
    assert getattr(r, "status_code", None) == 400, r
    assert st.calls == [], f"文件不存在却计了配额：{st.calls}"


def test_upload_path_valid_file_counts_once():
    st = _FakeStore(ok=True)
    with tempfile.TemporaryDirectory(prefix="vdl_share_q_") as d:
        f = Path(d) / "a.bin"
        f.write_bytes(b"x" * 16)
        with stub_app(st):
            r = sh.share_upload_path({"path": str(f), "src": "pagegen"})
        # 上传节点不可达会走到后面失败，但配额必须已经计了（发起即计）
        assert st.calls == [("share_page", 1)], st.calls
        # 返回值要么 ok（节点桩不存在时多半不是 ok），但不许是 429 之外的配额拒绝
        if getattr(r, "status_code", None) == 429:
            raise AssertionError("配额充足却返回 429")


# ---------------------------------------------------------------- 功能表两行

def test_feature_rows_present_in_member_center():
    """会员中心「使用统计」与后台配额面板都按 FEATURE_USAGE_DEFS 生成行。"""
    old_env = os.environ.get("VDL_DATA_DIR")
    with tempfile.TemporaryDirectory(prefix="vdl_share_rows_") as d:
        os.environ["VDL_DATA_DIR"] = d
        try:
            store = M.MembershipStore(path=Path(d) / "membership.json")
            rows = {r["key"]: r for r in M.feature_usage_status(store, "today")}
        finally:
            if old_env is None:
                os.environ.pop("VDL_DATA_DIR", None)
            else:
                os.environ["VDL_DATA_DIR"] = old_env
    for key, name in (("share_qr", "生成二维码"), ("share_page", "生成网页")):
        assert key in rows, f"功能表缺少 {key}（会员中心/后台都看不到这一行）"
        assert rows[key]["name"] == name, rows[key]
        # 全新 store = 免费档：20 次/日
        assert rows[key]["daily_limit"] == 20, rows[key]
        assert rows[key]["daily_remaining"] == 20, rows[key]


# ---------------------------------------------------------------- runner

def _main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    ok = fail = 0
    for name, fn in tests:
        try:
            fn()
        except Exception:  # noqa: BLE001
            fail += 1
            print("❌ %s\n%s" % (name, traceback.format_exc()))
        else:
            ok += 1
            print("✓ %s" % name)
    print("\n通过 %d / 失败 %d（共 %d）" % (ok, fail, len(tests)))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(_main())
