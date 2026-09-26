#!/usr/bin/env node
/**
 * 网页版「直链分片下载引擎」离线回归测试。
 *
 * 背景（2026-09-27，对标 DataTool 网页端下载）
 * -------------------------------------------
 * 我方原先的 `triggerDirectDownload()` 只是一个裸的 `<a download>`：单连接、无进度、
 * 无重试，源站一防盗链就失败。DataTool 的做法是「10MB/片 · 3 并发 · 3 重试 +
 * 媒体中继」。前端引擎是本次改动里最容易静默出错的一段（分片区间算错 → 文件损坏，
 * 并发数写错 → 被源站封，重试缺失 → 弱网必失败），所以用真源码 + 假源站把它钉住。
 *
 * 做法：从 `web/app.js` 按两个标记**抽取真实引擎源码**，注入假 `fetch`/`document`/`el`
 * 后在 Node 里直接运行。不复制算法（复制等于测副本，改坏了照样绿），也不打任何网络。
 *
 * 钉住的不变量：
 *   1. 分片参数与 DataTool 同档：10MB/片、3 并发、3 重试、<4MB 不分片；
 *   2. 分片区间 `bytes=a-b` 精确无误，3 片拼回来与源站**逐字节一致**（正确性底线）；
 *   3. 并发度确实 ≤ 3（实测并发峰值，不是读常量）；
 *   4. 单片失败会重试，且重试后结果仍然正确；
 *   5. 源站不认 Range（回 200 全量）→ 退单流；体积超上限 → 退单流；小文件 → 退单流；
 *   6. 中继整体不可用 → 降级为 `<a download>` 直连源站（不能白屏、不能吞错）；
 *   7. 再点一次按钮 = 取消（AbortError 分支可读）；
 *   8. 落盘文件名按 Content-Type / URL 补扩展名，已有扩展名不重复追加；
 *   9. 中继地址带上解析锁定节点的 base（海外站必须打对端）。
 *
 * 运行：node server/tests/test_web_direct_download.js
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
  console.error('❌ 无法从 web/app.js 提取下载引擎（标记缺失或顺序错乱）');
  console.error('   START=%s END=%s  →  标记被改名/移动时必须同步更新本测试', i0, i1);
  process.exit(2);
}
const ENGINE_SRC = src.slice(i0, i1);

// --------------------------------------------------------------------------- //
// 假 DOM / 假响应 / 假源站
// --------------------------------------------------------------------------- //
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function abortError() {
  const e = new Error('The operation was aborted.');
  e.name = 'AbortError';
  return e;
}

class FakeHeaders {
  constructor(init = {}) {
    this._m = {};
    for (const [k, v] of Object.entries(init)) this._m[String(k).toLowerCase()] = String(v);
  }
  get(name) {
    const v = this._m[String(name).toLowerCase()];
    return v === undefined ? null : v;
  }
}

class FakeResponse {
  constructor({ status = 200, headers = {}, body = Buffer.alloc(0), detail = null, delayMs = 0, onSettled = null, signal = null }) {
    this.status = status;
    this.ok = status >= 200 && status < 300;
    this.headers = new FakeHeaders(headers);
    this._body = body;
    this._detail = detail;
    this._delayMs = delayMs;
    this._onSettled = onSettled;
    this._settled = false;
    this._signal = signal;
  }
  _settle() {
    if (!this._settled) {
      this._settled = true;
      if (this._onSettled) this._onSettled();
    }
  }
  async json() {
    this._settle();
    if (this._detail === null) throw new Error('not json');
    return { detail: this._detail };
  }
  async arrayBuffer() {
    if (this._delayMs) await sleep(this._delayMs);
    // 只拷一次：先把内容取到一个精确长度的 ArrayBuffer 里再返回。
    // （早先写成 new Uint8Array(body).buffer.slice(0) 会拷两遍，本机内存吃紧时会被 OOM 掉）
    const buf = new ArrayBuffer(this._body.length);
    new Uint8Array(buf).set(this._body);
    this._settle();
    return buf;
  }
  get body() {
    const self = this;
    const step = Math.max(1, Math.ceil(self._body.length / 3));
    let off = 0;
    return {
      cancel: async () => { self._settle(); },
      getReader: () => ({
        read: async () => {
          if (self._delayMs) await sleep(self._delayMs);
          if (self._signal && self._signal.aborted) { self._settle(); throw abortError(); }
          if (off >= self._body.length) { self._settle(); return { done: true, value: undefined }; }
          const end = Math.min(self._body.length, off + step);
          const value = new Uint8Array(self._body.subarray(off, end));
          off = end;
          return { done: false, value };
        },
      }),
    };
  }
}

/** 源站内容按字节下标推导：byte(i) = i & 0xff（不驻留整份 body，省内存）。 */
const byteAt = (i) => i & 0xff;

