"""网页版百度网盘下载（server/routers/cloud_drive.py + server/baidu_cred_store.py）离线回归。

钉住的契约（任何一条被破坏都会让功能静默失效或出安全洞）：

  1. 登录态**存服务器**（用户 2026-10-25 拍板）：保存后落盘、换个进程读还在；
  2. 落盘是**加密**的：文件里搜不到明文 BDUSS；
  3. **按用户隔离**：A 的密文 B 解不开（下载令牌也不能跨用户用）；
  4. 没有密钥 ⇒ 拒绝落盘（绝不写明文，宁可功能不可用）；
  5. resolve 响应里**没有** dlink、**没有**百度 cookie（否则等于把登录态发到浏览器）；
  6. download 端点：未登录 403；令牌无效 400；非百度直链域拒绝（防 SSRF）；
  7. 下载必须带 Range（百度裸 GET 一律 403，错误码还伪装成 31326/31360，极误导）；
  8. 百度 cookie 只在服务器侧使用（透传给上游，绝不出现在响应里）。

全程离线：app.requests 换成假实现，不打任何外部网络。
运行：cd server && python tests/test_cloud_drive_baidu_web.py
"""
import json
import os
import sys
import types
import uuid

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

# 隔离数据目录：import app 会写 .auth_secret / stats.json，绝不能落到真实用户目录
os.environ.setdefault("VDL_DATA_DIR", "/tmp/vdl_test_cloud_drive_web")
os.environ.setdefault("VDL_CLOUD_LINK", "0")
# 凭据加密密钥（测试专用）
os.environ["VDL_BAIDU_SECRET"] = "test-secret-for-cloud-drive"

import app as A  # noqa: E402
import baidu_cred_store as BCS  # noqa: E402
import routers.cloud_drive as CD  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from auth_store import create_user, issue_token  # noqa: E402


def _mk_account(tag: str):
    """造一个真实账号 —— verify_token 要求账号必须存在，否则一律 fail-closed。"""
    ident = f"{tag}-{uuid.uuid4().hex[:8]}@example.com"
    uid = create_user(ident, "Test-Passw0rd!")
    assert uid, "测试账号创建失败"
    return uid, {"Authorization": f"Bearer {issue_token(uid)}"}


_UID, _AUTH = _mk_account("cdrive")
_OTHER_UID, _OTHER_AUTH = _mk_account("other")

_CALLS: list[dict] = []


class _FakeResp:
    def __init__(self, *, status=200, headers=None, chunks=()):
        self.status_code = status
        self.headers = dict(headers or {})
        self._chunks = list(chunks)
        self.closed = False

    def iter_content(self, chunk_size=0):
        for c in self._chunks:
            if c:
                yield c

    def close(self):
        self.closed = True


def _fake_get(url, headers=None, stream=False, timeout=None, proxies=None):
    _CALLS.append({"url": url, "headers": dict(headers or {}), "proxies": proxies})
    return _FakeResp(
        status=206,
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Length": "5",
            "Content-Range": "bytes 0-4/100",
        },
        chunks=[b"hello"],
    )


def _client() -> TestClient:
    return TestClient(A.app)


def _cred_file() -> str:
    return str(BCS._path())


# ───────────────────────── 1~4. 凭据存储 ─────────────────────────

def test_cred_roundtrip_and_encrypted_on_disk():
    BCS.clear_bduss(_UID)
    BCS.save_bduss(_UID, "BDUSS=super-secret-value")
    assert BCS.load_bduss(_UID) == "BDUSS=super-secret-value", "保存后应能读回"

    raw = open(_cred_file(), "r", encoding="utf-8").read()
    assert "super-secret-value" not in raw, "落盘文件里不能出现明文 BDUSS"
    assert "BDUSS=" not in raw, "落盘文件里不能出现明文 Cookie 头"

    # 裸值也接受（用户常只粘 BDUSS 那一段）
    BCS.save_bduss(_UID, "bare-token-123")
    assert BCS.load_bduss(_UID) == "BDUSS=bare-token-123", "裸 BDUSS 应自动补前缀"
    print("✅ 凭据可存取，且落盘是加密的（文件里搜不到明文）")


