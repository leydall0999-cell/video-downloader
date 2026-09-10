"""下载器链接解析 / 归一化回归测试（2026-09-10 新增）。

背景：downloader.py 里的「平台链接解析与归一化」是下载链路的最前端，
也是历史回归最频繁的区域（B站 短链展开、追踪参数剥离、平台识别）。
此前该区域零测试保护，改一行就可能让某平台下载静默失效。

本测试覆盖以下纯函数（全部离线、无网络、无真实下载）：
  - _host_of                取主机名（去 www./m.、小写化）
  - _normalize_bilibili_url B站 长链/b23.tv BV 短链/根路径 BV → 统一长链
  - _strip_tracking_params  剥离 vd_source/spm_id_from/utm_* 等追踪参数
  - _root_domain            取根域（v.qq.com → qq.com；a.b.com.cn → b.com.cn）
  - _is_douyin_host / _is_kuaishou_host / is_cookie_hardened_host  平台识别
  - _looks_like_direct_file 直链媒体文件识别（已知平台不误判）

运行：
    cd server && python tests/test_downloader_url_parsing.py
    cd server && python -m pytest tests/test_downloader_url_parsing.py -v
"""
import os
import sys

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from downloader import (  # noqa: E402
    _host_of, _is_douyin_host, _is_kuaishou_host, _looks_like_direct_file,
    _normalize_bilibili_url, _root_domain, _strip_tracking_params,
    is_cookie_hardened_host,
)

_BV = "BV1xx411c7mD"


# --------------------------------------------------------------------------- #
# _host_of
# --------------------------------------------------------------------------- #
def test_host_of_strips_www_and_m():
    """www./m. 前缀应被剥离，主机名统一小写。"""
    assert _host_of(f"https://www.bilibili.com/video/{_BV}") == "bilibili.com"
    assert _host_of(f"https://m.bilibili.com/video/{_BV}") == "bilibili.com"
    assert _host_of("https://WWW.Example.COM/a") == "example.com"
    print("✅ _host_of 剥离 www./m. 前缀并小写化")


def test_host_of_invalid_returns_empty():
    """无 scheme / 空串等解析不出主机名的情况返回空串，不抛异常。"""
    assert _host_of("") == ""
    assert _host_of("bilibili.com/video/x") == ""   # 无 scheme，urlparse 取不到 hostname
    assert _host_of("not a url") == ""
    print("✅ _host_of 非法输入返回空串且不抛异常")


# --------------------------------------------------------------------------- #
# _normalize_bilibili_url
# --------------------------------------------------------------------------- #
def test_normalize_bilibili_standard_long_url():
    """标准长链原样保留，仅标准化为 www.bilibili.com。"""
    assert _normalize_bilibili_url(f"https://www.bilibili.com/video/{_BV}") == \
        f"https://www.bilibili.com/video/{_BV}"
    # m. 子域也归一为 www.
    assert _normalize_bilibili_url(f"https://m.bilibili.com/video/{_BV}") == \
        f"https://www.bilibili.com/video/{_BV}"
    print("✅ 标准长链归一为 www.bilibili.com/video/BVxxx")


def test_normalize_bilibili_b23tv_bv_short():
    """b23.tv 携带 BV 号的短链可本地推导，无需发网络请求。"""
    got = _normalize_bilibili_url(f"https://b23.tv/{_BV}")
    assert got == f"https://www.bilibili.com/video/{_BV}", got
    got2 = _normalize_bilibili_url(f"https://www.b23.tv/{_BV}?p=3")
    assert got2 == f"https://www.bilibili.com/video/{_BV}?p=3", got2
    print("✅ b23.tv/BVxxx 短链本地推导为长链（不发网络请求）")


def test_normalize_bilibili_root_bv():
    """根路径 BV（无 /video/ 前缀）必须补全，否则 yt-dlp 会退化为 generic 提取器。"""
    got = _normalize_bilibili_url(f"https://www.bilibili.com/{_BV}")
    assert got == f"https://www.bilibili.com/video/{_BV}", got
    print("✅ 根路径 /BVxxx 补全为 /video/BVxxx")


def test_normalize_bilibili_keeps_p_and_t_drops_tracking():
    """保留分P(p)与时间戳(t)，剥离 share_source/vd_source/spm_id_from 等追踪参数。"""
    raw = f"https://www.bilibili.com/video/{_BV}?spm_id_from=333.999&vd_source=deadbeef&p=2&t=95"
    got = _normalize_bilibili_url(raw)
    assert got == f"https://www.bilibili.com/video/{_BV}?p=2&t=95", got
    print("✅ 保留 p/t，剥离 share_source/vd_source/spm_id_from")


def test_normalize_bilibili_non_bili_untouched():
    """非 B站 链接必须原样返回（不可误伤其它平台）。"""
    for url in (
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://v.douyin.com/xxxxx/",
        "https://example.com/video/BVnotbili",
    ):
        assert _normalize_bilibili_url(url) == url, url
    print("✅ 非 B站 链接原样返回，不误伤其它平台")


# --------------------------------------------------------------------------- #
# _strip_tracking_params
# --------------------------------------------------------------------------- #
def test_strip_tracking_removes_tracking_keeps_business():
    """剥离追踪参数，保留业务参数。"""
    got = _strip_tracking_params("https://x.com/a?vd_source=1&utm_source=y&p=2&t=9")
    assert got == "https://x.com/a?p=2&t=9", got
    print("✅ 剥离 vd_source/utm_*，保留 p/t")


