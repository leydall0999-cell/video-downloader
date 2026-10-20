#!/usr/bin/env python3
"""HK 节点定点补丁：build_quality_options 不得把「直链视频」误标成「最佳音质」。

背景（2026-10-20）：网页版把 `direct_url` 的视频从「浏览器侧分片引擎」改成统一走
「服务器下载任务」后，清晰度面板第一次对这类视频露出。yt-dlp 对**裸媒体直链**
（直接粘贴 .mp4、快手 kwaicdn 文件等）返回的 format 是
`height=None, vcodec=None, acodec=None` —— 三项**全未知**（本地 `yt-dlp -J` 实测）。
老判据只看 `height`，于是这类视频被「纯音频」分支吃掉，面板只剩
「最佳音质（自动）」，用户点「开始下载」拿到的却是视频。

修法：判据改为**显式声明** —— 只有「所有 format 都明确声明无视频轨、且至少一条
明确声明了音频编码」才算纯音频；编码未知的一律按视频给单一「最佳画质」档。

🔴 HK 是独立老分支，**禁整文件覆盖**（会引入跨版本组合爆炸）。本脚本只做插入：
   · 锚点唯一性断言（命中必须恰好 1 次）
   · 幂等（已含 `_no_video` 就跳过）
   · 写前备份到 /opt/vdl/backup/，写后 py_compile
   · 只新增、不删除任何既有行

用法（在 HK 上执行）：
    /opt/vdl/.venv/bin/python /tmp/hk_patch_quality_options.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

TARGET = "/opt/vdl/server/downloader.py"
BACKUP_DIR = "/opt/vdl/backup"
MARK = "_no_video"

ANCHOR = '        audio_ex = {str(f.get("ext") or "").lower() for f in formats}\n'

BLOCK = '''        # ⚠️ 「没有 height」≠「没有视频轨」，`formats` 为空更不等于音频。两种真实情形：
        #   ① **直链文件**：probe() 在 _looks_like_direct_file() 短路，**根本没跑 yt-dlp**
        #      ⇒ formats 为 0 条（2026-10-20 实测 mov_bbb.mp4：`formats count: 0`）；
        #   ② yt-dlp 对裸媒体返回 `height=None, vcodec=None, acodec=None`（三项全未知）。
        # 老判据只看 height ⇒ 直链视频被误判成音频，清晰度面板只剩「最佳音质（自动）」，
        # 用户点「开始下载」拿到的却是视频，观感上就是坏的（直链改走服务器任务后才暴露）。
        # 判据改为**显式声明 + 扩展名兜底**：要么「有 format 且全部明确无视频轨、至少一条
        # 明确声明音频编码」，要么「formats 为空但扩展名是音频」—— 才算纯音频；其余一律
        # 按视频给单一「最佳画质」档（格式选择器 `ba/b` 对两者都能兜底）。
        _ext = str(info.get("ext") or "").lower()
        if not _ext:
            _m = re.search(r"\\.([a-z0-9]{2,5})(?:\\?|#|$|&)",
                           str(info.get("url") or info.get("webpage_url") or ""), re.IGNORECASE)
            _ext = (_m.group(1) if _m else "").lower()
        _audio_url = _ext in {"mp3", "m4a", "aac", "ogg", "oga", "wav", "flac", "opus", "wma"}
        _no_video = all(str(f.get("vcodec") or "none") == "none" for f in formats)
        _has_audio = any(str(f.get("acodec") or "none") != "none" for f in formats)
        _is_audio = (_no_video and _has_audio) if formats else _audio_url
        if not _is_audio:
            return [
                {"key": BEST_KEY, "label": "最佳画质（自动）",
                 "note": "视频+音频自动合并", "approx_size": 0}
            ]
'''


def main():
    if not os.path.isfile(TARGET):
        print("❌ 目标文件不存在：%s" % TARGET)
        return 2
    with open(TARGET, encoding="utf-8") as fh:
        src = fh.read()

    if MARK in src:
        print("⏭ 已包含 %s，跳过（幂等）" % MARK)
        return 0

    hits = src.count(ANCHOR)
    if hits != 1:
        print("❌ 锚点命中 %d 次（要求恰好 1 次），拒绝写入" % hits)
        return 3

    before_lines = src.count("\n")
    patched = src.replace(ANCHOR, BLOCK + ANCHOR, 1)
    after_lines = patched.count("\n")
    if patched.count("\n") < before_lines:
        print("❌ 补丁会删除既有行，拒绝写入")
        return 4

    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(BACKUP_DIR, "downloader.py.pre-quality.%s" % ts)
    shutil.copy2(TARGET, backup)
    print("· 已备份 → %s" % backup)

    # 先写临时文件并 py_compile，通过后再原子替换
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(TARGET), suffix=".py")
    os.close(fd)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(patched)
    py = "/opt/vdl/.venv/bin/python"
    if not os.path.exists(py):
        py = sys.executable
    r = subprocess.run([py, "-m", "py_compile", tmp], capture_output=True, text=True)
    if r.returncode != 0:
        print("❌ py_compile 失败，保留原文件：\n" + r.stderr[-800:])
        os.unlink(tmp)
        return 5
    os.replace(tmp, TARGET)
    print("✅ 已写入 %s（新增 %d 行，删除 0 行）" % (TARGET, after_lines - before_lines))
    print("· 备份保留在 %s" % backup)
    print("· 下一步：systemctl restart vdl-web")
    return 0


if __name__ == "__main__":
    sys.exit(main())
