"""百度网盘接入单元测试（新接口四步，离线 mock）。

不依赖真实网络/真实账号：用 FakeSession 替换 requests.Session，验证
- URL 分类（短链 / share/init / 未知 / 提取码）
- 分享页 yunData 提取（对象字面量 + 点赋值两种页面形态）
- 新四步串联：verify → tplconfig → list → sharedownload，产出 dlink
- **sharedownload 必须 form 编码**（改回 json= 会让百度返回 errno=2 —— 回归防护）
- 无登录态 → need_login 标记；有登录态 → cookie 随文件下发
- 错误语义：提取码错（errno=-12）/ 空分享 / 只有文件夹 / 个人路径未支持
- 路由层：登录态取用顺序（缓存 → 手动 BDUSS）、need_credential 三态
"""
from __future__ import annotations

import json
import sys
import urllib.parse
from pathlib import Path
from unittest import mock

import pytest

# 让 server/ 在 import 路径中（与现有 test_*.py 约定一致：以 server 为 cwd 运行）
SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from cloud_drives import baidu  # noqa: E402


# ——————————————————————————————————————————————————————————————
# 固定样本
# ——————————————————————————————————————————————————————————————

SURL = "PsAzAEtERXiTYumhEUG0Bw"          # 去前导 1
RAW = "1" + SURL
SHARE_URL = f"https://pan.baidu.com/s/{RAW}"
PWD = "c4c0"
UK = "1100294005954"
SHARE_ID = "14483302138"
FS_ID = "1117595361416981"
SIGN = "0991edd7c421232aaf9e2cdaf60547e2a223e854"
TS = "1792762672"
SEKEY = "IqeYR1pPIMpILYG1OqMtSe8Aaxskii29IfBro7i2qsc="
DLINK = ("https://d.pcs.baidu.com/file/0edddf313med10c49a01286c8cb08b1c"
         f"?fid={UK}-250528-{FS_ID}&dstime=1792762690")

RECORDS = [{"fs_id": FS_ID, "server_filename": "001.17岁-刘德华.mp3",
            "size": "9610033", "isdir": "0"}]

# 页面形态 1：对象字面量（当前真实页面；注意页面里另有个占位的 uk:'0'）
PAGE_LITERAL = (
    '<html><script>window.yunData = {"share_uk":"%s","shareid":"%s",'
    '"uk":"0","sign":"","timestamp":""};</script></html>' % (UK, SHARE_ID)
)
# 页面形态 2：点赋值（老页面 / 历史 mock）
PAGE_DOT = (
    "<html><script>"
    "window.yunData.sign = '%s';"
    "window.yunData.shareid = '%s';"
    "window.yunData.uk = '%s';"
    "window.yunData.timestamp = '%s';"
    "</script></html>" % (SIGN, SHARE_ID, UK, TS)
)


class _Resp:
    def __init__(self, payload=None, text=""):
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class _FakeSession:
    """替身 requests.Session：记录调用，按 URL 回放固定响应。"""

    def __init__(self, records=None, page=PAGE_LITERAL,
                 list_errno=0, verify_errno=0, dl_errno=0, dl_list=None):
        self.headers = {}
        self.cookies = {}
        self.calls: list[tuple] = []
        self._records = RECORDS if records is None else records
        self._page = page
        self._list_errno = list_errno
        self._verify_errno = verify_errno
        self._dl_errno = dl_errno
        self._dl_list = dl_list

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self._dispatch("GET", url, kw)

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self._dispatch("POST", url, kw)

    def _dispatch(self, method, url, kw):
        if "/share/verify" in url:
            if self._verify_errno != 0:
                return _Resp({"errno": self._verify_errno})
            self.cookies["BDCLND"] = urllib.parse.quote(SEKEY)
            return _Resp({"errno": 0, "randsk": urllib.parse.quote(SEKEY)})
        if "/share/tplconfig" in url:
            return _Resp({"errno": 0, "data": {"sign": SIGN, "timestamp": TS}})
        if "/share/list" in url:
            if self._list_errno != 0:
                return _Resp({"errno": self._list_errno})
            return _Resp({"errno": 0, "list": self._records,
                          "uk": UK, "share_id": SHARE_ID})
        if "/api/sharedownload" in url:
            if self._dl_errno != 0:
                return _Resp({"errno": self._dl_errno})
            if self._dl_list is not None:
                return _Resp({"errno": 0, "list": self._dl_list})
            fids = json.loads((kw.get("data") or {}).get("fid_list") or "[]")
            return _Resp({"errno": 0,
                          "list": [{"fs_id": f, "dlink": DLINK} for f in fids]})
        if url.startswith("https://pan.baidu.com/s/"):
            return _Resp({}, self._page)
        return _Resp({})


