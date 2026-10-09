// AI 会员权益清单：必须真实存在，且前端必须真的渲染（2026-10-04）
//
// 背景（两个真问题）：
//  ① 后端 `AI_FEATURES` 写过「AI 字幕识别 / 视频总结 / 图片翻译体验」三项，
//     全仓搜不到对应路由 = 拿不存在的功能做卖点（与「今日使用」占位行同源，V1 抄 DataTool）。
//  ② 前端**从来没渲染过** `ai_member.features` —— 接口一直返回，AI 会员面板只有
//     一行「含VIP会员全部权益」+ 两张卡。用户反馈「AI 会员补充权益」才发现。
//
// 本守卫钉：
//   ① 每条 AI_FEATURES 都能在 server/ 里找到**实现落点**（关键词命中真实文件/路由/常量）；
//   ② 禁止再出现「本地」字眼（用户 2026-10-04 定档：措辞不讲架构口径）；
//   ③ 文案里的**数字**（积分额度、积分单价）必须与代码常量一致 —— 改文案不许编数字；
//   ④ 🔴 前端必须渲染 AI 轨权益（`features`），不能又只渲染 download 轨的 `benefits`；
//   ⑤ 网页端与桌面端清单**可以不同**（网页端无解说/抠图），但不得互相抄不存在的功能。
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const serverDir = join(repoRoot, 'server');
const PY = process.env.VDL_TEST_PY
  || '/Users/suixindelang/.workbuddy/binaries/python/versions/3.13.12/bin/python3';

/** 剥掉整行注释 —— app.py 里满屏是 `# ---- 自动解说 …` 这类说明，
 *  直接 includes('commentary') 恒为真会让守卫变绿（变异实测踩过）。 */
const strip = (s) => s.split('\n').map(l => l.replace(/^\s*(\/\/|\/\*|\*).*$/, '')).join('\n');

function collectPy(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    if (name === '__pycache__') continue;
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...collectPy(p));
    else if (name.endsWith('.py') && name !== 'membership.py') out.push(p);
  }
  return out;
}
const businessSrc = collectPy(serverDir)
  .map(p => ({ p, src: readFileSync(p, 'utf8') }))
  .filter(({ p }) => !p.includes(`${join('tests')}`));

const probe = `
import os, sys, tempfile, json
os.environ["VDL_DATA_DIR"] = tempfile.mkdtemp(prefix="vdlaifeat_")
os.environ["VDL_CLOUD_LINK"] = "0"; os.environ["VDL_PLANS_CLOUD"] = "0"
sys.path.insert(0, ${JSON.stringify(serverDir)})
import membership as M
print(json.dumps({
    "features": list(M.AI_FEATURES),
    "cloud_cost": M.MATTING_CLOUD_CREDIT_COST,
    "ai_plans": {k: v.get("credits") for k, v in M.AI_PLANS.items()},
}, ensure_ascii=False))
`;
const M = JSON.parse(execFileSync(PY, ['-c', probe], { encoding: 'utf8' }));

// ── ① 非空 + 每条有实现落点 ─────────────────────────────────────────────────
assert.ok(Array.isArray(M.features) && M.features.length >= 3,
  `AI_FEATURES 至少要有 3 条权益（当前 ${(M.features || []).length} 条）`);

// 每条文案里的**实义词**必须能在 server/ 业务代码里找到落点。
// （不用整句匹配 —— 文案是中文，代码是英文，靠关键词映射）
const LANDMARKS = [
  { kw: ['解说', '解说词'], files: ['commentary'] },
  { kw: ['网关', '密钥', '吊销'], files: ['gateway_config'] },
  { kw: ['抠图'], files: ['matting'] },
  { kw: ['积分'], files: ['membership'] },
  { kw: ['VIP会员'], files: ['membership'] },
];
for (const f of M.features) {
  const hit = LANDMARKS.filter(l => l.kw.some(k => f.includes(k)));
  assert.ok(hit.length > 0,
    `AI 权益「${f}」里没有任何可核对的实义词 —— 写之前先确认它在 server/ 里有实现落点`
    + '（例：解说→routers/commentary.py、网关→gateway_config.py、抠图→routers/matting.py）');
  for (const l of hit) {
    const found = businessSrc.some(({ p, src }) =>
      l.files.some(f => p.endsWith(`${f}.py`) || src.includes(f)));
    assert.ok(found,
      `AI 权益「${f}」提到的能力在 server/ 里找不到实现落点（找了 ${l.files.join('/')}）`
      + ' —— 拿不存在的功能做卖点是本项目已犯过的错（V1 从 DataTool 抄了 8 行占位）。');
  }
}

// ── ② 措辞：不得出现「本地」这类实现口径字眼 ────────────────────────────────
for (const f of M.features) {
  assert.ok(!f.includes('本地'),
    `AI 权益文案不得含「本地」（用户 2026-10-04 定档：措辞不讲架构口径）：${f}`);
}

