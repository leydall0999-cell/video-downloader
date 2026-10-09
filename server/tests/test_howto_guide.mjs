#!/usr/bin/env node
/**
 * 操作引导「怎么用」守卫 —— web-dev 树（2026-10-09）
 *
 * 蓝本：video-downloader-app(server/tests/test_howto_guide.mjs)，按网页版自己的
 * 功能集适配（网页版无 bridge/sr/page/compress/matting/library 等，含 shareqr/pagegen）。
 *
 * 钉死：
 *   ① 9 个功能入口每个有且仅有一条引导，结构与默认收起态完整；
 *   ② 每条引导落在自己视图/子面板区间内，不串台；
 *   ③ 引导里「」引用的控件名必须在 index.html / app.js 真实存在；
 *   ④ 首次自动展开 + 已读显式收起 + 已关闭跳过；
 *   ⑤ 折叠样式；
 *   ⑥ 右上角「?」按钮 8 处落点（hero + 子页签行 + 6 个标题）；
 *   ⑦「✕ 关闭引导」9 处，class 隐藏（绝不能 hidden 属性）；
 *   ⑧「?」三态：已隐藏→展开 / 收起→展开 / 展开→整条隐藏；
 *   ⑨ 子面板切换（去水印 图片/PDF、分享 二维码/网页）后同步引导与按钮。
 *
 * 运行：node server/tests/test_howto_guide.mjs
 */
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, '..', '..');
const REL = (p) => join(repoRoot, p);

const indexHtml = readFileSync(REL('web/index.html'), 'utf8');
const appJs = readFileSync(REL('web/app.js'), 'utf8');
const css = readFileSync(REL('web/styles.css'), 'utf8');

// ⚠️ 「出处库」必须先剥掉引导条自身，否则断言自证成真（写什么过什么）。
const HOWTO_BLOCK_RE = /<details class="howto"[\s\S]*?<\/details>/g;
const hay = indexHtml.replace(HOWTO_BLOCK_RE, '') + '\n' + appJs;
// ⚠️ JS 断言也必须剥注释：代码改坏而注释留着时，纯全文匹配会误判通过。
const appJsCode = appJs
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/(^|[^:])\/\/[^\n]*/g, '$1');

let count = 0;
const ok = (cond, msg) => { count++; assert.ok(cond, msg); };
const eq = (a, b, msg) => { count++; assert.equal(a, b, msg); };

/* ------------------------------------------------------------------ */
/* 期望清单：key -> [所属区间的前锚, 后锚]（web-dev 功能集，共 9 条）    */
/* ------------------------------------------------------------------ */
const EXPECT = new Map([
  ['download',      ['<section class="hero"', 'class="panel input-panel"']],
  ['uploadconvert', ['id="uploadConvertView"', 'id="musicConvertView"']],
  ['musicconvert',  ['id="musicConvertView"', 'id="imageConvertView"']],
  ['imageconvert',  ['id="imageConvertView"', 'id="subtitleView"']],
  ['subtitle',      ['id="subtitleView"', 'id="shareQrView"']],
  ['share',         ['id="shareQrView"', 'id="pageGenView"']],
  ['page',          ['id="pageGenView"', 'id="dwView"']],
  ['dw_img',        ['id="dwImgPane"', 'id="dwPdfPane"']],
  ['dw_pdf',        ['id="dwPdfPane"', 'id="appIntroView"']],
]);

/* ---------------- ① 每条引导的结构 ---------------- */
const blocks = [...indexHtml.matchAll(
  /<details class="howto" data-howto="([^"]+)"[\s\S]*?<\/details>/g,
)].map((m) => ({ key: m[1], html: m[0], at: m.index }));

eq(blocks.length, EXPECT.size,
  `引导条数量应恰好 ${EXPECT.size} 条（每个功能一条），实际 ${blocks.length} 条`);

const gotKeys = blocks.map((b) => b.key);
for (const key of EXPECT.keys()) {
  ok(gotKeys.includes(key), `缺少功能「${key}」的操作引导（该功能页没有引导条）`);
}
eq(new Set(gotKeys).size, gotKeys.length, `引导条 key 有重复：${gotKeys.join(', ')}`);

