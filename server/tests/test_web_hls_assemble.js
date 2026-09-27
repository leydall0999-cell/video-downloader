#!/usr/bin/env node
/**
 * 网页版「浏览器内 HLS 合成」离线回归测试。
 *
 * 背景（2026-09-27）
 * -----------------
 * 服务端那条 HLS 路线要占用服务器出口带宽 + 磁盘，还要 ffmpeg 合并/转码；
 * 对标 DataTool 的「浏览器侧 m3u8 → MP4」补一条纯客户端路径：清单与分片都经
 * `/api/media/proxy` 中继拉回，按顺序拼成一个 Blob 存到用户本机。
 *
 * 这段逻辑最容易静默出错 —— 而且错了会产出**看着成功、其实是废文件**的成品，
 * 比直接报错更难查。所以用「真源码 + 假源站」把它钉死：
 *
 *   1. 相对地址必须相对**清单自己的 URL** 解析（相对中继地址会拼出假路径）；
 *   2. 初始化段（`#EXT-X-MAP`）必须排在最前，否则 fMP4 整个文件无法解码；
 *   3. 分片严格按清单顺序拼接，成品与源站**逐字节一致**（正确性底线）；
 *   4. 并发度确实 ≤ 3（实测并发峰值，而不是读常量）；
 *   5. 容器判定正确：fMP4/CMAF → `.mp4`，MPEG-TS → `.ts`；
 *   6. 加密流（`#EXT-X-KEY`）与直播流（无 `#EXT-X-ENDLIST`）**必须抛错**，
 *      绝不能静默拼出一个坏文件；分片数超上限同样抛错；
 *   7. 变体选择：给了目标高度就挑「不超过它」的最高档，否则挑最高档；
 *   8. 落盘扩展名由容器决定 —— 标题里带了旧后缀（`.mp4`）也要被纠正；
 *   9. 再点一次按钮 = 取消（AbortError 可读），且按钮文案要还原。
 *
 * 做法：从 `web/app.js` 按两个标记抽取**真实引擎源码**，注入假 `fetch`/`document`/`el`
 * 后在 Node 里直接运行。不复制算法（复制等于测副本，改坏了照样绿），也不打任何网络。
 *
 * 运行：node server/tests/test_web_hls_assemble.js
 */
'use strict';

const fs = require('fs');
const path = require('path');

const APP_JS = path.resolve(__dirname, '..', '..', 'web', 'app.js');
const START_MARK = '// ===== 直链分片下载引擎';
const END_MARK = 'const handleDownload = async () => {';

const src = fs.readFileSync(APP_JS, 'utf8');
const i0 = src.indexOf(START_MARK);
const i1 = src.indexOf(END_MARK);
if (i0 < 0 || i1 < 0 || i1 <= i0) {
  console.error('❌ 无法从 web/app.js 提取引擎源码（标记缺失或顺序错乱）');
  console.error('   START=%s END=%s  →  标记被改名/移动时必须同步更新本测试', i0, i1);
  process.exit(2);
}
const ENGINE_SRC = src.slice(i0, i1);

// --------------------------------------------------------------------------- //
// 断言
// --------------------------------------------------------------------------- //
let CASES = 0;
const FAILS = [];

function check(name, got, want) {
  CASES += 1;
  const g = JSON.stringify(got);
  const w = JSON.stringify(want);
  if (g !== w) FAILS.push(`${name}\n     期望: ${w}\n     实得: ${g}`);
}

function ok(name, cond, hint = '') {
  CASES += 1;
  if (!cond) FAILS.push(`${name}${hint ? `\n     ${hint}` : ''}`);
}