function patternSlice(start, end) {
  const out = Buffer.allocUnsafe(end - start + 1);
  for (let i = 0; i < out.length; i += 1) out[i] = byteAt(start + i);
  return out;
}

function makeOrigin(opts = {}) {
  const {
    total = 1024,
    ranged = true,
    type = 'video/mp4',
    acceptRangesHeader = true,
    failFirstAttempts = {},
    delayMs = 0,
  } = opts;

  const state = { calls: [], attempts: {}, inflight: 0, maxInflight: 0, url: null };

  const raw = (url, o = {}) => {
    const headers = o.headers || {};
    const rng = headers.Range || headers.range || '';
    state.calls.push({ url, range: rng });
    state.attempts[rng] = (state.attempts[rng] || 0) + 1;
    if (failFirstAttempts[rng] && state.attempts[rng] <= failFirstAttempts[rng]) {
      return new FakeResponse({ status: 500, detail: 'boom', headers: {} });
    }
    if (!rng || !ranged) {
      return new FakeResponse({
        status: 200,
        headers: {
          'Content-Type': type,
          'Content-Length': String(total),
          ...(acceptRangesHeader ? { 'Accept-Ranges': 'bytes' } : {}),
        },
        body: patternSlice(0, total - 1),
        delayMs,
        signal: o.signal,
      });
    }
    const m = /^bytes=(\d+)-(\d*)$/.exec(rng.trim());
    if (!m) throw new Error(`源站收到无法解析的 Range: ${rng}`);
    const start = parseInt(m[1], 10);
    const end = m[2] ? Math.min(parseInt(m[2], 10), total - 1) : total - 1;
    return new FakeResponse({
      status: 206,
      headers: {
        'Content-Type': type,
        'Content-Length': String(end - start + 1),
        'Content-Range': `bytes ${start}-${end}/${total}`,
        ...(acceptRangesHeader ? { 'Accept-Ranges': 'bytes' } : {}),
      },
      body: patternSlice(start, end),
      delayMs,
      signal: o.signal,
    });
  };

  const fetchImpl = async (url, o = {}) => {
    state.url = url;
    state.inflight += 1;
    state.maxInflight = Math.max(state.maxInflight, state.inflight);
    const resp = raw(url, o);
    // 「请求收尾」才算释放并发额度 —— 这样并发峰值反映的是真正的重叠
    const orig = resp._onSettled;
    resp._onSettled = () => {
      if (orig) orig();
      state.inflight -= 1;
    };
    return resp;
  };

  return { state, fetch: fetchImpl, total };
}

function makeEnv(fetchImpl) {
  const clicked = [];
  const saved = [];
  const el = {
    directHint: { textContent: '', hidden: true },
    downloadBtn: { disabled: false, lastChild: { textContent: '直接保存到本机 ⬇' } },
    serverFallbackBtn: { hidden: true },
  };
  const document = {
    body: { appendChild() {} },
    createElement() {
      const node = {
        tag: 'a',
        href: '',
        download: '',
        rel: '',
        click() { clicked.push({ href: node.href, download: node.download }); },
        remove() {},
      };
      return node;
    },
  };
  const URLShim = {
    createObjectURL(blob) { saved.push(blob); return 'blob:fake'; },
    revokeObjectURL() {},
  };
  return { el, document, URL: URLShim, clicked, saved };
}

