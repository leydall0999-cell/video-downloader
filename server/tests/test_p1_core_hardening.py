"""P1 回归测试（core.py 四项加固）—— 安全审计 2026-10-11。

覆盖缺陷：
  · P1-6 admin 空密钥放行：`_require_admin` 原先写 `key == _ops_admin_key()`，
    服务端**未配置密钥**时 `_ops_admin_key()` 返回 "" ⇒ 远端不带 X-Admin-Key
    （或带空值）即通过，运维端点等于完全无鉴权。同时 `_is_local_request` 把
    `host == ""`（拿不到来源）也算本机。
  · P1-3 stream/proxy SSRF + Cookie 外泄：① 回包 `Access-Control-Allow-Origin: *`
    让任意网页能 `fetch()` 读走「用用户浏览器登录态取回的私密内容」；
    ② `_assert_safe_url` 只验首跳，`requests` 自动跟随重定向 ⇒ 公网地址 302 到
    169.254.169.254 即可绕过；③ 任意 URL 都会自动注入用户真实浏览器 Cookie。
  · P1-4 download/batch 缺 SSRF 护栏：`parse_source` 只做平台目录匹配、不校验目标
    地址，`/api/download`、`/api/batch` 可把内网/云元数据地址送进下载队列。
  · P1-5 retry/resume 绕过配额：两者此前既不过配额门也不计费 ⇒「取消→重试」
    与「历史条目重新下载」可无限刷。

设计约束：不联网（外部请求一律 monkeypatch）；不写用户家目录。
"""
import os
import sys
import tempfile
import types
from pathlib import Path

_SERVER_DIR = str(Path(__file__).resolve().parent.parent)
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402
from routers import core as C  # noqa: E402


# ---------------------------------------------------------------- 辅助

class _patch:
    """临时替换一串属性，退出时还原。"""

    def __init__(self, **attrs):
        self._attrs = attrs
        self._orig = {}

    def __enter__(self):
        for k, v in self._attrs.items():
            obj, name = (v[0], k) if isinstance(v, tuple) else (C, k)
            self._orig[k] = (obj, name, getattr(obj, name))
            setattr(obj, name, v[1] if isinstance(v, tuple) else v)
        return self

    def __exit__(self, *exc):
        for _k, (obj, name, old) in self._orig.items():
            setattr(obj, name, old)
        return False


def _req(host="127.0.0.1", headers=None):
    return types.SimpleNamespace(
        client=types.SimpleNamespace(host=host),
        headers=dict(headers or {}),
        query_params={},
        url=types.SimpleNamespace(hostname=host),
        base_url="http://127.0.0.1:8321/",
    )


class _FakeResp:
    def __init__(self, status_code=200, headers=None, body=b"#EXTM3U\n#EXT-X-VERSION:3\nseg.ts\n"):
        self.status_code = status_code
        self.headers = dict(headers if headers is not None
                            else {"Content-Type": "application/vnd.apple.mpegurl"})
        self.content = body
        self.closed = False

    def iter_content(self, chunk_size=65536):
        yield self.content

    def close(self):
        self.closed = True


def _is_http_error(exc, code=None) -> bool:
    return getattr(exc, "status_code", None) == code if code else hasattr(exc, "status_code")


# ---------------------------------------------------------------- P1-6 admin

def test_require_admin_rejects_when_key_not_configured():
    """未配置运维密钥时必须 fail-closed（此前 "" == "" 直接放行）。"""
    with _patch(_ops_admin_key=lambda: ""):
        try:
            C._require_admin(_req(host="203.0.113.7"))
            raise AssertionError("未配置密钥时远端请求应被拒绝")
        except server_app.HTTPException as e:
            assert e.status_code == 503, e.status_code


def test_require_admin_accepts_correct_key_only():
    with _patch(_ops_admin_key=lambda: "s3cret"):
        # 正确密钥 → 放行
        C._require_admin(_req(host="203.0.113.7", headers={"X-Admin-Key": "s3cret"}))
        # 空值 / 错误值 → 403
        for bad in ("", "wrong", "s3cre"):
            try:
                C._require_admin(_req(host="203.0.113.7", headers={"X-Admin-Key": bad}))
                raise AssertionError(f"密钥 {bad!r} 不应放行")
            except server_app.HTTPException as e:
                assert e.status_code == 403, (bad, e.status_code)


def test_require_admin_local_still_allowed():
    with _patch(_ops_admin_key=lambda: ""):
        C._require_admin(_req(host="127.0.0.1"))          # 本机免密钥（桌面 WebView）


def test_empty_host_is_not_treated_as_local():
    """拿不到来源地址不得当成「可信本机」。"""
    assert C._is_local_request(_req(host="")) is False
    assert C._is_local_request(_req(host="127.0.0.1")) is True
    assert C._is_local_request(_req(host="::1")) is True
    assert C._is_local_request(_req(host="203.0.113.7")) is False


