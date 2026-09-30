#!/usr/bin/env bash
# 公证 + 订书钉（Apple Gatekeeper 放行流程）
#
# 用法：
#   bash desktop/notarize.sh <VideoDownloader.app 或 VideoDownloader.dmg 路径>
#
# 前置条件（缺一不可）：
#   1. 目标已用 **Developer ID Application** 身份签名（带 --timestamp）；
#      ad-hoc 自签的包拿去公证只会失败 —— 公证的是「身份」，不是「文件」。
#   2. 钥匙串里存好了公证凭据，二选一：
#      a) keychain profile（推荐，一次存长期有效）
#         xcrun notarytool store-credentials "vdl-notary" \
#           --apple-id "<Apple ID>" --team-id "<TeamID>" --password "<app 专用密码>"
#         → VDL_NOTARY_PROFILE=vdl-notary
#      b) App Store Connect API key（CI 更稳，密码不落本机）
#         → VDL_NOTARY_KEY=<.p8 路径> VDL_NOTARY_KEY_ID=<KeyID> VDL_NOTARY_ISSUER=<IssuerUUID>
#
# ⚠️ 本机坑（2026-09-30 实测）：必须写 **xcrun stapler / xcrun notarytool**。
#    裸调 /usr/bin/stapler（xcode-select 的 shim）会报
#    "requires Xcode, but active developer directory is a command line tools instance"，
#    而走 xcrun 转发是正常的 —— 本机只有 CommandLineTools，没有 Xcode.app。
#
# 失败策略：任何一步失败都以非 0 退出，但**不删除产物**（还能当自用包继续用）。
set -u

TARGET="${1:-}"
if [ -z "$TARGET" ] || [ ! -e "$TARGET" ]; then
  echo "❌ 用法：bash desktop/notarize.sh <.app 或 .dmg 路径>"
  exit 1
fi

# ── 公证凭据 ────────────────────────────────────────────────
AUTH=()
if [ -n "${VDL_NOTARY_PROFILE:-}" ]; then
  AUTH=(--keychain-profile "$VDL_NOTARY_PROFILE")
  echo "   凭据来源：keychain profile「$VDL_NOTARY_PROFILE」"
elif [ -n "${VDL_NOTARY_KEY:-}" ] && [ -n "${VDL_NOTARY_KEY_ID:-}" ] && [ -n "${VDL_NOTARY_ISSUER:-}" ]; then
  AUTH=(--key "$VDL_NOTARY_KEY" --key-id "$VDL_NOTARY_KEY_ID" --issuer "$VDL_NOTARY_ISSUER")
  echo "   凭据来源：App Store Connect API key"
else
  echo "❌ 缺公证凭据。二选一："
  echo "   VDL_NOTARY_PROFILE=<profile 名>   （先跑 xcrun notarytool store-credentials）"
  echo "   VDL_NOTARY_KEY+VDL_NOTARY_KEY_ID+VDL_NOTARY_ISSUER  （.p8 API key）"
  exit 1
fi

# ── 打包待提交物 ────────────────────────────────────────────
# notarytool 支持直接交 .app，但官方推荐 zip / dmg（保持符号链接与资源叉）。
SUBMIT=""
ZIP=""
if [ -d "$TARGET" ]; then
  ZIP="$(dirname "$TARGET")/$(basename "$TARGET" .app)-notary.zip"
  rm -f "$ZIP" 2>/dev/null || true
  echo "   打包 $(basename "$TARGET") → $(basename "$ZIP")"
  /usr/bin/ditto -c -k --keepParent "$TARGET" "$ZIP" || { echo "❌ zip 打包失败"; exit 1; }
  SUBMIT="$ZIP"
else
  SUBMIT="$TARGET"
fi

# ── 提交公证（--wait 阻塞到 Apple 出结果，通常 1–3 分钟）──────
echo "▶ 提交 Apple 公证（等待扫描结果）"
SUBMIT_LOG="$(dirname "$TARGET")/_notary_submit.log"
xcrun notarytool submit "$SUBMIT" "${AUTH[@]}" --wait 2>&1 | tee "$SUBMIT_LOG"
if ! grep -q "status: Accepted" "$SUBMIT_LOG"; then
  echo "❌ 公证未通过。排查办法："
  echo "   xcrun notarytool log --keychain-profile \"${VDL_NOTARY_PROFILE:-}\" <submission-id>"
  echo "   常见原因：签名没带 --timestamp / entitlements 缺项 / 包内有未签名的嵌套可执行文件"
  exit 1
fi
echo "   ✔ Apple 已接受"

# ── 附订书钉（离线也能过 Gatekeeper）────────────────────────
echo "▶ 附加公证票（staple）"
if xcrun stapler staple "$TARGET" 2>&1 | tee -a "$SUBMIT_LOG" | grep -q "The staple and validate action worked"; then
  echo "   ✔ 票已附到 $(basename "$TARGET")"
else
  # stapler 对某些包名/路径偶发不同措辞，只要 exit 0 就以上面咨询为准
  if xcrun stapler validate "$TARGET" 2>&1 | grep -q "The validate action worked"; then
    echo "   ✔ 票已附到 $(basename "$TARGET")"
  else
    echo "⚠️  staple 失败（未联网也能放行，但首次打开会慢 / 需联网查票）"
    exit 1
  fi
fi

# ── 最终 Gatekeeper 评估（必须变成 accepted，否则等于白做）────
echo "▶ Gatekeeper 终评"
SPCTL_OUT="$(spctl -a -vv "$TARGET" 2>&1)"
echo "$SPCTL_OUT" | sed 's/^/   /'
if echo "$SPCTL_OUT" | grep -q "accepted"; then
  echo "   ✔ Gatekeeper 放行：任何人下载都不会再报「无法验证开发者 / 已损坏」"
else
  echo "❌ Gatekeeper 仍未放行，产物不可对外分发"
  exit 1
fi

[ -n "$ZIP" ] && rm -f "$ZIP" 2>/dev/null || true
exit 0
