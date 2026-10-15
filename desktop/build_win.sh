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
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
# 优先用品牌源图 desktop/icon.png（与 macOS icns 同源，保证两端图标一致）；
# 源图缺失时回退到手绘占位图标。
src = repo / "desktop" / "icon.png"
if src.is_file() and src.stat().st_size > 1024:
    img = Image.open(src).convert("RGBA")
    if img.size != (256, 256):
        img = img.resize((256, 256), Image.LANCZOS)
    print("icon source: desktop/icon.png")
else:
    print("icon source: fallback (desktop/icon.png 缺失，手绘占位)")
    W = 512
    img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, W, W], radius=112, fill=(45, 140, 240, 255))
    cx = W // 2
    d.polygon([(cx, 372), (cx - 96, 256), (cx + 96, 256)], fill=(255, 255, 255, 255))
    d.rectangle([cx - 30, 140, cx + 30, 300], fill=(255, 255, 255, 255))
    d.rounded_rectangle([cx - 120, 406, cx + 120, 452], radius=22, fill=(255, 255, 255, 255))
img.save(repo / "desktop" / "icon.ico", format="ICO", sizes=SIZES)
print("icon.ico generated")
PY
ICON_ICO="$REPO/desktop/icon.ico"

echo "▶ 打包 VideoDownloader.exe（单文件夹 / 无控制台）"

# ── 解说管线随包（自包含铁律：#198 单二进制双角色）──

