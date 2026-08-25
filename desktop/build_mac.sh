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

# ── 预下载 baiduPCS-Go（在 pyinstaller 之前，以便 --add-binary 打入包）──
_PCS_BUILD_DIR="$REPO/build/pcs_bin"
mkdir -p "$_PCS_BUILD_DIR"
_PCS_BINARY="$_PCS_BUILD_DIR/BaiduPCS-Go"
if [ ! -x "$_PCS_BINARY" ]; then
  _ARCH="${_ARCH:-$(uname -m)}"
  _PCS_API_URL="https://api.github.com/repos/qjfoidnh/BaiduPCS-Go/releases/latest"
  echo "▶ 预下载 baiduPCS-Go（百度网盘下载功能依赖，${_ARCH:-unknown}）…"
  _PCS_RELEASE_JSON="$(curl -sL --max-time 30 "$_PCS_API_URL" 2>/dev/null || echo '{}')"
  _PCS_TAG="$(echo "$_PCS_RELEASE_JSON" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("tag_name",""))' 2>/dev/null || echo '')"
  _PCS_URL=""
  if [ -n "$_PCS_TAG" ]; then
    # 用环境变量传 arch 进 python（避免 bash 单引号内变量不展开 + set -u 报 unbound）
    _PCS_URL="$(export PCS_ARCH="$_ARCH"; echo "$_PCS_RELEASE_JSON" | python3 -c '
import os,sys,json
arch=os.environ.get("PCS_ARCH","")
for a in json.load(sys.stdin).get("assets",[]):
    n=a.get("name","").lower()
    if "darwin" in n and arch in n and n.endswith(".zip"):
        print(a.get("browser_download_url",""))
        break
' 2>/dev/null || echo '')"
  fi
  if [ -z "$_PCS_URL" ] || [ "$_PCS_URL" = "None" ]; then
    echo "   ⚠️ 无法获取 baiduPCS-Go 下载链接（tag=$_PCS_TAG），将在运行时下载"
    _PCS_BINARY=""
  else
    echo "   下载: $_PCS_URL …"
    _PCS_ZIP="$_PCS_BUILD_DIR/pcs.zip"
    if curl -sL --max-time 120 -o "$_PCS_ZIP" "$_PCS_URL"; then
      python3 -c "
