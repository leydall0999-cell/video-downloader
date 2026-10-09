// 输入框 placeholder 灰度统一 回归守卫（2026-10-09 用户截图反馈）
//
// 用户诉求（截图 + 「字体颜色太深了，灰度与其他会话框保持一致」）：
//   网页版「高级选项」里的「会话 Cookie」输入框，placeholder 文案看着比别处深。
//
// 实测结论（真浏览器 computed style，非臆测）：
//   · #urlInput::placeholder  = rgb(162,172,189)  ← 全站唯一显式调浅的
//   · #cookieInput::placeholder = rgb(117,117,117) ← Chrome UA 默认深灰 #757575
//   即：深浅不齐的根因不是「Cookie 框单独被写深」，而是**只有主输入框被显式调浅**，
//   其余全部吃浏览器默认深灰（WKWebView 默认又偏浅 ⇒ 跨引擎也不一致）。
//   修复方式：加一条全局兜底 `input::placeholder, textarea::placeholder { color: <基准>; opacity: 1 }`。
//   基准迭代：首版 #a2acbd（亮度 171）→ 用户当日再反馈「再浅一点」→ 提浅为 #b8c2d2（亮度 193，
//   对齐主流组件库 placeholder 档位 Element UI #C0C4CC / Ant Design 191）。
//
// 本守卫钉死四条工程约束（每条都对应一个真实会踩的坑）：
//   ① 兜底规则必须存在、必须同时覆盖 input 与 textarea（只写 input 会漏掉 Cookie 会话框本身）。
//   ② 基准色必须与 #urlInput::placeholder **同值**（同源约束）：将来改基准若只改一处，
//      主输入框与其余框会再次错开 —— 那正是本次 bug 的形态。
//   ③ 顺序约束：兜底规则（特异性 0-1-1）必须排在 `.auth-input::placeholder`（同为 0-1-1）**之前**，
//      否则同权重下后写者胜，会把登录框既有的 #b8bec5 悄悄改掉（无声的视觉回归）。
//   ④ 反回归：任何 `::placeholder` 规则都不得再出现 Chrome 默认深灰 #757575；
//      且 `.adv-textarea` / `.adv-input` 自身不得再挂 placeholder 色（否则覆盖兜底，用户看到的框照旧深）。
//
// 本测试读 web/styles.css 的**真源码**做结构断言（不测副本）。
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const cssPath = join(repoRoot, 'web', 'styles.css');
const css = readFileSync(cssPath, 'utf8');
const indexHtml = readFileSync(join(repoRoot, 'web', 'index.html'), 'utf8');

let n = 0;
const ok = (cond, msg) => { assert.ok(cond, msg); n += 1; };
const eq = (a, b, msg) => { assert.equal(a, b, msg); n += 1; };