function loadEngine(fetchImpl) {
  const env = makeEnv(fetchImpl);
  const factory = new Function(
    'el', 'formatBytes', 'formatEta', 'fetch', 'document', 'URL',
    'AbortController', 'performance', 'setTimeout',
    `${ENGINE_SRC}\nreturn { triggerDirectDownload, _dlRun, _dlProbe, _dlChunk, _dlWithExt,`
    + ` _dlExtFromUrl, _dlRelayUrl, _DL_CHUNK, _DL_PARALLEL, _DL_RETRY, _DL_MIN_CHUNKED, _DL_MAX_CHUNKS };`,
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
  );
  return { api, env };
}

// --------------------------------------------------------------------------- //
// 断言小工具
// --------------------------------------------------------------------------- //
let PASS = 0;

function ok(cond, msg) {
  if (!cond) {
    console.error(`❌ ${msg}`);
    process.exit(1);
  }
}

/** 与「源站按下标推导的内容」逐字节比对（不驻留期望副本，省内存）。 */
function samePattern(u8, offset, label) {
  for (let i = 0; i < u8.length; i += 1) {
    const want = byteAt(offset + i);
    if (u8[i] !== want) {
      ok(false, `${label}：第 ${offset + i} 字节 ${u8[i]} != ${want}（分片拼接错位）`);
    }
  }
}

function done(msg) {
  PASS += 1;
  console.log(`✅ ${msg}`);
}

// --------------------------------------------------------------------------- //
// 1. 参数与文件名
// --------------------------------------------------------------------------- //
function testParamsAndNaming() {
  const { api } = loadEngine(async () => { throw new Error('不该发请求'); });
  ok(api._DL_CHUNK === 10 * 1024 * 1024, `分片应 10MB，实际 ${api._DL_CHUNK}`);
  ok(api._DL_PARALLEL === 3, `并发应 3，实际 ${api._DL_PARALLEL}`);
  ok(api._DL_RETRY === 3, `重试应 3，实际 ${api._DL_RETRY}`);
  ok(api._DL_MIN_CHUNKED === 4 * 1024 * 1024, `分片阈值应 4MB，实际 ${api._DL_MIN_CHUNKED}`);
  ok(api._DL_MAX_CHUNKS * api._DL_CHUNK >= 3 * 1024 ** 3,
    '分片上限不应低于 3GB（会误退单流）');
  done(`分片参数与 DataTool 同档（${api._DL_CHUNK / 1024 / 1024}MB/片 · ${api._DL_PARALLEL} 并发 · ${api._DL_RETRY} 重试）`);
}

function testExtensionMapping() {
  const { api } = loadEngine(async () => { throw new Error('不该发请求'); });
  const cases = [
    [['我的视频', 'video/mp4', ''], '我的视频.mp4'],
    [['已命名.mkv', 'video/mp4', ''], '已命名.mkv'],            // 已有扩展名不重复追加
    [['音频', 'audio/mp4', ''], '音频.m4a'],
    [['未知', 'application/octet-stream', ''], '未知'],
    [['未知', '', 'https://x.invalid/a/b/c.webm?t=1'], '未知.webm'], // 无 type 时从 URL 兜底
    [['', 'video/webm', ''], 'video.webm'],                     // 空标题兜底
    [['   ', 'video/mp4', ''], 'video.mp4'],
  ];
  for (const [[title, type, url], want] of cases) {
    const got = api._dlWithExt(title, type, url);
    ok(got === want, `_dlWithExt(${JSON.stringify([title, type, url])}) = ${got}，期望 ${want}`);
  }
  done('落盘文件名按 Content-Type / URL 补扩展名，已有扩展名不重复追加');
}

function testRelayUrlKeepsNodeBase() {
  const { api } = loadEngine(async () => { throw new Error('不该发请求'); });
  const u = 'https://rr3.googlevideo.com/videoplayback?id=1&sig=abc';
  ok(api._dlRelayUrl(u, '') === `/api/media/proxy?u=${encodeURIComponent(u)}`,
    '同节点应打相对路径');
  ok(api._dlRelayUrl(u, 'https://hk.hanyuxz.top').startsWith('https://hk.hanyuxz.top/api/media/proxy?u='),
    '解析锁定对端时必须打对端中继（否则 googlevideo 的 IP 签名对不上）');
  done('中继地址带解析锁定节点的 base');
}

