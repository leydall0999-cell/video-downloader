/* 视频工坊 媒体嗅探 —— popup 逻辑。
 *
 * 发送通道复用桌面端 /api/sniffer/send（add_manual → picked 队列），
 * 桌面端 web/js/desktop-app.js 的 picked 轮询会自动建下载任务 —— 与
 * CDP 悬浮球的兜底回传同一条路，不新增下载端点。
 *
 * 回执通道（2026-09-27 用户实测补充）：
 *   /api/sniffer/send 只是**入队**，必然返回 200；真正建任务发生在桌面端进程里。
 *   桌面端未登录 / 链接不支持 / 超配额时，建任务会失败，但失败发生在桌面端——
 *   扩展原本无从得知，于是按钮显示「已发送 ✓」而用户什么也没等到（用户原话
 *   「扩展点击下载 app 没反应」）。现在 send 返回 send_id，扩展轮询
 *   /api/sniffer/result 拿到真实结果，失败时把原因直接显示在条目上。
 */
'use strict';

var $ = function (id) { return document.getElementById(id); };
var listEl = $('list');

var state = { endpoint: '', sentUrls: {}, carryCookie: true };
// 桌面端登录态：true=已登录 / false=未登录 / null=未知（App 未启动或界面已关闭）
var desktopLoggedIn = null;

function fmtTime(ts) {
  if (!ts) return '';
  var d = new Date(ts * 1000);
  var p = function (n) { return (n < 10 ? '0' : '') + n; };
  return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}

function chipText(kind) {
  if (kind === 'page') return '页面';
  return kind === 'playlist' ? '清单' : (kind === 'segment' ? '分片' : '直链');
}

/** 条目下方的结果行（原来的失败是「静默无反应」，现在必须看得见）。 */
function setItemMsg(btn, text, isErr) {
  var item = btn && btn.closest ? btn.closest('.item') : null;
  var el = item ? item.querySelector('.item-msg') : null;
  if (!el) return;
  el.textContent = text || '';
  el.className = 'item-msg' + (isErr ? ' is-err' : '');
  el.hidden = !text;
}

/** 发送失败：按钮回到可重试状态，原因同时进 title 与条目文字。 */
function failSend(btn, msg) {
  btn.disabled = false;
  btn.textContent = '重试';
  btn.title = msg;
  setItemMsg(btn, msg, true);
}

function renderItem(it) {
  var div = document.createElement('div');
  div.className = 'item' + (state.sentUrls[it.url] ? ' sent' : '');

  var top = document.createElement('div');
  top.className = 'item-top';
  var chip = document.createElement('span');
  chip.className = 'chip ' + (it.kind || 'media');
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
  // 用户实测反馈（2026-09-27）：裸写 `00:36:14` 会被读成「时长」，以为 UI 音效有 36 分钟。
  // 它其实是「第一次嗅到这个地址的时刻」（本地时钟 时:分:秒），加前缀消歧。
  if (it.firstSeen) meta.push('嗅于 ' + fmtTime(it.firstSeen));
  if (it.referer) { try { meta.push(new URL(it.referer).host); } catch (e) { /* 忽略 */ } }
  if (meta.length) {
    var m = document.createElement('div');
    m.className = 'item-meta';
    m.textContent = meta.join(' · ');
    div.appendChild(m);
  }
  var msg = document.createElement('div');
  msg.className = 'item-msg';
  msg.hidden = true;
  div.appendChild(msg);
  return div;
}

