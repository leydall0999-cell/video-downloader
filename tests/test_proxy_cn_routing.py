"""国内站代理分流回归：VDL_PROXY_CN 必须被实际使用，不能被无条件置空。

历史上 _base_options 对国内站写死 options["proxy"] = ""，导致即便配了
VDL_PROXY_CN，B站/抖音等国内站请求也不会走国内出口代理，海外部署（Railway）
被地理围栏 403。本测试锁定修复后的正确行为。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import downloader  # noqa: E402


def _proxy_for(host: str, env=None) -> str:
    saved = {}
    try:
        for k in ("VDL_PROXY_CN", "VDL_PROXY", "https_proxy", "http_proxy"):
            saved[k] = os.environ.pop(k, None)
        if env:
            for k, v in env.items():
                os.environ[k] = v
        opts = downloader._base_options(host=host)
        return opts.get("proxy", "")
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)


def test_china_host_uses_vdl_proxy_cn_when_set():
    # 核心回归：配了国内出口代理，B站 必须走它
    assert (
        _proxy_for("www.bilibili.com", {"VDL_PROXY_CN": "http://user:pass@cn:18888"})
        == "http://user:pass@cn:18888"
    )


def test_china_host_empty_when_proxy_cn_unset():
    # 本机在国内直连：未配代理时显式置空，避免 yt-dlp 误读海外系统代理
    assert _proxy_for("www.bilibili.com") == ""


def test_overseas_host_not_affected_by_proxy_cn():
    # 海外站绝不能被 VDL_PROXY_CN 影响，仍走 VDL_PROXY/系统代理链路
    assert _proxy_for("www.youtube.com", {"VDL_PROXY_CN": "http://user:pass@cn:18888"}) == ""


def test_china_host_proxy_cn_does_not_set_mitm_skip():
    # 国内出口代理走 CONNECT 隧道（透明，不 MITM），不应跳过证书校验
    saved = {k: os.environ.pop(k, None) for k in ("VDL_PROXY_CN", "VDL_PROXY", "https_proxy", "http_proxy")}
    try:
        os.environ["VDL_PROXY_CN"] = "http://user:pass@cn:18888"
        opts = downloader._base_options(host="www.bilibili.com")
        assert opts.get("proxy") == "http://user:pass@cn:18888"
        assert opts.get("no_check_certificates") is not True
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