def _patch_session(**kw):
    """patch baidu.requests.Session，返回 (patcher, 实际被用到的 session)。"""
    box = {}

    def _factory():
        s = _FakeSession(**kw)
        box["s"] = s
        return s

    p = mock.patch.object(baidu.requests, "Session", side_effect=_factory)
    return p, box


# ——————————————————————————————————————————————————————————————
# URL 分类
# ——————————————————————————————————————————————————————————————

def test_parse_share_short():
    r = baidu.parse_baidu_url(f"https://pan.baidu.com/s/{RAW}")
    assert r["kind"] == "share"
    assert r["surl"] == SURL          # 去前导 1（verify/list 用）
    assert r["raw"] == RAW            # 带前导 1（tplconfig 用）


def test_parse_share_init_with_pwd():
    r = baidu.parse_baidu_url(f"https://pan.baidu.com/share/init?surl={SURL}&pwd={PWD}")
    assert r["kind"] == "share"
    assert r["surl"] == SURL
    assert r["pwd"] == PWD


def test_parse_personal_path():
    r = baidu.parse_baidu_url("https://pan.baidu.com/disk/main#/all?path=%2Fdocs%2Fdemo.mp4")
    assert r["kind"] == "personal"
    assert r["path"] == "/docs/demo.mp4"


def test_parse_unknown():
    assert baidu.parse_baidu_url("https://example.com/foo").get("kind") == "unknown"


# ——————————————————————————————————————————————————————————————
# 分享页 yunData 提取（两种页面形态）
# ——————————————————————————————————————————————————————————————

def test_yun_data_literal_form():
    d = baidu._extract_yun_data(PAGE_LITERAL)
    assert str(d.get("uk")) == UK          # 取 share_uk，忽略占位 uk:'0'
    assert str(d.get("shareid")) == SHARE_ID


def test_yun_data_dot_assign_form():
    d = baidu._extract_yun_data(PAGE_DOT)
    assert str(d.get("uk")) == UK
    assert str(d.get("shareid")) == SHARE_ID
    assert str(d.get("timestamp")) == TS


# ——————————————————————————————————————————————————————————————
# 四步主流程
# ——————————————————————————————————————————————————————————————

def test_resolve_share_full_flow():
    p, box = _patch_session()
    with p:
        files = baidu.resolve_share(SHARE_URL, login_cookie="BDUSS=abc", pwd=PWD)
    s = box["s"]
    assert len(files) == 1
    f = files[0]
    assert f.name == "001.17岁-刘德华.mp3"
    assert f.size == 9610033
    assert f.dlink == DLINK
    assert f.cookie == "BDUSS=abc"
    assert f.need_login is False
    # 四步都走到了，且顺序正确
    steps = [u for _, u, _ in s.calls]
    assert any("/share/verify" in u for u in steps)
    assert any("/share/tplconfig" in u for u in steps)
    assert any("/share/list" in u for u in steps)
    assert any("/api/sharedownload" in u for u in steps)


def test_sharedownload_uses_form_encoding():
    """回归防护：sharedownload 必须 form 编码（data=），用 json= 会被百度判 errno=2。"""
    p, box = _patch_session()
    with p:
        baidu.resolve_share(SHARE_URL, login_cookie="BDUSS=abc", pwd=PWD)
    s = box["s"]
    dl_calls = [kw for m, u, kw in s.calls if "/api/sharedownload" in u]
    assert dl_calls, "sharedownload 未被调用"
    kw = dl_calls[0]
    assert "data" in kw and "json" not in kw
    body = kw["data"]
    assert body["product"] == "share"
    assert body["uk"] == UK
    assert body["primaryid"] == SHARE_ID
    assert json.loads(body["fid_list"]) == [int(FS_ID)]
    assert json.loads(body["extra"])["sekey"] == SEKEY


