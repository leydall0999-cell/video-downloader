/* 扩展「默认自动共享到云端」测试（node 直跑，DOM/chrome 用桩）。
 *
 * 产品策略（2026-10-18 定）：登录态默认自动推到云端公共池，与桌面 App 的
 * 「启动推一次 + 每 30 分钟」同节奏。三条要害：
 *   ① 默认（用户没动过）就要推 —— popup.html 的 ckAgree 必须带 checked，
 *      且 autoPush 把 storage 里的 undefined 当「开启」（老用户升级不会静默停）。
 *   ② 30 分钟节流 —— background 的 alarm、onStartup、popup 补推三处共用
 *      同一个 storage.local 时间戳，任何一处推过，其余都不再推（否则撞云端限流）。
 *   ③ 用户取消勾选 → 既不推、也不写时间戳（写了会白压 30 分钟，用户以为坏了）。
 *
 * 节流/开关存在 chrome.storage.local（不是 localStorage）：background 是
 * service worker，没有 localStorage，两处必须共用 storage.local 才能互认。
 *
 * 运行：node extension/tests/test_cookie_autopush.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

let PASS = 0;
let FAIL = 0;
function check(name, cond, extra) {
  if (cond) { PASS++; console.log('  ✅ ' + name); }
  else { FAIL++; console.log('  ❌ ' + name + (extra !== undefined ? '  → ' + JSON.stringify(extra) : '')); }
}

const HARVEST = fs.readFileSync(path.join(__dirname, '..', 'cookie-harvest.js'), 'utf8');
const POPUP = fs.readFileSync(path.join(__dirname, '..', 'popup.js'), 'utf8');
const HTML = fs.readFileSync(path.join(__dirname, '..', 'popup.html'), 'utf8');

// ---------------- DOM 桩 ----------------
const REGISTRY = [];
function mkNode(tag, id) {
  const el = {
    tagName: (tag || 'div').toUpperCase(), id: id || '',
    textContent: '', className: '', hidden: false, checked: false, open: false,
    value: '', title: '', disabled: false, style: {}, children: [], _handlers: {},
    addEventListener(t, fn) { (this._handlers[t] = this._handlers[t] || []).push(fn); },
    fire(t, ev) { (this._handlers[t] || []).forEach((fn) => fn(ev || { target: this })); },
    appendChild(c) { this.children.push(c); return c; },
    removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); return c; },
    querySelector: () => null,
    querySelectorAll: () => [],
    setAttribute: () => {},
    getAttribute: () => null,
  };
  Object.defineProperty(el, 'innerHTML', {
    get() { return this._html || ''; },
    set(v) {
      this._html = v;
      this.children.forEach((c) => { const i = REGISTRY.indexOf(c); if (i >= 0) REGISTRY.splice(i, 1); });
      this.children = [];
    },
  });
  REGISTRY.push(el);
  return el;
}

const ELS = {};
function byId(id) {
  if (!ELS[id]) ELS[id] = mkNode('div', id);
  return ELS[id];
}

globalThis.document = {
  getElementById: byId,
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: (t) => mkNode(t),
  body: mkNode('body'),
};

// ---------------- chrome.storage.local 桩（节流/开关真源） ----------------
const LOCAL = new Map();
function storageGet(keys, cb) {
  const out = {};
  const list = Array.isArray(keys) ? keys
    : (typeof keys === 'string' ? [keys] : Object.keys(keys || {}));
  list.forEach((k) => { if (LOCAL.has(k)) out[k] = LOCAL.get(k); });
  setTimeout(() => cb(out), 0);
}
function storageSet(obj, cb) {
  Object.keys(obj || {}).forEach((k) => LOCAL.set(k, obj[k]));
  if (cb) setTimeout(cb, 0);
}

// ---------------- chrome / fetch 桩 ----------------
let COOKIE_STORE = {};
const PUSHED = [];

function makeChrome() {
  return {
    runtime: {
      lastError: null,
      sendMessage: (msg, cb) => {
        const t = msg && msg.type;
        if (t === 'getEndpoint') { if (cb) cb({ endpoint: 'http://127.0.0.1:8321' }); return; }
        if (cb) cb({});
      },
    },
    storage: { local: { get: storageGet, set: storageSet } },
    tabs: { query: (q, cb) => cb([{ id: 1, url: 'https://www.bilibili.com/video/BV1xx', title: 'B站' }]) },
    cookies: { getAll: (query, cb) => cb(COOKIE_STORE[query.domain] || []) },
  };
}

globalThis.fetch = (url) => {
  PUSHED.push(String(url));
  const body = '{"ok":true,"added":true,"verified":true}';
  return Promise.resolve({
    ok: true, status: 200,
    json: () => Promise.resolve(JSON.parse(body)),
    text: () => Promise.resolve(body),
  });
};

const tick = (ms) => new Promise((r) => setTimeout(r, ms || 20));

/** HTML 里 ckAgree 是否带 checked —— 默认值以真文件为准，别测桩的默认。 */
const HTML_DEFAULT_CHECKED = /id="ckAgree"[^>]*\bchecked\b/.test(HTML);