def test_cred_isolated_per_user():
    BCS.save_bduss(_UID, "BDUSS=uid-a-value")
    BCS.save_bduss(_OTHER_UID, "BDUSS=uid-b-value")
    assert BCS.load_bduss(_UID) == "BDUSS=uid-a-value"
    assert BCS.load_bduss(_OTHER_UID) == "BDUSS=uid-b-value"

    # A 的令牌不能被 B 用
    token_a = BCS.seal_token(_UID, "https://d.pcs.baidu.com/file?sign=x")
    assert BCS.open_token(_UID, token_a), "本人应能解开自己的令牌"
    assert BCS.open_token(_OTHER_UID, token_a) == "", "别人的令牌不能跨用户解开"

    # A 的密文也不能当 B 的用（存盘层就隔离）
    assert BCS.open_token(_UID, BCS.seal_token(_OTHER_UID, "https://x")) == ""
    print("✅ 凭据与令牌按用户隔离，密文不能跨用户搬运")


def test_cred_refuses_without_secret():
    os.environ.pop("VDL_BAIDU_SECRET", None)
    os.environ.pop("VDL_LICENSE_SECRET", None)
    try:
        BCS.save_bduss(_UID, "BDUSS=should-not-be-written")
        raise AssertionError("没有密钥时必须拒绝落盘")
    except BCS.CredentialError:
        pass
    finally:
        os.environ["VDL_BAIDU_SECRET"] = "test-secret-for-cloud-drive"
    assert BCS.load_bduss(_UID) != "BDUSS=should-not-be-written", "拒绝时不能落盘"
    print("✅ 没有密钥 ⇒ 拒绝落盘（绝不写明文）")


def test_cred_clear():
    BCS.save_bduss(_UID, "BDUSS=to-be-cleared")
    assert BCS.has_bduss(_UID)
    assert BCS.clear_bduss(_UID) is True
    assert BCS.load_bduss(_UID) == ""
    print("✅ 清除登录态生效")


# ───────────────────────── 5~8. 路由 ─────────────────────────

def test_resolve_requires_login():
    c = _client()
    r = c.post("/api/cloud-drive/resolve", json={"url": "https://pan.baidu.com/s/abc"})
    assert r.status_code == 403, f"未登录应 403，实际 {r.status_code}"
    assert "登录" in (r.json().get("detail") or ""), "403 文案要含「登录」，前端才会弹登录框"
    print("✅ 未登录访问被拦（403 + 含「登录」文案）")


def test_resolve_never_leaks_dlink_or_cookie():
    BCS.save_bduss(_UID, "BDUSS=secret-in-server")
    orig = CD.baidu.resolve

    def _fake_resolve(url, cookie="", pwd="", extra_cookies=None):
        assert cookie == "BDUSS=secret-in-server", "应带上服务器上存的登录态"
        return [CD.baidu.CloudFile(name="素材包.zip", size=1024,
                                   dlink="https://d.pcs.baidu.com/file?sign=SECRET_DLINK",
                                   cookie="BDUSS=secret-in-server", need_login=False)]

    CD.baidu.resolve = _fake_resolve
    try:
        c = _client()
        r = c.post("/api/cloud-drive/resolve",
                   json={"url": "https://pan.baidu.com/s/abc", "pwd": "1234"},
                   headers=_AUTH)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("ok") is True
        raw = r.text
        assert "SECRET_DLINK" not in raw, "响应里不能出现百度直链"
        assert "secret-in-server" not in raw, "响应里不能出现百度 cookie"
        f0 = data["files"][0]
        assert f0["name"] == "素材包.zip" and f0["size"] == 1024
        assert f0.get("id"), "应给前端一个不透明令牌 id"
    finally:
        CD.baidu.resolve = orig
    print("✅ resolve 只回令牌/文件名/大小，直链与 cookie 都不外泄")


