/* 视频工坊 媒体嗅探 —— MV3 service worker。
 *
 * 职责：webRequest 观察监听（不改写请求）→ sniff-core 判定 → 去重存储 →
 * 徽标计数；popup 通过 runtime 消息取列表 / 发送到桌面端。
 *
 * 设计要点（2026-09-27）：
 * - Referer/Cookie 在 Chrome 72+ 属于隐藏头，必须带 extraHeaders 才能读到；
 *   嗅探直链的 host 是 CDN，按 host 生成 Referer 必 403（桌面端 CDP 嗅探同坑），
 *   所以把页面真实 Referer / Cookie 一并捕获、随 /api/sniffer/send 回传。
 * - 过滤三类自噪音：本机回环（桌面端自己的 /api/media/proxy 中继流）、
 *   chrome-extension://（扩展自身请求）、非 http(s)。
 * - 只观察不阻塞：不用 onBeforeRequest 的 blocking 模式，MV3 无需
 *   declarativeNetRequest 权限，也不影响页面加载。
 * - 持久化用 chrome.storage.session（浏览器会话内有效，SW 被杀可恢复）。
 * - **分页隔离（2026-09-29）**：原实现是全局单库，逛多个站点后条目堆在一起，
 *   用户反馈「保存的信息太多了，保存当前页的就行」。现在按 tabId 分库
 *   （core.TabStores），并把「主文档请求」当作换页信号——换页即清掉该标签页
 *   上一页的条目；面板只呈现当前标签页的结果。
 */
'use strict';

importScripts('sniff-core.js');

var CORE = globalThis.VDLSniffCore;
var STORE_CAP = 200;        // 单个标签页的可下载项上限
var TAB_CAP = 30;           // 最多同时保留多少个标签页的分库
var PERSIST_DEBOUNCE_MS = 500;

var store = new CORE.TabStores(TAB_CAP, STORE_CAP);
var tabPage = {};            // storage.session['tabPage']，tabId -> 主文档 URL（换页判定基准）
var activeTabId = -1;        // 最近一次激活的标签页（徽标计数用）
var enabled = true;          // storage.local['enabled']，默认开
var carryCookie = true;      // storage.local['carryCookie']，发送时是否携带 Cookie
var endpoint = '';           // storage.local['endpoint']，探测到的桌面端基址
var sentUrls = {};           // storage.session['sentUrls']，已发送标记（popup 显示「已发送」）
var headerMeta = new Map();  // requestId -> {referer, cookie}
var persistTimer = null;

var LOCAL_HOST_RE = /^(127\.0\.0\.1|localhost|::1|\[::1\])$/;

/** 换页判定基准（实现与测试都在 sniff-core.pageKeyOf）：忽略 hash ——
 *  页内锚点跳转不算换页，否则会把当前页刚嗅到的条目误清。 */
var pageKeyOf = CORE.pageKeyOf;

function isLocalUrl(u) {
  try { return LOCAL_HOST_RE.test(new URL(u).hostname); } catch (e) { return false; }
}

function shouldSkip(url, initiator) {
  if (!/^https?:/i.test(url)) return true;
  if (isLocalUrl(url)) return true;
  if (initiator && /^chrome-extension:/i.test(initiator)) return true;
  if (initiator && isLocalUrl(initiator)) return true;
  return false;
}

function rememberMeta(requestId, requestHeaders) {
  var picked = CORE.pickHeaders(null, requestHeaders);
  if (!picked.referer && !picked.cookie) return;
  headerMeta.set(requestId, picked);
  if (headerMeta.size > 1000) {
    // Map 保持插入序，弹最旧的 200 条防泄漏（正常请求都会走 onCompleted 清理）
    var drop = headerMeta.keys();
    for (var i = 0; i < 200; i++) {
      var k = drop.next();
      if (k.done) break;
      headerMeta.delete(k.value);
    }
  }
}

