/* 扩展「登录态采集」行为测试（node 直跑，不需要浏览器）。
 *
 * 被测要害（为什么要写成行为测试而不是源码 grep）：
 *   ① 拼出来的 Cookie 请求头必须**能直接用**——同名重复、过期项、空值任何一条没滤掉，
 *      服务端验真就会 400，用户看到的是「同步失败」而不是「Cookie 无效」；
 *   ② 「已登录」判据只能用登录后才存在的字段——用 cna/buvid3 这类游客字段判，
 *      会把没登录的站显示成已登录，点同步必被拒（真实用户必踩）；
 *   ③ 云端贡献是**共享**语义，白名单外的站（微博/爱奇艺）必须被 pickReady(cloud) 拦掉，
 *      否则每点一次就吃一个 400。
 *
 * 运行：node extension/tests/test_cookie_harvest.js
 */
'use strict';

const path = require('path');
const vm = require('vm');
const fs = require('fs');

let PASS = 0;
let FAIL = 0;
function check(name, cond, extra) {
  if (cond) { PASS++; console.log('  ✅ ' + name); }
  else { FAIL++; console.log('  ❌ ' + name + (extra !== undefined ? '  → ' + JSON.stringify(extra) : '')); }
}

const SRC = path.join(__dirname, '..', 'cookie-harvest.js');
const code = fs.readFileSync(SRC, 'utf8');

// ---- 1) 浏览器侧挂载（window.VDLCookie）：扩展里 popup.js 就是靠它取到的 ----
const sandbox = { console, Date, JSON, Object, Math, String, Array };
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
check('浏览器侧挂到 window.VDLCookie', !!(sandbox.window.VDLCookie && sandbox.window.VDLCookie.harvest));

// ---- 2) node 侧 require：测试与打包检查用 ----
const V = require(SRC);
check('node 侧 module.exports 可用', typeof V.buildCookieHeader === 'function');

// ---- 3) buildCookieHeader：过滤 + 去重 + 排序稳定 ----
{
  const now = 1700000000000; // 固定时钟，避免用例随真实时间漂
  const cookies = [
    { name: 'SESSDATA', value: 'abc123', domain: '.bilibili.com', path: '/', expirationDate: (now / 1000) + 3600 },
    { name: 'SESSDATA', value: 'dup-old', domain: 'www.bilibili.com', path: '/video/', expirationDate: (now / 1000) + 3600 },
    { name: 'EXPIRED', value: 'gone', domain: '.bilibili.com', path: '/', expirationDate: (now / 1000) - 10 },
    { name: 'EMPTY', value: '', domain: '.bilibili.com', path: '/' },
    { name: 'buvid3', value: 'guest-xyz', domain: '.bilibili.com', path: '/' },
    { noName: true, value: 'x' },
    null
  ];
  const r = V.buildCookieHeader(cookies, now);
  check('过期 Cookie 被剔除', r.header.indexOf('EXPIRED') === -1, r.header);
  check('空值 Cookie 被剔除', r.header.indexOf('EMPTY') === -1, r.header);
  check('无名字段不炸', r.header.indexOf('undefined') === -1, r.header);
  check('同名只留一条', (r.header.match(/SESSDATA=/g) || []).length === 1, r.header);
  check('同名优选 path=/ 的那条', r.header.indexOf('SESSDATA=abc123') !== -1, r.header);
  check('游客字段照常保留（只用于拼头，不用于判登录）', r.header.indexOf('buvid3=guest-xyz') !== -1, r.header);
  check('count 与 names 同步', r.count === 2 && r.names.length === 2, r);

  // 时钟不注入时应按 Date.now() 走：把过期时间设到很早，必须被滤掉
  const r2 = V.buildCookieHeader([{ name: 'OLD', value: 'v', expirationDate: 1 }]);
  check('默认时钟也滤过期', r2.count === 0, r2);
}

// ---- 4) isLoggedIn：只认登录态字段 ----
{
  const bili = V.PLATFORMS.find(p => p.id === 'bilibili');
  check('只有游客字段 → 未登录',
    V.isLoggedIn(bili, [{ name: 'buvid3', value: 'guest-guest' }, { name: 'cna', value: 'abcdefgh' }]) === false);
  check('SESSDATA 短得像占位 → 不算登录',
    V.isLoggedIn(bili, [{ name: 'SESSDATA', value: 'abc' }]) === false);
  check('SESSDATA 正常长度 → 已登录',
    V.isLoggedIn(bili, [{ name: 'SESSDATA', value: '4f8c9a2e%2C1700000000%2Cabcdef%2A11' }]) === true);

  const yt = V.PLATFORMS.find(p => p.id === 'youtube');
  check('YouTube 认 __Secure-3PSID',
    V.isLoggedIn(yt, [{ name: '__Secure-3PSID', value: 'g.a000longvalue' }]) === true);
  check('YouTube 不认普通 VISITOR_INFO',
    V.isLoggedIn(yt, [{ name: 'VISITOR_INFO1_LIVE', value: 'abcdefghij' }]) === false);
}

