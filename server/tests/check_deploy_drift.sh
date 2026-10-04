#!/usr/bin/env bash
# 报告 cn / hk 两个部署面与本机仓库的文件漂移（**只报告，不同步**）
#
# 背景：2026-10-04 在 cn 上修好的安全修复（activate 超管门禁）因 hk 未同步而
# 在 hk 上被实测利用。hz 这类「另一个部署面」漂移不会自己暴露，需要显式比对。
#
# 用法：
#   bash server/tests/check_deploy_drift.sh              # 比文件指纹 + 跑门禁实测
#   bash server/tests/check_deploy_drift.sh --files-only # 只比文件指纹（不注册账号）
#
# ⚠️ hk 只能从 cn 两跳；hk 落位必须用脚本文件法（见 DEPLOY_TARGETS.md）。
set -u

CN_HOST="root@8.138.223.3"
CN_DIR="/opt/vdl-worker"
HK_HOST="47.82.101.79"
HK_DIR="/opt/vdl"

FILES=(
  "web/index.html"
  "web/app.js"
  "web/styles.css"
  "server/membership.py"
  "server/routers/membership.py"
)

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FILES_ONLY=0
[ "${1:-}" = "--files-only" ] && FILES_ONLY=1

# macOS 没有 sha256sum（只有 shasum -a 256），远端是 Linux 才有
if command -v sha256sum >/dev/null 2>&1; then
  HASH="sha256sum"
elif command -v shasum >/dev/null 2>&1; then
  HASH="shasum -a 256"
else
  echo "❌ 找不到 sha256sum 或 shasum"; exit 2
fi
local_hash() { $HASH "$1" | awk '{print $1}'; }

echo "══ 1) 本机仓库指纹（$HASH）══════════════════════════"
for f in "${FILES[@]}"; do
  if [ -f "$REPO/$f" ]; then
    printf "  %-32s %s\n" "$f" "$(local_hash "$REPO/$f")"
  else
    printf "  %-32s (本机无此文件)\n" "$f"
  fi
done

echo
echo "══ 2) cn ($CN_HOST) ══════════════════════════════════"
CN_OUT=$(ssh -n -o ConnectTimeout=20 -o StrictHostKeyChecking=no "$CN_HOST" \
  "cd $CN_DIR && sha256sum ${FILES[*]} 2>/dev/null" 2>/dev/null)
if [ -z "$CN_OUT" ]; then
  echo "  ❌ 连不上或路径不存在（$CN_DIR）"
else
  echo "$CN_OUT" | sed 's/^/  /'
fi

echo
echo "══ 3) hk ($HK_HOST，两跳) ═════════════════════════════"
HK_OUT=$(ssh -n -o ConnectTimeout=20 -o StrictHostKeyChecking=no "$CN_HOST" \
  "scp -o StrictHostKeyChecking=no -q -o ConnectTimeout=20 /dev/null root@$HK_HOST:/dev/null 2>/dev/null; \
   ssh -n -o StrictHostKeyChecking=no -o ConnectTimeout=20 root@$HK_HOST 'cd $HK_DIR && sha256sum ${FILES[*]} 2>/dev/null'" 2>/dev/null)
if [ -z "$HK_OUT" ]; then
  echo "  ❌ 连不上（需从 cn 两跳：先 ssh cn，再 ssh hk）"
else
  echo "$HK_OUT" | sed 's/^/  /'
fi

echo
echo "══ 4) 差异判定 ══════════════════════════════════════"
DIFF=0
python3 - "$REPO" "$CN_OUT" "$HK_OUT" "${FILES[@]}" <<'PY'
import hashlib, pathlib, sys
repo = pathlib.Path(sys.argv[1]); cn_out = sys.argv[2]; hk_out = sys.argv[3]
files = sys.argv[4:]

def parse(blob):
    d = {}
    for line in blob.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            d[parts[1].strip()] = parts[0]
    return d

cn, hk = parse(cn_out), parse(hk_out)
bad = 0
for f in files:
    p = repo / f
    local = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else '(缺)'
    c = cn.get(f, '(缺)'); h = hk.get(f, '(缺)')
    tags = []
    if c != local: tags.append('cn≠本机')
    if h != local: tags.append('hk≠本机')
    if tags:
        bad += 1
        print(f"  ⚠️  {f:32} {' / '.join(tags)}")
        print(f"        本机 {local[:16]}…")
        print(f"        cn  {c[:16]}…")
        print(f"        hk  {h[:16]}…")
    else:
        print(f"  ✅ {f:32} 三端一致")