function persist() {
  if (persistTimer) return;
  persistTimer = setTimeout(function () {
    persistTimer = null;
    try {
      chrome.storage.session.set({
        vdlSniff: store.toJSON(),
        tabPage: tabPage,
        sentUrls: sentUrls
      });
    } catch (e) { /* 会话存储不可用时静默（内存态仍在） */ }
  }, PERSIST_DEBOUNCE_MS);
}

/** 徽标 = **当前标签页**的可下载项数（列表已按页隔离，徽标口径必须跟着走）。 */
function updateBadge() {
  chrome.tabs.query({ active: true, currentWindow: true }, function (tabs) {
    var tab = tabs && tabs[0];
    if (tab && typeof tab.id === 'number') activeTabId = tab.id;
    var n = store.count(activeTabId);
    try {
      chrome.action.setBadgeText({ text: n > 0 ? String(n) : '' });
      chrome.action.setBadgeBackgroundColor({ color: '#2563eb' });
    } catch (e) { /* 无 action 场景忽略 */ }
  });
}

function handleCompleted(details) {
  var url = details.url || '';
  var initiator = details.initiator || details.originUrl || '';
  if (shouldSkip(url, initiator)) {
    headerMeta.delete(details.requestId);
    return;
  }
  var tabId = (typeof details.tabId === 'number') ? details.tabId : -1;

  // 主文档请求 = 这个标签页换页了 → 丢掉上一页嗅到的条目（「只保存当前页」的闸门）。
  // 用 pageKeyOf 忽略 hash：页内锚点跳转不算换页。
  if (details.type === 'main_frame') {
    var pk = String(tabId);
    var prev = tabPage[pk] || '';
    if (prev && prev !== pageKeyOf(url)) {
      store.clearTab(tabId);
      updateBadge();
    }
    tabPage[pk] = pageKeyOf(url);
    persist();
  }

  var picked = CORE.pickHeaders(details.responseHeaders, null);
  var meta = headerMeta.get(details.requestId) || {};
  headerMeta.delete(details.requestId);

  // mime 权威原则与桌面端一致：响应头 text/html 等错报一律过滤
  var res = store.add(tabId, {
    url: url,
    mime: picked.mime,
    referer: meta.referer || initiator || '',
    pageUrl: meta.referer || initiator || details.url || '',
    pageTitle: '',
    cookie: meta.cookie || '',
    ts: Date.now() / 1000
  });
  if (res && res.isNew) updateBadge();
  if (res && tabId > 0) {
    // 补页面标题（异步，失败不影响条目）
    try {
      chrome.tabs.get(tabId, function (tab) {
        if (chrome.runtime.lastError || !tab) return;
        res.item.pageTitle = tab.title || '';
        res.item.pageUrl = tab.url || res.item.pageUrl;
        persist();
        pushToServer(res.item);   // 拿到标题再推，面板「来源」更完整
      });
    } catch (e) {
      pushToServer(res.item);
      /* tab 已关闭等 */
    }
  } else if (res) {
    pushToServer(res.item);
  }
  persist();
}

/** 自动嗅探直推（2026-10-01）：扩展每嗅到新条目就发给桌面端，进同一个
 *  items 库——没有这条链路，桌面面板列表永远是空的（用户以为没嗅到）。
 *  服务端按 URL 去重，重复推无副作用；失败静默（下次同 URL 再推）。 */
var pushTries = 0;            // 本次 SW 生命周期内推送尝试数（遥测：定位「没捕获」vs「推送失败」）
function pushToServer(item) {
  if (!item || !item.url) return;
  pushTries++;
  getEndpoint(function (base) {
    if (!base) return;
    try {
      fetch(base + '/api/sniffer/ext-push', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          items: [{
            url: item.url,
            mime: item.mime || '',
            kind_hint: item.kind || '',
            referer: item.referer || '',
            cookie: item.cookie || '',
            page_url: item.pageUrl || '',
            page_title: item.pageTitle || ''
          }]
        })
      }).catch(function () {});
    } catch (e) { /* 静默 */ }
  });
}

