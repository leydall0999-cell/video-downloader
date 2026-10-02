/* 视频工坊 媒体嗅探 —— 纯逻辑核心（无 chrome.* 依赖，便于 node 离线测试）。
 *
 * 判定矩阵与桌面端 server/cdp_sniffer.py 的 classify_media() 逐条对齐
 * （playlist / media / segment / 空），两边不许分叉 —— python 侧测试
 * tests/test_cdp_sniffer.py 的参数化用例在 extension/tests/test_sniff_core.js
 * 里有同参数镜像，改任何一边必须同步另一边。
 *
 * MV3 装载方式：background.js 用 importScripts('sniff-core.js') 引入（classic
 * service worker）；node 测试直接 require 本文件后取 globalThis.VDLSniffCore。
 */
(function (root) {
  'use strict';

  // ---- 与 server/cdp_sniffer.py 顶部常量一致（2026-09-27 对齐） ----
  var MIME_PLAYLIST = [
    'application/vnd.apple.mpegurl',
    'application/x-mpegurl',
    'application/dash+xml'
  ];
  var MIME_MEDIA_PREFIX = ['video/', 'audio/'];
  var MEDIA_SUFFIXES = ['.mp4', '.webm', '.m4v', '.mov', '.flv', '.mkv',
    '.mp3', '.m4a', '.aac', '.wav', '.ogg'];
  var IGNORED_SUFFIXES = ['.vtt', '.srt', '.ass', '.jpg', '.jpeg', '.png',
    '.webp', '.gif', '.css', '.js', '.html', '.json', '.xml', '.txt', '.ico'];

  // ---- 站点内部 UI / 交互资源：主机后缀 → 该主机上「永远不是用户内容」的路径 ----
  // 2026-09-27 用户实测补充。实证案例：在 YouTube 搜索页，站点会加载自己的语音搜索
  // 提示音 https://www.youtube.com/s/search/audio/{success,failure,no_input,open}.mp3
  // （响应 audio/mpeg），被嗅探成「直链」列进面板；用户复制粘贴到工坊后，链接被
  // 当成 youtube:tab 页面交给 yt-dlp → 报「视频解析失败」，而点「下载」也只会建出
  // 必然失败的任务。这类资源不是内容，必须在判定阶段就丢掉。
  // **与 server/cdp_sniffer.py 的 _NOISE_HOST_PATHS 逐条对齐，改一边必须改另一边。**
  var NOISE_HOST_PATHS = [
    [/(^|\.)youtube\.com$/, /^\/(?:s\/search|youtubei)\//],
    [/(^|\.)youtube\.com$/, /^\/(?:ptracking|generate_204)$/]
  ];

  function isNoiseUrl(url) {
    var u;
    try { u = new URL(url); } catch (e) { return false; }
    var host = (u.hostname || '').toLowerCase();
    var path = u.pathname || '';
    for (var i = 0; i < NOISE_HOST_PATHS.length; i++) {
      if (NOISE_HOST_PATHS[i][0].test(host) && NOISE_HOST_PATHS[i][1].test(path)) {
        return true;
      }
    }
    return false;
  }

  function pathSuffix(url) {
    // 对齐 python _path_suffix()：剥查询串、取路径 basename 的小写后缀（含点）。
    var path;
    try { path = new URL(url).pathname; } catch (e) { return ''; }
    var base = path.split('/').pop() || '';
    var dot = base.lastIndexOf('.');
    if (dot < 0) return '';
    return base.slice(dot).toLowerCase();
  }

  /** 判定资源类型：'playlist' | 'media' | 'segment' | ''（与媒体无关）。
   *  顺序必须与 python classify_media() 完全一致。 */
  function classifyMedia(url, mime) {
    mime = (mime || '').split(';')[0].trim().toLowerCase();
    var suffix = pathSuffix(url);
    if (isNoiseUrl(url)) return '';
    if (IGNORED_SUFFIXES.indexOf(suffix) >= 0) return '';
    if (mime.indexOf('image/') === 0 ||
        ['text/html', 'text/css', 'application/javascript', 'text/javascript']
          .indexOf(mime) >= 0) return '';
    if (MIME_PLAYLIST.indexOf(mime) >= 0 || suffix === '.m3u8' ||
        suffix === '.mpd' || mime.indexOf('mpegurl') >= 0) return 'playlist';
    if (mime === 'application/dash+xml') return 'playlist';
    for (var i = 0; i < MIME_MEDIA_PREFIX.length; i++) {
      if (mime.indexOf(MIME_MEDIA_PREFIX[i]) === 0) {
        // video/mp4 可能是整片直链也可能是 fMP4 分片：.m4s 一定是分片
        return suffix === '.m4s' ? 'segment' : 'media';
      }
    }
    if (suffix === '.m4s' || suffix === '.ts') return 'segment';
    if (MEDIA_SUFFIXES.indexOf(suffix) >= 0) return 'media';
    return '';
  }

  /** 视频**页面**识别（2026-10-01）：YouTube 等站点 2026 起改用 UMP/SABR 传输
   *  （响应头 application/vnd.yt-ump），媒体流抓不到、且抓到也没法直接下载。
   *  这些站点改用「推页面链接」——桌面端用 yt-dlp 解析（含 web_safari 免 POT 回退），
   *  反而更稳。返回归一化后的页面 URL（去掉无关查询参数），非视频页返回 ''。
   *  规则与 python _video_page_url() 必须逐条对齐。 */
  var PAGE_RULES = [
    [/^(www\.|m\.)?youtube\.com$/, /\/(watch|shorts\/|live\/)/, ['v']],
    [/^youtu\.be$/, /^\/[\w-]{6,}/, ['list', 'index']],
    [/^(www\.|m\.)?bilibili\.com$/, /\/(video|bangumi\/play)\//, ['p', 't']],
    [/^(www\.)?douyin\.com$/, /\/(video|note|discover)\//, []],
    [/^(www\.)?tiktok\.com$/, /\/video\/|^\/@[^/]+\/video\//, []],
    [/^(www\.)?vimeo\.com$/, /^\/\d+/, []],
    [/^(www\.)?instagram\.com$/, /\/(reel|p|tv)\//, []],
    [/^(www\.)?(x|twitter)\.com$/, /\/status\//, []],
    [/^(www\.)?kuaishou\.com$/, /\/(short-video|fw\/photo)\//, []],
    [/^(www\.)?xiaohongshu\.com$/, /\/(explore|discovery\/item)\//, []]
  ];

  function isVideoPage(url) {
    var u;
    try { u = new URL(url); } catch (e) { return ''; }
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return '';
    var host = u.hostname.toLowerCase();
    for (var i = 0; i < PAGE_RULES.length; i++) {
      var rule = PAGE_RULES[i];
      if (!rule[0].test(host)) continue;
      if (!rule[1].test(u.pathname + (u.pathname.endsWith('/') ? '' : ''))) continue;
      // 只留能定位到视频本身的参数，去掉播放列表/追踪等噪声
      var out = u.origin + u.pathname;
      var keep = rule[2];
      if (keep.length) {
        var parts = [];
        for (var j = 0; j < keep.length; j++) {
          var v = u.searchParams.get(keep[j]);
          if (v) parts.push(keep[j] + '=' + encodeURIComponent(v));
        }
        if (parts.length) out += '?' + parts.join('&');
      }
      return out;
    }
    return '';
  }

  /** 从 webRequest 头数组里挑出判定/回传所需字段。 */
  function pickHeaders(responseHeaders, requestHeaders) {
    var out = { mime: '', contentLength: 0, referer: '', cookie: '' };
    (responseHeaders || []).forEach(function (h) {
      var n = (h.name || '').toLowerCase();
      if (n === 'content-type') out.mime = h.value || '';
      else if (n === 'content-length') out.contentLength = parseInt(h.value, 10) || 0;
    });
    (requestHeaders || []).forEach(function (h) {
      var n = (h.name || '').toLowerCase();
      if (n === 'referer' && !out.referer) out.referer = h.value || '';
      else if (n === 'cookie' && !out.cookie) out.cookie = h.value || '';
    });
    return out;
  }

  /** 环形去重存储：URL 全量去重（media/playlist 各自计数），
   *  segment 按主机聚合成占位（清单到达即取代同主机占位）——
   *  行为对齐 python CDPSniffer._register()。 */
  function SniffStore(cap) {
    this.cap = cap || 200;
    this.items = [];      // 旧→新；仅 playlist / media
    this._index = {};     // url -> item
    this._segments = {};  // host -> 占位 item
  }

  /** entry: {url, mime, referer, pageUrl, pageTitle, cookie, ts, contentLength}
   *  contentLength：响应头 Content-Length（字节，0/缺省 = 未知）→ 存成 item.size，
   *  推到桌面端后展示「大小」（2026-10-02 用户「把标题、时长、大小也加上」）。
   *  返回 {kind, item, isNew} 或 null（与媒体无关）。 */
  SniffStore.prototype.add = function (entry) {
    var kind = classifyMedia(entry.url, entry.mime);
    if (!kind) return null;
    var host = '';
    try { host = new URL(entry.url).host; } catch (e) { /* 保空 */ }

    if (kind === 'segment') {
      var ph = this._segments[host];
      if (!ph) {
        ph = this._segments[host] = {
          url: entry.url, mime: entry.mime || '', kind: 'segment', host: host,
          referer: entry.referer || '', pageUrl: entry.pageUrl || '',
          pageTitle: entry.pageTitle || '', firstSeen: entry.ts || 0, count: 0
        };
      }
      ph.count += 1;
      return { kind: kind, item: ph, isNew: ph.count === 1 };
    }

    var it = this._index[entry.url];
    if (it) {
      it.count += 1;
      // 首次响应可能没有 Content-Length（分块/流式）→ 后续响应补上，已有值不覆盖
      if (!it.size && entry.contentLength) it.size = entry.contentLength;
      return { kind: kind, item: it, isNew: false };
    }
    it = {
      url: entry.url, mime: entry.mime || '', kind: kind, host: host,
      referer: entry.referer || '', pageUrl: entry.pageUrl || '',
      pageTitle: entry.pageTitle || '', cookie: entry.cookie || '',
      firstSeen: entry.ts || 0, count: 1,
      size: entry.contentLength || 0
    };
    this._index[entry.url] = it;
    this.items.push(it);
    if (kind === 'playlist' && this._segments[host]) {
      delete this._segments[host];   // 清单到手 → 分片占位被取代
    }
    while (this.items.length > this.cap) {
      var old = this.items.shift();
      delete this._index[old.url];
    }
    return { kind: kind, item: it, isNew: true };
  };

  /** 可下载项（playlist + media），最新在前。 */
  SniffStore.prototype.list = function () {
    return this.items.slice().reverse();
  };

  /** 分片占位（按出现次序）。 */
  SniffStore.prototype.segments = function () {
    var keys = Object.keys(this._segments);
    var out = [];
    for (var i = 0; i < keys.length; i++) out.push(this._segments[keys[i]]);
    return out;
  };

  SniffStore.prototype.badgeCount = function () { return this.items.length; };

  SniffStore.prototype.clear = function () {
    this.items = [];
    this._index = {};
    this._segments = {};
  };

  SniffStore.prototype.toJSON = function () {
    return { items: this.items, segments: this._segments };
  };

  SniffStore.prototype.loadFrom = function (data) {
    this.clear();
    if (!data) return;
    var items = Array.isArray(data.items) ? data.items : [];
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      if (!it || !it.url) continue;
      this.items.push(it);
      this._index[it.url] = it;
    }
    var segs = data.segments || {};
    for (var k in segs) {
      if (Object.prototype.hasOwnProperty.call(segs, k)) this._segments[k] = segs[k];
    }
  };

  /**
   * 多标签页容器：每个 tabId 一个 SniffStore。
   *
   * 2026-09-29 用户反馈「插件保存的信息太多了，保存当前页的就行」——原实现是
   * **全局单库**，逛过支付宝商家平台再看百度，面板里两个站点的条目堆在一起，
   * 还带着早已离开的页面的标题，用户根本分不清哪条属于当前页。
   * 现在改为「按标签页分库 + 主文档导航即清」，面板只呈现当前页的嗅探结果。
   *
   * 与 SniffStore 一样零 chrome.* 依赖（tabId 只是外部传入的整型），
   * 便于 node 离线测试；chrome 侧的「导航/关页/切页」接线在 background.js。
   */
  function TabStores(cap, perTabCap) {
    this.cap = cap || 30;              // 最多同时保留多少个标签页的分库
    this.perTabCap = perTabCap || 200; // 单个标签页的可下载项上限
    this.tabs = {};                    // 'tabId' -> SniffStore（键保持插入序，便于淘汰）
  }

  TabStores.prototype.store = function (tabId) {
    var k = String(tabId);
    var s = this.tabs[k];
    if (s) return s;
    s = this.tabs[k] = new SniffStore(this.perTabCap);
    var keys = Object.keys(this.tabs);
    // 超限淘汰「最早建立分库」的标签页，绝不淘汰刚进来的这个（它才是用户当前在看/刚在用的）
    while (keys.length > this.cap) {
      var victim = keys.shift();
      if (victim === k) { keys.push(victim); continue; }  // 理论到不了（新键必在末尾）
      delete this.tabs[victim];
    }
    return s;
  };

  TabStores.prototype.add = function (tabId, entry) {
    return this.store(tabId).add(entry);
  };

  TabStores.prototype.list = function (tabId) {
    var s = this.tabs[String(tabId)];
    return s ? s.list() : [];
  };

  TabStores.prototype.segments = function (tabId) {
    var s = this.tabs[String(tabId)];
    return s ? s.segments() : [];
  };

  TabStores.prototype.count = function (tabId) {
    var s = this.tabs[String(tabId)];
    return s ? s.badgeCount() : 0;
  };

  TabStores.prototype.totalCount = function () {
    var n = 0;
    for (var k in this.tabs) {
      if (Object.prototype.hasOwnProperty.call(this.tabs, k)) n += this.tabs[k].badgeCount();
    }
    return n;
  };

  /** 只清一个标签页（页面跳转 / 用户点「清空」时用）。 */
  TabStores.prototype.clearTab = function (tabId) {
    var s = this.tabs[String(tabId)];
    if (s) s.clear();
  };

  /** 丢掉整个标签页的分库（标签页关闭时用）。 */
  TabStores.prototype.dropTab = function (tabId) {
    delete this.tabs[String(tabId)];
  };

  /** 丢掉已不存在的标签页的分库（SW 重启恢复后清理残留）。
   *  ⚠️ 只删不存在的，**绝不顺手清现存标签页**——SW 空闲被杀是常态，
   *  清掉会把用户当前页刚嗅到的条目也一起抹了。 */
  TabStores.prototype.dropTabsExcept = function (keepIds) {
    var keep = {};
    (keepIds || []).forEach(function (id) { keep[String(id)] = 1; });
    for (var k in this.tabs) {
      if (Object.prototype.hasOwnProperty.call(this.tabs, k) && !keep[k]) {
        delete this.tabs[k];
      }
    }
  };

  TabStores.prototype.clearAll = function () {
    for (var k in this.tabs) {
      if (Object.prototype.hasOwnProperty.call(this.tabs, k)) this.tabs[k].clear();
    }
  };

  TabStores.prototype.toJSON = function () {
    var out = {};
    for (var k in this.tabs) {
      if (Object.prototype.hasOwnProperty.call(this.tabs, k)) out[k] = this.tabs[k].toJSON();
    }
    return { tabs: out };
  };

  TabStores.prototype.loadFrom = function (data) {
    this.tabs = {};
    if (!data) return;
    var tabs = (data && data.tabs) || {};
    var keys = Object.keys(tabs);
    for (var i = 0; i < keys.length; i++) {
      var s = new SniffStore(this.perTabCap);
      s.loadFrom(tabs[keys[i]]);
      this.tabs[keys[i]] = s;
    }
  };

  /** 换页判定基准：`origin + pathname + search`（**故意丢掉 hash**）。
   *  页内锚点跳转 / 前端路由只改 hash 时不应算「换页」，否则会把当前页刚嗅到的
   *  条目误清。# 之外的变化（含 query）都算换页 —— 见 background.js 的用法。 */
  function pageKeyOf(url) {
    try {
      var p = new URL(url);
      return p.origin + p.pathname + p.search;
    } catch (e) { return url || ''; }
  }

  // 桌面端端口探测表：desktop_launcher._find_free_port 从 8321 顺延。
  var PROBE_PORT_START = 8321;
  var PROBE_PORT_COUNT = 30;
  function probeBases() {
    var out = [];
    for (var i = 0; i < PROBE_PORT_COUNT; i++) {
      out.push('http://127.0.0.1:' + (PROBE_PORT_START + i));
    }
    return out;
  }

  root.VDLSniffCore = {
    classifyMedia: classifyMedia,
    pathSuffix: pathSuffix,
    isNoiseUrl: isNoiseUrl,
    isVideoPage: isVideoPage,
    pickHeaders: pickHeaders,
    SniffStore: SniffStore,
    TabStores: TabStores,
    pageKeyOf: pageKeyOf,
    probeBases: probeBases
  };
})(typeof self !== 'undefined' ? self : globalThis);