// ── ③ 文案里的数字必须与代码常量一致 ─────────────────────────────────────────
const cloudCost = Number(M.cloud_cost);
assert.equal(cloudCost, 50, `MATTING_CLOUD_CREDIT_COST 应为 50（改文案不改成本）`);
const costFeat = M.features.find(f => f.includes('抠图'));
if (costFeat) {
  assert.ok(costFeat.includes(String(cloudCost)),
    `AI 权益写了云端抠图单价，必须与 MATTING_CLOUD_CREDIT_COST 一致（${cloudCost}）：${costFeat}`);
}
const creditVals = Object.values(M.ai_plans || {}).map(Number).filter(Boolean);
for (const f of M.features) {
  const m = f.match(/(\d{3,5})\s*\/\s*(\d{3,5})/);
  if (!m) continue;
  const pair = [Number(m[1]), Number(m[2])];
  assert.ok(pair.every(v => creditVals.includes(v)),
    `AI 权益「${f}」里的积分数 ${pair.join('/')} 与 AI_PLANS 的额度 `
    + `${creditVals.join('/')} 不一致 —— 改文案不许编数字。`);
}

// ── ④ 前端必须真的渲染 AI 轨权益 ────────────────────────────────────────────
const appJs = readFileSync(join(repoRoot, 'web', 'app.js'), 'utf8');
const code = strip(appJs);
assert.ok(/\.features/.test(code),
  'web/app.js 必须引用 ai_member.features —— 前端从未渲染过 AI 权益（2026-10-04 用户反馈）。');
assert.ok(!/key\s*===\s*['"]download['"]\s*&&\s*Array\.isArray\(t\.benefits\)/.test(code),
  '不能只在 download 轨渲染权益 —— AI 轨的 features 也要渲染。');

// ── ⑤ 网页端不得抄桌面端独有的能力 ──────────────────────────────────────────
// 桌面端有解说/抠图，网页端没有 → 网页端文案若出现这两项就是虚假承诺。
const appRepo = join(repoRoot, '..', 'video-downloader-app');
if (existsSync(appRepo)) {
  const probe2 = `
import os, sys, tempfile, json
os.environ["VDL_DATA_DIR"] = tempfile.mkdtemp(prefix="vdlaifeat2_")
os.environ["VDL_CLOUD_LINK"] = "0"; os.environ["VDL_PLANS_CLOUD"] = "0"
sys.path.insert(0, ${JSON.stringify(join(appRepo, 'server'))})
import membership as M
print(json.dumps({"features": list(M.AI_FEATURES)}, ensure_ascii=False))
`;
  const A = JSON.parse(execFileSync(PY, ['-c', probe2], { encoding: 'utf8' }));
  const webHas = (kw) => (M.features || []).some(f => f.includes(kw));
  const deskHas = (kw) => (A.features || []).some(f => f.includes(kw));
  // ⚠️ 判据不能用「文件存在」—— web 仓**有** server/routers/commentary.py 但
  //    **没 include 进 app**，接口实测 /api/commentary/diagnostics 返 404，
  //    是未接线的孤儿文件 ⇒ 「文件在」不等于「能力在」。
  // 正确判据：本端**真的挂载了对应路由**（`include_router` 出现在 app.py 里）。
  const MOUNTED = {
    // 只认真正的 include_router(...) 调用 —— 判据不能是「文件存在」也不能是
    // 「app.py 里出现该词」：web 仓有 routers/commentary.py 这个孤儿文件，
    // app.py 里也有大段 `# ---- 自动解说 …` 注释，但**从未 include**，
    // 真接口 /api/commentary/diagnostics 实测 404。
    '解说': /include_router\(\s*commentary\.router\s*\)|include_router\(\s*commentary\b/,
    '抠图': /include_router\(\s*matting\.router\s*\)|include_router\(\s*matting\b/,
  };
  const appCode = strip(readFileSync(join(repoRoot, 'server', 'app.py'), 'utf8'));
  const mounted = (kw) => MOUNTED[kw].test(appCode);
  for (const kw of ['解说', '抠图']) {
    if (webHas(kw)) {
      assert.ok(mounted(kw),
        `网页端 AI 权益写了「${kw}」，但 server/app.py 里没有 include 它的 router —— `
        + '承诺了做不到的功能（这些能力只在桌面端真正挂载）。'
        + '⚠️ 别用「文件是否存在」判断，web 仓有孤儿 commentary.py 但未接线。');
    }
  }
  // 交叉核对：本端没写、另一端写了 → 正常（能力不同源），不算问题，仅提示存在差异
  for (const kw of ['解说', '抠图']) {
    if (!webHas(kw) && deskHas(kw)) {
      console.log(`   ℹ️ 桌面端有「${kw}」、网页端没有 —— 预期内（两端能力不同源），已用不同文案`);
    }
  }
}

console.log(`✅ AI 会员权益守卫通过（${M.features.length} 条均有实现落点、措辞合规、数字与常量一致、`
  + '前端已渲染 AI 轨）');
