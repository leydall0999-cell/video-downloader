#!/usr/bin/env bash
# web_sync.sh — 网页版（web-dev）整树同步到国内 ECS(cn) + 香港节点(hk)
#
# 用法：
#   bash deploy/web_sync.sh            # 默认 dry-run（只列变更，不真改）
#   bash deploy/web_sync.sh apply      # 真同步
#
# 同步范围（务必含 extension —— 见下方 🔴 警告）：
#   server/  web/  extension/
# cn:  root@8.138.223.3:/opt/vdl-worker   (服务 vdl-web.service, 回源 18890)
# hk:  root@47.82.101.79:/opt/vdl          (服务 vdl-web,       回源 18080)
#
# 流程：① 本机 → cn（rsync 三目录）② ssh cn → hk（rsync 三目录，二跳）
#       ③ 每跳 --backup 留回滚点（不带 --delete，保留 hk 特有老文件）
#       ④ hk 上 py_compile 全部 .py → systemctl restart vdl-web → 轮询 18080 存活
#
# 🔴🔴 extension 必须同步（2026-10-17 教训）：浏览器扩展由 extension.py 每次请求现场从
#    <repo>/extension/ 打包；若整树漏了 extension/，服务器无此目录 ⇒ 扩展从冻结兜底
#    server/assets/extension_src/（旧到 1.0.33）出包。改这里目录列表务必带 extension。
set -euo pipefail

APPLY="${1:-dry}"

CN_HOST=root@8.138.223.3
HK_HOST=root@47.82.101.79
CN_DIR=/opt/vdl-worker
HK_DIR=/opt/vdl

# 仓库根（脚本放在 <repo>/deploy/ 下，上一级即 repo 根）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# 🔴 必须是这三个目录，缺一不可（server 接口 / web 页面 / extension 扩展源码）
DIRS=(server web extension)

EXCL=(--exclude '__pycache__' --exclude '*.pyc' --exclude '*.bak*' \
      --exclude '.pytest_cache' --exclude '.DS_Store')

SSH_COMMON="-o BatchMode=yes -o StrictHostKeyChecking=no"

if [[ "$APPLY" != "apply" ]]; then
  echo "═══ DRY-RUN 模式（列变更，不真改）。要真同步加参数: bash $0 apply ═══"
  RSYNC_DRY="--dry-run"
else
  echo "═══ APPLY 模式（真同步）═══"
  RSYNC_DRY=""
fi

ts=$(date +%Y%m%d-%H%M%S)

# ---------- ① 本机 → cn ----------
echo "==> [1/3] 本机 → cn ($CN_HOST:$CN_DIR)  [dirs: ${DIRS[*]}]"
for D in "${DIRS[@]}"; do
  echo "  rsync $D/"
  rsync -a $RSYNC_DRY --backup --backup-dir="$CN_DIR/backup/pre-sync-$ts/$D" \
    "${EXCL[@]}" -e "ssh $SSH_COMMON" \
    "$PROJECT_DIR/$D/" "$CN_HOST:$CN_DIR/$D/"
done

# ---------- ② cn → hk（二跳）----------
echo "==> [2/3] cn → hk ($HK_HOST:$HK_DIR)  [dirs: ${DIRS[*]}]"
if [[ "$APPLY" == "apply" ]]; then
  ssh $SSH_COMMON "$CN_HOST" "set -e
    BK='$HK_DIR/backup/pre-fullsync-$ts'
    mkdir -p \"\$BK\"
    for D in ${DIRS[*]}; do
      echo \"  rsync \$D/\"
      rsync -a --backup --backup-dir=\"\$BK/\$D\" ${EXCL[*]} -e 'ssh -o BatchMode=yes -o StrictHostKeyChecking=no' \
        \"$CN_DIR/\$D/\" \"$HK_HOST:$HK_DIR/\$D/\"
    done"
else
  # dry-run：只列一跳的变更（从本机直连 hk 估算，便于预览）
  for D in "${DIRS[@]}"; do
    echo "  (dry) rsync $D/ -> hk"
    rsync -a --dry-run "${EXCL[@]}" -e "ssh $SSH_COMMON" \
      "$PROJECT_DIR/$D/" "$HK_HOST:$HK_DIR/$D/" | head -20
  done
fi

# ---------- ③ 验证（仅 apply）----------
if [[ "$APPLY" == "apply" ]]; then
  # 🔴 2026-10-18 补：以前只重启 hk，**cn 不重启** ⇒ Python 进程仍跑旧代码。
  #    症状极具迷惑性：rsync 全部成功、hk 也起来了，但 cn 的
  #    /download/installer?p=windows 仍 302 到旧版本号（整树同步了却像没生效）。
  echo "==> [3/4] 重启 cn 并验证（回源 18890）"
  ssh $SSH_COMMON "$CN_HOST" "set -e
    find $CN_DIR/server -name '*.py' -print0 | xargs -0 python3 -m py_compile || { echo 'PY_COMPILE_FAIL_CN'; exit 1; }
    systemctl restart vdl-web
    for i in \$(seq 1 18); do sleep 5; code=\$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:18890/api/version || echo 000); echo \"  cn probe \$i: \$code\"; [ \"\$code\" = '200' ] && break; done"
  echo "==> [4/4] 验证 hk"
  ssh $SSH_COMMON "$HK_HOST" "set -e
    echo '--- py_compile server ---'
    find $HK_DIR/server -name '*.py' -print0 | xargs -0 python3 -m py_compile || { echo 'PY_COMPILE_FAIL'; exit 1; }
    systemctl restart vdl-web
    # 🔴 探测窗口必须 ≥3 分钟：hk 冷启动实测 **2 分 16 秒**（卡在 cookie_pool 自检），
    #    原来 6×2s=12s 必然全 000 ⇒ 误报「hk 502 挂了」，其实进程正健康启动中。
    for i in \$(seq 1 40); do sleep 5; code=\$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:18080/api/version || echo 000); echo \"  probe \$i: \$code\"; [ \"\$code\" = '200' ] && break; done
    echo '--- journalctl 最近异常 ---'
    journalctl -u vdl-web -n 30 --no-pager | grep -iE 'Traceback|ImportError|ModuleNotFound' || echo '  无 Traceback'"
  echo "--- 公网 hk 版本 ---"
  curl -s --max-time 10 https://hk.hanyuxz.top/api/version || echo "hk_pub_probe_fail"
  echo
  echo "✅ 同步完成。cn/hk 的 <repo>/extension/ 已就位（扩展从首选路径出包，不再回落旧兜底）。"
  echo "   若需回滚：cn/hk 的 backup/pre-sync-* / pre-fullsync-* 已留旧文件。"
else
  echo "═══ dry-run 结束。确认无误后执行: bash $0 apply ═══"
fi