# 白名单精选管线文件到临时 staging 目录（排除 .git/work/input/output/.venv 等垃圾）
# ⚠️ 2026-10-16 修：此前 scripts/ 用 `cp -R` 全量拷贝，而 scripts/ 下有两个 Qwen3-TTS
#   专用 venv（.venv_qwen3tts / .venv_qwen3tts_mlx，共 1.9G，mlx 版在 Windows 上还用不了），
#   会全量打进安装包。现与 build_mac.sh 对齐同一套排除集（Git Bash 的 find 是 GNU findutils，
#   -mindepth/-maxdepth 可用，无需 rsync）。
_stage_commentary() {
  local src="$1"
  local staging="$REPO/build/commentary_staging"
  rm -rf "$staging"
  mkdir -p "$staging"
  cp "$src/process.py" "$staging/" 2>/dev/null || true
  # #198 单二进制双角色：worker 子进程靠 commentary_worker.py 重入自身
  [ -f "$src/commentary_worker.py" ] && cp "$src/commentary_worker.py" "$staging/" 2>/dev/null || true
  if [ -d "$src/scripts" ]; then
    mkdir -p "$staging/scripts"
    find "$src/scripts" -mindepth 1 -maxdepth 1 \
      ! -name '.venv*' \
      ! -name '__pycache__' \
      ! -name '.DS_Store' \
      ! -name '*.log' \
      ! -name '*.bak*' \
      ! -name '*.orig' \
      ! -name '*.rej' \
      ! -name '*.pyc' \
      -exec cp -R {} "$staging/scripts/" \;
  fi
  # 与 build_mac.sh 对齐（2026-09-07 起）：不预置 whisper-base 模型（141M，改首次按需下载）。
  # 只复制 models/ 下除 whisper-base 外的其它内容(若有)。
  if [ -d "$src/models" ]; then
    mkdir -p "$staging/models"
    for _m in "$src/models"/*; do
      [ -e "$_m" ] || continue
      [ "$(basename "$_m")" = "whisper-base" ] && continue
      cp -R "$_m" "$staging/models/"
    done
  fi
  [ -d "$src/assets" ] && cp -R "$src/assets" "$staging/"
  # 字幕自包含铁律（对齐 build_mac.sh）：Windows 目标机（英文服务器/精简系统）普遍无中文字体，
  # staging/assets/fonts 必须带真实 .ttf/.ttc，否则 PIPL 字幕中文不显示。
  _fonts="$staging/assets/fonts"
  mkdir -p "$_fonts"
  if ! ls "$_fonts"/*.tt[cf] >/dev/null 2>&1; then
    for _sys in "/c/Windows/Fonts/msyh.ttc" "/c/Windows/Fonts/simhei.ttf" "/c/Windows/Fonts/msyh.ttf"; do
      [ -f "$_sys" ] && cp "$_sys" "$_fonts/" 2>/dev/null && echo "   ✔ 复制系统中文字体进包: $(basename "$_sys")" && break
    done
    ls "$_fonts"/*.tt[cf] >/dev/null 2>&1 || echo "   ⚠️ 包内无中文字体，字幕中文将依赖用户系统字体" >&2
  fi
  echo "$staging"
}

COMMENTARY_DIR="${COMMENTARY_PIPELINE_DIR:-$REPO/../commentary-pipeline}"
COMMENTARY_DATA=()
if [ -d "$COMMENTARY_DIR" ] && [ -f "$COMMENTARY_DIR/process.py" ]; then
  echo "▶ 捆绑解说管线(随包自包含): $COMMENTARY_DIR"
  "$VENV/Scripts/pip.exe" install --quiet --no-cache-dir --index-url "$PIP_INDEX" -r "$COMMENTARY_DIR/requirements.txt"
  COMMENTARY_STAGING=$(_stage_commentary "$COMMENTARY_DIR")
  # ── 铁律校验 1：随包的必须是 1.1.0+ 新 CLI（含 --one-click）──
  # 防止误捆绑旧版管线（历史上 COMMENTARY_PIPELINE_DIR 未传入时静默打进旧版 1.0.0）。
  if ! grep -q -- "--one-click" "$COMMENTARY_STAGING/process.py" 2>/dev/null; then
    echo "❌ 解说管线版本校验失败：staging 内未检测到 1.1.0 新 CLI(--one-click)，疑似误捆绑旧版！"
    echo "   当前 COMMENTARY_DIR=$COMMENTARY_DIR"
    exit 1
  fi
  echo "   ✔ 解说管线版本校验通过（检测到 1.1.0 新 CLI）"
  # ── 铁律校验 2：写管线指纹（与 build_mac.sh 同口径，供包内追溯）──
  _pl_sha="unknown"
  if [ -d "$COMMENTARY_DIR/.git" ]; then
    _pl_sha="$(git -C "$COMMENTARY_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    [ -n "$(git -C "$COMMENTARY_DIR" status --porcelain -- scripts process.py commentary_worker.py 2>/dev/null)" ] && _pl_sha="${_pl_sha}-dirty"
  fi
  printf '%s\n' "$_pl_sha" > "$COMMENTARY_STAGING/_pipeline_sha.txt"
  echo "   ✔ 管线指纹: ${_pl_sha}"
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
  echo "⚠️  注意：无解说管线 = Windows 版失去解说/字幕管线能力，正式发布前必须确认本项！"
fi

# ── deno：YouTube JS 挑战（nsig）的 JS runtime ──
# requirements.txt 里的 PyPI 包 `deno` 会把官方二进制装到 $VENV/Scripts/deno.exe，
# 而 PyInstaller **不会**分发 venv 的脚本目录 ⇒ 必须显式 --add-binary 才有。
# 落到包内 <_MEIPASS>/bin/deno(.exe)，运行时由 server/downloader.py 的
# _find_js_runtime_binary() 找到并交给 yt-dlp 的 js_runtimes。
# 缺了它：带登录态会卡在 "The page needs to be reloaded"，裸请求被 bot 拦截。
# 注：Windows 下 --add-binary 用 `;` 分隔（macOS/Linux 用 `:`）。
_DENO_BINARY_ARGS=()
if [ -f "$VENV/Scripts/deno.exe" ]; then
  _DENO_BINARY_ARGS=(--add-binary "$VENV/Scripts/deno.exe;bin")
else
  echo "⚠️  未找到 $VENV/Scripts/deno.exe —— 包内将没有 JS runtime，YouTube 可能解不了 nsig 挑战" >&2
fi

# ⚠️ 不要再加 --collect-all onnx：它会把 onnx 官方的测试数据集（约 2.4 万个文件）
#    整个打进包，属纯死重（server/ 代码零 import onnx），拖慢构建并撑大安装包。
#    与 build_mac.sh 保持一致；真需要时 PyInstaller 的常规依赖分析会自动收进来。
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
  --collect-submodules yt_dlp \
  --collect-all Cryptodome \
  "${COMMENTARY_DATA[@]}" \
  ${_DENO_BINARY_ARGS[@]+"${_DENO_BINARY_ARGS[@]}"} \
  "$REPO/desktop/desktop_launcher.py"

# 清理 staging 临时目录（Windows 无 ~/.Trash：mv 失败则就地删除，CI 上无副作用）
if [ -n "${COMMENTARY_STAGING:-}" ] && [ -d "$COMMENTARY_STAGING" ]; then
  mv "$COMMENTARY_STAGING" "$HOME/.Trash/commentary_staging_$(date +%s)" 2>/dev/null \
    || rm -rf "$COMMENTARY_STAGING" 2>/dev/null || true
fi

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

echo "▶ 写入版本身份（version.txt / build_version.txt）"
# 语义化版本号来自仓库根 VERSION（仅发版时手工 +1），与 build_mac.sh 同一来源。
# server/routers/system.py 的 _candidates_version_txt() 依次查找：
#   <exe 同级>/version.txt → <_MEIPASS>/server/version.txt → <server 包>/version.txt
# PyInstaller onedir 6.x+ 把运行期文件收进 _internal/，5.x 直接平铺在产物根，
# 故两处都写，兼容两种布局（macOS 则是 Contents/Resources/version.txt）。
APP_VERSION="$(cat "$REPO/VERSION" 2>/dev/null | tr -d '[:space:]' | head -1)"
if [ -z "$APP_VERSION" ]; then APP_VERSION="0.0.0"; fi
DIST_APP="$REPO/dist/VideoDownloader"
if [ -d "$DIST_APP/_internal" ]; then RUN_ROOT="$DIST_APP/_internal"; else RUN_ROOT="$DIST_APP"; fi
mkdir -p "$RUN_ROOT/server"
echo "$APP_VERSION" > "$DIST_APP/version.txt"
echo "$APP_VERSION" > "$RUN_ROOT/server/version.txt"
# build_version.txt：与 build_mac.sh 同口径的构建指纹（v<ver> · 构建 <short-sha> @ MM-DD HH:MM）。
# CI 环境（GitHub Actions）有 .git；本机无 .git 时退化到 "dev"，不影响功能。
BUILD_TAG="v${APP_VERSION} · 构建 dev"
if [ -d "$REPO/.git" ]; then
  _sha="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo dev)"
  _date="$(date +%m-%d' '%H:%M)"
  BUILD_TAG="v${APP_VERSION} · 构建 ${_sha} @ ${_date}"
fi
echo "$BUILD_TAG" > "$DIST_APP/build_version.txt"
echo "$BUILD_TAG" > "$RUN_ROOT/server/build_version.txt"
echo "   版本：${APP_VERSION}"
echo "   构建：${BUILD_TAG}"

echo "▶ 打包安装程序（Inno Setup → dist/VideoDownloader-Setup-${APP_VERSION}.exe）"
# iscc 查找顺序：PATH → 常见安装路径（Git Bash 的 /c/ 路径，MSYS 会转成 Windows 风格传给原生 exe）
_ISCC=""
if command -v iscc >/dev/null 2>&1; then
  _ISCC="$(command -v iscc)"
else
  for _p in "/c/Program Files (x86)/Inno Setup 6/iscc.exe" \
            "/c/Program Files/Inno Setup 6/iscc.exe" \
            "/c/Program Files (x86)/Inno Setup 5/iscc.exe"; do
    [ -f "$_p" ] && { _ISCC="$_p"; break; }
  done
fi
if [ -z "$_ISCC" ]; then
  echo "⚠️  未找到 Inno Setup（iscc）——跳过安装程序打包，只产出 onedir 文件夹"
  echo "⚠️  GitHub Actions 构建机请确保镜像含 Inno Setup 6（或 choco install innosetup）"
else
  echo "   iscc: $_ISCC"
  "$_ISCC" \
    "/DAppVersion=${APP_VERSION}" \
    "/DOutputDir=$REPO/dist" \
    "/DOutputBaseFilename=VideoDownloader-Setup-${APP_VERSION}" \
    "/DSourceDir=$DIST_APP" \
    "/DIconFile=$ICON_ICO" \
    "$REPO/desktop/installer.iss"
  SETUP_EXE="$REPO/dist/VideoDownloader-Setup-${APP_VERSION}.exe"
  if [ -f "$SETUP_EXE" ]; then
    SETUP_SIZE="$(stat -c '%s' "$SETUP_EXE" 2>/dev/null || wc -c < "$SETUP_EXE" | tr -d ' ')"
    echo "   ✔ 安装程序：$SETUP_EXE（${SETUP_SIZE} 字节）"
  else
    echo "❌ Inno Setup 编译后未找到安装程序：$SETUP_EXE"
    exit 1
  fi
fi

echo "✅ 完成：dist/VideoDownloader/VideoDownloader.exe"
if [ -f "$REPO/dist/VideoDownloader-Setup-${APP_VERSION}.exe" ]; then
  echo "   安装程序：dist/VideoDownloader-Setup-${APP_VERSION}.exe"
fi
echo "   双击打开即可，浏览器自动访问 http://127.0.0.1:8321（端口被占用会自动顺延）"
echo "   种子（磁力/种子）下载已随包自带 aria2c，无需本机安装"
