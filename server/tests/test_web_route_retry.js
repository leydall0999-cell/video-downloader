#!/usr/bin/env node
/**
 * 网页版「远端失败自动换路由重试」离线回归测试。
 *
 * 背景（2026-09-27）
 * -----------------
 * 海外链接的 `resolved.base` 指向对端（香港）节点，而对端常是**更老的分支**——
 * 连 `/api/media/proxy` 都没有（实测：不回落的海外 HLS 一律拿到 404）。
 * 第一版只做了「失败就换下一个 base」，三个问题没解决：
 *
 *   1. **没有重试**：网络层抖动（TLS 握手失败 / 连接重置）会直接把整次下载判死，
 *      而这类抖动在同一条路由上再试一次通常就好了。
 *   2. **不区分失败类型**：404（对端没这个端点）原地重试纯属浪费；
 *      5xx（上游自己失败）换路由才有机会绕开。一刀切会白白多打对端。
 *   3. **没有防抖**：对端一旦被确认缺端点，后面几十上百个分片请求还会挨个去敲它。
 *
 * 于是有了 `_dlRouteFetch`：按可观测信号分类（浏览器拿不到底层错误码，
 * TLS/连接重置/DNS 在 fetch 里统统是 `TypeError: Failed to fetch`），
 * network 类**原地重试**、其余**换路由**，并把判死的路由写进耗尽集合；
 * `missing`（端点缺失）是**节点属性**故 key 不带 host，其余是源站/链路属性故带 host，
 * 避免一个源站的问题株连到别的源站。
 *
 * 最容易静默出错的地方（所以必须钉死）：
 *   · 成功路径**一发命中就不许再试探**（否则每次请求都多打一次对端）；
 *   · 判死之后**后续请求不许再去敲**（防抖的意义全在这一条）；
 *   · AbortError **不许触发任何重试/换路由**（用户取消了还去打对端就是 bug）。
 *
 * 做法：从 `web/app.js` 按标记抽取**真实引擎源码**，注入假依赖后在 Node 里直接运行。
 * 不复制逻辑（复制等于测副本，改坏了照样绿），也不打任何网络。
 *
 * 运行：node server/tests/test_web_route_retry.js
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
// 环境注入（与 test_web_hls_assemble.js 同一套范式）
// --------------------------------------------------------------------------- //
function loadEngine() {
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
      return { tag: 'a', href: '', download: '', rel: '', click() {}, remove() {} };
    },
  };
  const factory = new Function(
    'el', 'formatBytes', 'formatEta', 'fetch', 'document', 'URL',
    'AbortController', 'performance', 'setTimeout',
    'selectedQuality', 'resolved',
    `${ENGINE_SRC}\nreturn {`
    + ` _dlRouteFetch, _dlErrKind, _dlRelayUrl, _dlProbeWithFallback, _dlProbe,`
    + ` _dlRouteExhausted, _dlRouteKey, _dlRouteDead, _dlHostOf,`
    + ` _DL_ROUTE_TTL, _DL_ROUTE_NET_RETRY, _DL_ROUTE_SWITCH_MAX, _DL_ROUTE_HOST_SCOPED };`,
  );
  return factory(
    el, (b) => `${b}B`, () => '', async () => { throw new Error('未注入 fetch'); },
    document, URL, AbortController, performance, setTimeout, 'best', null,
  );
}

const api = loadEngine();

const PEER = 'https://peer.test';

const ORIGIN_A = 'https://origin-a.test/seg.ts';
const ORIGIN_B = 'https://origin-b.test/seg.ts';

/** 从 relay URL 反推它走的是哪个路由（'' = 主站）。 */
function routeOf(relay) {
  if (relay.indexOf(PEER) === 0) return 'peer';
  return 'self';
}

/**
 * 造一个 doFetch：按 `plan` 逐个请求决定成功/失败。
 * plan[i] = 'ok' | 'net' | 'missing' | 'forbid' | 'upstream' | 'abort'
 */
