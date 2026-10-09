// 「贡献 Cookie 到公共池」勾选框改为隐藏 回归守卫（2026-10-09 用户截图反馈）
//
// 用户诉求（截图 + 两个字「隐藏」）：网页版「高级选项」里那个
//   ☑ 贡献 Cookie 到公共池（默认开启）
// 不要露出来。
//
// 关键的工程约束（本守卫存在的意义，三条都不是形式主义）：
//   ① **只隐藏、不删元素**。web/app.js 的 el 表登记了 `cookieContribute: $('cookieContribute')`，
//      并在两条粘贴 Cookie 的路径上判 `if (el.cookieContribute.checked)` 决定是否贡献。
//      元素一旦被删 ⇒ `$()` 返回 null ⇒ `.checked` 抛 TypeError，且贡献**静默**失效
//      （用户粘贴了 Cookie 却进不了池，线上没有任何报错）。所以必须断言元素仍在。
//   ② **仍是「默认贡献」**。隐藏是 UI 层面的，不能顺带把 `checked` 去掉——
//      那等于把功能关了，而页面上再也看不到开关，属于最坏形态。
//   ③ **不得留下悬空指引**。帮助文案里原有「不想共享可在上方取消『贡献 Cookie 到公共池』勾选」，
//      勾选框隐藏后这句话指向一个不可见的控件，必须同步去掉。
//
// 本测试读 web/index.html 与 web/app.js 的**真源码**做结构断言（不测副本）。
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const indexHtml = readFileSync(join(repoRoot, 'web', 'index.html'), 'utf8');
const appJs = readFileSync(join(repoRoot, 'web', 'app.js'), 'utf8');

let n = 0;
const ok = (cond, msg) => { assert.ok(cond, msg); n += 1; };
const eq = (a, b, msg) => { assert.equal(a, b, msg); n += 1; };

// ---- ① 元素本身：必须存在、唯一、且仍是 checked ----
const idx = indexHtml.indexOf('id="cookieContribute"');
ok(idx > 0, '找不到 #cookieContribute —— 元素被删了：app.js 取 .checked 会抛 TypeError，贡献会静默失效');
eq(indexHtml.split('id="cookieContribute"').length - 1, 1, '#cookieContribute 必须唯一');

const lbStart = indexHtml.lastIndexOf('<label', idx);
const lbEnd = indexHtml.indexOf('</label>', idx);
ok(lbStart > 0 && lbEnd > idx, '#cookieContribute 的 <label> 包裹结构被破坏（切片失败）');
const labelBlock = indexHtml.slice(lbStart, lbEnd + '</label>'.length);

ok(/type="checkbox"/.test(labelBlock), '勾选框 input 被换成了别的控件');
ok(/\bchecked\b/.test(labelBlock),
  '#cookieContribute 丢了 checked —— 隐藏后用户再也无法打开，等于把「默认贡献」关掉');

// ---- ② 隐藏：display:none 必须落在 label 自身（挂父级会连「代理地址」一起藏掉）----
ok(/style="display:none"/.test(labelBlock),
  'label 上没有 style="display:none" —— 勾选框又露出来了');
ok(!/hidden\b/.test(labelBlock.replace(/\/\*[\s\S]*?\*\//g, '')),
  '不要改用 hidden 属性：.adv-body 是 flex 容器，display 一旦被别的规则接管 hidden 会失效；统一用内联 display:none');

// 隐藏必须局限在 label 块内：紧随其后的「代理地址」标签不得被波及
ok(/<label class="adv-label" for="proxyInput"/.test(indexHtml.slice(lbEnd)),
  '隐藏波及到了后续的「代理地址」标签（切片右边界画错了）');

// ---- ③ 不得留悬空指引 ----
ok(!indexHtml.includes('取消勾选'),
  'index.html 仍有「取消勾选…」文案，指向已隐藏的控件（帮助文案 #cookieHelp 里那句要同步删掉）');
ok(/Cookie 会贡献到公共池/.test(indexHtml),
  '帮助文案应改成陈述句「Cookie 会贡献到公共池…」，而不是删掉整段共享说明');

// ---- ④ 行为不变：app.js 仍登记该节点、仍按它决定贡献 ----
ok(/cookieContribute:\s*\$\('cookieContribute'\)/.test(appJs),
  'app.js 的 el 表不再登记 cookieContribute（贡献开关的读取会拿不到节点）');
const readCount = appJs.split('el.cookieContribute.checked').length - 1;
ok(readCount >= 2,
  `app.js 只在 ${readCount} 处读 el.cookieContribute.checked（应有 2 处：粘贴 Cookie 的两条路径），贡献行为被削弱`);
ok(/const contributeCookie\s*=/.test(appJs), 'contributeCookie 实现被删了');
ok(appJs.includes('/api/cookie/contribute'),
  'contributeCookie 不再打 /api/cookie/contribute');

// ---- ⑤ 桌面端不受影响：App 侧本就没有这个开关（它走「仅本次请求、服务端不留存」口径）----
const desktopIndex = join(repoRoot, '..', 'video-downloader-app', 'web', 'index.html');
let desktopHtml = null;
try { desktopHtml = readFileSync(desktopIndex, 'utf8'); } catch { /* 兄弟树不在也不变红：本守卫只管网页版 */ }
if (desktopHtml) {
  ok(!desktopHtml.includes('id="cookieContribute"'),
    '桌面 App 的 index.html 意外出现了 Cookie 贡献开关（该口径桌面端不用）');
}

console.log(`  ✓ Cookie 贡献勾选框隐藏守卫：${n} 项断言通过`);