// --------------------------------------------------------------------------- //
// 2. 分片路径（核心正确性）
// --------------------------------------------------------------------------- //
async function testProbeReadsTotalFromContentRange() {
  const origin = makeOrigin({ total: 12345678 });
  const { api } = loadEngine(origin.fetch);
  const p = await api._dlProbe('/api/media/proxy?u=x', new AbortController().signal);
  ok(p.size === 12345678, `总长度应为 12345678，实际 ${p.size}`);
  ok(p.ranged === true, '应判定可分段');
  ok(p.type === 'video/mp4', `类型应为 video/mp4，实际 ${p.type}`);
  ok(origin.state.calls[0].range === 'bytes=0-0', `探测应发 bytes=0-0，实际 ${origin.state.calls[0].range}`);
  done('探测用 bytes=0-0 从 Content-Range 读出总长度与类型');
}

async function testChunkedDownloadByteExactWithConcurrency() {
  const chunk = 10 * 1024 * 1024;
  const total = chunk * 2 + 12345;              // 3 片：10MB + 10MB + 12345B
  const origin = makeOrigin({ total, delayMs: 5 });
  const { api, env } = loadEngine(origin.fetch);

  const progress = [];
  const blob = await api._dlRun('https://x.invalid/a.mp4', {
    base: '',
    title: 't',
    signal: new AbortController().signal,
    onProgress: (d, tt) => progress.push([d, tt]),
  });
  const got = new Uint8Array(await blob.arrayBuffer());
  ok(got.length === total, `拼接长度 ${got.length} != ${total}`);
  samePattern(got, 0, '分片拼接');

  const rangeCalls = origin.state.calls.filter((c) => c.range && c.range !== 'bytes=0-0');
  ok(rangeCalls.length === 3, `应发 3 个分片请求，实际 ${rangeCalls.length}`);
  ok(origin.state.maxInflight <= api._DL_PARALLEL,
    `并发峰值 ${origin.state.maxInflight} 超过上限 ${api._DL_PARALLEL}`);
  ok(origin.state.maxInflight >= 2,
    `并发峰值只有 ${origin.state.maxInflight}，说明退化成串行了`);
  ok(progress.length === 3, `进度回调应恰好 3 次（每片一次），实际 ${progress.length}`);
  ok(progress[progress.length - 1][0] === total, '最后进度应等于总长度');
  for (let i = 1; i < progress.length; i += 1) {
    ok(progress[i][0] > progress[i - 1][0], '进度必须单调递增');
  }
  ok(env.saved.length === 0, '_dlRun 不应负责落盘');
  done(`分片下载逐字节一致（${(total / 1024 / 1024).toFixed(2)}MB/3 片），并发峰值 ${origin.state.maxInflight}`);
}

async function testChunkRetryThenSucceed() {
  const chunk = 10 * 1024 * 1024;
  const total = chunk + 1000;
  const second = `bytes=${chunk}-${total - 1}`;
  const origin = makeOrigin({ total, failFirstAttempts: { [second]: 2 }, delayMs: 1 });
  const { api } = loadEngine(origin.fetch);

  const blob = await api._dlRun('https://x.invalid/a.mp4', {
    base: '', title: 't', signal: new AbortController().signal, onProgress: () => {},
  });
  const got = new Uint8Array(await blob.arrayBuffer());
  samePattern(got, 0, '重试后拼接');
  ok(origin.state.attempts[second] === 3,
    `失败片应恰好尝试 3 次（2 败 1 成），实际 ${origin.state.attempts[second]}`);
  done('单片失败自动重试，重试后结果仍逐字节正确');
}