// ---------- 工具：抽取所有 ::placeholder 规则（带源码位置） ----------
const rules = [];
{
  const re = /([^{}]*?::placeholder[^{}]*?)\{([^{}]*)\}/g;
  let m;
  while ((m = re.exec(css)) !== null) {
    const sel = m[1].replace(/\/\*[\s\S]*?\*\//g, '').trim();
    const body = m[2];
    const cm = body.match(/color\s*:\s*([^;]+)/i);
    rules.push({ sel, body, index: m.index, color: cm ? cm[1].trim() : null });
  }
}

// ---------- 工具：把颜色解析成亮度（越亮=越浅；无法解析的如 color-mix 返回 null） ----------
const luminance = (c) => {
  if (!c) return null;
  let r, g, b;
  let m = c.match(/^#([0-9a-f]{6})$/i);
  if (m) {
    r = parseInt(m[1].slice(0, 2), 16); g = parseInt(m[1].slice(2, 4), 16); b = parseInt(m[1].slice(4, 6), 16);
  } else if ((m = c.match(/^#([0-9a-f]{3})$/i))) {
    r = parseInt(m[1][0] + m[1][0], 16); g = parseInt(m[1][1] + m[1][1], 16); b = parseInt(m[1][2] + m[1][2], 16);
  } else if ((m = c.match(/rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)/i))) {
    r = +m[1]; g = +m[2]; b = +m[3];
  } else {
    return null;
  }
  return Math.round(0.2126 * r + 0.7152 * g + 0.0722 * b);
};

// ---------- ① 全局兜底规则存在，且 input + textarea 都覆盖 ----------
// 「全局」判据：选择器中存在独立成项的 `input::placeholder` / `textarea::placeholder`
// （`#urlInput::placeholder` 与 `.com-retain-input input::placeholder` 都不算）。
const isGlobalTerm = (sel, tag) => new RegExp(`(^|,)\\s*${tag}::placeholder\\s*(,|$)`).test(sel);
const fallback = rules.find((r) => isGlobalTerm(r.sel, 'input') || isGlobalTerm(r.sel, 'textarea'));
ok(!!fallback, '找不到全局 input/textarea::placeholder 兜底规则 —— Cookie 框会退回浏览器默认深灰 #757575');
ok(isGlobalTerm(fallback.sel, 'input'), `兜底规则的选择器里没有独立成项的 input::placeholder：${fallback.sel}`);
ok(isGlobalTerm(fallback.sel, 'textarea'),
  `兜底规则的选择器里没有独立成项的 textarea::placeholder（Cookie 会话框本身就是 textarea，漏掉它等于没修）：${fallback.sel}`);

// ---------- ② 兜底色 = #urlInput 的色（同源约束） ----------
const urlInputRule = rules.find((r) => /#urlInput::placeholder/.test(r.sel));
ok(!!urlInputRule, '找不到 #urlInput::placeholder 规则（主输入框的浅灰基准被删了？）');
const baseColor = urlInputRule.color;
ok(baseColor && luminance(baseColor) !== null, `#urlInput::placeholder 的 color 无法解析：${baseColor}`);
eq(luminance(baseColor), 193, `主输入框基准色亮度变了（期望 193 / #b8c2d2，实际 ${baseColor}）`);
eq((fallback.color || '').toLowerCase().replace(/\s+/g, ''), baseColor.toLowerCase().replace(/\s+/g, ''),
  `兜底规则色 ${fallback.color} 与 #urlInput 基准 ${baseColor} 不一致 —— 深浅又会错开（同源约束）`);
ok(luminance(baseColor) >= 190,
  `基准色 ${baseColor}（亮度 ${luminance(baseColor)}）偏深，不足以解决「字体颜色太深 / 再浅一点」的诉求`);
ok(/opacity\s*:\s*1/.test(fallback.body),
  '兜底规则缺 opacity:1 —— Firefox 默认给 placeholder 叠 0.54 透明度，会比其他引擎再浅一档');

// ---------- ③ 顺序：兜底必须早于 .auth-input::placeholder（同权重靠顺序） ----------
const authRule = rules.find((r) => /\.auth-input::placeholder/.test(r.sel));
if (authRule) {
  ok(fallback.index < authRule.index,
    '兜底规则排在 .auth-input::placeholder 之后：两者特异性同为 0-1-1，后写者胜 ⇒ 登录框既有的 #b8bec5 被悄悄改掉');
  eq(authRule.color, '#b8bec5', `.auth-input::placeholder 的专属色被改动：${authRule.color}`);
}
// .com-retain-input input::placeholder 特异性 0-2-1 高于兜底，顺序无影响，仅确保它没被误删
const retainRule = rules.find((r) => /\.com-retain-input\s+input::placeholder/.test(r.sel));
ok(!!retainRule, '「保留比例」输入框的 placeholder 规则被删了（它是有意设计的 45% 文字色，不是漏网）');

// ---------- ④ 反回归：不得再出现 Chrome 默认深灰；具体输入框不得自带更深覆盖 ----------
for (const r of rules) {
  ok(luminance(r.color) !== 117,
    `${r.sel} 又把 placeholder 写成 #757575（Chrome 默认深灰，亮度 117）—— 正是本次要修的形态`);
}
const advSelfRule = rules.find((r) => /\.adv-textarea::placeholder|\.adv-input::placeholder/.test(r.sel));
ok(!advSelfRule,
  `.adv-textarea/.adv-input 自挂了 placeholder 规则（${advSelfRule && advSelfRule.sel}）：特异性高于兜底，会把 Cookie 框重新压深`);

// ---------- ⑤ Cookie 框确实有 placeholder 文案（否则颜色断言无意义） ----------
ok(/id="cookieInput"[^>]*\n?\s*placeholder="/.test(indexHtml) || /id="cookieInput"/.test(indexHtml),
  'index.html 里找不到 #cookieInput');
const ci = indexHtml.indexOf('id="cookieInput"');
ok(ci > 0, '#cookieInput 被删了');
ok(/placeholder="[^"]{10,}"/.test(indexHtml.slice(ci, ci + 420)),
  '#cookieInput 没有多字 placeholder 文案（浅色基准也就无从体现）');

// ---------- ⑥ 跨树一致性：兄弟树若在，也必须统一（防止只改一边） ----------
const sibling = join(repoRoot, '..', repoRoot.endsWith('-app') ? 'video-downloader' : 'video-downloader-app',
  'web', 'styles.css');
if (existsSync(sibling)) {
  const sib = readFileSync(sibling, 'utf8');
  ok(/input::placeholder\s*,[\s\S]{0,40}?textarea::placeholder\s*\{\s*color\s*:\s*#b8c2d2/i.test(sib),
    `兄弟树 ${sibling} 未同步 placeholder 兜底规则 —— 网页版/桌面端灰度会再次分叉`);
}

console.log(`  ✓ 输入框 placeholder 灰度统一守卫：${n} 项断言通过`);
