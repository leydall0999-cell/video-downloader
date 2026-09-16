"""账号接管防护回归测试（2026-09-16 新增）：dev 模式的验证码**不得**回传给公网调用方。

背景（已实测复现的 P0，不是理论担忧）：
`POST /api/auth/reset-code` 在 dev 模式下把**验证码本身**放进响应体
（`{"ok": true, "dev_code": "216130", ...}`），随后拿该码调 `POST /api/auth/reset`
即可改掉该账号密码 —— 全程**不需要任何凭据**，只需知道账号标识。

原实现只判 `_send_mode() == "dev"`，而投递模式在**缺少 smtp.json 时自动回落 dev**
（实测生产 VPS 正是 dev：`_send_mode()='dev'`、`_smtp_accounts()=None`，且 `8.138.223.3:8888`
公网可达）⇒ 公网任何人 = 可接管任意账号。

修法：dev 模式的 `dev_code` **只回传给本机调用方**（桌面 App 前端固定访问 `127.0.0.1:8321`），
公网请求一律 `dev_code=None`。网页版要能重置密码应配 `smtp.json`（模式自动切 smtp），
而不是靠把验证码回传给调用方。

⚠️ 本判定依赖「反代后的 `request.client.host` 是真实客户端 IP」这一前提（VPS 实测
`vdl-web` 访问日志显示真实公网 IP，说明 uvicorn 采纳了 nginx 的 `X-Forwarded-For`）。
若某天 nginx 不再设该头，公网请求会退化成 `127.0.0.1` 而被误判为本机 —— 部署该改动时
必须复核这条（见 skill `vdl-build-release`）。

覆盖（8 项，全程不碰真实 ~/.video-downloader）：
  1) dev + 本机       → 回传 dev_code，且该码真能完成改密（保住桌面调试 UX）
  2) 🔴 dev + 公网     → dev_code 为 None
  3) 🔴 dev + 公网     → 拿不到码即无法接管（端到端反证：原密码仍可用）
  4) 未知账号         → 任何来源都不回传（防账号枚举）
  5) smtp 模式        → 任何来源都不回传，且确实调用了投递（桩拦，不发信）
  6) 源码棘轮         → dev_code 赋值行必须同时受 `_send_mode()` 与 `_is_loopback()` 约束
  7) 源码棘轮         → 本机白名单不得被放宽（只允许 127.0.0.1 / ::1 / localhost）
  8) 真实数据目录零污染

运行：
    cd server && python tests/test_auth_reset_code_leak.py
    cd server && python -m pytest tests/test_auth_reset_code_leak.py -v
"""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

_REAL_DATA_DIR = (Path.home() / ".video-downloader").resolve()
_TMP_ROOT = tempfile.mkdtemp(prefix="vdl_auth_leak_test_")

# 🔴 必须在 import app / auth_store **之前**设好，否则会落到真实数据目录
os.environ["VDL_DATA_DIR"] = _TMP_ROOT
os.environ.pop("VDL_SEND_MODE", None)          # 走自动判定：无 smtp.json ⇒ dev

_REAL_SNAPSHOT = {p.name for p in _REAL_DATA_DIR.iterdir()} if _REAL_DATA_DIR.exists() else set()


def _cleanup() -> None:
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)
    assert not os.path.exists(_TMP_ROOT), f"临时目录未清理干净：{_TMP_ROOT}"


atexit.register(_cleanup)

import auth_store  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402

# RFC 5737 文档用网段，确定不是本机
_LOOPBACK = ("127.0.0.1", 50000)
_PUBLIC = ("203.0.113.7", 50000)

_seq = [0]


def _client(host_port: tuple) -> TestClient:
    return TestClient(appmod.app, client=host_port)


def _new_account(password: str = "orig-pass-123") -> tuple:
    _seq[0] += 1
    ident = f"leakprobe{_seq[0]}@example.com"
    uid = auth_store.create_user(ident, password)
    assert uid, "建号失败"
    return ident, password


def test_dev_mode_is_self_consistent() -> None:
    """前提自检：隔离目录生效 + 当前确实是 dev 模式（否则后面几条断言无意义）。"""
    assert str(auth_store._base_dir()) == _TMP_ROOT, "VDL_DATA_DIR 隔离失效，立即中止"
    assert auth_store._send_mode() == "dev", f"本测试前提是 dev，实际 {auth_store._send_mode()}"
    print("✅ 前提：隔离目录生效 + 投递模式 = dev")


def test_dev_loopback_returns_code_and_code_works() -> None:
    """① dev + 本机：回传 dev_code，且该码真能改密（桌面 App 的调试体验不受影响）。"""
    ident, _old = _new_account()
    c = _client(_LOOPBACK)
    body = c.post("/api/auth/reset-code", json={"identifier": ident}).json()
    assert body.get("ok") is True, body
    code = body.get("dev_code")
    assert code, f"本机调用应回传 dev_code，实际 {body}"

    new_pw = "desktop-new-pass-456"
    r = c.post("/api/auth/reset", json={"identifier": ident, "password": new_pw, "code": code})
    assert r.json().get("ok") is True, r.json()
    assert auth_store.authenticate(ident, new_pw), "本机拿到的码应能完成改密"
    print("✅ ① dev + 本机：回传 dev_code 且改密成功（调试 UX 保留）")


