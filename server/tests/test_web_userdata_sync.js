#!/usr/bin/env node
/**
 * 网页版「云端资料（用户信息类数据备份）」引擎离线回归测试。
 *
 * 背景（2026-10-17，网页版接入云备份）
 * ------------------------------------
 * 网页版把「浏览器里的偏好 + 用户粘贴的平台登录态」随账号备份/恢复。前端这段引擎
 * 是最容易静默出错、且错了代价最大的一段：
 *   · 加密写错 → Cookie 明文出网（隐私事故，且肉眼看不见）；
 *   · 密钥派生写错 → 换浏览器解不开自己的备份，或**别人的**备份被解开；
 *   · 冲突判定写错 → 刚粘的登录态被云端旧态盖掉；
 *   · 摘要取错 → 会员中心那行「已备份 N 项 / M 个登录态」拿本机数冒充云端数。
 * 所以用「真源码 + 假 localStorage/假服务端 + Node 内置 WebCrypto」把它钉住。
 *
 * 做法：从 `web/app.js` 按两个标记**抽取真实引擎源码**，在 Node 里注入假环境运行。
 * 不复制算法（复制等于测副本，改坏了照样绿），不打任何网络，不动用户真实数据。
 * 加密用的是 Node 18+ 内置的 WebCrypto（与浏览器同一套 API/同一份实现规范）。
 *
 * 钉住的不变量：
 *   1. Cookie 在浏览器本机加密成密文（AES-256-GCM + PBKDF2-SHA256 20 万轮）；
 *   2. 上传体的**任何位置**都不含 Cookie 明文；服务端只拿得到密文、解不开；
 *   3. 本人同一密码 + 云端 salt ⇒ 换浏览器能解回（跨端恢复的数学前提）；
 *   4. 密码错 / 密文被篡改 ⇒ 解密 fail-closed（返回 null，绝不含糊使用）；
 *   5. 密钥解析：云端有 kdf 以云端为准；只有首次上传才新建 salt；缓存 salt 不符不硬凑；
 *   6. 重复同步幂等（内容没变不会反复上传）；本机无 Cookie 时**不隐式删除**云端密文；
 *   7. 本机 Cookie 比云端新 ⇒ 不被云端旧态覆盖（备份不是镜像）；
 *   8. 会员中心计数一律取云端实际量；提示语区分「解锁（云端已有密文）」与
 *      「首次上传（云端还没有密文）」；摘要取不到时明说「暂时不可取」，不冒充。
 *
 * 运行：node server/tests/test_web_userdata_sync.js
 */
'use strict';

const fs = require('fs');
const path = require('path');

const APP_JS = path.resolve(__dirname, '..', '..', 'web', 'app.js');
const START_MARK = '// 云端资料 · 网页版接入（2026-10-17）';
const END_MARK = '// 网页访客统计：首屏加载上报一次';

const src = fs.readFileSync(APP_JS, 'utf8');
const i0 = src.indexOf(START_MARK);
const i1 = src.indexOf(END_MARK);
if (i0 < 0 || i1 < 0 || i1 <= i0) {
  console.error('❌ 无法从 web/app.js 提取云备份引擎（标记缺失或顺序错乱）');
  console.error('   START=%s END=%s  →  标记被改名/移动时必须同步更新本测试', i0, i1);
  process.exit(2);
}
const ENGINE_SRC = src.slice(i0, i1);

// 引擎内部符号（改名时必须同步；缺失会在 new Function 调用时抛 ReferenceError 而失败）
const EXPORTS = [
  'udSync', 'udAfterLogin', 'udManualSync', 'udRender', 'udAutoSync', 'udScheduleAutoSync',
  'udDeriveBits', 'udEncrypt', 'udDecrypt', 'udResolveKey',
  'udCollectSettings', 'udApplySettings', 'udApplyCloud', 'udMarker', 'udMarkerSave',
];
const CONSTS = ['UD_PREF_KEYS', 'UD_ITERS', 'UD_COOKIE_KEY', 'UD_KEY_LS', 'UD_MARK_LS',
  'UD_COOKIE_TS_LS'];
const ALLOWED_URLS = ['/api/userdata/pull', '/api/userdata/push', '/api/userdata/status'];

