#!/usr/bin/env python3
"""MVP 质量验证工具 —— 一键把本地视频跑通解说成片, 人工判断质量是否值得接入。

把视频丢进来后自动:
  1. 软链/复制到 commentary-pipeline/input/
  2. 跑 process.py --auto (转写 -> 自动解说词草稿 -> 成片)
  3. 打印成片路径并(在 macOS 上)打开预览

用法(managed python):
  PY=/Users/suixindelang/.workbuddy/binaries/python/envs/default/bin/python
  $PY verify_quality.py /path/to/素材.mp4
  $PY verify_quality.py /path/to/素材.mp4 --vertical --voice zh-CN-YunxiNeural

依赖(managed python venv): faster_whisper / edge_tts / moviepy / ffmpeg
注意: --auto 出的是"机器草稿"(转写稿口播化, 无钩子/节奏)。
      想看爆款感, 先 python process.py 输入视频 只转写, 把 transcript.json 交 WorkBuddy 写解说词,
      再 python process.py 输入视频 --edit-only work/<名>.script.json 出片。
"""
import os
import sys
import shutil
import subprocess
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
INPUT = os.path.join(HERE, "input")
sys.path.insert(0, os.path.join(HERE, "scripts"))
from config import OUTPUT  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="解说成片质量验证")
    ap.add_argument("video", help="本地视频路径")
    ap.add_argument("--vertical", action="store_true", help="竖屏 9:16")
    ap.add_argument("--voice", default="zh-CN-XiaoxiaoNeural",
                    help="AI 旁白音色, 如 zh-CN-YunxiNeural")
    args = ap.parse_args()

    src = os.path.abspath(args.video)
    if not os.path.exists(src):
        print(f"[错误] 找不到视频: {src}")
        sys.exit(1)

    os.makedirs(INPUT, exist_ok=True)
    fname = os.path.basename(src)
    dst = os.path.join(INPUT, fname)
    if not os.path.exists(dst):
        try:
            os.symlink(src, dst)
            print(f"  软链 -> {dst}")
        except OSError:
            shutil.copy(src, dst)
            print(f"  复制 -> {dst}")

    cmd = [sys.executable, os.path.join(HERE, "process.py"),
           os.path.join(INPUT, fname), "--auto", "--voice", args.voice]
    if args.vertical:
        cmd.append("--vertical")
    print(">>> 跑解说管线:", " ".join(cmd))
    subprocess.run(cmd, check=True)

    suffix = "_竖屏成片.mp4" if args.vertical else "_成片.mp4"
    out = os.path.join(OUTPUT, os.path.splitext(fname)[0] + suffix)
    print(f"\n✅ 成片: {out}")
    if sys.platform == "darwin" and os.path.exists(out):
        subprocess.run(["open", out])
    print("→ 自己看一眼质量: 解说词有钩子/节奏吗? 配音自然吗? 画面切换顺吗?")


if __name__ == "__main__":
    main()
