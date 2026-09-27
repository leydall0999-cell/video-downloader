#!/usr/bin/env python3
"""香港节点定点补丁：m3u8 不得被当作「可直接下载的文件」。

为什么香港也要打
----------------
ECS 的 `_peer_overseas_fallback` 中间件会把含海外站的 `POST /api/resolve`
原样转发给对端（香港）。也就是说**海外 m3u8 链接的解析全部落在香港**，
只修 ECS 等于没修（线上实测：修完 ECS 后海外 .m3u8 仍返回 direct_url = 该清单）。

改动（两处，均为最小必要）
  1. `_DIRECT_EXT_RE` 白名单里去掉 `m3u8`；
  2. `_detect_direct_url` 增加 HLS 护栏（extension 之外再兜 protocol / _is_hls_url）。

幂等 + 断言：锚点必须各命中恰好一次；已打过补丁则直接跳过。
香港是独立的老分支，**禁止整文件覆盖**，只做定点替换。
"""
import io
import re
import shutil
import sys
import time

TARGET = "/opt/vdl/server/downloader.py"

OLD_RE = r'r"\.(mp4|webm|m4a|mp3|mov|mkv|ogg|flac|avi|wmv|m4v|ts|flv|f4v|m3u8)(\?|#|$|&)", re.IGNORECASE'
NEW_RE = r'r"\.(mp4|webm|m4a|mp3|mov|mkv|ogg|flac|avi|wmv|m4v|ts|flv|f4v)(\?|#|$|&)", re.IGNORECASE'

# 在 protocol 白名单检查之后插入 HLS 护栏（锚点取紧随其后的那行，全文件唯一）
ANCHOR_OLD = """    protocol = (info.get("protocol") or "").split("+")[0].lower()
    if protocol not in ("http", "https", ""):
        return None
    if _DIRECT_EXT_RE.search(url) or _DIRECT_EXT_RE.search(f".{info.get('ext') or ''}"):
        return url
    return None"""
ANCHOR_NEW = """    protocol = (info.get("protocol") or "").split("+")[0].lower()
    if protocol not in ("http", "https", ""):
        return None
    # HLS 清单永远不是「可直取的文件」：manifest 只是分片索引，存下来是废文本。
    # 护栏有双重意义 —— 既挡住 m3u8 扩展名，也挡住「无扩展名但实际是清单」的源
    # （此时 protocol 常为 m3u8_native，上面那行已拦；这里再兜一次 _is_hls_url）。
    if protocol in ("m3u8", "m3u8_native") or _is_hls_url(url):
        return None
    if _DIRECT_EXT_RE.search(url) or _DIRECT_EXT_RE.search(f".{info.get('ext') or ''}"):
        return url
    return None"""

with io.open(TARGET, "r", encoding="utf-8") as fh:
    src = fh.read()

already = 'm3u8_native") or _is_hls_url(url)' in src
if already:
    print("⏭ 已经打过补丁，跳过")
    sys.exit(0)

n_re = src.count(OLD_RE)
n_anchor = src.count(ANCHOR_OLD)
print(f"锚点命中：_DIRECT_EXT_RE={n_re}  _detect_direct_url={n_anchor}")
if n_re != 1 or n_anchor != 1:
    print("❌ 锚点命中数不为 1，拒绝打补丁（香港是独立老分支，格式可能已变）")
    sys.exit(2)

assert "_is_hls_url" in src, "❌ 找不到 _is_hls_url 定义，不能加护栏"

out = src.replace(OLD_RE, NEW_RE, 1).replace(ANCHOR_OLD, ANCHOR_NEW, 1)
assert out.count('m3u8_native") or _is_hls_url(url)') == 1
assert out.count(OLD_RE) == 0, "旧正则仍残留"

bak = f"{TARGET}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
shutil.copy2(TARGET, bak)
with io.open(TARGET, "w", encoding="utf-8") as fh:
    fh.write(out)
print(f"✅ 已打补丁；备份 {bak}")
print(f"   行数 {src.count(chr(10))} → {out.count(chr(10))}（应 +5）")