# ---------------------------------------------------------------- P1-3 stream

def test_stream_proxy_has_no_wildcard_cors():
    """回包不得再带 Access-Control-Allow-Origin:*（跨源读走带登录态内容的开关）。

    ⚠️ 本用例只验**回包头**，与目标 URL 是否可达无关；`_assert_safe_url` 会真的做
    DNS 解析（2026-10-12 起解析失败还会 fail-closed 抛 LinkError），离线/无 DNS 的
    沙盒里会误红。故这里把它打桩为放行 —— SSRF 护栏本身由
    `test_assert_safe_url_rejects_metadata_address` / `test_create_*_applies_ssrf_guard`
    等用例覆盖，不靠本用例。
    """
    fake = _FakeResp()
    with _patch(_stream_get=lambda *a, **k: fake,
                _assert_safe_url=(server_app, lambda u: None)):
        r = C.stream_proxy(u="https://www.bilibili.com/x.m3u8", cookie="x=1",
                           request=_req(host="127.0.0.1"))
    hdrs = {k.lower() for k in r.headers.keys()}
    assert "access-control-allow-origin" not in hdrs, dict(r.headers)


def test_stream_get_blocks_redirect_to_internal_address():
    """首跳是公网、302 到内网/云元数据地址 —— 必须被拦（旧实现自动跟随即绕过）。"""
    calls = []

    def _fake_get(url, **kw):
        calls.append(url)
        return _FakeResp(status_code=302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})

    with _patch(requests=(server_app, types.SimpleNamespace(get=_fake_get))):
        try:
            C._stream_get("http://93.184.216.34/a.m3u8", {}, None, (1, 2))
            raise AssertionError("重定向到链路本地地址应被拒绝")
        except server_app.LinkError:
            pass
    assert calls == ["http://93.184.216.34/a.m3u8"], calls


def test_stream_get_strips_cookie_on_cross_host_redirect():
    """跨 host 跳转必须丢弃 Cookie（不把 A 站登录态带给 B 站）。"""
    seen = []

    def _fake_get(url, **kw):
        seen.append((url, dict(kw.get("headers") or {})))
        if len(seen) == 1:
            # 跳到**另一个 host**（同 host 跳转保留 Cookie 是合理的，跨 host 才必须丢）
            return _FakeResp(status_code=302, headers={"Location": "https://1.1.1.1/next.ts"})
        return _FakeResp(status_code=200, headers={"Content-Type": "video/mp2t"}, body=b"ts")

    with _patch(requests=(server_app, types.SimpleNamespace(get=_fake_get))):
        resp = C._stream_get("https://93.184.216.34/a.ts",
                             {"Cookie": "sid=secret", "Referer": "https://a.com/"}, None, (1, 2))
    assert resp.status_code == 200
    assert len(seen) == 2, seen
    assert seen[0][1].get("Cookie") == "sid=secret"
    assert "Cookie" not in seen[1][1], seen[1][1]
    assert seen[1][1].get("Referer") == "https://a.com/"      # Referer 保留（防盗链需要）


def test_auto_cookie_only_for_own_pages():
    """外部页面的 Referer 不得触发「自动注入用户浏览器登录态」。"""
    # 自己的页面
    assert C._caller_is_own_page(_req(host="127.0.0.1",
                                      headers={"referer": "http://127.0.0.1:8321/index.html"})) is True
    # 外部页面
    assert C._caller_is_own_page(_req(host="127.0.0.1",
                                      headers={"referer": "https://evil.example/x.html"})) is False
    assert C._caller_is_own_page(_req(host="127.0.0.1",
                                      headers={"origin": "https://evil.example"})) is False
    # 无 Referer/Origin（原生播放器直连）→ 允许
    assert C._caller_is_own_page(_req(host="127.0.0.1")) is True


# ---------------------------------------------------------------- P1-4 SSRF 护栏

def test_assert_safe_url_rejects_metadata_address():
    try:
        server_app._assert_safe_url("http://169.254.169.254/latest/meta-data/")
        raise AssertionError("链路本地地址应被拒绝")
    except server_app.LinkError:
        pass


def test_create_download_applies_ssrf_guard():
    """下载入口必须对最终 URL 过 _assert_safe_url。"""
    payload = types.SimpleNamespace(
        url="http://169.254.169.254/latest/meta-data/", quality="best", cookie="",
        proxy="", title="", extract_script="", concurrent_fragments=0, downloader="",
        play_url="", watch_options=[], is_hls=False, referer="", format_id="",
    )
    with _patch(_check_rate_limit=(server_app, lambda req: None),
                _download_gate_error=(C, lambda req: None),
                parse_source=(server_app, lambda u: (u, types.SimpleNamespace(name="generic"))),
                _assert_safe_url=(server_app, lambda u: (_ for _ in ()).throw(
                    server_app.LinkError("该主机不在允许范围内", "请粘贴公开可访问的视频链接")))):
        try:
            C.create_download(payload, _req())
            raise AssertionError("内网地址不应进入下载队列")
        except server_app.LinkError:
            pass