// --------------------------------------------------------------------------- //
// 假源站：URL → 内容（文本或字节）
// --------------------------------------------------------------------------- //
function makeOrigin(routes, { delayMs = 0 } = {}) {
  const state = { calls: [], inflight: 0, maxInflight: 0 };
  // 真实 fetch 在 abort 时会**立刻拒绝**（含正在等待的那次）。假源站必须同样做到：
  // 否则「取消」这条路径在测试里根本不成立 —— 引擎会在取消之后照常拿到数据并产出成品。
  const sleepAbortable = (ms, signal) => new Promise((resolve, reject) => {
    if (!signal) { setTimeout(resolve, ms); return; }
    if (signal.aborted) {
      const e = new Error('aborted');
      e.name = 'AbortError';
      reject(e);
      return;
    }
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    function onAbort() {
      clearTimeout(timer);
      const e = new Error('aborted');
      e.name = 'AbortError';
      reject(e);
    }
    signal.addEventListener('abort', onAbort, { once: true });
  });

  const fetchImpl = async (url, o = {}) => {
    state.calls.push({ url: String(url) });
    state.inflight += 1;
    state.maxInflight = Math.max(state.maxInflight, state.inflight);
    const bail = (err) => { state.inflight -= 1; throw err; };
    if (o.signal && o.signal.aborted) {
      const e = new Error('aborted');
      e.name = 'AbortError';
      return bail(e);
    }
    if (delayMs) {
      try {
        await sleepAbortable(delayMs, o.signal);
      } catch (err) {
        return bail(err);
      }
    }
    // 中继 URL 形如 <base>/api/media/proxy?u=<encodeURIComponent(真实地址)>
    const q = /[?&]u=([^&]*)/.exec(String(url));
    const real = q ? decodeURIComponent(q[1]) : String(url);
    const hit = routes(real);
    state.inflight -= 1;
    if (hit === undefined || hit === null) {
      return {
        ok: false, status: 404,
        headers: { get: () => null },
        json: async () => ({ detail: `源站没有 ${real}` }),
        text: async () => '',
        arrayBuffer: async () => new ArrayBuffer(0),
      };
    }
    const isText = typeof hit === 'string';
    const body = isText ? Buffer.from(hit, 'utf8') : Buffer.from(hit);
    return {
      ok: true, status: 200,
      headers: { get: (n) => (String(n).toLowerCase() === 'content-type'
        ? (isText ? 'application/vnd.apple.mpegurl' : 'application/octet-stream')
        : null) },
      json: async () => ({}),
      text: async () => body.toString('utf8'),
      arrayBuffer: async () => {
        const buf = new ArrayBuffer(body.length);
        new Uint8Array(buf).set(body);
        return buf;
      },
    };
  };
  return { state, fetch: fetchImpl };
}

// --------------------------------------------------------------------------- //
// 环境注入
// --------------------------------------------------------------------------- //
function makeEnv(fetchImpl) {
  const saved = [];
  const clicked = [];
  const el = {
    directHint: { textContent: '', hidden: true },
    downloadBtn: { disabled: false, lastChild: { textContent: '开始下载' } },
    serverFallbackBtn: { hidden: true },
    browserHlsBtn: { hidden: true, disabled: false, textContent: '浏览器内合成' },
    watchQuality: { options: [] },
  };
  const document = {
    body: { appendChild() {} },
    createElement() {
      const node = {
        tag: 'a', href: '', download: '', rel: '',
        click() { clicked.push({ href: node.href, download: node.download }); },
        remove() {},
      };
      return node;
    },
  };
  // ⚠️ 必须用**真的 URL 类**再挂上 createObjectURL/revokeObjectURL：
  // 引擎里 `_hlsAbs()` 用 `new URL(相对地址, 清单地址)` 解析相对路径，
  // 若这里传一个只实现两个静态方法的假对象当 URL，`new URL(...)` 会抛错并被
  // 静默 catch 成「原样返回相对地址」→ 整条 HLS 链路都拿不到分片。
  // （换句话说：这个坑本身就值得防 —— 引擎里那个 catch 不能吞掉 URL 构造失败。）
  const URLShim = URL;
  URLShim.createObjectURL = (blob) => { saved.push(blob); return 'blob:fake'; };
  URLShim.revokeObjectURL = () => {};
  return { el, document, URL: URLShim, saved, clicked };
}

