"""P1 回归测试（回调幂等 / 超管 bootstrap / 保险箱明文 / 云账号身份）。

覆盖缺陷（安全审计 2026-10-11）：
  · P1-1 回调重复发权益：pay_server 两处 notify 只判 `status == "PAID"`，而真正的
    `_grant` 在锁**外** ⇒ 并发/重试通知双双通过检查即重复发货；授权中心 grant_impl
    也没有订单级幂等 ⇒ 会员天数/积分翻倍（真实账目损失）。
  · P1-2 超管 bootstrap 抢占：`ensure_superusers()` 在无显式名单且无超管时把
    **最早注册账号**提为超管 ⇒ 公网全新实例被抢注一个账号即拿到后台全部权限。
  · P1-7 保险箱明文持久化：`library_encfile` 把 .vdlenc 解密到 `DOWNLOAD_DIR/.vault_tmp`
    后**从不删除**，且 `scan_library`/`_resolve_safe` 都不排除该目录 ⇒ 以为「已加锁」
    的明文可被当普通媒体文件列出并下载。
  · P1-8 云账号全局身份：`/api/cloud/*` 一律操作全局账本且不校验调用者 ⇒ 任何可达
    端口者可读他人账号邮箱/设备、把别人登出、踢设备；`MembershipStore._cloud_token`
    回落全局 token 时不校验归属 ⇒ 越权借用他人账号身份。

设计约束：全部用临时目录 + monkeypatch，不联网、不碰真实 ~/.video-downloader。
"""
import os
import sys
import tempfile
import time
import types
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="vdl_p1b_")
os.environ["VDL_DATA_DIR"] = str(Path(_TMP) / "data")
os.environ["VDL_LICENSE_DATA"] = str(Path(_TMP) / "cards.json")
os.environ["VDL_LICENSE_SECRET"] = "test-secret-p1"
os.environ["VDL_LICENSE_ADMIN_TOKEN"] = "test-admin-p1"
os.environ.pop("VDL_ADMIN_IDENTIFIER", None)

