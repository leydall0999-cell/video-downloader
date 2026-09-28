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
  ['https://v.com/x.m4s', 'video/iso.segment', 'segment'],
  // 站内 UI/接口资源（2026-09-27 用户实测）：YouTube 搜索页的语音搜索音效被当成「直链」
  // 列进面板，用户复制粘贴到工坊后按 youtube:tab 页面解析 → 报「视频解析失败」。
  ['https://www.youtube.com/s/search/audio/success.mp3', 'audio/mpeg', ''],
  ['https://www.youtube.com/s/search/audio/no_input.mp3', 'audio/mpeg', ''],
  ['https://www.youtube.com/youtubei/v1/player', 'application/json', ''],
  // 只吃「该主机的内部路径」：别的站点同样路径、以及 YouTube 的非内部路径都照常展示
  ['https://cdn.example.com/s/search/audio/success.mp3', 'audio/mpeg', 'media'],
  ['https://www.youtube.com/clip/audio/real.mp3', 'audio/mpeg', 'media']
];
MATRIX.forEach(function (row, i) {
  eq(CORE.classifyMedia(row[0], row[1]), row[2], 'classifyMedia #' + i + ' ' + row[0]);
});

// ---- 1b) isNoiseUrl：只吃「指定主机的内部路径」，不吃同名路径的别的站点 ----
[
  ['https://www.youtube.com/s/search/audio/open.mp3', true],
  ['https://m.youtube.com/s/search/audio/open.mp3', true],
  ['https://www.youtube.com/youtubei/v1/browse', true],
  ['https://www.youtube.com/ptracking', true],
  ['https://www.youtube.com/generate_204', true],
  ['https://www.youtube.com/watch?v=dQw4w9WgXcQ', false],
  ['https://cdn.example.com/s/search/audio/open.mp3', false],
  ['https://notyoutube.com/s/search/audio/open.mp3', false],
  ['not a url', false]
].forEach(function (row, i) {
  eq(CORE.isNoiseUrl(row[0]), row[1], 'isNoiseUrl #' + i + ' ' + row[0]);
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

// ---- 9) TabStores：按标签页分库（「只保存当前页」的底座，2026-09-29） ----
var tb = new CORE.TabStores(3, 200);
tb.add(11, { url: 'https://b.alipay.com/a.mp4', mime: 'video/mp4', pageTitle: '商家平台', ts: 1 });
tb.add(11, { url: 'https://b.alipay.com/b.m4s', mime: 'video/mp4', ts: 2 });
tb.add(22, { url: 'https://www.baidu.com/x.mp4', mime: 'video/mp4', pageTitle: '百度一下', ts: 3 });

eq(tb.list(11).length, 1, '标签页 11 只有 1 条可下载项（分片不算）');
eq(tb.list(11)[0].pageTitle, '商家平台', '标签页 11 是支付宝那页的条目');
eq(tb.list(22).length, 1, '标签页 22 只有 1 条');
eq(tb.list(22)[0].pageTitle, '百度一下', '标签页 22 是百度那页的条目');
ok(tb.list(11).every(function (it) { return it.pageTitle !== '百度一下'; }),
  '★ 分库隔离：别的页面嗅到的条目不会串到当前页');
eq(tb.segments(11).length, 1, '标签页 11 的分片占位独立');
eq(tb.segments(22).length, 0, '标签页 22 没有分片占位');
eq(tb.count(11), 1, 'count(tab) 只数该页');
eq(tb.totalCount(), 2, 'totalCount 汇总所有页');
eq(tb.list(999).length, 0, '不存在的标签页返回空列表（不新建库）');

tb.clearTab(11);
eq(tb.list(11).length, 0, 'clearTab 清掉该页');
eq(tb.list(22).length, 1, '★ clearTab 不误伤别的页');

// 主文档导航 = 同一个 tabId 换页 → 旧页条目清掉、新页重新记
tb.add(22, { url: 'https://www.baidu.com/x.mp4', mime: 'video/mp4', ts: 4 });
eq(tb.list(22).length, 1, '同页同 URL 去重（不新增条目）');
eq(tb.list(22)[0].count, 2, '同页重复命中累加 count');
tb.clearTab(22);
tb.add(22, { url: 'https://v.qq.com/new.mp4', mime: 'video/mp4', pageTitle: '新页', ts: 5 });
eq(tb.list(22).length, 1, '换页后只留新页条目');
eq(tb.list(22)[0].pageTitle, '新页', '换页后是新页的数据');

// 关页丢库 / 恢复时只删不存在的页
tb.dropTab(22);
eq(tb.list(22).length, 0, 'dropTab 清掉整库');
tb.add(33, { url: 'https://c/v.mp4', mime: 'video/mp4', ts: 6 });
tb.dropTabsExcept([33, 44]);
eq(tb.list(33).length, 1, '★ dropTabsExcept 不动现存标签页的数据（SW 空闲重启不该丢当前页）');
ok(tb.list(44).length === 0 && tb.list(22).length === 0, '不存在的标签页分库被丢弃');

// 标签页数量上限：淘汰最早建库的，不淘汰刚进来的
var tb2 = new CORE.TabStores(2, 200);
tb2.add(1, { url: 'https://a/1.mp4', mime: 'video/mp4', ts: 1 });
tb2.add(2, { url: 'https://a/2.mp4', mime: 'video/mp4', ts: 2 });
tb2.add(3, { url: 'https://a/3.mp4', mime: 'video/mp4', ts: 3 });
eq(tb2.list(1).length, 0, '超上限时最早的标签页分库被淘汰');
eq(tb2.list(3).length, 1, '★ 新进来的标签页绝不会被淘汰');

// toJSON / loadFrom 往返（多标签页）
var snap2 = JSON.parse(JSON.stringify(tb.toJSON()));
ok(!!snap2.tabs, 'toJSON 输出 {tabs:{...}} 结构');
var tb3 = new CORE.TabStores(3, 200);
tb3.loadFrom(snap2);
eq(tb3.list(33).length, 1, 'loadFrom 恢复多标签页数据');
var tb4 = new CORE.TabStores(3, 200);
tb4.loadFrom({ items: [], segments: {} });   // 旧版（v1.0.33 全局单库）快照
eq(tb4.totalCount(), 0, '旧版单库快照不会把分库撑坏（安全降级为空）');

tb.clearAll();
eq(tb.totalCount(), 0, 'clearAll 清所有页');

// ---- 10) pageKeyOf：换页判定基准（丢 hash） ----
eq(CORE.pageKeyOf('https://b.alipay.com/home?x=1#frag'), 'https://b.alipay.com/home?x=1',
  'pageKeyOf 丢掉 hash');
eq(CORE.pageKeyOf('https://b.alipay.com/home#a'), CORE.pageKeyOf('https://b.alipay.com/home#b'),
  '★ 仅 hash 变化的页内跳转不算换页（不会误清当前页条目）');
ok(CORE.pageKeyOf('https://b.alipay.com/home') !== CORE.pageKeyOf('https://www.baidu.com/'),
  '★ 跨站点导航算换页');
ok(CORE.pageKeyOf('https://v.com/list?p=1') !== CORE.pageKeyOf('https://v.com/list?p=2'),
  'query 变化算换页');
eq(CORE.pageKeyOf('not a url'), 'not a url', '非 URL 原样返回（不抛异常）');
eq(CORE.pageKeyOf(''), '', '空串安全');

// ---- 汇总 ----
console.log('\n嗅探核心测试：通过 ' + passes + '，失败 ' + failures);
process.exit(failures ? 1 : 0);