def test_verify_seeds_sekey_into_session():
    """verify 的 randsk 会被 URL-encode；代码要 unquote 后再当 sekey 用。"""
    p, box = _patch_session()
    with p:
        baidu.resolve_share(SHARE_URL, login_cookie="BDUSS=abc", pwd=PWD)
    s = box["s"]
    assert s.cookies.get("BDCLND") == urllib.parse.quote(SEKEY)
    dl = [kw for m, u, kw in s.calls if "/api/sharedownload" in u][0]
    assert json.loads(dl["data"]["extra"])["sekey"] == SEKEY   # 未带 %3D 尾巴


def test_no_login_marks_need_login():
    p, _ = _patch_session()
    with p:
        files = baidu.resolve_share(SHARE_URL, login_cookie="", pwd=PWD)
    assert len(files) == 1
    assert files[0].need_login is True
    assert files[0].cookie == ""
    # 直链本身仍然拿到了（解析匿名可过，只是下载要登录态）
    assert files[0].dlink == DLINK


def test_public_share_without_pwd_skips_verify():
    p, box = _patch_session()
    with p:
        baidu.resolve_share(f"https://pan.baidu.com/s/{RAW}", login_cookie="BDUSS=a")
    s = box["s"]
    assert not any("/share/verify" in u for _, u, _ in s.calls)


# ——————————————————————————————————————————————————————————————
# 错误语义
# ——————————————————————————————————————————————————————————————

def test_wrong_pwd_raises_with_errno():
    p, _ = _patch_session(verify_errno=-12)
    with p:
        with pytest.raises(ValueError) as ei:
            baidu.resolve_share(SHARE_URL, login_cookie="BDUSS=a", pwd="0000")
    assert "-12" in str(ei.value)


def test_empty_share_raises():
    p, _ = _patch_session(records=[])
    with p:
        with pytest.raises(ValueError) as ei:
            baidu.resolve_share(SHARE_URL, login_cookie="BDUSS=a", pwd=PWD)
    assert "空" in str(ei.value) or "失效" in str(ei.value)


def test_dir_only_share_raises():
    p, _ = _patch_session(records=[{"fs_id": "1", "server_filename": "d",
                                    "size": "0", "isdir": "1"}])
    with p:
        with pytest.raises(ValueError) as ei:
            baidu.resolve_share(SHARE_URL, login_cookie="BDUSS=a", pwd=PWD)
    assert "文件夹" in str(ei.value)


def test_dlink_failure_raises():
    """取直链失败要带 errno 语义抛上来（errno=2 = 参数/编码不对）。"""
    p, _ = _patch_session(dl_errno=2)
    with p:
        with pytest.raises(ValueError) as ei:
            baidu.resolve_share(SHARE_URL, login_cookie="BDUSS=a", pwd=PWD)
    assert "errno=2" in str(ei.value)


def test_personal_path_not_supported_yet():
    with pytest.raises(ValueError) as ei:
        baidu.resolve_personal("/docs/demo.mp4", "BDUSS=a")
    assert "阶段 2" in str(ei.value)


def test_resolve_dispatch_and_unknown():
    p, _ = _patch_session()
    with p:
        files = baidu.resolve(SHARE_URL, "BDUSS=abc", PWD)
    assert len(files) == 1
    with pytest.raises(ValueError):
        baidu.resolve("https://example.com/x", "", "")


def test_fallback_to_page_when_list_lacks_ids():
    """list 没给 uk/share_id 时，要能从分享页 yunData 兜底取到。"""
    class _NoIdsSession(_FakeSession):
        def _dispatch(self, method, url, kw):
            if "/share/list" in url:
                return _Resp({"errno": 0, "list": RECORDS})
            return super()._dispatch(method, url, kw)

    box = {}

    def factory():
        s = _NoIdsSession(page=PAGE_LITERAL)
        box["s"] = s
        return s

    with mock.patch.object(baidu.requests, "Session", side_effect=factory):
        files = baidu.resolve_share(SHARE_URL, login_cookie="BDUSS=a", pwd=PWD)
    assert len(files) == 1
    dl = [kw for m, u, kw in box["s"].calls if "/api/sharedownload" in u][0]["data"]
    assert dl["uk"] == UK and dl["primaryid"] == SHARE_ID


