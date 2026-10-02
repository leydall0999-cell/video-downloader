/* 嗅探条目「标题 / 时长 / 大小」回归测试（2026-10-02 用户「这里把嗅探到的视频信息也加上，
 * 比如：标题、时长、大小」）。
 *
 * 三样信息的来源各不相同，测试要分别钉住：
 *   标题     = tab.title（pagewatch.js 的 document.title / tabs.get），服务端 page 条目「后到覆盖」；
 *   时长     = **页面侧** <video>.duration —— 只有 pagewatch.js 读得到（SABR 站的后台
 *              webRequest 既抓不到媒体流、也没有 Content-Duration 头）；
 *   大小     = 响应头 Content-Length（sniff-core.pickHeaders 已解析）→ item.size。
 *
 * 关键行为（去掉任一条，用户看到的就是「有时长有时没有 / 一直都是 0」）：
 *   ① onCompleted 的 Content-Length 必须**传进 store.add**（否则 pickHeaders 白解析）；
 *   ② pushToServer / pushPageToServer 的载荷必须**带上** size / duration；
 *   ③ 视频页的时长是「后到值」（播放器就绪才有）→ refreshPageTitle 的去重必须
 *      **连 duration 一起比**，否则「标题同、时长刚拿到」的补推会被当成重复丢掉。
 *
 * 跑的是**真实 background.js + 真实 sniff-core.js**（chrome.* 全部打桩）。
 * 为什么不上真浏览器：见 test_background_page_scope.js 顶部（本机沙盒里 CFT 连本机 HTTP 不通）。
 *
 * 运行：node extension/tests/test_sniff_meta.js
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

// ---- chrome.* 桩（与 test_page_title_lag.js 同款）----
const L = {};
const pushes = [];             // 收到的 /api/sniffer/ext-push 载荷
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
      if (cb2) cb2([{ id: 11, url: TAB_URL[11] || '', title: TAB_TITLE[11] || '' }]);
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
    getManifest: () => ({ version: '1.0.47' }),
    onMessage: { addListener: (fn) => { L.message = fn; } },
  },
  alarms: { create: () => {}, onAlarm: { addListener: (fn) => { L.alarm = fn; } } },
};

globalThis.importScripts = () => { require(path.join(__dirname, '..', 'sniff-core.js')); };
vm.runInThisContext(
  fs.readFileSync(path.join(__dirname, '..', 'background.js'), 'utf8'),
  { filename: 'background.js' });

const CORE = globalThis.VDLSniffCore;
ok(CORE && typeof CORE.SniffStore === 'function', 'sniff-core 已随 background.js 载入');

const TID = 11;
const PAGE = 'https://www.youtube.com/watch?v=METAMETA001';
const PAGE2 = 'https://www.youtube.com/watch?v=METAMETA002';
const T_TITLE = '（5）某个视频 - YouTube';
const MP4 = 'https://cdn.example/video/meta-flow.mp4';

const pause = () => new Promise((r) => setTimeout(r, 25));
const lastPush = () => pushes[pushes.length - 1];
const pushesFor = (u) => pushes.filter((p) => p.items && p.items[0] && p.items[0].url === u);
function ask(payload, sender) {
  let got = null;
  L.message(payload, sender || {}, (r) => { got = r; });
  return got;
}

(async function () {
  // ---- 1) sniff-core：Content-Length → item.size，且「已有值不被后到的覆盖」----
  var st = new CORE.SniffStore(50);
  var r1 = st.add({ url: 'https://cdn.x.com/a.mp4', mime: 'video/mp4', contentLength: 1024 });
  eq(r1.item.size, 1024, '★ sniff-core：Content-Length 落进 item.size');
  st.add({ url: 'https://cdn.x.com/a.mp4', mime: 'video/mp4', contentLength: 0 });
  eq(r1.item.size, 1024, '重复 add 不把已有 size 清零');
  st.add({ url: 'https://cdn.x.com/a.mp4', mime: 'video/mp4', contentLength: 9999 });
  eq(r1.item.size, 1024, '已有 size 不被后到的值覆盖');
  var r2 = st.add({ url: 'https://cdn.x.com/b.mp4', mime: 'video/mp4' });
  eq(r2.item.size, 0, '没给 Content-Length → size=0（未知，前端据此隐藏）');

  // ---- 2) 直链条目：响应头 Content-Length 一路推到桌面端 ----
  L.sendHeaders({
    requestId: 'r1', url: MP4, initiator: PAGE,
    requestHeaders: [{ name: 'Referer', value: PAGE }],
  });
  L.completed({
    requestId: 'r1', url: MP4, type: 'xmlhttprequest', tabId: TID, initiator: PAGE,
    responseHeaders: [
      { name: 'Content-Type', value: 'video/mp4' },
      { name: 'Content-Length', value: '123456' },
    ],
  });
  await pause();
  const p1 = pushesFor(MP4);
  eq(p1.length, 1, '直链嗅到即推送 1 次');
  eq(p1[0].items[0].size, 123456, '★ 推送载荷带上 size（字节）');
  eq(p1[0].items[0].kind_hint, 'media', 'kind_hint 仍是 media');
  eq(p1[0].items[0].duration, undefined, '直链条目不臆造 duration（页面侧才有）');

  // ---- 3) 视频页：首推没有时长，播放器就绪后补推带上时长 ----
  TAB_URL[TID] = PAGE; TAB_TITLE[TID] = T_TITLE;
  L.tabUpdated(TID, { url: PAGE }, { id: TID, url: PAGE, title: T_TITLE });
  await pause();
  eq(pushesFor(PAGE).length, 1, '视频页首推 1 次');
  eq(pushesFor(PAGE)[0].items[0].page_title, T_TITLE, '标题随首推送达');
  eq(pushesFor(PAGE)[0].items[0].duration, 0, '首推还没时长（播放器未就绪）→ 0=未知');

  const seen = ask(
    { type: 'pageSeen', url: PAGE, title: T_TITLE, why: 'player', duration: 3725.4 },
    { tab: { id: TID, url: PAGE, title: T_TITLE } });
  await pause();
  eq(seen.ok, true, 'pageSeen(player) 回执 ok');
  const pg = pushesFor(PAGE);
  eq(pg.length, 2, '★ 播放器就绪补推一次');
  eq(pg[1] && pg[1].items[0].duration, 3725.4, '★ 补推载荷带上的就是页面读到的时长');

  // ---- 4) 同页同标题、时长新到 → ★ 仍必须补推（去重口径含 duration）----
  const seen2 = ask(
    { type: 'pageSeen', url: PAGE, title: T_TITLE, why: 'player', duration: 42 },
    { tab: { id: TID, url: PAGE, title: T_TITLE } });
  await pause();
  eq(seen2.ok, true, '时长变化同样回 ok');
  eq(pushesFor(PAGE).length, 3,
    '★ 同页同标题但时长变了 → 必须补推（否则列表里的时长永远停在旧值）');
  eq(lastPush().items[0].duration, 42, '补推的是新的时长');

  // ---- 5) 同页同标题同时长 → 不重复打扰 ----
  const n5 = pushes.length;
  ask({ type: 'pageSeen', url: PAGE, title: T_TITLE, why: 'player', duration: 42 },
    { tab: { id: TID, url: PAGE, title: T_TITLE } });
  await pause();
  eq(pushes.length, n5, '★ 同页同标题同时长不再补推（播放器反复 tick 不刷屏）');

  // ---- 6) getState 把当前页时长带给 popup（popup 没有 scripting 权限，读不了 <video>）----
  let snap = null;
  L.message({ type: 'getState', tabId: TID }, {}, (r) => { snap = r; });
  await pause();
  ok(snap, 'getState 有响应');
  eq(snap.videoDuration, 42, '★ popup 能从 getState 拿到当前页时长');

  // ---- 7) 换页：新页还没就绪 → 时长归零，不沿用上一页的值 ----
  TAB_URL[TID] = PAGE2;
  L.tabUpdated(TID, { url: PAGE2 }, { id: TID, url: PAGE2, title: T_TITLE });
  await pause();
  eq(pushesFor(PAGE2).length, 1, '换页必推（新条目）');
  eq(pushesFor(PAGE2)[0].items[0].duration, 0, '★ 新页首推时长未知（0），不沿用上一页');

  // ---- 8) 关掉开关：元信息一样不许往外推 ----
  ask({ type: 'setEnabled', value: false }, {}, () => {});
  const n8 = pushes.length;
  ask({ type: 'pageSeen', url: PAGE2, title: T_TITLE, why: 'player', duration: 99 },
    { tab: { id: TID, url: PAGE2, title: T_TITLE } });
  L.completed({
    requestId: 'r2', url: 'https://cdn.example/video/after-off.mp4', type: 'xmlhttprequest',
    tabId: TID, initiator: PAGE2,
    responseHeaders: [{ name: 'Content-Type', value: 'video/mp4' }, { name: 'Content-Length', value: '777' }],
  });
  await pause();
  eq(pushes.length, n8, '★ 关掉嗅探开关后，时长/大小同样不推（开关是唯一总闸）');

  console.log('\n嗅探条目元信息测试：通过 ' + passes + '，失败 ' + failures);
  process.exit(failures ? 1 : 0);
})();
