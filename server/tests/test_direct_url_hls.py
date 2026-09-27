"""「m3u8 不得被当成可直接下载的文件」回归测试。

背景（2026-09-27 实测复现）
--------------------------
用户粘贴一个裸 m3u8 链接时，`probe()` 会在最前面命中
`_looks_like_direct_file(url)` 并**短路返回**（跳过 yt-dlp）：

    {"direct": True, "url": "<那个 m3u8>", "ext": "m3u8", ...}

于是 `summarize()` 里 `_detect_direct_url()` 把它当成直链返回，前端走「直接保存到本机」
分支 —— 用户下到的是一个**几百字节的播放列表文本**，不是视频。

根因是 `_DIRECT_EXT_RE` 的扩展名白名单里混进了 `m3u8`。HLS 清单是**分片索引**，
不是媒体文件：正确路径是交给 HLS 合成（服务器 ffmpeg 或浏览器内合成）。

本测试钉住：
  1. `.m3u8` 不再被 `_looks_like_direct_file` 命中（否则又短路跳过 yt-dlp）；
  2. `_detect_direct_url` 对 HLS 一律返回 None —— 无论靠扩展名还是 `protocol`，
     也无论 URL 带不带 query；
  3. 不误伤：`.mp4` / `.ts` / `.webm` 等真·媒体文件仍然透传（`.ts` 是 HLS 分片，
     但单个 `.ts` 本身就是完整可播的 MPEG-TS，属于「可直取文件」）；
  4. 非 `direct` 标记的 info 仍旧不产生直链。

运行：cd server && python tests/test_direct_url_hls.py
"""
import os
import sys

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

# 隔离数据目录：import downloader 可能牵连 app 的路径常量，绝不能落到真实用户目录
os.environ.setdefault("VDL_DATA_DIR", "/tmp/vdl_test_direct_hls")

import downloader as D  # noqa: E402

_FAILS: list[str] = []
_CASES = 0


def check(name: str, got, want) -> None:
    global _CASES
    _CASES += 1
    if got != want:
        _FAILS.append(f"{name}\n     期望: {want!r}\n     实得: {got!r}")


# ---------------------------------------------------------------- 1) 短路入口
# 这一层最关键：它决定 yt-dlp 会不会被调用。命中即「跳过解析」
check("m3u8 不再被当作直链文件（无 query）",
      D._looks_like_direct_file("https://cdn.example.com/hls/index.m3u8"), None)
check("m3u8 不再被当作直链文件（带 query）",
      D._looks_like_direct_file("https://cdn.example.com/hls/index.m3u8?token=abc"), None)
check("master.m3u8 同样不命中",
      D._looks_like_direct_file("https://cdn.example.com/hls/master.m3u8"), None)

# 真·媒体文件必须继续命中（否则会白跑一遍 yt-dlp，且丢掉「零服务器带宽」的好处）
check("mp4 仍被当作直链文件",
      D._looks_like_direct_file("https://cdn.example.com/a.mp4"),
      "https://cdn.example.com/a.mp4")
check("ts 仍被当作直链文件（单个 .ts 是完整 MPEG-TS）",
      D._looks_like_direct_file("https://cdn.example.com/seg-1.ts"),
      "https://cdn.example.com/seg-1.ts")
check("webm 仍被当作直链文件",
      D._looks_like_direct_file("https://cdn.example.com/a.webm"),
      "https://cdn.example.com/a.webm")
check("已知平台域名不受影响（仍是 None）",
      D._looks_like_direct_file("https://v.qq.com/x/cover/a.mp4"), None)


# ------------------------------------------------- 2) _detect_direct_url 护栏
_M3U8 = "https://cdn.example.com/hls/index.m3u8"

check("direct + m3u8 + m3u8_native → None",
      D._detect_direct_url({"direct": True, "url": _M3U8, "protocol": "m3u8_native"}),
      None)
check("direct + m3u8 + protocol=http（靠扩展名拦）→ None",
      D._detect_direct_url({"direct": True, "url": _M3U8, "protocol": "http"}),
      None)
check("direct + m3u8 + protocol 缺失 → None",
      D._detect_direct_url({"direct": True, "url": _M3U8}),
      None)
check("direct + m3u8 带 query + protocol=http → None",
      D._detect_direct_url({"direct": True, "url": _M3U8 + "?token=xyz", "protocol": "http"}),
      None)
check("direct + m3u8 + ext=m3u8（url 无扩展名，靠 ext 兜）→ None",
      D._detect_direct_url({"direct": True, "url": "https://cdn.example.com/manifest",
                            "ext": "m3u8", "protocol": "http"}),
      None)
check("direct + manifest 无扩展名但 protocol=m3u8_native → None",
      D._detect_direct_url({"direct": True, "url": "https://cdn.example.com/manifest",
                            "protocol": "m3u8_native"}),
      None)

# 不误伤：真媒体文件仍要透传
check("direct + mp4 → 透传",
      D._detect_direct_url({"direct": True, "url": "https://cdn.example.com/a.mp4",
                            "protocol": "https"}),
      "https://cdn.example.com/a.mp4")
check("direct + ts → 透传",
      D._detect_direct_url({"direct": True, "url": "https://cdn.example.com/seg.ts",
                            "protocol": "https"}),
      "https://cdn.example.com/seg.ts")
check("direct + 无扩展名 + ext=mp4 → 透传",
      D._detect_direct_url({"direct": True, "url": "https://cdn.example.com/play",
                            "ext": "mp4", "protocol": "http"}),
      "https://cdn.example.com/play")

# 无 direct 标记 → 一律 None（这条与原行为一致，防回归）
check("无 direct 标记 → None",
      D._detect_direct_url({"url": "https://cdn.example.com/a.mp4", "protocol": "https"}),
      None)
check("direct 但 url 为空 → None",
      D._detect_direct_url({"direct": True, "url": ""}), None)


# --------------------------------------------------------- 3) _is_hls_url 行为
check("_is_hls_url 认 .m3u8", D._is_hls_url("https://a/b.m3u8"), True)
check("_is_hls_url 认带 query 的 .m3u8", D._is_hls_url("https://a/b.m3u8?t=1"), True)
check("_is_hls_url 不认 .ts", D._is_hls_url("https://a/b.ts"), False)
check("_is_hls_url 空串为 False", D._is_hls_url(""), False)


# --------------------------------------------------------------- 4) 汇总
if _FAILS:
    print(f"✗ test_direct_url_hls: {len(_FAILS)}/{_CASES} 项失败\n")
    for i, f in enumerate(_FAILS, 1):
        print(f"  {i}. {f}\n")
    sys.exit(1)
print(f"✓ test_direct_url_hls: {_CASES}/{_CASES} 项通过")
