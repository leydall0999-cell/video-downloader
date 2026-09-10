#!/usr/bin/env bash
# publish_update.sh — 手工发布「自更新」版本到阿里云 VPS 更新源。
#
# ⚠️ 安全约定（用户明确要求）：构建（build_mac.sh）**绝不**自动发布更新。
# 自动发布会让「刚构建、尚未测试」的版本立刻对所有用户可见，风险极高。
# 正确流程：
#   1) bash desktop/build_mac.sh                      # 构建
#   2) ditto dist/VideoDownloader.app /Applications/  # 部署到本机
#   3) 在本机上充分测试，确认没有问题
#   4) bash desktop/publish_update.sh                 # 确认后手工发布
# 只有执行到第 4 步，用户端「检查更新」才会看到新版本。
#
# 用法：
#   bash desktop/publish_update.sh                    # 交互确认后发布
#   bash desktop/publish_update.sh --yes              # 跳过确认
#   bash desktop/publish_update.sh --force            # 允许发布不高于当前线上版本号的版本
#   VDL_RELEASE_NOTES="修复 XX 问题" bash desktop/publish_update.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$REPO/dist/VideoDownloader.app"
ZIP="$REPO/dist/VideoDownloader.app.zip"
LATEST_JSON="$REPO/dist/latest.json"

VPS_HOST="${VDL_UPDATE_HOST:-root@8.138.223.3}"
VPS_DIR="${VDL_UPDATE_DIR:-/opt/vdl-update}"
PUB_BASE="${VDL_UPDATE_BASE_URL:-http://8.138.223.3:8765}"
NOTES="${VDL_RELEASE_NOTES:-性能优化与问题修复}"

ASSUME_YES=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --yes|-y) ASSUME_YES=1 ;;
    --force)  FORCE=1 ;;
    *) echo "未知参数：${arg}（支持 --yes / --force）"; exit 2 ;;
  esac
done

# 版本比较：_ver_ge A B → A >= B 时返回 0，否则返回 1
_ver_ge() {
  python3 - "$1" "$2" <<'PY'
import sys
def p(v):
    n = []
    for x in v.split('.'):
        d = ''.join(c for c in x if c.isdigit())
        n.append(int(d) if d else 0)
    while len(n) < 3:
        n.append(0)
    return tuple(n[:3])
a, b = p(sys.argv[1]), p(sys.argv[2])
sys.exit(0 if a >= b else 1)
PY
}

echo "▶ 发布自更新版本到 VPS 更新源"
echo "   VPS      : $VPS_HOST:$VPS_DIR"
echo "   对外地址 : $PUB_BASE"

# 1) 前置检查
if [ ! -d "$APP" ]; then
  echo "❌ 未找到 $APP —— 请先执行 bash desktop/build_mac.sh 构建。"
  exit 1
fi
if [ ! -f "$REPO/VERSION" ]; then
  echo "❌ 未找到 $REPO/VERSION —— 无法确定版本号。"
  exit 1
fi
VERSION="$(tr -d '[:space:]' < "$REPO/VERSION")"
if [ -z "$VERSION" ]; then
  echo "❌ VERSION 文件为空。"
  exit 1
fi
echo "   本地版本 : v$VERSION"

# 2) 读取线上当前版本，防止误把旧版本发布成最新
REMOTE_VER="0.0.0"
REMOTE_URL=""
# 注意：ssh 必须加 -n，否则它会吃掉脚本的 stdin，导致后面「输入版本号确认」读到空而误判为取消。
if ssh -n -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$VPS_HOST" \
     "test -f '$VPS_DIR/latest.json' && cat '$VPS_DIR/latest.json'" 2>/dev/null >/tmp/vdl_remote_latest.json; then
  REMOTE_VER="$(python3 -c 'import json;print(json.load(open("/tmp/vdl_remote_latest.json")).get("version","0.0.0"))' 2>/dev/null || echo 0.0.0)"
  REMOTE_URL="$(python3 -c 'import json;print(json.load(open("/tmp/vdl_remote_latest.json")).get("url",""))' 2>/dev/null || echo)"
