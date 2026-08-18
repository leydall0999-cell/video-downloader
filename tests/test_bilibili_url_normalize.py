"""测试 B站 URL 统一归一化为 www.bilibili.com/video/BVxxx 长链。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from downloader import _normalize_bilibili_url


def test_long_url_to_bilibili_com():
    assert (
        _normalize_bilibili_url("https://www.bilibili.com/video/BV1Rh411h7Fp")
        == "https://www.bilibili.com/video/BV1Rh411h7Fp"
    )


def test_long_url_without_www():
    assert (
        _normalize_bilibili_url("https://bilibili.com/video/BV1Rh411h7Fp")
        == "https://www.bilibili.com/video/BV1Rh411h7Fp"
    )


def test_mobile_long_url():
    assert (
        _normalize_bilibili_url("https://m.bilibili.com/video/BV1Rh411h7Fp")
        == "https://www.bilibili.com/video/BV1Rh411h7Fp"
    )


def test_b23tv_bv_short_to_bilibili_com():
    assert (
        _normalize_bilibili_url("https://b23.tv/BV1Rh411h7Fp?p=2")
        == "https://www.bilibili.com/video/BV1Rh411h7Fp?p=2"
    )


def test_long_url_with_preserved_params():
    assert (
        _normalize_bilibili_url("https://www.bilibili.com/video/BV1Rh411h7Fp?p=2&t=120")
        == "https://www.bilibili.com/video/BV1Rh411h7Fp?p=2&t=120"
    )


def test_long_url_removes_tracking_params():
    assert (
        _normalize_bilibili_url(
            "https://www.bilibili.com/video/BV1Rh411h7Fp?vd_source=a8cc883975a4d31d1ac5d818f69e84b1&spm_id_from=333.999.0.0"
        )
        == "https://www.bilibili.com/video/BV1Rh411h7Fp"
    )


def test_b23tv_random_short_unchanged_by_bv_func():
    """随机短码无法被 BV 正则推导，应原样返回（由 _expand_b23tv_url 负责展开）。"""
    assert (
        _normalize_bilibili_url("https://b23.tv/pjNtDgD")
        == "https://b23.tv/pjNtDgD"
    )


def test_non_bilibili_unchanged():
    assert (
        _normalize_bilibili_url("https://www.douyin.com/video/123456")
        == "https://www.douyin.com/video/123456"
    )


if __name__ == "__main__":
    test_long_url_to_bilibili_com()
    test_long_url_without_www()
    test_mobile_long_url()
    test_b23tv_bv_short_to_bilibili_com()
    test_long_url_with_preserved_params()
    test_long_url_removes_tracking_params()
    test_b23tv_random_short_unchanged_by_bv_func()
    test_non_bilibili_unchanged()
    print("bilibili url normalize: 8 passed")