async function testChunkFailuresFallBackToSingleStream() {
  const chunk = 10 * 1024 * 1024;
  const total = chunk + 1000;
  const second = `bytes=${chunk}-${total - 1}`;
  // 某一片永远 500 → 分片整条失败 → 小文件应就地退单流重来（而不是直接放弃）
  const origin = makeOrigin({ total, failFirstAttempts: { [second]: 99 } });
  const { api } = loadEngine(origin.fetch);
  const blob = await api._dlRun('https://x.invalid/a.mp4', {
    base: '', title: 't', signal: new AbortController().signal, onProgress: () => {},
  });
  samePattern(new Uint8Array(await blob.arrayBuffer()), 0, '退单流后拼接');
  ok(origin.state.attempts[second] === 3,
    `失败片应恰好尝试 3 次后放弃，实际 ${origin.state.attempts[second]}`);
  ok(origin.state.calls.some((c) => !c.range), '分片失败后必须再发一个无 Range 的单流请求');
  done('分片失败（小文件）→ 就地退单流，结果仍逐字节正确');
}

async function testLargeFileChunkFailureDoesNotRedownload() {
  // 600MB > 512MB 兜底上限：不能再整份重下一遍（白烧用户流量与服务器带宽），
  // 直接把错误抛给上层 → 上层降级为浏览器直连源站。
  const total = 600 * 1024 * 1024;
  const { api } = loadEngine(async (url, o = {}) => {
    const rng = (o.headers || {}).Range;
    if (rng === 'bytes=0-0') {
      return new FakeResponse({
        status: 206,
        headers: { 'Content-Type': 'video/mp4', 'Content-Length': '1', 'Content-Range': `bytes 0-0/${total}` },
        body: Buffer.from([1]),
      });
    }
    return new FakeResponse({ status: 500, detail: 'boom', headers: {} });
  });
  let err = null;
  try {
    await api._dlRun('https://x.invalid/big.mp4', {
      base: '', title: 't', signal: new AbortController().signal, onProgress: () => {},
    });
  } catch (e) { err = e; }
  ok(err !== null, '大文件分片失败必须抛出（不能静默重下一遍）');
  done('超大文件分片失败 → 直接抛出（不重下，交由上层降级）');
}

async function testShortChunkResponseIsRejectedNotAssembled() {
  const chunk = 10 * 1024 * 1024;
  const total = chunk + 4096;
  const signal = new AbortController().signal;
  // 模拟「按最大分片尺寸裁剪 Range 的 CDN」：请求 10MB，只回 5MB。
  const { api } = loadEngine(async (url, o = {}) => {
    const rng = (o.headers || {}).Range || '';
    if (rng === 'bytes=0-0') {
      return new FakeResponse({
        status: 206,
        headers: { 'Content-Type': 'video/mp4', 'Content-Length': '1', 'Content-Range': `bytes 0-0/${total}` },
        body: Buffer.from([1]),
      });
    }
    const m = /^bytes=(\d+)-(\d+)$/.exec(rng);
    const start = parseInt(m[1], 10);
    const end = parseInt(m[2], 10);
    const realEnd = start + Math.max(0, Math.floor((end - start) / 2));
    return new FakeResponse({
      status: 206,
      headers: {
        'Content-Type': 'video/mp4',
        'Content-Length': String(realEnd - start + 1),
        'Content-Range': `bytes ${start}-${realEnd}/${total}`,
      },
      body: patternSlice(start, realEnd),
    });
  });
  // 直接对单片断言：必须报「长度不符」，绝不能把半片当成整片收下
  let err = null;
  try {
    await api._dlChunk('/api/media/proxy?u=x', 0, chunk - 1, signal);
  } catch (e) { err = e; }
  ok(err !== null, '源站只回半片时必须抛错');
  ok(/分片长度不符/.test(err.message), `报错应指明长度不符，实际：${err.message}`);
  done('源站裁短 Range → 明确报错（绝不把半片当整片，避免静默损坏）');
}

