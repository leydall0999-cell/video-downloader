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
 */
'use strict';

importScripts('sniff-core.js');

var CORE = globalThis.VDLSniffCore;
var STORE_CAP = 200;
var PERSIST_DEBOUNCE_MS = 500;

var store = new CORE.SniffStore(STORE_CAP);
var enabled = true;          // storage.local['enabled']，默认开
var carryCookie = true;      // storage.local['carryCookie']，发送时是否携带 Cookie
var endpoint = '';           // storage.local['endpoint']，探测到的桌面端基址
var sentUrls = {};           // storage.session['sentUrls']，已发送标记（popup 显示「已发送」）
var headerMeta = new Map();  // requestId -> {referer, cookie}
var persistTimer = null;

var LOCAL_HOST_RE = /^(127\.0\.0\.1|localhost|::1|\[::1\])$/;

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
        sentUrls: sentUrls
      });
    } catch (e) { /* 会话存储不可用时静默（内存态仍在） */ }
  }, PERSIST_DEBOUNCE_MS);
}

function updateBadge() {
  var n = store.badgeCount();
  try {
    chrome.action.setBadgeText({ text: n > 0 ? String(n) : '' });
    chrome.action.setBadgeBackgroundColor({ color: '#2563eb' });
  } catch (e) { /* 无 action 场景忽略 */ }
}

function handleCompleted(details) {
  var url = details.url || '';
  var initiator = details.initiator || details.originUrl || '';
  if (shouldSkip(url, initiator)) {
    headerMeta.delete(details.requestId);
    return;
  }
  var picked = CORE.pickHeaders(details.responseHeaders, null);
  var meta = headerMeta.get(details.requestId) || {};
  headerMeta.delete(details.requestId);

  // mime 权威原则与桌面端一致：响应头 text/html 等错报一律过滤
  var res = store.add({
    url: url,
    mime: picked.mime,
    referer: meta.referer || initiator || '',
    pageUrl: meta.referer || initiator || details.url || '',
    pageTitle: '',
    cookie: meta.cookie || '',
    ts: Date.now() / 1000
  });
  if (res && res.isNew) updateBadge();
  if (res && details.tabId && details.tabId > 0) {
    // 补页面标题（异步，失败不影响条目）
    try {
      chrome.tabs.get(details.tabId, function (tab) {
        if (chrome.runtime.lastError || !tab) return;
        res.item.pageTitle = tab.title || '';
        res.item.pageUrl = tab.url || res.item.pageUrl;
        persist();
      });
    } catch (e) { /* tab 已关闭等 */ }
  }
  persist();
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

// ---- 启动恢复 ----
(function restore() {
  chrome.storage.local.get(['enabled', 'carryCookie', 'endpoint'], function (st) {
    if (st.enabled === false) enabled = false;
    if (st.carryCookie === false) carryCookie = false;
    if (st.endpoint) endpoint = st.endpoint;
  });
  chrome.storage.session.get(['vdlSniff', 'sentUrls'], function (st) {
    if (st.vdlSniff) store.loadFrom(st.vdlSniff);
    if (st.sentUrls) sentUrls = st.sentUrls;
    updateBadge();
  });
})();

// ---- popup 消息通道 ----
chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
  msg = msg || {};
  switch (msg.type) {
    case 'getState':
      sendResponse({
        enabled: enabled,
        carryCookie: carryCookie,
        endpoint: endpoint,
        items: store.list(),
        segments: store.segments(),
        sentUrls: sentUrls
      });
      return false;
    case 'setEnabled':
      enabled = !!msg.value;
      chrome.storage.local.set({ enabled: enabled });
      if (!enabled) { store.clear(); persist(); updateBadge(); }
      sendResponse({ ok: true, enabled: enabled });
      return false;
    case 'setCarryCookie':
      carryCookie = !!msg.value;
      chrome.storage.local.set({ carryCookie: carryCookie });
      sendResponse({ ok: true, carryCookie: carryCookie });
      return false;
    case 'clear':
      store.clear();
      sentUrls = {};
      persist();
      updateBadge();
      sendResponse({ ok: true });
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