def test_download_rejects_bad_token_and_non_baidu_host():
    c = _client()
    # 未登录
    r = c.get("/api/cloud-drive/download?id=whatever")
    assert r.status_code == 403, f"未登录应 403，实际 {r.status_code}"
    # 令牌无效
    r = c.get("/api/cloud-drive/download?id=not-a-real-token", headers=_AUTH)
    assert r.status_code == 400, f"无效令牌应 400，实际 {r.status_code}"
    # 令牌有效但指向非百度域 ⇒ 必须拒（防 SSRF）
    evil = BCS.seal_token(_UID, "https://evil.example.com/secret.bin")
    r = c.get(f"/api/cloud-drive/download?id={evil}", headers=_AUTH)
    assert r.status_code == 400, f"非百度直链应 400，实际 {r.status_code}"
    assert "百度" in (r.json().get("detail") or "")
    print("✅ download 的令牌/域名护栏生效（未登录 403、坏令牌 400、非百度域 400）")


def test_download_sends_range_and_cookie_server_side():
    BCS.save_bduss(_UID, "BDUSS=secret-in-server")
    orig_req = A.requests
    _CALLS.clear()
    A.requests = types.SimpleNamespace(get=_fake_get)
    try:
        dlink = "https://d.pcs.baidu.com/file?sign=abc"
        token = BCS.seal_token(_UID, dlink)
        c = _client()
        r = c.get(f"/api/cloud-drive/download?id={token}&name=%E7%B4%A0%E6%9D%90.zip",
                  headers=_AUTH)
        # 带 Range 的请求，源站正确响应是 206 Partial Content（也容忍 200）
        assert r.status_code in (200, 206), f"下载应 200/206，实际 {r.status_code}：{r.text[:200]}"
        assert r.content == b"hello", "字节流应原样透传"
        assert _CALLS, "应有上游请求"
        call = _CALLS[0]
        assert call["url"] == dlink
        assert call["headers"].get("Range") == "bytes=0-", \
            f"百度裸 GET 一律 403，必须带 Range；实际 {call['headers'].get('Range')!r}"
        assert "secret-in-server" in (call["headers"].get("Cookie") or ""), \
            "服务器必须把百度登录态带给上游"
        # 响应里绝不能出现 cookie
        assert "secret-in-server" not in r.text
        assert "attachment" in (r.headers.get("content-disposition") or "")
    finally:
        A.requests = orig_req
    print("✅ 下载带 Range + 登录态，字节原样透传，cookie 不出现在响应里")


def test_media_proxy_force_range_wiring():
    """force_range 必须真的落到上游请求头（百度场景的关键前置）。"""
    src = open(os.path.join(_SERVER_DIR, "routers", "core.py"), encoding="utf-8").read()
    assert "force_range: str=''" in src, "media_proxy 需支持 force_range 参数"
    assert "headers['Range'] = force_range" in src, "force_range 必须写进请求头"
    assert "(_client_range or force_range)" in src, "206 时 Content-Range 也要回传"
    print("✅ media_proxy 的 force_range 接线正确")


if __name__ == "__main__":
    for fn in (
        test_cred_roundtrip_and_encrypted_on_disk,
        test_cred_isolated_per_user,
        test_cred_refuses_without_secret,
        test_cred_clear,
        test_resolve_requires_login,
        test_resolve_never_leaks_dlink_or_cookie,
        test_download_rejects_bad_token_and_non_baidu_host,
        test_download_sends_range_and_cookie_server_side,
        test_media_proxy_force_range_wiring,
    ):
        fn()
    print("\n🎉 网页版百度网盘离线测试全部通过")
