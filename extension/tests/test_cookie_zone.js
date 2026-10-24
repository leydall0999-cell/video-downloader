/* 扩展 popup「登录态同步区」行为测试（node 直跑，DOM/chrome 用桩）。
 *
 * 被测要害（真实用户会踩的三个点，全都不是「函数返回对不对」能覆盖的）：
 *   ① 按钮什么时候可点：扫到已登录平台但按钮仍灰着 → 用户认为功能坏了；
 *      没勾同意就能点「贡献到云端池」→ 共享语义被绕过。
 *   ② 云端白名单外的站（微博/爱奇艺）：必须只能推本机，不能让用户点了吃 400。
 *   ③ 缺 cookies 权限（manifest 加权限后 Chrome 会停用扩展）：文案必须指向真实原因，
 *      不能显示成「没有已登录的平台」——那会让用户去挨个重新登录。
 *
 * 运行：node extension/tests/test_cookie_zone.js
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
const IDS = ['list', 'endpointText', 'sniffToggle', 'carryCookie', 'sendQuality',
  'loginWarn', 'segNote', 'sendNote', 'reDetect', 'sendAll', 'sendPage', 'clearAll',
  'ckZone', 'ckSummary', 'ckScan', 'ckList', 'ckAgree', 'ckPushLocal', 'ckPushCloud', 'ckMsg'];
function byId(id) {
  if (!ELS[id]) ELS[id] = mkNode('div', id);
  return ELS[id];
}
// 扫描出来的行（ckRender 创建的 .ckz-row）
function rows() { return REGISTRY.filter((e) => e.className && e.className.indexOf('ckz-row') === 0); }

const documentStub = {
  getElementById: byId,
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: (t) => mkNode(t),
  body: mkNode('body'),
};

// ---------------- chrome 桩 ----------------
let COOKIE_STORE = {};
let COOKIE_API = true;      // false = 模拟「没有 cookies 权限」
let ENDPOINT = 'http://127.0.0.1:8321';
let FETCH_OK = true;
const PUSHED = [];          // 记录推送请求，断言「点了才发、发到哪」

function makeChrome() {
  const c = {
    runtime: {
      lastError: null,
      sendMessage: (msg, cb) => {
        const t = msg && msg.type;
        if (t === 'getEndpoint') { if (cb) cb({ endpoint: ENDPOINT }); return; }
        if (t === 'getState') {
          if (cb) cb({
            enabled: true, carryCookie: true, endpoint: ENDPOINT,
            items: [], segments: [], sentUrls: {}, videoPage: '',
          });
          return;
        }
        if (cb) cb({});
      },
    },
    storage: { local: { get: (k, cb) => cb({}), set: (o, cb) => { if (cb) cb(); } } },
    tabs: { query: (q, cb) => cb([{ id: 1, url: 'https://www.bilibili.com/video/BV1xx', title: 'B站' }]) },
  };
  if (COOKIE_API) {
    c.cookies = { getAll: (query, cb) => cb(COOKIE_STORE[query.domain] || []) };
  }
  return c;
}

globalThis.document = documentStub;
globalThis.fetch = (url) => {
  PUSHED.push(String(url));
  const body = FETCH_OK
    ? '{"ok":true,"added":true,"verified":true,"desktop_logged_in":true,"state":"ok"}'
    : '{"detail":"Cookie 无效，未能通过目标站验真"}';
  return Promise.resolve({
    ok: FETCH_OK, status: FETCH_OK ? 200 : 400,
    json: () => Promise.resolve(JSON.parse(body)),
    text: () => Promise.resolve(body),
  });
};

const tick = (ms) => new Promise((r) => setTimeout(r, ms || 20));

async function boot() {
  REGISTRY.length = 0;
  Object.keys(ELS).forEach((k) => delete ELS[k]);
  PUSHED.length = 0;
  globalThis.chrome = makeChrome();
  const ctx = vm.createContext(globalThis);
  vm.runInContext(HARVEST, ctx, { filename: 'cookie-harvest.js' });
  vm.runInContext(POPUP, ctx, { filename: 'popup.js' });
  // 桩默认 textContent 是空串，而真 DOM 里摘要带着 HTML 里的占位文案。
  // 不补这一句，「未扫描时提示去扫描」就变成在测空字符串（假红）；
  // 补的方式是从 popup.html 真读出来 —— 这样占位文案被删掉时断言仍会红。
  const html = fs.readFileSync(path.join(__dirname, '..', 'popup.html'), 'utf8');
  const m = html.match(/id="ckSummary"[^>]*>([^<]*)</);
  byId('ckSummary').textContent = m ? m[1] : '';
  await tick();
  return ctx;
}

function openZoneAndScan() {
  const zone = byId('ckZone');
  zone.open = true;
  zone.fire('toggle');
}

(async function run() {
  // ---- 1) 初始态：未扫描 → 两个按钮都灰着，别让用户以为能点 ----
  COOKIE_STORE = {
    'bilibili.com': [
      { name: 'SESSDATA', value: 'sess-value-long-enough', domain: '.bilibili.com', path: '/' },
      { name: 'buvid3', value: 'guest', domain: '.bilibili.com', path: '/' },
    ],
  };
  COOKIE_API = true;
  ENDPOINT = 'http://127.0.0.1:8321';
  await boot();
  check('未扫描时「存到我自己的 App」灰着', byId('ckPushLocal').disabled === true);
  check('未扫描时「贡献到云端池」灰着', byId('ckPushCloud').disabled === true);
  check('未扫描时摘要提示去扫描', /扫描/.test(byId('ckSummary').textContent), byId('ckSummary').textContent);

  // ---- 2) 扫描：B站已登录 → 本机按钮解禁；未勾同意 → 云端仍灰 ----
  openZoneAndScan();
  await tick();
  const r = rows();
  // 2026-10-23：新增百度网盘 ⇒ 13 行
  check('扫出 13 行平台', r.length === 13, r.length);
  check('已登录行排在第一位', r[0] && /哔哩哔哩/.test(r[0].children[0].textContent), r[0] && r[0].children[0].textContent);
  check('摘要显示已登录平台数', /1 个平台已登录/.test(byId('ckSummary').textContent), byId('ckSummary').textContent);
  check('本机按钮已解禁', byId('ckPushLocal').disabled === false);
  check('未勾同意时云端按钮仍灰', byId('ckPushCloud').disabled === true);
  check('云端按钮 title 说明要先勾同意', /同意/.test(byId('ckPushCloud').title), byId('ckPushCloud').title);

  // 微博/爱奇艺必须标「仅本机」
  const weiboRow = r.find((x) => x.children[0] && /微博/.test(x.children[0].textContent));
  check('微博标「仅本机」', !!weiboRow && weiboRow.children.some((c) => c.textContent === '仅本机'),
    weiboRow && weiboRow.children.map((c) => c.textContent));

  // ---- 3) 勾同意 → 云端解禁 ----
  byId('ckAgree').checked = true;
  byId('ckAgree').fire('change');
  check('勾同意后云端按钮解禁', byId('ckPushCloud').disabled === false);

  // ---- 4) 点「存到我自己的 App」→ 只发本机端点，并按成功数汇总 ----
  byId('ckPushLocal').fire('click');
  await tick(60);
  check('本机推送打到 /api/cookie/browser-push',
    PUSHED.some((u) => u.indexOf('/api/cookie/browser-push') !== -1), PUSHED);
  check('本机推送未触发云端接口', !PUSHED.some((u) => u.indexOf('/api/cookie/contribute') !== -1), PUSHED);
  check('成功后给绿字汇总', byId('ckMsg').className.indexOf('is-ok') !== -1 && /已同步 1 个平台/.test(byId('ckMsg').textContent),
    { cls: byId('ckMsg').className, txt: byId('ckMsg').textContent });
  check('推送结束后按钮恢复可点', byId('ckPushLocal').disabled === false);

  // ---- 5) 点「贡献到云端池」→ 打到云端，且只推 cloud=true 的站 ----
  PUSHED.length = 0;
  byId('ckPushCloud').fire('click');
  await tick(60);
  check('云端推送打到 /api/cookie/contribute',
    PUSHED.some((u) => u.indexOf('https://hanyuxz.top/api/cookie/contribute') !== -1), PUSHED);
  check('云端推送只发一次（微博不算）', PUSHED.filter((u) => u.indexOf('/api/cookie/contribute') !== -1).length === 1, PUSHED);

  // ---- 6) 只登录了微博（cloud=false）→ 即便勾了同意，云端按钮也必须灰着 ----
  COOKIE_STORE = {
    'weibo.com': [{ name: 'SUB', value: 'weibo-sub-value-long', domain: '.weibo.com', path: '/' }],
  };
  await boot();
  byId('ckAgree').checked = true;
  byId('ckAgree').fire('change');
  openZoneAndScan();
  await tick();
  check('只有非白名单站时本机按钮可用', byId('ckPushLocal').disabled === false);
  check('只有非白名单站时云端按钮保持灰色', byId('ckPushCloud').disabled === true);
  check('云端按钮 title 说明只能推本机', /只能同步到本机/.test(byId('ckPushCloud').title), byId('ckPushCloud').title);

  // ---- 7) 缺 cookies 权限 → 文案指向真实原因（重新启用扩展），不是「未登录」 ----
  COOKIE_API = false;
  COOKIE_STORE = {};
  await boot();
  openZoneAndScan();
  await tick();
  check('缺权限时摘要提示重新启用扩展', /缺少 Cookie 权限/.test(byId('ckSummary').textContent), byId('ckSummary').textContent);
  check('缺权限时摘要用警示色', byId('ckSummary').className.indexOf('is-warn') !== -1, byId('ckSummary').className);
  check('缺权限时本机按钮灰着', byId('ckPushLocal').disabled === true);

  // ---- 8) 没连上桌面端 → 本机按钮灰着且 title 说明要先启动 App ----
  COOKIE_API = true;
  COOKIE_STORE = { 'bilibili.com': [{ name: 'SESSDATA', value: 'sess-value-long-enough', domain: '.bilibili.com', path: '/' }] };
  ENDPOINT = '';
  await boot();
  openZoneAndScan();
  await tick();
  check('无桌面端时本机按钮灰着', byId('ckPushLocal').disabled === true);
  check('无桌面端时 title 指向启动 App', /启动/.test(byId('ckPushLocal').title), byId('ckPushLocal').title);
  // 点它也不能发请求（防「灰按钮其实可编程触发」）
  PUSHED.length = 0;
  byId('ckPushLocal').fire('click');
  await tick(40);
  check('无桌面端时点本机推送不发请求', PUSHED.length === 0, PUSHED);

  // ---- 9) 服务端拒绝 → 必须把 detail 落到界面（不是「失败」两个字） ----
  ENDPOINT = 'http://127.0.0.1:8321';
  FETCH_OK = false;
  await boot();
  openZoneAndScan();
  await tick();
  byId('ckAgree').checked = true;
  byId('ckAgree').fire('change');
  byId('ckPushCloud').fire('click');
  await tick(60);
  check('失败时红字显示服务端原因', byId('ckMsg').className.indexOf('is-err') !== -1 && /验真/.test(byId('ckMsg').textContent),
    { cls: byId('ckMsg').className, txt: byId('ckMsg').textContent });

  // ---- 10) 读取超时 → 摘要必须说「超时」，不能写成「没有已登录的平台」----
  // （真机踩过：Chrome 的 network service 崩了以后 chrome.cookies 回调永不回来；
  //   写成「没有已登录的平台」会让用户去挨个重新登录，而真正的解法是重试/重启浏览器）
  COOKIE_API = true;
  COOKIE_STORE = {};
  const ctx10 = await boot();
  vm.runInContext(
    'ckItems = VDLCookie.PLATFORMS.slice(0, 3).map(function (p) {' +
    '  return { id: p.id, name: p.name, domain: p.domain, cloud: !!p.cloud,' +
    '           header: "", loggedIn: false, count: 0, names: [], error: "timeout" };' +
    '}); ckScanned = true; ckRender();', ctx10);
  check('全部超时时摘要提示超时', /超时/.test(byId('ckSummary').textContent), byId('ckSummary').textContent);
  check('超时时摘要用警示色', byId('ckSummary').className.indexOf('is-warn') !== -1, byId('ckSummary').className);
  const toRow = rows().find((x) => x.children[0] && /哔哩哔哩/.test(x.children[0].textContent));
  check('超时的行写明「读取超时」',
    !!toRow && toRow.children.some((c) => c.textContent === '读取超时'),
    toRow && toRow.children.map((c) => c.textContent));

  console.log('\n' + (FAIL === 0 ? '✅ 全部通过' : '❌ 有失败') + '：' + PASS + ' passed, ' + FAIL + ' failed');
  process.exit(FAIL === 0 ? 0 : 1);
})();