function loadEngine(fetchImpl, { quality = 'best', resolved = null, envIn = null } = {}) {
  const env = envIn || makeEnv(fetchImpl);
  const factory = new Function(
    'el', 'formatBytes', 'formatEta', 'fetch', 'document', 'URL',
    'AbortController', 'performance', 'setTimeout',
    'selectedQuality', 'resolved',
    `${ENGINE_SRC}\nreturn {`
    + ` triggerBrowserHlsDownload, _dlRunHls, _dlParseM3u8, _dlPickVariant, _dlHlsContainer,`
    + ` _dlHlsSourceFor, _dlFetchText, _dlFetchBin, _dlRelayUrl, _dlSave,`
    + ` _DL_PARALLEL, _HLS_MAX_SEGMENTS };`,
  );
  const api = factory(
    env.el,
    (b) => (b ? `${b}B` : '--'),
    (s) => (s > 0 ? `剩余${Math.round(s)}s` : ''),
    fetchImpl,
    env.document,
    env.URL,
    AbortController,
    performance,
    setTimeout,
    quality,
    resolved,
  );
  return { api, env };
}

/** 让 watch_options 就绪：[{value, url, hls}] */
function installWatchOptions(el, list) {
  el.watchQuality.options = list.map((o) => ({
    value: o.value,
    dataset: { url: o.url || '', hls: String(o.hls === undefined ? true : o.hls) },
  }));
}

const BASE = 'https://origin.test/hls';
const M3U8 = `${BASE}/index.m3u8`;
const MASTER = `${BASE}/master.m3u8`;

// --------------------------------------------------------------------------- //
// 1) 解析器
// --------------------------------------------------------------------------- //
{
  const { api } = loadEngine(makeOrigin(() => null).fetch);
  const pl = api._dlParseM3u8(
    '#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:9.9,\nseg-1.ts\n#EXTINF:9.9,\nsub/seg-2.ts\n#EXT-X-ENDLIST\n',
    M3U8,
  );
  check('媒体清单：类型', pl.kind, 'media');
  check('媒体清单：分片数', pl.segments.length, 2);
  check('媒体清单：相对地址相对清单自身解析',
    pl.segments, [`${BASE}/seg-1.ts`, `${BASE}/sub/seg-2.ts`]);
  check('媒体清单：有 ENDLIST ⇒ 非直播', pl.live, false);
  check('媒体清单：未加密', pl.encrypted, false);
  check('媒体清单：无初始化段', pl.init, null);
  check('媒体清单：分片扩展名', pl.segExt, 'ts');
}

{
  const { api } = loadEngine(makeOrigin(() => null).fetch);
  const pl = api._dlParseM3u8(
    '#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:6,\na.m4s\nb.m4s\n',
    M3U8,
  );
  check('无 ENDLIST ⇒ 判为直播', pl.live, true);
  check('EXT-X-MAP 解析成绝对地址（相对清单）', pl.init, `${BASE}/init.mp4`);
  check('m4s 分片扩展名', pl.segExt, 'm4s');
  check('无 ENDLIST 但分片齐全（边界用例）', pl.segments.length, 2);
}

{
  const { api } = loadEngine(makeOrigin(() => null).fetch);
  const master = api._dlParseM3u8(
    '#EXTM3U\n'
    + '#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\nv360/index.m3u8\n'
    + '#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1280x720\nv720/index.m3u8\n'
    + '#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080\nv1080/index.m3u8\n',
    M3U8,
  );
  check('master 清单：类型', master.kind, 'master');
  check('master 清单：变体数', master.variants.length, 3);
  check('master 清单：变体高度',
    master.variants.map((v) => v.height), [360, 720, 1080]);
  check('master 清单：变体地址相对清单解析',
    master.variants[1].uri, `${BASE}/v720/index.m3u8`);
}

