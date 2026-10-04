// 会员页权益文案：不得出现「云端 / 算力 / AI / 本地」等实现口径字眼（2026-10-04 用户定档）
//
// 背景：这些词讲的是**架构与成本口径**（哪些算力跑在云端、哪些跑在本机），不是用户
// 视角的权益。用户看到「云端算力 200 次/日」只会困惑（我买的是会员，为什么分云端/本地）。
// 定档做法：在 membership.py 里用**显式隐藏名单** `_BENEFIT_HIDDEN` 过滤**输出**，
// 配额表与限流逻辑一概不动（不展示 ≠ 取消；抠图 8/日、云端算力 3/日仍是风控）。
//
// 本守卫钉四件事：
//   ① `download_benefits()` 的**渲染输出**里不得含这四个词（查输出，不是查源码模板——
//      模板保留着，靠名单过滤）；
//   ② 隐藏名单非空，且每一项都能在 `_BENEFIT_FROM_LIMITS` / `_BENEFIT_EXTRA` 里找到来源
//      （防止写了名单但对应条目根本不存在，那等于名单是摆设）；
//   ③ 🔴 **隐藏 ≠ 取消限流**：名单里每一条的具体配额数值必须原样存在。这条是变异测试
//      补上的——原先只断言「配额还在」，若有人连配额一起删了，断言会因「找不到」而跳过 ⇒ 假绿；
//   ④ `plans()` 返回的 benefits 与 `download_benefits()` 一致（前端购买中心读的是 plans）。
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const serverDir = join(repoRoot, 'server');
const PY = process.env.VDL_TEST_PY
  || '/Users/suixindelang/.workbuddy/binaries/python/versions/3.13.12/bin/python3';

// 直接问 Python 侧要真实渲染结果（别在 JS 里重实现一遍 membership 的逻辑，会漂移）
const probe = `
import os, sys, tempfile, json
os.environ["VDL_DATA_DIR"] = tempfile.mkdtemp(prefix="vdlbene_")
os.environ["VDL_CLOUD_LINK"] = "0"
os.environ["VDL_PLANS_CLOUD"] = "0"
sys.path.insert(0, ${JSON.stringify(serverDir)})
import membership as M
out = {
    "benefits": M.download_benefits(),
    "hidden": getattr(M, "_BENEFIT_HIDDEN", None),
    "from_limits": [k for k, _ in M._BENEFIT_FROM_LIMITS],
    "extra": [str(x.get("key")) for x in M._BENEFIT_EXTRA],
    "daily": M.DAILY_QUOTA_LIMITS,
    "free": M.FREE_DAILY_LIMITS,
    "feature_rows": [{"key": d.get("key"), "resource": d.get("resource"),
                      "member_limit": d.get("member_limit"),
                      "free_limit": d.get("free_limit")} for d in M.FEATURE_USAGE_DEFS],
}
store = M.MembershipStore()
out["plans_benefits"] = store.plans().get("download_member", {}).get("benefits") or []
print(json.dumps(out, ensure_ascii=False))
`;
const raw = execFileSync(PY, ['-c', probe], { encoding: 'utf8' });
const M = JSON.parse(raw);

// ── ① 渲染输出不得含实现口径字眼 ─────────────────────────────────────────────
const KEYWORDS = ['云端', '算力', 'AI', '本地'];
assert.ok(Array.isArray(M.benefits) && M.benefits.length > 0,
  'download_benefits() 应返回非空权益清单');
for (const b of M.benefits) {
  const text = String(b.text || '');
  const hit = KEYWORDS.filter(k => text.includes(k));
  assert.ok(hit.length === 0,
    `权益文案不得含实现口径词，但 key="${b.key}" 命中 ${JSON.stringify(hit)}：${text}`);
}

// ── ② 隐藏名单非空且每项都有来源 ─────────────────────────────────────────────
assert.ok(M.hidden && typeof M.hidden === 'object' && Object.keys(M.hidden).length > 0,
  'membership.py 必须定义非空的 _BENEFIT_HIDDEN（用户定档隐藏云端/算力/AI/本地字眼）');
const sources = new Set([...(M.from_limits || []), ...(M.extra || [])]);
for (const key of Object.keys(M.hidden)) {
  assert.ok(sources.has(key),
    `_BENEFIT_HIDDEN 里的 "${key}" 在 _BENEFIT_FROM_LIMITS / _BENEFIT_EXTRA 中找不到来源 —— `
    + '要么对应条目已改名/删除（请同步名单），要么名单是摆设。');
}

// ── ③ 隐藏 ≠ 取消限流（变异测试补上的关键断言）──────────────────────────────
// 网页端只有 cloud（配额 200/免费 3）与 no_credits（纯说明项，无配额）。
const EXPECTED = { cloud: { daily: 200, free: 3 } };
for (const [key, want] of Object.entries(EXPECTED)) {
  assert.ok(Object.keys(M.hidden).includes(key), `${key} 应在隐藏名单里`);
  assert.equal(Number(M.daily[key]), want.daily,
    `${key} 的会员限额被改动了 —— 隐藏只是不展示文案，配额与限流必须原样（隐藏 ≠ 取消）`);
  assert.equal(Number(M.free[key]), want.free,
    `${key} 的免费限额被改动了 —— 同上`);
  const row = (M.feature_rows || []).find(r => String(r.resource) === key);
  assert.ok(row, `${key} 应仍在 FEATURE_USAGE_DEFS（使用统计表照旧有这一行）`);
  assert.equal(Number(row.member_limit), want.daily,
    `${key} 在使用统计表里的 member_limit 应仍为 ${want.daily}`);
}

// ── ④ plans() 的 benefits 与 download_benefits() 一致 ───────────────────────
assert.equal((M.plans_benefits || []).length, M.benefits.length,
  'plans().download_member.benefits 与 download_benefits() 条数必须一致'
  + '（前端购买中心读的是 plans()）');

console.log(`✅ 会员页权益文案守卫通过（渲染输出 ${M.benefits.length} 条均不含「云端/算力/AI/本地」；`
  + `隐藏 ${Object.keys(M.hidden).length} 条但配额与限流原样）`);
