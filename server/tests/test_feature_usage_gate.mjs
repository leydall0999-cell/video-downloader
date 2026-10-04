// 「今日使用」功能配额表必须与服务器真实拦截点一致（2026-10-04）
//
// 背景（真实事故）：这张表在 V1 时是从另一个产品 DataTool 整份抄过来的，
// 9 行里 6 行**从来没有任何实现**——全仓搜不到对应路由、没有 use_daily 拦截点：
//   插件原画解析 / AI字幕识别 / 插件批量下载素材 / 插件批量下载评论 /
//   插件批量下载数据 / 插件批量下载字幕 / 图片翻译 / 视频总结
// 用户在个人中心看到 9 个功能，实际只有 1 个能用；会员页还把它们当卖点。
// 更糟的是反向也漏了：网页端真正生效的 cloud 配额（字幕提取/转码/拼接/去水印
// 烧录全走它）在表里根本没有行 —— 于是「买了会员到底能干什么」显示不出来。
//
// 本守卫钉死：**表里每一行的 resource，都必须能在本仓 server/ 业务代码里
// 搜到真实的配额拦截点**（use_daily / quota_state / cloud_quota_gate）。
// 想加新功能进表，必须先把它实现出来（加拦截点），顺序反了就红。
//
// 同时钉：
//  ① 表里不得残留已下线的死资源键（original / batch_material / comment /
//     data / ai_subtitle / subtitle_batch / image_translate）；
//  ② 每一行的 free_limit / member_limit 必须与 DAILY_QUOTA_LIMITS /
//     FREE_DAILY_LIMITS 一致（免费档限额的「体验剩余」列直接取这两个值）；
//  ③ download_benefits() 不得承诺已下线功能的权益文案。
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const memberPy = readFileSync(join(repoRoot, 'server', 'membership.py'), 'utf8');

// ── 递归收集 server/ 下所有 .py（跳过 tests 与缓存）────────────────────────
function collectPy(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    if (name === 'tests' || name === '__pycache__' || name === 'node_modules') continue;
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...collectPy(p));
    else if (name.endsWith('.py') && name !== 'membership.py') out.push(p);
  }
  return out;
}
const businessPy = collectPy(join(repoRoot, 'server'))
  .map(p => ({ p, src: readFileSync(p, 'utf8') }));

// ── 解析 FEATURE_USAGE_DEFS / DAILY_QUOTA_LIMITS / FREE_DAILY_LIMITS ────────
const rowsBlock = memberPy.match(/FEATURE_USAGE_DEFS:[^\[]*\[([\s\S]*?)\n\]/);
assert.ok(rowsBlock, 'server/membership.py 必须仍定义 FEATURE_USAGE_DEFS');
const rows = [...rowsBlock[1].matchAll(
  /"key":\s*"([^"]+)".*?"resource":\s*"([^"]+)".*?"free_limit":\s*(-?\d+).*?"member_limit":\s*(-?\d+)/g,
)].map(m => ({ key: m[1], resource: m[2], free: Number(m[3]), member: Number(m[4]) }));
assert.ok(rows.length > 0, 'FEATURE_USAGE_DEFS 解析不到任何行（格式变了？）');

function readLimits(name) {
  const b = memberPy.match(new RegExp(`${name}:[^\\{]*\\{([\\s\\S]*?)\\n\\}`));
  assert.ok(b, `server/membership.py 必须仍定义 ${name}`);
  return Object.fromEntries([...b[1].matchAll(/"([^"]+)":\s*(\d+)/g)].map(m => [m[1], Number(m[2])]));
}
const daily = readLimits('DAILY_QUOTA_LIMITS');
const free = readLimits('FREE_DAILY_LIMITS');

// ── ① 每行必须有真实拦截点 ─────────────────────────────────────────────────
// 拦截点形态：use_daily("X" / quota_state("X" / cloud_quota_gate（内部查 "X"）
function hasGate(resource) {
  const re = new RegExp(`(use_daily|quota_state)\\(\\s*["']${resource}["']`);
  return businessPy.some(({ p, src }) => re.test(src));
}

for (const r of rows) {
  assert.ok(
    hasGate(r.resource),
    `FEATURE_USAGE_DEFS 的 "${r.key}"（resource="${r.resource}"）在 server/ 里找不到任何 `
    + `use_daily("${r.resource}") / quota_state("${r.resource}") 拦截点 —— `
    + '这就是一个「页面上写着、实际做不到」的功能。要么先把它实现出来，'
    + '要么从表里删掉，别让会员页拿空气做卖点。',
  );
}

// ── ①b 已下线的死资源不得回到表里 ───────────────────────────────────────────
const DEAD = ['original', 'batch_material', 'comment', 'data',
              'ai_subtitle', 'subtitle_batch', 'image_translate'];
for (const r of rows) {
  assert.ok(!DEAD.includes(r.resource),
    `"${r.key}" 用的是已下线资源 "${r.resource}"（2026-10-04 移除，相关功能从未实现）。`);
  assert.ok(!/^plugin_/.test(r.key),
    `"${r.key}" 是从 DataTool 抄来的「插件…」命名，功能不存在；VDL 侧的对应功能请用真实命名。`);
}

// ── ② 表里的限额必须与两张配额表一致 ────────────────────────────────────────
for (const r of rows) {
  if (r.member >= 0) {
    assert.equal(daily[r.resource], r.member,
      `${r.key} 的 member_limit=${r.member} 与 DAILY_QUOTA_LIMITS["${r.resource}"]}=`
      + `${daily[r.resource]} 不一致 —— 「权益余额」列与实际拦截会打架。`);
  }
  if (r.free >= 0) {
    assert.equal(free[r.resource], r.free,
      `${r.key} 的 free_limit=${r.free} 与 FREE_DAILY_LIMITS["${r.resource}"]}=`
      + `${free[r.resource]} 不一致。`);
  }
}

// ── ③ 权益文案不得承诺已下线功能 ────────────────────────────────────────────
const benefitBlock = memberPy.match(/_BENEFIT_FROM_LIMITS:[^(]*\(([\s\S]*?)\n\)/);
assert.ok(benefitBlock, 'server/membership.py 必须仍定义 _BENEFIT_FROM_LIMITS');
const benefitBody = benefitBlock[1];
assert.ok(!/"original"/.test(benefitBody) && !/原画 \/ 4K/.test(benefitBody),
  '_BENEFIT_FROM_LIMITS 不得再承诺「原画 / 4K 直链解析 N 次/日」—— 原画是清晰度档位门'
  + '（>1080P 需会员），不按次计费，且该配额从未被拦截。');
assert.ok(!/批量下载素材/.test(benefitBody),
  '_BENEFIT_FROM_LIMITS 不得再承诺「批量下载素材 N 条/日」—— 该功能从未实现。');
// 真正生效的配额都必须在 _BENEFIT_FROM_LIMITS 里有文案（有配额必有卖点）
for (const key of Object.keys(daily)) {
  assert.ok(benefitBody.includes(`("${key}"`),
    `配额 ${key}=${daily[key]}（会员可用）却没写权益文案 —— 用户看不到买了能得到什么。`);
}

console.log(`✅ 功能配额表与真实拦截点一致（${rows.length} 行：`
  + `${rows.map(r => r.key).join(', ')}；逐行均搜到 use_daily/quota_state 拦截点）`);