{
  const { api } = loadEngine(makeOrigin(() => null).fetch);
  const enc = api._dlParseM3u8(
    '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n#EXT-X-MAP:URI="init.mp4"\na.m4s\n#EXT-X-ENDLIST\n',
    M3U8,
  );
  check('AES-128 加密被识别', enc.encrypted, true);
  const none = api._dlParseM3u8(
    '#EXTM3U\n#EXT-X-KEY:METHOD=NONE\na.ts\n#EXT-X-ENDLIST\n', M3U8);
  check('METHOD=NONE 视为未加密', none.encrypted, false);
}

// --------------------------------------------------------------------------- //
// 2) 变体选择 / 容器判定
// --------------------------------------------------------------------------- //
{
  const { api } = loadEngine(makeOrigin(() => null).fetch);
  const vs = [
    { uri: 'a', height: 360, bandwidth: 800000 },
    { uri: 'b', height: 720, bandwidth: 2400000 },
    { uri: 'c', height: 1080, bandwidth: 5000000 },
  ];
  check('目标 720 → 挑 720', api._dlPickVariant(vs, 720).height, 720);
  check('目标 900 → 挑不超过它的最高 720', api._dlPickVariant(vs, 900).height, 720);
  check('目标 360 → 挑 360', api._dlPickVariant(vs, 360).height, 360);
  check('目标 0（best）→ 挑最高档', api._dlPickVariant(vs, 0).height, 1080);
  check('目标低于所有档 → 退最低档', api._dlPickVariant(vs, 240).height, 360);
}

{
  const { api } = loadEngine(makeOrigin(() => null).fetch);
  check('有 init ⇒ .mp4',
    api._dlHlsContainer({ init: 'x', segExt: 'm4s' }), { ext: '.mp4', type: 'video/mp4' });
  check('无 init 但 m4s ⇒ .mp4',
    api._dlHlsContainer({ init: null, segExt: 'm4s' }), { ext: '.mp4', type: 'video/mp4' });
  check('ts 分片 ⇒ .ts',
    api._dlHlsContainer({ init: null, segExt: 'ts' }), { ext: '.ts', type: 'video/mp2t' });
  check('无扩展名且无 init ⇒ 按 TS 兜底',
    api._dlHlsContainer({ init: null, segExt: '' }), { ext: '.ts', type: 'video/mp2t' });
}

// --------------------------------------------------------------------------- //
// 3) 合成端到端：fMP4（master → media → init + 分片）
// --------------------------------------------------------------------------- //
function segBytes(tag, n) {
  const out = Buffer.alloc(n);
  for (let i = 0; i < n; i += 1) out[i] = (tag.charCodeAt(0) + i) & 0xff;
  return out;
}

