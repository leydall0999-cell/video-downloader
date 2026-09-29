// server/tests/test_concat_flow.mjs — 视频拼接面板状态机回归测试（2026-09-29 用户反馈四连）
//
// 用户实测（hanyuxz.top 拼接面板）暴露的四个问题，本测试逐一钉死：
//   ① 拼接完成后「开始拼接」仍可点击 → 反复点会堆出多个「合并结果」行；
//   ② 拼接成功后源片段还堆在列表里；
//   ③ 底部状态永远停在「拼接中…」（mcPoll 从不停表、也不更新状态）；
//   ④ 输出格式/文件名区块与上方片段列表没有视觉分区（HTML/CSS 层，静态断言钉住）。
// 修复：拼接中禁用按钮（响应前同步禁用）+ 重拼接替换旧结果 + 成功后清本次源片段
//（只删本任务提交的 segIds，不误伤新加片段）+ mcPoll 停表并更新完成/失败文案。
//
// 做法：从 web/app.js 按「===== 视频拼接」注释 slice 真实源码（不测副本），
// 假时钟 + 假上传/假服务端驱动完整流程：加片段→自动上传→拼接→轮询→完成。
//
// 运行：node server/tests/test_concat_flow.mjs
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import assert from 'node:assert/strict';

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..', '..');   // 本文件在 server/tests/ 下
const appJs = readFileSync(join(repoRoot, 'web', 'app.js'), 'utf8');
const indexHtml = readFileSync(join(repoRoot, 'web', 'index.html'), 'utf8');
const stylesCss = readFileSync(join(repoRoot, 'web', 'styles.css'), 'utf8');