for (const b of blocks) {
  const tag = `<details class="howto" data-howto="${b.key}">`;
  ok(b.html.startsWith(tag), `「${b.key}」引导条开标签不完整（应含 class="howto" 与 data-howto）`);
  ok(/<summary class="howto-head">/.test(b.html),
    `「${b.key}」缺少 <summary class="howto-head"> —— 没有它就点不开折叠`);
  ok(/class="howto-title"/.test(b.html), `「${b.key}」缺少 howto-title（用户看不到「怎么用 · xx」字样）`);
  ok(/<div class="howto-body">/.test(b.html), `「${b.key}」缺少 howto-body 内容容器`);
  ok(/<ol class="howto-steps">/.test(b.html), `「${b.key}」缺少 howto-steps 有序步骤列表`);
  ok(/class="howto-note"/.test(b.html), `「${b.key}」缺少 howto-note（注意事项）—— 每个功能至少要有边界说明`);

  const lis = [...b.html.matchAll(/<li>([\s\S]*?)<\/li>/g)].map((m) => m[1]);
  ok(lis.length >= 3, `「${b.key}」只有 ${lis.length} 步，操作引导至少要 3 步才讲得清`);
  lis.forEach((li, i) => {
    const plain = li.replace(/<[^>]+>/g, '').trim();
    ok(plain.length >= 8, `「${b.key}」第 ${i + 1} 步是空壳（剥掉标签后只剩 "${plain}"）`);
  });

  // 默认必须收起：HTML 里写死 open 会让「首次才展开」这个设计彻底失效
  ok(!/<details[^>]*\sopen[\s>]/.test(b.html),
    `「${b.key}」写了默认 open —— 引导条必须默认收起，由首次进入时才自动展开`);
}

/* ---------------- ② 每条落在自己的视图区间内 ---------------- */
function between(preAnchor, postAnchor) {
  const a = indexHtml.indexOf(preAnchor);
  const b = indexHtml.indexOf(postAnchor, a + 1);
  if (a < 0 || b < 0) return null;
  return { a, b, text: indexHtml.slice(a, b) };
}

for (const [key, [pre, post]] of EXPECT) {
  const seg = between(pre, post);
  ok(seg !== null, `定位「${key}」所在区间失败（锚点 "${pre}" 或 "${post}" 在 index.html 里找不到）`);
  ok(seg.text.includes(`data-howto="${key}"`),
    `「${key}」的引导条不在自己所属的视图区间内（跑到别的功能页去了）`);
  for (const other of EXPECT.keys()) {
    if (other === key) continue;
    ok(!seg.text.includes(`data-howto="${other}"`),
      `「${key}」区间内混入了「${other}」的引导条（两者串台，切页时会同时展开）`);
  }
}

/* ---------------- ③ 引导里的控件名必须真实存在 ---------------- */
for (const b of blocks) {
  const plain = b.html.replace(/<[^>]+>/g, '');
  const quoted = [...plain.matchAll(/「([^」]+)」/g)].map((m) => m[1]);
  ok(quoted.length >= 2,
    `「${b.key}」引导里只用「」标注了 ${quoted.length} 个控件名 —— 步骤没有指向真实按钮，用户找不到点哪里`);
  for (const q of quoted) {
    ok(hay.includes(q),
      `「${b.key}」引导里引用的「${q}」在 index.html / app.js 中不存在 —— ` +
      '文案与真实控件脱节（多半是按钮文案改了却没同步引导）');
  }
}

