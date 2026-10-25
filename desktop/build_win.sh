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

# 🔴 Windows Python 默认标准流编码是 cp1252（GitHub runner 为 en-US 区域）：
#    图标生成 heredoc / bundle_aria2.py 里的中文 print 会直接
#    UnicodeEncodeError 崩进程（2026-10-16 CI 实测崩于此，定位花 6 轮）。
#    Python 3.7+ 支持 UTF-8 模式，强制标准流走 UTF-8；本脚本启动的所有
#    python 子进程（venv/pip/pyinstaller/heredoc/bundle_aria2）均继承。
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# 🔴 MSYS/Git Bash 参数路径转换的坑（2026-10-16 CI 实测）：
#    纯 POSIX 路径参数（argv[0]、--icon、--paths）MSYS 会正确转成 Windows 路径；
#    但带 `;` 分隔符的参数（--add-data "SRC;DEST" / --add-binary）MSYS 不当成
#    纯路径、原样透传，Windows 程序（pyinstaller/iscc）就把 `/d/a/...` 按当前盘
#    根目录解析成 `D:\d\a\...` ⇒ "Unable to find" 直接构建失败。
#    解法：所有要传给原生 exe 的路径一律先用本函数显式转 Windows 风格，
#    MSYS 对已是 Windows 风格的参数不做转换，两头确定。
_win() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"
  elif command -v wslpath >/dev/null 2>&1; then wslpath -w "$1"
  else printf '%s' "$1"; fi
}

PIP_INDEX="${PIP_INDEX:-https://mirrors.aliyun.com/pypi/simple/}"
VENV="$REPO/.build_venv"

echo "▶ 准备构建环境（独立 venv）"
if [[ ! -x "$VENV/Scripts/python.exe" ]]; then
  python -m venv "$VENV"