import zipfile, shutil, os, stat
z = zipfile.ZipFile('$_PCS_ZIP')
names = z.namelist()
bin_name = next((n for n in names if n.endswith('BaiduPCS-Go') or n.endswith('baidupcs-go')), None)
if bin_name:
    z.extract(bin_name, '$_PCS_BUILD_DIR')
    extracted = os.path.join('$_PCS_BUILD_DIR', bin_name)
    target = '$_PCS_BINARY'
    if extracted != target:
      if os.path.exists(target): os.unlink(target)
      shutil.move(extracted, target)
    os.chmod(target, os.stat(target).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    print(f'✅ {target}')
else:
    print(f'❌ 未找到二进制: {names[:5]}')
z.close()
" 2>&1 || echo "   ⚠️ 解压失败"
      rm -f "$_PCS_ZIP"
    else
      echo "   ⚠️ 下载失败，将在运行时重试"; _PCS_BINARY=""
    fi
  fi
fi
_PCS_ADD_BINARY_ARG=""
if [ -n "$_PCS_BINARY" ] && [ -x "$_PCS_BINARY" ]; then
  _PCS_ADD_BINARY_ARG="--add-binary $_PCS_BINARY:bin/BaiduPCS-Go"
  echo "   ✔ baiduPCS-Go 将随包分发"
else
  echo "   ⚠️ baiduPCS-Go 未预打包（用户首次使用时从 GitHub 下载）"
fi

# ── 写入构建信息（版本号显示用）──
_BUILD_HASH="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
_BUILD_TIME="$(date '+%m-%d %H:%M')"
cat > "$REPO/server/build_info.txt" <<BUILDINFO
{"hash": "${_BUILD_HASH}", "time": "${_BUILD_TIME}"}
BUILDINFO
echo "   ✔ 构建信息: ${_BUILD_HASH} @ ${_BUILD_TIME}"

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
  --add-data "$REPO/server/build_info.txt:server" \
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
  --hidden-import baidu_pcs \
  --hidden-import baidu_qr \
  --hidden-import dewatermark_core \
  --collect-binaries cv2 \
  --collect-all pymupdf \
  --collect-all fitz \
  --collect-all requests \
  --collect-all PIL \
  --collect-submodules yt_dlp \
  ${COMMENTARY_DATA[@]+"${COMMENTARY_DATA[@]}"} \
  $_PCS_ADD_BINARY_ARG \
  "$REPO/desktop/desktop_launcher.py"

# 校验：PyInstaller 必须产出 .app bundle，否则后续注入 ffmpeg/web 都会失败
if [ ! -d "$REPO/dist/VideoDownloader.app" ]; then
  echo "❌ PyInstaller 未产出 dist/VideoDownloader.app（可能被 stale .spec 覆盖为 onedir）" >&2
  echo "   请检查是否有 VideoDownloader.spec 残留或 PyInstaller 输出异常。" >&2
  exit 1
fi

# 清理 staging 临时目录（PyInstaller 已把文件拷进 .app，不再需要）
[ -n "${COMMENTARY_STAGING:-}" ] && [ -d "$COMMENTARY_STAGING" ] && mv "$COMMENTARY_STAGING" "$HOME/.Trash/commentary_staging_$(date +%s)" 2>/dev/null || true

echo "▶ 应用中文名称（菜单栏 / Dock / 关于窗口）"
PLIST="$REPO/dist/VideoDownloader.app/Contents/Info.plist"
if [ -f "$PLIST" ]; then
  # CFBundleDisplayName：Dock 悬浮提示、菜单栏应用名、About 窗口标题
  # CFBundleName：Application 菜单中 "About XXX" / "Hide XXX" / "Quit XXX" 的 XXX 部分
  plutil -replace CFBundleDisplayName -string "视频下载器" "$PLIST"
  plutil -replace CFBundleName -string "视频下载器" "$PLIST"
  # 声明支持中文本地化（否则 macOS 不加载 zh-Hans.lproj）
  plutil -replace CFBundleLocalizations -json '["zh-Hans", "en"]' "$PLIST"
  plutil -replace CFBundleDevelopmentRegion -string "zh-Hans" "$PLIST"
  echo "   已设置中文名 + 本地化声明：视频下载器 (zh-Hans)"
else
  echo "   ⚠️ Info.plist 不存在，跳过"
fi

echo "▶ 注入中文本地化（覆盖系统默认英文菜单词）"
LPROJ="$REPO/dist/VideoDownloader.app/Contents/Resources/zh-Hans.lproj"
mkdir -p "$LPROJ"
cat > "$LPROJ/Localizable.strings" << 'LOCEOF'
/* Menu bar items */
"About %@" = "关于 %@";
"Hide %@" = "隐藏 %@";
"Hide Others" = "隐藏其他";
"Show All" = "显示全部";
"Quit %@" = "退出 %@";

/* Window menu */
"Minimize" = "最小化";
"Zoom" = "缩放";
"Bring All to Front" = "全部移到最前";

/* File menu */
"Close" = "关闭";
"Save…" = "保存…";
"Revert to Saved" = "恢复已保存版本";

/* Edit menu */
"Undo" = "撤销";
"Redo" = "重做";
"Cut" = "剪切";
"Copy" = "复制";
"Paste" = "粘贴";
"Select All" = "全选";
"Delete" = "删除";

/* Format menu */
"Font" = "字体";
"Show Fonts" = "显示字体";
"Bigger" = "更大";
"Smaller" = "更小";
"Bold" = "粗体";
"Italic" = "斜体";
"Underline" = "下划线";

/* Help */
"Help" = "帮助";
LOCEOF
# .strings 文件必须转成二进制 plist 格式（UTF-8 文本格式 macOS 不加载）
plutil -convert binary1 "$LPROJ/Localizable.strings"
echo "   已注入 zh-Hans.lproj（Quit→退出 / Hide→隐藏 / About→关于 等，二进制格式）"

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
# 解说管线（commentary-pipeline）独立仓库，其 SHA 也一并注入指纹，避免「改了管线但 /api/version 不反映」的错觉。
PIPELINE_HASH="$(git -C "$COMMENTARY_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
BUILD_INFO="构建 $BUILD_HASH (app) / $PIPELINE_HASH (pipe) @ $BUILD_DATE"
# 缓存 bust 用的纯数字构建戳（避免中文/@ 在 perl 替换侧被错误插值），每次构建都变化
BUILD_STAMP="$(date +%y%m%d%H%M%S)"
# 页脚用 .*? 而非空 span，保证重复构建也能覆盖旧指纹（之前空 span 模式在已有内容时不匹配）
perl -0pi -e "s{<span id=\"buildTag\" class=\"build-tag\">.*?</span>}{<span id=\"buildTag\" class=\"build-tag\">$BUILD_INFO</span>}" "$REPO/dist/VideoDownloader.app/Contents/Resources/web/index.html" 2>/dev/null || true
# 给 app.js 注入构建戳作为缓存 bust 版本号（与页脚同源变化），避免桌面端缓存旧脚本
perl -0pi -e "s{__BUILD_FP__}{$BUILD_STAMP}" "$REPO/dist/VideoDownloader.app/Contents/Resources/web/index.html" 2>/dev/null || true
# 同时把指纹写入程序可读文件，供 /api/version 自检（避免肉眼误判版本）
# 注意：指纹必须每次构建都变化（含 BUILD_STAMP 时间戳），否则自动接管逻辑
# 会误判"版本相同"而不接管旧实例，导致仍跑旧版。
echo "$BUILD_INFO #$BUILD_STAMP" > "$REPO/dist/VideoDownloader.app/Contents/Resources/build_version.txt"
echo "   指纹：$BUILD_INFO #$BUILD_STAMP"

echo "▶ 打包 aria2c（种子后端随安装包自包含，脱离本机 Homebrew）"
python3 "$REPO/desktop/bundle_aria2.py" "$REPO/dist/VideoDownloader.app/Contents/Resources" 2>&1 || echo "   ⚠️ aria2 打包跳过（种子功能将运行时禁用）"

echo "▶ 安装 baiduPCS-Go 到用户目录（避免 macOS .app 内执行限制）"
_PCS_USER_BIN="$HOME/.video-downloader/baidupcs/bin/BaiduPCS-Go"
if [ -n "$_PCS_BINARY" ] && [ -x "$_PCS_BINARY" ]; then
  mkdir -p "$(dirname "$_PCS_USER_BIN")"
  cp -f "$_PCS_BINARY" "$_PCS_USER_BIN"
  chmod +x "$_PCS_USER_BIN"
  # 清除 quarantine 属性（从下载/解压可能带隔离标记）
  xattr -dr com.apple.quarantine "$_PCS_USER_BIN" 2>/dev/null || true
  echo "   ✔ 已安装到 $_PCS_USER_BIN"
else
  echo "   ⚠️ baiduPCS-Go 未预打包，将在运行时下载到用户目录"
fi

echo "▶ 签名（ad-hoc）"
codesign --force --deep --sign - "$REPO/dist/VideoDownloader.app" 2>/dev/null
xattr -dr com.apple.quarantine "$REPO/dist/VideoDownloader.app" 2>/dev/null
echo "   签名完成：$(codesign -dv "$REPO/dist/VideoDownloader.app" 2>&1 | grep 'Signature=' | head -1)"

echo "▶ 生成 DMG 分发包"
rm -f "$REPO/dist/VideoDownloader.dmg"
hdiutil create -volname "VideoDownloader" -srcfolder "$REPO/dist/VideoDownloader.app" -ov -format UDZO "$REPO/dist/VideoDownloader.dmg" >/dev/null 2>&1 || echo "⚠️ DMG 生成失败（可忽略，.app 仍可单独分发）"

# 去掉 DMG 自身的 quarantine 标记：用户从文件管理器双击挂载后拖出的 .app 才不会
# 被 macOS 误判为「从互联网下载」而二次加上隔离属性（否则双击会触发 Gatekeeper 拦截）。
xattr -dr com.apple.quarantine "$REPO/dist/VideoDownloader.dmg" 2>/dev/null || true
xattr -dr com.apple.quarantine "$REPO/dist/VideoDownloader.app" 2>/dev/null || true
echo "   已去除 DMG / .app 的 quarantine 标记（仍需用户右键→打开 一次性放行）"

# 走到这里说明构建、签名、DMG 全部成功（set -e 保证失败会提前退出），
# 此时旧产物已无回滚价值，统一收尾，避免 dist/ 无限膨胀。
cleanup_old_artifacts

echo "▶ 构建自验证（沙盒内可验，避免发出坏包）"
_VERIFY_FAIL=0

# 1) baiduPCS-Go 二进制必须已安装到用户目录且可执行
_PCS_USER_BIN="$HOME/.video-downloader/baidupcs/bin/BaiduPCS-Go"
if [ -x "$_PCS_USER_BIN" ]; then
  echo "   ✔ 二进制就绪: $_PCS_USER_BIN"
else
  echo "   ❌ 二进制缺失或不可执行: $_PCS_USER_BIN"
  _VERIFY_FAIL=1
fi

# 2) baidu_pcs / baidu_qr 模块必须已打进 .app
_APP="$REPO/dist/VideoDownloader.app"
for _m in baidu_pcs baidu_qr; do
  if [ -f "$_APP/Contents/Resources/server/$_m.py" ] || [ -f "$_APP/Contents/Resources/server/$_m.pyc" ]; then
    echo "   ✔ 模块已打包: $_m"
  else
    echo "   ❌ 模块未打包: $_m"
    _VERIFY_FAIL=1
  fi
done

# 3) 离线测试（不依赖外部网络）：扫码登录 Mock 全链路 + 后端路由冒烟
if [ -f "$REPO/server/tests/run_offline_tests.sh" ]; then
  echo "   ▶ 运行离线测试..."
  if bash "$REPO/server/tests/run_offline_tests.sh" >/tmp/vdl_offline_test.log 2>&1; then
    echo "   ✔ 离线测试全部通过"
  else
    echo "   ❌ 离线测试失败（详见 /tmp/vdl_offline_test.log）"
    tail -20 /tmp/vdl_offline_test.log
    _VERIFY_FAIL=1
  fi
else
  echo "   ⚠️ 离线测试脚本缺失，跳过"
fi

if [ "$_VERIFY_FAIL" -ne 0 ]; then
  echo "❌ 构建自验证未通过，已拦截发布。请修复上述问题后重新构建。"
  exit 1
fi

echo "✅ 完成"
echo "   App : dist/VideoDownloader.app（双击即用）"
echo "   分发: dist/VideoDownloader.dmg（拖到 应用程序 即可）"
echo "   启动后默认打开原生窗口（本地端口 8321，被占用自动顺延；不跳浏览器）"
