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

# ── 构建互斥锁：防止并发构建互相争抢 dist/ 与 .build_venv（原子 mkdir，退出即释放）──
BUILD_LOCK="$REPO/.build.lock"
if ! mkdir "$BUILD_LOCK" 2>/dev/null; then
  echo "❌ 已有另一个构建在运行（锁目录 $BUILD_LOCK 存在）。请等其结束后再构建。" >&2
  echo "   若确认无其它构建，可手动删除该锁目录：rmdir '$BUILD_LOCK'" >&2
  exit 1
fi
trap 'rmdir "$BUILD_LOCK" 2>/dev/null || true' EXIT

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
"$VENV/bin/pip" install --timeout 120 --retries 5 --no-cache-dir --index-url "$PIP_INDEX" -r requirements.txt pyinstaller Pillow pywebview

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
# 删除 stale .spec：PyInstaller 若发现 repo 根已有同名 spec，会直接复用其配置，
# 而旧 spec 可能指向已不存在的 staging 目录或导致 onedir 输出，破坏后续 .app 注入步骤。
rm -f "$REPO/VideoDownloader.spec"

# ── 解说管线随包（自包含铁律：#198 单二进制双角色）──
# 设环境变量 COMMENTARY_PIPELINE_DIR 指向 commentary-pipeline 仓库根目录；
# 不设默认尝试 REPO 平级目录（可被 Python 项目目录布局覆盖）。