fi
# pythonnet：pywebview 的 winforms 后端依赖它（import clr；缺了它 Windows 端
# 只能回退浏览器打开，没有原生窗口）。其自带 PyInstaller hook（pyinstaller40
# entry point）会自动收 Python.Runtime.dll；clr_loader 的 ClrLoader.dll 不在
# hook 覆盖范围，下面显式 --add-data 补收。
"$VENV/Scripts/pip.exe" install --quiet --index-url "$PIP_INDEX" -r requirements.txt pyinstaller Pillow pywebview pythonnet

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
      # 🔴 本函数内的日志一律 `>&2`（与 build_mac.sh 同约定）：调用方是
      #    `COMMENTARY_STAGING=$(_stage_commentary ...)`，凡是走到 stdout 的东西
      #    都会被整体捕获进变量 ⇒ 变量变成多行 ⇒ 后续 grep/--add-data 静默失败。
      #    2026-10-16 CI 实证：本行漏了 `>&2`，Windows runner 上 msyh.ttc 必然存在
      #    必然触发，把 $COMMENTARY_STAGING 污染成两行，报「疑似误捆绑旧版」。
      [ -f "$_sys" ] && cp "$_sys" "$_fonts/" 2>/dev/null && echo "   ✔ 复制系统中文字体进包: $(basename "$_sys")" >&2 && break
    done
    ls "$_fonts"/*.tt[cf] >/dev/null 2>&1 || echo "   ⚠️ 包内无中文字体，字幕中文将依赖用户系统字体" >&2
  fi
  # 唯一允许走 stdout 的一行：函数用它把 staging 路径交回给调用方
  echo "$staging"
}

# 解说管线候选目录（按优先级；找不到 process.py 的候选一律跳过）
#   ① 显式指定 COMMENTARY_PIPELINE_DIR（CI / 本机都推荐）
#   ② 仓库同级  $REPO/../commentary-pipeline  —— 本机 app-dev 的 git worktree 布局
#   ③ 仓库内部  $REPO/commentary-pipeline      —— CI 里 actions/checkout(path: …) 的落点
# 🔴 曾经只认 ②，导致 CI 上 checkout 明明成功、脚本却看不见管线 → 静默出「无解说版」。
if [ -n "${COMMENTARY_PIPELINE_DIR:-}" ]; then
  COMMENTARY_DIR="$COMMENTARY_PIPELINE_DIR"
elif [ -f "$REPO/../commentary-pipeline/process.py" ]; then
  COMMENTARY_DIR="$REPO/../commentary-pipeline"
elif [ -f "$REPO/commentary-pipeline/process.py" ]; then
  COMMENTARY_DIR="$REPO/commentary-pipeline"
else
  COMMENTARY_DIR="$REPO/../commentary-pipeline"
fi
COMMENTARY_DATA=()
if [ -d "$COMMENTARY_DIR" ] && [ -f "$COMMENTARY_DIR/process.py" ]; then
  echo "▶ 捆绑解说管线(随包自包含): $COMMENTARY_DIR"
  "$VENV/Scripts/pip.exe" install --quiet --no-cache-dir --index-url "$PIP_INDEX" -r "$COMMENTARY_DIR/requirements.txt"
  COMMENTARY_STAGING=$(_stage_commentary "$COMMENTARY_DIR")
  # 🔴 诊断闸门（每次必打，一次就能定位这一类故障）：命令替换 `$(...)` 会把函数内
  #    所有 stdout 一并吞进来。staging 变量一旦变成多行（或 process.py 不在位），
  #    后面 grep / --add-data 就拿着不存在的路径静默失败，报错信息还会指向
  #    「疑似误捆绑旧版」这种南辕北辙的方向。2026-10-16 为此多花了两轮 CI。
  if [ -f "$COMMENTARY_STAGING/process.py" ] \
     && [ "$(printf '%s' "$COMMENTARY_STAGING" | wc -l | tr -d ' ')" = "0" ]; then
    echo "   ✔ staging 就位: $COMMENTARY_STAGING（process.py $(wc -c < "$COMMENTARY_STAGING/process.py" | tr -d ' ') 字节）"
  else
    echo "❌ staging 异常：_stage_commentary 的返回值不是干净的单个路径"
    echo "   捕获到的内容（换行以 | 表示）：$(printf '%s' "$COMMENTARY_STAGING" | tr '\n' '|')"
    echo "   该路径下的内容："
    ls -la "$COMMENTARY_STAGING" 2>&1 | head -10 || true
    exit 1
  fi
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
    --add-data "$(_win "$COMMENTARY_STAGING");commentary"
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
  _DENO_BINARY_ARGS=(--add-binary "$(_win "$VENV/Scripts/deno.exe");bin")
else
  echo "⚠️  未找到 $VENV/Scripts/deno.exe —— 包内将没有 JS runtime，YouTube 可能解不了 nsig 挑战" >&2
fi

# clr_loader 的原生 ClrLoader.dll：pythonnet 自带的 PyInstaller hook 只收
# pythonnet 包内的文件，不含 clr_loader；而 clr_loader 的 load_netfx() 用
# 「相对模块文件的显式路径」ffi.dlopen 它 —— PyInstaller 静态分析看不见，
# 不显式收进去，冻结后 import clr 必失败（Windows 端退化成浏览器打开）。
_CLRLOADER_DLLS="$VENV/Lib/site-packages/clr_loader/ffi/dlls"
_CLRLOADER_ARGS=()
if [ -d "$_CLRLOADER_DLLS" ]; then
  _CLRLOADER_ARGS=(--add-data "$(_win "$_CLRLOADER_DLLS");clr_loader/ffi/dlls")
else
  echo "⚠️  未找到 clr_loader/ffi/dlls（$_CLRLOADER_DLLS）——pythonnet 可能无法初始化" >&2
fi

# ⚠️ 不要再加 --collect-all onnx：它会把 onnx 官方的测试数据集（约 2.4 万个文件）
#    整个打进包，属纯死重（server/ 代码零 import onnx），拖慢构建并撑大安装包。
#    与 build_mac.sh 保持一致；真需要时 PyInstaller 的常规依赖分析会自动收进来。
"$VENV/Scripts/pyinstaller.exe" \
  --name VideoDownloader \
  --windowed \
  --noconfirm \
  --clean \
  --icon "$(_win "$ICON_ICO")" \
  --paths "$(_win "$REPO/server")" \
  --paths "$(_win "$REPO")" \
  --add-data "$(_win "$REPO/web");web" \
  --add-data "$(_win "$REPO/yt_dlp_plugins");yt_dlp_plugins" \
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
  --hidden-import clr \
  --collect-all pythonnet \
  --collect-all webview \
  "${COMMENTARY_DATA[@]}" \
  ${_DENO_BINARY_ARGS[@]+"${_DENO_BINARY_ARGS[@]}"} \
  ${_CLRLOADER_ARGS[@]+"${_CLRLOADER_ARGS[@]}"} \
  "$(_win "$REPO/desktop/desktop_launcher.py")"

# 清理 staging 临时目录（Windows 无 ~/.Trash：mv 失败则就地删除，CI 上无副作用）
if [ -n "${COMMENTARY_STAGING:-}" ] && [ -d "$COMMENTARY_STAGING" ]; then
  mv "$COMMENTARY_STAGING" "$HOME/.Trash/commentary_staging_$(date +%s)" 2>/dev/null \
    || rm -rf "$COMMENTARY_STAGING" 2>/dev/null || true
fi

# 🔴 Chocolatey shim 陷阱（2026-10-26 真机实测，整轮构建被拦在这里）：
#    CI 镜像里 PATH 上的 ffmpeg 是 C:\ProgramData\chocolatey\bin\ffmpeg.exe ——
#    它**不是真程序**，是 Chocolatey 的转发器（shim），靠相对路径
#    ..\lib\ffmpeg\tools\ffmpeg\bin\ffmpeg.exe 找真身。原来的逻辑直接
#    `cp "$(command -v ffmpeg)"` 把 shim 拷进包 ⇒ 相对路径在包里失效 ⇒
#    包内 ffmpeg.exe 执行报 "Cannot find file at '..\lib\ffmpeg\...'" ⇒
#    构建守门（实跑 --version）拦下 ⇒ 整轮中止、根本拿不到安装包。
#
#    所以拷贝前必须：① 解析到真身（排除 choco shim 目录）② 实跑 -version 验证。
#    只信「能跑出版本号的那个文件」，不信 PATH 上先撞见谁。
_resolve_real_bin() {
  local name="$1" explicit="$2" cand found
  for cand in "$explicit" "$(command -v "$name" 2>/dev/null)" "$REPO/$name.exe"; do
    [[ -n "$cand" && -f "$cand" ]] || continue
    # choco 的 bin 目录里全是 shim：原地能跑，拷进包必废 —— 直接排除
    case "$cand" in
      */chocolatey/bin/*|*"/chocolatey/bin/"*|*"\\chocolatey\\bin\\"*) continue ;;
    esac
    if "$cand" -version >/dev/null 2>&1; then
      echo "$cand"; return 0
    fi
  done
  # 兜底：到 Chocolatey 的 lib 目录里找真身（不限包内目录结构，深度限制避免扫全盘）
  if [[ -d /c/ProgramData/chocolatey/lib ]]; then
    found="$(find /c/ProgramData/chocolatey/lib -maxdepth 6 -type f -name "$name.exe" 2>/dev/null | head -1)"
    if [[ -n "$found" ]] && "$found" -version >/dev/null 2>&1; then
      echo "$found"; return 0
    fi
  fi
  return 1
}

echo "▶ 捆绑 ffmpeg / ffprobe（放入 bin/ 子目录，启动器会优先用捆绑版本）"
if ! FF="$(_resolve_real_bin ffmpeg "${VDL_FFMPEG_BIN:-}")"; then
  echo "⚠️  未找到可用的 ffmpeg（PATH 里若只有 Chocolatey shim 不算）：" >&2
  echo "    请先装静态版 ffmpeg，或用 VDL_FFMPEG_BIN 显式指向真身 ffmpeg.exe" >&2
  exit 1
fi
echo "   ffmpeg  真身: $FF"
mkdir -p "$REPO/dist/VideoDownloader/bin"
cp "$FF" "$REPO/dist/VideoDownloader/bin/ffmpeg.exe"
# ffprobe 与 ffmpeg 同目录（Windows 上为 ffprobe.exe），解说管线重度依赖
if [[ -f "${FF%ffmpeg.exe}ffprobe.exe" ]]; then
  cp "${FF%ffmpeg.exe}ffprobe.exe" "$REPO/dist/VideoDownloader/bin/ffprobe.exe"
  echo "   ffprobe 真身: ${FF%ffmpeg.exe}ffprobe.exe（与 ffmpeg 同目录）"
elif FP="$(_resolve_real_bin ffprobe "${VDL_FFPROBE_BIN:-}")"; then
  cp "$FP" "$REPO/dist/VideoDownloader/bin/ffprobe.exe"
  echo "   ffprobe 真身: $FP"
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

# ── 必备二进制硬核查（2026-10-21）──
# 上面 deno / ffmpeg / ffprobe 段落找不到时只打一句 ⚠️ 就继续，历史形态下可能发出
# 「YouTube 高码率静默降级」的包而构建日志全绿。这里改成硬断言：缺任一项或跑不起来
# 就中止，绝不把残包交给 Inno 打安装程序（判据含 --version 冒烟执行，能抓动态库缺失）。
echo "▶ 核查包内必备二进制（deno / ffmpeg / ffprobe）"
if "$VENV/Scripts/python.exe" "$REPO/desktop/ci_assert_bundle_binaries.py" "$DIST_APP"; then
  echo "   ✔ 必备二进制齐备"
else
  echo "❌ 包内缺少可用二进制（deno / ffmpeg / ffprobe 之一），已中止打包。" >&2
  echo "   修法：① 确认 requirements.txt 的 deno==2.9.7 已装进 venv（$VENV/Scripts/deno.exe）；" >&2
  echo "         ② ffmpeg.exe / ffprobe.exe 需在 PATH 或仓库根目录；然后重跑构建。" >&2
  exit 1
fi

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
  # 简体中文语言包：Inno Setup 官方发行版不带（Languages 里没有 ChineseSimplified.isl），
  # 由 CI 预下载到 desktop/langs/。找不到就退化为英文向导，不阻断出包。
  _MESSAGES_FILE="compiler:Default.isl"
  for _isl in "$REPO/desktop/langs/ChineseSimplified.isl" \
              "/c/Program Files (x86)/Inno Setup 6/Languages/ChineseSimplified.isl" \
              "/c/Program Files/Inno Setup 6/Languages/ChineseSimplified.isl"; do
    if [ -f "$_isl" ]; then _MESSAGES_FILE="$(_win "$_isl")"; break; fi
  done
  if [ "$_MESSAGES_FILE" = "compiler:Default.isl" ]; then
    echo "   lang: 未找到简体中文语言包 → 英文安装向导（不影响功能）"
  else
    echo "   lang: $_MESSAGES_FILE"
  fi
  # 关掉 MSYS 参数转换后，可执行文件自身也得转成 Windows 路径（execve 的路径
  # 映射与参数转换是两套机制，不指望它；转了一劳永逸）。
  MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' "$(_win "$_ISCC")" \
    "/DAppVersion=${APP_VERSION}" \
    "/DOutputDir=$(_win "$REPO/dist")" \
    "/DOutputBaseFilename=VideoDownloader-Setup-${APP_VERSION}" \
    "/DSourceDir=$(_win "$DIST_APP")" \
    "/DIconFile=$(_win "$ICON_ICO")" \
    "/DMessagesFile=${_MESSAGES_FILE}" \
    "$(_win "$REPO/desktop/installer.iss")"
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
