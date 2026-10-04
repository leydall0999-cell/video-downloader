// 会员页权益文案：条目全保留，只是不该出现「云端 / 算力 / AI / 本地」几个字（2026-10-04 用户定档）
//
// 定档原文：**隐藏那几个字，不是隐藏有关那几个字的全部条目**。
// 第一版理解错了，做成「整条隐藏」，被用户当场纠正 —— 权益条目是权益，不能少；
// 要改的只是**措辞**（那几个词讲的是架构与成本口径，用户看不懂也不关心）。
//
// 正确做法：直接改 `_BENEFIT_FROM_LIMITS` / `_BENEFIT_EXTRA` 里的**文案模板**，
// 条目数、配额、限流一概不动：
//   本地一键抠图        → 一键抠图
//   云端算力（转码…）   → 在线处理（转码…）
//   App 本地重算力（…） → 视频处理（…）
//   不含 AI 积分：云端… → 不含积分额度：…
//
// 本守卫钉四件事：
//   ① **渲染输出**里不得含这四个词（查输出，不是查源码常量——模板本身就该改干净）；
//   ② 🔴 **条目数不得减少**：每条 DAILY/FREE 配额都要在输出里出现（防止又退化成「隐藏条目」，
//      这是第一版走错路的直接防线）；member_limit=-1 的功能同样必须在；
//   ③ 配额与限流数值原样（改措辞不该顺手改额度）；
//   ④ plans() 的 benefits 与 download_benefits() 条数一致（前端购买中心读 plans()）。
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const serverDir = join(repoRoot, 'server');
const PY = process.env.VDL_TEST_PY
  || '/Users/suixindelang/.workbuddy/binaries/python/versions/3.13.12/bin/python3';

const probe = `
import os, sys, tempfile, json
os.environ["VDL_DATA_DIR"] = tempfile.mkdtemp(prefix="vdlbene_")
os.environ["VDL_CLOUD_LINK"] = "0"
os.environ["VDL_PLANS_CLOUD"] = "0"
sys.path.insert(0, ${JSON.stringify(serverDir)})
import membership as M
store = M.MembershipStore()
print(json.dumps({
    "benefits": M.download_benefits(),
    "daily": M.DAILY_QUOTA_LIMITS,
    "free": M.FREE_DAILY_LIMITS,
    "feature_rows": [{"key": d.get("key"), "resource": d.get("resource"),
                      "name": d.get("name"), "member_limit": d.get("member_limit")}
                     for d in M.FEATURE_USAGE_DEFS],
    "ai_features": M.AI_FEATURES,
    "plans_benefits": store.plans().get("download_member", {}).get("benefits") or [],
}, ensure_ascii=False))
`;
const M = JSON.parse(execFileSync(PY, ['-c', probe], { encoding: 'utf8' }));

// ── ① 措辞：输出里不得含实现口径词 ──────────────────────────────────────────
const KEYWORDS = ['云端', '算力', 'AI', '本地'];
assert.ok(Array.isArray(M.benefits) && M.benefits.length > 0,
  'download_benefits() 应返回非空权益清单');
for (const b of M.benefits) {
  const text = String(b.text || '');
  const hit = KEYWORDS.filter(k => text.includes(k));
  assert.ok(hit.length === 0,
    `权益文案不得含「云端/算力/AI/本地」这些字，但 key="${b.key}" 命中 ${JSON.stringify(hit)}：${text}`
    + '（正确做法是改措辞，如「云端算力」→「在线处理」，不是删整条）');
}
for (const f of (M.ai_features || [])) {
  const hit = KEYWORDS.filter(k => String(f).includes(k));
  assert.ok(hit.length === 0,
    `AI_FEATURES 文案不得含这些字，命中 ${JSON.stringify(hit)}：${f}`);
}

// ── ② 条目数不得减少（防止退化成「隐藏条目」）───────────────────────────────
// 🔴 这一条是针对第一版理解错误（整条隐藏）加的：权益条目是权益，不能少。
const keys = new Set((M.benefits || []).map(b => String(b.key)));
for (const key of Object.keys(M.daily || {})) {
  if (Number(M.daily[key]) > 0) {
    assert.ok(keys.has(key),
      `配额 ${key}=${M.daily[key]}（会员可用）必须在权益清单里出现 —— `
      + '条目只改措辞、不许删（2026-10-04 用户纠正过：隐藏的是那几个字，不是整条）。');
  }
}
for (const key of Object.keys(M.free || {})) {
  if (Number(M.free[key]) > 0 || key in (M.daily || {})) {
    // 免费表独有的键（如网页端没有的 subtitle）也要有对应文案或说明项
    if (!(key in (M.daily || {}))) {
      assert.ok(keys.has(key),
        `配额 ${key}（仅免费表有）应在权益清单里有对应条目或说明项。`);
    }
  }
}
for (const row of (M.feature_rows || [])) {
  if (Number(row.member_limit) === -1) {
    const expectKey = String(row.key);
    assert.ok(keys.has(expectKey),
      `${row.name}（member_limit=-1，会员不限次）必须在权益清单里写明「不限」—— `
      + '条目不能因为改措辞被弄丢。');
  }
}

// ── ③ 配额与限流数值原样（改措辞不该顺手改额度）────────────────────────────
// 2026-10-04 定档时的数值，改文案不许顺手动。
const EXPECTED = { download: [1000, 10], cloud: [200, 3] };
for (const [key, [memberV, freeV]] of Object.entries(EXPECTED)) {
  assert.equal(Number(M.daily[key]), memberV, `${key} 会员限额应为 ${memberV}/日（只改措辞）`);
  assert.equal(Number(M.free[key]), freeV, `${key} 免费限额应为 ${freeV}/日（只改措辞）`);
}

// ── ④ plans() 与 download_benefits() 条数一致 ──────────────────────────────
assert.equal((M.plans_benefits || []).length, M.benefits.length,
  'plans().download_member.benefits 与 download_benefits() 条数必须一致（前端读的是 plans()）');

console.log(`✅ 权益文案守卫通过（${M.benefits.length} 条权益全保留、文案不含「云端/算力/AI/本地」、`
  + '配额与限流原样）');