async function boot() {
  REGISTRY.length = 0;
  Object.keys(ELS).forEach((k) => delete ELS[k]);
  PUSHED.length = 0;
  globalThis.chrome = makeChrome();
  const ctx = vm.createContext(globalThis);
  vm.runInContext(HARVEST, ctx, { filename: 'cookie-harvest.js' });
  vm.runInContext(POPUP, ctx, { filename: 'popup.js' });
  const m = HTML.match(/id="ckSummary"[^>]*>([^<]*)</);
  byId('ckSummary').textContent = m ? m[1] : '';
  byId('ckAgree').checked = HTML_DEFAULT_CHECKED;
  await tick(30);   // 等 popup 读 storage 里的开关（若为 false 会改 checked）
  return ctx;
}

function openZoneAndScan() {
  const zone = byId('ckZone');
  zone.open = true;
  zone.fire('toggle');
}

const contribCount = () => PUSHED.filter((u) => u.indexOf('/api/cookie/contribute') !== -1).length;
const BILI = {
  'bilibili.com': [{ name: 'SESSDATA', value: 'sess-value-long-enough', domain: '.bilibili.com', path: '/' }],
};

(async function run() {
  // ---- 1) 默认值：HTML 必须默认勾选 ----
  check('popup.html 的 ckAgree 默认带 checked', HTML_DEFAULT_CHECKED,
    HTML.match(/<input[^>]*id="ckAgree"[^>]*>/));

  // ---- 2) 默认（storage 里没有任何键）→ 展开面板自动推一次 ----
  COOKIE_STORE = BILI;
  LOCAL.clear();
  await boot();
  check('storage 没键时勾选框保持默认选中', byId('ckAgree').checked === true);
  openZoneAndScan();
  await tick(150);
  check('默认状态下扫描后自动推了云端', contribCount() === 1, PUSHED);
  check('推送后写入节流时间戳', typeof LOCAL.get('vdlCookieLastPush') === 'number', LOCAL.get('vdlCookieLastPush'));

  // ---- 3) 30 分钟内再开面板（模拟 background 刚推过）→ 不重复推 ----
  PUSHED.length = 0;
  await boot();
  openZoneAndScan();
  await tick(150);
  check('30 分钟内再开面板不重复推', contribCount() === 0, PUSHED);

  // ---- 4) 超过 30 分钟 → 再推一次 ----
  PUSHED.length = 0;
  LOCAL.set('vdlCookieLastPush', Date.now() - 31 * 60 * 1000);
  await boot();
  openZoneAndScan();
  await tick(150);
  check('超过 30 分钟后会再推一次', contribCount() === 1, PUSHED);

  // ---- 5) 用户显式取消（storage 里 false）→ 不推、不写时间戳 ----
  PUSHED.length = 0;
  LOCAL.clear();
  LOCAL.set('vdlCookieShare', false);
  await boot();
  check('storage 明确关闭时勾选框变未选中', byId('ckAgree').checked === false);
  openZoneAndScan();
  await tick(150);
  check('关闭后不自动推', contribCount() === 0, PUSHED);
  check('关闭后不写节流时间戳', !LOCAL.has('vdlCookieLastPush'), LOCAL.get('vdlCookieLastPush'));
  check('关闭后云端按钮灰着', byId('ckPushCloud').disabled === true);
  byId('ckPushCloud').fire('click');
  await tick(80);
  check('关闭后点按钮也不发云端请求', contribCount() === 0, PUSHED);

  // ---- 6) 勾选框改动要写回 storage（background 读的就是这个键） ----
  PUSHED.length = 0;
  LOCAL.clear();
  await boot();
  byId('ckAgree').checked = false;
  byId('ckAgree').fire('change');
  await tick(40);
  check('取消勾选写回 storage=true→false', LOCAL.get('vdlCookieShare') === false, LOCAL.get('vdlCookieShare'));
  byId('ckAgree').checked = true;
  byId('ckAgree').fire('change');
  await tick(40);
  check('重新勾选写回 storage=false→true', LOCAL.get('vdlCookieShare') === true, LOCAL.get('vdlCookieShare'));

  // ---- 7) 只有「仅本机」的站（微博）→ 默认勾选也不推云端，且不写时间戳 ----
  COOKIE_STORE = {
    'weibo.com': [{ name: 'SUB', value: 'weibo-sub-value-long', domain: '.weibo.com', path: '/' }],
  };
  PUSHED.length = 0;
  LOCAL.clear();
  await boot();
  openZoneAndScan();
  await tick(150);
  check('只有非白名单站时不自动推云端', contribCount() === 0, PUSHED);
  check('没有可推的站时不写节流时间戳', !LOCAL.has('vdlCookieLastPush'));

  console.log('\n' + (FAIL === 0 ? '✅ 全部通过' : '❌ 有失败') + '：' + PASS + ' passed, ' + FAIL + ' failed');
  process.exit(FAIL === 0 ? 0 : 1);
})();