fi
echo "   线上版本 : v$REMOTE_VER"
if _ver_ge "$REMOTE_VER" "$VERSION"; then
  if [ "$FORCE" != "1" ]; then
    echo "❌ 线上版本 v$REMOTE_VER 已不低于待发布版本 v${VERSION}，拒绝发布。"
    echo "   请先把 $REPO/VERSION 改成更大的版本号；确需覆盖请加 --force。"
    exit 1
  fi
  echo "   ⚠️ 已指定 --force，强制覆盖线上 v$REMOTE_VER"
fi

# 3) 打包（ditto 保留 .app 的符号链接结构，解压后能直接运行）
#    注意：必须带 --keepParent，否则 ditto 会把 bundle 内容（Contents/…）平铺到 zip 根部，
#    客户端解压后拿不到 VideoDownloader.app/ 这一层，导致"更新准备失败"。
echo "▶ 打包 ${APP}（保留符号链接结构 + .app 外层目录）"
rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"
ZIP_SIZE="$(stat -f%z "$ZIP")"
ZIP_SHA="$(shasum -a 256 "$ZIP" | awk '{print $1}')"
echo "   更新包: $(du -h "$ZIP" | awk '{print $1}')  sha256=${ZIP_SHA:0:16}…"

# 3.5) 缓存本版「基线 app」目录（供生成文件级 delta 比对，不入版本库）
BASE_DIR="$REPO/desktop/.release_baselines"
NEW_BASE="$BASE_DIR/$VERSION/VideoDownloader.app"
mkdir -p "$(dirname "$NEW_BASE")"
# 用 ditto 覆盖（不先 rm -rf，避免触发沙箱批量删除保护而中断脚本）
if [ -d "$NEW_BASE" ]; then
  ditto "$APP" "$NEW_BASE"
else
  cp -R "$APP" "$NEW_BASE"
fi
echo "   本版基线 app 已缓存: $NEW_BASE"

# 3.6) 尝试生成文件级增量包（已装版本 == 线上版本 时，用户端只下几 MB 差分）
PATCH_BIN=""
PATCH_SHA=""
PATCH_SIZE=0
PATCH_URL=""
FROM_VERSION=""
BSDIFF_BIN="$REPO/desktop/tools/bsdiff"
[ -x "$BSDIFF_BIN" ] || BSDIFF_BIN="$(command -v bsdiff || echo bsdiff)"
if [ "$REMOTE_VER" != "0.0.0" ] && [ "$REMOTE_VER" != "$VERSION" ] && _ver_ge "$VERSION" "$REMOTE_VER"; then
  FROM_VERSION="$REMOTE_VER"
  OLD_BASE="$BASE_DIR/$FROM_VERSION/VideoDownloader.app"
  if [ ! -d "$OLD_BASE" ]; then
    echo "   未找到 $FROM_VERSION 本地基线，尝试从 VPS 下载上一版完整包以生成基线…"
    OLD_ZIP="/tmp/vdl_old_${FROM_VERSION}.zip"
    if [ -n "$REMOTE_URL" ] && curl -fsS --max-time 180 "$REMOTE_URL" -o "$OLD_ZIP" 2>/dev/null; then
      OLD_EXTRACT="/tmp/vdl_old_${FROM_VERSION}"
      rm -rf "$OLD_EXTRACT"; mkdir -p "$OLD_EXTRACT"
      if ditto -x -k "$OLD_ZIP" "$OLD_EXTRACT" 2>/dev/null && [ -d "$OLD_EXTRACT/VideoDownloader.app" ]; then
        mkdir -p "$(dirname "$OLD_BASE")"
        rm -rf "$OLD_BASE"; cp -R "$OLD_EXTRACT/VideoDownloader.app" "$OLD_BASE"
      fi
    fi
  fi
  if [ -d "$OLD_BASE" ]; then
    PATCH_BIN="$REPO/dist/patch-${FROM_VERSION}_${VERSION}.delta"
    if python3 "$REPO/desktop/tools/make_delta.py" "$OLD_BASE" "$NEW_BASE" "$PATCH_BIN" "$BSDIFF_BIN" 2>/dev/null; then
      PATCH_SIZE="$(stat -f%z "$PATCH_BIN")"
      PATCH_SHA="$(shasum -a 256 "$PATCH_BIN" | awk '{print $1}')"
      PATCH_URL="$PUB_BASE/patch-${FROM_VERSION}_${VERSION}.delta"
      echo "   增量更新包: $(du -h "$PATCH_BIN" | awk '{print $1}')  sha256=${PATCH_SHA:0:16}…"
    else
      PATCH_BIN=""; FROM_VERSION=""
    fi
  else
    echo "   ⚠️ 无法获取 $FROM_VERSION 基线，本次仅发布全量更新（用户端自动回退全量）。"
    FROM_VERSION=""
  fi
