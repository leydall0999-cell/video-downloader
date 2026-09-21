"""douyin_direct 离线测试：SM3 向量 + a_bogus 格式 + 解析逻辑（mock 网络）。

真实解析依赖抖音线上接口，属出网链路，不进离线套件（沙盒会拦出网）；
签名算法正确性由 SM3 国标向量与格式断言兜底，线上 E2E 见部署记录。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vps_worker"))
import douyin_direct  # noqa: E402


def test_sm3_standard_vectors():
    # GB/T 32905-2016 附录 A 官方向量（与 Linux 内核 testvec / OpenSSL 交叉核对）
    assert douyin_direct._SM3.hash_bytes(b"abc").hex() == (
        "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0")
    assert douyin_direct._SM3.hash_bytes(b"").hex() == (
        "1ab21d8355cfa17f8e61194831e81a8f22bec8c728fefb747ed035eb5082aa2b")
    # 64 字节整块（触发多块路径）
    v64 = douyin_direct._SM3.hash_bytes(b"abcd" * 16).hex()
    assert v64 == ("debe9ff92275b8a138604889c18e5a4d"
                   "6fdb70e5387e5765293dcba39c0c5732")


def test_abogus_format_and_determinism_shape():
    sig = douyin_direct.ABogus(douyin_direct.UA).generate(
        "device_platform=webapp&aid=6383&aweme_id=7380308675841297704")
    # 自定义 base64 表字符集 + 填充
    import re
    assert re.fullmatch(r"[Dkdpgh2ZmsQB80/MfvV36XI1R45\-WUAlEixNLwoqYTOPuzKFjJnry79HbGcaStCe=]{80,400}", sig)
    # 时间戳参与签名 → 两次生成必然不同（随机性来源存在）
    sig2 = douyin_direct.ABogus(douyin_direct.UA).generate("aid=6383")
    assert sig != sig2


def test_normalize_url_variants():
    n = douyin_direct.normalize_url
    assert n("https://www.douyin.com/video/7380308675841297704") == "7380308675841297704"
    assert n("https://www.douyin.com/note/73803086758412977041") == "73803086758412977041"
    assert n("https://www.iesdouyin.com/xg/video/7380308675841297704") == "7380308675841297704"
    assert n("https://www.ixigua.com/7380308675841297704") == "7380308675841297704"
    assert n("https://www.douyin.com/?modal_id=7380308675841297704") == "7380308675841297704"
    # 短链需出网展开 → 离线环境返回空串（不抛错）
    assert n("https://v.douyin.com/xxxxxx/") == ""


def test_resolve_contract_with_mock(monkeypatch):
    """mock 掉网络层，验证 resolve 的解析/轨道判定逻辑与输出契约。"""
    detail = {
        "status_code": 0,
        "aweme_detail": {
            "desc": "测试标题",
            "video": {
                "duration": 19000,
                "width": 1920,
                "height": 1080,
                "play_addr": {"url_list": [
                    "https://www.douyin.com/aweme/v1/play/?video_id=v0d00f&ratio=1080p&line=0"]},
            },
        },
    }
    seen = {}

    class FakeResp:
        def __init__(self, payload, url="", ctype=""):
            self._p = payload
            self.url = url
            self.headers = {"Content-Type": ctype or "application/json"}
        def geturl(self):
            return self.url
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return json.dumps(self._p).encode()

    def fake_urlopen_dispatch(req, timeout=15):
        if "aweme/v1/web/aweme/detail" in req.full_url:
            seen["detail_url"] = req.full_url
            return FakeResp(detail)
        # play 短链 302 → douyinvod 混合轨直链
        return FakeResp(None, url="https://v26-web.douyinvod.com/x/?mime_type=video_mp4",
                        ctype="video/mp4")

    monkeypatch.setattr(douyin_direct, "_ttwid", lambda timeout=10: "ttw-token")
    monkeypatch.setattr(douyin_direct.urllib.request, "urlopen", fake_urlopen_dispatch)

    r = douyin_direct.resolve("https://www.douyin.com/video/7380308675841297704")
    assert r["ok"] is True
    assert r["title"] == "测试标题"
    assert r["duration"] == 19
    assert r["video_id"] == "7380308675841297704"
    assert r["video_has_audio"] is True  # mime_type=video_mp4 是混合轨
    assert r["audio_url"] == ""
    assert r["ext"] == "mp4"
    # 签名参数确实带上了 a_bogus
    assert "a_bogus=" in seen["detail_url"]


def test_resolve_video_only_track(monkeypatch):
    """分离轨场景：mime_type=video（无 _mp4）→ 需配对 audio_url。"""
    detail = {
        "status_code": 0,
        "aweme_detail": {
            "desc": "分离轨",
            "video": {
                "duration": 5000,
                "width": 1280,
                "height": 720,
                "play_addr": {"url_list": []},
                "bit_rate": [
                    {"play_addr": {"url_list": [
                        "https://v26-web.douyinvod.com/a?mime_type=video"]}},
                ],
            },
        },
    }

    class FakeResp:
        def __init__(self, payload, url=""):
            self._p = payload
            self.url = url
        def geturl(self):
            return self.url
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return json.dumps(self._p).encode()

    def dispatch(req, timeout=15):
        if "aweme/v1/web/aweme/detail" in req.full_url:
            return FakeResp(detail)
        raise AssertionError("分离轨场景不应走 play 短链跟随")

    monkeypatch.setattr(douyin_direct, "_ttwid", lambda timeout=10: "ttw")
    monkeypatch.setattr(douyin_direct.urllib.request, "urlopen", dispatch)
    # detail 里埋一个分离音频 URL 供全局扫描
    detail["aweme_detail"]["video"]["bit_rate"][0]["audio_url"] = \
        "https://v26-web.douyinvod.com/b?mime_type=audio"

    r = douyin_direct.resolve("https://www.douyin.com/video/7380308675841297704")
    assert r["video_has_audio"] is False
    assert r["audio_url"].endswith("mime_type=audio")
