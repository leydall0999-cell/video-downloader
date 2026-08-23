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
      if (api && typeof api.open_external === 'function') {
        try {
          const r = api.open_external(url);
          return (typeof r === 'string') ? !r.startsWith('ERROR') : true;
        } catch (e) {
          return false; // 桥接异常：告知调用方回退
        }
      }
      return false; // 无原生桥接
    },
  };
  window.VDL.desktop = desktop;

  // 「同步 Cookie 到云端」：仅 App 端。把本机浏览器各强反爬站(抖音/B站/快手/小红书等)
  // 的登录态自动推送到网页版(Railway)公共池，让网页版访客无需手动粘贴即可复用。
  // 桌面版常驻时每 30 分钟自动刷新一次，使网页版共享登录态保持新鲜。
  (function initSyncButton() {
    const btn = document.createElement('button');
    btn.id = 'syncCookieBtn';
    btn.textContent = '同步 Cookie 到云端';
    btn.style.cssText = 'margin-left:8px;padding:4px 10px;cursor:pointer;';
    const badge = document.createElement('span');
    badge.id = 'syncCookieBadge';
    badge.style.cssText = 'margin-left:8px;font-size:12px;color:#888;';

    async function syncToCloud(showAlert) {
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

    btn.addEventListener('click', () => {
      const ok = window.confirm(
        '将把本机浏览器已登录的抖音/B站/快手/小红书等强反爬站登录态\n'
        + '推送到网页版(Railway)公共池，供网页版访客共享使用（不涉及密码，仅登录态 Cookie）。\n'
        + '桌面版会每 30 分钟自动刷新一次。是否立即同步？'
      );
      if (ok) syncToCloud(true);
    });

    const header = document.querySelector('header');
    if (header) {
      header.appendChild(btn);
      header.appendChild(badge);
    } else {
      document.body.appendChild(btn);
      document.body.appendChild(badge);
    }

    // 桌面版就绪后自动同步一次，并每 30 分钟保活
    const wire = () => { syncToCloud(false); };
    if (window.pywebview && window.pywebview.api) {
      wire();
    } else {
      document.addEventListener('pywebviewready', wire, { once: true });
    }
    setInterval(() => syncToCloud(false), 30 * 60 * 1000);
  })();

  // 「优酷零门槛解析」：桌面端用本机浏览器引擎自动拦截 ups，粘贴链接即出结果。
  // 仅桌面端（pywebview）显示卡片；纯 web 环境卡片保持 hidden。
  (function initYoukuLocal() {
    const card = document.getElementById('youkuLocalCard');
    if (!card) return;
    card.hidden = false;

    const statusEl = document.getElementById('youkuLocalStatus');
    const loginBtn = document.getElementById('youkuLoginBtn');
    const openBtn = document.getElementById('youkuOpenPage');
    const YK_HOME = 'https://www.youku.com';

    async function refreshStatus() {
      try {
        const r = await window.VDL.request('/api/youku/engine-status');
        const d = r || {};
        if (d.playwright && d.profile_logged_in) {
          statusEl.textContent = '● 已就绪：粘贴优酷链接即可解析';
          statusEl.style.color = '#2e8b57';
        } else if (d.playwright && !d.profile_logged_in) {
          statusEl.textContent = '○ 需登录：点「登录优酷」完成一次授权';
          statusEl.style.color = '#e6a23c';
          loginBtn.disabled = false;
        } else {
          statusEl.textContent = '○ 浏览器引擎不可用（缺 playwright）';
          statusEl.style.color = '#888';
          loginBtn.disabled = true;
        }
      } catch (e) {
        statusEl.textContent = '状态获取失败';
        statusEl.style.color = '#e64340';
      }
    }

    loginBtn.addEventListener('click', async () => {
      loginBtn.disabled = true;
      loginBtn.textContent = '登录中…（弹窗里登录后关闭）';
      statusEl.textContent = '● 请在弹出的浏览器窗口登录优酷…';
      statusEl.style.color = '#e6a23c';
      try {
        const r = await window.VDL.request('/api/youku/login', { method: 'POST' });
        const d = r || {};
        if (d.ok) {
          window.alert('✅ 优酷登录成功！现在粘贴优酷链接即可零门槛解析。');
        } else {
          window.alert('❌ 登录未完成：' + (d.detail || '未知错误') + '\n可重试。');
        }
      } catch (e) {
        window.alert('登录请求出错：' + e.message);
      } finally {
        loginBtn.textContent = '登录优酷';
        refreshStatus();
      }
    });

    openBtn.addEventListener('click', () => {
      window.VDL.desktop.openExternal(YK_HOME);
    });

    refreshStatus();
    setInterval(refreshStatus, 30 * 1000);
  })();
})();