try {
  chrome.webRequest.onSendHeaders.addListener(
    function (details) {
      if (!enabled) return;
      if (shouldSkip(details.url || '', details.initiator || '')) return;
      rememberMeta(details.requestId, details.requestHeaders);
    },
    { urls: ['http://*/*', 'https://*/*'] },
    ['requestHeaders', 'extraHeaders']
  );

  chrome.webRequest.onCompleted.addListener(
    function (details) {
      if (!enabled) return;
      handleCompleted(details);
    },
    { urls: ['http://*/*', 'https://*/*'] },
    ['responseHeaders', 'extraHeaders']
  );

  chrome.webRequest.onErrorOccurred.addListener(
    function (details) { headerMeta.delete(details.requestId); },
    { urls: ['http://*/*', 'https://*/*'] }
  );
} catch (e) {
  console.error('webRequest 监听注册失败', e);
}

// ---- 标签页生命周期：关页丢库、切页刷徽标（均不需要额外权限） ----
try {
  if (chrome.tabs && chrome.tabs.onRemoved) {
    chrome.tabs.onRemoved.addListener(function (tabId) {
      store.dropTab(tabId);
      delete tabPage[String(tabId)];
      persist();
      updateBadge();
    });
  }
  if (chrome.tabs && chrome.tabs.onActivated) {
    chrome.tabs.onActivated.addListener(function (info) {
      activeTabId = info && typeof info.tabId === 'number' ? info.tabId : activeTabId;
      updateBadge();
    });
  }
} catch (e) {
  console.error('标签页监听注册失败', e);
}

// ---- 端点探测：desktop_launcher 从 8321 起顺延找空闲端口，这里扫同一张表 ----
function fetchAbort(url, ms) {
  var ctrl = new AbortController();
  var t = setTimeout(function () { ctrl.abort(); }, ms);
  return fetch(url, { signal: ctrl.signal }).finally(function () { clearTimeout(t); });
}

async function probeEndpoint(cb) {
  var bases = CORE.probeBases();
  for (var i = 0; i < bases.length; i++) {
    var base = bases[i];
    try {
      var r = await fetchAbort(base + '/api/sniffer/status', 900);
      if (r.ok) {
        try {
          var j = await r.json();
          if (j && typeof j.state === 'string') { cb(base); return; }
        } catch (e) { /* JSON 不合预期 → 落到公开端点验证 */ }
      }
      // status 不合预期（如 VDL_API_TOKEN 开启后 401）→ 验证免 token 的 /api/nodes
      try {
        var r2 = await fetchAbort(base + '/api/nodes', 900);
        if (r2.ok) { cb(base); return; }
      } catch (e) { /* 下一个端口 */ }
    } catch (e) { /* 端口不通，下一个 */ }
  }
  cb('');
}

function getEndpoint(cb) {
  if (endpoint) { cb(endpoint); return; }
  chrome.storage.local.get(['endpoint'], function (st) {
    if (st.endpoint) { endpoint = st.endpoint; cb(endpoint); return; }
    probeEndpoint(function (base) {
      if (base) {
        endpoint = base;
        chrome.storage.local.set({ endpoint: base });
      }
      cb(base);
    });
  });
}

// ---- 心跳（2026-10-01）：桌面端面板显示「扩展已连接 ✓」的依据 ----
// MV3 SW 空闲 ~30s 被 Chrome 挂起；每次唤醒（webRequest 事件/消息/alarms）
// 都会重跑顶层代码 → 顺手 ping 一次。alarms 每分钟兜底（浏览器挂着不动也在线）。
function heartbeat() {
  getEndpoint(function (base) {
    if (!base) return;
    try {
      // 1.0.37 起心跳带自报版本：桌面端与包内扩展比对 → 旧版提示一键更新。
      // 1.0.38 起再带捕获/推送遥测：captured=本地库条数、pushed=推送尝试数
      // ——用户「嗅探不到」时，一眼分清断在捕获层还是推送层。
      var ver = '', captured = 0;
      try { ver = (chrome.runtime.getManifest() || {}).version || ''; } catch (e) {}
      try { captured = (typeof store.totalCount === 'function') ? store.totalCount() : 0; } catch (e) {}
      fetch(base + '/api/sniffer/ext-ping', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ version: ver, captured: captured, pushed: pushTries }),
      }).catch(function () {});
    } catch (e) {}
  });
}
heartbeat();
try {
  chrome.alarms.create('vdlHeartbeat', { periodInMinutes: 1 });
  chrome.alarms.onAlarm.addListener(function (a) {
    if (a && a.name === 'vdlHeartbeat') heartbeat();
  });
} catch (e) { console.error('alarms 注册失败', e); }