// ---- 5) harvest：无 cookies 权限必须明确报错，而不是「全部未登录」 ----
{
  const noPerm = {};
  let out = null;
  V.harvest(noPerm, r => { out = r; });
  check('无权限时每项都带 no-permission', out && out.length === V.PLATFORMS.length &&
    out.every(x => x.error === 'no-permission'), out && out[0]);
}

// ---- 6) harvest：正常读取（桩里按 domain 返回不同集合） ----
{
  const now = Date.now();
  const store = {
    'bilibili.com': [
      { name: 'SESSDATA', value: 'sess-cookie-value-long', domain: '.bilibili.com', path: '/', expirationDate: (now / 1000) + 600 },
      { name: 'buvid3', value: 'guest', domain: '.bilibili.com', path: '/' }
    ],
    'youtube.com': [
      { name: 'VISITOR_INFO1_LIVE', value: 'visitor-only', domain: '.youtube.com', path: '/' }
    ],
    'weibo.com': [
      { name: 'SUB', value: 'weibo-sub-cookie-value', domain: '.weibo.com', path: '/' }
    ],
    // 百度网盘（2026-10-23 接入）：BDUSS 挂在根域，cloud=false 只给本机
    'baidu.com': [
      { name: 'BDUSS', value: 'bduss-cookie-value-long', domain: '.baidu.com', path: '/' },
      { name: 'BAIDUID', value: 'guest-only-baiduid', domain: '.baidu.com', path: '/' }
    ]
  };
  const stub = {
    cookies: {
      getAll: function (q, cb) { cb(store[q.domain] || []); }
    }
  };
  let out = null;
  V.harvest(stub, r => { out = r; });
  const byId = {};
  (out || []).forEach(x => { byId[x.id] = x; });
  check('13 个平台全部返回（含 2026-10-23 新增的百度网盘）',
        out && out.length === V.PLATFORMS.length, out && out.length);
  check('B站判为已登录', byId.bilibili && byId.bilibili.loggedIn === true, byId.bilibili);
  check('B站 header 含 SESSDATA 与游客字段', byId.bilibili && byId.bilibili.header.indexOf('SESSDATA=') !== -1, byId.bilibili && byId.bilibili.header);
  check('YouTube 只有游客 Cookie → 未登录', byId.youtube && byId.youtube.loggedIn === false, byId.youtube);
  check('微博已登录但 cloud=false', byId.weibo && byId.weibo.loggedIn === true && byId.weibo.cloud === false, byId.weibo);
  check('百度网盘：有 BDUSS 判为已登录，且 cloud=false（不进公共池）',
        byId.baidupan && byId.baidupan.loggedIn === true && byId.baidupan.cloud === false, byId.baidupan);
  check('百度网盘 header 含 BDUSS',
        byId.baidupan && byId.baidupan.header.indexOf('BDUSS=') !== -1, byId.baidupan && byId.baidupan.header);

  // pickReady
  const ready = V.pickReady(out, { cloud: true });
  check('cloud 过滤掉微博', ready.every(x => x.cloud === true) && ready.length === 1, ready.map(x => x.id));
  const readyAll = V.pickReady(out);
  // 2026-10-23：百度网盘加入 PLATFORMS 且样本里已登录 ⇒ 不限定 cloud 时应为 3 个
  check('不限定 cloud 时含微博与百度', readyAll.length === 3, readyAll.map(x => x.id));
}

// ---- 7) harvest：getAll 抛异常 / runtime.lastError 都要降级成 error 而非中断 ----
{
  const stubThrow = {
    cookies: { getAll: function () { throw new Error('boom'); } }
  };
  let out = null;
  V.harvest(stubThrow, r => { out = r; }, [V.PLATFORMS[0], V.PLATFORMS[1]]);
  check('getAll 抛异常 → 逐项 read-error 且流程不中断',
    out && out.length === 2 && out.every(x => x.error === 'read-error'), out);

  const stubLE = {
    runtime: { lastError: { message: 'Cookie access denied' } },
    cookies: { getAll: function (q, cb) { cb([]); } }
  };
  let out2 = null;
  V.harvest(stubLE, r => { out2 = r; }, [V.PLATFORMS[0]]);
  check('runtime.lastError → 带 error 而不是空结果', out2 && out2[0].error === 'Cookie access denied', out2);
}

