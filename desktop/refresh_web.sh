#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# refresh_web.sh —— 「纯前端改动」快通道：只改 web/ 时不必跑 40 分钟的 PyInstaller
#
# 为什么可以这么做（有据可查）：
#   1) web/ 是通过 PyInstaller `--add-data "$REPO/web:web"` 打进去的**静态文件**，
#      不是编译产物；冻结态后端只从 `Contents/Resources/web` 提供前端
#      （见 server/app.py:92-100：`_resources = MacOS/../Resources`，存在则 WEB_DIR=Resources/web）。
#   2) 所以纯前端改动 = 换掉那几个文件 + 重新 ad-hoc 签名（否则原地改包会让签名失效、无法启动）。
#   3) 重活（解析/冻结 3 万个文件、逐文件签名）完全省掉 → 约 2 分钟 vs 约 40 分钟。
#
# ⚠️ 边界：只适用于 web/ 目录。改了 server/*.py、依赖、版本号、打包配置 → 仍然必须
#    走 `bash desktop/build_mac.sh` 全量构建（那些是被冻结进二进制的）。
#
# 用法：
#   bash desktop/refresh_web.sh              # 同步 + 重签名（产物留在 dist/）
#   bash desktop/refresh_web.sh --deploy     # 同步 + 重签名 + 部署到 /Applications 并自校验
#   VDL_REFRESH_APP=<某份 app> bash desktop/refresh_web.sh    # 指定要刷新的 app（默认 dist）
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="${VDL_REFRESH_APP:-$REPO/dist/VideoDownloader.app}"
DEPLOY=0
[ "${1:-}" = "--deploy" ] && DEPLOY=1

BACKUP_DIR="${VDL_BACKUP_DIR:-$HOME/.vdl_backups}"
mkdir -p "$BACKUP_DIR"

die() { echo "❌ $1" >&2; exit 1; }
step() { echo "▶ $1"; }

T0="$(date +%s)"
elapsed() { echo "   (已用 $(( $(date +%s) - T0 ))s)"; }

# ── 0) 前置检查 ────────────────────────────────────────────────────────────
[ -d "$REPO/web" ] || die "找不到 $REPO/web"
[ -d "$APP" ] || die "找不到 app：$APP
   若 dist/ 里没有 app，请先跑一次全量构建：bash desktop/build_mac.sh"
[ -d "$APP/Contents/Resources/web" ] || die "$APP 里没有 Contents/Resources/web，不像本项目的 .app"

if [ ! -f "$REPO/web/index.html" ]; then die "web/index.html 缺失"; fi
grep -q "var fp = '__BUILD_FP__';" "$REPO/web/index.html" \
  || die "web/index.html 里没有 var fp = '__BUILD_FP__'; 占位符（构建注入靠它做缓存击穿），请检查前端源文件"

# ── 1) JS 语法门禁（防止把语法错误的前端封进包）────────────────────────────
step "JS 语法门禁（node --check web/*.js）"
NODE="$(command -v node 2>/dev/null || true)"
if [ -z "$NODE" ] && [ -d "$HOME/.workbuddy/binaries/node/versions" ]; then
  for d in $(ls -1 "$HOME/.workbuddy/binaries/node/versions" 2>/dev/null | sort -V); do
    cand="$HOME/.workbuddy/binaries/node/versions/$d/bin/node"
    [ -x "$cand" ] && NODE="$cand" && break
  done
