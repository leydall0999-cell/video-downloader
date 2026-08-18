"""测试链接归一化：B站 长链转 b23.tv 短链 + 通用追踪参数净化。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from downloader import _normalize_share_url, _strip_tracking_params


# —— B站：长链自动转 b23.tv 短链（已验证有效绕过直链 403）——
def test_bili_long_to_b23tv():
    assert (
        _normalize_share_url("https://www.bilibili.com/video/BV1Rh411h7Fp")
        == "https://b23.tv/BV1Rh411h7Fp"
    )


def test_bili_long_without_www():
    assert (
        _normalize_share_url("https://bilibili.com/video/BV1Rh411h7Fp")
        == "https://b23.tv/BV1Rh411h7Fp"
    )


def test_bili_mobile_long():
    assert (
        _normalize_share_url("https://m.bilibili.com/video/BV1Rh411h7Fp")
        == "https://b23.tv/BV1Rh411h7Fp"
    )


def test_bili_long_preserves_p_t():
    assert (
        _normalize_share_url("https://www.bilibili.com/video/BV1Rh411h7Fp?p=2&t=120")
        == "https://b23.tv/BV1Rh411h7Fp?p=2&t=120"
    )


def test_bili_long_strips_tracking():
    assert (
        _normalize_share_url(
            "https://www.bilibili.com/video/BV1Rh411h7Fp?vd_source=a8cc883975&spm_id_from=333.999.0.0"
        )
        == "https://b23.tv/BV1Rh411h7Fp"
    )


def test_bili_short_unchanged():
    assert (
        _normalize_share_url("https://b23.tv/pjNtDgD")
        == "https://b23.tv/pjNtDgD"
    )


def test_bili_short_bv_unchanged():
    assert (
        _normalize_share_url("https://b23.tv/BV1Rh411h7Fp?p=2")
        == "https://b23.tv/BV1Rh411h7Fp?p=2"
    )


def test_bili_short_strips_tracking():
    # 短链自带的追踪参数也应被剥除
    assert (
        _normalize_share_url("https://b23.tv/pjNtDgD?from=share&vd_source=abc")
        == "https://b23.tv/pjNtDgD"
    )


# —— 通用追踪参数净化（抖音/快手/小红书/腾讯等，不转短链，仅去噪）——
def test_douyin_long_strips_tracking():
    assert (
        _normalize_share_url(
            "https://www.douyin.com/video/7378273621290576162?from=share&share_id=abc"
        )
        == "https://www.douyin.com/video/7378273621290576162"
    )


def test_douyin_short_unchanged():
    # 抖音分享短链（随机 token）原样返回，不强行构造
    assert (
        _normalize_share_url("https://v.douyin.com/iRqNxxxx/")
        == "https://v.douyin.com/iRqNxxxx/"
    )


def test_kuaishou_long_strips_tracking():
    assert (
        _normalize_share_url(
            "https://www.kuaishou.com/short-video/3xyz?shareId=abc&utm_source=wechat"
        )
        == "https://www.kuaishou.com/short-video/3xyz"
    )


def test_xiaohongshu_long_strips_tracking():
    assert (
        _normalize_share_url(
            "https://www.xiaohongshu.com/explore/65abc?xhsshare=copy&appuid=123"
        )
        == "https://www.xiaohongshu.com/explore/65abc"
    )


def test_non_platform_keeps_useful_params():
    # 非追踪参数应保留（如 v.qq.com 的 vid、youtube 的 v）
    assert (
        _normalize_share_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&feature=youtu.be")
        == "https://www.youtube.com/watch?v=dQw4w9WgXcQ&feature=youtu.be"
    )


def test_non_url_unchanged():
    assert _normalize_share_url("not a url") == "not a url"


# —— _strip_tracking_params 单元行为 ——
def test_strip_only_tracking():
    assert (
        _strip_tracking_params("https://x.com/a?b=1&vd_source=x&c=2")
        == "https://x.com/a?b=1&c=2"
    )


def test_strip_all_tracking_yields_clean():
    assert (
        _strip_tracking_params("https://x.com/a?from=share&vd_source=x&spm_id_from=y")
        == "https://x.com/a"
    )


if __name__ == "__main__":
    test_bili_long_to_b23tv()
    test_bili_long_without_www()
    test_bili_mobile_long()
    test_bili_long_preserves_p_t()
    test_bili_long_strips_tracking()
    test_bili_short_unchanged()
    test_bili_short_bv_unchanged()
    test_bili_short_strips_tracking()
    test_douyin_long_strips_tracking()
    test_douyin_short_unchanged()
    test_kuaishou_long_strips_tracking()
    test_xiaohongshu_long_strips_tracking()
    test_non_platform_keeps_useful_params()
    test_non_url_unchanged()
    test_strip_only_tracking()
    test_strip_all_tracking_yields_clean()
    print("share url normalize: 16 passed")
