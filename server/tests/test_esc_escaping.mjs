// HTML 转义函数「真转义」回归守卫（安全审计 2026-10-11，P2-22）
//
// 缺陷：`web/app.js` 里有两处 `const esc = (s) => (s == null ? '' : String(s));`
//   —— 名字叫 esc、注释写「轻量转义」，实际是**纯字符串化的 no-op**。
//   而它被用在 160+ 处 innerHTML 模板里：客服会话的消息正文与图片地址、
//   后台告警的 detail/email/ip、对账明细、事件日志、访客 path/message、
//   用户账号标识……全部原样当 HTML 解析 ⇒ 构成「用户 ↔ 客服 ↔ 后台」
//   互相注入的**存储型 XSS**（真正干活的转义函数是 `escHtml`，但被绕过了）。
//
// 修复：两处 `esc` 统一改为与 `escHtml` 同一实现（真转义）。
//
// 本守卫钉死：
//   ① 不得再出现 no-op 形态的 esc 定义（正则反回归）；
//   ② 每一处 esc 定义都**实际求值**并验证 & < > " ' 全部被转义（行为断言，非结构断言）；
//   ③ 真转义函数 escHtml 的源码里五种实体齐备；
//   ④ 唯一的**非 HTML 用法**（后台用户列表的搜索过滤）不得再用 esc ——
//      否则账号含撇号（o'brien@x.com）会被转成 &#39; 而搜不到；
//   ⑤ esc( 的调用点数量不得塌陷（防「靠删调用点让守卫变绿」）。
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const appJs = readFileSync(join(repoRoot, 'web', 'app.js'), 'utf8');

let n = 0;
const ok = (cond, msg) => { assert.ok(cond, msg); n += 1; };
const eq = (a, b, msg) => { assert.equal(a, b, msg); n += 1; };

// ---------- ① 反回归：no-op 形态必须彻底消失 ----------
const noopRe = /const\s+esc\s*=\s*\(s\)\s*=>\s*\(s\s*==\s*null\s*\?\s*''\s*:\s*String\(s\)\)/;
ok(!noopRe.test(appJs),
  'app.js 里仍有 no-op 的 esc 定义（String(s) 不做任何转义，用在 innerHTML 里即 XSS）');

// ---------- ② 行为验证：每一处 esc 定义都必须真的转义 ----------
// 求值 `esc` 定义时需要一个 escHtml 供别名式定义引用（`const esc = escHtml;`）。
// 这里用**参考实现**喂给它，随后 ③ 再单独校验真实 escHtml 的源码。
const REF_ESCHTML = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const defs = [...appJs.matchAll(/const\s+esc\s*=\s*([^;\n]+);/g)].map((m) => m[1].trim());
eq(defs.length, 2, `预期两处 esc 定义（外层 + admin 面板），实际 ${defs.length}：${defs}`);

for (const [i, d] of defs.entries()) {
  const fn = new Function('escHtml', `return (${d});`)(REF_ESCHTML);
  eq(typeof fn, 'function', `第 ${i + 1} 处 esc 定义求值后不是函数：${d}`);
  eq(fn('<script>'), '&lt;script&gt;', `第 ${i + 1} 处 esc 未转义尖括号`);
  eq(fn('<img src=x onerror=alert(1)>'), '&lt;img src=x onerror=alert(1)&gt;',
    `第 ${i + 1} 处 esc 未转义 img 注入串`);
  eq(fn('a"b'), 'a&quot;b', `第 ${i + 1} 处 esc 未转义双引号（属性逃逸）`);
  eq(fn("a'b"), 'a&#39;b', `第 ${i + 1} 处 esc 未转义单引号（属性逃逸）`);
  eq(fn('a&b'), 'a&amp;b', `第 ${i + 1} 处 esc 未转义 &`);
  eq(fn(null), '', `第 ${i + 1} 处 esc 对 null 应回空串`);
  eq(fn(0), '0', `第 ${i + 1} 处 esc 对数字 0 应回 "0"（不能当 falsy 丢掉）`);
}

// ---------- ③ 真转义函数 escHtml 的源码必须五种实体齐备 ----------
for (const ent of ['&amp;', '&lt;', '&gt;', '&quot;', '&#39;']) {
  ok(appJs.includes(ent), `escHtml 源码缺少实体 ${ent}（转义不完整）`);
}
ok(/replace\(\s*\/\[&<>"'\]\/g/.test(appJs) || /\.replace\(\/&\/g[\s\S]{0,200}replace\(\/'\/g/.test(appJs),
  '未找到 escHtml 的字符类替换实现');

// ---------- ④ 唯一的非 HTML 用法不得用 esc ----------
// 后台「用户管理」列表的搜索过滤：identifier 参与 includes(q) 比较，不是 HTML 输出。
ok(!/const\s+identifier\s*=\s*esc\(/.test(appJs),
  '搜索过滤处又用回了 esc —— 含撇号的账号（o\'brien@x.com）会被转义成 &#39; 而搜不到');

// ---------- ⑤ 调用点数量不得塌陷 ----------
const callSites = (appJs.match(/[^a-zA-Z]esc\(/g) || []).length;
ok(callSites >= 100,
  `esc( 调用点仅剩 ${callSites} 处 —— 若为「删调用点让守卫变绿」请改为真正转义`);

console.log(`  ✓ HTML 转义真转义守卫：${n} 项断言通过（esc 定义 ${defs.length} 处、调用点 ${callSites} 处）`);
