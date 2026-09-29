// tests/test_convert_poll_lifecycle.mjs — 音乐/图片转换「轮询定时器自杀停表」回归测试
//
// 2026-09-29 线上事故：网页版音乐格式转换永远卡在「转码中 30%」。
// 根因（nginx/应用日志实证 0 次 /api/convert/{id} 轮询，服务端 16s 就转完）：
//   musStartAllBtn 点击 → musEnsurePolling() 先启动 1.5s 定时器 → 此时文件还在上传
//   （status='uploading'，job_id 尚未返回）→ 第一个 tick 里
//   `if (!running.length) { musStopPolling(); }` 自杀式停表 → 之后 job_id 到手也
//   没人再重启轮询 → UI 永久停在占位的 30%。
// 修复（双保险）：
//   ① musPollAll/imgPollAll 加「活口」守卫：还有 uploading/running 项就不停表；
//   ② job_id 到手时 musEnsurePolling()/imgEnsurePolling() 再拉一次（幂等）。
// 本测试用假时钟 + 真实 app.js 源码切片，模拟「慢上传（6s）+ 慢 finish（3s）」，
// 断言：轮询必须存活到最后拿到 job_id 并 poll 出 completed。
// 变异验证：回退 musPollAll 守卫 → 本测试必红。
//
// 运行：node tests/test_convert_poll_lifecycle.mjs
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import assert from 'node:assert/strict';

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..', '..');   // 本文件在 server/tests/ 下
const appJs = readFileSync(join(repoRoot, 'web', 'app.js'), 'utf8');

// ===== 从真实 app.js 切出音乐转换模块（边界 = 两个区块注释） =====
const START = appJs.indexOf('// ===== 音乐格式转换');
const END = appJs.indexOf('// ===== 图片格式转换');
assert.ok(START > 0 && END > START, 'app.js 应包含「音乐格式转换」「图片格式转换」区块锚点');
const musBlock = appJs.slice(START, END);

// 静态守卫放在动态场景之后（文件末尾）：动态行为断言优先红，
// 静态文本断言兜底防「代码被挪走导致动态断言空转」。见文件末尾。