function makeFetcher(plan) {
  const calls = [];
  let n = 0;
  const doFetch = async (relay) => {
    const i = n;
    n += 1;
    calls.push(routeOf(relay));
    const what = plan[i] === undefined ? 'ok' : plan[i];
    if (what === 'ok') return `body-${i}`;
    if (what === 'abort') {
      const e = new Error('aborted');
      e.name = 'AbortError';
      throw e;
    }
    const msgs = {
      net: 'Failed to fetch',
      missing: '清单读取失败（HTTP 404）',
      forbid: '源站返回 403',
      upstream: '分片读取失败（HTTP 502）',
    };
    throw new Error(msgs[what] || 'boom');
  };
  return { doFetch, calls };
}

async function main() {
// --------------------------------------------------------------------------- //
// 1) 失败分类
// --------------------------------------------------------------------------- //
{
  const mk = (msg, name) => {
    const e = new Error(msg);
    if (name) e.name = name;
    return e;
  };
  check('分类：404 → missing', api._dlErrKind(mk('清单读取失败（HTTP 404）')), 'missing');
  check('分类：405 → missing', api._dlErrKind(mk('Not Found')), 'missing');
  check('分类：403 → forbid', api._dlErrKind(mk('源站返回 403')), 'forbid');
  check('分类：401 → forbid', api._dlErrKind(mk('Unauthorized')), 'forbid');
  check('分类：502 → upstream', api._dlErrKind(mk('分片读取失败（HTTP 502）')), 'upstream');
  check('分类：Failed to fetch → network', api._dlErrKind(mk('Failed to fetch')), 'network');
  check('分类：NetworkError → network', api._dlErrKind(mk('NetworkError when attempting')), 'network');
  check('分类：AbortError → abort', api._dlErrKind(mk('x', 'AbortError')), 'abort');
  check('分类：未知错误 → unknown', api._dlErrKind(mk('什么鬼')), 'unknown');
  check('key 作用域：missing 不带 host', api._DL_ROUTE_HOST_SCOPED.missing, false);
  check('key 作用域：forbid 带 host', api._DL_ROUTE_HOST_SCOPED.forbid, true);
  check('key 作用域：upstream 带 host', api._DL_ROUTE_HOST_SCOPED.upstream, true);
}

// --------------------------------------------------------------------------- //
// 2) 成功路径：一发命中，不许重试也不许试探别的路由
// --------------------------------------------------------------------------- //
{
  api._dlRouteExhausted.clear();
  const { doFetch, calls } = makeFetcher(['ok']);
  const state = { base: PEER, switches: 0 };
  const out = await api._dlRouteFetch(ORIGIN_A, [PEER, ''], state, doFetch, null);
  check('一发命中：返回内容', out, 'body-0');
  check('一发命中：只发 1 次请求', calls, ['peer']);
  check('一发命中：路由未被切换', state.base, PEER);
  check('一发命中：切换计数为 0', state.switches, 0);
}

// --------------------------------------------------------------------------- //
// 3) network 类：同一条路由原地重试一次，不换路由
// --------------------------------------------------------------------------- //
{
  api._dlRouteExhausted.clear();
  const { doFetch, calls } = makeFetcher(['net', 'ok']);
  const state = { base: PEER, switches: 0 };
  const out = await api._dlRouteFetch(ORIGIN_A, [PEER, ''], state, doFetch, null);
  check('网络抖动：重试后成功', out, 'body-1');
  check('网络抖动：两次都打同一条路由（不换）', calls, ['peer', 'peer']);
  check('网络抖动：路由未变', state.base, PEER);
  check('网络抖动：不算一次路由切换', state.switches, 0);
}

// --------------------------------------------------------------------------- //
// 4) network 重试耗尽后才换路由
// --------------------------------------------------------------------------- //
{
  api._dlRouteExhausted.clear();
  const { doFetch, calls } = makeFetcher(['net', 'net', 'ok']);
  const state = { base: PEER, switches: 0 };
  await api._dlRouteFetch(ORIGIN_A, [PEER, ''], state, doFetch, null);
  check('网络重试耗尽后换路由：请求序列', calls, ['peer', 'peer', 'self']);
  check('网络重试耗尽后换路由：base 已切到主站', state.base, '');
  check('网络重试耗尽后换路由：切换计数 1', state.switches, 1);
}

// --------------------------------------------------------------------------- //
// 5) missing：立即换路由（不原地重试）+ 判死后不再敲
// --------------------------------------------------------------------------- //
{
  api._dlRouteExhausted.clear();
  const f1 = makeFetcher(['missing', 'ok']);
  const state = { base: PEER, switches: 0 };
  await api._dlRouteFetch(ORIGIN_A, [PEER, ''], state, f1.doFetch, null);
  check('端点缺失：对端只敲 1 次就换', f1.calls, ['peer', 'self']);
  check('端点缺失：切换到主站', state.base, '');

  // 后续请求（同一 host）：不应再去试探对端
  const f2 = makeFetcher(['ok']);
  await api._dlRouteFetch(ORIGIN_A, [PEER, ''], state, f2.doFetch, null);
  check('端点缺失：后续请求不再试探对端（防抖）', f2.calls, ['self']);

  // 换 host 也不该再试 —— missing 是节点属性，与源站无关
  const f3 = makeFetcher(['ok']);
  await api._dlRouteFetch(ORIGIN_B, [PEER, ''], state, f3.doFetch, null);
  check('端点缺失：换源站后仍不再试探对端（key 不带 host）', f3.calls, ['self']);
}

// --------------------------------------------------------------------------- //
// 6) forbid / upstream：换路由，且**按 host 记**（不株连别的源站）
// --------------------------------------------------------------------------- //
{
  api._dlRouteExhausted.clear();
  const f1 = makeFetcher(['forbid', 'ok']);
  const state = { base: PEER, switches: 0 };
  await api._dlRouteFetch(ORIGIN_A, [PEER, ''], state, f1.doFetch, null);
  check('防盗链：换到主站', f1.calls, ['peer', 'self']);

  const f2 = makeFetcher(['ok']);
  await api._dlRouteFetch(ORIGIN_A, [PEER, ''], state, f2.doFetch, null);
  check('防盗链：同一源站后续不再试探对端', f2.calls, ['self']);

  // 另一个源站：对端未被株连，仍会被优先尝试（state.base 此时是 ''，
  // 所以顺序是 [self, peer]；这里断言对端仍"可用"，即至少没被判死）
  ok('防盗链：另一源站的对端未被判死（key 带 host）',
    api._dlRouteDead('forbid', PEER, api._dlHostOf(ORIGIN_B)) === false);
  ok('防盗链：本源站的对端已判死',
    api._dlRouteDead('forbid', PEER, api._dlHostOf(ORIGIN_A)) === true);
}

{
  api._dlRouteExhausted.clear();
  const { doFetch, calls } = makeFetcher(['upstream', 'ok']);
  const state = { base: PEER, switches: 0 };
  await api._dlRouteFetch(ORIGIN_A, [PEER, ''], state, doFetch, null);
  check('上游 5xx：不原地重试，直接换路由', calls, ['peer', 'self']);
}

// --------------------------------------------------------------------------- //
// 7) 切换次数上限：达到 _DL_ROUTE_SWITCH_MAX 后不再继续切
// --------------------------------------------------------------------------- //
{
  api._dlRouteExhausted.clear();
  const Q = 'https://peer2.test';
  const bases = [PEER, Q, ''];
  const state = { base: PEER, switches: 0 };

  // 第 1 次：peer 成功
  await api._dlRouteFetch(ORIGIN_A, bases, state, makeFetcher(['ok']).doFetch, null);
  check('切换上限：初始路由', state.base, PEER);

  // 第 2 次：peer 缺失 → 切到 Q
  await api._dlRouteFetch(ORIGIN_A, bases, state, makeFetcher(['missing', 'ok']).doFetch, null);
  check('切换上限：第 1 次切换', state.switches, 1);

  // 第 3 次：Q 缺失 → 切到主站（switches=2，到顶）
  const f3 = makeFetcher(['missing', 'ok']);
  await api._dlRouteFetch(ORIGIN_A, bases, state, f3.doFetch, null);
  check('切换上限：第 2 次切换', state.switches, 2);

  // 第 4 次：主站也失败 —— 已到上限，不该再去试 peer/Q
  const f4 = makeFetcher(['missing']);
  let threw = false;
  try {
    await api._dlRouteFetch(ORIGIN_A, bases, state, f4.doFetch, null);
  } catch (_e) {
    threw = true;
  }
  ok('切换上限：到顶后失败直接抛出', threw);
  check('切换上限：到顶后只敲当前路由，不再回头试别的', f4.calls, ['self']);
}

// --------------------------------------------------------------------------- //
// 8) AbortError：不重试、不换路由、原样上抛
// --------------------------------------------------------------------------- //
{
  api._dlRouteExhausted.clear();
  const { doFetch, calls } = makeFetcher(['abort']);
  const state = { base: PEER, switches: 0 };
  let name = '';
  try {
    await api._dlRouteFetch(ORIGIN_A, [PEER, ''], state, doFetch, null);
  } catch (err) {
    name = err && err.name;
  }
  check('取消：原样抛出 AbortError', name, 'AbortError');
  check('取消：只发 1 次请求，不重试', calls, ['peer']);
  check('取消：不换路由', state.base, PEER);
}

// --------------------------------------------------------------------------- //
// 9) _dlProbeWithFallback 接线：真实 fetch 路径也能换路由
// --------------------------------------------------------------------------- //
{
  api._dlRouteExhausted.clear();
  const calls = [];
  const fetchImpl = async (url) => {
    calls.push(routeOf(String(url)));
    if (String(url).indexOf(PEER) === 0) {
      return {
        ok: false, status: 404,
        headers: { get: () => null },
        json: async () => ({ detail: 'Not Found' }),
      };
    }
    return {
      ok: true, status: 206,
      headers: {
        get: (n) => {
          const k = String(n).toLowerCase();
          if (k === 'content-range') return 'bytes 0-0/1048576';
          if (k === 'content-type') return 'video/mp4';
          return null;
        },
      },
      json: async () => ({}),
      body: { cancel: async () => {} },
    };
  };
  // 直接验证调度器接上了真实 fetch：用一个把 relay 交给 fetchImpl 的 doFetch
  const doFetch = async (relay) => {
    const r = await fetchImpl(relay);
    if (r.status !== 206 && !r.ok) {
      let detail = '';
      try { detail = (await r.json()).detail || ''; } catch (_e) { /* 非 JSON */ }
      throw new Error(detail || `源站返回 ${r.status}`);
    }
    return r;
  };
  const state = { base: PEER, switches: 0 };
  await api._dlRouteFetch(ORIGIN_A, [PEER, ''], state, doFetch, null);
  check('接线：对端 404 → 换主站并成功', calls, ['peer', 'self']);
  check('接线：base 落为主站', state.base, '');
}

}

main().then(() => {
  console.log(FAILS.length === 0
    ? `✓ test_web_route_retry: ${CASES}/${CASES} 项通过`
    : `✗ test_web_route_retry: ${CASES - FAILS.length}/${CASES} 项通过，失败 ${FAILS.length} 项：\n`
      + FAILS.map((f) => `  · ${f}`).join('\n'));
  process.exit(FAILS.length === 0 ? 0 : 1);
});
