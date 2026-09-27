/* VideoDownloader 媒体嗅探 —— popup 逻辑。
 * 发送通道复用桌面端 /api/sniffer/send（add_manual → picked 队列），
 * 桌面端 web/js/desktop-app.js 的 picked 轮询会自动建下载任务 —— 与
 * CDP 悬浮球的兜底回传同一条路，不新增端点。
 */
'use strict';

var $ = function (id) { return document.getElementById(id); };
var listEl = $('list');

var state = { endpoint: '', sentUrls: {}, carryCookie: true };

function fmtTime(ts) {
  if (!ts) return '';
  var d = new Date(ts * 1000);
  var p = function (n) { return (n < 10 ? '0' : '') + n; };
  return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}

function fmtSize(n) {
  if (!n || n <= 0) return '';
  var units = ['B', 'KB', 'MB', 'GB'];
  var i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return n.toFixed(n >= 100 || i === 0 ? 0 : 1) + ' ' + units[i];
}

function chipText(kind) {
  return kind === 'playlist' ? '清单' : (kind === 'segment' ? '分片' : '直链');
}

function renderItem(it) {
  var div = document.createElement('div');
  div.className = 'item' + (state.sentUrls[it.url] ? ' sent' : '');

  var top = document.createElement('div');
  top.className = 'item-top';
  var chip = document.createElement('span');
  chip.className = 'chip ' + it.kind;
  chip.textContent = chipText(it.kind);
  var url = document.createElement('span');
  url.className = 'item-url';
  url.textContent = it.url;
  url.title = it.url;
  top.appendChild(chip);
  top.appendChild(url);

  if (it.kind !== 'segment') {
    var actions = document.createElement('span');
    actions.className = 'item-actions';
    var dl = document.createElement('button');
    dl.className = 'mini primary';
    dl.textContent = state.sentUrls[it.url] ? '已发送 ✓' : '下载';
    dl.addEventListener('click', function () { sendItem(it, dl); });
    var cp = document.createElement('button');
    cp.className = 'mini';
    cp.textContent = '复制';
    cp.addEventListener('click', function () {
      navigator.clipboard.writeText(it.url).then(function () {
        cp.textContent = '已复制';
        setTimeout(function () { cp.textContent = '复制'; }, 1200);
      });
    });
    actions.appendChild(dl);
    actions.appendChild(cp);
    top.appendChild(actions);
  }
  div.appendChild(top);

  if (it.pageTitle) {
    var t = document.createElement('div');
    t.className = 'item-title';
    t.textContent = it.pageTitle;
    div.appendChild(t);
  }
  var meta = [];
  if (it.count > 1) meta.push('×' + it.count);
  if (it.firstSeen) meta.push(fmtTime(it.firstSeen));
  if (it.referer) { try { meta.push(new URL(it.referer).host); } catch (e) { /* 忽略 */ } }
  if (meta.length) {
    var m = document.createElement('div');
    m.className = 'item-meta';
    m.textContent = meta.join(' · ');
    div.appendChild(m);
  }
  return div;
}

function render(st) {
  state = st;
  var epEl = $('endpointText');
  if (st.endpoint) {
    epEl.textContent = '已连接 ' + st.endpoint.replace('http://', '');
    epEl.className = 'ep-ok';
  } else {
    epEl.textContent = '未找到桌面端（请确认 App 已启动）';
    epEl.className = 'ep-none';
  }
  $('sniffToggle').checked = st.enabled;
  $('carryCookie').checked = st.carryCookie !== false;

  var items = st.items || [];
  var segs = st.segments || [];
  listEl.innerHTML = '';
  if (!items.length && !segs.length) {
    listEl.innerHTML = '<div class="empty">打开有视频的网页，这里会列出嗅探到的媒体流</div>';
  } else {
    items.forEach(function (it) { listEl.appendChild(renderItem(it)); });
  }

  var note = $('segNote');
  if (segs.length) {
    var total = 0;
    segs.forEach(function (s) { total += s.count || 0; });
    note.textContent = '另有 ' + segs.length + ' 路分片流（共 ' + total +
      ' 片）——正在播放的 HLS 流，清单出现后可整段下载';
    note.hidden = false;
  } else {
    note.hidden = true;
  }
}

function refresh() {
  chrome.runtime.sendMessage({ type: 'getState' }, function (st) {
    if (chrome.runtime.lastError) return;
    render(st);
  });
}

// ---- 发送到桌面端 ----
function postSend(base, it, carry) {
  var payload = {
    url: it.url,
    mime: it.mime || '',
    referer: it.referer || '',
    page_url: it.pageUrl || it.pageTitle || '',
    page_title: it.pageTitle || '',
    cookie: carry ? (it.cookie || '') : '',
    source: 'extension'
  };
  return fetch(base + '/api/sniffer/send', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  }).then(function (r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  });
}

function sendItem(it, btn) {
  if (!state.endpoint) {
    chrome.runtime.sendMessage({ type: 'getEndpoint' }, function (r) {
      if (r && r.endpoint) { state.endpoint = r.endpoint; sendItem(it, btn); }
      else { btn.textContent = '未连接'; }
    });
    return;
  }
  btn.disabled = true;
  btn.textContent = '发送中…';
  postSend(state.endpoint, it, state.carryCookie !== false)
    .then(function () {
      chrome.runtime.sendMessage({ type: 'markSent', url: it.url }, function () {
        btn.disabled = false;
        btn.textContent = '已发送 ✓';
      });
    })
    .catch(function (e) {
      btn.disabled = false;
      btn.textContent = '重试';
      btn.title = (e && e.message) || '发送失败';
    });
}

// ---- 事件绑定 ----
$('sniffToggle').addEventListener('change', function (e) {
  chrome.runtime.sendMessage({ type: 'setEnabled', value: e.target.checked }, refresh);
});
$('carryCookie').addEventListener('change', function (e) {
  chrome.runtime.sendMessage({ type: 'setCarryCookie', value: e.target.checked });
});
$('clearAll').addEventListener('click', function () {
  chrome.runtime.sendMessage({ type: 'clear' }, refresh);
});
$('reDetect').addEventListener('click', function () {
  var ep = $('endpointText');
  ep.textContent = '探测中…';
  ep.className = 'ep-unknown';
  chrome.runtime.sendMessage({ type: 'detectEndpoint' }, function (r) {
    if (chrome.runtime.lastError) return;
    state.endpoint = (r && r.endpoint) || '';
    refresh();
  });
});
$('sendAll').addEventListener('click', function () {
  var btn = $('sendAll');
  var targets = (state.items || []).filter(function (it) { return !state.sentUrls[it.url]; });
  if (!targets.length) return;
  if (!state.endpoint) { $('reDetect').click(); return; }
  btn.disabled = true;
  btn.textContent = '发送中…';
  var done = 0;
  var step = function () {
    if (done >= targets.length) {
      btn.disabled = false;
      btn.textContent = '全部发送';
      refresh();
      return;
    }
    var it = targets[done++];
    postSend(state.endpoint, it, state.carryCookie !== false)
      .then(function () {
        chrome.runtime.sendMessage({ type: 'markSent', url: it.url }, function () { step(); });
      })
      .catch(function () { step(); });
  };
  step();
});

// 初始化：端点未探测时先探测一轮
chrome.runtime.sendMessage({ type: 'getEndpoint' }, function (r) {
  if (chrome.runtime.lastError) return;
  refresh();
});