print()
if bad:
    print(f"❌ {bad} 个文件存在漂移。安全修复必须两端都下发（见 DEPLOY_TARGETS.md）。")
else:
    print("✅ 三端文件一致")
sys.exit(1 if bad else 0)
PY
DRIFT=$?

if [ "$FILES_ONLY" = "1" ]; then
  exit $DRIFT
fi

echo
echo "══ 5) 激活码门禁实测（两台各跑一次）═══════════════════"
cat > /tmp/_drift_gate.sh <<'GATE'
set -e
PORT=$1
NAME=$2
T="drift_${NAME}_$(date +%s)@self.test"
# 关键：必须注册【两个】账号 —— 该机第一个注册账号会被 ensure_superusers
# 提升为超管（无 VDL_ADMIN_IDENTIFIER 名单时），用它测门禁会得到假绿。
A=$(curl -s -m 25 -X POST "http://127.0.0.1:$PORT/api/auth/register" \
      -H 'Content-Type: application/json' -d "{\"identifier\":\"boss_${T}\",\"password\":\"driftpass123\"}")
B=$(curl -s -m 25 -X POST "http://127.0.0.1:$PORT/api/auth/register" \
      -H 'Content-Type: application/json' -d "{\"identifier\":\"u_${T}\",\"password\":\"driftpass123\"}")
TOK=$(printf '%s' "$B" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("token",""))' 2>/dev/null || true)
if [ -z "$TOK" ]; then
  echo "  [$NAME] 注册失败，跳过实测：${B:0:120}"
else
  ISADMIN=$(printf '%s' "$B" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("is_admin"))')
  R=$(curl -s -m 20 -X POST "http://127.0.0.1:$PORT/api/member/activate" \
      -H 'Content-Type: application/json' -H "Authorization: Bearer $TOK" -d '{"code":"download_year"}')
  echo "  [$NAME] 第二个账号 is_admin=$ISADMIN"
  echo "  [$NAME] activate → $R"
  case "$R" in
    *USE_REDEEM*) echo "  ✅ [$NAME] 门禁在位（普通用户被拒）" ;;
    *)            echo "  ❌ [$NAME] 门禁缺失！任意登录用户可白嫖会员" ;;
  esac
fi
# 清理
python3 - "$T" <<'CLN'
import json, pathlib, glob, sys
tag = sys.argv[1]
p = pathlib.Path('/root/.video-downloader/users.json')
if p.is_file():
    d = json.loads(p.read_text()); b = len(d.get('users', []))
    d['users'] = [u for u in d.get('users', []) if tag not in u.get('identifier', '')]
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2))
    print(f"  [{tag[:12]}…] 清理 {b - len(d['users'])} 个测试账号")
for f in glob.glob('/root/.video-downloader/memberships/*.json'):
    try:
        if tag in pathlib.Path(f).read_text():
            pathlib.Path(f).unlink()
    except Exception:
        pass
CLN
GATE

echo "--- cn :18890 ---"
scp -o ConnectTimeout=20 -q /tmp/_drift_gate.sh "$CN_HOST":/tmp/ 2>/dev/null \
  && ssh -n -o ConnectTimeout=20 -o StrictHostKeyChecking=no "$CN_HOST" \
       "sed 's/PORT_HERE/18890/' /tmp/_drift_gate.sh > /tmp/_g1.sh && bash /tmp/_g1.sh 18890 cn" 2>&1 | sed 's/^/  /'

echo "--- hk :18080（两跳）---"
scp -o ConnectTimeout=20 -q /tmp/_drift_gate.sh "$CN_HOST":/tmp/ 2>/dev/null
ssh -n -o ConnectTimeout=25 -o StrictHostKeyChecking=no "$CN_HOST" \
  "scp -o StrictHostKeyChecking=no -q /tmp/_drift_gate.sh root@$HK_HOST:/tmp/ && \
   ssh -n -o StrictHostKeyChecking=no -o ConnectTimeout=25 root@$HK_HOST 'bash /tmp/_drift_gate.sh 18080 hk'" 2>&1 | sed 's/^/  /'

rm -f /tmp/_drift_gate.sh
ssh -n -o ConnectTimeout=20 -o StrictHostKeyChecking=no "$CN_HOST" 'rm -f /tmp/_drift_gate.sh /tmp/_g1.sh' 2>/dev/null || true
exit $DRIFT
