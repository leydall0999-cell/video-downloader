/* 「嗅探比视频慢一步」回归测试（2026-10-02 用户实测）。
 *
 * 现象：桌面面板列表里**最新那条永远是上一个视频**的标题；想看当前视频的条目，
 *      得再点开另一个视频。用户原话：「app上嗅探到的比视频慢一步，当前最新嗅探到的
 *      是上一个视频，想要看的当前视频的嗅探结果得打开另一个视频，嗅探没有实时同步」。
 *
 * 根因（真实 Chrome 时序，本测试就是照它构造的）：
 *   YouTube 点开新视频是 SPA 换页 → Chrome 先报 tabs.onUpdated 的 changeInfo.url，
 *   **此刻 tab.title 还是上一页的标题**（新页标题要等数据加载完才改）。
 *   只在这一拍推送 → 首推必然带旧标题；而 pagePushed 的 5 分钟冷却又把重推挡掉，
 *   桌面端条目就永久停在错标题上。
 *
 * 修法：
 *   ① tabs.onUpdated 的 changeInfo.title（标题 settle）→ refreshPageTitle() 补推一次；
 *   ② pageSeen 的 why='player'（播放器就绪，此刻 document.title 已是本视频）同样走补推，
 *      不再被 5 分钟冷却挡下。
 *   服务端配套见 server/cdp_sniffer.py::_register（page 条目允许标题后到覆盖）
 *   与 server/tests/test_sniffer_ext_push.py::test_ext_page_title_late_correction。
 *
 * 为什么用 node + chrome 桩而不是真浏览器：见 test_background_page_scope.js 顶部注释
 * （本机沙盒里 Chrome for Testing 连本机 HTTP 完全不通）。这里跑的是**真实 background.js**。
 *
 * 运行：node extension/tests/test_page_title_lag.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

let passes = 0;
let failures = 0;
function eq(actual, expect, msg) {
  if (actual === expect) { passes++; return; }
  failures++;
  console.error('FAIL: ' + msg + '（期望 ' + JSON.stringify(expect) +
    '，实际 ' + JSON.stringify(actual) + '）');
}
function ok(cond, msg) { eq(!!cond, true, msg); }

// ---- chrome.* 桩（与 test_background_page_scope.js 同款）----
const L = {};
const pushes = [];             // 收到的 /api/sniffer/ext-push 载荷
let stubActiveTab = 11;
const TAB_URL = {};
const TAB_TITLE = {};

globalThis.fetch = (url, opts) => {
  const u = String(url);
  if (u.indexOf('/api/sniffer/ext-push') >= 0) {
    pushes.push(JSON.parse((opts && opts.body) || '{}'));
  }
  const payload = u.indexOf('/api/sniffer/status') >= 0 ? { state: 'idle' } : { ok: true };
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(payload) });
};

globalThis.chrome = {
  webRequest: {
    onSendHeaders: { addListener: (fn) => { L.sendHeaders = fn; } },
    onCompleted: { addListener: (fn) => { L.completed = fn; } },
    onErrorOccurred: { addListener: (fn) => { L.errorOccurred = fn; } },
  },
  storage: {
    local: { get: (_k, cb) => cb({}), set: (_o, cb) => { if (cb) cb(); }, remove: (_k, cb) => { if (cb) cb(); } },
    session: { get: (_k, cb) => cb({}), set: (_o, cb) => { if (cb) cb(); } },
  },
  action: { setBadgeText: () => {}, setBadgeBackgroundColor: () => {} },
  tabs: {
    query: (q, cb) => {
      const cb2 = typeof q === 'function' ? q : cb;
      if (cb2) {
        cb2([{ id: stubActiveTab, url: TAB_URL[stubActiveTab] || '', title: TAB_TITLE[stubActiveTab] || '' }]);
      }
    },
    get: (id, cb) => cb({ id: id, title: TAB_TITLE[id] || '', url: TAB_URL[id] || '' }),
    create: () => {},
    update: () => {},
    onRemoved: { addListener: (fn) => { L.tabRemoved = fn; } },
    onActivated: { addListener: (fn) => { L.tabActivated = fn; } },
    onUpdated: { addListener: (fn) => { L.tabUpdated = fn; } },
  },
  runtime: {
    lastError: null,
    getManifest: () => ({ version: '1.0.46' }),
    onMessage: { addListener: (fn) => { L.message = fn; } },
  },
  alarms: { create: () => {}, onAlarm: { addListener: (fn) => { L.alarm = fn; } } },
};

globalThis.importScripts = () => { require(path.join(__dirname, '..', 'sniff-core.js')); };
vm.runInThisContext(
  fs.readFileSync(path.join(__dirname, '..', 'background.js'), 'utf8'),
  { filename: 'background.js' });

ok(typeof L.tabUpdated === 'function', 'background.js 已注册 tabs.onUpdated 监听器');
ok(typeof globalThis.refreshPageTitle === 'function', '★ 已提供 refreshPageTitle（标题修正入口）');

const TID = 11;
const A = 'https://www.youtube.com/watch?v=AAAAAAAAAAA';
const B = 'https://www.youtube.com/watch?v=BBBBBBBBBBB';
const C = 'https://www.youtube.com/watch?v=CCCCCCCCCCC';
const T_A = '（5）A 视频的标题 - YouTube';
const T_B = '（5）B 视频的标题 - YouTube';

const pause = () => new Promise((r) => setTimeout(r, 25));
const lastPush = () => pushes[pushes.length - 1];
const pushesFor = (u) => pushes.filter((p) => p.items && p.items[0] && p.items[0].url === u);
function ask(payload, sender) {
  let got = null;
  L.message(payload, sender || {}, (r) => { got = r; });
  return got;
}

(async function () {
  // ---- 0) 追平初始状态：用户已在看视频 A（标题已 settle）----
  TAB_URL[TID] = A; TAB_TITLE[TID] = T_A;
  L.tabUpdated(TID, { url: A }, { id: TID, url: A, title: T_A });
  await pause();
  eq(pushesFor(A).length, 1, '初始页 A 推了 1 次');
  eq(lastPush().items[0].page_title, T_A, 'A 的标题正确');

  // ---- 1) 点开视频 B：地址先到，标题还是 A 的（真实 Chrome 时序）----
  TAB_URL[TID] = B;                       // tab.url 已变
  // TAB_TITLE 故意保持 A —— 这就是真实瞬间
  L.tabUpdated(TID, { url: B }, { id: TID, url: B, title: T_A });
  await pause();

  const bPushes = pushesFor(B);
  eq(bPushes.length, 1, '地址变化即推送（首推不能等标题）');
  eq(bPushes[0].items[0].page_title, T_A,
    '① 首推带的是**上一页**标题 —— 这就是 bug 现场（Chrome 此刻只给得到 A 的标题）');

  // ---- 2) 标题 settle：Chrome 报 changeInfo.title ----
  TAB_TITLE[TID] = T_B;
  L.tabUpdated(TID, { title: T_B }, { id: TID, url: B, title: T_B });
  await pause();

  const bPushes2 = pushesFor(B);
  eq(bPushes2.length, 2, '★ 标题就绪后必须**补推**一次（否则条目永远停在上一页标题）');
  eq(bPushes2[1].items[0].page_title, T_B, '★ 补推带的是 B 自己的标题');
  eq(bPushes2[1].items[0].url, B, '补推的 URL 仍是同一页（不新增条目）');
  eq(bPushes2[1].items[0].kind_hint, 'page', '补推同样是「视频页」条目');

  // ---- 3) 同一页重复同一标题：不重复打扰 ----
  L.tabUpdated(TID, { title: T_B }, { id: TID, url: B, title: T_B });
  await pause();
  eq(pushesFor(B).length, 2, '★ 同页同标题不再补推（标题抖动不刷屏）');

  // 换页到 C：即便标题暂时还是 B 的，也必须推（key 变了）
  TAB_URL[TID] = C;
  L.tabUpdated(TID, { url: C }, { id: TID, url: C, title: T_B });
  await pause();
  eq(pushesFor(C).length, 1, '★ 换页必推（去重口径是「同页 + 同标题」）');

  // ---- 4) 非视频页的标题变化：一个请求都不发 ----
  const n0 = pushes.length;
  L.tabUpdated(TID, { title: '订阅 - YouTube' },
    { id: TID, url: 'https://www.youtube.com/feed/subscriptions', title: '订阅 - YouTube' });
  await pause();
  eq(pushes.length, n0, '★ 非视频页标题变化不推');

  // ---- 5) 内容脚本哨兵 why='player'：绕过冷却补推正确标题 ----
  const t1 = pushes.length;
  const seen = ask({ type: 'pageSeen', url: C, title: 'C 已就绪的标题', why: 'player' },
    { tab: { id: TID, url: C, title: 'C 已就绪的标题' } });
  await pause();
  eq(seen.ok, true, 'pageSeen(player) 回执 ok');
  ok(pushes.length > t1, '★ 播放器就绪（同页、冷却期内）也要把标题补推出去');
  eq(lastPush().items[0].page_title, 'C 已就绪的标题', '补推的是播放器就绪时的准确标题');

  // ---- 6) 回归：哨兵不带 why 时仍遵守 5 分钟冷却 ----
  const t2 = pushes.length;
  ask({ type: 'pageSeen', url: C, title: 'C 已就绪的标题' }, { tab: { id: TID, url: C } });
  await pause();
  eq(pushes.length, t2, '★ 普通 pageSeen 同页重复上报不重复推（原冷却语义不变）');

  // ---- 7) 关掉嗅探开关：标题变化也不许推 ----
  ask({ type: 'setEnabled', value: false }, {}, () => {});
  const t3 = pushes.length;
  L.tabUpdated(TID, { title: '关了开关也不许推 - YouTube' }, { id: TID, url: C, title: '关了开关也不许推 - YouTube' });
  await pause();
  eq(pushes.length, t3, '★ 关掉嗅探开关后，标题变化同样不推（开关是唯一总闸）');

  console.log('\n嗅探标题滞后回归测试：通过 ' + passes + '，失败 ' + failures);
  process.exit(failures ? 1 : 0);
})();