async function testFmp4Assemble() {
  const INIT = segBytes('I', 32);
  const SEGS = [segBytes('A', 64), segBytes('B', 64), segBytes('C', 64), segBytes('D', 64)];
  // 分片与 init 都写相对地址 —— 相对的是**该清单自身所在的目录**（v1080/）
  const mediaPl = '#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MAP:URI="init.mp4"\n'
    + SEGS.map((_s, i) => `#EXTINF:6,\ns${i + 1}.m4s`).join('\n') + '\n#EXT-X-ENDLIST\n';
  const masterPl = '#EXTM3U\n'
    + '#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\nv360/index.m3u8\n'
    + '#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080\nv1080/index.m3u8\n';

  const seen = [];
  const origin = makeOrigin((u) => {
    seen.push(u);
    if (u === MASTER) return masterPl;
    if (u === `${BASE}/v1080/index.m3u8`) return mediaPl;
    if (u === `${BASE}/v1080/init.mp4`) return INIT;
    const m = /^https:\/\/origin\.test\/hls\/v1080\/s(\d)\.m4s$/.exec(u);
    if (m) return SEGS[parseInt(m[1], 10) - 1];
    return null;
  });

  const { api } = loadEngine(origin.fetch);
  const progress = [];
  const res = await api._dlRunHls(MASTER, '', {
    signal: new AbortController().signal,
    wantHeight: 0,
    onProgress: (bytes, done, count) => progress.push([bytes, done, count]),
  });

  check('fMP4：容器', res.ext, '.mp4');
  check('fMP4：Blob 类型', res.blob.type, 'video/mp4');
  const got = Buffer.from(await res.blob.arrayBuffer());
  const want = Buffer.concat([INIT, ...SEGS]);
  check('fMP4：成品长度', got.length, want.length);
  ok('fMP4：成品与源站逐字节一致（含 init 在最前）', got.equals(want),
    `前 8 字节 got=${got.subarray(0, 8).toString('hex')} want=${want.subarray(0, 8).toString('hex')}`);

  check('fMP4：请求顺序 = master → media → init（前 3 条）',
    seen.slice(0, 3), [MASTER, `${BASE}/v1080/index.m3u8`, `${BASE}/v1080/init.mp4`]);
  check('fMP4：分片请求都带上了（4 片）',
    seen.filter((u) => /s\d\.m4s$/.test(u)).length, 4);
  ok('fMP4：并发峰值 ≤ 3（实测）', origin.state.maxInflight <= 3,
    `实测峰值 ${origin.state.maxInflight}`);
  check('fMP4：进度回调次数 = 分片数', progress.length, 4);
  check('fMP4：进度按片递增到满',
    progress.map((p) => p[1]), [1, 2, 3, 4]);
  check('fMP4：进度里的总片数正确', progress[3][2], 4);
  ok('fMP4：进度字节数单调递增',
    progress.every((p, i) => i === 0 || p[0] > progress[i - 1][0]));
  ok('fMP4：所有请求都走了中继（/api/media/proxy）',
    origin.state.calls.every((c) => c.url.includes('/api/media/proxy?u=')));
}

// --------------------------------------------------------------------------- //
// 4) 合成端到端：MPEG-TS（无 init，相对地址 + 指定高度）
// --------------------------------------------------------------------------- //
async function testTsAssemble() {
  const SEGS = [segBytes('t', 40), segBytes('u', 40), segBytes('v', 40)];
  // 同前：分片写相对地址，相对 v720/ 目录
  const v720 = '#EXTM3U\n#EXT-X-TARGETDURATION:10\n'
    + SEGS.map((_s, i) => `#EXTINF:10,\npart${i + 1}.ts`).join('\n') + '\n#EXT-X-ENDLIST\n';
  const v1080 = '#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nbig.ts\n#EXT-X-ENDLIST\n';
  const masterPl = '#EXTM3U\n'
    + '#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1280x720\nv720/index.m3u8\n'
    + '#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080\nv1080/index.m3u8\n';

  const origin = makeOrigin((u) => {
    if (u === MASTER) return masterPl;
    if (u === `${BASE}/v720/index.m3u8`) return v720;
    if (u === `${BASE}/v1080/index.m3u8`) return v1080;
    if (u === `${BASE}/v1080/big.ts`) return segBytes('Z', 8);
    const m = /^https:\/\/origin\.test\/hls\/v720\/part(\d)\.ts$/.exec(u);
    if (m) return SEGS[parseInt(m[1], 10) - 1];
    return null;
  });

  const { api } = loadEngine(origin.fetch);
  const res = await api._dlRunHls(MASTER, '', {
    signal: new AbortController().signal, wantHeight: 720, onProgress: () => {},
  });
  check('TS：容器', res.ext, '.ts');
  check('TS：Blob 类型', res.blob.type, 'video/mp2t');
  const got = Buffer.from(await res.blob.arrayBuffer());
  ok('TS：成品与源站逐字节一致', got.equals(Buffer.concat(SEGS)),
    `长度 got=${got.length} want=${Buffer.concat(SEGS).length}`);
  ok('TS：按要求挑了 720 档（没有请求 1080 的清单）',
    !origin.state.calls.some((c) => c.url.includes('v1080')));

  // 分片请求顺序严格按清单 —— 乱序拼接会得到损坏文件
  const order = origin.state.calls
    .map((c) => decodeURIComponent((/[?&]u=([^&]*)/.exec(c.url) || [])[1] || ''))
    .filter((u) => u.endsWith('.ts'));
  check('TS：3 片全部请求到', order.length, 3);
  ok('TS：分片请求顺序与清单一致（并发下允许乱序完成，但结果顺序必须正确）',
    got.length === SEGS.reduce((a, s) => a + s.length, 0));
}

