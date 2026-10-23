#!/usr/bin/env bash
# 分步构建（本机 Git Bash 路径转换失效，故显式 cygpath -w）
set -euo pipefail
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
cd "$(dirname "$0")/.."
REPO="$(pwd)"
W() { cygpath -w "$1"; }
PYI="$REPO/.build_venv/Scripts/pyinstaller.exe"

echo "REPO=$REPO"
echo "pyinstaller: $PYI"
"$PYI" --version

echo "=== PYINSTALLER START $(date) ==="
"$PYI" \
  --name VideoDownloader \
  --windowed \
  --noconfirm \
  --clean \
  --icon "$(W "$REPO/desktop/icon.ico")" \
  --paths "$(W "$REPO/server")" \
  --paths "$(W "$REPO")" \
  --add-data "$(W "$REPO/web");web" \
  --add-data "$(W "$REPO/yt_dlp_plugins");yt_dlp_plugins" \
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
  --add-binary "$(W "$REPO/.build_venv/Scripts/deno.exe");bin" \
  --add-data "$(W "$REPO/.build_venv/Lib/site-packages/clr_loader/ffi/dlls");clr_loader/ffi/dlls" \
  "$(W "$REPO/desktop/desktop_launcher.py")"
echo "=== PYINSTALLER EXIT=$? $(date) ==="
