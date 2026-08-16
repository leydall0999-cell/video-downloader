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
})();