// ---- 8) 推送通道：URL / body 契约（mock fetch 记录参数） ----
(async function () {
  // ---- 7b) getAll 永不回调（真机踩过：network service 崩溃 / 钥匙串拒绝授权时）----
  // 没有超时兜底的话，UI 会永远停在「扫描中…」，用户只能重开面板。
  await new Promise((res) => {
    const stub = { cookies: { getAll: function () { /* 故意不回调 */ } } };
    const t0 = Date.now();
    V.harvest(stub, (out) => {
      const ms = Date.now() - t0;
      check('永不回调时按超时返回', out.length === 2 && out.every((x) => x.error === 'timeout'), out);
      check('超时不拖过设定的上限', ms < 400, ms);
      res();
    }, [V.PLATFORMS[0], V.PLATFORMS[1]], { timeoutMs: 60 });
  });

  // 一个站正常、一个站永不回调 → 正常的那个必须照常出结果（并发而非串行）
  await new Promise((res) => {
    const store = { 'bilibili.com': [{ name: 'SESSDATA', value: 'concurrent-value-long', domain: '.bilibili.com', path: '/' }] };
    const stub = {
      cookies: {
        getAll: function (q, cb) {
          if (q.domain === 'bilibili.com') cb(store[q.domain]);
          /* youtube.com 永不回调 */
        }
      }
    };
    V.harvest(stub, (out) => {
      const byId = {};
      out.forEach((x) => { byId[x.id] = x; });
      check('一挂一好：正常站仍出结果', byId.bilibili && byId.bilibili.loggedIn === true, byId.bilibili);
      check('一挂一好：挂住的站标 timeout', byId.youtube && byId.youtube.error === 'timeout', byId.youtube);
      res();
    }, [V.PLATFORMS[0], V.PLATFORMS[1]], { timeoutMs: 80 });
  });

  const calls = [];
  const prevFetch = global.fetch;
  global.fetch = function (url, opt) {
    calls.push({ url: url, opt: opt });
    return Promise.resolve({
      ok: true,
      status: 200,
      text: () => Promise.resolve('{"ok":true,"added":true,"verified":true}')
    });
  };

  const bili = V.PLATFORMS.find(p => p.id === 'bilibili');
  await new Promise(res => V.pushLocal('http://127.0.0.1:8321/', bili, 'SESSDATA=x', r => {
    check('本机推送走 /api/cookie/browser-push', calls[0].url === 'http://127.0.0.1:8321/api/cookie/browser-push', calls[0].url);
    const body = JSON.parse(calls[0].opt.body);
    check('本机推送带 domain/cookie/platform',
      body.domain === 'bilibili.com' && body.cookie === 'SESSDATA=x' && body.platform === 'bilibili', body);
    check('本机推送成功解析 added/verified', r.ok === true && r.added === true && r.verified === true, r);
    res();
  }));

  await new Promise(res => V.pushCloud(bili, 'SESSDATA=x', r => {
    check('云端推送走 /api/cookie/contribute', calls[1].url === 'https://hanyuxz.top/api/cookie/contribute', calls[1].url);
    const body = JSON.parse(calls[1].opt.body);
    check('云端推送用 url+cookie（与网页版手动贡献同契约）',
      body.url === 'https://www.bilibili.com/' && body.cookie === 'SESSDATA=x', body);
    check('云端推送成功', r.ok === true, r);
    res();
  }));

  // 失败分支：400 带 detail 必须把 detail 转给用户，而不是「失败」两个字
  global.fetch = function () {
    return Promise.resolve({ ok: false, status: 400, text: () => Promise.resolve('{"detail":"Cookie 无效，未能通过目标站验真"}') });
  };
  await new Promise(res => V.pushCloud(bili, 'bad', r => {
    check('失败时把服务端 detail 透出', r.ok === false && r.status === 400 && /验真/.test(r.error), r);
    res();
  }));

  // 端点为空：不该发请求
  const before = calls.length;
  await new Promise(res => V.pushLocal('', bili, 'x', r => {
    check('无端点直接返回 no-endpoint', r.ok === false && r.error === 'no-endpoint', r);
    check('无端点时不发请求', calls.length === before, calls.length);
    res();
  }));

  global.fetch = prevFetch;

  console.log('\n' + (FAIL === 0 ? '✅ 全部通过' : '❌ 有失败') + '：' + PASS + ' passed, ' + FAIL + ' failed');
  process.exit(FAIL === 0 ? 0 : 1);
})();