// --------------------------------------------------------------------------- //
// 迷你断言器
// --------------------------------------------------------------------------- //
let PASS = 0;
let FAIL = 0;
function check(name, cond, extra) {
  if (cond) { PASS++; console.log('  ✅ ' + name); }
  else {
    FAIL++;
    console.log('  ❌ ' + name + (extra !== undefined ? ('  → ' + extra) : ''));
  }
}
async function group(name, fn) {
  console.log('\n▶ ' + name);
  await fn();
}

const b64 = (u8) => Buffer.from(u8).toString('base64');
const unb64 = (s) => new Uint8Array(Buffer.from(String(s), 'base64'));
const nowSec = () => Math.floor(Date.now() / 1000);

// --------------------------------------------------------------------------- //
// 引擎装载器：假 localStorage / 假服务端 / 假 DOM，真 WebCrypto
// --------------------------------------------------------------------------- //
function loadEngine(opts) {
  const o = opts || {};
  const store = Object.create(null);
  const ls = {
    getItem: (k) => (Object.prototype.hasOwnProperty.call(store, String(k)) ? store[String(k)] : null),
    setItem: (k, v) => { store[String(k)] = String(v); },
    removeItem: (k) => { delete store[String(k)]; },
  };
  if (o.seed) Object.keys(o.seed).forEach((k) => ls.setItem(k, o.seed[k]));

  // 假服务端：只认 /api/userdata/{pull,push,status}，并记录调用（用于断言「只打这几个接口」）
  const srv = { blob: o.blob || null, pushes: 0, pulls: 0, urls: [], lastBody: null };
  let status = o.status || {
    ok: true, logged_in: true, cloud_linked: true, cloud: null, cloud_error: '',
  };

  const els = Object.create(null);
  const getEl = (id) => (els[id] || (els[id] = {
    id, textContent: '', hidden: false, value: '', disabled: false, _click: null,
    addEventListener(ev, fn) { if (ev === 'click') this._click = fn; },
  }));

  const toasts = [];
  const logs = [];

  async function request(url, options) {
    const oo = options || {};
    srv.urls.push(url);
    if (url === '/api/userdata/pull') {
      srv.pulls++;
      return { ok: true, payload: srv.blob, meta: null };
    }
    if (url === '/api/userdata/push') {
      srv.pushes++;
      srv.lastBody = JSON.parse(oo.body || '{}');
      const payload = srv.lastBody.payload || {};
      if (!payload.updated_at) payload.updated_at = nowSec();
      srv.blob = payload;
      return { ok: true, updated_at: payload.updated_at, meta: null };
    }
    if (url === '/api/userdata/status') return status;
    throw new Error('unexpected request: ' + url);
  }

  const ctx = {
    localStorage: ls,
    request,
    document: { getElementById: getEl },
    navigator: { userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/131.0.0.0' },
    window: {
      crypto: globalThis.crypto, TextEncoder: globalThis.TextEncoder,
      TextDecoder: globalThis.TextDecoder, btoa: globalThis.btoa, atob: globalThis.atob,
    },
    crypto: globalThis.crypto,
    TextEncoder: globalThis.TextEncoder,
    TextDecoder: globalThis.TextDecoder,
    btoa: globalThis.btoa,
    atob: globalThis.atob,
    console: { info: (m) => logs.push(String(m)), error: () => {}, log: () => {} },
    el: {},
    showToast: (m) => toasts.push(String(m)),
    pfToken: () => (o.token === undefined ? 'tok-123' : o.token),
    setTimeout: (fn) => { try { fn(); } catch (_) {} return 0; },  // 测试里立即执行防抖回调
    clearTimeout: () => {},
  };

  const FACTORY_SRC = 'const { localStorage, request, document, navigator, window, crypto,'
    + ' TextEncoder, TextDecoder, btoa, atob, console, el, showToast, pfToken,'
    + ' setTimeout, clearTimeout } = ctx;\n'
    + ENGINE_SRC + '\n'
    + 'return { ' + EXPORTS.join(', ') + ', __c: { ' + CONSTS.join(', ') + ' } };';

  const factory = new Function('ctx', FACTORY_SRC);
  const eng = factory(ctx);
  return { eng, ls, srv, els, toasts, logs, setStatus: (s) => { status = s; } };
}

// --------------------------------------------------------------------------- //
// 用例
// --------------------------------------------------------------------------- //
(async function main() {
  console.log('=== test_web_userdata_sync.js（网页版云备份引擎 · 真源码 + 真 WebCrypto）===');
  console.log('    抽取区间: %s … %s  (%d 字符)', START_MARK.slice(0, 16), END_MARK.slice(0, 12),
    ENGINE_SRC.length);

  await group('① 加解密：Cookie 只在浏览器本机加密成密文', async () => {
    const { eng } = loadEngine();
    const SECRET = 'SESSDATA=super-secret-cookie-abc; bili_jct=deadbeef';
    const salt = globalThis.crypto.getRandomValues(new Uint8Array(16));
    const bits = await eng.udDeriveBits('my-pass-123456', salt, 200000);
    check('派生 32 字节密钥（AES-256 档）', bits.length === 32, bits.length);

    const blob = await eng.udEncrypt({ header: SECRET }, bits, salt, 200000);
    check('密文结构 = aesgcm256 + pbkdf2 kdf(salt/iters=20万) + nonce + ct',
      blob.enc === 'aesgcm256' && !!blob.kdf && !!blob.kdf.salt
      && Number(blob.kdf.iters) === 200000 && !!blob.nonce && !!blob.ct,
      JSON.stringify(Object.keys(blob)));
    check('序列化后不含任何 Cookie 明文字样', !JSON.stringify(blob).includes('super-secret-cookie'));

    const back = await eng.udDecrypt(blob, bits);
    check('同密钥可解回原文（往返一致）', !!back && back.header === SECRET);

    const bits2 = await eng.udDeriveBits('my-pass-123456', unb64(blob.kdf.salt),
      Number(blob.kdf.iters));
    check('换浏览器：同密码 + 云端 salt ⇒ 密钥一致(能解开自己的备份)',
      b64(bits2) === b64(bits));

    const wrong = await eng.udDeriveBits('WRONG-pass-999999', unb64(blob.kdf.salt), 200000);
    check('密码错 ⇒ 解密返回 null（fail-closed）', (await eng.udDecrypt(blob, wrong)) === null);

    const raw = unb64(blob.ct);
    raw[0] = raw[0] ^ 0xff;
    const tampered = Object.assign({}, blob, { ct: b64(raw) });
    check('密文被篡改 ⇒ 解密返回 null（GCM 完整性校验生效）',
      (await eng.udDecrypt(tampered, bits)) === null);
  });

  await group('② 密钥解析：云端有 kdf 以云端为准；只有首次上传才建 salt', async () => {
    const salt = globalThis.crypto.getRandomValues(new Uint8Array(16));
    const cloudKdf = { alg: 'pbkdf2-sha256', iters: 200000, salt: b64(salt) };

    const a = loadEngine();
    const rk = await a.eng.udResolveKey('pw-abcdef', cloudKdf, false);
    check('云端有 kdf + 有密码 ⇒ 用云端 salt 派生', !!rk && b64(rk.salt) === b64(salt));
    check('云端无 kdf 且不允许建 salt ⇒ null（不瞎造 salt）',
      (await a.eng.udResolveKey('pw-abcdef', null, false)) === null);

    const rk2 = await a.eng.udResolveKey('pw-abcdef', null, true);
    check('首次上传（无 kdf + createSalt）⇒ 新建 16 字节 salt',
      !!rk2 && rk2.salt.length === 16);
    check('解析阶段不写密钥缓存（密码可能是错的，只由加解密成功路径写）',
      a.ls.getItem(a.eng.__c.UD_KEY_LS) === null);

    // 免密同步：pw 空 + 云端 kdf + 本机缓存同 salt ⇒ 用缓存
    const bits = await a.eng.udDeriveBits('pw-abcdef', salt, 200000);
    const b = loadEngine();
    b.ls.setItem(b.eng.__c.UD_KEY_LS, JSON.stringify(
      { v: 1, key: b64(bits), salt: b64(salt), iters: 200000 }));
    const rk3 = await b.eng.udResolveKey('', cloudKdf, true);
    check('免密同步：pw 空 + 缓存同 salt ⇒ 用缓存密钥', !!rk3 && b64(rk3.bits) === b64(bits));

    // 缓存 salt 与云端不一致 ⇒ 不能用缓存（不能拿旧钥匙开新锁）
    const c = loadEngine();
    const other = globalThis.crypto.getRandomValues(new Uint8Array(16));
    c.ls.setItem(c.eng.__c.UD_KEY_LS, JSON.stringify(
      { v: 1, key: b64(bits), salt: b64(other), iters: 200000 }));
    check('缓存 salt 与云端不一致 + 无密码 ⇒ null（宁可不解，也不乱试）',
      (await c.eng.udResolveKey('', cloudKdf, true)) === null);
  });

  await group('③ 首次同步：Cookie 加密后才上传，上传体绝无明文', async () => {
    const SECRET = 'SESSDATA=very-secret-token-zzz; bili_jct=cafebabe';
    const ctx = loadEngine({
      seed: { vdl_use: 'mp4', vdl_peer: '1', vdl_region: 'cn', vdl_cookie: SECRET },
    });
    const rep = await ctx.eng.udSync('my-pass-123456', {});
    check('云端本来没有资料 ⇒ 触发首次上传', rep.pushed === true, JSON.stringify(rep));

    const payloadStr = JSON.stringify(ctx.srv.blob || null);
    const parts = (ctx.srv.blob || {}).parts || {};
    check('偏好（白名单三键）进了 settings',
      !!parts.settings && parts.settings.vdl_use === 'mp4' && parts.settings.vdl_region === 'cn');
    check('Cookie 以密文上传（enc=aesgcm256 + 有 ct）',
      !!parts.cookies && parts.cookies.enc === 'aesgcm256' && !!parts.cookies.ct);
    check('🔴 上传体任何位置都不含 Cookie 明文（整包序列化检索）',
      !payloadStr.includes('very-secret-token-zzz'));
    check('Cookie 元信息只给条数、不给内容',
      !!parts.cookies.meta && Number(parts.cookies.meta.count) === 1
      && !JSON.stringify(parts.cookies.meta).includes('very-secret'));

    // 服务端/授权中心视角：只有密文、没有密码 ⇒ 读不出内容
    let serverRead = false;
    try {
      const guess = await ctx.eng.udDeriveBits('admin-guess', unb64(parts.cookies.kdf.salt),
        parts.cookies.kdf.iters);
      serverRead = !!(await ctx.eng.udDecrypt(parts.cookies, guess));
    } catch (_) { /* ignore */ }
    check('服务端拿不到密码 ⇒ 读不出内容（端到端加密成立）', serverRead === false);

    const mine = await ctx.eng.udDeriveBits('my-pass-123456', unb64(parts.cookies.kdf.salt),
      parts.cookies.kdf.iters);
    const plain = await ctx.eng.udDecrypt(parts.cookies, mine);
    check('本人用同一密码 ⇒ 能解回 Cookie 明文（换机能恢复）',
      !!plain && plain.header === SECRET);
    check('密钥缓存已落本机（供后续免密同步）',
      !!ctx.ls.getItem(ctx.eng.__c.UD_KEY_LS));
    check('云备份只走 /api/userdata/*，不把资料打到别的接口',
      ctx.srv.urls.every((u) => ALLOWED_URLS.indexOf(u) >= 0), JSON.stringify(ctx.srv.urls));
  });

  await group('④ 幂等 / 不隐式删除云端密文 / 本机更新不被云端旧态覆盖', async () => {
    const SECRET = 'SESSDATA=keepme; x=1';
    const ctx = loadEngine({ seed: { vdl_use: 'mp4', vdl_cookie: SECRET } });
    await ctx.eng.udSync('my-pass-123456', {});
    const pushes1 = ctx.srv.pushes;

    const rep2 = await ctx.eng.udSync(null, {});
    check('内容未变 ⇒ 不再重复上传（不会每次登录都刷一遍）',
      rep2.pushed === false && ctx.srv.pushes === pushes1, JSON.stringify(rep2));

    const before = JSON.stringify((ctx.srv.blob.parts || {}).cookies);
    ctx.ls.removeItem('vdl_cookie');
    await ctx.eng.udSync(null, {});
    const after = JSON.stringify((ctx.srv.blob.parts || {}).cookies);
    check('本机无 Cookie ⇒ 云端密文原样保留（绝不隐式删除已有备份）', after === before);

    // 冲突守卫：云端是旧的，本机刚粘的更新 ⇒ 不能被云端旧态盖掉
    const T0 = nowSec() - 3600;
    const salt = globalThis.crypto.getRandomValues(new Uint8Array(16));
    const fresh = loadEngine({ seed: { vdl_cookie: 'BRAND-NEW-COOKIE',
      vdl_cookie_ts: String(nowSec()) } });
    const bits = await fresh.eng.udDeriveBits('pw-abcdef', salt, 1000);
    const oldBlob = await fresh.eng.udEncrypt({ header: 'OLD-CLOUD-COOKIE' }, bits, salt, 1000);
    oldBlob.meta = { count: 1, platforms: [], updated_at: T0 };
    const cloud = { updated_at: T0, parts: { settings: {}, cookies: oldBlob } };
    const ap = await fresh.eng.udApplyCloud(cloud, 'pw-abcdef');
    check('本机 Cookie 比云端新 ⇒ 保留本机（备份不是镜像，不拿旧态盖新粘的）',
      fresh.ls.getItem('vdl_cookie') === 'BRAND-NEW-COOKIE' && ap.cookies === 0);

    // 反向：本机更旧 ⇒ 落回云端那一份
    const older = loadEngine({ seed: { vdl_cookie: 'STALE-LOCAL',
      vdl_cookie_ts: String(T0 - 3600) } });
    const ap2 = await older.eng.udApplyCloud(cloud, 'pw-abcdef');
    check('本机 Cookie 比云端旧 ⇒ 落回云端那一条',
      older.ls.getItem('vdl_cookie') === 'OLD-CLOUD-COOKIE' && ap2.cookies === 1,
      JSON.stringify({ v: older.ls.getItem('vdl_cookie'), ap2 }));
  });

  await group('⑤ 会员中心那行：计数取云端实际量 + 提示语区分「解锁 / 首次上传」', async () => {
    const C = (o) => Object.assign(
      { ok: true, logged_in: true, cloud_linked: true, cloud: null, cloud_error: '' }, o);

    // 云端有 3 条登录态、本机没钥匙 ⇒ 「解锁」
    const a = loadEngine({});
    a.setStatus(C({ cloud: { updated_at: nowSec(),
      settings: { count: 4, files: ['a.json', 'b.json', 'c.json', 'd.json'] },
      cookies: { count: 3, platforms: ['bilibili'] } } }));
    await a.eng.udRender();
    // 2026-10-19 用户要求隐藏这张卡片：文案逻辑仍要正确（后面几项），但绝不能显示。
    check('🔴 卡片默认隐藏（UD_CARD_VISIBLE=false）',
      a.els.cloudDataCard.hidden === true,
      JSON.stringify({ hidden: a.els.cloudDataCard.hidden }));
    check('计数用云端实际量（4 项偏好 / 3 个登录态）',
      a.els.cloudDataText.textContent === '云端资料 · 已备份 4 项偏好 / 3 个平台登录态',
      a.els.cloudDataText.textContent);
    check('云端有密文 + 本机无钥匙 ⇒ 提示「解锁」',
      a.els.cloudDataHint.textContent.includes('解锁'), a.els.cloudDataHint.textContent);

    // 🔴 用户报的 bug 现场：云端一条都没有、本机有登录态
    const b = loadEngine({ seed: { vdl_cookie: 'LOCAL-ONLY-COOKIE' } });
    b.setStatus(C({ cloud: { updated_at: nowSec(),
      settings: { count: 1, files: ['x.json'] }, cookies: null } }));
    await b.eng.udRender();
    check('🔴 云端 0 条登录态 ⇒ 显示 0（不拿本机条数冒充）',
      b.els.cloudDataText.textContent === '云端资料 · 已备份 1 项偏好 / 0 个平台登录态',
      b.els.cloudDataText.textContent);
    check('🔴 云端还没有密文 ⇒ 提示「上传」（首次），不是「解锁」',
      b.els.cloudDataHint.textContent.includes('上传')
      && !b.els.cloudDataHint.textContent.includes('解锁'),
      b.els.cloudDataHint.textContent);

    const c = loadEngine({});
    c.setStatus(C({ cloud: null }));
    await c.eng.udRender();
    check('云端暂无备份 ⇒ 明确说「云端暂无备份」',
      c.els.cloudDataText.textContent === '云端资料 · 云端暂无备份',
      c.els.cloudDataText.textContent);

    const d = loadEngine({});
    d.setStatus(C({ cloud: null, cloud_error: '网络超时' }));
    await d.eng.udRender();
    check('摘要取不到 ⇒ 显示「暂时不可取」并附原因（不冒充云端量）',
      d.els.cloudDataText.textContent === '云端资料 · 云端状态暂时不可取'
      && d.els.cloudDataHint.textContent.includes('网络超时'),
      d.els.cloudDataText.textContent);

    const e = loadEngine({});
    e.setStatus(C({ cloud_linked: false }));
    await e.eng.udRender();
    check('账号未接入云端 ⇒ 明确告知资料只在本机',
      e.els.cloudDataText.textContent === '云端资料 · 本机账号未接入云端'
      && e.els.cloudDataHint.textContent.includes('只存在这个浏览器'),
      e.els.cloudDataText.textContent);
  });

  await group('⑥ 触发点接线：登录后自动同步 + 手动「同步」按钮', async () => {
    const a = loadEngine({ seed: { vdl_use: 'mp4', vdl_cookie: 'C=1' } });
    await a.eng.udAfterLogin('my-pass-123456');
    check('登录成功 ⇒ 自动同步一次（此刻才有密码可派生密钥）', a.srv.pushes >= 1);
    check('自动同步不抛错（失败也要静默跳过、不打断登录）',
      a.logs.some((m) => m.includes('登录后同步')), JSON.stringify(a.logs));

    const b = loadEngine({ seed: { vdl_use: 'mp4' } });
    b.els.cloudDataSyncBtn.textContent = '立即同步';   // 与 index.html 初始文案一致
    await b.eng.udManualSync();
    check('手动按钮 ⇒ 强制推一次', b.srv.pushes >= 1);
    check('手动同步给出可读反馈（toast）',
      b.toasts.length >= 1 && /已/.test(b.toasts[b.toasts.length - 1]),
      JSON.stringify(b.toasts));
    check('按钮状态与文案回到原样（不卡在「同步中…」、不改掉初始文案）',
      b.els.cloudDataSyncBtn.disabled === false
      && b.els.cloudDataSyncBtn.textContent === '立即同步',
      JSON.stringify({ d: b.els.cloudDataSyncBtn.disabled,
        t: b.els.cloudDataSyncBtn.textContent }));
  });

  await group('⑦ 粘贴 Cookie 后自动同步：防抖触发 + 没登录账号时安全跳过', async () => {
    // A. 已登录账号（有本机密钥缓存）⇒ 粘贴新 Cookie 后自动备份把最新登录态上传
    const a = loadEngine({ seed: { vdl_use: 'mp4', vdl_cookie: 'OLD-COOKIE' } });
    await a.eng.udAfterLogin('my-pass-123456');   // 首次上传，建立本机密钥缓存（pushes=1）
    const pushesBefore = a.srv.pushes;
    a.ls.setItem('vdl_cookie', 'BRAND-NEW-AUTO-COOKIE');   // 模拟用户在网页版粘贴了新登录态
    a.ls.setItem('vdl_cookie_ts', String(nowSec()));
    a.eng.udScheduleAutoSync(0);                  // 防抖回调立即执行（测试 setTimeout 即调，但内部是异步）
    // 等防抖回调里的异步同步跑完（udAutoSync 内含 await 网络，需 flush 微任务）
    await new Promise((resolve) => {
      let n = 0;
      const tick = () => { if (n++ > 100) return resolve(); globalThis.setTimeout(tick, 0); };
      tick();
    });
    check('粘贴新 Cookie ⇒ 自动同步触发了一次上传', a.srv.pushes > pushesBefore,
      'pushes ' + pushesBefore + '→' + a.srv.pushes);
    const last = a.srv.lastBody && a.srv.lastBody.payload && a.srv.lastBody.payload.parts;
    check('自动上传的 Cookie 是密文（绝不明文出网）',
      !!last && !!last.cookies && last.cookies.enc === 'aesgcm256' && !!last.cookies.ct);
    check('🔴 自动上传体任何位置都不含 Cookie 明文',
      !(JSON.stringify(a.srv.lastBody)).includes('BRAND-NEW-AUTO-COOKIE'));

    // B. 没登录账号（本机无密钥缓存、云端无密文）⇒ 自动同步安全跳过，绝不报错/绝不上传明文
    const b = loadEngine({ seed: { vdl_cookie: 'LOCAL-ONLY-BUT-NO-KEY' } });
    let threw = false;
    try { await b.eng.udAutoSync(); } catch (e) { threw = true; }
    check('没登录账号时自动同步不抛错（静默跳过）', threw === false);
    check('没登录账号 ⇒ 不会把明文 Cookie 传出去（无密钥则不上传 Cookie）',
      !(JSON.stringify(b.srv.blob || {})).includes('LOCAL-ONLY-BUT-NO-KEY'));
  });

  console.log('\n=========================================');
  console.log('通过 ' + PASS + ' 项，失败 ' + FAIL + ' 项');
  console.log('=========================================');
  process.exit(FAIL === 0 ? 0 : 1);
})().catch((e) => {
  console.error('❌ 测试自身异常：', e);
  process.exit(3);
});