// --------------------------------------------------------------------------- //
// 3. 三条降级链
// --------------------------------------------------------------------------- //
async function testOriginIgnoringRangeFallsBackToSingleStream() {
  const origin = makeOrigin({ total: 5 * 1024 * 1024, ranged: false });
  const { api } = loadEngine(origin.fetch);
  const blob = await api._dlRun('https://x.invalid/a.mp4', {
    base: '', title: 't', signal: new AbortController().signal, onProgress: () => {},
  });
  const got = new Uint8Array(await blob.arrayBuffer());
  samePattern(got, 0, '单流降级');
  const rangedCalls = origin.state.calls.filter((c) => c.range && c.range !== 'bytes=0-0');
  ok(rangedCalls.length === 0, `源站不认 Range 时不应再切分片，实际发了 ${rangedCalls.length} 个`);
  done('源站忽略 Range → 自动退单流（仍逐字节正确）');
}

async function testSmallFileUsesSingleStream() {
  const origin = makeOrigin({ total: 512 * 1024 });     // 512KB < 4MB
  const { api } = loadEngine(origin.fetch);
  const blob = await api._dlRun('https://x.invalid/a.mp4', {
    base: '', title: 't', signal: new AbortController().signal, onProgress: () => {},
  });
  samePattern(new Uint8Array(await blob.arrayBuffer()), 0, '小文件');
  const rangedCalls = origin.state.calls.filter((c) => c.range && c.range !== 'bytes=0-0');
  ok(rangedCalls.length === 0, '小文件不该分片（3 个请求的开销不划算）');
  done('小于 4MB 的文件走单流（不分片）');
}

async function testHugeFileUsesSingleStream() {
  // 5GB：超过 _DL_MAX_CHUNKS * _DL_CHUNK 上限，必须避免把 400 个分片全塞内存。
  // 注意：绝不能让 makeOrigin 真去分配 5GB（那会把测试进程 OOM 掉），
  // 所以这里只谎报 Content-Range 的总长度，实际 body 只有 2 字节。
  const tiny = Buffer.from('ok');
  const chunkCalls = [];
  const { api } = loadEngine(async (url, o = {}) => {
    const rng = (o.headers || {}).Range;
    if (rng && rng !== 'bytes=0-0') { chunkCalls.push(rng); }
    if (rng) {
      return new FakeResponse({
        status: 206,
        headers: { 'Content-Type': 'video/mp4', 'Content-Length': '1', 'Content-Range': 'bytes 0-0/5368709120' },
        body: Buffer.from([1]),
      });
    }
    return new FakeResponse({
      status: 200,
      headers: { 'Content-Type': 'video/mp4', 'Content-Length': '5368709120' },
      body: tiny,
    });
  });
  const blob = await api._dlRun('https://x.invalid/huge.mp4', {
    base: '', title: 't', signal: new AbortController().signal, onProgress: () => {},
  });
  const got = Buffer.from(await blob.arrayBuffer());
  ok(got.equals(tiny), '超大文件应走单流');
  ok(chunkCalls.length === 0, `超大文件不该切片（会吃爆内存），实际切了 ${chunkCalls.length} 片`);
  done('超过分片上限的文件走单流（保护内存）');
}

async function testRelayFailureFallsBackToAnchorDownload() {
  const origin = { fetch: async () => new FakeResponse({ status: 403, detail: '防盗链被拒' }) };
  const { api, env } = loadEngine(origin.fetch);
  const url = 'https://x.invalid/dir/video.mp4?token=1';
  await api.triggerDirectDownload(url, '某标题', '');
  ok(env.clicked.length === 1, `中继失败必须降级为 <a download>，实际点击 ${env.clicked.length} 次`);
  ok(env.clicked[0].href === url, `降级应直连源站，实际 ${env.clicked[0].href}`);
  ok(env.clicked[0].download === '某标题.mp4',
    `降级文件名应从 URL 补扩展名，实际 ${env.clicked[0].download}`);
  ok(/加速下载不可用/.test(env.el.directHint.textContent), `提示应说明已降级：${env.el.directHint.textContent}`);
  ok(env.el.downloadBtn.lastChild.textContent === '直接保存到本机 ⬇', '按钮文案必须复原');
  ok(env.el.serverFallbackBtn.hidden === true || env.el.serverFallbackBtn.hidden === false,
    'serverFallbackBtn 状态需可读');
  done('中继不可用 → 降级直连源站（带扩展名、有可读提示、按钮复原）');
}

