#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy_mac.sh —— 构建产物的「自校验部署」
#
# 解决的历史坑：过去用 cp -Rf 覆盖一个【正在运行】的 app，旧进程仍占着内存里
# 的旧二进制，复制不可靠，导致用户实际跑的根本不是刚构建的版本，却一直以为
# 装好了。本脚本把「退出→复制→校验→启动→确认运行中」串成一条原子链路，任
# 一步不符都直接失败报错，绝不空口宣称成功。
#
# 用法：
#   bash desktop/deploy_mac.sh            # 构建后部署到 /Applications 并自校验
#   bash desktop/deploy_mac.sh --no-build # 仅部署（不重新构建，dist 已存在）
#   VDL_DEPLOY_TARGET=/path/to/app bash desktop/deploy_mac.sh
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$REPO/dist/VideoDownloader.app"
APP="${VDL_DEPLOY_TARGET:-/Applications/VideoDownloader.app}"
EXE="$APP/Contents/MacOS/VideoDownloader"

die() { echo "❌ $1" >&2; exit 1; }

# 把路径送入 ~/.Trash（与 build_mac.sh trash_path 同款，可从回收站找回）
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
  mv "$src" "$dest" 2>/dev/null && echo "   ♻️  移入回收站：$base" || echo "   ⚠️  移入回收站失败（未删除）：$src"
}

# 可选：先构建
if [ "${1:-}" != "--no-build" ]; then
  echo "▶ 重新构建（build_mac.sh）..."
  bash "$REPO/desktop/build_mac.sh" 2>&1 | grep -v "install_name_tool\|replacing existing signature" | tail -30 || die "构建失败"
fi

[ -d "$DIST" ] || die "dist 不存在，请先构建"
[ -f "$DIST/Contents/Resources/build_version.txt" ] || die "dist 未含 build_version.txt（构建脚本需更新）"

EXPECTED="$(cat "$DIST/Contents/Resources/build_version.txt")"
echo "目标版本: $EXPECTED"

# 1) 退出运行中的旧实例，确保进程真的死了（不死绝不复制）
echo "▶ 退出运行中的实例..."
osascript -e 'quit app "VideoDownloader"' 2>/dev/null || true
for _ in $(seq 1 30); do
  pgrep -f "$EXE" >/dev/null || break
  sleep 1
done
pkill -9 -f "$EXE" 2>/dev/null || true
sleep 2
pgrep -f "$EXE" >/dev/null && die "旧实例进程仍存活，无法安全部署（请手动检查 Activity Monitor）"

# 2) 先把旧 app 改名备份（万一新版本有问题可回退），备份最多保留 3 份，超出送 ~/.Trash。
#    用 mv 而不是 rm -rf + ditto 的好处：保留旧文件，且若 ditto 拷贝中途失败仍有完整旧版可用。
echo "▶ 备份旧版本 ..."
if [ -e "$APP" ]; then
  BK="$APP.backup-$(date +%Y%m%d-%H%M%S)"
  mv "$APP" "$BK" || die "备份旧 app 失败：$BK"
  echo "   旧版本已备份：$BK"
  # 封顶 3 份：按修改时间新→旧，跳过最新 3 份，把更早的送回收站
  ls -dt "$APP".backup-* 2>/dev/null | tail -n +4 | while IFS= read -r old; do
    trash_path "$old"
  done
else
  echo "   无旧版本，跳过备份"
fi
echo "▶ 复制新版本到 $APP ..."
ditto "$DIST" "$APP" || die "ditto 复制失败"

# 3) 校验安装产物指纹 == 构建指纹（文件级，避免复制中途损坏/旧残留）
GOT="$(cat "$APP/Contents/Resources/build_version.txt" 2>/dev/null || echo MISSING)"
[ "$GOT" = "$EXPECTED" ] || die "安装后指纹不匹配: 期望[$EXPECTED] 实际[$GOT]"

# 4) 启动
echo "▶ 启动应用..."
open "$APP" || die "启动失败"

# 5) 轮询 /api/version，直到「运行中版本 == 构建指纹」且「exe 路径 == 目标 app」
echo "▶ 验证运行中的服务（轮询 /api/version）..."
PORT=""
for _ in $(seq 1 50); do
  for p in $(seq 8321 8365); do
    if [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$p/api/version" 2>/dev/null)" = "200" ]; then
      PORT=$p; break
    fi
  done
  [ -n "$PORT" ] && break
  sleep 1
done
[ -n "$PORT" ] || die "服务未在 8321-8365 响应 /api/version（启动可能失败，看控制台）"

RESP="$(curl -s "http://127.0.0.1:$PORT/api/version")"
RV="$(printf '%s' "$RESP" | sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
REXE="$(printf '%s' "$RESP" | sed -n 's/.*"exe"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
echo "   运行中版本: $RV"
echo "   运行中 exe: $REXE"

[ "$RV" = "$EXPECTED" ] || die "运行中版本与构建不符: 期望[$EXPECTED] 实际[$RV]"
[ "$REXE" = "$EXE" ] || die "运行中的 exe 不是目标 app: $REXE"

echo "✔ 部署校验通过：运行中的实例就是刚构建的版本（$RV @ http://127.0.0.1:$PORT/）"
echo "   可执行文件: $EXE"
