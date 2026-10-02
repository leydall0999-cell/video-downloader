/* 扩展 popup「清晰度回显 = 实际发送」行为测试（node 直跑，DOM/chrome 用桩）。
 *
 * 被测现象（2026-10-02 用户截图）：下拉明明选的「2K 1440P」，下面却写
 * 「将以「最佳画质（自动）」下载」，实际也是按 best 下载的。根因是 render(st) 里
 * `state = st`，而后台 getState 快照（background.js 的 snap）**没有 quality 字段**
 * → 用户选的档位当场被抹成 undefined；下拉 DOM 因为 `state.quality &&` 守卫没被重置，
 * 视觉上还停在 1440P。于是「能选、看着生效、实际没生效」。
 *
 * 为什么必须写成行为测试而不是源码断言：这次 bug 的全部要害都在**两次渲染之间**
 * —— grep `state.quality` 到处都是，grep 不出「哪一次渲染会把它带走」。
 *
 * 运行：node extension/tests/test_popup_quality_state.js
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

const POPUP = fs.readFileSync(path.join(__dirname, '..', 'popup.js'), 'utf8');

const QUALITY_OPTIONS = ['best', '2160', '1440', '1080', '720', '480', '360', 'audio'];
const OPTION_TEXT = {
  best: '最佳画质（自动）', 2160: '4K 2160P', 1440: '2K 1440P', 1080: '1080P 高清',
  720: '720P 高清', 480: '480P 标清', 360: '360P 流畅', audio: '仅音频 MP3',
};

// ---- DOM 桩：节点带 parent 指针 + 全局登记，让 querySelector('.empty-q') 语义接近真实 ----
const REGISTRY = [];
function mkNode(tag, id) {
  const el = {
    tagName: tag, id: id || '', textContent: '', className: '', hidden: false,
    checked: false, value: '', innerHTML: '', title: '', disabled: false, type: '', style: {},
    options: QUALITY_OPTIONS.map((v) => ({ value: v, textContent: OPTION_TEXT[v] })),
    children: [], _handlers: {}, _parent: null,
    addEventListener(t, fn) { (this._handlers[t] = this._handlers[t] || []).push(fn); },
    fire(t, ev) { (this._handlers[t] || []).forEach((fn) => fn(ev || {})); },
    dispatch(t, ev) { this.fire(t, ev); },
    appendChild(c) { c._parent = this; this.children.push(c); return c; },
    insertBefore(c) { c._parent = this; this.children.push(c); return c; },
    querySelector: () => null,
    querySelectorAll: () => [],
    closest: () => null,
    setAttribute: () => {},
    getAttribute: () => null,
  };
  // innerHTML 写入模拟「清空子节点」：旧的 .empty-q 就此从 DOM 消失
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
  'loginWarn', 'segNote', 'sendNote', 'reDetect', 'sendAll', 'sendPage', 'clearAll'];
function byId(id) {
  if (!ELS[id]) ELS[id] = mkNode('div', id);
  return ELS[id];
}
// 只暴露当前挂在 DOM 上的 .empty-q（最近一次渲染创建的那个）
function findEmptyQ() {
  for (let i = REGISTRY.length - 1; i >= 0; i--) {
    if (REGISTRY[i].className === 'empty-q') return REGISTRY[i];
  }
  return null;
}

const documentStub = {
  getElementById: byId,
  querySelector: (sel) => (sel === '.empty-q' ? findEmptyQ() : null),
  querySelectorAll: () => [],
  createElement: (t) => mkNode(t),
  body: mkNode('body'),
};

// ---- 环境桩 ----
const MEM = {};                                  // chrome.storage.local
let snapshot = null;                             // 下一次 getState 的返回（**照抄 background.js：没有 quality 字段**）
let desktopLoggedIn = true;

function makeChrome() {
  return {
    runtime: {
      lastError: null,
      sendMessage: (msg, cb) => {
        const t = msg && msg.type;
        if (t === 'getEndpoint') { if (cb) cb({ endpoint: 'http://127.0.0.1:8321' }); return; }
        if (t === 'getState') { if (cb) cb(snapshot); return; }
        if (cb) cb({});
      },
    },
    storage: {
      local: {
        get: (keys, cb) => { const o = {}; keys.forEach((k) => { if (k in MEM) o[k] = MEM[k]; }); cb(o); },
        set: (o, cb) => { Object.assign(MEM, o); if (cb) cb(); },
      },
    },
    tabs: {
      query: (q, cb) => {
        const f = typeof q === 'function' ? q : cb;
        f([{ id: 1, url: 'https://www.youtube.com/watch?v=abc', title: '标题' }]);
      },
    },
  };
}

globalThis.fetch = () => Promise.resolve({
  ok: true, status: 200,
  json: () => Promise.resolve({ desktop_logged_in: desktopLoggedIn }),
});
globalThis.document = documentStub;
globalThis.chrome = makeChrome();

function freshSnapshot() {
  // 与 background.js 的 getState 快照同构：**故意不带 quality**
  return {
    enabled: true, carryCookie: true, endpoint: 'http://127.0.0.1:8321',
    items: [], segments: [], sentUrls: {},
    videoPage: 'https://www.youtube.com/watch?v=abc',
    pagePushAt: 1759300000, pushOk: 1, pushErr: 0,
  };
}

function boot() {
  REGISTRY.length = 0;
  Object.keys(ELS).forEach((k) => delete ELS[k]);
  snapshot = freshSnapshot();
  const ctx = vm.createContext(globalThis);
  vm.runInContext(POPUP, ctx, { filename: 'popup.js' });
  return ctx;
}

const tick = () => new Promise((r) => setTimeout(r, 25));
const footerText = (ctx) => {
  const el = findEmptyQ();
  return el ? el.textContent : '(无空状态回显)';
};
const sentQuality = (ctx, kind) => ctx.qualityForItem({ kind: kind, url: 'https://x/y' });

(async () => {
  console.log('▶ 扩展 popup 清晰度「回显 = 实际发送」契约');

  // ---- ① 打开面板：记忆值 1440 必须三处一致（下拉 / 回显 / 实际发送）----
  MEM.sendQuality = '1440';
  let ctx = boot();
  await tick();
  await tick();
  check('① 下拉停在记忆档 2K 1440P', byId('sendQuality').value === '1440', byId('sendQuality').value);
  check('① 状态保留 1440（render 不再抹掉）', ctx.state.quality === '1440', ctx.state.quality);
  check('① 且 state.quality 有值（不靠 DOM 兜底）', !!ctx.state.quality, ctx.state.quality);
  check('① 空状态回显「将以 2K 1440P 下载」'
    + '（截图里这里写的是「最佳画质（自动）」）', /2K 1440P/.test(footerText(ctx)), footerText(ctx));
  check('① 实际发送 quality=1440（页面类条目）', sentQuality(ctx, 'page') === '1440', sentQuality(ctx, 'page'));

  // ---- ② 一次后台刷新（pollResult / 心跳都会触发 refresh → render）不得把它带走 ----
  ctx.refresh();                       // 等价于「下载回执轮询后又渲染了一次」
  await tick();
  check('② 再渲染一次仍然是 1440（本次 bug 的要害）', ctx.state.quality === '1440', ctx.state.quality);
  check('② 回显仍与下拉一致', /2K 1440P/.test(footerText(ctx)), footerText(ctx));
  check('② 发送仍是 1440', sentQuality(ctx, 'page') === '1440', sentQuality(ctx, 'page'));

  // ---- ③ 用户改选 → 三处同步 ----
  byId('sendQuality').fire('change', { target: { value: '2160' } });
  check('③ 改选后 state = 2160', ctx.state.quality === '2160', ctx.state.quality);
  check('③ 改选后回显变 4K 2160P', /4K 2160P/.test(footerText(ctx)), footerText(ctx));
  check('③ 改选后立即写回 storage', MEM.sendQuality === '2160', MEM.sendQuality);
  check('③ 改选后发送 2160', sentQuality(ctx, 'page') === '2160', sentQuality(ctx, 'page'));

  // ---- ④ 非法值不许污染（下拉被注入脏值时回落 best）----
  byId('sendQuality').fire('change', { target: { value: '8k-hack' } });
  check('④ 非法档回落 best', ctx.state.quality === 'best', ctx.state.quality);
  check('④ 非法档不写脏 storage', MEM.sendQuality === 'best', MEM.sendQuality);

  // ---- ⑤ 直链/分片不带清晰度（单一流，带档位反而挑不到流）----
  byId('sendQuality').fire('change', { target: { value: '1080' } });
  check('⑤ 直链条目不携带 quality', sentQuality(ctx, 'media') === '', sentQuality(ctx, 'media'));
  check('⑤ 分片条目不携带 quality', sentQuality(ctx, 'segment') === '', sentQuality(ctx, 'segment'));
  check('⑤ 页面条目携带 quality=1080', sentQuality(ctx, 'page') === '1080', sentQuality(ctx, 'page'));
  check('⑤ 清单条目携带 quality=1080', sentQuality(ctx, 'playlist') === '1080', sentQuality(ctx, 'playlist'));

  // ---- ⑥ 没有任何记忆值 → 默认 best，且回显说人话 ----
  Object.keys(MEM).forEach((k) => delete MEM[k]);
  ctx = boot();
  await tick();
  await tick();
  check('⑥ 无记忆值时默认 best', ctx.state.quality === 'best', ctx.state.quality);
  check('⑥ 默认 best 的回显是「最佳画质（自动）」', /最佳画质（自动）/.test(footerText(ctx)), footerText(ctx));

  console.log('');
  console.log('=========================================');
  console.log('  通过: ' + PASS + '   失败: ' + FAIL);
  console.log('=========================================');
  if (FAIL) {
    console.error('（清晰度回显测试失败 —— 用户会遇到「选了分辨率却没生效」）');
    process.exit(1);
  }
})();
