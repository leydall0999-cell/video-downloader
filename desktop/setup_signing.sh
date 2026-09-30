#!/usr/bin/env bash
# Developer ID 签名证书落地助手 —— 把「付费之后」的命令行步骤全部自动化。
#
#   子命令                                  作用
#   check                    诊断当前状态（签名身份 / 公证凭据 / Gatekeeper 结论）
#   csr <邮箱> <名称>        生成私钥 + CSR（私钥留本机钥匙串，CSR 上传 Apple）
#   import <证书.cer>        把 Apple 签发的证书 + 私钥导入钥匙串，codesign 可用
#   profile <名>             创建公证凭据（要输入 Apple ID + app 专用密码）
#
# 完整流程：
#   1) developer.apple.com 加入 Apple Developer Program（¥688/年）—— 网页操作
#   2) bash desktop/setup_signing.sh csr you@x.com "Your Name"
#   3) Apple 后台 Certificates → + → Developer ID Application → 上传那个 .certSigningRequest → Download
#   4) bash desktop/setup_signing.sh import ~/Downloads/developerID_application.cer
#   5) appleid.apple.com → 登录 → App 专用密码 → 生成一个（给 notary 用）
#   6) bash desktop/setup_signing.sh profile vdl-notary
#   7) bash desktop/notarize.sh dist/VideoDownloader.app   （或直接重跑 build_mac.sh，会自动公证）
set -u

KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"
WORKDIR="$HOME/.vdl-signing"

cmd_check() {
  echo "▸ 签名身份"
  security find-identity -v -p codesigning 2>/dev/null | sed 's/^/   /' || true
  echo "▸ Developer ID 身份"
  if DID=$(security find-identity -v -p codesigning 2>/dev/null | grep -m1 "Developer ID Application" | awk -F'"' '{print $2}') && [ -n "$DID" ]; then
    echo "   ✔ $DID  （构建时会自动用它签名 + 公证）"
  else
    echo "   ✘ 没有 → 构建只能 ad-hoc 自签，产物不可对外分发"
  fi
  echo "▸ 公证工具链"
  xcrun notarytool --version 2>/dev/null | sed 's/^/   notarytool /' || echo "   ✘ notarytool 不可用"
  echo "▸ 当前已装 App 的 Gatekeeper 结论"
  if [ -d /Applications/视频工坊.app ]; then
    spctl -a -vv /Applications/视频工坊.app 2>&1 | sed 's/^/   /'
  fi
}

cmd_csr() {
  local EMAIL="${1:-}" NAME="${2:-}"
  if [ -z "$EMAIL" ] || [ -z "$NAME" ]; then
    echo "❌ 用法：bash desktop/setup_signing.sh csr <邮箱> <名称>"
    exit 1
  fi
  mkdir -p "$WORKDIR"
  local KEY="$WORKDIR/developerID.key" CSR="$WORKDIR/VideoDownloader.certSigningRequest"
  echo "▶ 生成私钥 + CSR（→ $WORKDIR）"
  openssl req -new -newkey rsa:2048 -sha256 -nodes \
    -keyout "$KEY" -out "$CSR" \
    -subj "/emailAddress=$EMAIL/CN=$NAME/C=CN" 2>/dev/null \
    || { echo "❌ openssl 生成失败"; exit 1; }
  chmod 600 "$KEY"
  # 私钥先进钥匙串：Apple 回来的 cer 必须能与本机私钥配对，否则 identity 无效
  security import "$KEY" -k "$KEYCHAIN" -T /usr/bin/codesign >/dev/null 2>&1 \
    && echo "   ✔ 私钥已入钥匙串" || echo "⚠️  私钥导入失败（后续 import 会再试）"
  echo "   ✔ $CSR"
  echo "   下一步：Apple 后台 Certificates → + → Developer ID Application → 上传这个文件"
}

cmd_import() {
  local CER="${1:-}"
  [ -f "$CER" ] || { echo "❌ 用法：bash desktop/setup_signing.sh import <证书.cer>"; exit 1; }
  echo "▶ 导入证书 $CER"
  security import "$CER" -k "$KEYCHAIN" -T /usr/bin/codesign >/dev/null 2>&1 \
    || { echo "❌ 导入失败（私钥不匹配？确认 csr 和 cert 是同一对）"; exit 1; }
  echo "   ✔ 已导入，校验身份："
  cmd_check
}

cmd_profile() {
  local NAME="${1:-vdl-notary}"
  echo "▶ 创建公证凭据「$NAME」"
  printf "Apple ID："; read -r APPLEID
  printf "Team ID（10 位，见 Apple 后台 Membership）："; read -r TEAMID
  printf "App 专用密码（不是登录密码）："; read -rs APPPWD; echo
  [ -n "$APPLEID" ] && [ -n "$TEAMID" ] && [ -n "$APPPWD" ] || { echo "❌ 三项都不能为空"; exit 1; }
  xcrun notarytool store-credentials "$NAME" \
    --apple-id "$APPLEID" --team-id "$TEAMID" --password "$APPPWD" --validate 2>&1 | sed 's/^/   /'
  echo "   ✔ 之后构建会用到：VDL_NOTARY_PROFILE=$NAME"
}

case "${1:-}" in
  check)   cmd_check ;;
  csr)     cmd_csr "${2:-}" "${3:-}" ;;
  import)  cmd_import "${2:-}" ;;
  profile) cmd_profile "${2:-vdl-notary}" ;;
  *) echo "用法：bash desktop/setup_signing.sh {check|csr|import|profile}" ; exit 1 ;;
esac