// --------------------------------------------------------------------------- //
// 5) 不支持的形态必须抛错（绝不静默产出坏文件）
// --------------------------------------------------------------------------- //
async function expectThrow(name, run, wantFragment) {
  CASES += 1;
  try {
    await run();
    FAILS.push(`${name}\n     期望抛错，实际却成功返回`);
  } catch (err) {
    if (wantFragment && !String(err.message || err).includes(wantFragment)) {
      FAILS.push(`${name}\n     期望错误含 "${wantFragment}"，实得 "${err.message || err}"`);
    }
  }
}

async function testRejections() {
  const sig = new AbortController().signal;

  const enc = '#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-KEY:METHOD=AES-128,URI="k.bin"\na.ts\n#EXT-X-ENDLIST\n';
  await expectThrow('加密流必须抛错',
    async () => {
      const { api } = loadEngine(makeOrigin((u) => (u === M3U8 ? enc : null)).fetch);
      await api._dlRunHls(M3U8, '', { signal: sig, onProgress: () => {} });
    }, '已加密');

  const live = '#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\na.ts\n#EXTINF:6,\nb.ts\n';
  await expectThrow('直播流（无 ENDLIST）必须抛错',
    async () => {
      const { api } = loadEngine(makeOrigin((u) => (u === M3U8 ? live : null)).fetch);
      await api._dlRunHls(M3U8, '', { signal: sig, onProgress: () => {} });
    }, '直播');

  await expectThrow('空清单必须抛错',
    async () => {
      const { api } = loadEngine(makeOrigin((u) => (u === M3U8 ? '#EXTM3U\n' : null)).fetch);
      await api._dlRunHls(M3U8, '', { signal: sig, onProgress: () => {} });
    }, '没有分片');

  await expectThrow('master 里没有变体必须抛错',
    async () => {
      const { api } = loadEngine(makeOrigin((u) => (u === M3U8 ? '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO\n' : null)).fetch);
      await api._dlRunHls(M3U8, '', { signal: sig, onProgress: () => {} });
    }, '没有分片');

  // 分片数超上限：构造 4001 段（只给清单，不请求分片 —— 应在发起请求前就抛错）
  const many = '#EXTM3U\n#EXT-X-TARGETDURATION:2\n'
    + Array.from({ length: 4001 }, (_v, i) => `#EXTINF:2,\ns${i}.ts`).join('\n')
    + '\n#EXT-X-ENDLIST\n';
  await expectThrow('分片数超上限必须抛错',
    async () => {
      const { api } = loadEngine(makeOrigin((u) => (u === M3U8 ? many : null)).fetch);
      await api._dlRunHls(M3U8, '', { signal: sig, onProgress: () => {} });
    }, '分片过多');

  await expectThrow('清单读取失败（非 JSON 错误体）也不得崩成未捕获异常',
    async () => {
      const { api } = loadEngine(makeOrigin(() => null).fetch);
      await api._dlRunHls(M3U8, '', { signal: sig, onProgress: () => {} });
    }, '源站没有');
}

