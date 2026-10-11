"""P3-2 验证轮回归：订阅源 SSRF 护栏。

背景（安全审计验证轮 2026-10-11）：`/api/subscriptions` 的 `add_subscription` 在
`parse_source` 之后直接调用 `probe_channel(url, ...)`（yt-dlp extract_flat，真实发起
HTTP 请求）。而 `parse_source` 对非已知平台链接**只回退到 `_generic_platform`**，做的是
「功能白名单」而非「网络可达性白名单」——内部地址（169.254.169.254 云元数据、
127.0.0.1、私有网段）不会被它拦截。于是任意登录用户都能让本机去请求云元数据 / 内网
服务（VPS 多租户部署下尤其危险，169.254.169.254 可回 IAM 临时凭证）。

修复：在 `parse_source` 之后、调用 `probe_channel` 之前，补一道 `app._assert_safe_url(url)`，
与 /api/download、/api/resolve 口径完全一致。

设计约束：不联网（用 monkeypatch 的护栏替身模拟「放行 / 拦截」两种结果）、
不写用户家目录、退出还原所有补丁。真正的「169.254.169.254 被拒」由
test_p1_core_hardening.py::test_assert_safe_url_rejects_metadata_address 覆盖，
本文件只证明「护栏确实接在 probe_channel 之前」。
"""
import sys
import tempfile
import types
from pathlib import Path

_SERVER_DIR = str(Path(__file__).resolve().parent.parent)
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402
import subscriptions as subs_mod  # noqa: E402
from routers import subscriptions as subs_router  # noqa: E402


class _Patch:
    def __init__(self, **attrs):
        self._attrs = attrs
        self._orig = {}

    def __enter__(self):
        for k, v in self._attrs.items():
            obj, name = (v.mod, v.name) if isinstance(v, _Attr) else (server_app, k)
            val = v.value if isinstance(v, _Attr) else v
            self._orig[(obj, name)] = getattr(obj, name)
            setattr(obj, name, val)
        return self

    def __exit__(self, *exc):
        for (obj, name), val in self._orig.items():
            setattr(obj, name, val)
        return False


class _Attr:
    """显式指定补丁目标（值本身可能是 tuple / callable，不能用 (obj, val) 约定）。"""

    def __init__(self, mod, name, value):
        self.mod = mod
        self.name = name
        self.value = value


class _Req:
    def __init__(self, owner: str = "u:tester"):
        self.headers = {}
        self.query_params = {}
        self._owner = owner

    def __repr__(self):  # pragma: no cover - 调试用
        return "<Req owner=%r>" % self._owner


class _FakeDownloader:
    def is_valid_quality(self, q):
        return True

    def quality_label(self, q):
        return "1080P"


class _Payload:
    def __init__(self, url):
        self.url = url
        self.quality = "1080P"
        self.name = ""
        self.cookie = ""
        self.proxy = ""
        self.auto_check = True


def _setup(tmp, safe_url_checker, probe_recorder):
    """把 add_subscription 的依赖都换成本地替身，返回 (patch, store)。"""
    store = subs_mod.SubscriptionStore(tmp / "subs" / ".subscriptions.json")

    class _LinkError(ValueError):  # noqa: N801 - 仅占位，便于阅读
        pass

    def _assert_safe(url):
        safe_url_checker(url)  # 由用例决定抛还是放行

    patch = _Patch(
        SUB_ENABLED=True,
        downloader=_FakeDownloader(),
        sub_store=store,
        _task_owner=(lambda req: req._owner),
        _assert_safe_url=_assert_safe,
        _vault_load=(lambda: None),
        VAULT_KEY=None,
        CRYPTO_ENABLED=False,
        uuid=types.SimpleNamespace(uuid4=lambda: types.SimpleNamespace(hex="a" * 24)),
        time=types.SimpleNamespace(time=lambda: 1.0),
        probe_channel=_Attr(subs_mod, "probe_channel",
                            lambda *a, **k: probe_recorder(*a, **k)),
    )
    return patch, store


def test_subscription_blocks_internal_url_before_probe():
    """内部地址（云元数据）在 probe_channel 之前被拒，且 probe_channel 一次都不被调。"""
    tmp = Path(tempfile.mkdtemp(prefix="vdl_p3_2_blk_"))
    calls = []

    def _reject(url):
        class LinkError(ValueError):
            pass
        raise LinkError("该链接指向非公开网络，已拒绝")

    def _probe(url, cookie="", proxy="", limit=100):
        calls.append(url)
        return []

    patch, _store = _setup(tmp, _reject, _probe)
    with patch:
        raised = False
        try:
            subs_router.add_subscription(
                _Payload("http://169.254.169.254/latest/meta-data/"), _Req())
        except ValueError:
            raised = True
        assert raised, "内部地址未被拦截"
        assert calls == [], "probe_channel 不应被调用（内部地址直接拒）"


def test_subscription_allows_public_url_and_probes():
    """安全地址通过护栏后，probe_channel 被调用且拿到原 URL。"""
    tmp = Path(tempfile.mkdtemp(prefix="vdl_p3_2_ok_"))
    calls = []

    def _allow(url):
        return None  # 护栏放行

    def _probe(url, cookie="", proxy="", limit=100):
        calls.append(url)
        return [{"id": "v1", "title": "t", "url": url, "uploader": "u"}]

    patch, store = _setup(tmp, _allow, _probe)
    with patch:
        res = subs_router.add_subscription(
            _Payload("https://www.youtube.com/@some-channel"), _Req())
        assert calls == ["https://www.youtube.com/@some-channel"], \
            "probe_channel 应拿到归一化后的原 URL"
        assert res.get("id"), "应成功创建订阅"
        # 落库的 url 同样是归一化后的安全地址
        saved = store.get(res["id"])
        assert saved is not None and saved.url.startswith("https://")


if __name__ == "__main__":  # pragma: no cover
    import traceback
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
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