fi
if [ -n "$NODE" ]; then
  for f in "$REPO"/web/*.js; do
    [ -f "$f" ] || continue
    "$NODE" --check "$f" || die "JS 语法错误：$f"
  done
  echo "   ✔ 通过"
else
  echo "   ⚠️  未找到 node，跳过语法门禁"
fi

# ── 2) 备份将被替换的 web（可回滚）──────────────────────────────────────────
step "备份旧前端到 $BACKUP_DIR"
BK="$BACKUP_DIR/web.before-refresh-$(date +%Y%m%d-%H%M%S)"
ditto "$APP/Contents/Resources/web" "$BK" 2>/dev/null || true
echo "   $BK"
# 只留最近 3 份
ls -dt "$BACKUP_DIR"/web.before-refresh-* 2>/dev/null | tail -n +4 | while IFS= read -r old; do
  mv "$old" "$HOME/.Trash/$(basename "$old")" 2>/dev/null || true
done

# ── 3) 同步 web/ 到两个落点 ────────────────────────────────────────────────
# Resources/web 是冻结态真正对外提供服务的目录；Frameworks/web 是 PyInstaller 的另一份拷贝，
# 一起同步以免两处内容不一致造成困惑（它不参与服务）。
step "同步 web/ 进 app"
for sub in Resources Frameworks; do
  d="$APP/Contents/$sub/web"
  [ -d "$d" ] || continue
  ditto "$REPO/web" "$d"
  echo "   → $sub/web 已同步（$(ls -1 "$d" | wc -l | tr -d ' ') 项）"
done

# ── 4) 重注入构建戳（缓存击穿 + 页脚指纹 + 自动接管用指纹）─────────────────
step "重注入构建指纹"
BUILD_HASH="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"
BUILD_DATE="$(git -C "$REPO" log -1 --format='%cd' --date=format:'%m-%d %H:%M' 2>/dev/null || echo '?')"
APP_VERSION="$(cat "$REPO/VERSION" 2>/dev/null | tr -d '[:space:]' | head -1)"
[ -z "$APP_VERSION" ] && APP_VERSION="0.0.0"
BUILD_STAMP="$(date +%y%m%d%H%M%S)"
BUILD_INFO="v$APP_VERSION · 前端热更 $BUILD_HASH @ $BUILD_DATE"

# var fp 在 index.html 里出现 2 处（styles.css 缓存击穿 + isDesktopBuild 判定），必须 /g
for sub in Resources Frameworks; do
  IDX="$APP/Contents/$sub/web/index.html"
  [ -f "$IDX" ] || continue
  perl -0pi -e "s{var fp = '__BUILD_FP__';}{var fp = '$BUILD_STAMP';}g" "$IDX"
  perl -0pi -e "s{<span id=\"buildTag\" class=\"build-tag\">.*?</span>}{<span id=\"buildTag\" class=\"build-tag\">$BUILD_INFO</span>}" "$IDX" 2>/dev/null || true
done
# build_version.txt 必须每次变化：自动接管逻辑靠它判断"本进程是新版"，不变会拒绝接管旧实例
echo "$BUILD_INFO #$BUILD_STAMP" > "$APP/Contents/Resources/build_version.txt"
echo "   指纹：$BUILD_INFO #$BUILD_STAMP"
echo "   版本：${APP_VERSION}（语义化版本不变，只动前端）"
elapsed

# ── 5) 重新 ad-hoc 签名（原地改包后必须重签，否则 macOS 拒绝启动）──────────
step "重新 ad-hoc 签名"
codesign --force --deep --sign - "$APP" 2>/dev/null || die "签名失败"
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true
if codesign --verify "$APP" 2>/dev/null; then
  echo "   ✔ codesign --verify 通过"
else
  die "codesign --verify 失败（包不完整）"
fi
elapsed

# ── 6) 可选：部署到 /Applications ──────────────────────────────────────────
if [ "$DEPLOY" != "1" ]; then
  echo "✅ 前端已刷新到：$APP"
  echo "   部署到 /Applications：再跑一次并加 --deploy"
  exit 0
fi

TARGET="${VDL_DEPLOY_TARGET:-/Applications/视频工坊.app}"
EXE="$TARGET/Contents/MacOS/视频工坊"
step "部署到 $TARGET"
osascript -e 'quit app "视频工坊"' 2>/dev/null || true
for _ in $(seq 1 15); do
  pgrep -f "视频工坊.app/Contents/MacOS/视频工坊" >/dev/null || break
  sleep 1
done
pkill -9 -f "视频工坊.app/Contents/MacOS/视频工坊" 2>/dev/null || true
sleep 1

if [ -e "$TARGET" ]; then
  BKA="$BACKUP_DIR/app-before-refresh-$(date +%Y%m%d-%H%M%S)"
  mv "$TARGET" "$BKA" || die "备份旧 app 失败"
  echo "   旧版本已备份：$BKA"
fi
# 经 /tmp 中转 + rename 换入：/Applications 逐文件写入会被 macOS 保护（且会留下 AppleDouble 边车）
STAGE="/tmp/VDL_refresh_$$.app"
ditto "$APP" "$STAGE" || die "中转复制失败"
mv "$STAGE" "$TARGET" || die "换入 /Applications 失败（可能需要手动授权）"
xattr -dr com.apple.quarantine "$TARGET" 2>/dev/null || true
echo "   已安装：$TARGET"

step "校验安装产物"
[ "$(cat "$TARGET/Contents/Resources/version.txt" 2>/dev/null)" = "$APP_VERSION" ] || die "安装后 version.txt 不符"
codesign --verify "$TARGET" 2>/dev/null || die "安装后签名校验失败"
NEWSTAMP="$(cat "$TARGET/Contents/Resources/build_version.txt" 2>/dev/null)"
echo "   $NEWSTAMP"
case "$NEWSTAMP" in *"#$BUILD_STAMP"*) echo "   ✔ 指纹一致（就是本次刷新）";; *) die "指纹不一致：$NEWSTAMP";; esac
elapsed

echo "✅ 完成。启动 App 即可看到新前端（版本号仍是 v${APP_VERSION}，前端已是最新）。"
echo "   注意：前端是热更进去的，语义化版本没变；下一轮正式发版仍要跑全量 build_mac.sh。"