function render(st) {
  state = st;
  var epEl = $('endpointText');
  if (st.endpoint) {
    epEl.textContent = '已连接桌面端';
    epEl.className = 'ep-ok';
  } else {
    epEl.textContent = '未找到桌面端（请确认 App 已启动）';
    epEl.className = 'ep-none';
  }
  $('sniffToggle').checked = st.enabled;
  $('carryCookie').checked = st.carryCookie !== false;

  // 桌面端未登录时必须提前说清楚：否则用户点下载只会看到桌面端弹登录框
  //（浏览器弹窗焦点在前台，桌面端窗口在后台，用户以为「没反应」）。
  var warn = $('loginWarn');
  if (desktopLoggedIn === false) {
    warn.textContent = '桌面端未登录 —— 请先在「视频工坊」里登录，扩展发来的下载才会开始';
    warn.hidden = false;
  } else {
    warn.hidden = true;
  }

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

function fetchResult(base, sid) {
  return fetch(base + '/api/sniffer/result?send_id=' + encodeURIComponent(sid))
    .then(function (r) { return r.json(); });
}

/** 轮询回执直到有结论：ok → 已加入下载；error → 显示桌面端给的原因。 */
function pollResult(it, btn, sid, okLabel, left) {
  fetchResult(state.endpoint, sid).then(function (j) {
    var st = (j && j.state) || 'unknown';
    if (st === 'ok') {
      chrome.runtime.sendMessage({ type: 'markSent', url: it.url }, function () {});
      btn.disabled = false;
      btn.textContent = '已加入下载 ✓';
      btn.title = '';
      setItemMsg(btn, '', false);
      refresh();
      return;
    }
    if (st === 'error') {
      failSend(btn, (j && j.message) || '桌面端建任务失败');
      return;
    }
    if (left > 0) {
      setTimeout(function () { pollResult(it, btn, sid, okLabel, left - 1); }, 900);
      return;
    }
    // 超时（桌面端界面没开 / 卡住）：不谎报成功，如实说「已投递但未确认」
    chrome.runtime.sendMessage({ type: 'markSent', url: it.url }, function () {});
    btn.disabled = false;
    btn.textContent = okLabel;
    setItemMsg(btn, '已投递，但未收到桌面端确认（请到桌面端任务列表查看）', false);
  }).catch(function () {
    btn.disabled = false;
    btn.textContent = okLabel;
  });
}

function sendItem(it, btn, okLabel) {
  okLabel = okLabel || '已发送 ✓';
  if (!state.endpoint) {
    chrome.runtime.sendMessage({ type: 'getEndpoint' }, function (r) {
      if (r && r.endpoint) { state.endpoint = r.endpoint; sendItem(it, btn, okLabel); }
      else { failSend(btn, '未找到桌面端：请先启动「视频工坊」App'); }
    });
    return;
  }
  if (desktopLoggedIn === false) {
    // 可能是「刚在桌面端登录完」的旧信号：再核一次，确认仍未登录才拒绝，
    // 免得用户明明登录了却点不动（桌面端每 3s 轮询会刷新这个信号）
    refreshDesktopAuth(function () {
      if (desktopLoggedIn === false) {
        failSend(btn, '桌面端未登录：请先在「视频工坊」里登录，再点下载');
      } else {
        sendItem(it, btn, okLabel);
      }
    });
    return;
  }
  btn.disabled = true;
  btn.textContent = '发送中…';
  postSend(state.endpoint, it, state.carryCookie !== false)
    .then(function (resp) {
      var sid = resp && resp.send_id;
      if (!sid) {
        chrome.runtime.sendMessage({ type: 'markSent', url: it.url }, function () {});
        btn.disabled = false;
        btn.textContent = okLabel;
        return;
      }
      pollResult(it, btn, sid, okLabel, 12);
    })
    .catch(function (e) {
      failSend(btn, '发送失败：' + ((e && e.message) || '桌面端未响应'));
    });
}

/** 发送当前标签页的页面地址（2026-09-27）。
 *  作用：YouTube 等站点用 MSE 播放，扩展嗅不到可下载的媒体直链（能嗅到的往往
 *  只是站点自己的 UI 音效），把页面地址交给桌面端走 yt-dlp 解析才是正解。 */
function sendCurrentPage(btn) {
  chrome.tabs.query({ active: true, currentWindow: true }, function (tabs) {
    if (chrome.runtime.lastError) return;
    var tab = tabs && tabs[0];
    var url = (tab && tab.url) || '';
    if (!/^https?:/i.test(url)) {
      failSend(btn, '当前标签页不是网页地址，无法发送');
      return;
    }
    var it = {
      url: url, mime: '', kind: 'page', referer: url,
      pageUrl: url, pageTitle: (tab && tab.title) || '', cookie: ''
    };
    // 页面项不进嗅探列表，结果写在工具栏下方的提示行里
    sendItemWithNote(it, btn, '页面已发送 ✓');
  });
}

/** 与 sendItem 同流程，但结果写到 #sendNote（没有对应条目 DOM 可写）。 */
function sendItemWithNote(it, btn, okLabel) {
  if (!state.endpoint) {
    chrome.runtime.sendMessage({ type: 'getEndpoint' }, function (r) {
      if (r && r.endpoint) { state.endpoint = r.endpoint; sendItemWithNote(it, btn, okLabel); }
      else { note(btn, '未找到桌面端：请先启动「视频工坊」App', true); }
    });
    return;
  }
  if (desktopLoggedIn === false) {
    // 同 sendItem：先核实一次再拒绝，避免旧信号误伤刚登录的用户
    refreshDesktopAuth(function () {
      if (desktopLoggedIn === false) {
        note(btn, '桌面端未登录：请先在「视频工坊」里登录，再发送', true);
      } else {
        sendItemWithNote(it, btn, okLabel);
      }
    });
    return;
  }
  btn.disabled = true;
  btn.textContent = '发送中…';
  note(btn, '正在把页面地址交给桌面端解析…', false);
  postSend(state.endpoint, it, state.carryCookie !== false)
    .then(function (resp) {
      var sid = resp && resp.send_id;
      if (!sid) { donePage(btn, okLabel, '已投递（未返回回执）'); return; }
      pollPageResult(btn, sid, okLabel, 20);
    })
    .catch(function (e) {
      note(btn, '发送失败：' + ((e && e.message) || '桌面端未响应'), true);
      btn.disabled = false;
      btn.textContent = '发送当前页';
    });
}

function pollPageResult(btn, sid, okLabel, left) {
  fetchResult(state.endpoint, sid).then(function (j) {
    var st = (j && j.state) || 'unknown';
    if (st === 'ok') { donePage(btn, okLabel, '桌面端已开始解析并加入下载队列'); return; }
    if (st === 'error') {
      note(btn, '失败：' + ((j && j.message) || '桌面端建任务失败'), true);
      btn.disabled = false;
      btn.textContent = '发送当前页';
      return;
    }
    if (left > 0) { setTimeout(function () { pollPageResult(btn, sid, okLabel, left - 1); }, 900); return; }
    donePage(btn, okLabel, '已投递，但未收到桌面端确认（请到桌面端任务列表查看）');
  }).catch(function () {
    donePage(btn, okLabel, '已投递（未收到回执）');
  });
}

function donePage(btn, okLabel, text) {
  btn.disabled = false;
  btn.textContent = okLabel;
  note(btn, text, false);
}

/** 结果提示行（工具栏下方）。第一个参数保留为触发按钮，便于将来按来源分流。 */
function note(_btn, text, isErr) {
  var el = $('sendNote');
  el.textContent = text || '';
  el.className = 'send-note' + (isErr ? ' is-err' : '');
  el.hidden = !text;
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
$('sendPage').addEventListener('click', function (e) {
  sendCurrentPage(e.currentTarget);
});
$('reDetect').addEventListener('click', function () {
  var ep = $('endpointText');
  ep.textContent = '探测中…';
  ep.className = 'ep-unknown';
  chrome.runtime.sendMessage({ type: 'detectEndpoint' }, function (r) {
    if (chrome.runtime.lastError) return;
    state.endpoint = (r && r.endpoint) || '';
    refresh();
    refreshDesktopAuth();
  });
});
$('sendAll').addEventListener('click', function () {
  var btn = $('sendAll');
  var targets = (state.items || []).filter(function (it) { return !state.sentUrls[it.url]; });
  if (!targets.length) return;
  if (!state.endpoint) { $('reDetect').click(); return; }
  // 这里不做「未登录就不发」的前置拦截：批量发送靠逐条回执给结论（见 pollBatchResults），
  // 免得桌面端刚登录、扩展还握着旧信号时把用户挡在门外；顶部红条已提前提醒。
  btn.disabled = true;
  btn.textContent = '发送中…';
  var sids = [];
  var done = 0;
  var step = function () {
    if (done >= targets.length) {
      btn.disabled = false;
      btn.textContent = '全部发送';
      if (sids.length) pollBatchResults(sids, 10);
      else refresh();
      return;
    }
    var it = targets[done++];
    postSend(state.endpoint, it, state.carryCookie !== false)
      .then(function (resp) {
        if (resp && resp.send_id) sids.push(resp.send_id);
        chrome.runtime.sendMessage({ type: 'markSent', url: it.url }, function () { step(); });
      })
      .catch(function () { step(); });
  };
  step();
});

/** 批量回执：每轮并发查一次，未出结论的下一轮再查，全部有结论或超时后给一行汇总
 *  （绝不谎报成功）。⚠️ 下一轮必须等本轮 fetch 全部回填后再排——否则 pending 还是空的。 */
function pollBatchResults(sids, left) {
  if (!sids.length) return;
  var pending = [];
  var failed = [];
  var doneCount = 0;
  var settle = function () {
    doneCount++;
    if (doneCount < sids.length) return;
    if (failed.length) {
      note($('sendAll'), failed.length + ' 条未成功，例如：' + failed[0], true);
    } else if (pending.length) {
      if (left > 0) {
        setTimeout(function () { pollBatchResults(pending, left - 1); }, 1200);
        return;
      }
      note($('sendAll'),
        '有 ' + pending.length + ' 条已投递但未收到桌面端确认（请到桌面端任务列表查看）', false);
    } else {
      note($('sendAll'), '', false);
    }
    refresh();
  };
  sids.forEach(function (sid) {
    fetchResult(state.endpoint, sid).then(function (j) {
      var st = (j && j.state) || 'unknown';
      if (st === 'error') failed.push((j && j.message) || '未知原因');
      else if (st !== 'ok') pending.push(sid);
      settle();
    }).catch(function () { pending.push(sid); settle(); });
  });
}

/** 取桌面端登录态（借 /api/sniffer/status 的 desktop_logged_in 字段）。cb 用于「先核实再拒绝」。 */
function refreshDesktopAuth(cb) {
  cb = cb || function () {};
  if (!state.endpoint) { desktopLoggedIn = null; refresh(); cb(); return; }
  fetch(state.endpoint + '/api/sniffer/status')
    .then(function (r) { return r.json(); })
    .then(function (j) {
      desktopLoggedIn = (j && typeof j.desktop_logged_in === 'boolean')
        ? j.desktop_logged_in : null;
      refresh();
      cb();
    })
    .catch(function () { desktopLoggedIn = null; refresh(); cb(); });
}

// 初始化：先取背景状态，再取桌面端登录态
chrome.runtime.sendMessage({ type: 'getEndpoint' }, function (r) {
  if (chrome.runtime.lastError) return;
  if (r && r.endpoint) state.endpoint = r.endpoint;
  refresh();
  refreshDesktopAuth();
});