def test_dev_public_client_gets_no_code() -> None:
    """② 🔴 dev + 公网：绝不回传验证码（本次修复的核心断言）。"""
    ident, _pw = _new_account()
    body = _client(_PUBLIC).post("/api/auth/reset-code", json={"identifier": ident}).json()
    assert body.get("ok") is True, "仍应返回 ok（防账号枚举）"
    assert body.get("dev_code") is None, f"🔴 公网调用拿到了验证码：{body}"
    assert "dev_code" in body, "字段应保留（前端按空值处理），只是值必须为 null"
    print("✅ ② dev + 公网：dev_code = null")


def test_public_client_cannot_take_over_account() -> None:
    """③ 🔴 端到端反证：公网调用者拿不到码 ⇒ 改不动密码 ⇒ 原密码仍可用。"""
    ident, old_pw = _new_account()
    c = _client(_PUBLIC)
    body = c.post("/api/auth/reset-code", json={"identifier": ident}).json()
    leaked = body.get("dev_code")
    assert leaked is None, f"公网不应拿到码：{body}"

    hijack_pw = "hijacked-999"
    for guess in ("000000", leaked or ""):
        r = c.post("/api/auth/reset", json={"identifier": ident, "password": hijack_pw, "code": guess})
        assert r.json().get("ok") is not True, f"公网改密不应成功：{r.json()}"

    assert auth_store.authenticate(ident, old_pw), "🔴 原密码必须仍然可用"
    assert not auth_store.authenticate(ident, hijack_pw), "🔴 劫持密码不得生效"
    print("✅ ③ 公网无法接管：原密码仍可用、劫持密码无效")


def test_unknown_identifier_never_returns_code() -> None:
    """④ 账号不存在时任何来源都不回传（防账号枚举）。"""
    for host, label in ((_LOOPBACK, "本机"), (_PUBLIC, "公网")):
        body = _client(host).post(
            "/api/auth/reset-code", json={"identifier": "nobody-9f3a@example.com"}
        ).json()
        assert body.get("ok") is True, f"{label}：账号不存在也应返回 ok（防枚举）"
        assert body.get("dev_code") is None, f"{label}：不得回传任何码"
    print("✅ ④ 未知账号：两种来源均不回传")


def test_smtp_mode_never_returns_code() -> None:
    """⑤ 切到 smtp 后不回传验证码，且确实走了投递（桩拦住，不发真信）。"""
    ident, _pw = _new_account()
    original = auth_store.deliver_reset_code
    delivered = []
    auth_store.deliver_reset_code = lambda i, c: delivered.append((i, c))
    os.environ["VDL_SEND_MODE"] = "smtp"
    try:
        body = _client(_LOOPBACK).post("/api/auth/reset-code", json={"identifier": ident}).json()
    finally:
        os.environ.pop("VDL_SEND_MODE", None)
        auth_store.deliver_reset_code = original

    assert body.get("dev_code") is None, f"smtp 模式不应回传验证码：{body}"
    assert delivered and delivered[0][1], "smtp 模式应真的调用投递（被桩记录）"
    print("✅ ⑤ smtp 模式：不回传，但投递被调用")


def test_source_ratchet_dev_code_is_gated() -> None:
    """⑥ 源码棘轮：dev_code 赋值必须同时受投递模式与本机判定约束（防改回去）。"""
    src = (Path(_SERVER_DIR) / "routers" / "auth.py").read_text(encoding="utf-8")
    lines = [l.strip() for l in src.splitlines() if "dev_code = " in l and "dev_code =" in l]
    assert len(lines) == 1, f"应只有一处 dev_code 赋值，实际 {len(lines)} 处：{lines}"
    line = lines[0]
    assert "_send_mode()" in line, f"dev_code 必须受投递模式约束：{line}"
    assert "_is_loopback(" in line, f"dev_code 必须受本机判定约束：{line}"
    print("✅ ⑥ 源码棘轮：dev_code 同时受 _send_mode 与 _is_loopback 约束")


def test_source_ratchet_loopback_whitelist_is_minimal() -> None:
    """⑦ 源码棘轮：本机白名单不得被放宽（0.0.0.0 之类会把公网当本机）。"""
    src = (Path(_SERVER_DIR) / "routers" / "auth.py").read_text(encoding="utf-8")
    marker = '_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})'
    assert marker in src, f"本机白名单应恰为 {marker}"
    for bad in ("0.0.0.0", "*"):
        assert f'"{bad}"' not in src, f"白名单不得含 {bad}"
    print("✅ ⑦ 源码棘轮：本机白名单最小化")


def test_home_dir_untouched() -> None:
    """⑧ 全程不得写真实数据目录。"""
    after = {p.name for p in _REAL_DATA_DIR.iterdir()} if _REAL_DATA_DIR.exists() else set()
    added = after - _REAL_SNAPSHOT
    assert not added, f"测试污染了用户数据目录：多出 {sorted(added)}"
    print("✅ ⑧ 未向真实 ~/.video-downloader 写入任何文件")


if __name__ == "__main__":
    test_dev_mode_is_self_consistent()
    test_dev_loopback_returns_code_and_code_works()
    test_dev_public_client_gets_no_code()
    test_public_client_cannot_take_over_account()
    test_unknown_identifier_never_returns_code()
    test_smtp_mode_never_returns_code()
    test_source_ratchet_dev_code_is_gated()
    test_source_ratchet_loopback_whitelist_is_minimal()
    test_home_dir_untouched()
    print("\n🎉 账号接管防护测试全部通过（8 项 + 1 项前提自检）")