_SERVER_DIR = str(Path(__file__).resolve().parent.parent)
_DEPLOY_DIR = str(Path(_SERVER_DIR).parent / "deploy")
for _p in (_SERVER_DIR, _DEPLOY_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import app as server_app  # noqa: E402
import auth_store as au  # noqa: E402
import membership as ms  # noqa: E402
import library as lib  # noqa: E402
import license_server as ls  # noqa: E402
import pay_server as ps  # noqa: E402
from routers import cloud_account as ca  # noqa: E402
from routers import library as lib_router  # noqa: E402


class _patch:
    def __init__(self, **attrs):
        self._attrs = attrs
        self._orig = {}

    def __enter__(self):
        for k, v in self._attrs.items():
            obj, val = v if isinstance(v, tuple) else (server_app, v)
            self._orig[k] = (obj, k, getattr(obj, k))
            setattr(obj, k, val)
        return self

    def __exit__(self, *exc):
        for _k, (obj, name, old) in self._orig.items():
            setattr(obj, name, old)
        return False


def _err_code(exc) -> object:
    return getattr(exc, "status_code", getattr(exc, "status", None))


# ============================================================ P1-1 回调幂等

def _license_state_with_user(email="u1@example.com"):
    st = {}
    ls._users(st)[email] = {"email": email, "created_at": time.time(),
                            "devices": [], "purchases": []}
    return st, email


def test_grant_is_idempotent_by_order_key():
    st, email = _license_state_with_user()
    now = time.time()
    note = "xunhupay-auto:VDLORDER00000001"
    r1 = ls.grant_impl(st, email, "DL7", now, note=note)
    assert r1.get("ok") and not r1.get("duplicate")
    exp1 = ls._users(st)[email]["purchases"][0]["id"]

    r2 = ls.grant_impl(st, email, "DL7", now + 1, note=note)
    assert r2.get("duplicate") is True, r2
    assert r2.get("purchase_id") == exp1
    assert len(ls._users(st)[email]["purchases"]) == 1, "同一订单重复回调发了两笔"


def test_grant_different_orders_both_land():
    st, email = _license_state_with_user()
    now = time.time()
    ls.grant_impl(st, email, "DL7", now, note="xunhupay-auto:VDLA")
    ls.grant_impl(st, email, "DL7", now + 1, note="xunhupay-auto:VDLB")
    assert len(ls._users(st)[email]["purchases"]) == 2


def test_manual_grant_is_not_deduped():
    """管理员手动补发（note 非 <渠道>-auto:）不受订单级幂等约束。"""
    st, email = _license_state_with_user()
    now = time.time()
    ls.grant_impl(st, email, "DL7", now, note="手动补发")
    ls.grant_impl(st, email, "DL7", now + 1, note="手动补发")
    assert len(ls._users(st)[email]["purchases"]) == 2


def test_order_key_parser():
    assert ls._order_key_of_note("xunhupay-auto:VDL123") == "xunhupay:VDL123"
    assert ls._order_key_of_note("alipay-auto:VDL9") == "alipay:VDL9"
    assert ls._order_key_of_note("手动补发") == ""
    assert ls._order_key_of_note("") == ""
    assert ls._order_key_of_note(None) == ""


def test_pay_server_claim_grant_cas():
    now = time.time()
    assert ps._claim_grant({"status": "PAID"}, now) is False
    inflight = {"status": "GRANTING", "granting_at": now - 10}
    assert ps._claim_grant(inflight, now) is False          # 另一次通知正在发货
    stale = {"status": "GRANTING", "granting_at": now - 9999}
    assert ps._claim_grant(stale, now) is True              # 卡死 → 允许重试
    assert stale["status"] == "GRANTING" and stale["granting_at"] == now
    fresh = {"status": "PENDING"}
    assert ps._claim_grant(fresh, now) is True
    assert fresh["granting_at"] == now


# ============================================================ P1-2 超管 bootstrap

def _new_account(tag):
    ident = f"{tag}@example.com"
    uid = au.create_user(ident, "Pw#123456")
    assert uid
    return ident, uid


def test_bootstrap_disabled_does_not_promote_first_user():
    """公网路径（allow_bootstrap=False）：不得把最早注册账号提为超管。"""
    ident, uid = _new_account("pubfirst")
    au._SUPERUSER_CACHE["ts"] = 0.0
    au.ensure_superusers(allow_bootstrap=False)
    assert au.user_is_admin(uid) is False, "公网路径不应自动提权"


def test_bootstrap_enabled_promotes_on_desktop():
    """桌面路径（allow_bootstrap=True）：无显式名单时把最早注册账号提为超管。

    注意本用例不依赖执行顺序：断言「确实产生了一个超管」，
    至于是哪个账号由「最早注册」规则决定。
    """
    _new_account("deskfirst")
    au._SUPERUSER_CACHE["ts"] = 0.0
    au.ensure_superusers(allow_bootstrap=True)
    data = au._load_users()
    assert any(u.get("is_admin") for u in data["users"]), "桌面路径应兜底产生超管"


def test_explicit_admin_list_works_regardless_of_bootstrap_flag():
    ident, uid = _new_account("listed")
    os.environ["VDL_ADMIN_IDENTIFIER"] = ident
    try:
        au._SUPERUSER_CACHE["ts"] = 0.0
        au.ensure_superusers(allow_bootstrap=False)
        assert au.user_is_admin(uid) is True
    finally:
        os.environ.pop("VDL_ADMIN_IDENTIFIER", None)


# ============================================================ P1-7 保险箱明文

def test_scan_library_skips_vault_tmp_and_dot_dirs():
    root = Path(tempfile.mkdtemp(prefix="vdl_lib_"))
    (root / "normal.mp4").write_bytes(b"x")
    vt = root / ".vault_tmp"
    vt.mkdir(parents=True, exist_ok=True)
    (vt / "abc.dec").write_bytes(b"plaintext")
    hid = root / ".hidden"
    hid.mkdir(parents=True, exist_ok=True)
    (hid / "secret.mp4").write_bytes(b"x")

    names = {i["name"] for i in lib.scan_library(root)}
    assert "normal.mp4" in names
    assert "abc.dec" not in names, "保险箱解密明文被列进媒体库"
    assert "secret.mp4" not in names, "点开头目录不应进媒体库"


def test_resolve_safe_refuses_vault_tmp_path():
    root = Path(tempfile.mkdtemp(prefix="vdl_lib_")).resolve()   # macOS /var → /private/var 软链
    vt = root / ".vault_tmp"
    vt.mkdir(parents=True, exist_ok=True)
    f = vt / "abc.dec"
    f.write_bytes(b"plaintext")
    lib_id = lib.encode_id(".vault_tmp/abc.dec")
    assert lib._resolve_safe(root, lib_id) is None, "构造 id 仍能取到解密明文"
    ok = root / "ok.mp4"
    ok.write_bytes(b"x")
    got = lib._resolve_safe(root, lib.encode_id("ok.mp4"))
    assert got is not None and got.resolve() == ok.resolve()


def test_purge_vault_tmp_removes_plaintext():
    tmpdir = Path(tempfile.mkdtemp(prefix="vdl_vt_"))
    (tmpdir / "a.dec").write_bytes(b"secret")
    (tmpdir / "b.dec").write_bytes(b"secret")
    with _patch(VAULT_TMP=tmpdir):
        lib_router._purge_vault_tmp()
    assert list(tmpdir.glob("*.dec")) == [], "解密明文未被清理"

    keep = tmpdir / "keep.dec"
    keep.write_bytes(b"secret")
    other = tmpdir / "other.dec"
    other.write_bytes(b"secret")
    with _patch(VAULT_TMP=tmpdir):
        lib_router._purge_vault_tmp(keep=keep)
    assert keep.exists() and not other.exists()


def test_vault_range_decrypt_matches_plaintext():
    """分块 GCM 随机访问解密：任意区间都必须等于原文对应切片（含跨块边界）。"""
    import crypto_vault as cv
    tmp = Path(tempfile.mkdtemp(prefix="vdl_vr_"))
    payload = bytes((i * 7 + 3) % 256 for i in range(5000))
    src = tmp / "in.bin"
    src.write_bytes(payload)
    enc = tmp / "out.vdlenc"
    key = os.urandom(32)

    # 把块大小压到 1 KiB，才能真的踩到「跨块边界」这条路径
    with _patch(CHUNK=(cv, 1024)):
        cv.encrypt_file(src, enc, key, "clip.mp4", "video")
        assert cv.plaintext_size(enc) == len(payload)
        assert cv.decrypt_range(enc, key, 0, len(payload) - 1)[0] == payload
        for a, b in [(0, 0), (1023, 1024), (1024, 2047), (500, 3500),
                     (4096, 4999), (4999, 4999), (2000, 2000), (1024, 1024)]:
            got = cv.decrypt_range(enc, key, a, b)[0]
            assert got == payload[a:b + 1], (a, b, len(got))
        # 末尾开放区间（end 超出真实长度 → 截到末尾）
        assert cv.decrypt_range(enc, key, 4500, 10 ** 9)[0] == payload[4500:]
        # 起点越过末尾 → 空
        assert cv.decrypt_range(enc, key, 6000, 7000)[0] == b""
        # 文件头里的原名/类型一并带回（返回值是 (data, orig_name, orig_kind)）
        _data, name, kind = cv.decrypt_range(enc, key, 0, 10)
        assert (name, kind) == ("clip.mp4", "video")


def test_vault_range_decrypt_rejects_tampered_chunk():
    """篡改密文必须解密失败（GCM 认证标签），不能静默返回错数据。"""
    import crypto_vault as cv
    tmp = Path(tempfile.mkdtemp(prefix="vdl_vr_"))
    payload = b"A" * 3000
    src = tmp / "in.bin"
    src.write_bytes(payload)
    enc = tmp / "out.vdlenc"
    key = os.urandom(32)
    with _patch(CHUNK=(cv, 1024)):
        cv.encrypt_file(src, enc, key, "clip.mp4", "video")
        raw = bytearray(enc.read_bytes())
        raw[-1] ^= 0xFF                      # 破坏最后一块的认证标签
        enc.write_bytes(bytes(raw))
        try:
            cv.decrypt_range(enc, key, 2000, 2500)
            raise AssertionError("篡改后的块不应解密成功")
        except Exception as e:  # noqa: BLE001
            assert "InvalidTag" in type(e).__name__ or "InvalidTag" in str(e), e


def test_encfile_range_serves_without_writing_plaintext():
    """端点级：Range 请求返回 206 + 正确 Content-Range，且**不产生任何落盘明文**。"""
    import crypto_vault as cv
    root = Path(tempfile.mkdtemp(prefix="vdl_enc_")).resolve()
    payload = bytes(range(256)) * 20          # 5120 字节
    src = root / "clip.mp4"
    src.write_bytes(payload)
    key = os.urandom(32)
    enc = root / "clip.mp4.vdlenc"
    cv.encrypt_file(src, enc, key, "clip.mp4", "video")
    src.unlink()
    vtmp = root / ".vault_tmp"
    lib_id = lib.encode_id("clip.mp4.vdlenc")

    def _req(rng):
        return types.SimpleNamespace(
            headers={"range": rng} if rng else {},
            client=types.SimpleNamespace(host="127.0.0.1"), query_params={})

    with _patch(DOWNLOAD_DIR=root, VAULT_KEY=key, VAULT_TMP=vtmp,
                _require_crypto=lambda: None, _require_unlocked=lambda: None):
        # 带 Range → 206 + 精确切片
        r = lib_router.library_encfile(lib_id, _req("bytes=100-199"))
        assert r.status_code == 206, r.status_code
        assert r.body == payload[100:200], len(r.body)
        assert r.headers["content-range"] == f"bytes 100-199/{len(payload)}"
        assert r.headers["accept-ranges"] == "bytes"
        assert r.headers["content-length"] == "100", r.headers["content-length"]
        # 响应头不得出现重复的 Content-Length（Starlette 会自动补一条）
        assert len(r.raw_headers) == len({k.lower() for k, _ in r.raw_headers}) or \
            [k for k, _ in r.raw_headers].count(b"content-length") == 1, r.raw_headers
        # 无 Range → 200 全量（小文件）
        r2 = lib_router.library_encfile(lib_id, _req(""))
        assert r2.status_code == 200 and r2.body == payload
        # 越界 Range → 416
        r3 = lib_router.library_encfile(lib_id, _req("bytes=999999-1000000"))
        assert r3.status_code == 416, r3.status_code
        assert r3.headers["content-range"] == f"bytes */{len(payload)}"

    assert not vtmp.exists() or list(vtmp.glob("*.dec")) == [], \
        "解密播放不得在磁盘上留下明文"


# ============================================================ P1-8 云账号身份

def _fake_req(token=""):
    hdrs = {"Authorization": "Bearer " + token} if token else {}
    return types.SimpleNamespace(headers=hdrs, client=types.SimpleNamespace(host="127.0.0.1"),
                                 query_params={})


def _temp_store(name, email="", token=""):
    st = ms.MembershipStore(path=Path(_TMP) / name)
    st._ensure_loaded()
    if email or token:
        st._state.setdefault("meta", {})["account"] = {"email": email, "token": token}
    return st


def test_cloud_owner_binding():
    ident, uid = _new_account("cloudowner")
    other_ident, other_uid = _new_account("cloudother")

    gstore = _temp_store("g1.json", email=ident, token="CLOUDTOK")
    with _patch(member_store=gstore):
        # 未登录 → 非归属者
        assert ca._is_cloud_owner(_fake_req()) is False
        try:
            ca._require_cloud_owner(_fake_req())
            raise AssertionError("未登录不应通过")
        except server_app.HTTPException as e:
            assert e.status_code == 403
        # 归属者 → 通过
        assert ca._is_cloud_owner(_fake_req(au.issue_token(uid))) is True
        ca._require_cloud_owner(_fake_req(au.issue_token(uid)))
        # 他人账号 → 403
        assert ca._is_cloud_owner(_fake_req(au.issue_token(other_uid))) is False
        try:
            ca._require_cloud_owner(_fake_req(au.issue_token(other_uid)))
            raise AssertionError("非归属者不应通过")
        except server_app.HTTPException as e:
            assert e.status_code == 403


def test_cloud_status_does_not_leak_to_non_owner():
    ident, uid = _new_account("statusowner")
    gstore = _temp_store("g2.json", email=ident, token="CLOUDTOK")
    gstore._state["meta"]["account"]["devices"] = [{"fp": "deadbeef", "name": "secret-mac"}]
    with _patch(member_store=gstore):
        out = ca.cloud_status(_fake_req())          # 匿名
        assert out["account"]["email"] == "", out
        assert out["account"]["devices"] == [], out


def test_cloud_token_fallback_requires_same_owner():
    """回落全局 token 前必须校验归属（否则越权借用他人账号身份）。"""
    ident, uid = _new_account("tokenowner")
    gstore = _temp_store("g3.json", email=ident, token="CLOUDTOK")

    mine = _temp_store("u1.json")
    mine._owner_email = ident
    stranger = _temp_store("u2.json")
    stranger._owner_email = "someoneelse@example.com"
    unknown = _temp_store("u3.json")           # 归属未知

    with _patch(member_store=gstore):
        assert mine._cloud_token() == "CLOUDTOK"
        assert stranger._cloud_token() == "", "非归属者借用了全局云 token"
        assert unknown._cloud_token() == "", "归属未知时不应回落"
    # 全局 store 自己仍能直接取到
    assert gstore._cloud_token() == "CLOUDTOK"


if __name__ == "__main__":  # pragma: no cover
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception:
            failed += 1
            print("FAIL", fn.__name__)
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
