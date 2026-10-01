/* extension/background.js 的「按标签页分页」行为测试（node 直跑，chrome.* 用桩）。
 *
 * 为什么不用真浏览器：本机沙盒里的 Chrome for Testing 连本机 HTTP 服务**完全不通**
 * （127.0.0.1 / LAN IP / localhost / nip.io / host.docker.internal 五种形态实测
 * 都是请求挂在 pending、服务端零日志），真浏览器 E2E 在本环境不可用。
 * 所以改用 vm.runInThisContext 跑**真实的 background.js 源码**，只把 chrome.* 换成桩——
 * 被测的是真代码，桩只负责把事件喂进去。
 *
 * 覆盖（本次「保存的信息太多了，保存当前页的就行」的验收点）：
 *   1. 主文档请求 = 换页信号 → 清掉该标签页上一页的条目；
 *   2. 不同标签页互不串（分库）；
 *   3. 仅 hash 变化不算换页，不误清；
 *   4. 标签页关闭 → 丢库；恢复时只删不存在的库；
 *   5. getState / clear 按 tabId 取数与清理，不带 tabId 时回落最近激活标签页。
 *
 * 运行：node extension/tests/test_background_page_scope.js
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

// ---- chrome.* 桩 ----
const L = {};                  // 捕获 background.js 注册的监听器
const sessionSet = [];         // 记录 storage.session.set 的载荷
const fetchCalls = [];         // 记录 fetch 请求（真实网络一律不发）
let stubActiveTab = 11;                  // 桩里「当前激活标签页」（= 扩展 updateBadge 查到的）
const TAB_TITLE = { 11: '页面A', 22: '页面B', 33: '页面C' };
const TAB_URL = {};            // tabId -> 该标签页当前 URL（tabs.get/query 用）
const DEFAULT_PATH = (id) => 'http://site/p' + id;

// fetch 桩：background.js 的推送 / 心跳都走这里，断言用
globalThis.fetch = (url, opts) => {
  const u = String(url);
  fetchCalls.push({ url: u, body: (opts && opts.body) || '' });
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
    local: {
      get: (_k, cb) => cb({}),
      set: (_o, cb) => { if (cb) cb(); },
      remove: (_k, cb) => { if (cb) cb(); },
    },
    session: {
      get: (_k, cb) => cb({}),
      set: (o, cb) => { sessionSet.push(o); if (cb) cb(); },
    },
  },
  action: { setBadgeText: () => {}, setBadgeBackgroundColor: () => {} },
  tabs: {
    query: (q, cb) => {
      const cb2 = typeof q === 'function' ? q : cb;
      if (cb2) {
        cb2([{
          id: stubActiveTab,
          url: TAB_URL[stubActiveTab] || '',
          title: TAB_TITLE[stubActiveTab] || '',
        }]);
      }
    },
    get: (id, cb) => cb({
      id: id,
      title: TAB_TITLE[id] || '',
      url: TAB_URL[id] || DEFAULT_PATH(id),
    }),
    create: () => {},
    update: () => {},
    onRemoved: { addListener: (fn) => { L.tabRemoved = fn; } },
    onActivated: { addListener: (fn) => { L.tabActivated = fn; } },
    onUpdated: { addListener: (fn) => { L.tabUpdated = fn; } },
  },
  runtime: {
    lastError: null,
    getManifest: () => ({ version: '1.0.40' }),
    onMessage: { addListener: (fn) => { L.message = fn; } },
  },
  alarms: {
    create: () => {},
    onAlarm: { addListener: (fn) => { L.alarm = fn; } },
  },
};

// importScripts 桩：background.js 用它加载判定核心（真实文件）
globalThis.importScripts = () => { require(path.join(__dirname, '..', 'sniff-core.js')); };

// 跑真实 background.js（runInThisContext → 顶层 var 落到 globalThis，便于断言）
const src = fs.readFileSync(path.join(__dirname, '..', 'background.js'), 'utf8');
vm.runInThisContext(src, { filename: 'background.js' });

ok(globalThis.VDLSniffCore && globalThis.VDLSniffCore.TabStores,
  'background.js 已加载 sniff-core（importScripts 桩生效）');
ok(typeof L.completed === 'function' && typeof L.message === 'function',
  'background.js 已注册 onCompleted / onMessage 监听器');
ok(globalThis.store && typeof globalThis.store.add === 'function',
  '分页存储已就位（store 是 TabStores）');

// ---- 事件构造helper ----
let rid = 0;
function complete(tabId, url, opts) {
  opts = opts || {};
  const details = {
    url: url,
    type: opts.type || 'xmlhttprequest',
    tabId: tabId,
    requestId: 'r' + (++rid),
    initiator: opts.initiator || 'http://site/',
    responseHeaders: opts.mime
      ? [{ name: 'Content-Type', value: opts.mime }]
      : [],
  };
  L.completed(details);
}
function mainFrame(tabId, url) {
  complete(tabId, url, { type: 'main_frame', mime: 'text/html' });
  // 桩的标签页标题跟着页面走（真实浏览器里 tabs.get 拿到的就是当前页标题）
  TAB_TITLE[tabId] = url.indexOf('/b.html') >= 0 ? '页面B' : '页面A';
}
function media(tabId, url) { complete(tabId, url, { mime: 'video/mp4' }); }
function ask(payload, sender) {
  let got = null;
  L.message(payload, sender || {}, (r) => { got = r; });
  return got;
}
function urls(tabId) {
  return globalThis.store.list(tabId).map((i) => i.url);
}

// ---- 1) 页面 A：主文档 + 一条媒体 ----
mainFrame(11, 'http://site-a/a.html');
media(11, 'http://cdn.site-a/a1.mp4');
eq(urls(11).length, 1, '页面 A 嗅到 1 条');
eq(globalThis.tabPage['11'], 'http://site-a/a.html', '换页基准记为 A 页');
eq(urls(11)[0], 'http://cdn.site-a/a1.mp4', 'A 页条目是 a1.mp4');

// ---- 2) 另一标签页：分库隔离 ----
mainFrame(22, 'http://site-b/b.html');
media(22, 'http://cdn.site-b/b1.mp4');
media(22, 'http://cdn.site-b/b2.mp4');
eq(urls(22).length, 2, '页面 B 嗅到 2 条');
ok(urls(11).every((u) => u.indexOf('b') < 0 || u.indexOf('b1') < 0),
  '★ 页面 B 的条目没串进页面 A（分库隔离）');
eq(urls(11).length, 1, '★ 开新标签页不影响标签页 11 的数据');

// ---- 3) 同一个标签页换页 → 旧页条目必须清掉 ----
mainFrame(11, 'http://site-b/b.html');
eq(urls(11).length, 0, '★ 换页瞬间：上一页的条目已清空');
media(11, 'http://cdn.site-b/b1.mp4');
media(11, 'http://cdn.site-b/b2.mp4');
eq(urls(11).length, 2, '换页后只记新页的 2 条');
ok(urls(11).every((u) => u.indexOf('a1.mp4') < 0), '★ 换页后旧页条目不会复活');
eq(urls(22).length, 2, '★ 标签页 11 换页不影响标签页 22');

// ---- 4) 仅 hash 变化不算换页 ----
mainFrame(11, 'http://site-b/b.html#frag');
eq(urls(11).length, 2, '★ 仅 hash 变化不误清当前页条目');
eq(globalThis.tabPage['11'], 'http://site-b/b.html', 'hash 被 pageKeyOf 丢掉');

// query 变化算换页
mainFrame(11, 'http://site-b/b.html?p=2');
eq(urls(11).length, 0, 'query 变化算换页（清单跟着页走）');

// ---- 5) 页面标题回填（tabs.get 异步回调） ----
media(11, 'http://cdn.site-b/b9.mp4');
eq(urls(11).length, 1, '换页后新条目进库');
eq(globalThis.store.list(11)[0].pageTitle, '页面B', '条目回填了页面标题');

// ---- 6) getState 按 tabId 取数；不带 tabId 回落最近激活标签页 ----
const st22 = ask({ type: 'getState', tabId: 22 });
eq(st22.items.length, 2, '★ getState(tabId=22) 只返回标签页 22 的 2 条');
ok(st22.items.every((i) => i.url.indexOf('site-b') >= 0), '★ 不混入别的标签页');
eq(st22.tabId, 22, 'getState 回显 tabId');
stubActiveTab = 33;
globalThis.activeTabId = 33;             // 扩展内部的 activeTabId（顶层 var → 全局）
const stNoTab = ask({ type: 'getState' });
eq(stNoTab.tabId, 33, '不带 tabId 时回落到最近激活的标签页');
eq(stNoTab.items.length, 0, '该标签页没有数据 → 空列表（不串别的页）');

// ---- 7) clear 只清当前页 ----
ask({ type: 'clear', tabId: 22 });
eq(urls(22).length, 0, '★ clear(tabId=22) 清掉 22');
eq(urls(11).length, 1, '★ clear 不误伤别的标签页');

// ---- 8) 关页丢库 ----
L.tabRemoved(11);
eq(urls(11).length, 0, '标签页关闭 → 整库丢弃');
ok(!globalThis.tabPage['11'], '标签页关闭 → 换页基准也清掉');

// ---- 9) 恢复时只删不存在的库，不动现存标签页 ----
media(22, 'http://cdn.site-b/keep.mp4');
globalThis.store.dropTabsExcept([22, 33]);
eq(urls(22).length, 1, '★ dropTabsExcept 保住现存标签页的数据（SW 空闲重启不丢当前页）');

// ---- 10) 关嗅探开关 → 全清 ----
L.message({ type: 'setEnabled', value: false }, {}, () => {});
eq(globalThis.store.totalCount(), 0, '关掉嗅探开关时清空全部分库');

// ---- 11) 视频页直推（v1.0.40）：不依赖主文档请求的多入口触发 ----
// 回归场景（2026-10-01 用户实测「YouTube 还是嗅探不到」）：YouTube 换页是 SPA
// （不发主文档请求），用户又常把页面在装/更新扩展前就开着了 → 原来只挂在
// webRequest main_frame 上的判据永不触发，桌面端条目里零 YouTube 记录。
ask({ type: 'setEnabled', value: true }, {}, () => {});   // 第 10 步关过开关，先打开
const trips = () => globalThis.pushStat.tries;

const ytUrl = 'https://www.youtube.com/watch?v=abc12345678&list=RDxyz&index=6';
const t0 = trips();
const seen = ask({ type: 'pageSeen', url: ytUrl, title: '某视频' },
  { tab: { id: 44, url: ytUrl } });
eq(seen.ok, true, '内容脚本哨兵报视频页 → 后台接住');
ok(trips() > t0, '★ 视频页直推被触发（推送记账 +1）');
eq(globalThis.pushStat.pageTries, 1, '页面直推计数 +1');
eq(globalThis.pagePushed[44] && globalThis.pagePushed[44].key,
  'https://www.youtube.com/watch?v=abc12345678',
  '★ 按 tabId 记归一化后的页面（list/index 噪声参数已丢）');

const t1 = trips();
ask({ type: 'pageSeen', url: ytUrl, title: '某视频' }, { tab: { id: 44, url: ytUrl } });
eq(trips(), t1, '★ 同一页重复上报不重复推（5 分钟冷却）');

const t2 = trips();
eq(ask({ type: 'pageSeen', url: 'https://www.youtube.com/feed/subscriptions' },
  { tab: { id: 45 } }).ok, false, '非视频页：哨兵消息不触推');
eq(trips(), t2, '★ 非视频页一个请求都不发');

// 11.3 切标签页 → 早就开着的视频页也会被交出去
TAB_URL[46] = 'https://www.youtube.com/watch?v=zzzzzzzzzzz';
TAB_TITLE[46] = '一直在开着的视频';
const t3 = trips();
L.tabActivated({ tabId: 46 });
ok(trips() > t3, '★ 切到视频页标签页 → 直推触发（覆盖「装扩展前就开着的页」）');

// 11.4 SPA 换页（tabs.onUpdated 的 changeInfo.url，没有主文档请求）
TAB_URL[47] = 'https://www.bilibili.com/video/BV1xx411c7mD';
const t4 = trips();
L.tabUpdated(47, { url: TAB_URL[47] }, { id: 47, url: TAB_URL[47], title: 'B站视频' });
ok(trips() > t4, '★ SPA 换页（changeInfo.url）→ 直推触发');
eq(globalThis.pagePushed[47] && globalThis.pagePushed[47].key, TAB_URL[47],
  'B站视频页按 tabId 记账');

// 11.5 心跳兜底：浏览器挂着不动时也能在 ≤60s 内交出去
stubActiveTab = 48;
TAB_URL[48] = 'https://www.douyin.com/video/7123456789012345678';
TAB_TITLE[48] = '抖音视频';
const t5 = trips();
globalThis.heartbeat();
ok(trips() > t5, '★ 心跳兜底：激活标签页是视频页 → 直推触发');

// 11.6 关掉嗅探开关 → 一律不推；打开后 popup 强推可越过冷却
ask({ type: 'setEnabled', value: false }, {}, () => {});
const t6 = trips();
ask({ type: 'pushPageNow', tabId: 49, url: 'https://www.youtube.com/watch?v=qqqqqqqqqqq' }, {}, () => {});
eq(trips(), t6, '★ 关掉嗅探开关后不再推送');
ask({ type: 'setEnabled', value: true }, {}, () => {});
const t7 = trips();
const nowRes = ask({ type: 'pushPageNow', tabId: 49, url: 'https://www.youtube.com/watch?v=qqqqqqqqqqq' }, {}, () => {});
eq(nowRes.url, 'https://www.youtube.com/watch?v=qqqqqqqqqqq', 'pushPageNow 回显归一化页面');
ok(trips() > t7, '★ 面板打开即强推（用户显式意图，忽略 5 分钟冷却）');

// ---- 12/13) 心跳字段与推送载荷（需要等一拍让异步推送落地）----
(async function () {
  await new Promise((r) => setTimeout(r, 20));

  globalThis.heartbeat();
  const pings = fetchCalls.filter((c) => c.url.indexOf('/api/sniffer/ext-ping') >= 0);
  ok(pings.length > 0, '心跳已发出（fetch 桩记录到 /api/sniffer/ext-ping）');
  const body = JSON.parse(pings[pings.length - 1].body);
  ok('push_ok' in body && 'push_err' in body && 'push_page' in body && 'page_saw' in body,
    '★ 心跳带 push_ok/push_err/push_page/page_saw（判据不再依赖会归零的 pushed）');
  eq(body.version, '1.0.40', '心跳带自报版本（桌面端据此提示「扩展有新版本」）');
  eq(body.page_saw, 3, '哨兵计数与「页面侧上报次数」一致（含被冷却与非法页挡下的两次）');
  eq(body.push_page, globalThis.pushStat.pageTries, '心跳里的页面直推数与本地记账一致');
  ok(body.push_page >= 5, '★ 四条触发入口都真的推了（哨兵/切换/换页/心跳兜底）');

  const pushes = fetchCalls.filter((c) => c.url.indexOf('/api/sniffer/ext-push') >= 0);
  ok(pushes.length >= 5, '★ 推送请求已发出（/api/sniffer/ext-push）');
  const one = JSON.parse(pushes[pushes.length - 1].body);
  eq(one.items[0].kind_hint, 'page', '★ 页面直推带 kind_hint=page（桌面端据此标「视频页」）');
  eq(one.items[0].mime, 'text/html', '页面直推的 mime 为 text/html');

  // ---- 汇总 ----
  console.log('\nbackground 分页行为测试：通过 ' + passes + '，失败 ' + failures);
  process.exit(failures ? 1 : 0);
})();