def test_strip_tracking_no_query_untouched():
    """无 query 时原样返回。"""
    url = "https://x.com/a/b"
    assert _strip_tracking_params(url) == url
    print("✅ 无 query 时原样返回")


def test_strip_tracking_all_removed_no_dangling_question_mark():
    """全部参数都是追踪参数时，结果不应残留 '?'。"""
    got = _strip_tracking_params("https://a.com/x?vd_source=1&utm_source=y")
    assert got == "https://a.com/x", got
    assert "?" not in got
    print("✅ 全为追踪参数时不残留悬空 '?'")


def test_strip_tracking_keep_whitelist():
    """keep 白名单内的参数必须保留（爱奇艺 shareId 等业务标识场景）。"""
    got = _strip_tracking_params(
        "https://a.com/x?shareId=ABC&vd_source=1", keep={"shareId"}
    )
    assert got == "https://a.com/x?shareId=ABC", got
    print("✅ keep 白名单参数被保留")


# --------------------------------------------------------------------------- #
# _root_domain
# --------------------------------------------------------------------------- #
def test_root_domain_basic():
    assert _root_domain("v.qq.com") == "qq.com"
    assert _root_domain("www.douyin.com") == "douyin.com"
    assert _root_domain("example.com") == "example.com"
    print("✅ _root_domain 取二级根域")


def test_root_domain_multi_level_cctld():
    """a.b.com.cn → b.com.cn（多级国别域）。"""
    assert _root_domain("a.b.com.cn") == "b.com.cn"
    assert _root_domain("a.b.net.cn") == "b.net.cn"
    print("✅ _root_domain 正确处理 .com.cn 多级国别域")


def test_root_domain_single_label_and_empty():
    assert _root_domain("localhost") == "localhost"
    assert _root_domain("") == ""
    print("✅ _root_domain 处理单段主机名与空串")


# --------------------------------------------------------------------------- #
# 平台识别
# --------------------------------------------------------------------------- #
def test_platform_host_detection_matches_subdomains():
    """平台识别应同时匹配裸域与子域，不误伤相似域。"""
    assert _is_douyin_host("www.douyin.com")
    assert _is_douyin_host("douyin.com")
    assert _is_douyin_host("v.douyin.com")
    assert not _is_douyin_host("youtube.com")
    assert not _is_douyin_host("notdouyin.com")   # 防后缀误匹配

    assert _is_kuaishou_host("v.kuaishou.com")
    assert _is_kuaishou_host("kuaishou.com")
    assert not _is_kuaishou_host("kuaishou.com.evil.net")
    print("✅ 平台识别匹配子域且不误伤相似域")


def test_cookie_hardened_hosts():
    """强反爬平台（需浏览器 Cookie）识别正确。"""
    assert is_cookie_hardened_host("v.qq.com")
    assert is_cookie_hardened_host("www.douyin.com")
    assert is_cookie_hardened_host("www.xiaohongshu.com")
    assert not is_cookie_hardened_host("bilibili.com")   # B站 走公共池，非 hardened
    assert not is_cookie_hardened_host("")
    print("✅ is_cookie_hardened_host 识别正确")


# --------------------------------------------------------------------------- #
# _looks_like_direct_file
# --------------------------------------------------------------------------- #
def test_looks_like_direct_file_recognizes_media_ext():
    """非平台域名 + 媒体扩展名 → 判定为直链。"""
    for url in (
        "https://cdn.example.com/a.mp4",
        "https://cdn.example.com/hls.m3u8",
        "https://cdn.example.com/a.mkv?token=1",
        "https://files.example.org/song.m4a",
    ):
        assert _looks_like_direct_file(url) == url, url
    print("✅ 媒体扩展名直链被正确识别")


def test_looks_like_direct_file_rejects_known_platform():
    """已知平台即使以 .mp4 结尾也不能走直链透传（必须交给 yt-dlp 解析）。"""
    assert _looks_like_direct_file(f"https://www.bilibili.com/video/{_BV}.mp4") is None
    assert _looks_like_direct_file("https://www.youtube.com/x.mp4") is None
    print("✅ 已知平台不被误判为直链")


def test_looks_like_direct_file_rejects_non_media():
    """无媒体扩展名 → 不是直链。"""
    assert _looks_like_direct_file("https://example.com/page") is None
    assert _looks_like_direct_file("") is None
    print("✅ 非媒体链接返回 None")


if __name__ == "__main__":
    test_host_of_strips_www_and_m()
    test_host_of_invalid_returns_empty()
    test_normalize_bilibili_standard_long_url()
    test_normalize_bilibili_b23tv_bv_short()
    test_normalize_bilibili_root_bv()
    test_normalize_bilibili_keeps_p_and_t_drops_tracking()
    test_normalize_bilibili_non_bili_untouched()
    test_strip_tracking_removes_tracking_keeps_business()
    test_strip_tracking_no_query_untouched()
    test_strip_tracking_all_removed_no_dangling_question_mark()
    test_strip_tracking_keep_whitelist()
    test_root_domain_basic()
    test_root_domain_multi_level_cctld()
    test_root_domain_single_label_and_empty()
    test_platform_host_detection_matches_subdomains()
    test_cookie_hardened_hosts()
    test_looks_like_direct_file_recognizes_media_ext()
    test_looks_like_direct_file_rejects_known_platform()
    test_looks_like_direct_file_rejects_non_media()
    print("\n🎉 下载器链接解析测试全部通过（19 项）")