else
  echo "   首次发布或强制覆盖同版本：仅生成全量更新。"
fi

# 4) 生成 latest.json（发布清单）
PUB_AT="$(date +%Y-%m-%d)"
cat > "$LATEST_JSON" <<JSON
{
  "version": "$VERSION",
  "notes": "$NOTES",
  "published_at": "$PUB_AT",
  "url": "$PUB_BASE/VideoDownloader.app.zip",
  "size": $ZIP_SIZE,
  "sha256": "$ZIP_SHA",
  "from_version": "$FROM_VERSION",
  "patch_url": "$PATCH_URL",
  "patch_size": $PATCH_SIZE,
  "patch_sha256": "$PATCH_SHA"
}
JSON
cat "$LATEST_JSON"

# 5) 发布前人工确认（默认开启，避免误发未测试版本）
if [ "$ASSUME_YES" != "1" ]; then
  echo
  echo "⚠️  即将把 v$VERSION 发布为线上最新版本，所有用户的「检查更新」都会看到它。"
  printf "   确认已在本机测试通过？请输入版本号 %s 以继续（直接回车取消）: " "$VERSION"
  read -r confirm || confirm=""
  if [ "$confirm" != "$VERSION" ]; then
    echo "   已取消发布。"
    exit 0
  fi
fi

# 6) 上传
echo "▶ 上传到 $VPS_HOST:$VPS_DIR"
ssh -n -o StrictHostKeyChecking=no "$VPS_HOST" "mkdir -p '$VPS_DIR'"
scp -o StrictHostKeyChecking=no "$ZIP" "$VPS_HOST:$VPS_DIR/VideoDownloader.app.zip"
scp -o StrictHostKeyChecking=no "$LATEST_JSON" "$VPS_HOST:$VPS_DIR/latest.json"
if [ -n "$PATCH_BIN" ]; then
  scp -o StrictHostKeyChecking=no "$PATCH_BIN" "$VPS_HOST:$VPS_DIR/$(basename "$PATCH_BIN")"
  echo "   ✔ 已上传增量补丁 $(basename "$PATCH_BIN")"
fi
echo "   ✔ 已上传安装包与 latest.json"

# 7) 校验对外可访问性（外部可达需阿里云安全组放行对应端口）
echo "▶ 校验对外可访问性"
if curl -fsS --max-time 8 "$PUB_BASE/latest.json" >/tmp/vdl_pub_check.json 2>/dev/null; then
  echo "   ✔ 可访问：$PUB_BASE/latest.json"
  python3 -c 'import json;d=json.load(open("/tmp/vdl_pub_check.json"));print("   线上版本:",d.get("version"),"| 说明:",d.get("notes"))' 2>/dev/null || true
else
  echo "   ⚠️ 当前环境访问 $PUB_BASE 失败（多半是阿里云安全组未放行该端口）。"
  echo "      文件已上传成功，放行后用户端即可正常检查到更新。"
fi

echo
echo "✅ 发布完成：v${VERSION}（更新说明：${NOTES}）"
