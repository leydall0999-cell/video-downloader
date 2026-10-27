#!/usr/bin/env bash
# 三道闸「拦截能力」一键验证（2026-10-27，零副作用版）
#
# 为什么需要它：钩子接入后我们只验证过「放行」（Mac 真推过提交），但「拦截」
# 从没触发过 —— 而拦截才是这三道闸存在的理由。这个脚本造几个临时场景，
# 确认钩子真的会拦，全程不真推送、不污染真实分支/工作区：
#
#   场景A：本地落后远端 → ① 应拦
#   场景B：非发版机改 VERSION → ② 应拦
#   场景C：故意写个坏 .js → ③ 应拦
#
# 安全保证：所有改动都是「临时提交 + git reset --soft 回滚」或「临时 ref + 删除」，
# 跑完工作区与 git 状态完全复原。仍可加 VDL_SKIP_PUSH_GUARD=1 跳过任一阵。
#
# 用法（在仓库根目录执行）：bash tools/verify_hooks.sh

set -uo pipefail
export PYTHONIOENCODING=utf-8
export GIT_TERMINAL_PROMPT=0

PWD0="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$PWD0" || exit 1

# ── 找 python / node ──────────────────────────────────────────────────────
PY=""
if [ -n "${VDL_PYTHON:-}" ] && command -v "$VDL_PYTHON" >/dev/null 2>&1; then
  PY="$VDL_PYTHON"
else
  for c in python python3 py; do
    if command -v "$c" >/dev/null 2>&1; then PY="$( [ "$c" = py ] && echo 'py -3' || echo "$c" )"; break; fi
  done
fi
NODE=""
if [ -n "${VDL_NODE:-}" ] && command -v "$VDL_NODE" >/dev/null 2>&1; then
  NODE="$VDL_NODE"
else
  NODE="$(command -v node || command -v node.exe || true)"
fi
export VDL_PYTHON="$PY" VDL_NODE="$NODE"

RC=0
pass(){ echo "  ✅ $1"; }
fail(){ echo "  ❌ $1"; RC=1; }
skip(){ echo "  ⚪ $1"; }

echo ""
echo "══════════════ 第①道闸：落后远端拦截 ══════════════"
echo "（构造：本地分支 _verify_behind 落后，且缓存的 origin/_verify_behind 更新）"
git branch -f _verify_behind HEAD~2 >/dev/null 2>&1
git update-ref refs/remotes/origin/_verify_behind "$(git rev-parse HEAD)" 2>/dev/null
# 让 fetch 失败（远端没有这个分支），强制走「本地缓存」判断
OUT="$(GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=1 -o ProxyCommand=none" \
      git push --dry-run origin _verify_behind 2>&1)"
git branch -D _verify_behind >/dev/null 2>&1
git update-ref -d refs/remotes/origin/_verify_behind 2>/dev/null
if echo "$OUT" | grep -q "落后"; then
  pass "第①道闸拦下了「落后远端」"
  echo "$OUT" | sed 's/^/      /'
else
  fail "第①道闸没拦住（或 fetch 连上了远端、没触发缓存判断）"
  echo "$OUT" | tail -4 | sed 's/^/      /'
fi

echo ""
echo "══════════════ 第②道闸：非发版机改 VERSION 拦截 ══════════════"
HAD_RH="$(git config --bool --get vdl.releaseHost 2>/dev/null || echo __none__)"
git config vdl.releaseHost false
cp VERSION /tmp/_verify_ver.bak 2>/dev/null
printf '9.9.99\n' > VERSION
git add VERSION >/dev/null 2>&1
git commit -q -m "verify: temp version bump (auto-reset)" 2>/dev/null
OUT2="$(git push --dry-run origin "$(git rev-parse --abbrev-ref HEAD)" 2>&1)"
git reset --soft HEAD~1 >/dev/null 2>&1
git checkout HEAD -- VERSION >/dev/null 2>&1
git reset HEAD VERSION >/dev/null 2>&1
rm -f /tmp/_verify_ver.bak
if [ "$HAD_RH" = "__none__" ]; then git config --unset vdl.releaseHost 2>/dev/null; else git config vdl.releaseHost "$HAD_RH"; fi
if echo "$OUT2" | grep -q "发版文件\|vdl.releaseHost"; then
  pass "第②道闸拦下了「非发版机改 VERSION」"
  echo "$OUT2" | grep -A1 "发版文件" | sed 's/^/      /'
else
  fail "第②道闸没拦住（注意：本机若已设 vdl.releaseHost=true 则不应拦，属正常）"
  echo "$OUT2" | tail -4 | sed 's/^/      /'
fi

echo ""
echo "══════════════ 第③道闸：语法坏拦截 ══════════════"
cp web/app.js /tmp/_verify_app.bak 2>/dev/null
printf '\nfunction __verify_broken( {\n' >> web/app.js
git add web/app.js >/dev/null 2>&1
git commit -q -m "verify: temp broken js (auto-reset)" 2>/dev/null
OUT3="$(git push --dry-run origin "$(git rev-parse --abbrev-ref HEAD)" 2>&1)"
git reset --soft HEAD~1 >/dev/null 2>&1
git checkout HEAD -- web/app.js >/dev/null 2>&1
git reset HEAD web/app.js >/dev/null 2>&1
rm -f /tmp/_verify_app.bak
if echo "$OUT3" | grep -qi "语法校验没过\|有语法错误"; then
  pass "第③道闸拦下了「语法坏」"
else
  fail "第③道闸没拦住（可能本机没装 node，导致校验被 SKIP —— 设 VDL_NODE 再跑）"
  echo "$OUT3" | grep -i "node\|语法\|SKIP" | sed 's/^/      /'
fi

echo ""
echo "══════════════ 工作区复原检查 ══════════════"
if [ -z "$(git status --porcelain --untracked-files=no 2>/dev/null)" ]; then
  pass "工作区干净，验证过程零副作用"
else
  skip "工作区有残留（见下），请手动 git checkout / reset 复原："
  git status --porcelain | sed 's/^/      /'
fi

echo ""
if [ "$RC" -eq 0 ]; then echo "🎉 三道闸拦截能力全部验证通过"; else echo "⚠️ 有道闸没按预期拦截，见上"; fi
exit $RC
