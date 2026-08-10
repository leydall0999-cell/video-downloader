#!/usr/bin/env bash
# 构建 Windows 桌面版（VideoDownloader.exe）
# 用法（在 Windows 的 Git Bash / WSL 中）：bash desktop/build_win.sh
# 产物：dist/VideoDownloader/VideoDownloader.exe（双击即用，无需安装 Python/ffmpeg）
#
# 注意：
#   1. 此脚本需在 Windows 环境运行（PyInstaller 无法跨平台编译 exe）。
#   2. 构建机需先安装 ffmpeg 且可在 PATH 中找到（或把 ffmpeg.exe 放到仓库根）。
#   3. 若 pip 慢，可设置 PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PIP_INDEX="${PIP_INDEX:-https://mirrors.aliyun.com/pypi/simple/}"
VENV="$REPO/.build_venv"

echo "▶ 准备构建环境（独立 venv）"
if [[ ! -x "$VENV/Scripts/python.exe" ]]; then
  python -m venv "$VENV"
fi
"$VENV/Scripts/pip.exe" install --quiet --index-url "$PIP_INDEX" -r requirements.txt pyinstaller Pillow pywebview

echo "▶ 生成应用图标"
"$VENV/Scripts/python.exe" - "$REPO" <<'PY'
import sys
from pathlib import Path
from PIL import Image, ImageDraw
repo = Path(sys.argv[1])
W = 512
img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([0, 0, W, W], radius=112, fill=(45, 140, 240, 255))
cx = W // 2
d.polygon([(cx, 372), (cx - 96, 256), (cx + 96, 256)], fill=(255, 255, 255, 255))
d.rectangle([cx - 30, 140, cx + 30, 300], fill=(255, 255, 255, 255))
d.rounded_rectangle([cx - 120, 406, cx + 120, 452], radius=22, fill=(255, 255, 255, 255))
img.save(repo / "desktop" / "icon.ico")
print("icon.ico generated")
PY
ICON_ICO="$REPO/desktop/icon.ico"

echo "▶ 打包 VideoDownloader.exe（单文件夹 / 无控制台）"

# ── 解说管线随包（自包含铁律：#198 单二进制双角色）──
COMMENTARY_DIR="${COMMENTARY_PIPELINE_DIR:-$REPO/../commentary-pipeline}"
COMMENTARY_DATA=()
if [ -d "$COMMENTARY_DIR" ] && [ -f "$COMMENTARY_DIR/process.py" ]; then
  echo "▶ 捆绑解说管线(随包自包含): $COMMENTARY_DIR"
  "$VENV/Scripts/pip.exe" install --quiet --no-cache-dir --index-url "$PIP_INDEX" -r "$COMMENTARY_DIR/requirements.txt"
  COMMENTARY_DATA=(
    --add-data "$COMMENTARY_DIR;commentary"
    --hidden-import faster_whisper
    --hidden-import ctranslate2
    --hidden-import tokenizers
    --hidden-import onnxruntime
    --hidden-import edge_tts
    --hidden-import av
    --collect-submodules edge_tts
  )
else
  echo "⚠️  未找到解说管线($COMMENTARY_DIR)，解说功能不包含在包内；设 COMMENTARY_PIPELINE_DIR=<路径> 可启用"
fi

"$VENV/Scripts/pyinstaller.exe" \
  --name VideoDownloader \
  --windowed \
  --noconfirm \
  --clean \
  --icon "$ICON_ICO" \
  --paths "$REPO/server" \
  --paths "$REPO" \
  --add-data "$REPO/web;web" \
  --add-data "$REPO/yt_dlp_plugins;yt_dlp_plugins" \
  --hidden-import app \
  --hidden-import downloader \
  --hidden-import clouddrive \
  --hidden-import platforms \
  --hidden-import tasks \
  --hidden-import yt_dlp_plugins \
  --hidden-import yt_dlp_plugins.extractor \
  --hidden-import yt_dlp_plugins.extractor.chrqj \
  --hidden-import yt_dlp_plugins.extractor.kuaishou \
  --hidden-import webview \
  --hidden-import webview.platforms.winforms \
  --collect-submodules yt_dlp \
  "${COMMENTARY_DATA[@]}" \
  "$REPO/desktop/desktop_launcher.py"

echo "▶ 捆绑 ffmpeg / ffprobe（放入 bin/ 子目录，启动器会优先用捆绑版本）"
if command -v ffmpeg >/dev/null 2>&1; then
  FF="$(command -v ffmpeg)"
elif [[ -f "$REPO/ffmpeg.exe" ]]; then
  FF="$REPO/ffmpeg.exe"
else
  echo "⚠️  未找到 ffmpeg，请先安装或把 ffmpeg.exe 放到仓库根目录"
  exit 1
fi
mkdir -p "$REPO/dist/VideoDownloader/bin"
cp "$FF" "$REPO/dist/VideoDownloader/bin/ffmpeg.exe"
# ffprobe 与 ffmpeg 同目录（Windows 上为 ffprobe.exe），解说管线重度依赖
if [[ -f "${FF%ffmpeg}ffprobe" ]]; then
  cp "${FF%ffmpeg}ffprobe" "$REPO/dist/VideoDownloader/bin/ffprobe.exe"
elif command -v ffprobe >/dev/null 2>&1; then
  cp "$(command -v ffprobe)" "$REPO/dist/VideoDownloader/bin/ffprobe.exe"
elif [[ -f "$REPO/ffprobe.exe" ]]; then
  cp "$REPO/ffprobe.exe" "$REPO/dist/VideoDownloader/bin/ffprobe.exe"
else
  echo "⚠️  未找到 ffprobe，解说功能将不可用（下载不受影响）"
fi

echo "✅ 完成：dist/VideoDownloader/VideoDownloader.exe"
echo "   双击打开即可，浏览器自动访问 http://127.0.0.1:8321（端口被占用会自动顺延）"