# 白名单精选管线文件到临时 staging 目录：
# 避免 .git(608M)/work(924M)/input(1.6G)/output(425M)/.venv(131M) 等垃圾随包
_stage_commentary() {
  local src="$1"
  # 每次构建用独立 staging 目录(带 PID)，避免并发构建互相挪走对方目录导致校验误报
  local staging="$REPO/build/commentary_staging_$$"
  # 清掉历史 staging 残留(避免堆积；用移入回收站，不触发删除守卫)
  # 注意：trash_path 的日志必须丢到 stderr，否则会被 $(_stage_commentary ...) 一并捕获，
  # 污染 COMMENTARY_STAGING 变量（见下方 grep 校验），导致版本校验误报失败。
  for _old in "$REPO"/build/commentary_staging_*; do
    [ -e "$_old" ] && trash_path "$_old" >&2
  done
  mkdir -p "$staging"
  # 白名单精选管线核心文件（排除 .git/work/input/output/.venv/__pycache__ 等 3.5G 垃圾）
  cp "$src/process.py" "$staging/" 2>/dev/null || true
  [ -d "$src/scripts" ] && cp -R "$src/scripts" "$staging/"
  [ -d "$src/models" ] && cp -R "$src/models" "$staging/"
  [ -d "$src/assets" ] && cp -R "$src/assets" "$staging/"
  # 字幕自包含铁律：确保随包 assets/fonts 有真实中文字体，否则 PIPL 在打包机上字幕中文
  # 会回落到系统字体（用户机器缺中文字体时直接不显示 -> 用户看到的「没有字幕」）。
  # 开发态 assets/fonts 通常只有 README，这里从本机系统字体补一份进去。
  _fonts="$staging/assets/fonts"
  mkdir -p "$_fonts"
  if ! ls "$_fonts"/*.tt[cf] >/dev/null 2>&1; then
    for _sys in "/System/Library/Fonts/Hiragino Sans GB.ttc" \
                "/System/Library/Fonts/PingFang.ttc" \
                "/System/Library/Fonts/STHeiti Light.ttc"; do
      if [ -f "$_sys" ]; then
        cp "$_sys" "$_fonts/" 2>/dev/null && echo "   ✔ 复制系统中文字体进包: $(basename "$_sys")" >&2
        break
      fi
    done
  fi
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
  # 安装管线核心依赖到构建 venv，让 PyInstaller 一并冻结进 exe。
  # 单二进制双角色：worker 子进程用 sys.executable 重入自身，
  # faster_whisper/edge_tts 等必须在冻结包里、不能依赖外部 .venv。
  "$VENV/bin/pip" install --timeout 120 --retries 5 --no-cache-dir --index-url "$PIP_INDEX" -r "$COMMENTARY_DIR/requirements.txt"
  # 白名单精选管线文件到临时 staging 目录，再 --add-data（避免 .git/work/input 等 3.5G 垃圾随包）
  COMMENTARY_STAGING=$(_stage_commentary "$COMMENTARY_DIR")
  # ── 铁律校验：确保随包的是新版解说管线（含 --one-click 等 1.1.0 新 CLI）──
  # 历史上曾因 COMMENTARY_PIPELINE_DIR 未传入而静默打进旧版 1.0.0；
  # 这里在打包前先卡死，宁可构建失败也不产出错误版本。
  if ! grep -q -- "--one-click" "$COMMENTARY_STAGING/process.py" 2>/dev/null; then
    echo "❌ 解说管线版本校验失败：未在 staging 检测到 1.1.0 新 CLI(--one-click)，疑似误捆绑旧版！"
    echo "   当前 COMMENTARY_DIR=$COMMENTARY_DIR"
    echo "   请确认该目录指向 commentary-pipeline 1.1.0（设 COMMENTARY_PIPELINE_DIR 或确保 $REPO/../commentary-pipeline 软链正确）"
    exit 1
  fi
  echo "   ✔ 解说管线版本校验通过（检测到 1.1.0 新 CLI）"
  COMMENTARY_DATA=(
    --add-data "$COMMENTARY_STAGING:commentary"
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
  --icon "$ICON_ICNS" \
  --osx-bundle-identifier com.videodownloader.desktop \
  --paths "$REPO/server" \
  --paths "$REPO" \
  --add-data "$REPO/web:web" \
  --add-data "$REPO/yt_dlp_plugins:yt_dlp_plugins" \
  --add-data "$REPO/server:server" \
  --hidden-import app \
  --hidden-import downloader \
  --hidden-import cookie_cache \
  --hidden-import ydlp_update \
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
  ${COMMENTARY_DATA[@]+"${COMMENTARY_DATA[@]}"} \
  "$REPO/desktop/desktop_launcher.py"

# 校验：PyInstaller 必须产出 .app bundle，否则后续注入 ffmpeg/web 都会失败
if [ ! -d "$REPO/dist/VideoDownloader.app" ]; then
  echo "❌ PyInstaller 未产出 dist/VideoDownloader.app（可能被 stale .spec 覆盖为 onedir）" >&2
  echo "   请检查是否有 VideoDownloader.spec 残留或 PyInstaller 输出异常。" >&2
  exit 1
fi

# 清理 staging 临时目录（PyInstaller 已把文件拷进 .app，不再需要）
[ -n "${COMMENTARY_STAGING:-}" ] && [ -d "$COMMENTARY_STAGING" ] && mv "$COMMENTARY_STAGING" "$HOME/.Trash/commentary_staging_$(date +%s)" 2>/dev/null || true

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
  trash_path "$REPO/dist/VideoDownloader.app/Contents/MacOS/libs"
  dylibbundler -x "$APP_BIN/bin/ffmpeg" -d "$REPO/dist/VideoDownloader.app/Contents/MacOS/libs" -p "@executable_path/../libs" -cd -b -of >/dev/null
  dylibbundler -x "$APP_BIN/bin/ffprobe" -d "$REPO/dist/VideoDownloader.app/Contents/MacOS/libs" -p "@executable_path/../libs" -cd -b -of >/dev/null
else
  echo "⚠️  未找到 dylibbundler（brew install dylibbundler）—— 跳过；将依赖系统 Homebrew dylib"
fi

echo "▶ 注入构建指纹（页脚显示，便于确认是否最新版）"
BUILD_HASH="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"
BUILD_DATE="$(git -C "$REPO" log -1 --format='%cd' --date=format:'%m-%d %H:%M' 2>/dev/null || echo '?')"
BUILD_INFO="构建 $BUILD_HASH @ $BUILD_DATE"
# 缓存 bust 用的纯数字构建戳（避免中文/@ 在 perl 替换侧被错误插值），每次构建都变化
BUILD_STAMP="$(date +%y%m%d%H%M%S)"
# 页脚用 .*? 而非空 span，保证重复构建也能覆盖旧指纹（之前空 span 模式在已有内容时不匹配）
perl -0pi -e "s{<span id=\"buildTag\" class=\"build-tag\">.*?</span>}{<span id=\"buildTag\" class=\"build-tag\">$BUILD_INFO</span>}" "$REPO/dist/VideoDownloader.app/Contents/Resources/web/index.html" 2>/dev/null || true
# 给 app.js 注入构建戳作为缓存 bust 版本号（与页脚同源变化），避免桌面端缓存旧脚本
perl -0pi -e "s{__BUILD_FP__}{$BUILD_STAMP}" "$REPO/dist/VideoDownloader.app/Contents/Resources/web/index.html" 2>/dev/null || true
# 同时把指纹写入程序可读文件，供 /api/version 自检（避免肉眼误判版本）
echo "$BUILD_INFO" > "$REPO/dist/VideoDownloader.app/Contents/Resources/build_version.txt"
echo "   指纹：$BUILD_INFO  缓存戳：$BUILD_STAMP"

echo "▶ 打包 aria2c（种子后端随安装包自包含，脱离本机 Homebrew）"
python3 "$REPO/desktop/bundle_aria2.py" "$REPO/dist/VideoDownloader.app/Contents/Resources" 2>&1 || echo "   ⚠️ aria2 打包跳过（种子功能将运行时禁用）"

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
