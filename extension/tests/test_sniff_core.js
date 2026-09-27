/* extension/sniff-core.js 离线测试（node 直跑，无 chrome.* 依赖）。
 *
 * classifyMedia 参数化用例与 server/tests 中 python 侧 classify_media 的
 * 判定矩阵同参数镜像（两边不许分叉）；另覆盖去重计数、分片聚合与清单
 * 取代、环形上限、pickHeaders、端口探测表。
 *
 * 运行：node extension/tests/test_sniff_core.js
 */
'use strict';

require('../sniff-core.js');
var CORE = globalThis.VDLSniffCore;

var passes = 0;
var failures = 0;

function ok(cond, msg) {
  if (cond) { passes++; return; }
  failures++;
  console.error('FAIL: ' + msg);
}

function eq(actual, expect, msg) {
  ok(actual === expect, msg + '（期望 ' + JSON.stringify(expect) +
    '，实际 ' + JSON.stringify(actual) + '）');
}

// ---- 1) classifyMedia：与 python classify_media 同参数镜像 ----
var MATRIX = [
  ['https://cdn.x.com/live/master.m3u8?sign=abc', 'application/vnd.apple.mpegurl', 'playlist'],
  ['https://cdn.x.com/live.m3u8', '', 'playlist'],
  ['https://v.com/dash.mpd', 'application/dash+xml', 'playlist'],
  ['https://v.com/M3U8-upper', '', ''],
  ['https://upos.b.com/v/30080.m4s?e=sig', 'video/mp4', 'segment'],
  ['https://cdn.x.com/seg-1.ts', '', 'segment'],
  ['https://v.com/full.mp4', 'video/mp4', 'media'],
  ['https://v.com/a.m4a', 'audio/mp4', 'media'],
  ['https://v.com/x.webm', 'video/webm', 'media'],
  ['https://v.com/subs.vtt', 'text/vtt', ''],
  ['https://v.com/api.json', 'application/json', ''],
  ['https://v.com/', 'text/html', ''],
  ['https://v.com/img.jpg', 'image/jpeg', ''],
  ['https://googlevideo.com/videoplayback?expire=1', '', ''],
  ['https://v.com/full.mp4', 'text/html', ''],
  // 扩展场景补充：带 charset 参数的 mime、ts 分片带 video/mp2t mime（对齐 python 顺序）
  ['https://v.com/live/index.m3u8', 'application/vnd.apple.mpegurl; charset=utf-8', 'playlist'],
  ['https://v.com/x.m4s', 'video/iso.segment', 'segment']
];
MATRIX.forEach(function (row, i) {
  eq(CORE.classifyMedia(row[0], row[1]), row[2], 'classifyMedia #' + i + ' ' + row[0]);
});

// ---- 2) pathSuffix：剥查询串、大小写归一、无后缀空 ----
eq(CORE.pathSuffix('https://a/v/1.MP4?sign=x'), '.mp4', 'pathSuffix 大小写+查询串');
eq(CORE.pathSuffix('https://a/videoplayback?e=1'), '', 'pathSuffix 无后缀');
eq(CORE.pathSuffix('not a url'), '', 'pathSuffix 非法 URL');

// ---- 3) pickHeaders ----
var picked = CORE.pickHeaders(
  [{ name: 'Content-Type', value: 'video/mp4' },
   { name: 'content-length', value: '12345' }],
  [{ name: 'Referer', value: 'https://p/watch' },
   { name: 'Cookie', value: 'sid=abc' }]
);
eq(picked.mime, 'video/mp4', 'pickHeaders mime');
eq(picked.contentLength, 12345, 'pickHeaders contentLength');
eq(picked.referer, 'https://p/watch', 'pickHeaders referer');
eq(picked.cookie, 'sid=abc', 'pickHeaders cookie');
var none = CORE.pickHeaders(null, null);
eq(none.mime, '', 'pickHeaders 空头不炸');

// ---- 4) SniffStore：URL 去重计数 ----
var s = new CORE.SniffStore(200);
for (var i = 0; i < 3; i++) {
  s.add({ url: 'https://c/x.m3u8?a=1', mime: '', ts: 1 });
}
eq(s.list().length, 1, '同 URL 去重为 1 条');
eq(s.list()[0].count, 3, '重复请求计数 +1');
eq(s.badgeCount(), 1, '徽标数只算可下载项');

// ---- 5) 分片聚合 + 清单取代占位 ----
var s2 = new CORE.SniffStore(200);
s2.add({ url: 'https://c/seg1.m4s?x=1', mime: 'video/mp4', ts: 1 });
s2.add({ url: 'https://c/seg2.m4s?x=2', mime: 'video/mp4', ts: 2 });
eq(s2.list().length, 0, '分片不进可下载列表');
eq(s2.segments().length, 1, '同主机分片聚合成 1 个占位');
eq(s2.segments()[0].count, 2, '占位计数 = 分片数');
s2.add({ url: 'https://c/master.m3u8', mime: 'application/vnd.apple.mpegurl', ts: 3 });
eq(s2.segments().length, 0, '清单到达 → 同主机分片占位被取代');
eq(s2.list().length, 1, '清单进可下载列表');
eq(s2.list()[0].kind, 'playlist', '清单类型正确');

// ---- 6) 环形上限：超限弹最旧 ----
var s3 = new CORE.SniffStore(3);
for (var j = 1; j <= 5; j++) {
  s3.add({ url: 'https://c/v' + j + '.mp4', mime: 'video/mp4', ts: j });
}
eq(s3.list().length, 3, '环形上限裁剪');
eq(s3.list()[0].url, 'https://c/v5.mp4', '最新在前');
ok(!s3.list().some(function (it) { return it.url === 'https://c/v1.mp4'; }), '最旧被弹出');

// ---- 7) toJSON / loadFrom 往返（SW 重启恢复） ----
var s4 = new CORE.SniffStore(200);
s4.add({ url: 'https://c/a.mp4', mime: 'video/mp4', cookie: 'sid=1', ts: 9 });
s4.add({ url: 'https://c/s.m4s', mime: 'video/mp4', ts: 9 });
var snap = JSON.parse(JSON.stringify(s4.toJSON()));
var s5 = new CORE.SniffStore(200);
s5.loadFrom(snap);
eq(s5.list().length, 1, 'loadFrom 恢复可下载项');
eq(s5.list()[0].cookie, 'sid=1', 'loadFrom 保留 cookie 字段');
eq(s5.segments().length, 1, 'loadFrom 恢复分片占位');

// ---- 8) probeBases：与 desktop_launcher._find_free_port 同一张表 ----
var bases = CORE.probeBases();
eq(bases[0], 'http://127.0.0.1:8321', '探测表起点 8321');
eq(bases.length, 30, '探测表长度 30');
ok(bases.every(function (b) { return /^http:\/\/127\.0\.0\.1:\d+$/.test(b); }), '探测表格式');

// ---- 汇总 ----
console.log('\n嗅探核心测试：通过 ' + passes + '，失败 ' + failures);
process.exit(failures ? 1 : 0);
