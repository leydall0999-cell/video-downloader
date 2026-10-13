#!/bin/bash
# 把火山凭据写进服务器金库（0600），随后立刻自检。
#
# 用法（在服务器上跑）：
#   bash /opt/vdl-gateway/set_mat_credential.sh mediakit <API_Key>
#   bash /opt/vdl-gateway/set_mat_credential.sh visual <AccessKey> <SecretKey>
#
# 安全约定：
#   · 只接收命令行参数，**不回显任何密钥字符**（只打印长度）；
#   · 写文件用 umask 077 + chmod 600，先写 .tmp 再 mv 原子替换；
#   · 覆盖前自动备份为 matting.json.bak-<时间戳>（保留最近 5 份）；
#   · 写完立刻重启网关并调 verify_cv.py 自检。
set -u
F="${VDL_MAT_FILE:-/opt/vdl-gateway/matting.json}"
MODE="${1:-}"
K1="${2:-}"
K2="${3:-}"

die() { echo "❌ $*"; exit 1; }
len() { printf '%s' "${1:-}" | wc -c | tr -d ' '; }

[ -n "$MODE" ] || die "用法: $0 {mediakit|visual} <key> [secret]   （当前未提供模式）"

case "$MODE" in
  mediakit)
    [ -n "$K1" ] || die "缺少 API Key。用法: $0 mediakit <API_Key>"
    [ "$(len "$K1")" -ge 8 ] || die "API Key 太短（$(len "$K1") 字符），疑似复制不全"
    ;;
  visual)
    [ -n "$K1" ] && [ -n "$K2" ] || die "缺少 AK/SK。用法: $0 visual <AccessKey> <SecretKey>"
    ;;
  *) die "未知模式: $MODE（只支持 mediakit / visual）" ;;
esac

python3 - "$F" "$MODE" "$K1" "$K2" <<'PY'
import json, os, sys, time
from pathlib import Path

path = Path(sys.argv[1]); mode, k1, k2 = sys.argv[2], sys.argv[3], sys.argv[4]
cfg = {}
if path.exists():
    try:
        cfg = json.loads(path.read_text())
    except Exception:
        cfg = {}
if mode == "mediakit":
    cfg["mediakit_api_key"] = k1
    print(f"  写入 mediakit_api_key（{len(k1)} 字符）")
else:
    cfg["ak"], cfg["sk"] = k1, k2
    print(f"  写入 ak（{len(k1)} 字符）/ sk（{len(k2)} 字符）")

path.parent.mkdir(parents=True, exist_ok=True)
if path.exists():
    bak = path.with_suffix(path.suffix + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
    bak.write_bytes(path.read_bytes())
    os.chmod(bak, 0o600)
    print(f"  已备份: {bak.name}")
    # 只保留最近 5 份备份
    baks = sorted(path.parent.glob("matting.json.bak-*"), reverse=True)
    for old in baks[5:]:
        old.unlink(missing_ok=True)

umask = os.umask(0o077)
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
os.chmod(tmp, 0o600)
os.replace(tmp, path)          # 原子替换
os.umask(umask)
os.chmod(path, 0o600)
print(f"  ✅ 已写入金库 {path}（权限 {oct(path.stat().st_mode)[-3:]}）")
PY
[ $? -eq 0 ] || die "写入金库失败"

echo ""
echo "=== 重启网关使新凭据生效 ==="
systemctl restart vdl-gateway 2>/dev/null && echo "  vdl-gateway 已重启" || echo "  ⚠️ 重启命令未生效（服务名可能不同，请手动确认）"
sleep 2
systemctl is-active vdl-gateway 2>/dev/null | sed 's/^/  状态: /'

echo ""
echo "=== 自检（只输出状态码，不打印任何密钥）==="
if [ -f /opt/vdl-gateway/verify_cv.py ]; then
  python3 /opt/vdl-gateway/verify_cv.py
  rc=$?
  echo "自检退出码=$rc  （0=可用 / 1=仍被拒 / 2=无凭据或网络异常）"
  exit $rc
else
  echo "  ⚠️ 未找到 verify_cv.py，跳过自检"
  exit 0
fi