// ===== 假时钟 =====
function makeClock() {
  let now = 0;
  let seq = 1;
  const timers = new Map();   // id -> {fn, interval, nextAt}
  const api = {
    get now() { return now; },
    setInterval(fn, ms) {
      const id = seq++;
      timers.set(id, { fn, interval: ms, nextAt: now + ms });
      return id;
    },
    clearInterval(id) { timers.delete(id); },
    setTimeout(fn, ms) {
      const id = seq++;
      timers.set(id, { fn, interval: 0, nextAt: now + (ms || 0) });
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
    async advance(ms) {
      const until = now + ms;
      for (;;) {
        let nextId = null, nextAt = Infinity;
        for (const [id, t] of timers) {
          if (t.nextAt < nextAt) { nextAt = t.nextAt; nextId = id; }
        }
        if (nextId === null || nextAt > until) break;
        now = nextAt;
        const t = timers.get(nextId);
        if (t.interval > 0) t.nextAt = now + t.interval; else timers.delete(nextId);
        t.fn();                       // 回调内部可能再注册定时器
        await drain(4);               // 等 microtask / promise 链走完
      }
      now = until;
      await drain(4);
    },
    hasTimer(id) { return timers.has(id); },
  };
  return api;
}
const drain = (n) => new Promise(r => setImmediate(() => setImmediate(r))).then(() => (n > 1 ? drain(n - 1) : undefined));

// ===== 沙盒桩 =====
function mkEl() {
  return {
    _h: {}, hidden: false, textContent: '', innerHTML: '', value: '', checked: false,
    disabled: false, files: [],
    addEventListener(ev, h) { (this._h[ev] = this._h[ev] || []).push(h); },
    fire(ev, e) { (this._h[ev] || []).forEach(h => h(e || {})); },
    click() {}, classList: { add() {}, remove() {} }, dataset: {},
  };
}
const elProxy = new Proxy({}, {
  get(t, k) { if (typeof k === 'string' && !(k in t)) t[k] = mkEl(); return t[k]; },
});

async function runScenario({ uploadMs, finishMs }) {
  const clock = makeClock();
  const convertGets = [];
  let jobAssigned = false;

  const sandbox = {
    el: elProxy,
    UC_POLL_INTERVAL: 1500,
    deviceId: () => 'test-device',
    window: { VDL_API_BASE: '', VDL: {}, confirm: () => true },
    FormData: class { append() {} },
    fetch: () => Promise.resolve({ ok: true }),
    XMLHttpRequest: class {
      open(m, u) { this._url = u; }
      setRequestHeader() {}
      addEventListener(ev, h) { (this._h = this._h || {})[ev] = h; }
      send() {
        clock.setTimeout(() => {
          this.status = 200;
          this.responseText = JSON.stringify({ job_id: 'testjob0001' });
          this._h && this._h.load && this._h.load();
        }, finishMs);
      }
    },
    // 分片上传：延迟 uploadMs（> 一个 tick）后才 resolve，复刻「上传期间 tick 先到」
    ucUploadChunk: () => new Promise(res => clock.setTimeout(res, uploadMs)),
    request: (path) => {
      if (path.startsWith('/api/convert/')) {
        convertGets.push({ at: clock.now, path });
        // 前 2 次回 running（验证轮询持续存活），之后回 completed
        const st = convertGets.length <= 2
          ? { status: 'running', progress: 50, stage: '转码中' }
          : { status: 'completed', library_id: '' };
        return Promise.resolve(st);
      }
      return Promise.resolve({});
    },
    setInterval: clock.setInterval, clearInterval: clock.clearInterval,
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    localStorage: { getItem: () => null },  // 2026-09-29 app.js 的 finish XHR 会读登录 token
    console,
  };

  // eslint-disable-next-line no-new-func
  new Function('el', 'UC_POLL_INTERVAL', 'deviceId', 'window', 'FormData', 'fetch',
    'XMLHttpRequest', 'ucUploadChunk', 'request', 'setInterval', 'clearInterval',
    'setTimeout', 'clearTimeout', 'localStorage', 'console', `"use strict";\n${musBlock}`)(
    sandbox.el, sandbox.UC_POLL_INTERVAL, sandbox.deviceId, sandbox.window, sandbox.FormData,
    sandbox.fetch, sandbox.XMLHttpRequest, sandbox.ucUploadChunk, sandbox.request,
    sandbox.setInterval, sandbox.clearInterval, sandbox.setTimeout, sandbox.clearTimeout,
    sandbox.localStorage, sandbox.console);

  // ① 加一个 5.8MB 的网页文件（走真实 change 处理器 → musAddFiles）
  elProxy.musFileInput.files = [{ name: '阿刁-赵雷-16827758.mp3', size: 5.8 * 1024 * 1024,
    slice: (s, e) => ({ size: e - s }) }];   // Blob.slice 桩，musUploadOne 分片要用
  elProxy.musFileInput.fire('change');
  assert.ok(elProxy.musCount.textContent.includes('1 个文件'), '文件应已加入列表');

  // ② 点「开始转换」（批量入口，即事故路径）
  elProxy.musStartAllBtn.fire('click');

  // ③ 推进假时钟 60s（覆盖：上传 6s → finish 3s → 多个轮询 tick）
  await clock.advance(60_000);

  return { convertGets, statusText: elProxy.musList.innerHTML, jobAssigned: convertGets.length > 0 };
}

// ===== 场景 1：慢上传（6s）+ 慢 finish（3s）—— 事故复现路径 =====
{
  const { convertGets } = await runScenario({ uploadMs: 6000, finishMs: 3000 });
  assert.ok(convertGets.length >= 1,
    `轮询必须存活到 job_id 到手并发出 /api/convert/{id} 请求；实际发了 ${convertGets.length} 次（0 次 = 定时器被自杀停表，即线上事故）`);
  const firstPollAt = convertGets[0].at;
  assert.ok(firstPollAt >= 9000,
    `首次轮询应发生在 finish 返回（≈9s）之后，实际在 ${firstPollAt}ms`);
  // 拿到 job_id 后应持续轮询而不是只 poll 一次就哑火（防「只兜底一次」的半吊子修法）
  assert.ok(convertGets.length >= 3,
    `轮询应持续进行（实际 ${convertGets.length} 次）`);
}

// ===== 场景 2：极快路径（上传/finish 都瞬时）—— 不能因为守卫而不轮询/不停表 =====
{
  const { convertGets } = await runScenario({ uploadMs: 10, finishMs: 10 });
  assert.ok(convertGets.length >= 1, '快路径也必须正常轮询到 completed');
  // 全部结束后定时器应被回收（无 running/uploading 项 → musStopPolling）
}

// ===== 静态守卫（放最后：动态行为断言优先红） =====
assert.ok(/const live = musState\.list\.some\(x => x\.status === 'uploading' \|\| x\.status === 'running'\)/.test(musBlock),
  'musPollAll 必须有「活口」守卫：uploading/running 在途时绝不停表');
assert.ok(/const live = imgState\.list\.some\(x => x\.status === 'uploading' \|\| x\.status === 'running'\)/.test(appJs),
  'imgPollAll 必须有同款「活口」守卫');
assert.ok(/musEnsurePolling\(\);/.test(musBlock.split('const musFinishOne')[1] || ''),
  'musFinishOne 拿到 job_id 后必须 musEnsurePolling() 兜底重启轮询');

console.log('✅ 转换轮询生命周期回归测试通过（慢上传/快上传两场景 + 静态守卫）');