# ——————————————————————————————————————————————————————————————
# 路由层
# ——————————————————————————————————————————————————————————————

def _router():
    import app as _app  # noqa: F401  （确保路由已挂载到 app）
    from routers import cloud_drive as rt
    return rt


def test_router_resolve_returns_files():
    rt = _router()
    fake = [baidu.CloudFile(name="x.mp4", size=10, dlink=DLINK,
                            cookie="BDUSS=z", headers={"User-Agent": "ua"})]
    with mock.patch.object(rt, "_baidu_login_cookie", return_value="BDUSS=z"), \
         mock.patch.object(rt.baidu, "resolve", return_value=fake):
        out = rt.cloud_drive_resolve(rt.CloudDriveResolveReq(url=SHARE_URL))
    assert out["ok"] is True
    assert out["platform"] == "baidupan"
    assert out["files"][0]["name"] == "x.mp4"
    assert out["need_credential"] is False


def test_router_resolve_third_state_parsed_but_needs_login():
    """解析成功但没登录态 ⇒ ok=True 且 need_credential=True（下载会 403）。"""
    rt = _router()
    fake = [baidu.CloudFile(name="x.mp4", size=10, dlink=DLINK, need_login=True)]
    with mock.patch.object(rt, "_baidu_login_cookie", return_value=""), \
         mock.patch.object(rt.baidu, "resolve", return_value=fake):
        out = rt.cloud_drive_resolve(rt.CloudDriveResolveReq(url=SHARE_URL))
    assert out["ok"] is True
    assert out["need_credential"] is True
    assert out["need_login"] is True


def test_router_resolve_error_flags_credential():
    rt = _router()
    with mock.patch.object(rt, "_baidu_login_cookie", return_value=""), \
         mock.patch.object(rt.baidu, "resolve",
                           side_effect=ValueError("不是有效的百度网盘分享链接")):
        out = rt.cloud_drive_resolve(rt.CloudDriveResolveReq(url="https://pan.baidu.com/x"))
    assert out["ok"] is False
    assert out["need_credential"] is True


def test_login_cookie_prefers_stored_over_cache():
    """手动保存的 BDUSS 优先于本机缓存（2026-10-24 改序）。

    原因：百度限速按账号等级，用户为了跑满带宽会去网盘页填**会员账号**的 BDUSS；
    若缓存优先，会被本机 Chrome 里那个非会员账号盖掉 ⇒ 表现成「填了没反应」。
    显式指定的账号必须赢过自动抓取的缓存。
    """
    rt = _router()
    with mock.patch("cookie_cache.peek_cached_cookie_header",
                    return_value="BDUSS=from-cache"), \
         mock.patch.object(rt, "get_baidu_bduss", return_value="BDUSS=stored"):
        assert rt._baidu_login_cookie() == "BDUSS=stored"


def test_login_cookie_uses_cache_when_no_stored():
    """没手动保存时仍用本机缓存（装了扩展的用户零配置）。"""
    rt = _router()
    with mock.patch("cookie_cache.peek_cached_cookie_header",
                    return_value="BDUSS=from-cache"), \
         mock.patch.object(rt, "get_baidu_bduss", return_value=""):
        assert rt._baidu_login_cookie() == "BDUSS=from-cache"


def test_login_cookie_falls_back_to_stored():
    """缓存没有时用手动保存的 BDUSS（patch 必须打在路由模块的引用上，
    `from credential_store import get_baidu_bduss` 是值绑定，打 credential_store 无效）。"""
    rt = _router()
    with mock.patch("cookie_cache.peek_cached_cookie_header", return_value=None), \
         mock.patch.object(rt, "get_baidu_bduss", return_value="BDUSS=stored"):
        assert rt._baidu_login_cookie() == "BDUSS=stored"


def test_router_credential_accepts_bare_bduss():
    rt = _router()
    with mock.patch.object(rt, "set_baidu_bduss", return_value="keychain") as m:
        out = rt.cloud_drive_set_credential(rt.CloudDriveCredentialReq(bduss="abc123"))
    assert out["ok"] is True
    m.assert_called_once_with("abc123")


def test_router_credential_rejects_empty():
    rt = _router()
    out = rt.cloud_drive_set_credential(rt.CloudDriveCredentialReq(bduss="  "))
    assert out["ok"] is False