// ---- 启动恢复 ----
(function restore() {
  chrome.storage.local.get(['enabled', 'carryCookie', 'endpoint'], function (st) {
    if (st.enabled === false) enabled = false;
    if (st.carryCookie === false) carryCookie = false;
    if (st.endpoint) endpoint = st.endpoint;
  });
  chrome.storage.session.get(['vdlSniff', 'tabPage', 'sentUrls'], function (st) {
    if (st.vdlSniff) store.loadFrom(st.vdlSniff);
    if (st.tabPage) tabPage = st.tabPage;
    if (st.sentUrls) sentUrls = st.sentUrls;
    // 已关闭标签页的残留分库清掉（只删不存在的，不动现存标签页的数据）
    try {
      chrome.tabs.query({}, function (tabs) {
        if (chrome.runtime.lastError) return;
        var ids = (tabs || []).map(function (t) { return t.id; });
        store.dropTabsExcept(ids);
        updateBadge();
        persist();
      });
    } catch (e) {
      updateBadge();
    }
  });
})();

// ---- popup 消息通道 ----
chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
  msg = msg || {};
  // popup 传它自己的标签页 id；没传（旧版 popup）则回落到最近激活的标签页
  function scopeTabId() {
    return (typeof msg.tabId === 'number' && msg.tabId >= 0) ? msg.tabId : activeTabId;
  }
  switch (msg.type) {
    case 'getState':
      var tid = scopeTabId();
      sendResponse({
        enabled: enabled,
        carryCookie: carryCookie,
        endpoint: endpoint,
        tabId: tid,
        items: store.list(tid),
        segments: store.segments(tid),
        sentUrls: sentUrls
      });
      return false;
    case 'setEnabled':
      enabled = !!msg.value;
      chrome.storage.local.set({ enabled: enabled });
      if (!enabled) {
        store.clearAll();
        tabPage = {};
        persist();
        updateBadge();
      }
      sendResponse({ ok: true, enabled: enabled });
      return false;
    case 'setCarryCookie':
      carryCookie = !!msg.value;
      chrome.storage.local.set({ carryCookie: carryCookie });
      sendResponse({ ok: true, carryCookie: carryCookie });
      return false;
    case 'clear':
      // 「清空」= 清当前页（面板里能看到的就是这一页的）；
      // 已发送标记只跟着这一页的条目走，别把别页的标记一起抹了
      var ctid = scopeTabId();
      store.list(ctid).forEach(function (it) { delete sentUrls[it.url]; });
      store.clearTab(ctid);
      persist();
      updateBadge();
      sendResponse({ ok: true, tabId: ctid });
      return false;
    case 'markSent':
      sentUrls[msg.url] = Date.now();
      persist();
      sendResponse({ ok: true });
      return false;
    case 'getEndpoint':
      getEndpoint(function (base) { sendResponse({ endpoint: base }); });
      return true;   // 异步
    case 'detectEndpoint':
      endpoint = '';
      chrome.storage.local.remove('endpoint');
      probeEndpoint(function (base) {
        if (base) {
          endpoint = base;
          chrome.storage.local.set({ endpoint: base });
        }
        sendResponse({ endpoint: base });
      });
      return true;   // 异步
  }
  return false;
});