// --------------------------------------------------------------------------- //
// 4. 取消与成功路径
// --------------------------------------------------------------------------- //
async function testCancelOnSecondClick() {
  let calls = 0;
  const hang = (url, o = {}) => {
    calls += 1;
    return new Promise((_resolve, reject) => {
      const sig = o.signal;
      if (!sig) return;
      if (sig.aborted) return reject(abortError());
      sig.addEventListener('abort', () => reject(abortError()));
    });
  };
  const { api, env } = loadEngine(hang);
  const p1 = api.triggerDirectDownload('https://x.invalid/a.mp4', '标题', '');
  await sleep(20);
  // 第二次点击 = 取消（async 函数始终返回 Promise，故用「有没有发起新请求」判定，而不是返回值）
  await api.triggerDirectDownload('https://x.invalid/a.mp4', '标题', '');
  await p1;
  ok(calls === 1, `取消不应另起一轮请求，实际发起 ${calls} 次`);
  ok(/已取消下载/.test(env.el.directHint.textContent),
    `提示应显示已取消，实际：${env.el.directHint.textContent}`);
  ok(env.saved.length === 0 && env.clicked.length === 0,
    '取消后不得落盘、也不得误触发直连降级（env.clicked 是降级入口）');
  ok(env.el.downloadBtn.lastChild.textContent === '直接保存到本机 ⬇', '取消后按钮文案必须复原');
  done('再点一次按钮 = 取消（AbortError 分支可读，且不误触发降级）');
}

async function testSuccessSavesBlobWithExtension() {
  // 刻意用 <4MB（走单流）：本用例考的是「落盘 + 文件名 + 按钮复原」，
  // 分片路径已由 _dlRun 的用例覆盖，这里不重复吃 20MB 内存。
  const total = 1024 * 1024;
  const origin = makeOrigin({ total, acceptRangesHeader: false });
  const { api, env } = loadEngine(origin.fetch);
  await api.triggerDirectDownload('https://x.invalid/a.mp4', '我的视频', '');
  ok(env.saved.length === 1, `应落盘一次，实际 ${env.saved.length}`);
  const got = new Uint8Array(await env.saved[0].arrayBuffer());
  ok(got.length === total, `落盘长度 ${got.length} != ${total}`);
  samePattern(got, 0, '成功落盘');
  ok(env.clicked.length === 1, '应触发一次下载');
  ok(env.clicked[0].href === 'blob:fake', '应通过 objectURL 落盘（不是直连源站）');
  ok(env.clicked[0].download === '我的视频.mp4',
    `文件名应补 .mp4，实际 ${env.clicked[0].download}`);
  ok(/已保存到本机/.test(env.el.directHint.textContent),
    `成功后应有可读反馈，实际：${env.el.directHint.textContent}`);
  ok(env.el.downloadBtn.lastChild.textContent === '直接保存到本机 ⬇', '成功后按钮文案必须复原');
  done('成功路径：objectURL 落盘、文件名补扩展名、按钮复原');
}

// --------------------------------------------------------------------------- //
async function main() {
  testParamsAndNaming();
  testExtensionMapping();
  testRelayUrlKeepsNodeBase();
  await testProbeReadsTotalFromContentRange();
  await testChunkedDownloadByteExactWithConcurrency();
  await testChunkRetryThenSucceed();
  await testChunkFailuresFallBackToSingleStream();
  await testLargeFileChunkFailureDoesNotRedownload();
  await testShortChunkResponseIsRejectedNotAssembled();
  await testOriginIgnoringRangeFallsBackToSingleStream();
  await testSmallFileUsesSingleStream();
  await testHugeFileUsesSingleStream();
  await testRelayFailureFallsBackToAnchorDownload();
  await testCancelOnSecondClick();
  await testSuccessSavesBlobWithExtension();
  console.log(`\n✅ 网页直链分片下载引擎回归测试全部通过（${PASS} 项）`);
}

main().catch((e) => {
  console.error('❌ 测试异常终止：', e && e.stack ? e.stack : e);
  process.exit(1);
});
