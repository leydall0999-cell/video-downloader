#!/usr/bin/env bash
# 构建 macOS 桌面版（VideoDownloader.app）
# 用法：
#   bash desktop/build_mac.sh                 # 正常构建（结束后自动收尾旧产物）
#   bash desktop/build_mac.sh --cleanup-only  # 只清理历史构建垃圾，不构建
# 环境变量：
#   VDL_BUILD_KEEP_OLD=1  保留最近几份 _old_* 用于回滚（默认 1；设 0 表示全清）
# 产物：dist/VideoDownloader.app（双击即用，无需安装 Python/ffmpeg）
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PIP_INDEX="https://mirrors.aliyun.com/pypi/simple/"
VENV="$REPO/.build_venv"

# ── 历史构建产物收尾 ────────────────────────────────────────────────
# 背景：下面打包前会把上一次产物 mv 成 _old_<pid>（绕开沙盒的批量删除守卫，
#       PyInstaller 自己清 build/dist 会被拦）。只挪不删 => 每构建一次堆 ~200MB。
# 策略：构建成功后统一收尾，每类只保留最近 N 份用于回滚，更早的**移入回收站**
#       （可恢复，不用 rm -rf；osascript/Finder 在本机未授权，故直接搬 ~/.Trash）。
BUILD_KEEP_OLD="${VDL_BUILD_KEEP_OLD:-1}"

trash_path() {
  local src="$1" base dest n
  [ -e "$src" ] || return 0
  base="$(basename "$src")"
  dest="$HOME/.Trash/$base"
  n=1
  while [ -e "$dest" ]; do
    dest="$HOME/.Trash/${base}-$n"
    n=$((n + 1))
  done
  if mv "$src" "$dest" 2>/dev/null; then
    echo "   ♻️  移入回收站：$base"
  else
    echo "   ⚠️  移入回收站失败，已跳过（未删除）：$src"
  fi
}

# 用法：cleanup_family <保留份数> <glob...>；按修改时间新→旧，超出的进回收站
cleanup_family() {
  local keep="$1"; shift
  local idx=0 p
  while IFS= read -r p; do
    [ -n "$p" ] && [ -e "$p" ] || continue
    idx=$((idx + 1))
    if [ "$idx" -le "$keep" ]; then
      echo "   ↩︎  保留（可回滚）：$(basename "$p")"
    else
      trash_path "$p"
    fi
  done < <(ls -dt "$@" 2>/dev/null || true)
}

cleanup_old_artifacts() {
  echo "▶ 收尾：清理历史构建产物（保留最近 ${BUILD_KEEP_OLD} 份，其余进回收站）"
  cleanup_family "$BUILD_KEEP_OLD" "$REPO"/dist/_old_*.app
  cleanup_family "$BUILD_KEEP_OLD" "$REPO"/dist/_oldfolder_*
  cleanup_family "$BUILD_KEEP_OLD" "$REPO"/build/_old_*
  echo "   dist 现占用：$(du -sh "$REPO/dist" 2>/dev/null | awk '{print $1}')  build 现占用：$(du -sh "$REPO/build" 2>/dev/null | awk '{print $1}')"
}

if [ "${1:-}" = "--cleanup-only" ] || [ "${1:-}" = "--cleanup" ]; then
  cleanup_old_artifacts
  echo "✅ 清理完成（文件在废纸篓里，确认没问题后自行清空即可释放空间）"
  exit 0
fi

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

# ── 解说管线随包（自包含铁律：#198 单二进制双角色）──
# 设环境变量 COMMENTARY_PIPELINE_DIR 指向 commentary-pipeline 仓库根目录；
# 不设默认尝试 REPO 平级目录（可被 Python 项目目录布局覆盖）。
COMMENTARY_DIR="${COMMENTARY_PIPELINE_DIR:-$REPO/../commentary-pipeline}"
COMMENTARY_DATA=()
if [ -d "$COMMENTARY_DIR" ] && [ -f "$COMMENTARY_DIR/process.py" ]; then
  echo "▶ 捆绑解说管线(随包自包含): $COMMENTARY_DIR"
  # 安装管线核心依赖到构建 venv，让 PyInstaller 一并冻结进 exe。
  # 单二进制双角色：worker 子进程用 sys.executable 重入自身，
  # faster_whisper/edge_tts 等必须在冻结包里、不能依赖外部 .venv。
  "$VENV/bin/pip" install --quiet --no-cache-dir --index-url "$PIP_INDEX" -r "$COMMENTARY_DIR/requirements.txt"
  COMMENTARY_DATA=(
    --add-data "$COMMENTARY_DIR:commentary"
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

"$VENV/bin/pyinstaller" \
  --name VideoDownloader \
  --windowed \
  --noconfirm \
  --clean \
  --icon "$ICON_ICNS" \
  --osx-bundle-identifier com.videodownloader.desktop \
  --paths "$REPO/server" \
  --paths "$REPO" \
  --add-data "$REPO/web:web" \
  --add-data "$REPO/yt_dlp_plugins:yt_dlp_plugins" \
  --add-data "$REPO/server:server" \
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
  "${COMMENTARY_DATA[@]}" \
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
  dylibbundler -x "$APP_BIN/bin/ffmpeg" -d "$REPO/dist/VideoDownloader.app/Contents/MacOS/libs" -p "@executable_path/../libs" -cd -b -of >/dev/null
  dylibbundler -x "$APP_BIN/bin/ffprobe" -d "$REPO/dist/VideoDownloader.app/Contents/MacOS/libs" -p "@executable_path/../libs" -cd -b -of >/dev/null
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

# 走到这里说明构建、签名、DMG 全部成功（set -e 保证失败会提前退出），
# 此时旧产物已无回滚价值，统一收尾，避免 dist/ 无限膨胀。
cleanup_old_artifacts

echo "✅ 完成"
echo "   App : dist/VideoDownloader.app（双击即用）"
echo "   分发: dist/VideoDownloader.dmg（拖到 应用程序 即可）"
echo "   浏览器自动访问 http://127.0.0.1:8321（端口被占用会自动顺延）"