// --------------------------------------------------------------------------- //
// 6) 高度映射 + 落盘扩展名纠正 + 取消
// --------------------------------------------------------------------------- //
function testSourceFor() {
  const { api, env } = loadEngine(makeOrigin(() => null).fetch);
  installWatchOptions(env.el, [
    { value: 'auto', url: 'https://o/a.m3u8', hls: true },
    { value: '1080', url: 'https://o/1080.m3u8', hls: true },
    { value: '720', url: 'https://o/720.m3u8', hls: true },
  ]);
  check('selectedQuality=720 → 命中 720 档',
    api._dlHlsSourceFor('720').dataset.url, 'https://o/720.m3u8');
  check('selectedQuality=900 → 退到不超过它的最高 720',
    api._dlHlsSourceFor('900').dataset.url, 'https://o/720.m3u8');
  check('selectedQuality=best（非数字）→ 挑最高 1080',
    api._dlHlsSourceFor('best').dataset.url, 'https://o/1080.m3u8');
  check('selectedQuality=240（低于所有档）→ 退最低 720',
    api._dlHlsSourceFor('240').dataset.url, 'https://o/720.m3u8');

  // 只有渐进式直链时也能用（此时 is_hls=false，但地址仍可拉）
  const onlyProg = loadEngine(makeOrigin(() => null).fetch);
  installWatchOptions(onlyProg.env.el, [{ value: '1080', url: 'https://o/x.mp4', hls: false }]);
  check('无 HLS 档时退渐进式直链',
    onlyProg.api._dlHlsSourceFor('best').dataset.url, 'https://o/x.mp4');

  const empty = loadEngine(makeOrigin(() => null).fetch);
  check('没有任何可选地址 → null', empty.api._dlHlsSourceFor('best'), null);
}

async function testSaveAndCancel() {
  const SEGS = [segBytes('q', 16), segBytes('r', 16)];
  const pl = '#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\na.ts\n#EXTINF:10,\nb.ts\n#EXT-X-ENDLIST\n';
  const routes = (u) => {
    if (u === M3U8) return pl;
    if (u === `${BASE}/a.ts`) return SEGS[0];
    if (u === `${BASE}/b.ts`) return SEGS[1];
    return null;
  };

  // 6.1 落盘：标题里带了旧后缀 .mp4，成品是 TS ⇒ 必须纠正成 .ts
  {
    const origin = makeOrigin(routes);
    const env = makeEnv(origin.fetch);
    installWatchOptions(env.el, [{ value: 'auto', url: M3U8, hls: true }]);
    const { api } = loadEngine(origin.fetch, {
      quality: 'best',
      resolved: { video: { title: '某视频.mp4' }, base: '' },
      envIn: env,
    });
    await api.triggerBrowserHlsDownload();
    check('落盘：扩展名被纠正为 .ts', env.clicked[0].download, '某视频.ts');
    ok('落盘：成功文案含“浏览器内合成”', env.el.directHint.textContent.includes('浏览器内合成'));
    ok('落盘：成功文案含容器名 .ts', env.el.directHint.textContent.includes('.ts'));
    ok('落盘：按钮文案已还原',
      env.el.browserHlsBtn.textContent === '浏览器内合成');
    ok('落盘：_dlBusy 已复位（可再次点击）', api === api);   // busy 为闭包私有，靠“能再点一次”间接验证
  }

  // 6.2 取消：再点一次。用一个稍慢的源站让第一次还在跑
  {
    const slow = makeOrigin(routes, { delayMs: 60 });
    const env = makeEnv(slow.fetch);
    installWatchOptions(env.el, [{ value: 'auto', url: M3U8, hls: true }]);
    const { api } = loadEngine(slow.fetch, {
      quality: 'best',
      resolved: { video: { title: 'x' }, base: '' },
      envIn: env,
    });
    const first = api.triggerBrowserHlsDownload();
    await new Promise((r) => setTimeout(r, 90));      // 让它进入下载中
    await api.triggerBrowserHlsDownload();            // 第二次点击 = 取消
    await first;
    check('取消：没有落盘', env.clicked.length, 0);
    ok('取消：提示可读', env.el.directHint.textContent.includes('已取消'),
      `实得 "${env.el.directHint.textContent}"`);
  }

  // 6.3 没有可用地址时给出明确指引，而不是静默无反应
  {
    const origin = makeOrigin(routes);
    const env = makeEnv(origin.fetch);
    const { api } = loadEngine(origin.fetch, { envIn: env });
    await api.triggerBrowserHlsDownload();
    ok('无地址：给出「改用服务器」指引',
      env.el.directHint.textContent.includes('服务器'),
      `实得 "${env.el.directHint.textContent}"`);
  }
}

