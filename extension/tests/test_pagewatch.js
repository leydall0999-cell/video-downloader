/* pagewatch.js（页面侧哨兵）离线测试（2026-10-01 v1.0.40）。
 *
 * 为什么单独测：这条链路是「YouTube 嗅探不到」的根因所在 —— 后台 SW 只挂在
 * webRequest 的 main_frame 上，而 YouTube 换页是 SPA（不发主文档请求）。
 * 哨兵负责在页面侧发现「换到了视频页 / 播放器起播」并唤醒后台。
 *
 * 跑法：node extension/tests/test_pagewatch.js（真实源码 + DOM/chrome 桩）
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

// ---- 页面环境桩 ----
const msgs = [];
globalThis.chrome = {
  runtime: {
    lastError: null,
    sendMessage: (m, cb) => { msgs.push(m); if (cb) cb(); },
  },
};
let tickFn = null;
let intervalMs = 0;
globalThis.setInterval = (fn, ms) => { tickFn = fn; intervalMs = ms; return 1; };
globalThis.clearInterval = () => {};
globalThis.document = {
  title: '某视频',
  readyState: 'complete',
  querySelector: () => null,
  addEventListener: () => {},
};
globalThis.location = { href: 'https://example.com/' };

// sniff-core 是 manifest 里 content_scripts 的第一个文件：哨兵靠它做初筛
require(path.join(__dirname, '..', 'sniff-core.js'));   // → globalThis.VDLSniffCore
globalThis.self = globalThis;                            // 哨兵读 self.VDLSniffCore

vm.runInThisContext(
  fs.readFileSync(path.join(__dirname, '..', 'pagewatch.js'), 'utf8'),
  { filename: 'pagewatch.js' }
);

ok(typeof tickFn === 'function', '哨兵已启动并注册轮询');
eq(intervalMs, 2000, '轮询周期 2s（前 1 分钟）');
eq(msgs.length, 0, '★ 非视频页：加载即不打扰后台（一个字节都不发）');

// ---- SPA 换页：地址变了但没有主文档请求 ----
location.href = 'https://www.youtube.com/watch?v=abc12345678&list=RDx&index=6';
tickFn();
eq(msgs.length, 1, '★ 换到 YouTube 视频页 → 上报后台');
eq(msgs[0].type, 'pageSeen', '消息类型为 pageSeen');
eq(msgs[0].url, 'https://www.youtube.com/watch?v=abc12345678',
  '★ 上报归一化后的页面（list/index 噪声参数已丢）');
eq(msgs[0].why, 'poll', '上报原因为 poll');
eq(msgs[0].title, '某视频', '带上页面标题（桌面端列表显示更可读）');

tickFn();
eq(msgs.length, 1, '★ 同一页重复轮询不重发（不吵后台）');

location.href = 'https://www.youtube.com/feed/subscriptions';
tickFn();
eq(msgs.length, 1, '★ 非视频页（订阅列表）不上报');

location.href = 'https://www.bilibili.com/video/BV1xx411c7mD';
tickFn();
eq(msgs.length, 2, '★ 换到另一个视频页会再报一次');
eq(msgs[1].why, 'poll', '第二页原因仍为 poll');

// ---- 播放器就绪：同一页允许补报一次（用户确实在播这一页）----
document.querySelector = () => ({ duration: 12, readyState: 4 });
tickFn();
eq(msgs.length, 3, '★ 播放器就绪 → 同页补报一次');
eq(msgs[2].why, 'player', '补报原因为 player');
tickFn();
eq(msgs.length, 3, '同一页 player 只补报一次');

// ---- 无时长（video 还没加载出来）不算播放器就绪 ----
document.querySelector = () => ({ duration: NaN, readyState: 0 });
tickFn();
eq(msgs.length, 3, 'video 还没有时长 → 不算就绪，不补报');

console.log('\n哨兵（pagewatch）测试：通过 ' + passes + '，失败 ' + failures);
process.exit(failures ? 1 : 0);
