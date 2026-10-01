/* 页面侧哨兵（v1.0.40）—— 为什么必须加它：
 *
 * MV3 的后台 service worker 空闲 ~30s 就被 Chrome 挂起，而用户常常在装/更新扩展前
 * 就把 YouTube 页面开着了；YouTube 从首页点进某个视频又是 SPA 换页（不发主文档请求）。
 * 于是后台那条「视频页直推」一次都不触发 —— 2026-10-01 用户实测：弹窗显示
 * 「当前页没有嗅探到媒体流」、桌面端条目里零 YouTube 记录（腾讯视频等整页导航的站正常）。
 *
 * 内容脚本活在页面里，不受 SW 挂起影响，负责在三个时机通知后台：
 *   ① 加载完成；② 地址变化（SPA，低频轮询兜底）；③ 播放器就绪（video 出现且有时长）。
 * 判定规则只有一份：这里只做「是不是视频页」的**初筛**，用的是同一个 sniff-core.js
 * （manifest 的 content_scripts 里先加载它），后台再做一次权威判定与去重推送。
 *
 * 只对视频页发消息：非视频页 isVideoPage 返回 ''，一个字节都不发。
 */
(function () {
  'use strict';

  var CORE = (typeof self !== 'undefined' && self.VDLSniffCore) || null;
  if (!CORE || typeof CORE.isVideoPage !== 'function') return;

  var lastKey = '';      // 已上报过的归一化视频页 URL
  var lastWhy = '';      // 上一次上报的原因（'player' 用于「同一页也要补报一次播放器就绪」）
  var ticks = 0;
  var timer = null;

  function report(why) {
    var vp = '';
    try { vp = CORE.isVideoPage(location.href); } catch (e) { return; }
    if (!vp) return;
    // 同一页只报一次；只有「播放器就绪」允许在同一页补报一次
    if (vp === lastKey && (why !== 'player' || lastWhy === 'player')) return;
    lastKey = vp;
    lastWhy = why;
    try {
      chrome.runtime.sendMessage({
        type: 'pageSeen',
        url: vp,
        title: document.title || '',
        why: why
      }, function () { void chrome.runtime.lastError; });
    } catch (e) { /* 扩展被重载/停用：忽略 */ }
  }

  /** 播放器就绪 = 页面上已有 video 且拿到了时长（用户确实在播这一页）。 */
  function checkPlayer() {
    var v = null;
    try { v = document.querySelector('video'); } catch (e) { return; }
    if (!v) return;
    if (!(v.duration > 0) && !(v.readyState > 0)) return;
    report('player');
  }

  function tick() {
    report('poll');
    checkPlayer();
    ticks++;
    // 前 1 分钟 2s 一次（覆盖 SPA 换页与播放器起播），之后降到 10s 长期守候
    if (ticks === 30 && timer) {
      clearInterval(timer);
      timer = setInterval(tick, 10000);
    }
  }

  var start = function () {
    report('load');
    checkPlayer();
    timer = setInterval(tick, 2000);
  };

  if (document.readyState === 'complete' || document.readyState === 'interactive') {
    start();
  } else {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  }
})();
