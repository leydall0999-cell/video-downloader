// ==UserScript==
// @name         百度网盘直链下载助手 (VideoDownloader)
// @namespace    https://video-downloader.local
// @version      1.0
// @description  拦截百度网盘下载直链，交给本地 VideoDownloader (aria2c) 高速下载，摆脱客户端限速
// @match        https://pan.baidu.com/*
// @match        https://yun.baidu.com/*
// @grant        none
// @run-at       document-start
// ==/UserScript==

(function () {
    'use strict';

    // 本地 VideoDownloader 后端地址（与 app 启动端口一致，默认 8321）
    const API = 'http://127.0.0.1:8321/api/baidu_dlink';

    // ---- 状态提示条 ----
    let _bar = null;
    function showStatus(msg, isErr) {
        if (!_bar) {
            _bar = document.createElement('div');
            _bar.id = 'vdl-tm-status';
            _bar.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;' +
                'padding:8px 14px;font:bold 13px/1.4 -apple-system,sans-serif;' +
                'text-align:center;color:#fff;background:#07c160;' +
                'box-shadow:0 2px 10px rgba(0,0,0,.25);cursor:pointer;';
            _bar.addEventListener('click', function () { _bar && (_bar.style.display = 'none'); });
            (document.body || document.documentElement).appendChild(_bar);
        }
        _bar.textContent = 'VDL: ' + msg;
        _bar.style.background = isErr ? '#e64340' : '#07c160';
        _bar.style.display = 'block';
    }

    // ---- 提取 dlink 并提交给本地后端 ----
    function submitDlink(data) {
        try {
            let link = '', fname = 'baidu_file';
            if (data && data.dlink) { link = data.dlink; if (data.filename) fname = data.filename; }
            else if (data && data.list && data.list[0]) {
                link = data.list[0].dlink || '';
                fname = data.list[0].filename || fname;
            }
            if (!link) return false;
            showStatus('已拿到直链，提交 aria2c 下载…');
            fetch(API, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ dlink: link, filename: fname })
            }).then(function (r) { return r.json(); }).then(function (r) {
                if (r && r.ok) showStatus('✓ 已提交下载: ' + fname);
                else showStatus('提交失败: ' + ((r && r.detail) || '未知错误'), true);
            }).catch(function (e) {
                showStatus('提交失败(后端未运行?): ' + e.message, true);
            });
            return true;
        } catch (e) { return false; }
    }

    function tryExtract(respText) {
        try {
            const data = JSON.parse(respText);
            // errno=0 才是成功
            if (data && data.errno === 0) return submitDlink(data);
        } catch (e) {}
        return false;
    }

    // ---- 拦截 XMLHttpRequest ----
    const origOpen = XMLHttpRequest.prototype.open;
    const origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (m, u) {
        this.__vdl_url = u;
        return origOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function () {
        const self = this;
        const url = self.__vdl_url || '';
        if (url.indexOf('sharedownload') !== -1 ||
            (url.indexOf('rest/2.0/xpan/file') !== -1 && url.indexOf('method=download') !== -1)) {
            self.addEventListener('load', function () {
                tryExtract(self.responseText);
            });
        }
        return origSend.apply(this, arguments);
    };

    // ---- 拦截 fetch ----
    const origFetch = window.fetch;
    window.fetch = function () {
        const urlArg = arguments[0];
        const urlStr = (typeof urlArg === 'string') ? urlArg : (urlArg && urlArg.url) || '';
        return origFetch.apply(this, arguments).then(function (resp) {
            if (urlStr.indexOf('sharedownload') !== -1 ||
                (urlStr.indexOf('rest/2.0/xpan/file') !== -1 && urlStr.indexOf('method=download') !== -1)) {
                resp.clone().json().then(function (data) {
                    if (data && data.errno === 0) submitDlink(data);
                }).catch(function () {});
            }
            return resp;
        });
    };

    // ---- 主动「高速下载」按钮：点击时自己构造 sharedownload 请求 ----
    // 利用页面已有的 bdstoken / sign（百度页面全局暴露）
    function getPageVar(name) {
        try { return window[name]; } catch (e) { return undefined; }
    }

    function buildShareDownload() {
        // 分享页：从 URL 和页面全局变量取参数
        const params = new URLSearchParams(location.search);
        const surl = params.get('surl') || (location.pathname.match(/\/s\/([A-Za-z0-9\-_]+)/) || [])[1] || '';
        // 选中的 fs_id（从页面 DOM 的 checkbox/data 属性取）
        const checked = document.querySelectorAll('.file-item input[type=checkbox]:checked, [data-fsid]:not([data-fsid=""])');
        let fsId = '';
        if (checked.length) {
            fsId = checked[0].getAttribute('data-fsid') || checked[0].value || '';
        }
        if (!fsId) {
            // 尝试从 yunData / 全局拿第一个文件
            const yd = getPageVar('yunData');
            if (yd && yd.file_list && yd.file_list[0]) fsId = yd.file_list[0].fs_id;
        }
        if (!fsId) { showStatus('未找到选中文件，请先在页面勾选', true); return; }

        const bdstoken = (getPageVar('yunData') && getPageVar('yunData').bdstoken) || '';
        const sign = (getPageVar('yunData') && getPageVar('yunData').sign) || '';
        const timestamp = Math.floor(Date.now() / 1000);
        const shareid = (getPageVar('yunData') && getPageVar('yunData').shareid) ||
                        (getPageVar('shareData') && getPageVar('shareData').shareid) || '';
        const uk = (getPageVar('yunData') && getPageVar('yunData').uk) ||
                   (getPageVar('shareData') && getPageVar('shareData').uk) || '';

        const query = new URLSearchParams({
            app_id: '250528',
            method: 'download',
            shareid: String(shareid),
            uk: String(uk),
            sign: String(sign),
            timestamp: String(timestamp),
            bdstoken: String(bdstoken),
            channel: 'chunlei',
            web: '1',
            fid_list: JSON.stringify([parseInt(fsId, 10)]),
        });
        const url = 'https://pan.baidu.com/api/sharedownload?' + query.toString();
        showStatus('正在请求直链…');
        fetch(url, { credentials: 'include', headers: { 'X-Requested-With': 'XMLHttpRequest' } })
            .then(function (r) { return r.json(); })
            .then(function (data) { if (!submitDlink(data)) showStatus('获取直链失败(可能需刷新页面或 sign 过期)', true); })
            .catch(function (e) { showStatus('请求失败: ' + e.message, true); });
    }

    function injectButton() {
        if (document.getElementById('vdl-tm-btn')) return;
        const btn = document.createElement('button');
        btn.id = 'vdl-tm-btn';
        btn.textContent = 'VDL 高速下载';
        btn.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:2147483647;' +
            'padding:10px 16px;border:none;border-radius:8px;background:#07c160;color:#fff;' +
            'font:bold 14px -apple-system,sans-serif;cursor:pointer;box-shadow:0 3px 12px rgba(0,0,0,.3);';
        btn.addEventListener('click', buildShareDownload);
        (document.body || document.documentElement).appendChild(btn);
    }

    // 页面加载后注入按钮
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', injectButton);
    } else {
        injectButton();
    }

    showStatus('VDL 油猴脚本已加载：点页面「下载」或右下角「VDL 高速下载」', false);
    setTimeout(function () { if (_bar) _bar.style.display = 'none'; }, 4000);
})();
