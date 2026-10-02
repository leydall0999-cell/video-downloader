/* 嗅探条目「标题 / 时长 / 大小」+「图标角标提示」回归测试。
 *
 * 前半段（2026-10-02 用户「这里把嗅探到的视频信息也加上，比如：标题、时长、大小」）：
 * 三样信息的来源各不相同，测试要分别钉住：
 *   标题     = tab.title（pagewatch.js 的 document.title / tabs.get），服务端 page 条目「后到覆盖」；
 *   时长     = **页面侧** <video>.duration —— 只有 pagewatch.js 读得到（SABR 站的后台
 *              webRequest 既抓不到媒体流、也没有 Content-Duration 头）；
 *   大小     = 响应头 Content-Length（sniff-core.pickHeaders 已解析）→ item.size。
 *
 * 后半段（2026-10-02 用户「这里加个提示吧，让用户知道有提取到，不用点进去才知道有没有提取到」）：
 * 角标必须表达**四态**，只数本地 store 是不够的 —— YouTube 这类 SABR 站点的收成不进
 * store（整页交给桌面端解析），旧口径在 YouTube 上永远空着：
 *   数字（蓝）= 本地可下载条目数；✓（绿）= 整页已提取且桌面端确认收到；
 *   !（琥珀）= 提交了但桌面端没响应；''（灰）= 本页没提取到 / 已暂停。
 * 并同步写进 action.setTitle 悬停提示。
 *
 * 关键行为（去掉任一条，用户看到的就是「有时长有时没有 / 一直都是 0」）：
 *   ① onCompleted 的 Content-Length 必须**传进 store.add**（否则 pickHeaders 白解析）；
 *   ② pushToServer / pushPageToServer 的载荷必须**带上** size / duration；
 *   ③ 视频页的时长是「后到值」（播放器就绪才有）→ refreshPageTitle 的去重必须
 *      **连 duration 一起比**，否则「标题同、时长刚拿到」的补推会被当成重复丢掉；
 *   ④ 桌面端回执必须写回 per-tab 记账并刷新角标，否则「提没提取到」无从判断。
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
const SESSION_SETS = [];       // 每次落 storage.session 的载荷快照（JSON 克隆，供时序断言）
const TAB_URL = {};
const TAB_TITLE = {};
let ACTIVE_ID = 11;            // 桩里「当前激活标签页」（updateBadge 查到的那个）
let FETCH_OK = true;           // 控制 /api/sniffer/ext-push 的成败（模拟桌面端没启动）
const BADGE = { text: null, color: null, textColor: null, title: null };   // 角标最后一次设置的值

globalThis.fetch = (url, opts) => {
  const u = String(url);
  const isPush = u.indexOf('/api/sniffer/ext-push') >= 0;
  if (isPush) {
    pushes.push(JSON.parse((opts && opts.body) || '{}'));
  }
  const payload = u.indexOf('/api/sniffer/status') >= 0 ? { state: 'idle' } : { ok: true };
  const good = isPush ? FETCH_OK : true;   // 只有推送给桌面端这一步受 FETCH_OK 控制
  return Promise.resolve({
    ok: good, status: good ? 200 : 500, json: () => Promise.resolve(payload),
  });
};

globalThis.chrome = {
  webRequest: {
    onSendHeaders: { addListener: (fn) => { L.sendHeaders = fn; } },
    onCompleted: { addListener: (fn) => { L.completed = fn; } },
    onErrorOccurred: { addListener: (fn) => { L.errorOccurred = fn; } },
  },
  storage: {
    local: { get: (_k, cb) => cb({}), set: (_o, cb) => { if (cb) cb(); }, remove: (_k, cb) => { if (cb) cb(); } },
    session: {
      get: (_k, cb) => cb({}),
      // 克隆后再存：让断言能证明「这次落盘发生在记账写入**之后**」，而不是拿到
      // 一个还在被后续改动影响的活引用（对象引用会让早期快照也「看起来是对的」）。
      set: (o, cb) => { SESSION_SETS.push(JSON.parse(JSON.stringify(o || {}))); if (cb) cb(); },
    },
  },
  // 角标四件套都记下来：文本 + 底色 + 文字色 + 悬停提示（少一个就有一档状态没法断言）
  action: {
    setBadgeText: (o) => { BADGE.text = (o || {}).text; },
    setBadgeBackgroundColor: (o) => { BADGE.color = (o || {}).color; },
    setBadgeTextColor: (o) => { BADGE.textColor = (o || {}).color; },
    setTitle: (o) => { BADGE.title = (o || {}).title; },
  },
  tabs: {
    query: (q, cb) => {
      const cb2 = typeof q === 'function' ? q : cb;
      if (cb2) cb2([{ id: ACTIVE_ID, url: TAB_URL[ACTIVE_ID] || '', title: TAB_TITLE[ACTIVE_ID] || '' }]);
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
    getManifest: () => ({ version: '1.0.50' }),
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
const TID2 = 22;                       // 「本地没有直链条目」的标签页（YouTube 的常态）
const PAGE = 'https://www.youtube.com/watch?v=METAMETA001';
const PAGE2 = 'https://www.youtube.com/watch?v=METAMETA002';
const T_TITLE = '（5）某个视频 - YouTube';
const MP4 = 'https://cdn.example/video/meta-flow.mp4';

const pause = () => new Promise((r) => setTimeout(r, 25));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
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
  eq(snap.videoTitle, T_TITLE, '★ popup 能拿到当前页视频标题（兜底视图要显示标题）');

  // ---- 6b) 打开面板时的强推（pushPageNow）**不带** duration —— ★ 绝不能把时长抹成 0 ----
  // 2026-10-02 用户截图实测：加密流兜底视图里的「时长」一直是空的，根因就是
  // popup 一打开先发 pushPageNow，把刚由页面侧读到的时长覆盖成了 0。
  ask({ type: 'pushPageNow', tabId: TID, url: PAGE, title: T_TITLE }, {}, () => {});
  await pause();
  let snap2 = null;
  L.message({ type: 'getState', tabId: TID }, {}, (r) => { snap2 = r; });
  await pause();
  eq(snap2.videoDuration, 42, '★ 面板强推之后时长仍是 42（同页保留已知时长，不写 0）');
  eq(snap2.videoTitle, T_TITLE, '强推后标题照常给出');
  eq(snap2.videoPage, PAGE, '强推后仍认得当前视频页');

  // ---- 6c) 记账必须落 storage.session：SW 挂起被重启后，弹窗照样能答出这一页的信息 ----
  //（popup 的强推不带 duration，无从重建；不落盘就永久丢失）
  await sleep(700);   // persist() 是 500ms 节流写入
  ok(SESSION_SETS.some((s) => s && s.pagePushed && s.pagePushed[TID]
      && s.pagePushed[TID].duration === 42),
    '★ pagePushed 记账（含时长）已落 storage.session');

  // ---- 7) 换页：新页还没就绪 → 时长归零，不沿用上一页的值 ----
  TAB_URL[TID] = PAGE2;
  L.tabUpdated(TID, { url: PAGE2 }, { id: TID, url: PAGE2, title: T_TITLE });
  await pause();
  eq(pushesFor(PAGE2).length, 1, '换页必推（新条目）');
  eq(pushesFor(PAGE2)[0].items[0].duration, 0, '★ 新页首推时长未知（0），不沿用上一页');
  let snap3 = null;
  L.message({ type: 'getState', tabId: TID }, {}, (r) => { snap3 = r; });
  await pause();
  eq(snap3.videoDuration, 0, '★ 换页后 getState 的时长归零（等新页播放器就绪再读）');

  // ---- 7b) ★★ 角标 = 「不用点开就知道提取到没有」（v1.0.49）★★ ----
  // 用户原话：「这里加个提示吧，让用户知道有提取到，不用点进去才知道有没有提取到」。
  // 旧口径只数本地 store，而 YouTube/SABR 站点的收成**不进 store**（整页交桌面端解析）
  // → 人在 YouTube 上角标永远空着，必须点开面板才知道。四态都要钉住。

  // (a) 有本地直链条目 → 数字（蓝），悬停提示写清「已提取 N 个」
  ACTIVE_ID = TID;
  L.tabActivated({ tabId: TID });
  await pause();
  eq(BADGE.text, '1', '★ 角标显示本页可下载条目数（本地直链/清单）');
  eq(BADGE.color, '#2563eb', '数字角标用蓝色');
  ok(/已提取 1 个可下载资源/.test(String(BADGE.title)), '★ 悬停提示写明「已提取 N 个」');
  eq(BADGE.textColor, '#ffffff', '角标文字色设为白（保证在彩色底上可读）');

  // (b) 视频页整页交付成功 → ✓（绿）：本地**没有**条目也必须给出「已提取」信号
  ACTIVE_ID = TID2;
  TAB_URL[TID2] = PAGE; TAB_TITLE[TID2] = T_TITLE;
  let s22 = null;
  L.message({ type: 'getState', tabId: TID2 }, {}, (r) => { s22 = r; });
  await pause();
  eq(s22.items.length, 0, '前提：这个标签页本地没有任何直链条目（YouTube/SABR 常态）');
  L.tabActivated({ tabId: TID2 });     // 切过去 → updateBadge + 判视频页 → 推送
  await pause();
  eq(BADGE.text, '✓', '★ 本地无条目但整页已交付桌面端 → 角标 ✓（这才是用户要的「已提取」信号）');
  eq(BADGE.color, '#16a34a', '✓ 用绿色');
  ok(/已提取/.test(String(BADGE.title)), '★ 悬停提示含「已提取」');
  ok(pushesFor(PAGE).length >= 1, '（该页确实发生过推送）');

  // (c) 桌面端没响应（App 没启动）→ !（琥珀），提示去启动 App
  FETCH_OK = false;
  ask({ type: 'pushPageNow', tabId: TID2, url: PAGE, title: T_TITLE }, {}, () => {});
  await pause();
  eq(BADGE.text, '!', '★ 桌面端没响应 → 角标 ! （不用点开就知道「提取没成功」）');
  eq(BADGE.color, '#d97706', '! 用琥珀色');
  ok(/视频工坊/.test(String(BADGE.title)), '★ 悬停提示引导用户确认「视频工坊」是否已启动');

  // (d) 桌面端恢复 → 角标回到 ✓（失败不是终态，别一直挂着 !）
  FETCH_OK = true;
  ask({ type: 'pushPageNow', tabId: TID2, url: PAGE, title: T_TITLE }, {}, () => {});
  await pause();
  eq(BADGE.text, '✓', '★ 桌面端恢复后角标回到 ✓');

  // (e) 非视频页且无条目 → 留空（不打扰），提示也说明白
  TAB_URL[TID2] = 'https://example.com/some/article';
  L.tabActivated({ tabId: TID2 });
  await pause();
  eq(BADGE.text, '', '★ 非视频页 + 无条目 → 角标留空');
  ok(/未提取到可下载内容/.test(String(BADGE.title)), '悬停提示说明本页没有可下载内容');

  // ---- 8) 关掉开关：元信息一样不许往外推（角标同时清零并说明「已暂停」）----
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
  eq(BADGE.text, '', '★ 关掉开关后角标清空（不再假装有提取结果）');
  ok(/已暂停/.test(String(BADGE.title)), '★ 悬停提示显示「已暂停」');

  console.log('\n嗅探条目元信息测试：通过 ' + passes + '，失败 ' + failures);
  process.exit(failures ? 1 : 0);
})();
