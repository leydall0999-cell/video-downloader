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

# 白名单精选管线文件到临时 staging 目录（排除 .git/work/input/output/.venv 等垃圾）
_stage_commentary() {
  local src="$1"
  local staging="$REPO/build/commentary_staging"
  rm -rf "$staging"
  mkdir -p "$staging"
  cp "$src/process.py" "$staging/" 2>/dev/null || true
  [ -d "$src/scripts" ] && cp -R "$src/scripts" "$staging/"
  [ -d "$src/models" ] && cp -R "$src/models" "$staging/"
  [ -d "$src/assets" ] && cp -R "$src/assets" "$staging/"
  for _f in "requirements.txt" "requirements-gpu.txt" "requirements-moviepy.txt" "requirements-worker.txt" \
            "VERSION" "selfcheck.py" "prepare_model.py" "start_worker.sh" \
            "COMMENTARY_PLAN.md" "verify_quality.py" "run_local.sh" "README.md"; do
    [ -f "$src/$_f" ] && cp "$src/$_f" "$staging/"
  done
  echo "$staging"
}

COMMENTARY_DIR="${COMMENTARY_PIPELINE_DIR:-$REPO/../commentary-pipeline}"
COMMENTARY_DATA=()
if [ -d "$COMMENTARY_DIR" ] && [ -f "$COMMENTARY_DIR/process.py" ]; then
  echo "▶ 捆绑解说管线(随包自包含): $COMMENTARY_DIR"
  "$VENV/Scripts/pip.exe" install --quiet --no-cache-dir --index-url "$PIP_INDEX" -r "$COMMENTARY_DIR/requirements.txt"
  COMMENTARY_STAGING=$(_stage_commentary "$COMMENTARY_DIR")
  COMMENTARY_DATA=(
    --add-data "$COMMENTARY_STAGING;commentary"
    --collect-all faster_whisper
    --collect-all edge_tts
    --collect-all ctranslate2
    --collect-all tokenizers
    --collect-all onnxruntime
    --collect-all av
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
  --hidden-import platforms \
  --hidden-import tasks \
  --hidden-import yt_dlp_plugins \
  --hidden-import yt_dlp_plugins.extractor \
  --hidden-import yt_dlp_plugins.extractor.chrqj \
  --hidden-import yt_dlp_plugins.extractor.kuaishou \
  --hidden-import webview \
  --hidden-import webview.platforms.winforms \
  --hidden-import dewatermark_core \
  --collect-binaries cv2 \
  --collect-all pymupdf \
  --collect-all fitz \
  --collect-all onnx \
  --collect-submodules yt_dlp \
  "${COMMENTARY_DATA[@]}" \
  "$REPO/desktop/desktop_launcher.py"

# 清理 staging 临时目录
[ -n "${COMMENTARY_STAGING:-}" ] && [ -d "$COMMENTARY_STAGING" ] && mv "$COMMENTARY_STAGING" "$HOME/.Trash/commentary_staging_$(date +%s)" 2>/dev/null || true

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

echo "▶ 捆绑 aria2c（种子后端随安装包自包含，从官方 GitHub release 下载 Windows 版）"
"$VENV/Scripts/python.exe" "$REPO/desktop/bundle_aria2.py" "$REPO/dist/VideoDownloader" 2>&1 || echo "   ⚠️ aria2 打包跳过（种子功能将运行时禁用，需本机安装 aria2 或装 VC++ 运行库）"

echo "✅ 完成：dist/VideoDownloader/VideoDownloader.exe"
echo "   双击打开即可，浏览器自动访问 http://127.0.0.1:8321（端口被占用会自动顺延）"
echo "   种子（磁力/种子）下载已随包自带 aria2c，无需本机安装"