def test_create_batch_applies_ssrf_guard():
    """批量入口同样要过护栏，且不安全地址计入 skipped 而非中断整批。"""
    payload = types.SimpleNamespace(urls=["http://169.254.169.254/a"],
                                    quality="best", cookie="", proxy="",
                                    concurrency=0, retries=-1, extract_script="")
    with _patch(_check_rate_limit=(server_app, lambda req: None),
                _download_gate_error=(C, lambda req: None),
                _quality_gate_error=(C, lambda req, q: None),
                parse_source=(server_app, lambda u: (u, types.SimpleNamespace(name="generic"))),
                _assert_safe_url=(server_app, lambda u: (_ for _ in ()).throw(
                    server_app.LinkError("该主机不在允许范围内", "请粘贴公开可访问的视频链接")))):
        try:
            C.create_batch(payload, _req())
            raise AssertionError("整批都不安全时应报错")
        except server_app.HTTPException as e:
            assert e.status_code == 400, e.status_code


# ---------------------------------------------------------------- P1-5 配额

class _FakeTask:
    def __init__(self):
        self.id = "t1"
        self.status = "failed"
        self.workdir = Path(tempfile.mkdtemp(prefix="vdl_p1_"))
        self.quality_key = "best"
        self.cookie = ""
        self.proxy = ""
        self.concurrent_fragments = 0
        self.downloader_type = ""
        self.progress = 0.0
        self.downloaded_bytes = 0
        self.total_bytes = 0
        self.cancel_requested = False
        self.cancel_reason = ""
        self.file_expired = False
        self.filepath = None
        # 2026-10-19：重试计费带标题与平台（后台「使用详情」定位用）
        self.title = ""
        self.url = "https://example.com/v/1"
        self.platform = "example.com"


class _FakeStore:
    def __init__(self):
        self.updates = []

    def update(self, task_id, **kw):
        self.updates.append((task_id, kw))


class _FakeMembership:
    def __init__(self):
        self.calls = 0

    def use_daily(self, resource, n, title="", platform=""):
        self.calls += 1
        return {"used": self.calls}


def test_retry_blocked_when_quota_exhausted():
    task = _FakeTask()
    with _patch(_require_owned_task=(server_app, lambda tid, req: task),
                _download_gate_error=(C, lambda req: "今日免费下载次数已用尽")):
        try:
            C.retry_task("t1", _req())
            raise AssertionError("配额用尽时重试应被拒绝")
        except server_app.HTTPException as e:
            assert e.status_code == 402, e.status_code
            assert "MEMBER_QUOTA" in str(e.detail), e.detail


def test_resume_blocked_when_quota_exhausted():
    task = _FakeTask()
    task.status = "paused"
    with _patch(_require_owned_task=(server_app, lambda tid, req: task),
                _download_gate_error=(C, lambda req: "今日免费下载次数已用尽")):
        try:
            C.resume_task("t1", _req())
            raise AssertionError("配额用尽时恢复应被拒绝")
        except server_app.HTTPException as e:
            assert e.status_code == 402, e.status_code


def test_retry_charges_only_when_new_transfer():
    """无 .part 的重试 = 新传输 → 计 1 次；有 .part 的续传 → 不计费。"""
    task = _FakeTask()
    store = _FakeStore()
    member = _FakeMembership()
    submitted = []

    def _run(*a, **k):
        submitted.append(a)

    with _patch(_require_owned_task=(server_app, lambda tid, req: task),
                _download_gate_error=(C, lambda req: None),
                store=(server_app, store),
                scheduler=(server_app, types.SimpleNamespace(submit=_run)),
                current_member_store=(server_app, lambda req: member),
                _has_partial=(server_app.downloader, lambda wd: False)):
        C.retry_task("t1", _req())
    assert member.calls == 1, f"无 .part 的重试应计费 1 次，实际 {member.calls}"

    task2 = _FakeTask()
    member2 = _FakeMembership()
    with _patch(_require_owned_task=(server_app, lambda tid, req: task2),
                _download_gate_error=(C, lambda req: None),
                store=(server_app, _FakeStore()),
                scheduler=(server_app, types.SimpleNamespace(submit=_run)),
                current_member_store=(server_app, lambda req: member2),
                _has_partial=(server_app.downloader, lambda wd: True)):
        r = C.retry_task("t1", _req())
    assert r.get("resume") is True, r
    assert member2.calls == 0, f"续传不应重复计费，实际 {member2.calls}"


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