/* ---------------- ④ 首次自动展开 + 已读显式收起 + 已关闭跳过 ---------------- */
ok(/function howtoAutoOpen\s*\(/.test(appJsCode),
  'app.js 里找不到 howtoAutoOpen() —— 首次进入功能时不会自动展开引导');
ok(/const HOWTO_SEEN_KEY = 'vdl_howto_seen';/.test(appJsCode),
  '已读记录用的 localStorage key 不见了（换了 key 会导致所有用户被重新弹一遍引导）');

// ⚠️ 以下断言必须限定在 howtoAutoOpen 的函数体内部（同名字样在别处也会出现）
const howtoFnStart = appJsCode.indexOf('function howtoAutoOpen()');
const howtoFnEnd = appJsCode.indexOf('\n  }\n', howtoFnStart);
const howtoFn = howtoFnStart > 0
  ? appJsCode.slice(howtoFnStart, howtoFnEnd > 0 ? howtoFnEnd : howtoFnStart + 1500)
  : '';
ok(howtoFn.length > 200, '定位 howtoAutoOpen 的函数体失败（找不到起止位置）');
ok(/localStorage\.getItem\(HOWTO_SEEN_KEY\)/.test(howtoFn)
   && /localStorage\.setItem\(HOWTO_SEEN_KEY/.test(howtoFn),
  'howtoAutoOpen 内部没有同时读写已读记录 —— 只读不写会每次进页面都弹');
ok(/d\.closest\('\[hidden\]'\)/.test(howtoFn),
  'howtoAutoOpen 内部没有用 closest([hidden]) 判断可见性 —— 会把其它隐藏视图的引导也一并展开');
ok(/details\.howto\[data-howto\]/.test(howtoFn),
  'howtoAutoOpen 的选取器不再是 details.howto[data-howto] —— 引导条会被漏选或误选');
ok(/seen\.indexOf\(k\)\s*>=\s*0/.test(howtoFn),
  'howtoAutoOpen 内部没有「已读过就跳过」的判断 —— 每次切页都会重新弹引导');
// 🔴 已读的条必须**显式收起**，不能只 return：SPA 不重建 DOM，上次展开着离开
//    再切回来仍是展开的，就不满足「除第一次外每次进入都默认收起」。
ok(/if \(d\.open\) d\.open = false;/.test(howtoFn),
  'howtoAutoOpen 对已读引导条只跳过、没有显式收起 —— 上次展开着离开，切回来还是展开的（应默认收起）');
ok(/is-closed'\)\)\s*return/.test(howtoFn),
  'howtoAutoOpen 没有跳过已关闭的引导 —— 用户关掉后切页回来又会被自动弹开');

const swDef = appJsCode.indexOf('function switchView(');
// ⚠️ 必须从 swDef 之后找：网页版 howtoAfterPaneSwitch 定义在 switchView 之前，
//    其函数体内的调用会抢走「首次出现」，从全文找会假失败。
const swCall = appJsCode.indexOf('try { howtoAutoOpen(); }', swDef > 0 ? swDef : 0);
ok(swDef > 0 && swCall > swDef,
  'switchView 里没有调用 howtoAutoOpen() —— 切到某功能页时引导不会展开');
const fns = appJsCode.match(/function howtoAutoOpen\s*\(/g) || [];
eq(fns.length, 1, `howtoAutoOpen 定义了 ${fns.length} 次（应恰好 1 次）`);
// 启动即套用已关闭状态（不依赖 switchView），冷启动时不闪一下
ok(appJsCode.indexOf('try { howtoApplyClosed(); }') < howtoFnStart
   && (appJsCode.match(/try \{ howtoApplyClosed\(\); \} catch \(_\) \{\}/g) || []).length >= 2,
  '缺少启动时的 howtoApplyClosed() 调用 —— 冷启动时已关闭的引导条会先闪一下再消失');

// 🔴 init 的 /api/nodes 异步回调会再 switchView 一次默认视图：同步兜底已经进入过
//    download（首见展开+写已读），二次进入会按「已读」把刚展开的引导**当场收走**
//    （真机实测：冷启动 ~2s 展开 → ~2.4s 被收走）。异步重套用必须以 !bootViewSet 为前置。
ok(/if \(!bootViewSet && !document\.querySelector\('\.tab\.is-active:not\(#tabDownload\)'\)\)/.test(appJsCode),
  'init 异步回调里的默认视图重套用缺 !bootViewSet 前置 —— 同视图二次进入会触发已读收起，冷启动首见展开被当场收走（实测过的缺陷）');

/* ---------------- ⑤ 样式必须能折叠成一行 ---------------- */
const cssCode = css.replace(/\/\*[\s\S]*?\*\//g, '');
function ruleBody(selector) {
  const i = cssCode.indexOf(selector);
  if (i < 0) return null;
  const s = cssCode.indexOf('{', i);
  if (s < 0) return null;
  const e = cssCode.indexOf('}', s);
  if (e < 0) return null;
  const body = cssCode.slice(s + 1, e).trim();
  return body.length > 0 ? body : null;
}

const need = [
  ['.howto {', /border-radius|border:/, '引导条外框（缺了会变成裸文字，跟正文糊在一起）'],
  ['.howto-head {', /cursor:\s*pointer/, '引导条标题行（缺了用户不知道这行可以点）'],
  ['.howto-head::-webkit-details-marker {', /display:\s*none/, '去掉原生三角（否则和自绘箭头重复出现）'],
  ['.howto[open] .howto-caret {', /transform/, '展开时箭头翻转'],
  ['.howto-body {', /padding/, '展开后的内容内边距'],
  ['.howto-steps li {', /font-size|line-height/, '步骤字号行高'],
  ['.howto-note {', /border-top|font-size/, '注意事项样式'],
  ['.howto-btn {', /margin-left:\s*auto/, '右上角「?」按钮（少了 margin-left:auto 会紧贴标题文字而不是靠最右）'],
  ['.howto-btn.is-active {', /color|background/, '「?」按钮的展开态（用户看不出引导正开着）'],
  ['#downloadView .hero .howto-btn {', /position:\s*absolute/, '下载页 hero 是居中布局，「?」按钮必须绝对定位到右上角'],
  ['.howto.is-flash {', /animation/, '点「?」展开时的闪烁提示'],
];
for (const [sel, re, why] of need) {
  const body = ruleBody(sel);
  ok(body !== null, `styles.css 里缺少可用规则 ${sel} —— ${why}`);
  ok(re.test(body), `styles.css 的 ${sel} 规则体缺少关键声明（${why}）`);
}
ok(/@keyframes howtoFlash\s*\{/.test(cssCode),
  'styles.css 缺少 howtoFlash 动画定义 —— 点「?」展开时不会有视线提示');

/* ---------------- ⑥ 右上角「?」按钮：8 处落点 ---------------- */
// web-dev 落点：下载 hero 1 + 视频处理子页签行 1 + 标题 6（mus/img/sb/sqr/pg/dw）。
// 去水印 2 个子面板、分享 2 个子页共用标题上的一个按钮（靠可见性天然唯一）。
const BTN_RE = /<button type="button" class="howto-btn"[^>]*>\?<\/button>/g;
const btnCount = (indexHtml.match(BTN_RE) || []).length;
eq(btnCount, 8, `「?」查看引导按钮应恰好 8 个（hero + 子页签行 + 6 个标题），实际 ${btnCount} 个`);

const TITLE_IDS = ['musTitle', 'imgTitle', 'sbTitle', 'sqrTitle', 'pgTitle', 'dwTitle'];
for (const id of TITLE_IDS) {
  const reBtn = new RegExp(`<h2 id="${id}"[^>]*>[\\s\\S]*?class="howto-btn"[\\s\\S]*?</h2>`);
  ok(reBtn.test(indexHtml),
    `「${id}」标题里没有「?」按钮 —— 该功能右上角缺少随时重看引导的入口`);
}

const heroStart = indexHtml.indexOf('<section class="hero"');
const heroEnd = indexHtml.indexOf('class="panel input-panel"');
ok(heroStart > 0 && heroEnd > heroStart, '定位下载页 hero 区间失败');
ok(indexHtml.slice(heroStart, heroEnd).includes('class="howto-btn"'),
  '下载页 hero 右上角没有「?」按钮');

// 视频处理子页签行（格式转换 / 拼接）的按钮：两个子模块共用，落点在子页签行内
const ucTabs = between('id="ucSubTabs"', 'data-howto="uploadconvert"');
ok(ucTabs !== null && ucTabs.text.includes('class="howto-btn"'),
  '视频处理子页签行没有「?」按钮 —— 格式转换 / 拼接缺少随时重看引导的入口');

// JS 侧：按钮同步 + 三态点击 + 作用域定位
ok(/function howtoSyncButtons\s*\(/.test(appJsCode),
  'app.js 缺少 howtoSyncButtons() —— 「?」按钮的显示与激活态不会更新');
ok(/e\.target\.closest\('\.howto-btn'\)/.test(appJsCode),
  'app.js 没有绑定「?」按钮的点击 —— 按钮点了没反应');
ok(/btn\.classList\.toggle\('is-active'/.test(appJsCode),
  '「?」按钮没有激活态同步逻辑（引导开着时按钮不会高亮）');
// 🔴 网页版 shareQrView / pageGenView / downloadView 是 <div> 不是 <section>，
//    app-dev 的 section[id] 选择器在这里匹配不到 ⇒ 必须按 id 后缀匹配。
ok(/howtoScopeOf\s*=\s*\(btn\)\s*=>\s*btn\.closest\('\[id\$="View"\],\s*\[id\$="Pane"\]'\)/.test(appJsCode),
  'howtoScopeOf 的作用域定位被改动 —— 网页版多个视图是 div，必须用 [id$="View"], [id$="Pane"] 匹配，否则会退化到 document.body 找错引导');
ok(/addEventListener\('toggle',[\s\S]{0,700}?\}, true\)/.test(appJsCode),
  '缺少对 details toggle 的捕获监听（toggle 事件不冒泡，必须用捕获）—— 手动开合引导时按钮状态会不同步');
const swSync = appJsCode.indexOf('try { howtoSyncButtons(); }', swDef);
ok(swSync > swDef,
  'switchView 里没有调用 howtoSyncButtons() —— 切到另一功能时按钮状态会残留');
// 网页版这 6 个标题目前没有 JS 动态赋值；一旦有人改成 textContent 整体赋值，
// 标题里的「?」按钮会被一起抹掉 —— 提前把这条路堵死。
for (const id of TITLE_IDS) {
  ok(!new RegExp(`el\\.${id}\\.textContent\\s*=`).test(appJsCode),
    `「${id}」被改成 textContent 整体赋值 —— 会清空 h2 全部子节点，标题里的「?」按钮一起被抹掉`);
}

/* ---------------- ⑦「✕ 关闭引导」：关掉整条消失，右上角「?」重新打开 ---------------- */
const closeCount = (indexHtml.match(/class="howto-close"/g) || []).length;
eq(closeCount, EXPECT.size,
  `「✕ 关闭引导」按钮应恰好 ${EXPECT.size} 个（每条引导一个），实际 ${closeCount} 个`);

for (const b of blocks) {
  ok(/class="howto-close"/.test(b.html), `「${b.key}」没有「✕ 关闭引导」按钮 —— 用户打开了却关不掉`);
  ok(/class="howto-foot"/.test(b.html), `「${b.key}」缺少 howto-foot 容器`);
  const iSum = b.html.indexOf('</summary>');
  const iBody = b.html.indexOf('<div class="howto-body">');
  const iClose = b.html.indexOf('class="howto-close"');
  ok(iBody > 0 && iClose > iBody,
    `「${b.key}」的关闭按钮不在 .howto-body 内 —— 收起状态下也会露出来，且与标题行的开合点击打架`);
  ok(iSum > 0 && iClose > iSum,
    `「${b.key}」的关闭按钮落在 <summary> 里 —— 点它会同时触发标题行的开合`);
  // 关闭区文案里不得出现「」引用：③ 段会把引导条内所有「」当成"界面真实控件名"去校验
  const footSeg = b.html.slice(b.html.indexOf('class="howto-foot"'));
  ok(!/「/.test(footSeg),
    `「${b.key}」的关闭区文案里出现了「」引用 —— 会被 ③ 段当成控件名去 index.html/app.js 里找，必假失败`);
}

const closeCssNeed = [
  ['.howto.is-closed {', /display:\s*none/, '关掉的引导条必须整条不显示（否则还占一行，等于没关掉）'],
  ['.howto-foot {', /display:\s*flex/, '关闭区布局（按钮与提示文字同一行）'],
  ['.howto-close {', /cursor:\s*pointer/, '关闭按钮要能被认出可点'],
];
for (const [sel, re, why] of closeCssNeed) {
  const body = ruleBody(sel);
  ok(body !== null, `styles.css 里缺少可用规则 ${sel} —— ${why}`);
  ok(re.test(body), `styles.css 的 ${sel} 规则体缺少关键声明（${why}）`);
}

ok(/const HOWTO_CLOSED_KEY = 'vdl_howto_closed';/.test(appJsCode),
  'app.js 缺少 HOWTO_CLOSED_KEY —— 关闭状态不会被记住，用户关掉后下次进来又被弹出来');
ok(/function howtoApplyClosed\s*\(/.test(appJsCode),
  'app.js 缺少 howtoApplyClosed() —— 已关闭的引导条不会真正隐藏');
ok(/e\.target\.closest\('\.howto-close'\)/.test(appJsCode),
  'app.js 没有绑定「✕ 关闭引导」的点击 —— 按钮点了没反应');
ok(/classList\.add\('is-closed'\)/.test(appJsCode),
  '关闭引导时没有打上 .is-closed —— 引导条不会消失');

// 🔴 关掉的引导条**绝不能用 hidden 属性**：howtoSyncButtons / visibleHowtoIn 靠
//    closest('[hidden]') 判「本视图有没有引导条」，一旦给 details 加 hidden，
//    右上角「?」按钮会把自己也判成"没内容"而跟着隐藏 ⇒ 用户再也打不开。
const closeSeg = appJsCode.slice(
  appJsCode.indexOf("closest('.howto-close')"),
  appJsCode.indexOf("closest('.howto-btn')"));
ok(closeSeg.length > 100 && closeSeg.length < 900,
  '定位「关闭引导」的处理逻辑失败（切片长度异常）');
ok(!/\.hidden\s*=\s*true/.test(closeSeg) && !/setAttribute\(\s*'hidden'/.test(closeSeg),
  '关闭引导时给元素加了 hidden 属性 —— 会让右上角「?」按钮把自己也判成没内容而隐藏，用户再也打不开');

// switchView 里必须「先套用已关闭状态、再自动展开」，顺序反了会把刚关掉的又弹开
const swClosed = appJsCode.indexOf('try { howtoApplyClosed(); }', swDef);
const swCallAt = appJsCode.indexOf('try { howtoAutoOpen(); }', swDef);
ok(swClosed > swDef,
  'switchView 里没有调用 howtoApplyClosed() —— 切页时已关闭的引导条会重新露面');
ok(swClosed > swDef && swCallAt > swClosed,
  'switchView 里 howtoApplyClosed() 必须排在 howtoAutoOpen() 之前（顺序反了会把已关闭的引导又自动展开）');

/* ---------------- ⑧ 点第二次「?」必须整条隐藏，不能只是「收起」 ---------------- */
// 三态：① 已隐藏 → 显示+展开  ② 可见但收起 → 展开  ③ 可见且已展开 → 整条隐藏
const btnSeg = appJsCode.slice(
  appJsCode.indexOf("closest('.howto-btn')"),
  appJsCode.indexOf("closest('.howto-btn')") + 1100);
ok(/classList\.remove\('is-closed'\)/.test(btnSeg),
  '点「?」时没有解除 .is-closed —— 关掉的引导条再也打不开（「重新打开就点问号」这条链路断掉）');
// 🔴 必须把 d.open = true 限定在 is-closed 分支**内部**（到 } else if 为止）。
ok(/classList\.remove\('is-closed'\);[\s\S]{0,250}?d\.open = true;\s*\n\s*\} else if \(!d\.open\)/.test(btnSeg),
  '点「?」解除关闭后没有在本分支内强制展开 —— 用户点一下只看到一行空壳，需要再点一下才展开');
ok(!/d\.open\s*=\s*!\s*d\.open/.test(btnSeg),
  '点「?」又变回 open = !open 的开合 —— 第二次点击只会「收起」，引导条仍占一行（用户要的是整条隐藏）');
ok(/classList\.add\('is-closed'\)[\s\S]{0,120}?d\.open = false;/.test(btnSeg),
  '「已展开」态点「?」没有整条隐藏（缺 classList.add(is-closed) + open=false，且必须先打 class 再置 open）—— 会留下占一行的收起态');
// 🔴 真机探针抓到的真缺陷：引导条初始就是「收起但可见」，若点「?」直接落到隐藏分支，
//    用户第一次点开看到的反而是引导被关掉。必须有独立的「收起 → 展开」分支。
ok(/\} else if \(!d\.open\) \{\s*\n\s*d\.open = true;\s*\n\s*\}/.test(btnSeg),
  '缺少「收起态点「?」= 展开」的分支 —— 用户第一次点按钮会看到引导反而消失（真机探针实测过的缺陷）');
ok(/if \(d\.classList\.contains\('is-closed'\)\) \{[\s\S]{0,400}?classList\.remove\('is-closed'\)/.test(btnSeg),
  '「?」的打开分支结构被破坏（is-closed 判断与 remove 不再成对）—— 关掉的引导条打不开');
// 隐藏时不得滚动 / 闪动：条已不可见，滚动只会让页面莫名跳一下
ok(/if \(!d\.classList\.contains\('is-closed'\)\) \{[\s\S]{0,200}?scrollIntoView/.test(btnSeg),
  '「?」隐藏引导条时仍执行 scrollIntoView —— 条已消失，滚动会让页面莫名跳动');
ok(/is-active',\s*!!\(d && d\.open && !d\.classList\.contains\('is-closed'\)\)/.test(appJsCode),
  'is-active 判定没有排除 is-closed —— 引导条已隐藏、「?」按钮却还亮着（状态自相矛盾）');

// toggle 监听里**绝不能**把「收起」转成整条隐藏：「收起」是合法默认态
const toggleSeg = appJsCode.slice(
  appJsCode.indexOf("addEventListener('toggle'"),
  appJsCode.indexOf("addEventListener('toggle'") + 400);
ok(toggleSeg.length > 150,
  '定位 toggle 监听失败（找不到 addEventListener(\'toggle\') 片段）');
ok(!/classList\.add\('is-closed'\)/.test(toggleSeg),
  'toggle 监听里把「收起」转成了整条隐藏 —— 用户点标题行收起后整条消失，只能靠「?」找回（收起必须是合法态）');
ok(/howtoSyncButtons\(\)/.test(toggleSeg),
  'toggle 监听丢了 howtoSyncButtons() —— 手动开合引导时「?」按钮状态不同步');

/* ---------------- ⑨ 子面板切换后必须同步（web-dev 特有） ---------------- */
// 网页版有两组不走 switchView 的子面板：去水印 图片/PDF（dwSwitchPane）、
// 分享 二维码/网页（shareSubnav）。切完必须跑 howtoAfterPaneSwitch()，
// 否则刚露出来的子面板引导不展开、右上角「?」的激活态也不换目标。
const paneCalls = appJsCode.match(/howtoAfterPaneSwitch\(\);/g) || [];
ok(paneCalls.length >= 2,
  `howtoAfterPaneSwitch() 只有 ${paneCalls.length} 处调用（应 ≥2：去水印子面板 + 分享子页）—— 子面板切换后引导与按钮状态不同步`);
// 分享子页：pageGenView 显隐复位之后必须紧跟调用
ok(/pageGenView\.hidden = _sharePane !== 'pagegen';[\s\S]{0,300}?howtoAfterPaneSwitch\(\);/.test(appJsCode),
  '分享子页切换后没有调用 howtoAfterPaneSwitch() —— 二维码/网页两条引导不会按首次/已读规则展开');
// 去水印子面板：dwSwitchPane 里的调用
ok(/dwSwitchPane\s*=\s*\(toImg\)\s*=>[\s\S]{0,600}?howtoAfterPaneSwitch\(\);/.test(appJsCode),
  '去水印子面板切换（dwSwitchPane）后没有调用 howtoAfterPaneSwitch() —— 图片/PDF 两条引导不会按首次/已读规则展开');

console.log(`✅ 操作引导守卫生效（web-dev）：${EXPECT.size} 个功能入口全覆盖、位置正确、`
  + `文案与真实控件一致、首次展开与已读收起机制在位、`
  + `右上角「?」按钮 8 处落点、三态交互、`
  + `「✕ 关闭引导」${EXPECT.size} 处、子面板切换同步（共 ${count} 项断言）`);