// ===== 静态断言 ④：输出设置分区 =====
assert.ok(/<div class="uc-bulk mc-output" id="mcActions">/.test(indexHtml), '拼接输出设置区块应有 mc-output 分区类');
assert.ok(indexHtml.includes('<div class="mc-output-title">输出设置</div>'), '拼接输出设置应有「输出设置」标题');
assert.ok(/\.mc-output \{ margin-top/.test(stylesCss) && /\.mc-output-title \{/.test(stylesCss), 'styles.css 应有 mc-output 分区样式');

// ===== 从真实 app.js 切出拼接模块 =====
const START = appJs.indexOf('// ===== 视频拼接（简单无损合并）');
const END = appJs.indexOf('\n  // 事件绑定', START);
assert.ok(START > 0 && END > START, 'app.js 应包含「视频拼接」区块且其后有「事件绑定」锚点');
const mcBlock = appJs.slice(START, END);

// ===== 假时钟 =====
function makeClock() {
  let now = 0; let seq = 1;
  const timers = new Map();
  const api = {
    get now() { return now; },
    setInterval(fn, ms) { const id = seq++; timers.set(id, { fn, interval: ms, nextAt: now + ms }); return id; },
    clearInterval(id) { timers.delete(id); },
    setTimeout(fn, ms) { const id = seq++; timers.set(id, { fn, interval: 0, nextAt: now + (ms || 0) }); return id; },
    clearTimeout(id) { timers.delete(id); },
    timerCount() { return timers.size; },
    async advance(ms) {
      // 先 drain 再找最近的到期定时器：promise 链（上传→finish→send）会在 microtask 里
      // 注册新定时器，必须循环处理到没有任何 ≤until 的定时器为止，否则会漏掉
      // 「advance 末尾才注册」的定时器（第一次跑就栽在这）。
      const until = now + ms;
      for (let guard = 0; guard < 10_000; guard++) {
        await drain(4);
        let nextId = null, nextAt = Infinity;
        for (const [id, t] of timers) {
          if (t.nextAt <= until && t.nextAt < nextAt) { nextAt = t.nextAt; nextId = id; }
        }
        if (nextId === null) break;
        now = nextAt;
        const t = timers.get(nextId);
        if (t.interval > 0) t.nextAt = now + t.interval; else timers.delete(nextId);
        t.fn();
      }
      now = until;
      await drain(4);
    },
  };
  return api;
}
const drain = (n) => new Promise(r => setImmediate(() => setImmediate(r))).then(() => (n > 1 ? drain(n - 1) : undefined));

// ===== 桩 =====
function mkEl() {
  return {
    _h: {}, hidden: false, textContent: '', innerHTML: '', value: '', checked: false, disabled: false, files: [],
    addEventListener(ev, h) { (this._h[ev] = this._h[ev] || []).push(h); },
    fire(ev, e) { (this._h[ev] || []).forEach(h => h(e || {})); },
    click() {}, classList: { add() {}, remove() {} }, dataset: {},
    querySelector: () => null,
  };
}
const els = {};
const doc = { getElementById: (id) => (els[id] = els[id] || mkEl()), addEventListener() {}, createElement: () => mkEl() };

function runScenario() {
  const clock = makeClock();
  let concatPosts = 0;
  let pollCount = 0;
  const sandbox = {
    document: doc,
    console,
    performance,
    Date,
    ucFormatSize: (b) => `${b}B`,
    ucFormatSpeed: (b) => `${b}B/s`,
    ucChStats: () => ({ total: 0, add() {} }),
    ucPickEndpoint: () => 'https://test',
    ucUploadChunk: () => Promise.resolve(),
    ucLastProgressByItem: new Map(),
    UC_UPLOAD_ENDPOINTS: ['https://test'],
    UC_CHUNK_SIZE: 4 * 1024 * 1024,
    UC_BIG_CHUNK_SIZE: 8 * 1024 * 1024,
    UC_CHUNK_CONCURRENCY: 2,
    UC_CHUNK_RETRIES: 3,
    UC_POLL_INTERVAL: 1500,
    UC_EXT_OF: { mp4: 'mp4', mov: 'mov', mkv: 'mkv', m4v: 'm4v', ts: 'ts', flv: 'flv', webm: 'webm' },
    deviceId: () => 'test-device',
    window: { VDL_API_BASE: '' },
    FormData: class { append() {} },
    fetch: () => Promise.resolve({ ok: true }),
    XMLHttpRequest: class {
      open() {}
      setRequestHeader() {}
      addEventListener(ev, h) { (this._h = this._h || {})[ev] = h; }
      send() {
        clock.setTimeout(() => {
          this.status = 200;
          this.responseText = JSON.stringify({ seg_id: 'seg' + Math.random().toString(36).slice(2, 6), seg_name: 'up_store_xxx.mp4' });
          this._h && this._h.load && this._h.load();
        }, 20);
      }
    },
    request: (path, opts) => {
      if (path === '/api/concat') {
        concatPosts++;
        return Promise.resolve({ job_id: 'cjt' + String(concatPosts).padStart(7, '0') });
      }
      if (path.startsWith('/api/convert/')) {
        pollCount++;
        return Promise.resolve(pollCount <= 2
          ? { status: 'running', progress: 50, stage: '拼接中' }
          : { status: 'completed', library_id: '' });
      }
      return Promise.resolve({});
    },
    setInterval: clock.setInterval, clearInterval: clock.clearInterval,
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    localStorage: { getItem: () => null },  // 2026-09-29 app.js 的 finish XHR 会读登录 token
  };
  // eslint-disable-next-line no-new-func
  new Function('document', 'console', 'performance', 'Date', 'ucFormatSize', 'ucFormatSpeed', 'ucChStats',
    'ucPickEndpoint', 'ucUploadChunk', 'ucLastProgressByItem', 'UC_UPLOAD_ENDPOINTS', 'UC_CHUNK_SIZE',
    'UC_BIG_CHUNK_SIZE', 'UC_CHUNK_CONCURRENCY', 'UC_CHUNK_RETRIES', 'UC_POLL_INTERVAL', 'UC_EXT_OF',
    'deviceId', 'window', 'FormData', 'fetch', 'XMLHttpRequest', 'request', 'localStorage',
    'setInterval', 'clearInterval', 'setTimeout', 'clearTimeout', `"use strict";\n${mcBlock}`)(
    sandbox.document, sandbox.console, sandbox.performance, sandbox.Date, sandbox.ucFormatSize,
    sandbox.ucFormatSpeed, sandbox.ucChStats, sandbox.ucPickEndpoint, sandbox.ucUploadChunk,
    sandbox.ucLastProgressByItem, sandbox.UC_UPLOAD_ENDPOINTS, sandbox.UC_CHUNK_SIZE, sandbox.UC_BIG_CHUNK_SIZE,
    sandbox.UC_CHUNK_CONCURRENCY, sandbox.UC_CHUNK_RETRIES, sandbox.UC_POLL_INTERVAL, sandbox.UC_EXT_OF,
    sandbox.deviceId, sandbox.window, sandbox.FormData, sandbox.fetch, sandbox.XMLHttpRequest, sandbox.request,
    sandbox.localStorage,
    sandbox.setInterval, sandbox.clearInterval, sandbox.setTimeout, sandbox.clearTimeout);
  return { clock, state: { get concatPosts() { return concatPosts; } } };
}

const rowCnt = () => (els.mcList.innerHTML.match(/data-id=/g) || []).length;
// 结果行 = 总行数 − 带上移按钮的行（两端结果行都没有 ↑↓ 按钮；web 端 title=「合并结果」，桌面端 title=输出名）
const resultCnt = () => rowCnt() - (els.mcList.innerHTML.match(/data-act="up"/g) || []).length;

// ===== 主流程 =====
{
  const { clock } = runScenario();

  // ① 加 2 个片段（5MB、chunk 4MB → 各 2 片），自动上传
  els.mcFileInput.files = [
    { name: '早教启蒙儿歌大全.mp4', size: 5 * 1024 * 1024, slice: (s, e) => ({ size: e - s }) },
    { name: '拔萝卜.mp4', size: 5 * 1024 * 1024, slice: (s, e) => ({ size: e - s }) },
  ];
  els.mcFileInput.fire('change', { target: els.mcFileInput });   // 处理器读 e.target.files
  await clock.advance(2000);   // 上传（瞬时）+ finish（20ms）
  assert.ok(/已就绪/.test(els.mcList.innerHTML), '两个片段应已上传就绪');
  assert.equal(els.mcMergeBtn.disabled, false, '片段就绪后「开始拼接」应可用');

  // ② 点拼接：响应返回前按钮必须已同步禁用（防双击重复提交）
  els.mcMergeBtn.fire('click');
  assert.equal(els.mcMergeBtn.disabled, true, '点击拼接后按钮应立即禁用');
  await clock.advance(200);
  assert.equal(resultCnt(), 1, '应只有一行「合并结果」');
  assert.equal(rowCnt(), 3, '2 片段 + 1 结果 = 3 行');

  // ③ 拼接进行中再点 → 必须被拦，且不新增结果行（①②的「堆多个结果」回归）
  els.mcMergeBtn.fire('click');
  assert.ok(/正在拼接中/.test(els.mcStatus.textContent), '拼接进行中再点应被拦截提示');
  assert.equal(resultCnt(), 1, '进行中重复点击不得新增结果行');

  // ④ 轮询至完成：状态文案更新、停表、源片段被清掉
  await clock.advance(15_000);
  assert.ok(/拼接完成/.test(els.mcStatus.textContent), `完成后的状态应是「拼接完成…」，实际：${els.mcStatus.textContent}`);
  assert.equal(rowCnt(), 1, '拼接成功后源片段应被移除，只剩结果行');
  assert.equal(resultCnt(), 1, '结果行保留供下载');
  assert.ok(/尚未添加/.test(els.mcCount.textContent), `片段计数应清零，实际：${els.mcCount.textContent}`);
  assert.equal(els.mcMergeBtn.disabled, true, '没有片段时拼接按钮应禁用');
  assert.equal(clock.timerCount(), 0, '拼接结束后轮询定时器应停止（③④的「永远拼接中」回归）');

  // ④b 再次添加片段：上次结果应降级为「上次结果」并置灰，新片段不混淆（2026-09-29 用户反馈）
  els.mcFileInput.files = [
    { name: '社会百态上.mp4', size: 5 * 1024 * 1024, slice: (s2, e) => ({ size: e - s2 }) },
    { name: '社会百态下.mp4', size: 5 * 1024 * 1024, slice: (s2, e) => ({ size: e - s2 }) },
  ];
  els.mcFileInput.fire('change', { target: els.mcFileInput });
  await clock.advance(2000);
  assert.ok(/上次结果/.test(els.mcList.innerHTML), '再次添加片段后旧结果应标注「上次结果」');
  assert.ok(/is-stale/.test(els.mcList.innerHTML), '旧结果行应带 is-stale 置灰类');
  assert.equal((els.mcList.innerHTML.match(/is-stale/g) || []).length, 1, '只有旧结果行置灰，新片段不置灰');

  // ④c 重新拼接：新结果用「输出文件名」命名，旧结果被替换（不堆叠）
  els.mcOutFormat.value = 'MKV';
  els.mcOutName.value = '合二';
  els.mcMergeBtn.fire('click');
  await clock.advance(200);
  assert.equal(resultCnt(), 1, '重新拼接应替换旧结果，不堆叠');
  assert.ok(/合二/.test(els.mcList.innerHTML), `拼接中新结果行应显示输出名「合二」，实际：${els.mcList.innerHTML.slice(0, 300)}`);
  await clock.advance(15_000);
  assert.ok(/\[MKV\]合二\.MKV/.test(els.mcList.innerHTML), `完成后的结果行应显示输出文件名 [MKV]合二.MKV，实际：${els.mcList.innerHTML.slice(0, 400)}`);
  assert.ok(/拼接完成/.test(els.mcStatus.textContent), '第二轮拼接完成后状态应更新');
  assert.equal(clock.timerCount(), 0, '第二轮结束后轮询定时器应停止');

  // ⑤ 结果行的 × 应可移除（此前是死按钮）
  const m = els.mcList.innerHTML.match(/data-id="(\d+)"/);
  assert.ok(m, '结果行应带 data-id');
  const li = { dataset: { id: m[1] } };
  const t = { dataset: { act: 'remove' }, closest: () => li };
  els.mcList.fire('click', { target: { closest: () => t } });
  assert.equal(rowCnt(), 0, '结果行的 × 应可移除最后一行');
}

console.log('✅ 拼接面板状态机回归测试通过（防重复结果/停表/清源片段/输出分区）');
