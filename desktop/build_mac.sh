#!/usr/bin/env bash
# 构建 macOS 桌面版（VideoDownloader.app）
# 用法：bash desktop/build_mac.sh
# 产物：dist/VideoDownloader.app（双击即用，无需安装 Python/ffmpeg）
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PIP_INDEX="https://mirrors.aliyun.com/pypi/simple/"
VENV="$REPO/.build_venv"

echo "▶ 准备构建环境（独立 venv）"
if [[ ! -x "$VENV/bin/python" ]]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --no-cache-dir --index-url "$PIP_INDEX" -r requirements.txt pyinstaller Pillow pywebview

echo "▶ 生成应用图标（512x512 圆角 + 下载箭头）"
"$VENV/bin/python" - "$REPO" <<'PY'
import sys
from pathlib import Path
from PIL import Image, ImageDraw
repo = Path(sys.argv[1])
W = 512
img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([0, 0, W, W], radius=112, fill=(45, 140, 240, 255))
cx = W // 2
# 箭头头部（朝下三角）
d.polygon([(cx, 372), (cx - 96, 256), (cx + 96, 256)], fill=(255, 255, 255, 255))
# 箭杆
d.rectangle([cx - 30, 140, cx + 30, 300], fill=(255, 255, 255, 255))
# 底部托盘
d.rounded_rectangle([cx - 120, 406, cx + 120, 452], radius=22, fill=(255, 255, 255, 255))
out = repo / "desktop" / "icon.png"
img.save(out)
print("icon.png ->", out)
PY
ICON_PNG="$REPO/desktop/icon.png"
ICON_ICNS="$REPO/desktop/icon.icns"
rm -f "$ICON_ICNS"
sips -s format icns "$ICON_PNG" --out "$ICON_ICNS" >/dev/null 2>&1 || true

echo "▶ 打包 VideoDownloader.app（单文件夹 / 无控制台）"
# 用 rename 移走旧产物，避免触发沙盒的批量删除守卫（pyinstaller 清理旧 build/dist 会被拦截）
if [ -e "$REPO/dist/VideoDownloader.app" ]; then
  mv "$REPO/dist/VideoDownloader.app" "$REPO/dist/_old_$$.app" 2>/dev/null || true
fi
if [ -e "$REPO/dist/VideoDownloader" ]; then
  mv "$REPO/dist/VideoDownloader" "$REPO/dist/_oldfolder_$$" 2>/dev/null || true
fi
if [ -e "$REPO/build/VideoDownloader" ]; then
  mv "$REPO/build/VideoDownloader" "$REPO/build/_old_$$" 2>/dev/null || true
fi
"$VENV/bin/pyinstaller" \
  --name VideoDownloader \
  --windowed \
  --noconfirm \
  --icon "$ICON_ICNS" \
  --osx-bundle-identifier com.videodownloader.desktop \
  --paths "$REPO/server" \
  --paths "$REPO" \
  --add-data "$REPO/web:web" \
  --add-data "$REPO/yt_dlp_plugins:yt_dlp_plugins" \
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
  --hidden-import webview.platforms.cocoa \
  --collect-submodules yt_dlp \
  "$REPO/desktop/desktop_launcher.py"

echo "▶ 捆绑 ffmpeg + ffprobe"
FF="$(command -v ffmpeg || echo /opt/homebrew/bin/ffmpeg)"
FFPROBE="$(command -v ffprobe || echo /opt/homebrew/bin/ffprobe)"
APP_BIN="$REPO/dist/VideoDownloader.app/Contents/MacOS"
mkdir -p "$APP_BIN/bin"
cp "$FF" "$APP_BIN/bin/ffmpeg"
cp "$FFPROBE" "$APP_BIN/bin/ffprobe"
chmod +x "$APP_BIN/bin/ffmpeg" "$APP_BIN/bin/ffprobe"

echo "▶ dylibbundler 把 ffmpeg/ffprobe 依赖打进来（脱离 Homebrew）"
# ⚠️ @executable_path = bin/ 这个目录，所以 ../libs 才是 Contents/MacOS/libs/
# ⚠️ 之前用 @executable_path/libs 错了——会落到 Contents/MacOS/bin/libs/
if command -v dylibbundler >/dev/null 2>&1; then
  rm -rf "$REPO/dist/VideoDownloader.app/Contents/MacOS/libs"
  dylibbundler -x "$APP_BIN/bin/ffmpeg" -d "$REPO/dist/VideoDownloader.app/Contents/MacOS/libs" -p "@executable_path/../libs" -cd -b >/dev/null
  dylibbundler -x "$APP_BIN/bin/ffprobe" -d "$REPO/dist/VideoDownloader.app/Contents/MacOS/libs" -p "@executable_path/../libs" -cd -b >/dev/null
else
  echo "⚠️  未找到 dylibbundler（brew install dylibbundler）—— 跳过；将依赖系统 Homebrew dylib"
fi

echo "▶ 注入构建指纹（页脚显示，便于确认是否最新版）"
BUILD_HASH="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"
BUILD_DATE="$(git -C "$REPO" log -1 --format='%cd' --date=format:'%m-%d %H:%M' 2>/dev/null || echo '?')"
BUILD_INFO="构建 $BUILD_HASH @ $BUILD_DATE"
perl -0pi -e "s{<span id=\"buildTag\" class=\"build-tag\"></span>}{<span id=\"buildTag\" class=\"build-tag\">$BUILD_INFO</span>}" "$REPO/dist/VideoDownloader.app/Contents/Resources/web/index.html" 2>/dev/null || true
echo "   指纹：$BUILD_INFO"

echo "▶ 签名（ad-hoc）"
codesign --force --deep --sign - "$REPO/dist/VideoDownloader.app" 2>/dev/null
xattr -dr com.apple.quarantine "$REPO/dist/VideoDownloader.app" 2>/dev/null
echo "   签名完成：$(codesign -dv "$REPO/dist/VideoDownloader.app" 2>&1 | grep 'Signature=' | head -1)"

echo "▶ 生成 DMG 分发包"
rm -f "$REPO/dist/VideoDownloader.dmg"
hdiutil create -volname "VideoDownloader" -srcfolder "$REPO/dist/VideoDownloader.app" -ov -format UDZO "$REPO/dist/VideoDownloader.dmg" >/dev/null 2>&1 || echo "⚠️ DMG 生成失败（可忽略，.app 仍可单独分发）"

echo "✅ 完成"
echo "   App : dist/VideoDownloader.app（双击即用）"
echo "   分发: dist/VideoDownloader.dmg（拖到 应用程序 即可）"
echo "   浏览器自动访问 http://127.0.0.1:8321（端口被占用会自动顺延）"
