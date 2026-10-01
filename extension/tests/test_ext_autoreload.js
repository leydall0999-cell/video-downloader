/* extension/background.js 的「零点击自动更新」行为测试（node 直跑，chrome.* 用桩）。
 *
 * 被测行为（2026-10-02 用户问「扩展程序更新怎么办」→ 做成零点击）：
 *   心跳 POST /api/sniffer/ext-ping 的**响应**里若带回 reload_to（且严格新于自身版本），
 *   扩展调 chrome.runtime.reload() 自己重载自己 —— 用户一次都不用点。
 *
 * 为什么值得写成行为测试而不是只做源码断言：这条链路的三个致命分支都藏在
 * 「不该动」的一侧 —— 同版本重载 = 无限重启、旧版本目标重载 = 自降级、
 * 目标重复重载 = 死循环。源码里 grep 得到「有 reload」，grep 不出「什么时候不 reload」。
 *
 * 运行：node extension/tests/test_ext_autoreload.js
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

// ---- 可变桩状态 ----
let ownVersion = '1.0.43';                 // 桩里「当前装在浏览器里的扩展版本」
let heartbeatPayload = { ok: true };       // 下一次心跳响应（默认：无更新）
const MEM = {};                            // chrome.storage.local 的内存实现
const reloadCalls = [];                    // chrome.runtime.reload() 调用记录
const fetchCalls = [];                     // fetch 请求记录（真实网络一律不发）
const L = {};                              // 捕获 background.js 注册的监听器

const ENDPOINT = 'http://127.0.0.1:8321';
MEM.endpoint = ENDPOINT;                   // 预置端点 → 免走 probeEndpoint 的多端口探测

globalThis.fetch = (url, opts) => {
  const u = String(url);
  fetchCalls.push({ url: u, body: (opts && opts.body) || '' });
  // /status 探测固定返回 idle；心跳返回当前 heartbeatPayload
  const payload = u.indexOf('/api/sniffer/status') >= 0 ? { state: 'idle' } : heartbeatPayload;
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(payload) });
};

function memGet(keys, cb) {
  if (typeof keys === 'function') { cb({}); return; }
  if (keys == null) { cb(Object.assign({}, MEM)); return; }
  const out = {};
  (Array.isArray(keys) ? keys : [keys]).forEach((k) => {
    if (k in MEM) out[k] = MEM[k];
  });
  cb(out);
}
function memSet(obj, cb) {
  Object.keys(obj || {}).forEach((k) => { MEM[k] = obj[k]; });
  if (cb) cb();
}

globalThis.chrome = {
  webRequest: {
    onSendHeaders: { addListener: (fn) => { L.sendHeaders = fn; } },
    onCompleted: { addListener: (fn) => { L.completed = fn; } },
    onErrorOccurred: { addListener: (fn) => { L.errorOccurred = fn; } },
  },
  storage: {
    local: { get: memGet, set: memSet, remove: (_k, cb) => { if (cb) cb(); } },
    session: { get: (_k, cb) => cb({}), set: (_o, cb) => { if (cb) cb(); } },
  },
  action: { setBadgeText: () => {}, setBadgeBackgroundColor: () => {} },
  tabs: {
    query: (q, cb) => {
      const cb2 = typeof q === 'function' ? q : cb;
      if (cb2) cb2([]);                     // 无标签页：restore 的 dropTabsExcept 空跑
    },
    get: (id, cb) => cb({ id: id, url: '', title: '' }),
    create: () => {},
    update: () => {},
    onRemoved: { addListener: (fn) => { L.tabRemoved = fn; } },
    onActivated: { addListener: (fn) => { L.tabActivated = fn; } },
    onUpdated: { addListener: (fn) => { L.tabUpdated = fn; } },
  },
  runtime: {
    lastError: null,
    getManifest: () => ({ version: ownVersion }),
    reload: () => { reloadCalls.push({ at: Date.now() }); },
    onMessage: { addListener: (fn) => { L.message = fn; } },
  },
  alarms: {
    create: () => {},
    onAlarm: { addListener: (fn) => { L.alarm = fn; } },
  },
};

// importScripts 桩：background.js 用它加载判定核心（真实文件）
globalThis.importScripts = () => { require(path.join(__dirname, '..', 'sniff-core.js')); };

// 跑真实 background.js（runInThisContext → 顶层函数/var 落到 globalThis，便于断言）
const src = fs.readFileSync(path.join(__dirname, '..', 'background.js'), 'utf8');
vm.runInThisContext(src, { filename: 'background.js' });

const tick = (ms) => new Promise((r) => setTimeout(r, ms || 8));

(async function main() {
  ok(typeof globalThis.heartbeat === 'function', 'background.js 暴露了 heartbeat()');
  ok(typeof globalThis.maybeAutoReload === 'function', 'background.js 暴露了 maybeAutoReload()');

  // 等模块顶层那次 heartbeat（无 reload_to）跑完，再把计数清零
  await tick();
  reloadCalls.length = 0;

  // ---------- ① 版本比较纯函数（先钉住语义，后面分支都依赖它） ----------
  const nw = globalThis._verNewer;
  if (typeof nw === 'function') {
    ok(nw('1.0.44', '1.0.43') === true, '① 1.0.44 比 1.0.43 新');
    ok(nw('1.0.43', '1.0.44') === false, '① 1.0.43 不比 1.0.44 新');
    ok(nw('1.0.44', '1.0.44') === false, '① 同版本不算新（否则会自重启）');
    ok(nw('1.0.100', '1.0.99') === true, '① 按段比较（100 > 99，非字符串比较）');
    ok(nw('1.1.0', '1.0.99') === true, '① 高位段优先');
    ok(nw('abc', '1.0.43') === false, '① 非法版本一律「不比新」（不触发重载）');
    ok(nw('', '1.0.43') === false, '① 空版本不比新');
  } else {
    ok(false, '① 未找到 _verNewer（版本比较函数缺失）');
  }

  // ---------- ② 有更新 → 重载自己 ----------
  ownVersion = '1.0.43';
  heartbeatPayload = { ok: true, reload_to: '1.0.44' };
  globalThis.heartbeat();
  await tick();
  eq(reloadCalls.length, 1, '② 回传 reload_to=1.0.44（>自身 1.0.43）→ 调 chrome.runtime.reload()');
  ok(MEM.autoReloadTried && MEM.autoReloadTried['1.0.44'],
    '② 已把目标版本记进 storage.local.autoReloadTried（防重复重启）');

  // ---------- ③ 同一目标再来 → 只重载一次（防死循环） ----------
  globalThis.heartbeat();
  globalThis.heartbeat();
  await tick();
  eq(reloadCalls.length, 1, '③ 同一目标版本重复心跳不再重载（否则会无限重启）');

  // ---------- ④ 已是最新（目标 == 自身）→ 不重载 ----------
  ownVersion = '1.0.44';
  heartbeatPayload = { ok: true, reload_to: '1.0.44' };
  globalThis.heartbeat();
  await tick();
  eq(reloadCalls.length, 1, '④ 目标等于自身版本 → 不重载（同版本自重启是死循环）');

  // ---------- ⑤ 目标比自身旧 → 不重载（绝不自降级） ----------
  ownVersion = '1.0.44';
  heartbeatPayload = { ok: true, reload_to: '1.0.43' };
  globalThis.heartbeat();
  await tick();
  eq(reloadCalls.length, 1, '⑤ 目标比自身旧 → 不重载（防被降级回旧版）');

  // ---------- ⑥ 响应没带 reload_to → 不重载 ----------
  ownVersion = '1.0.43';
  heartbeatPayload = { ok: true };
  globalThis.heartbeat();
  await tick();
  eq(reloadCalls.length, 1, '⑥ 响应无 reload_to → 什么都不做（正常心跳路径）');

  // ---------- ⑦ 非法目标版本 → 不重载 ----------
  heartbeatPayload = { ok: true, reload_to: 'abc' };
  globalThis.heartbeat();
  await tick();
  eq(reloadCalls.length, 1, '⑦ 非法目标版本 → 不重载');

  // ---------- ⑧ ok !== true（异常响应）→ 不重载 ----------
  heartbeatPayload = { ok: false, reload_to: '9.9.9' };
  globalThis.heartbeat();
  await tick();
  eq(reloadCalls.length, 1, '⑧ 响应 ok !== true → 不重载（非成功心跳不触发重启）');

  // ---------- ⑨ 心跳确实读的是**响应体**，不是硬编码 ----------
  ok(fetchCalls.some((c) => c.url.indexOf('/api/sniffer/ext-ping') >= 0),
    '⑨ 心跳确实 POST /api/sniffer/ext-ping');

  // ---------- ⑩ 未引入新权限（加了权限静默更新就失效） ----------
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'manifest.json'), 'utf8'));
  const perms = (manifest.permissions || []).concat(manifest.optional_permissions || []);
  ok(!perms.includes('management'), '⑩ manifest 未申请 management 权限');
  ok(!perms.includes('permissions'), '⑩ manifest 未申请 permissions 权限');
  ok(!perms.some((p) => String(p).indexOf('management') === 0),
    '⑩ 没有以 management 开头的权限');

  console.log('\n' + '-'.repeat(64));
  console.log('通过: ' + passes + '  失败: ' + failures);
  if (failures) {
    console.error('（自动更新行为测试失败 —— 用户会遇到「更新了但扩展没生效」或「扩展反复重启」）');
  }
  process.exit(failures ? 1 : 0);
})();