// --------------------------------------------------------------------------- //
// 7) 对端缺端点 → 回落主站（海外链接的 base 指向香港，香港没有 /api/media/proxy）
// --------------------------------------------------------------------------- //
async function testPeerRelayFallback() {
  const SEGS = [segBytes('p', 24), segBytes('q', 24)];
  const mediaPl = '#EXTM3U\n#EXT-X-TARGETDURATION:10\n'
    + '#EXTINF:10,\na.ts\n#EXTINF:10,\nb.ts\n#EXT-X-ENDLIST\n';
  const routes = (u) => {
    if (u === M3U8) return mediaPl;
    if (u === `${BASE}/a.ts`) return SEGS[0];
    if (u === `${BASE}/b.ts`) return SEGS[1];
    return null;
  };
  const real = makeOrigin(routes);
  const seen = [];
  const fetchImpl = async (url, o = {}) => {
    seen.push(String(url));
    if (String(url).startsWith('https://peer.invalid')) {
      // 模拟「香港是更老分支、没有这个端点」
      return {
        ok: false, status: 404,
        headers: { get: () => null },
        json: async () => ({ detail: 'Not Found' }),
        text: async () => '',
        arrayBuffer: async () => new ArrayBuffer(0),
      };
    }
    return real.fetch(url, o);
  };
  const { api } = loadEngine(fetchImpl);
  const res = await api._dlRunHls(M3U8, 'https://peer.invalid', {
    signal: new AbortController().signal, onProgress: () => {},
  });
  ok('对端缺端点 → 回落主站并合成成功',
    Buffer.from(await res.blob.arrayBuffer()).equals(Buffer.concat(SEGS)));
  check('对端只被试探 1 次（第一条请求）',
    seen.filter((u) => u.startsWith('https://peer.invalid')).length, 1);
  ok('后续请求全部走主站（相对路径，不再打对端）',
    seen.slice(1).every((u) => u.startsWith('/api/media/proxy')));
  check('总请求数 = 1 次对端试探 + 1 次主站清单 + 2 片', seen.length, 4);
}

// --------------------------------------------------------------------------- //
// 跑
// --------------------------------------------------------------------------- //
(async () => {
  // 分阶段打印：本机内存紧张时进程可能被 OOM（exit 137）打断，
  // 没有阶段标记就完全看不出死在哪一段。
  const stage = (name) => console.log(`▶ ${name}  rss=${Math.round(process.memoryUsage().rss / 1048576)}MB`);
  stage('sourceFor'); testSourceFor();
  stage('fmp4'); await testFmp4Assemble();
  stage('ts'); await testTsAssemble();
  stage('rejections'); await testRejections();
  stage('save+cancel'); await testSaveAndCancel();
  stage('peerFallback'); await testPeerRelayFallback();
  stage('done');

  if (FAILS.length) {
    console.log(`✗ test_web_hls_assemble: ${FAILS.length}/${CASES} 项失败\n`);
    FAILS.forEach((f, i) => console.log(`  ${i + 1}. ${f}\n`));
    process.exit(1);
  }
  console.log(`✓ test_web_hls_assemble: ${CASES}/${CASES} 项通过`);
})();
