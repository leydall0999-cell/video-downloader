/* 视频下载站 · 前端逻辑（无依赖）
 * 约定：所有动态文本一律使用 textContent 写入，杜绝 innerHTML 注入风险。 */
(() => {
  'use strict';

  // 启动诊断：捕获任何未处理的脚本错误并显示在页面顶部红条，便于定位初始化失败
  // （之前默认视图兜底没生效，很可能是 IIFE 中途同步抛错导致末尾 switchView 未执行）。
  window.addEventListener('error', (e) => {
    try {
      let box = document.getElementById('bootErr');
      if (!box) {
        box = document.createElement('div');
        box.id = 'bootErr';
        box.style.cssText = 'position:fixed;left:0;right:0;top:0;z-index:2147483647;background:#c0392b;color:#fff;font:12px/1.5 monospace;padding:8px 12px;white-space:pre-wrap;';
        (document.body || document.documentElement).appendChild(box);
      }
      const loc = e.filename ? (' @ ' + String(e.filename).split('/').pop() + ':' + e.lineno) : '';
      box.textContent = '启动错误: ' + (e.message || (e.error && e.error.message) || e.error) + loc;
    } catch (_) {}
  });
  // 默认展示「已生成成片」列表：用 setTimeout 异步兜底，即使后续初始化同步抛错，
  // 事件循环也会执行本回调切到解说视图，不再依赖 IIFE 末尾是否跑到。
  setTimeout(() => { try { switchView('commentary'); } catch (_) {} }, 0);

  // 启动即强制隐藏全局错误提示框，确保「打开默认不显示」（即使带缓存的旧 DOM 残留 hidden 被改动）
  try { const _ab = document.getElementById('alertBox'); if (_ab) _ab.hidden = true; } catch (_) {}

  const STATUS_TEXT = {
    pending: '排队中',
    downloading: '下载中',
    merging: '合并中',
    paused: '已暂停',
    completed: '已完成',
    failed: '失败',
    canceled: '已取消',
  };
  const ACTIVE_STATES = ['pending', 'downloading', 'merging', 'paused'];
  const POLL_FALLBACK_MS = 1500;

  /** 时间格式化 (mm:ss.s) —— 提前到 IIFE 顶部，避免 Safari TDZ 误报 */
  const fmtTs = (t) => {
    const m = Math.floor(t / 60);
    const s = (t % 60).toFixed(1);
    return `${m}:${s.padStart(4, '0')}`;
  };
  /** HTML 转义 —— 提前到 IIFE 顶部，避免 Safari TDZ 误报 */
  const escHtml = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const $ = (id) => document.getElementById(id);
  const el = {
    form: $('resolveForm'),
    input: $('urlInput'),
    clearBtn: $('clearBtn'),
    resolveBtn: $('resolveBtn'),
    chips: $('platformChips'),
    alert: $('alertBox'),
    alertTitle: $('alertTitle'),
    alertHint: $('alertHint'),
    resultPanel: $('resultPanel'),
    thumb: $('videoThumb'),
    duration: $('videoDuration'),
    title: $('videoTitle'),
    platform: $('videoPlatform'),
    uploader: $('videoUploader'),
    qualityGrid: $('qualityGrid'),
    downloadBtn: $('downloadBtn'),
    tasksPanel: $('tasksPanel'),
    taskList: $('taskList'),
    badge: $('engineBadge'),
    template: $('taskTemplate'),
    modal: $('platformModal'),
    modalGrid: $('platformModalGrid'),
    modalTitle: $('platformModalTitle'),
    modalClose: $('platformModalClose'),
    cookieInput: $('cookieInput'),
    cookieDetectBtn: $('cookieDetectBtn'),
    cookieStatus: $('cookieStatus'),
    proxyInput: $('proxyInput'),
    qualityBlock: $('qualityBlock'),
    extractSelect: $('extractSelect'),
    directHint: $('directHint'),
    serverFallbackBtn: $('serverFallbackBtn'),
    nodeBar: $('nodeBar'),
    nodeDot: $('nodeDot'),
    nodeText: $('nodeText'),
    nodeSwitch: $('nodeSwitch'),
    adsSlot: $('adsSlot'),
    subBadge: $('subBadge'),
    subModal: $('subModal'),
    subModalClose: $('subModalClose'),
    subModalSub: $('subModalSub'),
    subInput: $('subInput'),
    subApply: $('subApply'),
    subMsg: $('subMsg'),
    cloudModal: $('cloudModal'),
    cloudModalClose: $('cloudModalClose'),
    cloudWebdavForm: $('cloudWebdavForm'),
    cloudBaiduForm: $('cloudBaiduForm'),
    cloudBaiduRadio: $('cloudBaiduRadio'),
    cloudWebdavUrl: $('cloudWebdavUrl'),
    cloudWebdavUser: $('cloudWebdavUser'),
    cloudWebdavPass: $('cloudWebdavPass'),
    cloudBaiduBtn: $('cloudBaiduBtn'),
    cloudBaiduStatus: $('cloudBaiduStatus'),
    cloudDest: $('cloudDest'),
    cloudSave: $('cloudSave'),
    cloudStatus: $('cloudStatus'),
    cloudSubNote: $('cloudSubNote'),
    // 批量下载（桌面版万能下载器重点能力）
    batchToggle: $('batchToggle'),
    batchBox: $('batchBox'),
    batchInput: $('batchInput'),
    batchQuality: $('batchQuality'),
    batchConcurrency: $('batchConcurrency'),
    batchConcVal: $('batchConcVal'),
    batchBtn: $('batchBtn'),
    // 正版素材库入口 & 下载用途确认
    consentModal: $('consentModal'),
    consentCommercialBox: $('consentCommercialBox'),
    consentAuthorized: $('consentAuthorized'),
    consentConfirm: $('consentConfirm'),
    consentCancel: $('consentCancel'),
    consentClose: $('consentClose'),
    consentErr: $('consentErr'),
    queueBar: $('queueBar'),
    cancelAllBtn: $('cancelAllBtn'),
    openFolderBtn: $('openFolderBtn'),
    // 媒体库（桌面版功能）
    tabs: $('tabs'),
    tabDownload: $('tabDownload'),
    tabLibrary: $('tabLibrary'),
    downloadView: $('downloadView'),
    libraryView: $('libraryView'),
    libRefresh: $('libRefresh'),
    libCleanup: $('libCleanup'),
    cleanModal: $('cleanModal'),
    cleanModalClose: $('cleanModalClose'),
    cleanUsage: $('cleanUsage'),
    cleanAuto: $('cleanAuto'),
    cleanInterval: $('cleanInterval'),
    cleanTempOn: $('cleanTempOn'),
    cleanTempDays: $('cleanTempDays'),
    cleanFramesOn: $('cleanFramesOn'),
    cleanFramesDays: $('cleanFramesDays'),
    cleanThumbsOn: $('cleanThumbsOn'),
    cleanThumbsDays: $('cleanThumbsDays'),
    cleanMediaOn: $('cleanMediaOn'),
    cleanMediaDays: $('cleanMediaDays'),
    cleanQuotaOn: $('cleanQuotaOn'),
    cleanQuotaGb: $('cleanQuotaGb'),
    cleanTrashWarn: $('cleanTrashWarn'),
    cleanSave: $('cleanSave'),
    cleanScan: $('cleanScan'),
    cleanRun: $('cleanRun'),
    cleanStatus: $('cleanStatus'),
    cleanPreview: $('cleanPreview'),
    // 归档网盘（桌面版功能）
    libArchive: $('libArchive'),
    archiveModal: $('archiveModal'),
    archiveModalClose: $('archiveModalClose'),
    arcBaiduRadio: $('arcBaiduRadio'),
    arcWebdavForm: $('arcWebdavForm'),
    arcBaiduForm: $('arcBaiduForm'),
    arcWebdavUrl: $('arcWebdavUrl'),
    arcWebdavUser: $('arcWebdavUser'),
    arcWebdavPass: $('arcWebdavPass'),
    arcBaiduBtn: $('arcBaiduBtn'),
    arcBaiduToken: $('arcBaiduToken'),
    arcBaiduStatus: $('arcBaiduStatus'),
    arcTemplate: $('arcTemplate'),
    arcTokens: $('arcTokens'),
    arcVideo: $('arcVideo'),
    arcAudio: $('arcAudio'),
    arcImage: $('arcImage'),
    arcMinAge: $('arcMinAge'),
    arcMaxGb: $('arcMaxGb'),
    arcDeleteAfter: $('arcDeleteAfter'),
    arcAuto: $('arcAuto'),
    arcInterval: $('arcInterval'),
    arcTrashWarn: $('arcTrashWarn'),
    arcSave: $('arcSave'),
    arcScan: $('arcScan'),
    arcRun: $('arcRun'),
    arcCancel: $('arcCancel'),
    arcStatus: $('arcStatus'),
    arcPreview: $('arcPreview'),
    arcRecords: $('arcRecords'),
    arcForget: $('arcForget'),
    // 库内保险箱（桌面版功能）
    libCrypto: $('libCrypto'),
    cryptoModal: $('cryptoModal'),
    cryptoModalClose: $('cryptoModalClose'),
    cryptoView: $('cryptoView'),
    cryptoPass: $('cryptoPass'),
    cryptoConfirm: $('cryptoConfirm'),
    cryptoUnlockPass: $('cryptoUnlockPass'),
    cryptoSetPass: $('cryptoSetPass'),
    cryptoUnlock: $('cryptoUnlock'),
    cryptoLock: $('cryptoLock'),
    cryptoFilter: $('cryptoFilter'),
    cryptoList: $('cryptoList'),
    cryptoEncrypt: $('cryptoEncrypt'),
    cryptoDecrypt: $('cryptoDecrypt'),
    cryptoStatus: $('cryptoStatus'),
    cryptoJob: $('cryptoJob'),
    libSearch: $('libSearch'),
    libPlatform: $('libPlatform'),
    libKind: $('libKind'),
    libGrid: $('libGrid'),
    libEmpty: $('libEmpty'),
    libModal: $('libModal'),
    libModalClose: $('libModalClose'),
    libPlayer: $('libPlayer'),
    libMeta: $('libMeta'),
    libDownload: $('libDownload'),
    libDelete: $('libDelete'),
    libSubtitle: $('libSubtitle'),
    subPanel: $('subPanel'),
    subPanelClose: $('subPanelClose'),
    subCookie: $('subCookie'),
    subProbe: $('subProbe'),
    subStatus: $('subStatus'),
    subExtractRow: $('subExtractRow'),
    subLang: $('subLang'),
    subExtract: $('subExtract'),
    subExtractList: $('subExtractList'),
    subApiKey: $('subApiKey'),
    subBaseUrl: $('subBaseUrl'),
    subModel: $('subModel'),
    subTarget: $('subTarget'),
    subTranslate: $('subTranslate'),
    subBurn: $('subBurn'),
    // LLM 服务商选择器（统一配置面板）
    llmProvider: $('llmProvider'),
    llmApiKey: $('llmApiKey'),
    llmBaseUrl: $('llmBaseUrl'),
    llmModel: $('llmModel'),
    llmSave: $('llmSave'),
    llmStatus: $('llmStatus'),
    // 格式 / 片段加工（桌面版功能）
    libProcess: $('libProcess'),
    libCommentary: $('libCommentary'),
    libCommentaryStatus: $('libCommentaryStatus'),
    libCommentaryFile: $('libCommentaryFile'),
    // 解说成片独立标签页
    commentaryView: $('commentaryView'),
    comGrid: $('comGrid'),
    comEmpty: $('comEmpty'),
    comHistory: $('comHistory'),
    comHistoryCount: $('comHistoryCount'),
    comHistoryToolbar: $('comHistoryToolbar'),
    comGrid: $('comGrid'),
    comSortBtn: $('comSortBtn'),
    comSortMenu: $('comSortMenu'),
    comSortLabel: $('comSortLabel'),
    comSource: $('comSource'),
    comGenerateScript: $('comGenerateScript'),
    comScriptPanel: $('comScriptPanel'),
    comScriptVoice: $('comScriptVoice'),
    comScriptVoicePreview: $('comScriptVoicePreview'),
    comScriptSegments: $('comScriptSegments'),
    comScriptSave: $('comScriptSave'),
    comScriptRender: $('comScriptRender'),
    comScriptFile: $('comScriptFile'),
    comScriptStatus: $('comScriptStatus'),
    comScriptPrevAll: $('comScriptPrevAll'),
    comAudioPreview: $('comAudioPreview'),
    comProgress: $('comProgress'),
    comPhase: $('comPhase'),
    comPercent: $('comPercent'),
    comBarFill: $('comBarFill'),
    comStatus: $('comStatus'),
    comReviewActions: $('comReviewActions'),
    comOpenReview: $('comOpenReview'),
    comGenerateOneClick: $('comGenerateOneClick'),
    comIntroHighlight: $('comIntroHighlight'),
    comSkipIntroOutro: $('comSkipIntroOutro'),
    comKeepNoNarrate: $('comKeepNoNarrate'),
    comRetainPct: $('comRetainPct'),
    comStepsPanel: $('comStepsPanel'),
    comStepsList: $('comStepsList'),
    comLogs: $('comLogs'),
    comRefresh: $('comRefresh'),
    comEnvStatus: $('comEnvStatus'),
    comFileInput: $('comFileInput'),
    comFileBtn: $('comFileBtn'),
    comFileName: $('comFileName'),
    comFileStatus: $('comFileStatus'),
    comDropZone: $('comDropZone'),
    comTrimCard: $('comTrimCard'),
    comPreview: $('comPreview'),
    comTrimStartRange: $('comTrimStartRange'),
    comTrimEndRange: $('comTrimEndRange'),
    comTrimStart: $('comTrimStart'),
    comTrimEnd: $('comTrimEnd'),
    comTrimDuration: $('comTrimDuration'),
    comTrimSetStart: $('comTrimSetStart'),
    comTrimSetEnd: $('comTrimSetEnd'),
    comTrimPreview: $('comTrimPreview'),
    comTrimReset: $('comTrimReset'),
    comEta: $('comEta'),
    tabCommentary: $('tabCommentary'),
    processPanel: $('processPanel'),
    processPanelClose: $('processPanelClose'),
    processOp: $('processOp'),
    processParams: $('processParams'),
    processRun: $('processRun'),
    processStatus: $('processStatus'),
    // 批量处理 + 加工队列
    libShowQueue: $('libShowQueue'),
    queuePanel: $('queuePanel'),
    queuePanelClose: $('queuePanelClose'),
    queueConcurrency: $('queueConcurrency'),
    queueConcurrencyVal: $('queueConcurrencyVal'),
    queueList: $('queueList'),
    queueEmpty: $('queueEmpty'),
    libBatch: $('libBatch'),
    libSelectAll: $('libSelectAll'),
    libDeselectAll: $('libDeselectAll'),
    libBatchCount: $('libBatchCount'),
    libBatchProcess: $('libBatchProcess'),
    // 订阅追更（桌面版功能）
    tabSubscribe: $('tabSubscribe'),
    subscribeView: $('subscribeView'),
    subUrl: $('subUrl'),
    subName: $('subName'),
    subQuality: $('subQuality'),
    subAuto: $('subAuto'),
    subAddBtn: $('subAddBtn'),
    subHint: $('subHint'),
    subList: $('subList'),
    subEmpty: $('subEmpty'),
    // 种子下载（桌面版功能）
    tabTorrent: $('tabTorrent'),
    torrentView: $('torrentView'),
    torAddInput: $('torAddInput'),
    torTorrentFile: $('torTorrentFile'),
    torSavePath: $('torSavePath'),
    torAddBtn: $('torAddBtn'),
    torEmpty: $('torEmpty'),
    torList: $('torList'),
    torStatus: $('torStatus'),
  };

  // 解说裁剪状态
  let comTrimStart = 0;        // 裁剪起点（秒）
  let comTrimEnd = 0;          // 裁剪终点（秒）
  let comPreviewDuration = 0;  // 源视频总时长（秒）
  let comPreviewUrl = null;    // 预览视频 URL（本地文件为 objectURL，需释放）
  let comPreviewW = 0;         // 源视频宽度（像素，0=未加载）
  let comPreviewH = 0;         // 源视频高度（像素，0=未加载）

  /** 当前解析结果：{ url, platform, video, qualities, base } */
  let resolved = null;
  let selectedQuality = 'best';
  let allPlatforms = [];
  const trackers = new Map();

  // 解说成片列表视图状态
  let commentaryItems = [];
  let commentaryViewMode = 'list';   // grid | list | timeline | gallery
  let commentarySort = 'mtime-desc'; // mtime-desc | mtime-asc | size-desc | size-asc | name-asc | name-desc

  // -------------------------------------------------------------- 节点分流
  // 双节点部署时，国内站请求发往国内节点、海外站发往海外节点，各自直连目标站，
  // 免去跨境回源。单节点部署（peer 为空）时全部走本节点，行为与以前一致。

  const node = { region: 'global', peer: '', chinaDomains: [], commentaryEnabled: false, adsEnabled: false,
    convertSubRequired: false, convertFreeDaily: 3,
    downloadSubRequired: false, downloadFreeDaily: 10, downloadFreeUsed: 0, subscribed: false,
    cloudSubRequired: false, cloudFreeDaily: 5, cloudFreeUsed: 0,
    cloudProviders: ['webdav'], baiduAvailable: false, baiduAuthUrl: '',
    libraryEnabled: false,
    subscriptionsEnabled: false,
    retentionEnabled: false,
    trashAvailable: false,
    archiveEnabled: false,
    archiveBaiduAvailable: false,
    archiveConfigured: false,
    cryptoEnabled: false,
    cryptoHasPass: false,
    cryptoLocked: true,
  };
  /** 手动覆盖：null=自动判断，'cn'/'global'=用户强制指定 */
  let forcedRegion = null;
  /** 最近一个下载完成的任务（供交叉入口「存到网盘」定位；task 结束时 trackers 会移除，故单独留存） */
  let lastCompletedTask = null;
  let lastCompletedRefs = null;

  const hostOf = (raw) => {
    try {
      return new URL(String(raw).trim()).hostname.toLowerCase().replace(/^(www|m)\./, '');
    } catch {
      return '';
    }
  };

  const isChinaHost = (host) => {
    if (!host) return false;
    if (host.endsWith('.cn') || host.includes('.com.cn')) return true;
    return node.chinaDomains.some((d) => host === d || host.endsWith(`.${d}`));
  };

  /** 这条链接该由哪个区处理 */
  const regionFor = (raw) => forcedRegion || (isChinaHost(hostOf(raw)) ? 'cn' : 'global');

  /** 目标区对应的 API 前缀：本节点用相对路径（空串），对端用其完整地址 */
  const baseFor = (raw) => (!node.peer || regionFor(raw) === node.region ? '' : node.peer);

  const REGION_LABEL = { cn: '国内线路', global: '海外线路' };

  const paintNodeBar = () => {
    el.nodeBar.hidden = false;                     // 线路条常驻：让用户始终能看到当前走哪条线
    if (!node.peer) {
      // 单节点部署：全部请求走本机，不提供线路切换
      el.nodeDot.className = 'node-dot';
      el.nodeText.textContent = '线路：本机直连';
      el.nodeSwitch.hidden = true;
      return;
    }
    const region = regionFor(el.input.value);
    el.nodeDot.className = `node-dot is-${region}`;
    el.nodeText.textContent = `线路：${REGION_LABEL[region]}（${forcedRegion ? '已手动指定' : '自动'}）`;
    el.nodeSwitch.hidden = false;
    el.nodeSwitch.textContent = forcedRegion ? '恢复自动' : '切换线路';
  };

  // ------------------------------------------------------------------ 工具

  const _parseResponse = async (response) => {
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const err = { message: payload.error || payload.detail || '请求失败，请稍后重试', hint: payload.hint || '' };
      if (response.status === 402) err.subscribe = true;   // 免费额度耗尽，引导订阅
      throw err;
    }
    return payload;
  };

  const request = async (path, options = {}, base = '') => {
    const headers = {};
    const subKey = localStorage.getItem('vdl_sub_key');
    if (subKey) headers['X-Subscription-Key'] = subKey;
    const apiToken = localStorage.getItem('vdl_api_token');
    if (apiToken) headers['X-Api-Key'] = apiToken;
    // FormData（multipart 上传）不强制 Content-Type，交给浏览器设 boundary；
    // 其余默认 JSON。options.headers 仅做增强、不覆盖（避免丢失 token）。
    const isForm = options.body instanceof FormData;
    if (!isForm && !(options.headers && 'Content-Type' in options.headers)) {
      headers['Content-Type'] = 'application/json';
    }
    const merged = { ...headers, ...(options.headers || {}) };
    const doFetch = () => fetch(base + path, { ...options, headers: merged });
    let response = await doFetch();
    if (response.status === 401) {
      // 服务端启用了 token 鉴权但本端未提供/提供错误：引导用户输入
      const t = (typeof prompt === 'function') ? prompt('该服务已启用访问令牌，请输入 API Token：') : null;
      if (t && t.trim()) {
        localStorage.setItem('vdl_api_token', t.trim());
        merged['X-Api-Key'] = t.trim();
        response = await doFetch();
      }
    }
    return await _parseResponse(response);
  };

  const formatBytes = (bytes) => {
    if (!bytes || bytes <= 0) return '--';
    const units = ['B', 'KB', 'MB', 'GB'];
    let value = bytes;
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    return `${value.toFixed(value >= 100 || index === 0 ? 0 : 1)} ${units[index]}`;
  };

  // 自定义确认弹窗（不依赖 pywebview 的 window.confirm，后者在 WebView 下无效）
  const showConfirm = (message, { okText = '确定', cancelText = '取消', danger = false } = {}) =>
    new Promise((resolve) => {
      let overlay = document.getElementById('vdl-confirm-overlay');
      if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'vdl-confirm-overlay';
        overlay.style.cssText =
          'position:fixed;inset:0;background:rgba(0,0,0,.45);display:none;align-items:center;justify-content:center;z-index:9999;';
        document.body.appendChild(overlay);
      }
      overlay.innerHTML = '';
      const box = document.createElement('div');
      box.style.cssText =
        'background:#1f2430;color:#eaeaea;max-width:360px;width:90%;border-radius:10px;padding:18px;box-shadow:0 8px 30px rgba(0,0,0,.4);font-size:14px;';
      const msg = document.createElement('div');
      msg.style.cssText = 'white-space:pre-wrap;line-height:1.5;margin-bottom:16px;';
      msg.textContent = message;
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;gap:10px;justify-content:flex-end;';
      const cancel = document.createElement('button');
      cancel.textContent = cancelText;
      cancel.style.cssText =
        'padding:7px 14px;border:none;border-radius:6px;background:#3a4356;color:#eaeaea;cursor:pointer;';
      const ok = document.createElement('button');
      ok.textContent = okText;
      ok.style.cssText = `padding:7px 14px;border:none;border-radius:6px;cursor:pointer;background:${danger ? '#e5484d' : '#2f81f7'};color:#fff;`;
      const close = (v) => {
        overlay.style.display = 'none';
        resolve(v);
      };
      cancel.onclick = () => close(false);
      ok.onclick = () => close(true);
      row.appendChild(cancel);
      row.appendChild(ok);
      box.appendChild(msg);
      box.appendChild(row);
      overlay.appendChild(box);
      overlay.style.display = 'flex';
      ok.focus();
    });

  // 轻量 toast 提示
  const showToast = (msg, ms = 2600) => {
    let t = document.getElementById('vdl-toast');
    if (!t) {
      t = document.createElement('div');
      t.id = 'vdl-toast';
      t.style.cssText =
        'position:fixed;left:50%;bottom:32px;transform:translateX(-50%);background:#222a38;color:#eaeaea;padding:10px 16px;border-radius:8px;font-size:13px;z-index:10000;box-shadow:0 6px 20px rgba(0,0,0,.4);max-width:80vw;display:none;';
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.style.display = 'block';
    clearTimeout(t._timer);
    t._timer = setTimeout(() => {
      t.style.display = 'none';
    }, ms);
  };


  const formatDuration = (seconds) => {
    if (!seconds || seconds <= 0) return '';
    const total = Math.round(seconds);
    const parts = [Math.floor(total / 3600), Math.floor((total % 3600) / 60), total % 60];
    const trimmed = parts[0] ? parts : parts.slice(1);
    return trimmed.map((n, i) => (i === 0 ? String(n) : String(n).padStart(2, '0'))).join(':');
  };

  const formatEta = (seconds) => (seconds > 0 ? `剩余 ${formatDuration(seconds) || '<1s'}` : '');

  // 秒数 → HH:MM:SS（裁剪时间输入用）
  const formatHMS = (sec) => {
    if (!isFinite(sec) || sec < 0) sec = 0;
    sec = Math.floor(sec);
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    return [h, m, s].map((n) => String(n).padStart(2, '0')).join(':');
  };
  // HH:MM:SS / MM:SS / 纯数字秒 → 秒数
  const parseHMS = (str) => {
    if (str == null) return 0;
    str = String(str).trim();
    if (/^\d+(\.\d+)?$/.test(str)) return parseFloat(str);
    const parts = str.split(':').map((p) => parseInt(p, 10) || 0);
    if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
    if (parts.length === 2) return parts[0] * 60 + parts[1];
    return parts[0] || 0;
  };
  // unix 时间戳 → 本地时钟 HH:MM:SS
  const formatClock = (ts) => {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    return [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, '0')).join(':');
  };

  // 转换订阅额度显示：仅在订阅墙开启时生效
  const updateConvertQuota = (refs, quota) => {
    if (!node.convertSubRequired) return;
    const q = refs.convertQuota;
    q.hidden = false;
    if (quota && quota.subscribed) {
      node.subscribed = true;
      q.textContent = '已订阅 · 无限转换 ✓';
      q.className = 'convert-quota is-sub';
      el.subBadge.textContent = '已订阅 ✓';
      el.subBadge.hidden = false;
      return;
    }
    const total = (quota && quota.free_daily) || node.convertFreeDaily;
    const used = (quota && quota.free_used) || 0;
    const left = Math.max(0, total - used);
    q.className = 'convert-quota' + (left <= 0 ? ' is-empty' : '');
    q.textContent = left > 0
      ? `今日免费剩余 ${left}/${total} 次`
      : '今日免费次数已用完 · 点右上角订阅解锁';
  };

  const buildStats = (task) => {
    if (task.status === 'completed') return `${formatBytes(task.filesize)} · 已就绪`;
    if (task.status === 'failed') return '下载中断';
    if (task.status === 'canceled') return '已取消';
    if (task.status === 'paused') return `已暂停（已下载 ${formatBytes(task.downloaded_bytes)}）`;
    if (task.status === 'merging') return '正在合并音视频…';
    if (task.status === 'downloading') {
      const eta = task.eta > 0 ? ` · 剩余 ${formatEta(task.eta)}` : '';
      // 总大小已知：显示百分比 + 已下/总 + 速度 + ETA（最完整）
      if (task.total_bytes > 0) {
        const pct = ((task.downloaded_bytes || 0) / task.total_bytes * 100);
        const speed = task.speed > 0 ? `${formatBytes(task.speed)}/s` : '';
        return `${pct.toFixed(1)}% · ${formatBytes(task.downloaded_bytes)} / ${formatBytes(task.total_bytes)} · ${speed}${eta}`.replace(' ·  · ', ' · ');
      }
      // 拿不到总大小（多数小站）：有下载量就立刻显示，不等 speed
      if (task.downloaded_bytes > 0) {
        const parts = [`已下载 ${formatBytes(task.downloaded_bytes)}`];
        if (task.speed > 0) parts.push(`${formatBytes(task.speed)}/s`);
        if (task.eta > 0) parts.push(`剩余 ${formatEta(task.eta)}`);
        return parts.join(' · ');
      }
      // 还没接到首个进度回调：占位 + indeterminate 扫动已在 paintTask 里处理
      return '建立连接中…';
    }
    // 排队中、解析中等
    if (!task.total_bytes) return '正在建立连接…';
    const speed = task.speed > 0 ? `${formatBytes(task.speed)}/s` : '';
    return [`${formatBytes(task.downloaded_bytes)} / ${formatBytes(task.total_bytes)}`, speed, formatEta(task.eta)]
      .filter(Boolean)
      .join(' · ');
  };

  // ------------------------------------------------------------------ 提示

  const showError = (message, hint = '') => {
    const msg = String(message || '').trim();
    if (!msg) { clearError(); return; }
    el.alertTitle.textContent = msg;
    el.alertHint.textContent = String(hint || '').trim();
    el.alertHint.hidden = !String(hint || '').trim();
    el.alert.hidden = false;
  };

  const clearError = () => { el.alert.hidden = true; };
  document.getElementById('alertClose').addEventListener('click', clearError);

  const setLoading = (loading) => {
    el.resolveBtn.classList.toggle('loading', loading);
    el.resolveBtn.disabled = loading;
    el.resolveBtn.querySelector('.btn-label').textContent = loading ? '解析中…' : '解析链接';
  };

  // ------------------------------------------------------------------ 渲染

  const MAX_VISIBLE_PLATFORMS = 16;

  const renderPlatforms = (platforms) => {
    allPlatforms = platforms;
    el.chips.replaceChildren();
    platforms.slice(0, MAX_VISIBLE_PLATFORMS).forEach(({ name, icon }) => {
      const chip = document.createElement('span');
      chip.className = 'chip';
      chip.textContent = (icon ? icon + ' ' : '') + name;
      el.chips.appendChild(chip);
    });
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'chip chip-more';
    more.textContent = `查看全部 ${platforms.length} 个平台 →`;
    more.setAttribute('aria-haspopup', 'dialog');
    more.addEventListener('click', () => openPlatformModal(platforms));
    el.chips.appendChild(more);
    el.badge.textContent = `支持 ${platforms.length} 个平台`;
  };

  const openPlatformModal = (platforms) => {
    el.modalGrid.replaceChildren();
    platforms.forEach(({ name, icon }) => {
      const item = document.createElement('div');
      item.className = 'modal-item';
      const ic = document.createElement('span');
      ic.className = 'modal-item-icon';
      ic.textContent = icon || '🌐';
      const nm = document.createElement('span');
      nm.textContent = name;
      item.append(ic, nm);
      el.modalGrid.appendChild(item);
    });
    el.modalTitle.textContent = `支持的平台（${platforms.length}）`;
    if (typeof el.modal.showModal === 'function') el.modal.showModal();
    else el.modal.setAttribute('open', '');
  };

  const renderQualities = (qualities) => {
    el.qualityGrid.replaceChildren();
    selectedQuality = qualities[0]?.key ?? 'best';

    qualities.forEach((quality) => {
      const option = document.createElement('button');
      option.type = 'button';
      option.className = 'quality-opt';
      option.setAttribute('role', 'radio');
      option.setAttribute('aria-checked', String(quality.key === selectedQuality));
      option.dataset.key = quality.key;

      const label = document.createElement('strong');
      label.textContent = quality.label;
      const note = document.createElement('small');
      note.textContent = quality.approx_size
        ? `${quality.note} · 约 ${formatBytes(quality.approx_size)}`
        : quality.note;

      option.append(label, note);
      option.addEventListener('click', () => selectQuality(quality.key));
      el.qualityGrid.appendChild(option);
    });
  };

  const selectQuality = (key) => {
    selectedQuality = key;
    el.qualityGrid.querySelectorAll('.quality-opt').forEach((node) => {
      node.setAttribute('aria-checked', String(node.dataset.key === key));
    });
  };

  const renderVideo = (data) => {
    const { video, platform } = data;
    el.title.textContent = video.title;
    el.platform.textContent = platform.name;
    el.uploader.textContent = video.uploader || '未知作者';

    const duration = formatDuration(video.duration);
    el.duration.textContent = duration;
    el.duration.hidden = !duration;

    el.thumb.hidden = !video.thumbnail;
    if (video.thumbnail) {
      el.thumb.src = video.thumbnail;
      el.thumb.alt = `${video.title} 封面`;
      el.thumb.onerror = () => { el.thumb.hidden = true; };
    }

    const directUrl = video.direct_url;
    if (directUrl) {
      // 直链透传：跳过清晰度选择与服务器下载，直接让浏览器从源站拉文件
      el.qualityBlock.hidden = true;
      el.downloadBtn.lastChild.textContent = '直接保存到本机 ⬇';
      el.directHint.hidden = false;
      el.directHint.textContent = '✅ 检测到这是可直接下载的文件，已为你跳过服务器处理。点上方按钮即从源站保存到你的电脑，不经过我们的服务器。';
      el.serverFallbackBtn.hidden = false;
    } else {
      el.qualityBlock.hidden = false;
      el.downloadBtn.lastChild.textContent = '开始下载';
      el.directHint.hidden = true;
      el.directHint.textContent = '';
      el.serverFallbackBtn.hidden = true;
      renderQualities(data.qualities);
    }
    el.resultPanel.hidden = false;
  };

  // ------------------------------------------------------------------ 任务卡片

  const createTaskCard = (taskId, meta) => {
    const node = el.template.content.firstElementChild.cloneNode(true);
    node.dataset.taskId = taskId;
    const refs = {
      root: node,
      fallbackTitle: meta.title,
      title: node.querySelector('[data-title]'),
      platform: node.querySelector('[data-platform]'),
      quality: node.querySelector('[data-quality]'),
      status: node.querySelector('[data-status]'),
      bar: node.querySelector('[data-bar]'),
      stats: node.querySelector('[data-stats]'),
      cancel: node.querySelector('[data-cancel]'),
      pause: node.querySelector('[data-pause]'),
      resume: node.querySelector('[data-resume]'),
      save: node.querySelector('[data-save]'),
      error: node.querySelector('[data-error]'),
      saveHint: node.querySelector('[data-save-hint]'),
      convertWrap: node.querySelector('[data-convert-wrap]'),
      convertTarget: node.querySelector('[data-convert-target]'),
      convertRes: node.querySelector('[data-convert-res]'),
      convertBtn: node.querySelector('[data-convert-btn]'),
      convertFile: node.querySelector('[data-convert-file]'),
      convertStatus: node.querySelector('[data-convert-status]'),
      cloud: node.querySelector('[data-cloud]'),
      cloudStatus: node.querySelector('[data-cloud-status]'),
      retry: node.querySelector('[data-retry]'),
      del: node.querySelector('[data-delete]'),
      slowWarn: node.querySelector('[data-slow-warn]'),
      stepsBox: node.querySelector('[data-steps-box]'),
      stepsToggle: node.querySelector('[data-steps-toggle]'),
      stepsToggleLabel: node.querySelector('.task-steps-toggle-label'),
      stepsChevron: node.querySelector('[data-steps-chevron]'),
      stepsPanel: node.querySelector('[data-steps-panel]'),
      stepsList: node.querySelector('[data-steps-list]'),
      logs: node.querySelector('[data-logs]'),
      extractWrap: node.querySelector('[data-extract-wrap]'),
      extractBody: node.querySelector('[data-extract-text]'),
      extractCopy: node.querySelector('[data-extract-copy]'),
      extractRetry: node.querySelector('[data-extract-retry]'),
    };
    refs.cancel.addEventListener('click', () => cancelTask(taskId, refs.base || ''));
    refs.pause.addEventListener('click', () => pauseTask(taskId, refs.base || ''));
    refs.resume.addEventListener('click', () => resumeTask(taskId, refs.base || ''));
    refs.retry.addEventListener('click', () => retryTask(taskId, refs));
    refs.del.addEventListener('click', () => deleteTask(taskId, refs));
    // 慢速告警横幅内的快捷动作（换清晰度重试 / 填代理），事件委托到卡片根节点，
    // 每次 paintTask 重渲染横幅也不会重复绑定。
    node.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-action]');
      if (!btn) return;
      const action = btn.dataset.action;
      const url = refs.root.dataset.srcUrl;
      if (action === 'retry-quality') {
        const quality = btn.dataset.quality;
        // 自动取消当前慢速任务，开启更低清晰度重试（复用当前解析的 url/cookie/proxy）
        cancelTask(taskId, refs.base || '');
        startDownload(quality, {
          url,
          cookie: el.cookieInput.value.trim(),
          proxy: el.proxyInput.value.trim(),
        });
      } else if (action === 'focus-proxy') {
        el.proxyInput.focus();
        el.proxyInput.scrollIntoView({ behavior: 'smooth', block: 'center' });
        if (el.advancedToggle) el.advancedToggle.classList.add('is-open');
      }
    });
    refs.stepsToggle.addEventListener('click', () => {
      const hidden = refs.stepsPanel.hidden;
      refs.stepsPanel.hidden = !hidden;
      refs.stepsChevron.textContent = hidden ? '▼' : '▶';
      refs.stepsToggleLabel.textContent = hidden ? '收起过程' : '查看过程';
    });
    refs.extractCopy.addEventListener('click', () => {
      const text = refs.extractBody.textContent || '';
      if (!text) return;
      navigator.clipboard.writeText(text).then(() => {
        const old = refs.extractCopy.textContent;
        refs.extractCopy.textContent = '已复制';
        setTimeout(() => { refs.extractCopy.textContent = old; }, 1500);
      });
    });
    refs.extractRetry.addEventListener('click', () => {
      if (!refs.extractRetry.dataset.running) {
        refs.extractRetry.dataset.running = '1';
        const base = refs.base || '';
        request(`/api/tasks/${taskId}/extract-text`, { method: 'POST' }, base)
          .catch((err) => alert('重试提取失败：' + (err.message || err)))
          .finally(() => { refs.extractRetry.dataset.running = ''; });
      }
    });
    refs.title.textContent = meta.title;
    refs.platform.textContent = meta.platform;
    el.taskList.prepend(node);
    el.tasksPanel.hidden = false;
    return refs;
  };

  const paintTask = (refs, task, autoSave) => {
    const active = ACTIVE_STATES.includes(task.status);
    refs.title.textContent = task.title || refs.fallbackTitle || '解析中…';
    refs.platform.textContent = task.platform;
    refs.quality.textContent = task.quality;
    refs.status.textContent = STATUS_TEXT[task.status] || task.status;
    refs.status.dataset.state = task.status;
    // 进度条：所有进行中状态无 total 时都走 indeterminate 扫动，
    // 让 chrqj 这类秒下的任务从 queued→downloading→merging 全程有视觉反馈
    const indeterminate = active && (!task.total_bytes || task.total_bytes <= 0);
    refs.bar.parentElement.classList.toggle('is-indeterminate', indeterminate);
    refs.bar.style.width = indeterminate ? '45%' : `${task.progress}%`;
    refs.stats.textContent = buildStats(task);
    const downloading = task.status === 'downloading' || task.status === 'merging';
    const isPaused = task.status === 'paused';
    refs.pause.hidden = !downloading;
    refs.resume.hidden = !isPaused;
    refs.cancel.hidden = isPaused;
    refs.root.classList.toggle('is-active', active);
    refs.root.classList.toggle('is-done', task.status === 'completed');
    refs.root.classList.toggle('is-error', task.status === 'failed' || task.status === 'canceled');
    refs.root.classList.toggle('is-paused', isPaused);
    // 已完成任务：折叠过程/进度条/转换等冗余信息，只留标题+核心动作（保存到本机/网盘/删除）
    refs.root.classList.toggle('is-collapsed', task.status === 'completed');

    const failed = task.status === 'failed';
    refs.error.hidden = !failed;
    refs.error.textContent = failed ? [task.error, task.hint].filter(Boolean).join(' — ') : '';

    // 慢速告警横幅：下载中且速率持续过低时展示提示 + 快捷动作
    renderSlowWarning(refs, task);
    // 失败 / 已取消的任务展示「重试」按钮
    refs.retry.hidden = !(task.status === 'failed' || task.status === 'canceled');
    // 「删除任务」按钮：终态时可见（进行中用取消代替删除）
    refs.del.hidden = active;

    // 刷新过程时间线（步骤 + 日志）：进行中/失败时自动展开
    renderTaskSteps(refs, task);

    // 提取文案结果展示（下载/转写中也会显示进度）
    renderExtractedText(refs, task);

    // 任务离开完成态后，必须隐藏完成态专属入口，避免重试/失败后仍显示转换/保存/存网盘
    if (task.status !== 'completed') {
      refs.save.hidden = true;
      refs.saveHint.hidden = true;
      refs.convertWrap.hidden = true;
      refs.cloud.hidden = true;
      return;
    }
    refs.save.hidden = false;
    // 任务在哪个节点跑，文件就从哪个节点取
    refs.save.href = `${refs.base || ''}/api/tasks/${task.task_id}/file`;
    refs.save.setAttribute('download', task.filename || '');
    refs.save.textContent = '保存到本机 ⬇';
    refs.status.textContent = autoSave
      ? '已完成 · 已自动保存到本机'
      : '已完成 · 点「保存到本机」';
    if (autoSave) {
      refs.save.click();
      refs.saveHint.hidden = false;
      refs.saveHint.textContent = '文件已自动保存到你的下载文件夹；若浏览器拦截未出现，请点上方按钮手动保存。';
    } else {
      refs.saveHint.hidden = false;
      refs.saveHint.textContent = '点上方「保存到本机」即可把处理好的视频从服务器下载到你的电脑（浏览器限制，网站无法直接写入你本地）。';
    }

    // 已完成任务展示格式转换入口（增值能力）
    refs.convertWrap.hidden = false;
    if (!refs.convertBtn.dataset.bound) {
      refs.convertBtn.dataset.bound = '1';
      refs.convertBtn.addEventListener('click', () => startConvert(task.task_id, refs));
    }
    if (node.convertSubRequired) updateConvertQuota(refs, null);

    // 下载完成后展示「存到网盘」入口（增值能力）：把文件上传到用户自己的网盘
    refs.cloud.hidden = false;
    if (!refs.cloud.dataset.bound) {
      refs.cloud.dataset.bound = '1';
      refs.cloud.addEventListener('click', () => openCloudModal(task.task_id, refs));
    }
    lastCompletedTask = task.task_id;
    lastCompletedRefs = refs;
  };

  const renderSlowWarning = (refs, task) => {
    // 记录当前任务 url 供横幅内「换清晰度重试」委托处理器读取（全局 resolved 可能已被新链接覆盖）
    refs.root.dataset.srcUrl = task.source_url || '';
    const warn = task.slow_warning;
    const active = ACTIVE_STATES.includes(task.status);
    if (!warn || !active) {
      refs.slowWarn.hidden = true;
      refs.slowWarn.innerHTML = '';
      return;
    }
    const msg = escHtml(warn.message || '下载速度过慢');
    const tips = (Array.isArray(warn.suggestions) ? warn.suggestions : [])
      .map((s) => `<li>${escHtml(s)}</li>`).join('');
    const keys = Array.isArray(warn.suggested_quality_keys) ? warn.suggested_quality_keys : [];
    const retryBtns = keys.map((q) =>
      `<button type="button" class="btn btn-accent btn-sm" data-action="retry-quality" data-quality="${escHtml(q)}">换 ${escHtml(q)}P 重试</button>`
    ).join('');
    refs.slowWarn.innerHTML =
      `<div class="slow-warn-head">⚠️ <span>${msg}</span></div>` +
      (tips ? `<ul class="slow-warn-tips">${tips}</ul>` : '') +
      `<div class="slow-warn-actions">${retryBtns}` +
      `<button type="button" class="btn btn-ghost btn-sm" data-action="focus-proxy">填入代理</button></div>`;
    refs.slowWarn.hidden = false;
  };

  const renderTaskSteps = (refs, task) => {
    const steps = Array.isArray(task.steps) ? task.steps : [];
    const logs = Array.isArray(task.logs) ? task.logs : [];
    const hasSteps = steps.length > 0;
    if (refs.stepsBox) refs.stepsBox.hidden = !hasSteps;
    if (!hasSteps || !refs.stepsList) return;

    const activeStatus = ['pending', 'downloading', 'merging', 'running', 'failed'];
    const autoOpen = activeStatus.includes(task.status);
    if (autoOpen && refs.stepsPanel && refs.stepsPanel.hidden) {
      refs.stepsPanel.hidden = false;
      if (refs.stepsChevron) refs.stepsChevron.textContent = '▼';
      if (refs.stepsToggleLabel) refs.stepsToggleLabel.textContent = '收起过程';
    }

    refs.stepsList.innerHTML = steps.map((s) => {
      const statusClass = s.status === 'running' ? 'task-step--running' :
                          s.status === 'done' ? 'task-step--done' :
                          s.status === 'error' ? 'task-step--error' : 'task-step--pending';
      const icon = s.status === 'running' ? '●' :
                   s.status === 'done' ? '✓' :
                   s.status === 'error' ? '✕' : '○';
      const detail = s.detail ? `<span class="task-step-detail">${escHtml(String(s.detail))}</span>` : '';
      return `<div class="task-step ${statusClass}">
        <span class="task-step-dot">${icon}</span>
        <div class="task-step-body">
          <span class="task-step-name">${escHtml(s.name)}</span>
          ${detail}
        </div>
      </div>`;
    }).join('');

    if (refs.logs) {
      refs.logs.textContent = logs.slice(-30).join('\n');
      const logsWrap = refs.logs.parentElement;
      if (logsWrap && logsWrap.tagName.toLowerCase() === 'details') {
        logsWrap.open = logs.length > 0 && (task.status === 'failed' || logs.length > 3);
      }
    }
  };

  /** 渲染文案提取结果到任务卡片。 */
  const renderExtractedText = (refs, task) => {
    if (!refs.extractWrap || !refs.extractBody) return;
    const mode = task.extract_mode;
    const status = task.extract_status;
    const data = task.extracted_text || {};
    if (!mode) {
      refs.extractWrap.hidden = true;
      return;
    }

    const spoken = data.spoken || {};
    const desc = data.description || {};
    const parts = [];
    if (desc.ok && desc.title) {
      parts.push(`标题：${desc.title}`);
      if (desc.uploader) parts.push(`作者：${desc.uploader}`);
      if (desc.description) parts.push(`\n简介：\n${desc.description}`);
      if (desc.tags && desc.tags.length) parts.push(`\n标签：${desc.tags.join(' / ')}`);
    }
    if (spoken.ok && spoken.text) {
      if (parts.length) parts.push('\n---\n口播文案：\n');
      else parts.push('口播文案：\n');
      parts.push(spoken.text);
    }

    const hasContent = parts.length > 0;
    const hasError = (!spoken.ok && spoken.error) || (!desc.ok && desc.error);
    const running = status === 'running' || (!status && task.status !== 'completed');

    if (running && !hasContent && !hasError) {
      refs.extractWrap.hidden = false;
      refs.extractBody.textContent = '正在提取文案…';
      refs.extractCopy.hidden = true;
      refs.extractRetry.hidden = true;
      return;
    }
    if (hasError && !hasContent) {
      refs.extractWrap.hidden = false;
      const errs = [];
      if (desc.error) errs.push(`发布简介：${desc.error}`);
      if (spoken.error) errs.push(`口播文案：${spoken.error}`);
      refs.extractBody.textContent = '提取失败\n' + errs.join('\n');
      refs.extractCopy.hidden = true;
      refs.extractRetry.hidden = false;
      return;
    }
    if (hasContent) {
      refs.extractWrap.hidden = false;
      if (hasError) {
        const errs = [];
        if (desc.error) errs.push(`发布简介：${desc.error}`);
        if (spoken.error) errs.push(`口播文案：${spoken.error}`);
        parts.push('\n---\n部分提取失败：\n' + errs.join('\n'));
      }
      refs.extractBody.textContent = parts.join('\n');
      refs.extractCopy.hidden = false;
      refs.extractRetry.hidden = task.status !== 'completed';
      return;
    }
    // 已开启但尚无结果，且任务未完成：展示占位
    refs.extractWrap.hidden = task.status === 'completed';
    refs.extractBody.textContent = '暂无提取结果';
    refs.extractCopy.hidden = true;
    refs.extractRetry.hidden = task.status !== 'completed';
  };

  /** 对已完成的任务发起格式转换，轮询直到出片。 */
  const startConvert = async (taskId, refs) => {
    const target = refs.convertTarget.value;
    const resolution = refs.convertRes.value;
    refs.convertBtn.disabled = true;
    refs.convertStatus.textContent = '转换中…';
    const base = refs.base || '';
    try {
      const data = await request(
        '/api/convert',
        { method: 'POST', body: JSON.stringify({ task_id: taskId, target, resolution }) },
        base,
      );
      const jobId = data.job_id;
      updateConvertQuota(refs, data.quota);
      const timer = setInterval(async () => {
        try {
          const st = await request('/api/convert/' + jobId, {}, base);
          if (st.status === 'completed') {
            clearInterval(timer);
            refs.convertFile.href = `${base}/api/convert/${jobId}/file`;
            refs.convertFile.setAttribute('download', st.filename || 'converted');
            refs.convertFile.hidden = false;
            refs.convertStatus.textContent = '转换完成 ✅';
            refs.convertBtn.disabled = false;
          } else if (st.status === 'failed') {
            clearInterval(timer);
            refs.convertStatus.textContent = '转换失败：' + (st.error || '未知错误');
            refs.convertBtn.disabled = false;
          }
        } catch (_e) { /* 轮询中出错则继续 */ }
      }, 3000);
    } catch (error) {
      refs.convertBtn.disabled = false;
      if (error.subscribe) {
        refs.convertStatus.textContent = '今日免费次数已用完，点右上角「订阅解锁」无限使用';
        el.subBadge.hidden = false;
        el.subBadge.classList.add('pulse');
      } else {
        refs.convertStatus.textContent = '转换请求失败：' + (error.message || '');
      }
    }
  };

  // ------------------------------------------------------------------ 下载用途确认弹窗
  // 临时关闭：当前不弹用途确认（2026-08-13）。后期启用 → 把 CONSENT_MODAL_ENABLED 改为 true 即可，弹窗逻辑完好保留。
  const CONSENT_MODAL_ENABLED = false;
  /** 下载前校验用途：未确认过时弹窗询问；选商用则要求确认已获授权。返回是否允许继续。 */
  const ensureConsent = () => new Promise((resolve) => {
    if (!CONSENT_MODAL_ENABLED) return resolve(true);
    const saved = localStorage.getItem('vdl_use');
    if (saved === 'personal' || saved === 'commercial') return resolve(true);
    const dlg = el.consentModal;
    const errEl = el.consentErr;
    const box = el.consentCommercialBox;
    // 缓存错位/旧版 HTML 缺元素时直接放行，避免阻断下载
    if (!dlg || !errEl || !box || typeof dlg.showModal !== 'function') {
      localStorage.setItem('vdl_use', 'personal');
      return resolve(true);
    }
    const radio = () => dlg.querySelector('input[name="consentUse"]:checked')?.value || 'personal';
    errEl.hidden = true;
    box.hidden = true;

    const onUseChange = () => {
      const commercial = radio() === 'commercial';
      box.hidden = !commercial;
      if (!commercial) { el.consentAuthorized.checked = false; errEl.hidden = true; }
    };
    const cleanup = () => {
      el.consentConfirm.removeEventListener('click', onConfirm);
      el.consentCancel.removeEventListener('click', onCancel);
      el.consentClose.removeEventListener('click', onCancel);
      dlg.removeEventListener('click', onBackdrop);
      dlg.removeEventListener('close', onClose);
      dlg.querySelector('input[name="consentUse"][value="personal"]').checked = true;
      el.consentAuthorized.checked = false;
      box.hidden = true;
      errEl.hidden = true;
    };
    const onBackdrop = (e) => { if (e.target === dlg) dlg.close(); };
    const onClose = () => { cleanup(); resolve(false); };
    const onCancel = () => { dlg.close(); };
    const onConfirm = () => {
      const use = radio();
      if (use === 'commercial' && !el.consentAuthorized.checked) {
        errEl.hidden = false;
        return;
      }
      localStorage.setItem('vdl_use', use);
      dlg.close();
      resolve(true);
    };
    el.consentConfirm.addEventListener('click', onConfirm);
    el.consentCancel.addEventListener('click', onCancel);
    el.consentClose.addEventListener('click', onCancel);
    dlg.addEventListener('click', onBackdrop);
    dlg.addEventListener('close', onClose);
    dlg.querySelectorAll('input[name="consentUse"]').forEach((r) => r.addEventListener('change', onUseChange));
    dlg.showModal();
  });

  /** 直接创建下载任务（不解析、用默认 best 画质），供批量模式复用。 */
  const enqueueDownload = async (url, { cookie = '', proxy = '', base = '' } = {}) => {
    try {
      const data = await request(
        '/api/download',
        { method: 'POST', body: JSON.stringify({ url, quality: 'best', cookie, proxy }) },
        base,
      );
      if (data.quota) {
        node.downloadFreeUsed = data.quota.free_used || 0;
        if (node.downloadSubRequired) refreshSubModalText();
      }
      const refs = createTaskCard(data.task_id, { title: url, platform: '' });
      refs.base = base;
      trackTask(data.task_id, refs, base);
      return data.task_id;
    } catch (error) {
      if (error.subscribe) {
        promptSubscribe();
        return { subscribe: true };
      }
      console.warn('批量任务创建失败:', url, error);
      return null;
    }
  };

  /** 批量下载：逐条创建任务并进入列表。 */
  const runBatch = async (urls, cookie, proxy) => {
    if (!(await ensureConsent())) return;
    clearError();
    el.resultPanel.hidden = true;
    el.batchBtn.disabled = true;
    const origLabel = el.batchBtn.textContent;
    el.batchBtn.textContent = '提交中…';
    const quality = el.batchQuality.value || 'best';
    const concurrency = parseInt(el.batchConcurrency.value, 10) || 3;
    try {
      const data = await request('/api/batch', {
        method: 'POST',
        body: JSON.stringify({ urls, quality, cookie, proxy, concurrency }),
      });
      el.alert.hidden = true;
      data.task_ids.forEach((tid) => {
        const refs = createTaskCard(tid, { title: '解析中…', platform: '' });
        trackTask(tid, refs, '');
      });
    } catch (error) {
      if (error.subscribe) {
        promptSubscribe();
        showError('今日免费下载次数已用完', '点右上角「订阅解锁」即可无限下载');
      } else {
        showError(error.message || '批量提交失败', error.hint);
      }
    } finally {
      el.batchBtn.disabled = false;
      el.batchBtn.textContent = origLabel;
    }
  };

  /** 用 SSE 跟踪进度，浏览器不支持或连接断开时回退到轮询。 */
  const trackTask = (taskId, refs, base = '') => {
    let finished = false;

    const finish = (task) => {
      finished = true;
      paintTask(refs, task, task.status === 'completed');
      const tracker = trackers.get(taskId);
      tracker?.source?.close();
      clearInterval(tracker?.timer);
      trackers.delete(taskId);
    };

    const handle = (task) => {
      if (finished) return;
      if (['completed', 'failed', 'canceled'].includes(task.status)) finish(task);
      else paintTask(refs, task, false);
    };

    const poll = setInterval(async () => {
      if (finished) return;
      try {
        handle(await request(`/api/tasks/${taskId}`, {}, base));
      } catch {
        /* 静默重试，SSE 或下一轮轮询会补上 */
      }
    }, POLL_FALLBACK_MS);

    const source = new EventSource(`${base}/api/tasks/${taskId}/events`);
    source.onmessage = (event) => handle(JSON.parse(event.data));
    source.onerror = () => source.close();

    trackers.set(taskId, { source, timer: poll });
  };

  // ------------------------------------------------------------------ 动作

  /** 平台 key → 中文名，用于登录态提示。 */
  const platCN = (k) => ({
    tencent: '腾讯视频', douyin: '抖音', kuaishou: '快手',
    xiaohongshu: '小红书', bilibili: 'B站', youtube: 'YouTube',
  }[k] || '该平台');

  /** 调用 /api/cookie/status 检测本机浏览器是否含目标站登录态，并刷新状态徽章。
   *  命中则告知「已自动读取，无需手动粘贴」；未命中则提示先登录或手动粘贴。 */
  const updateCookieStatus = async (url) => {
    const btn = el.cookieDetectBtn;
    const span = el.cookieStatus;
    if (!url) { span.textContent = ''; span.className = 'cookie-status'; return; }
    btn.disabled = true;
    span.textContent = '检测中…';
    span.className = 'cookie-status loading';
    try {
      const data = await request(`/api/cookie/status?url=${encodeURIComponent(url)}`);
      const name = platCN(data.platform);
      if (data.available) {
        const where = data.profile && data.profile !== 'Default'
          ? `${data.browser}（${data.profile}）` : data.browser;
        span.textContent = `✅ 已自动读取 ${where} 的${name}登录态，无需手动粘贴`;
        span.className = 'cookie-status ok';
      } else if (data.browser) {
        span.textContent = `⚠️ 未在 ${data.browser} 中检测到${name}登录态，如需更快速度/会员内容请先在浏览器登录，或手动粘贴 Cookie`;
        span.className = 'cookie-status warn';
      } else {
        span.textContent = '⚠️ 未检测到浏览器登录态，部分平台需手动粘贴 Cookie 后才能下载';
        span.className = 'cookie-status warn';
      }
    } catch (_) {
      span.textContent = '检测失败，可直接手动粘贴 Cookie';
      span.className = 'cookie-status err';
    } finally {
      btn.disabled = false;
    }
  };

  const handleResolve = async (event) => {
    event.preventDefault();
    const url = el.input.value.trim();
    const cookie = el.cookieInput.value.trim();
    const proxy = el.proxyInput.value.trim();
    if (!url) {
      showError('请输入视频链接', '把视频页面的地址粘贴到输入框即可');
      return;
    }
    // 批量：检测到多个链接时直接进入批量下载，跳过单链接解析面板
    const urls = url.split(/\s+/).map((s) => s.trim()).filter(Boolean);
    if (urls.length > 1) {
      await runBatch(urls, cookie, proxy);
      return;
    }
    clearError();
    setLoading(true);
    el.resultPanel.hidden = true;
    const base = baseFor(url);
    try {
      resolved = await request('/api/resolve', { method: 'POST', body: JSON.stringify({ url, cookie, proxy }) }, base);
      resolved.cookie = cookie;
      resolved.proxy = proxy;
      resolved.base = base;                        // 后续下载/进度/取件都锁定同一节点
      renderVideo(resolved);
      updateCookieStatus(url);   // 解析成功后自动检测浏览器登录态，省去手动粘贴
    } catch (error) {
      resolved = null;
      showError(error.message || '解析失败', error.hint);
    } finally {
      setLoading(false);
    }
  };

  /** 用指定清晰度发起一个下载任务；返回 taskId 或 null。被"开始下载"与"转 MP3"复用。 */
  const startDownload = async (quality, opts = {}) => {
    const url = opts.url || resolved?.url;
    if (!url) return null;
    if (!(await ensureConsent())) return null;
    clearError();
    const base = resolved?.base || '';
    try {
      const data = await request('/api/download', {
        method: 'POST',
        body: JSON.stringify({
          url,
          quality,
          cookie: opts.cookie ?? resolved?.cookie ?? '',
          proxy: opts.proxy ?? resolved?.proxy ?? '',
          extract_script: el.extractSelect.value || '',
        }),
      }, base);
      const taskId = data.task_id;
      if (data.quota) {
        node.downloadFreeUsed = data.quota.free_used || 0;
        if (node.downloadSubRequired) refreshSubModalText();
      }
      const refs = createTaskCard(taskId, {
        title: resolved.video.title,
        platform: resolved.platform.name,
      });
      refs.base = base;
      trackTask(taskId, refs, base);
      return taskId;
    } catch (error) {
      if (error.subscribe) {
        promptSubscribe();
        showError('今日免费下载次数已用完', '点右上角「订阅解锁」后即可无限下载');
      } else {
        showError(error.message || '创建下载任务失败', error.hint);
      }
      return null;
    }
  };

  const triggerDirectDownload = (url, title) => {
    const a = document.createElement('a');
    a.href = url;
    if (title) a.download = title;
    document.body.appendChild(a);
    a.click();
    a.remove();
    el.directHint.textContent = '⬇ 已开始从源站下载，请查看浏览器下载栏（文件不经过我们的服务器）。若源站拒绝直连，请用上方「改用服务器下载」。';
  };

  const handleDownload = () => {
    if (resolved?.video?.direct_url) {
      triggerDirectDownload(resolved.video.direct_url, resolved.video.title);
      return;
    }
    startDownload(selectedQuality);
  };

  const cancelTask = async (taskId, base = '') => {
    try {
      await request(`/api/tasks/${taskId}`, { method: 'DELETE' }, base);
    } catch (error) {
      showError(error.message || '取消失败', error.hint);
    }
  };

  const pauseTask = async (taskId, base = '') => {
    try {
      await request(`/api/tasks/${taskId}/pause`, { method: 'POST' }, base);
    } catch (error) {
      showError(error.message || '暂停失败', error.hint);
    }
  };

  const resumeTask = async (taskId, base = '') => {
    try {
      await request(`/api/tasks/${taskId}/resume`, { method: 'POST' }, base);
    } catch (error) {
      showError(error.message || '继续失败', error.hint);
    }
  };

  // 任务重试：失败 / 已取消的任务重新加入下载队列
  const retryTask = async (taskId, refs) => {
    try {
      await request(`/api/tasks/${taskId}/retry`, { method: 'POST' }, refs.base || '');
      refs.retry.hidden = true;
      refs.error.hidden = true;
      trackTask(taskId, refs, refs.base || '');  // 重新跟踪（原 tracker 已因终态移除）
    } catch (error) {
      showError(error.message || '重试失败', error.hint);
    }
  };

  // 删除单条任务记录（已完成/失败/已取消都能删）
  const deleteTask = async (taskId, refs) => {
    const isFinished = refs.status?.dataset?.state
      ? !['pending','downloading','processing','resolving'].includes(refs.status.dataset.state)
      : true;
    const fileNote = isFinished
      ? '\n文件也会被一并删除（回收站优先，无法回收时直接清理）'
      : '\n任务将被取消';
    if (!window.confirm(`确定删除这条任务记录吗？${fileNote}`)) return;
    try {
      await request(`/api/tasks/${taskId}`, { method: 'DELETE' });
      refs.root.remove();  // 从 DOM 移除
    } catch (error) {
      showError(error.message || '删除失败', error.hint);
    }
  };

  // 在 Finder / 资源管理器中打开下载目录（仅桌面版可用）
  const openDownloadFolder = async () => {
    try {
      await request('/api/fs/open', { method: 'POST', body: JSON.stringify({}) });
    } catch (error) {
      showError(error.message || '打开下载目录失败', error.hint);
    }
  };

  // 队列概览：轮询任务统计，刷新进度条与「全部取消」可见性
  const loadQueue = async () => {
    if (el.downloadView.hidden) return;  // 仅下载视图可见时轮询，省请求
    try {
      const data = await request('/api/tasks');
      paintQueue(data.stats);
      syncMissingCards(data.tasks);
    } catch { /* 瞬时错误忽略，下一轮补上 */ }
  };

  const paintQueue = (stats) => {
    const active = (stats.active != null) ? stats.active : (stats.downloading + stats.merging);
    const parts = [];
    if (active) parts.push(`进行中 ${active}`);
    if (stats.pending) parts.push(`排队 ${stats.pending}`);
    if (stats.completed) parts.push(`完成 ${stats.completed}`);
    if (stats.failed) parts.push(`失败 ${stats.failed}`);
    if (stats.canceled) parts.push(`已取消 ${stats.canceled}`);
    el.queueBar.textContent = parts.length ? parts.join(' · ') : '暂无任务';
    el.cancelAllBtn.hidden = (active + stats.pending) === 0;
  };

  // 刷新后 / 跨标签页补齐「活跃任务」卡片（避免任务在跑但列表空）
  const syncMissingCards = (tasks) => {
    tasks.forEach((t) => {
      if (!ACTIVE_STATES.includes(t.status)) return;
      if (el.taskList.querySelector(`[data-task-id="${t.task_id}"]`)) return;
      const refs = createTaskCard(t.task_id, { title: t.title, platform: t.platform });
      trackTask(t.task_id, refs, '');
    });
  };

  // 全部取消：取消所有进行中 / 排队的任务（不删已完成文件）
  const cancelAll = async () => {
    if (!window.confirm('确定取消所有进行中 / 排队的下载任务吗？已完成的文件不会删除。')) return;
    try {
      await request('/api/tasks/cancel-all', { method: 'POST' });
      loadQueue();
    } catch (error) {
      showError(error.message || '取消失败', error.hint);
    }
  };

  // ------------------------------------------------------------------ 自动解说（增值功能）
  // 下载完成的任务 → 点「生成解说成片」→ 后台先 script-only 生成脚本 → 前端展示人工审核面板
  // → 用户确认后调 commentary-pipeline/process.py --edit-only 渲染成片。
  // 解说算力由独立 worker 承担，UI 只负责触发与轮询，不感知具体渲染过程。

  // 通用轮询：拿到 job_id 后定时查状态，更新 refs（commentary 按钮 / status / file 链接）。
  const pollCommentaryJob = (job_id, refs, base = '', onCompleted = null) => {
    refs.commentaryStatus.hidden = false;
    refs.commentaryStatus.textContent = '正在生成解说成片，长视频可能需数分钟…';
    let shownProgress = 0;  // 已显示过的进度行数，避免重复追加
    const poll = setInterval(async () => {
      try {
        const st = await request(`/api/commentary/${job_id}`, {}, base);
        // 优先用后端返回的结构化步骤时间线
        const hasSteps = Array.isArray(st.steps) && st.steps.length > 0;
        if (hasSteps) {
          renderComSteps(st);
        }
        if (st.status === 'completed') {
          clearInterval(poll);
          refs.commentaryStatus.textContent = '解说成片已生成';
          refs.commentaryFile.href = `${base}/api/commentary/${job_id}/file`;
          refs.commentaryFile.setAttribute('download', '解说成片.mp4');
          refs.commentaryFile.hidden = false;
          if (refs.commentary) refs.commentary.hidden = true;
          el.comProgress.hidden = true;
          el.comEta.hidden = true;
          if (typeof onCompleted === 'function') onCompleted();
        } else if (st.status === 'failed') {
          clearInterval(poll);
          refs.commentaryStatus.textContent = `生成失败：${st.error || '未知错误'}`;
          if (refs.commentary) {
            refs.commentary.disabled = false;
            refs.commentary.textContent = '重试生成解说';
          }
          el.comProgress.hidden = true;
          el.comEta.hidden = true;
        } else if (st.status === 'running') {
          // 实时把进程最新输出追加到状态文本（兼容无 steps 的旧后端）
          if (!hasSteps) {
            const progress = Array.isArray(st.progress) ? st.progress : [];
            if (progress.length > shownProgress) {
              const newLines = progress.slice(shownProgress).join('\n');
              shownProgress = progress.length;
              const prev = refs.commentaryStatus.textContent || '';
              refs.commentaryStatus.textContent = (prev ? prev + '\n' : '') + newLines;
            }
          }
          // 进度条：优先从 steps 计算，无 steps 时退回到日志解析
          el.comProgress.hidden = false;
          const phasePct = _deriveComPhasePct(st);
          el.comPhase.textContent = phasePct.phase;
          el.comPercent.textContent = phasePct.pct + '%';
          el.comBarFill.style.width = phasePct.pct + '%';
          if (phasePct.pct >= 100) el.comBarFill.style.background = 'var(--success)';
          // ETA：预计完成时间 + 剩余时间
          if (st.eta_done_at) {
            el.comEta.hidden = false;
            el.comEta.textContent = `预计完成 ${formatClock(st.eta_done_at)} · ${formatEta(st.eta_remaining)}`;
          } else {
            el.comEta.hidden = true;
          }
        }
      } catch {
        /* 静默重试，下一轮轮询补上 */
      }
    }, 2500);
  };

  const _deriveComPhasePct = (st) => {
    const steps = Array.isArray(st.steps) ? st.steps : [];
    if (steps.length > 0) {
      const running = steps.find((s) => s.status === 'running');
      const doneCount = steps.filter((s) => s.status === 'done').length;
      const phase = running ? running.name : (doneCount >= steps.length - 1 ? '完成' : '等待中');
      const pct = Math.round((doneCount / Math.max(steps.length - 1, 1)) * 100);
      return { phase, pct: Math.min(pct, 99) };
    }
    // fallback：按日志文本推导
    let phase = '处理中', pct = 0;
    const all = (st.progress || []).join('\n');
    if (/===.*(?:转写|transcribe)/i.test(all)) phase = '转写中';
    else if (/自动解说词|解说词草稿|LLM.*生成/i.test(all)) phase = '生成解说词';
    else if (/开始批量生成旁白|旁白生成/i.test(all)) phase = '生成旁白中';
    else if (/开始并行渲染|✓\s*\[/i.test(all)) phase = '渲染中';
    else if (/拼接成片/i.test(all)) phase = '拼接中';
    else if (/✅|🎬.*全部完成/i.test(all)) phase = '完成';
    const match = all.match(/✓\s*\[(\d+)\s*\/\s*(\d+)\]/g);
    if (match) {
      const last = match[match.length - 1];
      const m2 = last.match(/(\d+)\s*\/\s*(\d+)/);
      if (m2) pct = Math.round((+m2[1] / +m2[2]) * 100);
    } else if (/转写完成/.test(all)) pct = 30;
    else if (/旁白生成完成/.test(all)) {
      const ppm = all.match(/旁白生成完成\s*[（(]\s*(\d+)\s*\/\s*(\d+)/);
      if (ppm) pct = Math.round((+ppm[1] / +ppm[2]) * 30 + 35);
      else pct = 50;
    } else if (/拼接成片/.test(all)) pct = 95;
    else if (/✅|🎬/.test(all)) pct = 100;
    return { phase, pct };
  };

  const renderComSteps = (st) => {
    const steps = Array.isArray(st.steps) ? st.steps : [];
    const logs = Array.isArray(st.logs) ? st.logs : [];
    if (steps.length === 0) {
      el.comStepsPanel.hidden = true;
      return;
    }
    el.comStepsPanel.hidden = false;
    el.comStepsList.innerHTML = steps.map((s) => {
      const statusClass = s.status === 'running' ? 'task-step--running' :
                          s.status === 'done' ? 'task-step--done' :
                          s.status === 'error' ? 'task-step--error' : 'task-step--pending';
      const icon = s.status === 'running' ? '●' :
                   s.status === 'done' ? '✓' :
                   s.status === 'error' ? '✕' : '○';
      const detail = s.detail ? `<span class="task-step-detail">${escHtml(String(s.detail))}</span>` : '';
      return `<div class="task-step ${statusClass}">
        <span class="task-step-dot">${icon}</span>
        <div class="task-step-body">
          <span class="task-step-name">${escHtml(s.name)}</span>
          ${detail}
        </div>
      </div>`;
    }).join('');
    el.comLogs.textContent = logs.slice(-30).join('\n');
    const logsWrap = el.comLogs.parentElement;
    if (logsWrap && logsWrap.tagName.toLowerCase() === 'details') {
      logsWrap.open = logs.length > 0 && (st.status === 'failed' || logs.length > 3);
    }
  };

  // source: { taskId }（下载完成的任务）或 { fileId }（媒体库里的现成视频）
  // 读取当前选中的剪辑选项（解说类型 / 高光来源 / 开关 / 保留时长 / 一键生成）
  const comGetOptions = (forceOneClick = false) => {
    const typeEl = document.querySelector('input[name="comType"]:checked');
    const srcEl = document.querySelector('input[name="comHlSource"]:checked');
    const styleEl = document.querySelector('input[name="comStyle"]:checked');
    const rp = el.comRetainPct && el.comRetainPct.value ? Number(el.comRetainPct.value) : null;
    return {
      commentary_type: typeEl ? typeEl.value : 'deep_hl',
      highlight_source: srcEl ? srcEl.value : 'ai',
      intro_highlight: !!(el.comIntroHighlight && el.comIntroHighlight.checked),
      skip_intro_outro: !!(el.comSkipIntroOutro && el.comSkipIntroOutro.checked),
      // 默认保留片头片尾·不解说；若勾选「去片头片尾」则以 skip 优先（互斥，后端处理）
      no_narrate_intro_outro: !!(el.comKeepNoNarrate && el.comKeepNoNarrate.checked),
      retain_pct: rp,
      one_click: !!forceOneClick,
      style: styleEl ? styleEl.value : 'none',
    };
  };

  /** 画幅选择：auto（跟视频走，默认）/ landscape（横屏）/ vertical（竖屏 9:16）。 */
  const comGetAspect = () => {
    const aspectEl = document.querySelector('input[name="comAspect"]:checked');
    return aspectEl ? aspectEl.value : 'auto';
  };

  /** 把画幅选择解析成 vertical 布尔值。auto 时用已加载的视频宽高判断（竖屏素材→竖屏）。 */
  const resolveVertical = () => {
    const aspect = comGetAspect();
    if (aspect === 'vertical') return true;
    if (aspect === 'landscape') return false;
    // auto：优先用已加载的预览宽高；拿不到（媒体库直出未加载预览）就按横屏兜底。
    if (comPreviewW > 0 && comPreviewH > 0) return comPreviewH > comPreviewW;
    return false;
  };

  /** 根据按钮 id 返回初始文案，错误恢复时使用。 */
  const comButtonOriginalText = (btn) => {
    if (!btn) return '';
    if (btn.id === 'libCommentary') return '生成解说成片';
    return btn.dataset.originalText || '生成';
  };

  /** 统一的「生成解说」入口：统一先走 script-only，打开人工审核面板，
   *  用户确认后再点击「生成成片」。避免直接渲染导致无法修改。
   *  从媒体库调用时自动切到解说标签页。 */
  const createCommentary = async (source, refs, base = '', oneClick = false) => {
    switchView('commentary');
    if (refs.commentary) {
      refs.commentary.disabled = true;
      refs.commentary.textContent = '生成脚本中…';
    }
    el.comStatus.hidden = false;
    el.comStatus.textContent = '正在生成解说词，生成后可在下方审核修改…';
    try {
      const opts = comGetOptions(oneClick);
      const body = source.taskId
        ? { task_id: source.taskId, vertical: resolveVertical(), trim_start: comTrimStart, trim_end: comTrimEnd, ...opts }
        : { file_id: source.fileId, vertical: resolveVertical(), trim_start: comTrimStart, trim_end: comTrimEnd, ...opts };
      const { job_id } = await request('/api/commentary/script-only', {
        method: 'POST',
        body: JSON.stringify(body),
      }, base);
      currentScriptJobId = job_id;
      el.comGenerateScript.disabled = true;
      el.comGenerateScript.textContent = '正在转写+生成解说词…';
      el.comScriptPanel.hidden = true;
      el.comReviewActions.hidden = true;
      pollScriptJob(job_id);
    } catch (err) {
      el.comStatus.hidden = false;
      el.comStatus.textContent = `无法开始：${err.message || '请稍后重试'}`;
      if (refs.commentary) {
        refs.commentary.disabled = false;
        refs.commentary.textContent = comButtonOriginalText(refs.commentary);
      }
      el.comGenerateScript.disabled = false;
      el.comGenerateScript.textContent = '生成脚本（可审核修改）';
    }
  };

  const createCommentaryFromFile = async (file, refs, oneClick = false) => {
    switchView('commentary');
    if (refs.commentary) {
      refs.commentary.disabled = true;
      refs.commentary.textContent = '生成脚本中…';
    }
    el.comStatus.hidden = false;
    el.comStatus.textContent = '正在上传视频并生成解说词，生成后可在下方审核修改…';
    try {
      const form = new FormData();
      form.append('file', file);
      form.append('vertical', String(resolveVertical()));
      form.append('trim_start', String(comTrimStart));
      form.append('trim_end', String(comTrimEnd));
      const opts = comGetOptions(oneClick);
      form.append('commentary_type', opts.commentary_type);
      form.append('highlight_source', opts.highlight_source);
      form.append('intro_highlight', String(opts.intro_highlight));
      form.append('skip_intro_outro', String(opts.skip_intro_outro));
      form.append('no_narrate_intro_outro', String(opts.no_narrate_intro_outro));
      if (opts.retain_pct != null) form.append('retain_pct', String(opts.retain_pct));
      form.append('one_click', String(opts.one_click));
      form.append('style', opts.style || 'none');
      const { job_id } = await request('/api/commentary/script-only/upload', { method: 'POST', body: form });
      currentScriptJobId = job_id;
      el.comGenerateScript.disabled = true;
      el.comGenerateScript.textContent = '正在上传+生成解说词…';
      el.comScriptPanel.hidden = true;
      el.comReviewActions.hidden = true;
      pollScriptJob(job_id);
    } catch (err) {
      el.comStatus.hidden = false;
      el.comStatus.textContent = `无法开始：${err.message || '请稍后重试'}`;
      if (refs.commentary) {
        refs.commentary.disabled = false;
        refs.commentary.textContent = comButtonOriginalText(refs.commentary);
      }
      el.comGenerateScript.disabled = false;
      el.comGenerateScript.textContent = '生成脚本（可审核修改）';
    }
  };

  // ---- 脚本审核模式 ----

  /** 从媒体库创建脚本-only 任务 */
  const createScriptOnly = async (source) => {
    el.comGenerateScript.disabled = true;
    el.comGenerateScript.textContent = '正在转写+生成解说词…';
    el.comScriptPanel.hidden = true;
    el.comReviewActions.hidden = true;
    el.comScriptSegments.replaceChildren();
    el.comStatus.hidden = false;
    el.comStatus.textContent = '正在转写并生成AI解说词（不渲染成片），长视频可能需数分钟…';
    try {
      const opts = comGetOptions();
      const body = source.taskId
        ? { task_id: source.taskId, vertical: resolveVertical(), trim_start: comTrimStart, trim_end: comTrimEnd, ...opts }
        : { file_id: source.fileId, vertical: resolveVertical(), trim_start: comTrimStart, trim_end: comTrimEnd, ...opts };
      const { job_id } = await request('/api/commentary/script-only', {
        method: 'POST', body: JSON.stringify(body),
      });
      currentScriptJobId = job_id;
      pollScriptJob(job_id);
    } catch (err) {
      el.comStatus.textContent = `无法开始：${err.message || '请稍后重试'}`;
      el.comGenerateScript.disabled = false;
      el.comGenerateScript.textContent = '生成脚本（可审核修改）';
    }
  };

  /** 从本地文件创建脚本-only 任务 */
  const createScriptOnlyFromFile = async (file) => {
    el.comGenerateScript.disabled = true;
    el.comGenerateScript.textContent = '正在上传+生成解说词…';
    el.comScriptPanel.hidden = true;
    el.comReviewActions.hidden = true;
    el.comStatus.hidden = false;
    el.comStatus.textContent = '正在上传视频并生成AI解说词（不渲染成片）…';
    try {
      const form = new FormData();
      form.append('file', file);
      form.append('vertical', String(resolveVertical()));
      form.append('trim_start', String(comTrimStart));
      form.append('trim_end', String(comTrimEnd));
      const opts = comGetOptions();
      form.append('commentary_type', opts.commentary_type);
      form.append('highlight_source', opts.highlight_source);
      form.append('intro_highlight', String(opts.intro_highlight));
      form.append('skip_intro_outro', String(opts.skip_intro_outro));
      form.append('no_narrate_intro_outro', String(opts.no_narrate_intro_outro));
      if (opts.retain_pct != null) form.append('retain_pct', String(opts.retain_pct));
      form.append('one_click', String(opts.one_click));
      form.append('style', opts.style || 'none');
      const { job_id } = await request('/api/commentary/script-only/upload', { method: 'POST', body: form });
      currentScriptJobId = job_id;
      pollScriptJob(job_id);
    } catch (err) {
      el.comStatus.textContent = `无法开始：${err.message || '请稍后重试'}`;
      el.comGenerateScript.disabled = false;
      el.comGenerateScript.textContent = '生成脚本（可审核修改）';
    }
  };

  /** 轮询脚本-only 任务，拿到 script.json 后载入编辑面板 */
  const pollScriptJob = (job_id) => {
    el.comProgress.hidden = false;
    el.comPhase.textContent = '转写+生成解说词';
    el.comPercent.textContent = '...';
    el.comEmpty.hidden = true;
    currentScriptJobId = job_id;
    let shownProgress = 0;
    const poll = setInterval(async () => {
      try {
        const st = await request(`/api/commentary/${job_id}`);
        const hasSteps = Array.isArray(st.steps) && st.steps.length > 0;
        if (hasSteps) {
          renderComSteps(st);
        }
        if (st.status === 'script_ready') {
          clearInterval(poll);
          el.comProgress.hidden = true;
          el.comStatus.textContent = 'AI 解说词已生成，请审核修改后生成成片';
          el.comReviewActions.hidden = false;
          openScriptReview(job_id, { autoScroll: true });
        } else if (st.status === 'failed') {
          clearInterval(poll);
          el.comProgress.hidden = true;
          el.comScriptPanel.hidden = true;
          el.comReviewActions.hidden = true;
          el.comStatus.textContent = `生成失败：${st.error || '未知错误'}`;
          el.comGenerateScript.disabled = false;
          el.comGenerateScript.textContent = '重试生成脚本';
        } else if (st.status === 'running') {
          if (!hasSteps) {
            const progress = Array.isArray(st.progress) ? st.progress : [];
            if (progress.length > shownProgress) {
              shownProgress = progress.length;
            }
            const all = progress.join('\n');
            let phase = '处理中', pct = 0;
            if (/===.*(?:转写|transcribe)/i.test(all)) { phase = '转写中'; pct = 10; }
            else if (/转写完成/.test(all)) { phase = '生成解说词中'; pct = 35; }
            else if (/自动解说词|解说词草稿|LLM/.test(all)) { phase = 'AI 生成解说词中'; pct = 60; }
            else if (/脚本已生成|✅/.test(all)) { phase = '脚本就绪'; pct = 100; }
            el.comPhase.textContent = phase;
            el.comPercent.textContent = pct + '%';
            el.comBarFill.style.width = pct + '%';
          } else {
            const phasePct = _deriveComPhasePct(st);
            el.comPhase.textContent = phasePct.phase;
            el.comPercent.textContent = phasePct.pct + '%';
            el.comBarFill.style.width = phasePct.pct + '%';
          }
          if (st.eta_done_at) {
            el.comEta.hidden = false;
            el.comEta.textContent = `预计完成 ${formatClock(st.eta_done_at)} · ${formatEta(st.eta_remaining)}`;
          } else {
            el.comEta.hidden = true;
          }
        }
      } catch {
        /* 静默重试 */
      }
    }, 2500);
  };

  /** 打开脚本审核面板：先展示面板（带加载态），再异步拉取脚本内容。
   *  即使拉取失败也保留面板可见，并给出重试按钮，避免用户看不到任何反馈。 */
  const openScriptReview = (job_id, opts = {}) => {
    el.comScriptPanel.hidden = false;
    el.comEmpty.hidden = true;
    el.comScriptSegments.replaceChildren();
    el.comScriptStatus.hidden = false;
    el.comScriptStatus.className = 'com-script-status';
    el.comScriptStatus.textContent = '正在加载解说词…';
    el.comScriptSave.disabled = true;
    el.comScriptRender.disabled = true;
    if (opts.autoScroll) {
      el.comScriptPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
    loadScriptToPanel(job_id);
  };

  /** 加载脚本到编辑面板 */
  const loadScriptToPanel = async (job_id) => {
    try {
      const data = await request(`/api/commentary/script/${job_id}`);
      el.comScriptPanel.hidden = false;
      el.comScriptSegments.replaceChildren();

      // 初始化全局配音选择器（默认选中当前风格联动的音色）
      el.comScriptVoice.replaceChildren();
      const linkedVoice = STYLE_VOICE[comCurrentStyle()] || 'zh-CN-XiaoxiaoNeural';
      COM_VOICES.forEach((v) => {
        const o = document.createElement('option');
        o.value = v.value;
        o.textContent = v.label;
        if (v.value === linkedVoice) o.selected = true;
        el.comScriptVoice.appendChild(o);
      });

      // 逐段渲染可编辑行
      const segs = data.segments || [];
      currentScriptSegments = segs;  // 保留原始时间戳+note，供 saveScript 合并
      segs.forEach((seg, idx) => {
        const row = document.createElement('div');
        row.className = 'com-seg-row';
        const dur = `${fmtTs(seg.start)} – ${fmtTs(seg.end)}`;
        row.innerHTML = `<div class="com-seg-meta">
          <span class="com-seg-idx">#${idx + 1}</span>
          <span class="com-seg-time">${dur} (${(seg.end - seg.start).toFixed(1)}s)</span>
          ${seg.note ? `<span class="com-seg-note">${escHtml(seg.note)}</span>` : ''}
        </div>
        <textarea class="adv-input com-seg-text" data-idx="${idx}" rows="3">${escHtml(seg.narration || '')}</textarea>`;
        el.comScriptSegments.appendChild(row);
      });

      el.comScriptStatus.hidden = true;
      el.comScriptSave.disabled = false;
      el.comScriptRender.disabled = false;
      currentScriptJobId = job_id; // 兜底：面板打开时确保全局 job_id 与显示内容一致
      el.comGenerateScript.textContent = '重新生成脚本';
      el.comGenerateScript.disabled = false;

      // 滚动到面板
      el.comScriptPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (err) {
      el.comScriptPanel.hidden = false;
      el.comScriptSegments.innerHTML = `<div class="com-seg-row">
        <p class="com-script-status com-script-err">加载脚本失败：${escHtml(err.message || '未知错误')}</p>
        <button type="button" class="btn btn-sm btn-secondary" id="comScriptRetryLoad">重新加载</button>
      </div>`;
      const retryBtn = document.getElementById('comScriptRetryLoad');
      if (retryBtn) {
        retryBtn.addEventListener('click', () => loadScriptToPanel(job_id));
      }
      el.comScriptStatus.hidden = true;
      el.comGenerateScript.disabled = false;
      el.comGenerateScript.textContent = '重试生成脚本';
    }
  };

  /** 保存人工修改后的脚本回写 server，保留原始时间戳和 note。 */
  const saveScript = async () => {
    if (!currentScriptJobId) return;
    const orig = currentScriptSegments || [];
    const segments = [];
    const rows = el.comScriptSegments.querySelectorAll('.com-seg-row');
    rows.forEach((row) => {
      const ta = row.querySelector('.com-seg-text');
      if (!ta) return;
      const idx = parseInt(ta.dataset.idx, 10);
      const narration = ta.value.trim();
      if (!narration) return;
      const oseg = (idx >= 0 && idx < orig.length) ? orig[idx] : null;
      segments.push({
        start: oseg ? oseg.start : 0,
        end: oseg ? oseg.end : 0,
        narration,
        note: oseg ? (oseg.note || '') : '',
      });
    });
    if (segments.length === 0) {
      el.comScriptStatus.hidden = false;
      el.comScriptStatus.className = 'com-script-status com-script-err';
      el.comScriptStatus.textContent = '至少保留一段解说词';
      return;
    }
    el.comScriptStatus.hidden = false;
    el.comScriptStatus.className = 'com-script-status';
    el.comScriptStatus.textContent = '保存中…';
    try {
      await request(`/api/commentary/script/${currentScriptJobId}`, {
        method: 'PUT',
        body: JSON.stringify({
          segments,
          voice: el.comScriptVoice.value,
        }),
      });
      el.comScriptStatus.textContent = '已保存 ✓';
      el.comScriptStatus.className = 'com-script-status com-script-ok';
      setTimeout(() => { el.comScriptStatus.hidden = true; }, 2000);
    } catch (err) {
      el.comScriptStatus.textContent = `保存失败：${err.message}`;
      el.comScriptStatus.className = 'com-script-status com-script-err';
    }
  };

  /** 用已审核脚本渲染成片 */
  const renderFromScript = async () => {
    if (!currentScriptJobId) {
      el.comScriptStatus.hidden = false;
      el.comScriptStatus.className = 'com-script-status com-script-err';
      el.comScriptStatus.textContent = '未找到当前任务，请重新生成脚本后再试';
      return;
    }
    el.comScriptRender.disabled = true;
    el.comScriptRender.textContent = '渲染中…';
    el.comScriptSave.disabled = true;
    el.comProgress.hidden = false;
    el.comPhase.textContent = '渲染成片';
    el.comPercent.textContent = '0%';
    el.comStatus.hidden = false;
    el.comStatus.textContent = '正在用已审核脚本渲染成片…';
    try {
      const form = new FormData();
      form.append('vertical', String(resolveVertical()));
      form.append('voice', el.comScriptVoice.value);
      const { job_id } = await request(`/api/commentary/render/${currentScriptJobId}`, {
        method: 'POST', body: form,
      });
      pollCommentaryJob(job_id,
        { commentary: el.comScriptRender, commentaryStatus: el.comStatus, commentaryFile: el.comScriptFile },
        '',
        () => {
          loadCommentary();
          el.comScriptPanel.hidden = true;
          currentScriptJobId = null;
        });
    } catch (err) {
      el.comStatus.textContent = `渲染启动失败：${err.message}`;
      el.comScriptRender.disabled = false;
      el.comScriptRender.textContent = '🎬 生成成片';
      el.comScriptSave.disabled = false;
    }
  };

  /** 用选中 voice 试听/预览音频。使用 DOM 内的 <audio> 元素 + data URL，避免 pywebview
   *  的 WKWebView 对 new Audio()/blob URL 支持不佳导致 "The operation is not supported"。 */
  const blobToDataUrl = (blob) => new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onloadend = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(blob);
  });

  const playAudio = async (blobOrUrl) => {
    const audio = el.comAudioPreview;
    if (!audio) return Promise.reject(new Error('音频播放器未初始化'));
    audio.pause();
    // blob URL 在 pywebview 的 WKWebView 里常被媒体播放器拒绝，统一转成 data URL。
    // data URL 不需要 URL.revokeObjectURL，src 被覆盖后即可被 GC。
    let dataUrl = blobOrUrl;
    if (typeof Blob !== 'undefined' && blobOrUrl instanceof Blob) {
      dataUrl = await blobToDataUrl(blobOrUrl);
    } else if (typeof blobOrUrl === 'string' && blobOrUrl.startsWith('blob:')) {
      try {
        const resp = await fetch(blobOrUrl);
        dataUrl = await blobToDataUrl(await resp.blob());
      } catch (_) {
        // 兜底：仍尝试原 blob URL
        dataUrl = blobOrUrl;
      }
    }
    audio.src = dataUrl;
    audio.dataset.dataUrl = dataUrl;
    audio.currentTime = 0;
    audio.muted = false;
    audio.playsInline = true;
    try {
      audio.load();
      return await audio.play();
    } catch (err) {
      // 若 DOM audio 仍失败，给出更具体提示
      throw new Error(`当前环境不支持自动播放音频：${err.message || '请尝试升级系统或手动点击播放'}`);
    }
  };

  /** 试听：把当前 voice + 一句示例文本发到后端 edge-tts 生成 mp3 播放 */
  const previewVoice = async () => {
    if (!commentaryEnvReady) {
      el.comScriptStatus.hidden = false;
      el.comScriptStatus.textContent = '解说环境未就绪，无法试听';
      return;
    }
    const voice = el.comScriptVoice.value;
    const originalText = el.comScriptVoicePreview.textContent;
    el.comScriptVoicePreview.disabled = true;
    el.comScriptVoicePreview.textContent = '⏳ 生成中…';
    try {
      const form = new FormData();
      form.append('voice', voice);
      form.append('text', '你好，我是视频解说员。我将为你解说这段视频。');
      // request 不能直接拿 blob，但 /api/commentary/voice-preview 返回 mp3 二进制；
      // 这里直接用 fetch 处理，方便放 audio 播放
      const resp = await fetch('/api/commentary/voice-preview', { method: 'POST', body: form });
      if (!resp.ok) {
        const errData = await resp.json().catch(() => ({}));
        throw new Error(errData.detail || errData.error || '生成失败');
      }
      const blob = await resp.blob();
      await playAudio(blob);
      // 30s 后清空 src，让 data URL 字符串尽早被 GC（data URL 不需要 revokeObjectURL）
      setTimeout(() => {
        const a = el.comAudioPreview;
        if (a && a.dataset.dataUrl === a.src) {
          a.pause();
          a.removeAttribute('src');
          a.load();
          a.removeAttribute('data-data-url');
        }
      }, 30000);
      el.comScriptStatus.hidden = false;
      el.comScriptStatus.className = 'com-script-status com-script-ok';
      el.comScriptStatus.textContent = `✓ 已用 ${voice} 试听`;
      setTimeout(() => { el.comScriptStatus.hidden = true; }, 2000);
    } catch (err) {
      el.comScriptStatus.hidden = false;
      el.comScriptStatus.className = 'com-script-status com-script-err';
      el.comScriptStatus.textContent = `试听失败：${err.message}`;
    } finally {
      el.comScriptVoicePreview.disabled = false;
      el.comScriptVoicePreview.textContent = originalText;
    }
  };

  /** 预览全部：把 script.json 前 3 段 narrations 用当前 voice 串成一段 mp3 播放 */
  const previewAllSegments = async () => {
    if (!currentScriptJobId) return;
    if (!commentaryEnvReady) {
      el.comScriptStatus.hidden = false;
      el.comScriptStatus.textContent = '解说环境未就绪，无法预览';
      return;
    }
    const originalText = el.comScriptPrevAll.textContent;
    el.comScriptPrevAll.disabled = true;
    el.comScriptPrevAll.textContent = '⏳ 生成中…';
    el.comScriptStatus.hidden = false;
    el.comScriptStatus.className = 'com-script-status';
    el.comScriptStatus.textContent = '正在用当前配音生成前 3 段预览…';
    try {
      const form = new FormData();
      form.append('voice', el.comScriptVoice.value);
      form.append('max_segments', '3');
      const resp = await fetch(`/api/commentary/preview/${currentScriptJobId}`, {
        method: 'POST', body: form,
      });
      if (!resp.ok) {
        const errData = await resp.json().catch(() => ({}));
        throw new Error(errData.detail || errData.error || '生成失败');
      }
      const blob = await resp.blob();
      await playAudio(blob);
      // 60s 后清空 src，让 data URL 字符串尽早被 GC
      setTimeout(() => {
        const a = el.comAudioPreview;
        if (a && a.dataset.dataUrl === a.src) {
          a.pause();
          a.removeAttribute('src');
          a.load();
          a.removeAttribute('data-data-url');
        }
      }, 60000);
      el.comScriptStatus.className = 'com-script-status com-script-ok';
      el.comScriptStatus.textContent = '✓ 预览播放中…';
    } catch (err) {
      el.comScriptStatus.className = 'com-script-status com-script-err';
      el.comScriptStatus.textContent = `预览失败：${err.message}`;
    } finally {
      el.comScriptPrevAll.disabled = false;
      el.comScriptPrevAll.textContent = originalText;
    }
  };

  // 脚本面板事件
  el.comScriptSave.addEventListener('click', saveScript);
  el.comScriptRender.addEventListener('click', renderFromScript);
  el.comScriptVoicePreview.addEventListener('click', previewVoice);
  el.comScriptPrevAll.addEventListener('click', previewAllSegments);

  // 桌面版(pywebview)下载拦截：<a download> 在 WebKit 里不弹保存框，
  // 下载/保存解说成片。由于后端被冻结在 PyInstaller 二进制里，新增 POST 路由
  // 不会生效；因此复用已经存在的 GET /api/commentary/file/{cid} 文件路由，
  // 优先尝试原生桥接，桥接不可用时再用 Blob + <a download> 触发浏览器保存。
  function wireSaveToDownloads(aEl) {
    if (!aEl) return;
    aEl.addEventListener('click', async (ev) => {
      const api = window.pywebview && window.pywebview.api;
      const href = aEl.href || '';
      // 兼容两种资源标识：
      //  - 已生成成片列表卡片：/api/commentary/file/{cid}
      //  - 生成完成的「保存到本机」：/api/commentary/{jobId}/file
      const mFile = /\/api\/commentary\/file\/([^/?#]+)/.exec(href);
      const mJob = /\/api\/commentary\/([^/]+)\/file/.exec(href);
      const cid = mFile ? mFile[1] : (mJob ? mJob[1] : '');
      const filename = aEl.getAttribute('download') || '解说成片.mp4';
      const orig = aEl.textContent;
      ev.preventDefault();
      if (!cid) { aEl.textContent = '保存失败：缺少成片标识'; setTimeout(() => aEl.textContent = orig, 3000); return; }

      // 方案 A：优先调用原生 Python 桥接。该桥接内部会请求 GET /api/commentary/{id}/file
      // 并把文件写到用户「下载」文件夹（VideoDownloader 桌面版的原生能力）。
      if (api && api.save_commentary_file) {
        aEl.textContent = '保存中…';
        try {
          const res = await api.save_commentary_file(cid, filename);
          if (typeof res === 'string' && res.startsWith('ERROR:')) {
            aEl.textContent = '保存失败：' + res.replace(/^ERROR:\s*/, '').slice(0, 40);
          } else {
            aEl.textContent = '已保存到下载文件夹 ✓';
          }
        } catch (err) {
          aEl.textContent = '保存失败：' + ((err && err.message) || '桥接调用失败');
        }
        setTimeout(() => { aEl.textContent = orig; }, 3000);
        return;
      }

      // 方案 B：无原生桥接时，用 fetch 获取已存在的 GET 文件路由，
      // 构造 Blob URL 并触发 <a download> 让浏览器完成保存。
      aEl.textContent = '保存中…';
      try {
        const resp = await fetch(`/api/commentary/file/${encodeURIComponent(cid)}`);
        if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail || `HTTP ${resp.status}`);
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        aEl.textContent = '已触发下载 ✓';
      } catch (err) {
        aEl.textContent = '保存失败：' + ((err && err.message) || '网络错误');
      }
      setTimeout(() => { aEl.textContent = orig; }, 3000);
    });
  }
  wireSaveToDownloads(el.comScriptFile);
  wireSaveToDownloads(el.libCommentaryFile);

  // ---- 视频解说独立标签页 ----
  const noopComFile = { hidden: true, href: '', setAttribute() {}, classList: { toggle() {} } };
  let selectedLocalFile = null;
  let commentaryEnvReady = false;
  let currentScriptJobId = null;  // 当前脚本审核任务的 job_id
  let currentScriptSegments = null;  // 原始脚本 segments（保留 start/end/note 供 save 合并）

  /** edge-tts 中文 Neural 音色可选列表。
   *  只保留 edge-tts list_voices() 真实返回、且经实测稳定的音色。
   *  云希(Yunxi) 对部分含口语/方言文本会返回 NoAudioReceived，故用云健(Yunjian) 替代「沉稳男声」。
   */
  const COM_VOICES = [
    { value: 'zh-CN-XiaoxiaoNeural', label: '晓晓（温柔女声）' },
    { value: 'zh-CN-XiaoyiNeural', label: '晓伊（活泼女声）' },
    { value: 'zh-CN-YunjianNeural', label: '云健（沉稳男声）' },
    { value: 'zh-CN-YunyangNeural', label: '云扬（新闻腔男声）' },
    { value: 'zh-CN-YunxiaNeural', label: '云夏（青年男声）' },
    { value: 'zh-CN-liaoning-XiaobeiNeural', label: '晓北（辽宁话女声）' },
    { value: 'zh-CN-shaanxi-XiaoniNeural', label: '晓妮（陕西话女声）' },
  ];

  /** 解说风格 → 默认联动音色（选择风格时自动套用，用户仍可在审核面板手动改）。
   *  key 与后端 commentary-worker/scripts/llm_script.py 的 STYLE_CONFIG 保持一致。 */
  const STYLE_VOICE = {
    none:        'zh-CN-XiaoxiaoNeural',  // 默认：温柔女声
    funny:       'zh-CN-YunxiaNeural',    // 搞笑：青年男声（年轻活泼）
    serious:     'zh-CN-YunyangNeural',   // 严肃：新闻腔男声
    domineering: 'zh-CN-YunjianNeural',   // 霸道：沉稳男声（低沉笃定）
    angry:       'zh-CN-YunyangNeural',   // 愤青：新闻腔男声
    suspense:    'zh-CN-YunjianNeural',   // 悬疑：沉稳男声（低沉神秘）
    healing:     'zh-CN-XiaoxiaoNeural',  // 治愈：温柔女声
    sarcastic:   'zh-CN-YunyangNeural',   // 毒舌：新闻腔男声（犀利冷幽默）
  };

  /** 把音色 value 翻译成展示名（用于在提示里显示联动音色）。 */
  const comVoiceLabel = (v) => {
    const hit = COM_VOICES.find((x) => x.value === v);
    return hit ? hit.label : (v || '默认');
  };

  /** 当前选中的解说风格 key（默认 none）。 */
  const comCurrentStyle = () => {
    const el2 = document.querySelector('input[name="comStyle"]:checked');
    return el2 ? el2.value : 'none';
  };

  /** 风格联动：自动把全局配音切到该风格的推荐音色，并更新提示文案。 */
  const comApplyStyleVoice = () => {
    const st = comCurrentStyle();
    const v = STYLE_VOICE[st] || 'zh-CN-XiaoxiaoNeural';
    if (el.comScriptVoice && !el.comScriptVoice.disabled) {
      el.comScriptVoice.value = v;
    }
    const hint = document.getElementById('comStyleHint');
    if (hint) {
      if (st === 'none') {
        hint.textContent = '默认风格：音色由下方「全局配音」决定。';
      } else {
        hint.textContent = `「${st}」风格已联动音色：${comVoiceLabel(v)}（可在下方「全局配音」手动改）。`;
      }
    }
  };

  const refreshCommentaryDiagnostics = async () => {
    try {
      const d = await request('/api/commentary/diagnostics');
      const issues = d.issues || [];
      const ready = d.ready && !issues.length;
      commentaryEnvReady = ready;
      el.comEnvStatus.hidden = false;
      el.comEnvStatus.className = 'com-env-status ' + (ready ? 'ok' : 'err');
      if (ready) {
        el.comEnvStatus.textContent = '✓ 解说环境就绪：python=' + d.python + '  ffprobe=' + (d.ffmpeg_dir || '');
      } else if (!d.enabled) {
        el.comEnvStatus.textContent = '⚠ 解说功能未启用：未检测到 commentary-pipeline 目录。';
      } else {
        el.comEnvStatus.textContent = '⚠ 解说环境未就绪：' + issues.join('；') + '  dir=' + (d.dir || 'none') + ' python=' + (d.python || '');
      }
    } catch (e) {
      commentaryEnvReady = false;
      el.comEnvStatus.hidden = false;
      el.comEnvStatus.className = 'com-env-status err';
      el.comEnvStatus.textContent = '⚠ 无法读取解说环境诊断：' + (e.message || '未知错误');
    }
  };

  async function loadCommentary() {
    // 重置生成区状态
    el.comGenerateScript.disabled = false;
    el.comGenerateScript.textContent = '生成脚本（可审核修改）';
    el.comGenerateScript.hidden = false;
    el.comScriptPanel.hidden = true;
    el.comScriptSegments.replaceChildren();
    el.comScriptStatus.hidden = true;
    currentScriptJobId = null;
    el.comProgress.hidden = true;
    el.comStatus.hidden = true;
    el.comFileStatus.hidden = true;
    el.comEta.hidden = true;
    el.comSource.value = '';
    selectedLocalFile = null;
    el.comFileName.textContent = '';
    setupComPreview(null);

    try {
      const data = await request('/api/commentary/list');
      commentaryItems = data.items || [];
      renderCommentaryList();
    } catch (e) {
      el.comEmpty.hidden = false;
      el.comEmpty.textContent = '读取解说成片失败：' + (e.message || '未知错误');
    }
    refreshComSource();
    refreshCommentaryDiagnostics();
  };

  /** 按当前视图模式与排序重新渲染成片列表 */
  const renderCommentaryList = () => {
    const items = commentaryItems.slice();
    const [sortKey, sortOrder] = commentarySort.split('-');
    items.sort((a, b) => {
      let av, bv;
      if (sortKey === 'mtime') { av = a.mtime; bv = b.mtime; }
      else if (sortKey === 'size') { av = a.size; bv = b.size; }
      else { av = String(a.name).toLowerCase(); bv = String(b.name).toLowerCase(); }
      if (av < bv) return sortOrder === 'asc' ? -1 : 1;
      if (av > bv) return sortOrder === 'asc' ? 1 : -1;
      return 0;
    });

    el.comGrid.className = 'com-grid com-view-' + commentaryViewMode;
    el.comGrid.replaceChildren();
    el.comEmpty.hidden = items.length > 0;
    el.comHistory.hidden = items.length === 0;

    if (items.length === 0) {
      el.comEmpty.textContent = '还没有解说成片。从下载历史库选择视频，或拖入本地视频即可开始。';
    } else {
      el.comHistoryCount.textContent = `${items.length} 个`;
      if (commentaryViewMode === 'timeline') {
        renderComTimeline(items);
      } else if (commentaryViewMode === 'gallery') {
        renderComGallery(items);
      } else {
        items.forEach((it) => el.comGrid.appendChild(createComCard(it)));
      }
    }
  };

  /** 时间线视图：按日期分组 */
  const renderComTimeline = (items) => {
    const groups = {};
    items.forEach((it) => {
      const d = new Date(it.mtime * 1000).toLocaleDateString();
      (groups[d] = groups[d] || []).push(it);
    });
    Object.keys(groups).sort((a, b) => {
      const desc = commentarySort === 'mtime-desc';
      const da = new Date(a).getTime();
      const db = new Date(b).getTime();
      return desc ? db - da : da - db;
    }).forEach((date) => {
      const h = document.createElement('div');
      h.className = 'com-timeline-date';
      h.textContent = date;
      el.comGrid.appendChild(h);
      groups[date].forEach((it) => el.comGrid.appendChild(createComCard(it)));
    });
  };

  /** 画廊视图：只放视频大图，隐藏元信息 */
  const renderComGallery = (items) => {
    items.forEach((it) => el.comGrid.appendChild(createComCard(it, true)));
  };

  const refreshComSource = async () => {
    try {
      const data = await request('/api/library');
      const items = (data.items || []).filter((i) => i.kind === 'video');
      const current = el.comSource.value;
      el.comSource.replaceChildren();
      const def = document.createElement('option');
      def.value = '';
      def.textContent = items.length ? '选择视频…' : '媒体库暂无视频';
      el.comSource.appendChild(def);
      items.forEach((i) => {
        const o = document.createElement('option');
        o.value = i.id;
        o.textContent = i.title || i.name || i.id;
        el.comSource.appendChild(o);
      });
      if ([...el.comSource.options].some((o) => o.value === current)) el.comSource.value = current;
    } catch {
      // 媒体库不可用时下拉只保留默认提示
      el.comSource.replaceChildren();
      const def = document.createElement('option');
      def.value = ''; def.textContent = '无法读取媒体库';
      el.comSource.appendChild(def);
    }
  };

  // ---- 预览与裁剪逻辑 ----
  const releaseComPreview = () => {
    if (comPreviewUrl && comPreviewUrl.startsWith('blob:')) {
      URL.revokeObjectURL(comPreviewUrl);
    }
    comPreviewUrl = null;
  };

  const setupComPreview = (url) => {
    if (!url) {
      el.comTrimCard.hidden = true;
      releaseComPreview();
      comTrimStart = 0;
      comTrimEnd = 0;
      comPreviewDuration = 0;
      return;
    }
    releaseComPreview();
    comPreviewUrl = url;
    el.comTrimCard.hidden = false;
    el.comPreview.src = url;
    el.comPreview.load();
    el.comPreview.onloadedmetadata = () => {
      comPreviewDuration = el.comPreview.duration || 0;
      comPreviewW = el.comPreview.videoWidth || 0;
      comPreviewH = el.comPreview.videoHeight || 0;
      el.comTrimStartRange.max = String(comPreviewDuration || 100);
      el.comTrimEndRange.max = String(comPreviewDuration || 100);
      resetTrim();
    };
    el.comPreview.onerror = () => { el.comTrimCard.hidden = true; };
  };

  const resetTrim = () => {
    comTrimStart = 0;
    comTrimEnd = comPreviewDuration || 0;
    syncTrimInputs();
  };

  const syncTrimInputs = () => {
    el.comTrimStartRange.value = String(comTrimStart);
    el.comTrimEndRange.value = String(comTrimEnd);
    el.comTrimStart.value = formatHMS(comTrimStart);
    el.comTrimEnd.value = formatHMS(comTrimEnd);
    updateTrimDurationText();
  };

  const updateTrimDurationText = () => {
    const dur = Math.max(0, comTrimEnd - comTrimStart);
    el.comTrimDuration.textContent = `裁剪后时长：${formatDuration(dur) || '0s'}`;
  };

  const clampTrim = () => {
    const total = comPreviewDuration || 0;
    let s = Math.max(0, Math.min(comTrimStart, total));
    let e = Math.max(0, Math.min(comTrimEnd, total));
    if (e <= s) {
      if (s >= total) s = Math.max(0, total - 0.5);
      e = Math.min(total, s + 0.5);
    }
    comTrimStart = s;
    comTrimEnd = e;
    syncTrimInputs();
  };

  const createComCard = (it, gallery = false) => {
    const card = document.createElement('div');
    card.className = 'com-card' + (gallery ? ' com-card-gallery' : '');
    card.dataset.id = it.id;

    const url = `/api/commentary/file/${encodeURIComponent(it.id)}`;
    const video = document.createElement('video');
    video.className = 'com-video';
    video.src = url;
    video.controls = true;
    video.preload = 'metadata';

    const meta = document.createElement('div');
    meta.className = 'com-meta';
    const name = document.createElement('span');
    name.className = 'com-name';
    name.title = it.name;
    name.textContent = it.name;
    const size = document.createElement('span');
    size.className = 'com-size';
    size.textContent = `${formatBytes(it.size)} · ${new Date(it.mtime * 1000).toLocaleString()}`;
    meta.appendChild(name);
    meta.appendChild(size);

    const actions = document.createElement('div');
    actions.className = 'com-actions';
    const dl = document.createElement('button');
    dl.type = 'button';
    dl.className = 'btn btn-success btn-sm';
    dl.title = '选择保存位置（可重命名），默认存入下载文件夹';
    dl.textContent = '💾 保存';
    dl.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      saveCommentaryAs(it.id, it.name, dl);
    });
    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.className = 'btn btn-ghost btn-sm';
    delBtn.title = '删除（移入回收站）';
    delBtn.textContent = '🗑 删除';
    delBtn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      deleteCommentary(it.id, it.name, card);
    });
    actions.append(dl, delBtn);

    card.appendChild(video);
    card.appendChild(meta);
    card.appendChild(actions);
    return card;
  };

  /** 「保存」：桌面端弹出原生保存面板（默认下载文件夹、可重命名/改位置）；Web 端退化为浏览器下载 */
  const saveCommentaryAs = async (id, name, btn) => {
    const api = window.pywebview && window.pywebview.api;
    const orig = btn.textContent;
    btn.disabled = true;
    btn.textContent = '选择中…';
    try {
      if (api && api.save_commentary_file_dialog) {
        const res = await api.save_commentary_file_dialog(id, name);
        if (typeof res === 'string') {
          if (res === 'CANCELLED' || res.startsWith('ERROR: 已取消')) {
            // 用户取消，静默无操作
          } else if (res.startsWith('ERROR:')) {
            showError('保存失败：' + res.slice(6).trim(), '');
          } else {
            showToast('已保存到：' + res);
          }
        }
      } else {
        // Web 模式：回退到浏览器下载
        const a = document.createElement('a');
        a.href = `/api/commentary/file/${encodeURIComponent(id)}`;
        a.download = name;
        a.click();
      }
    } catch (e) {
      showError('保存失败：' + (e.message || '未知错误'), '');
    } finally {
      btn.disabled = false;
      btn.textContent = orig;
    }
  };

  /** 重命名：行内编辑文件名，回车或失焦提交，Esc 取消 */
  const startRename = (it, nameSpan) => {
    const input = document.createElement('input');
    input.type = 'text';
    input.value = it.name;
    input.className = 'com-rename-input';
    input.style.cssText = 'width:100%;box-sizing:border-box;font-size:13px;padding:2px 4px;';
    nameSpan.replaceWith(input);
    input.focus();
    input.select();
    let settled = false;
    const finish = async (commit) => {
      if (settled) return;
      settled = true;
      if (!commit) {
        input.replaceWith(nameSpan);
        return;
      }
      const newName = input.value.trim();
      if (!newName || newName === it.name) {
        input.replaceWith(nameSpan);
        return;
      }
      try {
        await request(`/api/commentary/file/${encodeURIComponent(it.id)}`, {
          method: 'PUT',
          body: JSON.stringify({ name: newName }),
        });
        // 后端可能改了 cid，直接重新拉取列表最稳妥
        await loadCommentary();
      } catch (e) {
        showError('重命名失败：' + (e.message || '未知错误'), '');
        input.replaceWith(nameSpan);
      }
    };
    input.addEventListener('blur', () => finish(true));
    input.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') {
        ev.preventDefault();
        finish(true);
      } else if (ev.key === 'Escape') {
        ev.preventDefault();
        finish(false);
      }
    });
  };

  /** 删除解说成片：后端移入回收站，成功后刷新列表 */
  const deleteCommentary = async (id, name, cardEl) => {
    const ok = await showConfirm(
      `确定删除解说成片「${name}」？\n删除后会移入系统回收站，可从回收站找回。`,
      { okText: '删除', danger: true }
    );
    if (!ok) return;
    cardEl.style.opacity = '.5';
    try {
      await request(`/api/commentary/file/${encodeURIComponent(id)}`, { method: 'DELETE' });
      await loadCommentary();
    } catch (e) {
      cardEl.style.opacity = '1';
      showError(e.message || '删除失败', '删除后已移入回收站，请检查回收站是否可用');
    }
  };

  el.comGenerateScript.addEventListener('click', () => {
    if (!commentaryEnvReady) {
      el.comStatus.hidden = false;
      el.comStatus.textContent = '解说环境未就绪，请先看上方环境状态条排查依赖';
      return;
    }
    const fileId = el.comSource.value;
    if (fileId) {
      createScriptOnly({ fileId });
      return;
    }
    if (selectedLocalFile) {
      createScriptOnlyFromFile(selectedLocalFile);
      return;
    }
    el.comStatus.hidden = false;
    el.comStatus.textContent = '请从下载历史库选择视频，或选择本地视频';
  });

  el.comOpenReview.addEventListener('click', () => {
    if (currentScriptJobId) {
      openScriptReview(currentScriptJobId, { autoScroll: true });
    }
  });

  // 解说风格切换：联动默认音色 + 更新提示文案（用户仍可在审核面板手动改音色）
  document.querySelectorAll('input[name="comStyle"]').forEach((r) => {
    r.addEventListener('change', comApplyStyleVoice);
  });
  comApplyStyleVoice();  // 初始化提示

  // 一键生成：强制「全片深入解说 + 联网找资料 + 片头插精彩片段」，其余沿用用户选择；
  // 仍走脚本审核流程（默认铁律：AI 解说词可人工审核修改）。
  el.comGenerateOneClick.addEventListener('click', () => {
    if (!commentaryEnvReady) {
      el.comStatus.hidden = false;
      el.comStatus.textContent = '解说环境未就绪，请先看上方环境状态条排查依赖';
      return;
    }
    const fileId = el.comSource.value;
    if (fileId) {
      createCommentary(
        { fileId },
        { commentary: el.comGenerateOneClick, commentaryStatus: el.comStatus, commentaryFile: el.comScriptFile },
        '',
        true,
      );
      return;
    }
    if (selectedLocalFile) {
      createCommentaryFromFile(
        selectedLocalFile,
        { commentary: el.comGenerateOneClick, commentaryStatus: el.comStatus, commentaryFile: el.comScriptFile },
        true,
      );
      return;
    }
    el.comStatus.hidden = false;
    el.comStatus.textContent = '请从下载历史库选择视频，或选择本地视频';
  });

  // 来源互斥：选了下拉就清空本地文件
  el.comSource.addEventListener('change', () => {
    if (el.comSource.value) {
      selectedLocalFile = null;
      el.comFileName.textContent = '';
      el.comFileStatus.hidden = true;
      setupComPreview(`/api/library/file/${encodeURIComponent(el.comSource.value)}`);
    } else {
      setupComPreview(null);
    }
  });

  // 入口 2：从本地文件生成
  const setLocalFile = (file) => {
    if (!file || !file.type.startsWith('video/')) {
      el.comFileStatus.hidden = false;
      el.comFileStatus.textContent = '请选择视频文件';
      return;
    }
    selectedLocalFile = file;
    el.comFileName.textContent = file.name;
    el.comFileStatus.hidden = true;
    el.comSource.value = '';
    setupComPreview(URL.createObjectURL(file));
  };
  el.comFileBtn.addEventListener('click', () => el.comFileInput.click());
  el.comFileInput.addEventListener('change', () => {
    const file = el.comFileInput.files[0];
    if (file) setLocalFile(file);
  });
  ['dragenter', 'dragover'].forEach((ev) => {
    el.comDropZone.addEventListener(ev, (e) => {
      e.preventDefault();
      el.comDropZone.classList.add('is-dragover');
    });
  });
  ['dragleave', 'drop'].forEach((ev) => {
    el.comDropZone.addEventListener(ev, (e) => {
      e.preventDefault();
      el.comDropZone.classList.remove('is-dragover');
    });
  });
  el.comDropZone.addEventListener('drop', (e) => {
    const file = e.dataTransfer.files[0];
    if (file) setLocalFile(file);
  });

  // 裁剪控件事件
  el.comTrimStartRange.addEventListener('input', () => {
    comTrimStart = parseFloat(el.comTrimStartRange.value) || 0;
    if (comTrimStart > comTrimEnd) comTrimStart = comTrimEnd;
    syncTrimInputs();
  });
  el.comTrimEndRange.addEventListener('input', () => {
    comTrimEnd = parseFloat(el.comTrimEndRange.value) || 0;
    if (comTrimEnd < comTrimStart) comTrimEnd = comTrimStart;
    syncTrimInputs();
  });
  el.comTrimStart.addEventListener('change', () => {
    comTrimStart = Math.max(0, parseHMS(el.comTrimStart.value));
    clampTrim();
  });
  el.comTrimEnd.addEventListener('change', () => {
    comTrimEnd = parseHMS(el.comTrimEnd.value);
    clampTrim();
  });
  el.comTrimSetStart.addEventListener('click', () => {
    comTrimStart = Math.max(0, Math.min(el.comPreview.currentTime, comTrimEnd - 0.5));
    syncTrimInputs();
  });
  el.comTrimSetEnd.addEventListener('click', () => {
    comTrimEnd = Math.min(comPreviewDuration || el.comPreview.currentTime,
                          Math.max(el.comPreview.currentTime, comTrimStart + 0.5));
    syncTrimInputs();
  });
  el.comTrimPreview.addEventListener('click', () => {
    if (!comPreviewDuration) return;
    el.comPreview.currentTime = comTrimStart;
    const stopAt = comTrimEnd;
    const onTime = () => {
      if (el.comPreview.currentTime >= stopAt) {
        el.comPreview.pause();
        el.comPreview.removeEventListener('timeupdate', onTime);
      }
    };
    el.comPreview.addEventListener('timeupdate', onTime);
    el.comPreview.play().catch(() => {});
  });
  el.comTrimReset.addEventListener('click', resetTrim);

  el.comRefresh.addEventListener('click', loadCommentary);

  // 解说成片：视图模式切换
  el.comHistoryToolbar.addEventListener('click', (e) => {
    const btn = e.target.closest('.com-view-btn');
    if (!btn) return;
    commentaryViewMode = btn.dataset.mode;
    el.comHistoryToolbar.querySelectorAll('.com-view-btn').forEach((b) => b.classList.toggle('active', b === btn));
    renderCommentaryList();
  });

  // 解说成片：排序切换（自定义弹出菜单，仿 macOS 原生菜单）
  const SORT_LABELS = {
    'mtime-desc': '时间：最新在前', 'mtime-asc': '时间：最早在前',
    'size-desc': '大小：从大到小', 'size-asc': '大小：从小到大',
    'name-asc': '名称：A-Z', 'name-desc': '名称：Z-A',
  };
  const syncSortMenu = () => {
    el.comSortLabel.textContent = SORT_LABELS[commentarySort] || commentarySort;
    el.comSortMenu.querySelectorAll('li[data-value]').forEach((li) =>
      li.classList.toggle('selected', li.dataset.value === commentarySort));
  };
  const closeSortMenu = () => {
    el.comSortMenu.classList.remove('open');
    el.comSortBtn.setAttribute('aria-expanded', 'false');
  };
  el.comSortBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    const open = el.comSortMenu.classList.toggle('open');
    el.comSortBtn.setAttribute('aria-expanded', String(open));
  });
  el.comSortMenu.addEventListener('click', (e) => {
    const li = e.target.closest('li[data-value]');
    if (!li) return;
    commentarySort = li.dataset.value;
    syncSortMenu();
    closeSortMenu();
    renderCommentaryList();
  });
  document.addEventListener('click', (e) => {
    if (!el.comSortMenu.contains(e.target) && !el.comSortBtn.contains(e.target)) closeSortMenu();
  });
  syncSortMenu();

  // ------------------------------------------------------------------ 初始化

  const toggleClearButton = () => { el.clearBtn.hidden = el.input.value.length === 0; };

  el.form.addEventListener('submit', handleResolve);
  el.batchBtn.addEventListener('click', () => {
    const urls = el.batchInput.value.split(/\s+/).map((s) => s.trim()).filter(Boolean);
    if (!urls.length) { showError('请先粘贴至少一个视频链接'); return; }
    runBatch(urls, el.cookieInput.value.trim(), el.proxyInput.value.trim());
  });
  el.batchToggle.addEventListener('click', () => {
    const open = el.batchBox.hidden;
    el.batchBox.hidden = !open;
    el.batchToggle.setAttribute('aria-expanded', String(open));
    el.batchToggle.classList.toggle('is-open', open);
    if (open) setTimeout(() => el.batchInput.focus(), 50);
  });
  el.batchConcurrency.addEventListener('input', () => { el.batchConcVal.textContent = el.batchConcurrency.value; });
  el.cancelAllBtn.addEventListener('click', cancelAll);
  el.openFolderBtn.addEventListener('click', openDownloadFolder);
  el.downloadBtn.addEventListener('click', handleDownload);
  el.cookieDetectBtn.addEventListener('click', () => {
    const u = el.input.value.trim();
    if (!u) { el.cookieStatus.textContent = '请先粘贴视频链接再检测'; el.cookieStatus.className = 'cookie-status warn'; return; }
    updateCookieStatus(u);
  });
  el.serverFallbackBtn.addEventListener('click', () => startDownload(selectedQuality || 'best'));
  el.input.addEventListener('input', () => { toggleClearButton(); paintNodeBar(); });
  el.clearBtn.addEventListener('click', () => {
    el.input.value = '';
    el.cookieInput.value = '';
    el.proxyInput.value = '';
    toggleClearButton();
    paintNodeBar();
    clearError();
    el.resultPanel.hidden = true;
    el.input.focus();
  });
  // 自动判断不准时手动掰：先固定到另一条线路，再点一次恢复自动
  el.nodeSwitch.addEventListener('click', () => {
    forcedRegion = forcedRegion ? null : (regionFor(el.input.value) === 'cn' ? 'global' : 'cn');
    paintNodeBar();
  });

  el.modalClose.addEventListener('click', () => el.modal.close());
  el.modal.addEventListener('click', (event) => {
    if (event.target === el.modal) el.modal.close();
  });
  el.badge.addEventListener('click', () => openPlatformModal(allPlatforms));

  // 订阅解锁（增值能力变现）：默认不开墙则 UI 不出现；convert 或 download 任一开墙即显示入口
  const refreshSubModalText = () => {
    const parts = [];
    if (node.convertSubRequired) parts.push(`格式转换每日限 ${node.convertFreeDaily} 次`);
    if (node.downloadSubRequired) {
      const left = Math.max(0, node.downloadFreeDaily - node.downloadFreeUsed);
      parts.push(`下载每日限 ${node.downloadFreeDaily} 次（当前剩余 ${left}）`);
    }
    if (node.cloudSubRequired) {
      const left = Math.max(0, node.cloudFreeDaily - node.cloudFreeUsed);
      parts.push(`存网盘每日限 ${node.cloudFreeDaily} 次（当前剩余 ${left}）`);
    }
    el.subModalSub.textContent = parts.length
      ? `免费用户：${parts.join('；')}。订阅后全部无限使用。`
      : '订阅后解锁全部增值能力，无限使用。';
  };

  const initSubUI = () => {
    if (!node.convertSubRequired && !node.downloadSubRequired && !node.cloudSubRequired) return;
    const key = localStorage.getItem('vdl_sub_key');
    el.subBadge.hidden = false;
    el.subBadge.textContent = key ? '已订阅 ✓' : '🔓 订阅解锁';
    refreshSubModalText();
  };

  // 免费额度耗尽 / 未订阅时，引导用户点右上角订阅（闪烁提示 + 入口常驻）
  const promptSubscribe = () => {
    el.subBadge.hidden = false;
    el.subBadge.classList.add('pulse');
    el.subBadge.textContent = '🔓 订阅解锁';
  };
  el.subBadge.addEventListener('click', () => {
    if (typeof el.subModal.showModal === 'function') el.subModal.showModal();
    else el.subModal.setAttribute('open', '');
  });
  el.subModalClose.addEventListener('click', () => el.subModal.close());
  el.subModal.addEventListener('click', (event) => {
    if (event.target === el.subModal) el.subModal.close();
  });
  el.subApply.addEventListener('click', () => {
    const key = el.subInput.value.trim();
    const msg = el.subMsg;
    if (!key) {
      msg.hidden = false; msg.className = 'sub-msg is-err'; msg.textContent = '请输入订阅密钥';
      return;
    }
    localStorage.setItem('vdl_sub_key', key);
    msg.hidden = false; msg.className = 'sub-msg is-ok';
    msg.textContent = '已保存，下次下载 / 转换将自动验证解锁';
    refreshSubModalText();
    el.subBadge.textContent = '已订阅 ✓';
    el.subBadge.hidden = false;
    setTimeout(() => el.subModal.close(), 900);
  });

  // ------------------------------------------------------------------ 云盘存盘
  let cloudCurrentTaskId = null;
  let cloudCurrentRefs = null;
  let baiduToken = localStorage.getItem('vdl_baidu_token') || '';

  const syncCloudForm = () => {
    const p = document.querySelector('input[name=cloudProvider]:checked').value;
    el.cloudWebdavForm.hidden = p !== 'webdav';
    el.cloudBaiduForm.hidden = p !== 'baidu';
  };

  const openCloudModal = (taskId, refs) => {
    cloudCurrentTaskId = taskId;
    cloudCurrentRefs = refs;
    try {
      const wd = JSON.parse(localStorage.getItem('vdl_webdav') || '{}');
      el.cloudWebdavUrl.value = wd.url || '';
      el.cloudWebdavUser.value = wd.user || '';
      el.cloudWebdavPass.value = wd.pass || '';
    } catch { /* 忽略损坏的本地配置 */ }
    el.cloudDest.value = '';
    el.cloudStatus.textContent = '';
    el.cloudStatus.className = 'cloud-status';
    el.cloudBaiduRadio.hidden = !node.baiduAvailable;
    if (!node.baiduAvailable) {
      const wdRadio = document.querySelector('input[name=cloudProvider][value=webdav]');
      if (wdRadio) wdRadio.checked = true;
    }
    syncCloudForm();
    el.cloudBaiduStatus.textContent = baiduToken ? '已授权 ✓' : '未授权';
    if (node.cloudSubRequired) {
      const left = Math.max(0, node.cloudFreeDaily - node.cloudFreeUsed);
      el.cloudSubNote.hidden = false;
      el.cloudSubNote.textContent = node.subscribed
        ? '已订阅 · 无限存网盘 ✓'
        : (left > 0 ? `今日免费剩余 ${left}/${node.cloudFreeDaily} 次` : '今日免费次数已用完 · 点右上角订阅解锁');
    } else {
      el.cloudSubNote.hidden = true;
    }
    if (typeof el.cloudModal.showModal === 'function') el.cloudModal.showModal();
    else el.cloudModal.setAttribute('open', '');
  };

  const startCloudSave = async () => {
    if (!cloudCurrentTaskId) return;
    const provider = document.querySelector('input[name=cloudProvider]:checked').value;
    const dest = el.cloudDest.value.trim();
    const body = { task_id: cloudCurrentTaskId, provider, dest_path: dest };
    if (provider === 'webdav') {
      const wd = {
        url: el.cloudWebdavUrl.value.trim(),
        user: el.cloudWebdavUser.value.trim(),
        pass: el.cloudWebdavPass.value,
      };
      if (!wd.url) {
        el.cloudStatus.textContent = '请填写 WebDAV 地址';
        el.cloudStatus.className = 'cloud-status is-err';
        return;
      }
      localStorage.setItem('vdl_webdav', JSON.stringify(wd));
      body.webdav = wd;
    } else if (provider === 'baidu') {
      if (!baiduToken) {
        el.cloudStatus.textContent = '请先点「授权百度网盘」完成授权';
        el.cloudStatus.className = 'cloud-status is-err';
        return;
      }
      body.baidu = { token: baiduToken };
    }
    el.cloudSave.disabled = true;
    el.cloudStatus.textContent = '上传中…';
    el.cloudStatus.className = 'cloud-status';
    try {
      const { job_id: jobId, quota } = await request('/api/cloud/save', {
        method: 'POST', body: JSON.stringify(body),
      });
      if (quota) {
        if (quota.subscribed) node.subscribed = true;
        node.cloudFreeUsed = quota.free_used || node.cloudFreeUsed;
      }
      pollCloud(jobId);
    } catch (error) {
      el.cloudSave.disabled = false;
      const msg = (error && error.message) || '';
      if (msg.indexOf('订阅') >= 0) {
        promptSubscribe();
        el.cloudStatus.textContent = '今日免费次数已用完，点右上角「订阅解锁」无限存网盘';
      } else {
        el.cloudStatus.textContent = '保存失败：' + msg;
      }
      el.cloudStatus.className = 'cloud-status is-err';
    }
  };

  const pollCloud = (jobId) => {
    const timer = setInterval(async () => {
      try {
        const st = await request('/api/cloud/status/' + jobId);
        if (st.status === 'completed') {
          clearInterval(timer);
          el.cloudSave.disabled = false;
          el.cloudStatus.textContent = '已存到网盘 ✓' + (st.remote_path ? '（' + st.remote_path + '）' : '');
          el.cloudStatus.className = 'cloud-status is-ok';
          if (cloudCurrentRefs) {
            cloudCurrentRefs.cloud.hidden = false;
            cloudCurrentRefs.cloudStatus.hidden = false;
            cloudCurrentRefs.cloudStatus.textContent = '已存到网盘：' + (st.remote_path || '');
          }
        } else if (st.status === 'failed') {
          clearInterval(timer);
          el.cloudSave.disabled = false;
          el.cloudStatus.textContent = '保存失败：' + (st.error || '未知错误');
          el.cloudStatus.className = 'cloud-status is-err';
        } else {
          el.cloudStatus.textContent = '上传中…' + (st.progress ? ' ' + st.progress + '%' : '');
        }
      } catch { /* 轮询出错继续 */ }
    }, 3000);
  };

  // 在当前页内（dialog）完成百度授权，绝不再开第二个浏览器窗口
  const openBaiduAuthInPage = (url) => {
    document.getElementById('baiduAuthDialog')?.remove();
    const dlg = document.createElement('dialog');
    dlg.id = 'baiduAuthDialog';
    dlg.className = 'modal';
    dlg.innerHTML =
      '<div class="modal-card">' +
      '  <div class="baidu-auth-head">' +
      '    <h2>百度网盘授权</h2>' +
      '    <button type="button" class="modal-close" id="baiduAuthClose" aria-label="关闭">✕</button>' +
      '  </div>' +
      '  <iframe class="baidu-auth-frame" src="' + url + '"></iframe>' +
      '</div>';
    document.body.appendChild(dlg);
    dlg.showModal();
    dlg.addEventListener('click', (e) => { if (e.target === dlg) dlg.close(); });
    dlg.querySelector('#baiduAuthClose').addEventListener('click', () => dlg.close());
    dlg.addEventListener('close', () => dlg.remove());
  };

  // 云盘弹窗事件绑定
  el.cloudModalClose.addEventListener('click', () => el.cloudModal.close());
  el.cloudModal.addEventListener('click', (e) => { if (e.target === el.cloudModal) el.cloudModal.close(); });
  el.cloudSave.addEventListener('click', startCloudSave);
  el.cloudModal.querySelectorAll('input[name=cloudProvider]').forEach((r) => r.addEventListener('change', syncCloudForm));
  el.cloudBaiduBtn.addEventListener('click', () => {
    if (!node.baiduAuthUrl) { el.cloudBaiduStatus.textContent = '该实例未启用百度网盘'; return; }
    openBaiduAuthInPage(node.baiduAuthUrl);
  });
  window.addEventListener('message', (e) => {
    if (e.origin !== location.origin) return;
    const d = e.data || {};
    if (d.source !== 'vdl-baidu') return;
    if (d.token) {
      baiduToken = d.token;
      localStorage.setItem('vdl_baidu_token', d.token);
      el.cloudBaiduStatus.textContent = '已授权 ✓';
    } else if (d.error) {
      el.cloudBaiduStatus.textContent = '授权失败：' + d.error;
    }
    const bd = document.getElementById('baiduAuthDialog');
    if (bd) bd.close();
  });

  // ------------------------------------------------------------------ 媒体库（桌面版功能）
  // 以磁盘文件为准浏览/播放/删除已下载内容；能力由 /api/nodes 的 library.enabled 控制。
  let libItems = [];
  let currentLibItem = null;

  const debounce = (fn, ms) => {
    let t;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  };
  const libThumbUrl = (id) => `/api/library/thumb/${encodeURIComponent(id)}`;
  const libFileUrl = (id) => `/api/library/file/${encodeURIComponent(id)}`;
  const libEncFileUrl = (id) => `/api/library/encfile/${encodeURIComponent(id)}`;

  function switchView(view) {
    const isLib = view === 'library';
    const isSub = view === 'subscribe';
    const isTor = view === 'torrent';
    const isCom = view === 'commentary';
    el.downloadView.hidden = isLib || isSub || isTor || isCom;
    el.libraryView.hidden = !isLib;
    el.subscribeView.hidden = !isSub;
    el.torrentView.hidden = !isTor;
    el.commentaryView.hidden = !isCom;
    el.tabDownload.classList.toggle('is-active', !isLib && !isSub && !isTor && !isCom);
    el.tabLibrary.classList.toggle('is-active', isLib);
    el.tabSubscribe.classList.toggle('is-active', isSub);
    el.tabTorrent.classList.toggle('is-active', isTor);
    el.tabCommentary.classList.toggle('is-active', isCom);
    if (isLib) loadLibrary();
    if (isSub) loadSubscriptions();
    if (isCom) loadCommentary();
    if (isTor) { loadTorrents(); startTorPoll(); }
    else stopTorPoll();
  };

  const loadLibrary = async () => {
    const params = new URLSearchParams();
    const q = el.libSearch.value.trim();
    const platform = el.libPlatform.value;
    const kind = el.libKind.value;
    if (q) params.set('q', q);
    if (platform) params.set('platform', platform);
    if (kind && kind !== 'all') params.set('kind', kind);
    try {
      // 每次刷新清除旧选中（卡片重新渲染，旧 ID 已无效）
      selectedLibIds.clear();
      updateBatchUI();
      const data = await request(`/api/library?${params.toString()}`);
      libItems = data.items || [];
      renderLibGrid(libItems);
    } catch (e) {
      el.libEmpty.hidden = false;
      el.libEmpty.textContent = '读取媒体库失败：' + (e.message || '未知错误');
    }
  };

  const refreshLibPlatforms = (items) => {
    const current = el.libPlatform.value;
    const platforms = Array.from(new Set(items.map((i) => i.platform).filter(Boolean))).sort();
    el.libPlatform.replaceChildren();
    const all = document.createElement('option');
    all.value = ''; all.textContent = '全部平台';
    el.libPlatform.appendChild(all);
    platforms.forEach((p) => {
      const o = document.createElement('option');
      o.value = p; o.textContent = p;
      el.libPlatform.appendChild(o);
    });
    if ([...el.libPlatform.options].some((o) => o.value === current)) el.libPlatform.value = current;
  };

  const renderLibGrid = (items) => {
    el.libGrid.replaceChildren();
    el.libEmpty.hidden = items.length > 0;
    if (items.length === 0) el.libEmpty.textContent = '还没有下载内容。去「下载」粘贴链接保存第一个视频吧。';
    items.forEach((item) => el.libGrid.appendChild(createLibCard(item)));
    refreshLibPlatforms(items);
  };

  // ---- 订阅追更（桌面版功能） ----
  const loadSubscriptions = async () => {
    if (!node.subscriptionsEnabled) return;
    try {
      const data = await request('/api/subscriptions');
      renderSubscriptions(data.subscriptions || []);
    } catch (e) {
      el.subEmpty.hidden = false;
      el.subEmpty.textContent = '读取订阅失败：' + (e.message || '未知错误');
    }
  };

  const renderSubscriptions = (subs) => {
    el.subList.replaceChildren();
    el.subEmpty.hidden = subs.length > 0;
    if (subs.length === 0) {
      el.subEmpty.textContent = '还没有订阅。添加频道后，新视频会自动下载。';
      return;
    }
    subs.forEach((s) => el.subList.appendChild(createSubCard(s)));
  };

  const createSubCard = (s) => {
    const li = document.createElement('li');
    li.className = 'sub-item';
    li.dataset.id = s.id;

    const head = document.createElement('div');
    head.className = 'sub-item-head';
    const title = document.createElement('span');
    title.className = 'sub-item-title';
    title.textContent = s.name || s.platform || '订阅';
    const meta = document.createElement('span');
    meta.className = 'sub-item-meta';
    const checked = s.last_checked ? new Date(s.last_checked * 1000).toLocaleString() : '未检查';
    const known = Array.isArray(s.last_video_ids) ? s.last_video_ids.length : 0;
    meta.textContent = `${s.platform} · 已关注 ${known} 个视频 · 最近检查 ${checked}`;
    head.appendChild(title);
    head.appendChild(meta);

    const actions = document.createElement('div');
    actions.className = 'sub-item-actions';
    const checkBtn = document.createElement('button');
    checkBtn.type = 'button';
    checkBtn.className = 'btn btn-ghost btn-sm';
    checkBtn.textContent = '检查更新';
    checkBtn.addEventListener('click', () => checkSubscription(s.id));
    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.className = 'btn btn-ghost btn-sm';
    delBtn.textContent = '删除';
    delBtn.addEventListener('click', () => deleteSubscription(s.id));
    actions.appendChild(checkBtn);
    actions.appendChild(delBtn);

    li.appendChild(head);
    li.appendChild(actions);
    return li;
  };

  const addSubscription = async () => {
    const url = el.subUrl.value.trim();
    if (!url) { showSubHint('请粘贴频道主页链接', true); return; }
    el.subAddBtn.disabled = true;
    try {
      const data = await request('/api/subscriptions', {
        method: 'POST',
        body: JSON.stringify({
          url,
          name: el.subName.value.trim(),
          quality: el.subQuality.value,
          auto_check: el.subAuto.checked,
        }),
      });
      const known = Array.isArray(data.last_video_ids) ? data.last_video_ids.length : 0;
      showSubHint(`已订阅「${data.name || data.platform}」，记录 ${known} 个已有视频；之后发布的新视频将自动下载`, false);
      el.subUrl.value = '';
      el.subName.value = '';
      loadSubscriptions();
    } catch (e) {
      showSubHint(e.message || '添加订阅失败', true);
    } finally {
      el.subAddBtn.disabled = false;
    }
  };

  const checkSubscription = async (id) => {
    try {
      const data = await request(`/api/subscriptions/${id}/check`, { method: 'POST' });
      const n = (data.new_videos || []).length;
      const tid = (data.task_ids || []).length;
      if (n === 0) {
        showSubHint('已是最新，没有新视频', false);
      } else {
        showSubHint(`发现 ${n} 个新视频，已加入下载队列（${tid} 个任务）`, false);
        switchView('download');
      }
      loadSubscriptions();
    } catch (e) {
      showSubHint(e.message || '检查失败', true);
    }
  };

  const deleteSubscription = async (id) => {
    if (!window.confirm('确定取消该订阅？已下载的视频不会删除。')) return;
    try {
      await request(`/api/subscriptions/${id}`, { method: 'DELETE' });
      loadSubscriptions();
    } catch (e) {
      showSubHint(e.message || '删除失败', true);
    }
  };

  const showSubHint = (msg, isError) => {
    el.subHint.hidden = false;
    el.subHint.textContent = msg;
    el.subHint.classList.toggle('is-error', !!isError);
  };

  const createLibCard = (item) => {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'lib-card selectable';
    card.setAttribute('aria-label', item.title || item.name);

    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.className = 'lib-check';
    cb.dataset.libId = item.id;
    cb.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleLibSelect(item.id, card, cb);
    });

    const thumbBox = document.createElement('div');
    thumbBox.className = 'lib-thumb';
    if (item.kind === 'video') {
      const img = document.createElement('img');
      img.loading = 'lazy';
      img.alt = '';
      img.src = libThumbUrl(item.id);
      img.onerror = () => { img.remove(); };
      thumbBox.appendChild(img);
    }
    const fallback = document.createElement('span');
    fallback.className = 'lib-thumb-fallback';
    fallback.textContent = item.encrypted ? '🔒' : (item.kind === 'video' ? '🎬' : '🎵');
    thumbBox.appendChild(fallback);
    if (item.encrypted) {
      const lock = document.createElement('span');
      lock.className = 'lib-lock-badge';
      lock.textContent = '🔒 已加密';
      thumbBox.appendChild(lock);
    }
    if (item.duration) {
      const dur = document.createElement('span');
      dur.className = 'lib-duration';
      dur.textContent = formatDuration(item.duration);
      thumbBox.appendChild(dur);
    }

    const metaBox = document.createElement('div');
    metaBox.className = 'lib-card-meta';
    const title = document.createElement('span');
    title.className = 'lib-card-title';
    title.textContent = item.title || item.name;
    const sub = document.createElement('span');
    sub.className = 'lib-card-sub';
    const parts = [item.platform, formatBytes(item.size), new Date(item.mtime * 1000).toLocaleDateString()].filter(Boolean);
    sub.textContent = parts.join(' · ');
    metaBox.append(title, sub);

    card.append(cb, thumbBox, metaBox);
    card.addEventListener('click', () => openLibModal(item));
    return card;
  };

  const openLibModal = (item) => {
    currentLibItem = item;
    el.libPlayer.replaceChildren();
    const isEnc = !!item.encrypted;
    const fileUrl = isEnc ? libEncFileUrl(item.id) : libFileUrl(item.id);
    if (item.kind === 'video') {
      const v = document.createElement('video');
      v.src = fileUrl;
      v.controls = true;
      v.preload = 'metadata';
      v.className = 'lib-video';
      el.libPlayer.appendChild(v);
    } else if (item.kind === 'image') {
      const img = document.createElement('img');
      img.src = fileUrl;
      img.className = 'lib-image';
      el.libPlayer.appendChild(img);
    } else {
      const a = document.createElement('audio');
      a.src = fileUrl;
      a.controls = true;
      a.className = 'lib-audio';
      el.libPlayer.appendChild(a);
    }
    el.libMeta.replaceChildren();
    const rows = [
      ['标题', item.title || item.name],
      ['平台', item.platform || '—'],
      ['作者', item.uploader || '—'],
      ['时长', item.duration ? formatDuration(item.duration) : '—'],
      ['大小', formatBytes(item.size)],
      ['下载于', new Date(item.mtime * 1000).toLocaleString()],
    ];
    rows.forEach(([k, v]) => {
      const row = document.createElement('div');
      row.className = 'lib-meta-row';
      const kk = document.createElement('span'); kk.className = 'lib-meta-k'; kk.textContent = k;
      const vv = document.createElement('span'); vv.className = 'lib-meta-v'; vv.textContent = String(v);
      row.append(kk, vv);
      el.libMeta.appendChild(row);
    });
    if (isEnc) {
      const row = document.createElement('div');
      row.className = 'lib-meta-row';
      const kk = document.createElement('span'); kk.className = 'lib-meta-k'; kk.textContent = '保险箱';
      const vv = document.createElement('span'); vv.className = 'lib-meta-v'; vv.textContent = '🔒 已加密存放';
      row.append(kk, vv);
      el.libMeta.appendChild(row);
    }
    el.libDownload.href = fileUrl;
    el.libDownload.setAttribute('download', item.name || 'video');
    if (typeof el.libModal.showModal === 'function') el.libModal.showModal();
    else el.libModal.setAttribute('open', '');
    const showSub = node.libraryEnabled && item.kind === 'video';
    el.libSubtitle.hidden = !showSub;
    el.libProcess.hidden = !node.libraryEnabled;
    // 媒体库现成视频生成解说：统一先出脚本，展示人工审核面板后再渲染成片
    const showCommentary = node.commentaryEnabled && item.kind === 'video' && !isEnc;
    el.libCommentary.hidden = !showCommentary;
    el.libCommentary.disabled = false;
    el.libCommentary.textContent = '生成解说成片';
    el.libCommentaryStatus.hidden = true;
    el.libCommentaryStatus.textContent = '';
    el.libCommentaryFile.hidden = true;
    resetSubPanel();
    resetProcessPanel();
  };

  const deleteLibItem = async () => {
    if (!currentLibItem) return;
    if (!window.confirm('确定从磁盘删除这个文件吗？此操作不可恢复。')) return;
    try {
      await request(`/api/library/${encodeURIComponent(currentLibItem.id)}`, { method: 'DELETE' });
      if (typeof el.libModal.close === 'function') el.libModal.close();
      loadLibrary();
    } catch (e) {
      window.alert('删除失败：' + (e.message || '未知错误'));
    }
  };

  // ---- 字幕处理（桌面版功能） ----
  let extractedSubs = [];
  let selectedSub = null;

  const resetSubPanel = () => {
    extractedSubs = [];
    selectedSub = null;
    el.subPanel.hidden = true;
    el.subStatus.hidden = true;
    el.subExtractRow.hidden = true;
    el.subLang.replaceChildren();
    const def = document.createElement('option');
    def.value = ''; def.textContent = '选择语言';
    el.subLang.appendChild(def);
    el.subExtractList.replaceChildren();
  };

  const showSubStatus = (msg, isError) => {
    el.subStatus.hidden = false;
    el.subStatus.textContent = msg;
    el.subStatus.classList.toggle('is-error', !!isError);
  };

  const toggleSubPanel = () => { el.subPanel.hidden = !el.subPanel.hidden; };

  async function probeSubtitles() {
    if (!currentLibItem) return;
    try {
      const data = await request('/api/subtitles/list', {
        method: 'POST',
        body: JSON.stringify({ lib_id: currentLibItem.id, cookie: el.subCookie.value.trim() }),
      });
      const subs = data.subs || [];
      el.subLang.replaceChildren();
      const def = document.createElement('option');
      def.value = ''; def.textContent = subs.length ? '选择语言' : '无可用字幕';
      el.subLang.appendChild(def);
      subs.forEach((s) => {
        const o = document.createElement('option');
        o.value = s.lang;
        o.textContent = `${s.lang} · ${s.name}${s.auto ? '（自动生成）' : ''}`;
        el.subLang.appendChild(o);
      });
      el.subExtractRow.hidden = false;
      if (subs.length === 0) showSubStatus('未探测到在线字幕；可直接点「提取字幕」（语言留空）尝试抽取内嵌字幕流。', false);
    } catch (e) {
      showSubStatus(e.message || '探测失败', true);
    }
  };

  async function extractSubtitle() {
    if (!currentLibItem) return;
    const lang = el.subLang.value;
    try {
      const data = await request('/api/subtitles/extract', {
        method: 'POST',
        body: JSON.stringify({ lib_id: currentLibItem.id, lang, cookie: el.subCookie.value.trim() }),
      });
      extractedSubs.push({ sub_rel: data.sub_rel, lang: data.lang || lang, size: data.size });
      selectedSub = data.sub_rel;
      renderSubList();
      showSubStatus(`已提取字幕：${data.sub_rel}`, false);
    } catch (e) {
      showSubStatus(e.message || '提取失败', true);
    }
  };

  const renderSubList = () => {
    el.subExtractList.replaceChildren();
    if (extractedSubs.length === 0) return;
    extractedSubs.forEach((s) => {
      const li = document.createElement('li');
      li.className = 'sub-item' + (selectedSub === s.sub_rel ? ' is-selected' : '');
      li.textContent = `${s.sub_rel}（${s.lang}）`;
      li.addEventListener('click', () => { selectedSub = s.sub_rel; renderSubList(); });
      el.subExtractList.appendChild(li);
    });
  };

  async function translateSubtitle() {
    if (!currentLibItem) return;
    if (!selectedSub) { showSubStatus('请先在上方选择一个字幕文件', true); return; }
    try {
      const data = await request('/api/subtitles/translate', {
        method: 'POST',
        body: JSON.stringify({
          lib_id: currentLibItem.id, sub_rel: selectedSub,
          api_key: el.subApiKey.value.trim(), base_url: el.subBaseUrl.value.trim(),
          model: el.subModel.value.trim(), target: el.subTarget.value.trim() || '简体中文',
        }),
      });
      extractedSubs.push({ sub_rel: data.sub_rel, lang: data.lang || '中', size: 0 });
      selectedSub = data.sub_rel;
      renderSubList();
      showSubStatus(`已翻译并生成 ${data.sub_rel}（可立即烧录）`, false);
    } catch (e) {
      showSubStatus(e.message || '翻译失败', true);
    }
  };

  async function burnSubtitle() {
    if (!currentLibItem) return;
    if (!selectedSub) { showSubStatus('请先选择一个字幕文件（提取或翻译后）', true); return; }
    try {
      const data = await request('/api/subtitles/burn', {
        method: 'POST',
        body: JSON.stringify({ lib_id: currentLibItem.id, sub_rel: selectedSub }),
      });
      showSubStatus(`已生成字幕版视频：${data.name}（去「媒体库」刷新即可看到）`, false);
    } catch (e) {
      showSubStatus(e.message || '烧录失败', true);
    }
  };

  el.tabDownload.addEventListener('click', () => switchView('download'));
  el.tabLibrary.addEventListener('click', () => switchView('library'));
  el.tabCommentary.addEventListener('click', () => switchView('commentary'));
  el.tabSubscribe.addEventListener('click', () => switchView('subscribe'));
  el.tabTorrent.addEventListener('click', () => switchView('torrent'));
  el.subAddBtn.addEventListener('click', addSubscription);
  // ---- 时效自动清理：预览 → 确认 → 执行。媒体档强制二次确认 + 回收站 ----
  const CLEAN_LABELS = {
    temp: '中断下载的临时碎片',
    frames: '批量抽帧目录',
    thumbs: '缩略图缓存',
    media: '媒体文件（超过保留期）',
    quota: '媒体文件（超出容量上限）',
  };
  const DANGER_CATS = ['media', 'quota'];
  let cleanPlan = null;

  const fmtSize = (n) => {
    let v = Number(n) || 0;
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    return i === 0 ? `${Math.round(v)} B` : `${v.toFixed(1)} ${units[i]}`;
  };

  const showCleanStatus = (msg, isErr = false) => {
    el.cleanStatus.textContent = msg || '';
    el.cleanStatus.classList.toggle('is-error', !!isErr);
  };

  const applyTrashLock = () => {
    const locked = !node.trashAvailable;
    el.cleanTrashWarn.hidden = !locked;
    [el.cleanMediaOn, el.cleanQuotaOn].forEach((cb) => {
      cb.disabled = locked;
      if (locked) cb.checked = false;
    });
    el.cleanMediaDays.disabled = locked;
    el.cleanQuotaGb.disabled = locked;
  };

  const fillCleanForm = (cfg) => {
    el.cleanAuto.checked = !!cfg.auto_enabled;
    el.cleanInterval.value = cfg.interval_hours ?? 6;
    el.cleanTempOn.checked = !!cfg.temp_enabled;
    el.cleanTempDays.value = cfg.temp_days ?? 2;
    el.cleanFramesOn.checked = !!cfg.frames_enabled;
    el.cleanFramesDays.value = cfg.frames_days ?? 7;
    el.cleanThumbsOn.checked = !!cfg.thumbs_enabled;
    el.cleanThumbsDays.value = cfg.thumbs_days ?? 30;
    el.cleanMediaOn.checked = !!cfg.media_enabled;
    el.cleanMediaDays.value = cfg.media_days ?? 30;
    el.cleanQuotaOn.checked = !!cfg.quota_enabled;
    el.cleanQuotaGb.value = cfg.quota_gb ?? 20;
    applyTrashLock();
  };

  const collectCleanConfig = () => ({
    auto_enabled: el.cleanAuto.checked,
    interval_hours: Number(el.cleanInterval.value) || 6,
    temp_enabled: el.cleanTempOn.checked,
    temp_days: Number(el.cleanTempDays.value) || 0,
    frames_enabled: el.cleanFramesOn.checked,
    frames_days: Number(el.cleanFramesDays.value) || 0,
    thumbs_enabled: el.cleanThumbsOn.checked,
    thumbs_days: Number(el.cleanThumbsDays.value) || 0,
    media_enabled: el.cleanMediaOn.checked,
    media_days: Number(el.cleanMediaDays.value) || 30,
    quota_enabled: el.cleanQuotaOn.checked,
    quota_gb: Number(el.cleanQuotaGb.value) || 20,
  });

  const paintUsage = (usage) => {
    if (!usage) return;
    el.cleanUsage.textContent =
      `下载目录已占用 ${fmtSize(usage.dir_size)}，磁盘剩余 ${fmtSize(usage.disk_free)} · ${usage.path}`;
  };

  const openCleanModal = async () => {
    cleanPlan = null;
    el.cleanPreview.hidden = true;
    el.cleanPreview.replaceChildren();
    el.cleanRun.disabled = true;
    showCleanStatus('');
    el.cleanUsage.textContent = '正在读取磁盘占用…';
    if (typeof el.cleanModal.showModal === 'function') el.cleanModal.showModal();
    try {
      const data = await request('/api/retention/config');
      node.trashAvailable = !!data.trash_available;
      fillCleanForm(data.config || {});
      paintUsage(data.usage);
    } catch (err) {
      el.cleanUsage.textContent = '';
      showCleanStatus(err.message || '读取清理设置失败', true);
    }
  };

  const saveClean = async () => {
    el.cleanSave.disabled = true;
    showCleanStatus('保存中…');
    try {
      await request('/api/retention/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(collectCleanConfig()),
      });
      showCleanStatus('设置已保存');
    } catch (err) {
      showCleanStatus(err.message || '保存失败', true);
    } finally {
      el.cleanSave.disabled = false;
    }
  };

  const renderCleanPreview = (plan) => {
    el.cleanPreview.replaceChildren();
    const cats = plan.categories || {};
    const rows = Object.entries(cats).filter(([, v]) => (v.count || 0) > 0);
    if (!rows.length) {
      const p = document.createElement('p');
      p.className = 'clean-empty';
      p.textContent = '按当前设置，没有需要清理的东西。';
      el.cleanPreview.appendChild(p);
      el.cleanPreview.hidden = false;
      el.cleanRun.disabled = true;
      return;
    }
    const head = document.createElement('p');
    head.className = 'clean-total';
    head.textContent = `共 ${plan.total_files} 项，可释放约 ${fmtSize(plan.total_size)}`;
    el.cleanPreview.appendChild(head);

    rows.forEach(([cat, info]) => {
      const box = document.createElement('div');
      box.className = 'clean-cat' + (DANGER_CATS.includes(cat) ? ' clean-cat-danger' : '');
      const title = document.createElement('p');
      title.className = 'clean-cat-title';
      title.textContent = `${CLEAN_LABELS[cat] || cat} · ${info.count} 项 · ${fmtSize(info.size)}`
        + (DANGER_CATS.includes(cat) ? '（移入回收站）' : '');
      box.appendChild(title);
      const list = document.createElement('ul');
      list.className = 'clean-cat-list';
      (info.items || []).slice(0, 8).forEach((it) => {
        const li = document.createElement('li');
        li.textContent = `${it.rel}${it.is_dir ? '/' : ''} · ${fmtSize(it.size)} · ${it.age_days} 天前`;
        list.appendChild(li);
      });
      if (info.count > 8) {
        const li = document.createElement('li');
        li.className = 'clean-more';
        li.textContent = `…另有 ${info.count - 8} 项`;
        list.appendChild(li);
      }
      box.appendChild(list);
      el.cleanPreview.appendChild(box);
    });
    el.cleanPreview.hidden = false;
    el.cleanRun.disabled = false;
  };

  const scanClean = async () => {
    el.cleanScan.disabled = true;
    el.cleanRun.disabled = true;
    showCleanStatus('正在扫描…');
    try {
      // 先落盘当前表单，保证预览用的就是屏幕上这套设置
      await request('/api/retention/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(collectCleanConfig()),
      });
      cleanPlan = await request('/api/retention/scan', { method: 'POST' });
      paintUsage(cleanPlan.usage);
      renderCleanPreview(cleanPlan);
      showCleanStatus('');
    } catch (err) {
      showCleanStatus(err.message || '扫描失败', true);
    } finally {
      el.cleanScan.disabled = false;
    }
  };

  const runClean = async () => {
    if (!cleanPlan) { showCleanStatus('请先点「预览将清理什么」', true); return; }
    const cats = Object.entries(cleanPlan.categories || {})
      .filter(([, v]) => (v.count || 0) > 0)
      .map(([k]) => k);
    if (!cats.length) { showCleanStatus('没有需要清理的内容'); return; }
    const dangerous = cats.filter((c) => DANGER_CATS.includes(c));
    const total = cleanPlan.total_files;
    let msg = `确定清理 ${total} 项、释放约 ${fmtSize(cleanPlan.total_size)}？`;
    if (dangerous.length) {
      const n = dangerous.reduce((s, c) => s + (cleanPlan.categories[c].count || 0), 0);
      msg += `\n\n⚠️ 其中 ${n} 个是你的媒体文件，会连同字幕/元信息一起移入系统回收站（可从回收站找回）。`;
    }
    if (!window.confirm(msg)) return;

    el.cleanRun.disabled = true;
    showCleanStatus('正在清理…');
    try {
      const res = await request('/api/retention/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ categories: cats }),
      });
      paintUsage(res.usage);
      let text = `已清理 ${res.removed} 项，释放 ${res.freed_text || fmtSize(res.freed)}`;
      if (res.failed) text += `，${res.failed} 项失败`;
      showCleanStatus(text, !!res.failed);
      if ((res.errors || []).length) {
        const box = document.createElement('div');
        box.className = 'clean-errors';
        res.errors.forEach((e) => {
          const p = document.createElement('p');
          p.textContent = e;
          box.appendChild(p);
        });
        el.cleanPreview.appendChild(box);
      }
      cleanPlan = null;
      loadLibrary();
    } catch (err) {
      showCleanStatus(err.message || '清理失败', true);
    }
  };

  el.libCleanup.addEventListener('click', openCleanModal);
  el.cleanModalClose.addEventListener('click', () => {
    if (typeof el.cleanModal.close === 'function') el.cleanModal.close();
  });
  el.cleanSave.addEventListener('click', saveClean);
  el.cleanScan.addEventListener('click', scanClean);
  el.cleanRun.addEventListener('click', runClean);

  // ================================ 归档网盘 ================================
  const arcState = { jobId: null, pollTimer: null, items: [] };

  const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const showArcStatus = (msg, isErr = false) => {
    el.arcStatus.textContent = msg || '';
    el.arcStatus.classList.toggle('is-error', !!isErr);
  };

  const currentArcProvider = () =>
    (document.querySelector('input[name="arcProvider"]:checked') || {}).value || 'webdav';

  const toggleArcProviderForm = (prov) => {
    el.arcWebdavForm.hidden = prov !== 'webdav';
    el.arcBaiduForm.hidden = prov !== 'baidu';
  };

  const fillArcForm = (data) => {
    const cfg = data.config || {};
    el.arcTemplate.value = cfg.dest_template || '';
    el.arcVideo.checked = !!cfg.include_video;
    el.arcAudio.checked = !!cfg.include_audio;
    el.arcImage.checked = !!cfg.include_image;
    el.arcMinAge.value = cfg.min_age_minutes ?? 3;
    el.arcMaxGb.value = cfg.max_file_gb ?? 10;
    el.arcDeleteAfter.checked = !!cfg.delete_after;
    el.arcAuto.checked = !!cfg.auto_enabled;
    el.arcInterval.value = cfg.interval_hours ?? 6;
    const wd = (data.creds && data.creds.webdav) || {};
    el.arcWebdavUrl.value = wd.url || '';
    el.arcWebdavUser.value = wd.user || '';
    el.arcWebdavPass.value = '';
    const bd = (data.creds && data.creds.baidu) || {};
    el.arcBaiduStatus.textContent = bd.token_set ? '已授权' : '未授权';
    const toks = data.tokens || {};
    el.arcTokens.replaceChildren();
    const tip = document.createElement('span');
    tip.textContent = '可用占位符：';
    el.arcTokens.appendChild(tip);
    Object.entries(toks).forEach(([k, v], i, arr) => {
      const code = document.createElement('code');
      code.textContent = k;
      code.title = v;
      el.arcTokens.appendChild(code);
      if (i < arr.length - 1) el.arcTokens.appendChild(document.createTextNode(' '));
    });
    el.arcTrashWarn.hidden = !!data.trash_available;
    el.arcDeleteAfter.disabled = !data.trash_available;
    if (!data.trash_available) el.arcDeleteAfter.checked = false;
    el.arcBaiduRadio.hidden = !node.archiveBaiduAvailable;
    const prov = cfg.provider || 'webdav';
    const radio = document.querySelector(`input[name="arcProvider"][value="${prov}"]`);
    if (radio) radio.checked = true;
    toggleArcProviderForm(prov);
  };

  const renderArcRecords = (records) => {
    el.arcRecords.replaceChildren();
    (records || []).forEach((r) => {
      const li = document.createElement('li');
      const name = (r.rel || r.remote || '').split('/').pop();
      li.textContent = `${name} → ${r.remote || ''} · ${fmtSize(r.size || 0)}`;
      el.arcRecords.appendChild(li);
    });
    if (!(records || []).length) {
      const li = document.createElement('li');
      li.className = 'arc-records-empty';
      li.textContent = '暂无归档记录';
      el.arcRecords.appendChild(li);
    }
  };

  const collectArcConfig = () => {
    const prov = currentArcProvider();
    const body = {
      provider: prov,
      dest_template: el.arcTemplate.value.trim(),
      include_video: el.arcVideo.checked,
      include_audio: el.arcAudio.checked,
      include_image: el.arcImage.checked,
      min_age_minutes: Number(el.arcMinAge.value) || 0,
      max_file_gb: Number(el.arcMaxGb.value) || 0,
      delete_after: el.arcDeleteAfter.checked,
      auto_enabled: el.arcAuto.checked,
      interval_hours: Number(el.arcInterval.value) || 6,
    };
    if (prov === 'webdav') {
      body.webdav = {
        url: el.arcWebdavUrl.value.trim(),
        user: el.arcWebdavUser.value.trim(),
        pass: el.arcWebdavPass.value,
      };
    } else if (prov === 'baidu') {
      body.baidu = { token: el.arcBaiduToken.value.trim() };
    }
    return body;
  };

  const openArchiveModal = async () => {
    arcState.jobId = null;
    stopArcPoll();
    el.arcPreview.hidden = true;
    el.arcPreview.replaceChildren();
    el.arcRun.disabled = true;
    el.arcCancel.hidden = true;
    showArcStatus('');
    if (typeof el.archiveModal.showModal === 'function') el.archiveModal.showModal();
    try {
      const data = await request('/api/archive/config');
      fillArcForm(data);
      renderArcRecords(data.records);
    } catch (err) {
      showArcStatus(err.message || '读取归档设置失败', true);
    }
  };

  const saveArcConfig = async () => {
    el.arcSave.disabled = true;
    showArcStatus('保存中…');
    try {
      await request('/api/archive/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(collectArcConfig()),
      });
      showArcStatus('配置已保存');
      node.archiveConfigured = true;
    } catch (err) {
      showArcStatus(err.message || '保存失败', true);
    } finally {
      el.arcSave.disabled = false;
    }
  };

  const scanArc = async () => {
    el.arcScan.disabled = true;
    el.arcRun.disabled = true;
    showArcStatus('正在扫描…');
    try {
      const data = await request('/api/archive/scan', { method: 'POST' });
      arcState.items = data.items || [];
      renderArcPreview(data);
      showArcStatus('');
    } catch (err) {
      showArcStatus(err.message || '扫描失败', true);
    } finally {
      el.arcScan.disabled = false;
    }
  };

  const renderArcPreview = (data) => {
    el.arcPreview.replaceChildren();
    const items = data.items || [];
    if (!items.length) {
      const p = document.createElement('p');
      p.className = 'arc-empty';
      p.textContent = data.configured
        ? '没有待归档的文件（全部已归档，或都不符合筛选条件）。'
        : '尚未配置网盘凭据，请先填写上方 WebDAV / 百度网盘信息并保存。';
      el.arcPreview.appendChild(p);
      el.arcPreview.hidden = false;
      el.arcRun.disabled = true;
      return;
    }
    const head = document.createElement('p');
    head.className = 'arc-total';
    head.textContent = `共 ${data.count} 项待归档，约 ${data.size_text || fmtSize(data.size || 0)}`;
    el.arcPreview.appendChild(head);
    const list = document.createElement('ul');
    list.className = 'arc-list';
    items.forEach((it) => {
      const li = document.createElement('li');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = true;
      cb.dataset.id = it.id;
      cb.className = 'arc-item-cb';
      const label = document.createElement('label');
      label.className = 'arc-item';
      const span = document.createElement('span');
      span.innerHTML = `<strong>${escapeHtml(it.name)}</strong> → <code>${escapeHtml(it.dest)}</code> · ${fmtSize(it.size || 0)}`;
      label.appendChild(cb);
      label.appendChild(span);
      li.appendChild(label);
      list.appendChild(li);
    });
    el.arcPreview.appendChild(list);
    el.arcPreview.hidden = false;
    el.arcRun.disabled = false;
  };

  const selectedArcIds = () =>
    Array.from(el.arcPreview.querySelectorAll('.arc-item-cb:checked')).map((cb) => cb.dataset.id);

  const runArc = async () => {
    const ids = selectedArcIds();
    if (!ids.length) { showArcStatus('请至少勾选一个文件', true); return; }
    el.arcRun.disabled = true;
    el.arcScan.disabled = true;
    el.arcCancel.hidden = false;
    el.arcCancel.disabled = false;
    showArcStatus('正在归档…');
    try {
      const res = await request('/api/archive/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lib_ids: ids }),
      });
      arcState.jobId = res.job_id;
      pollArcStatus();
    } catch (err) {
      showArcStatus(err.message || '归档启动失败', true);
      el.arcCancel.hidden = true;
      el.arcRun.disabled = false;
      el.arcScan.disabled = false;
    }
  };

  const stopArcPoll = () => {
    if (arcState.pollTimer) { clearTimeout(arcState.pollTimer); arcState.pollTimer = null; }
  };

  const pollArcStatus = async () => {
    if (!arcState.jobId) return;
    try {
      const s = await request(`/api/archive/status/${arcState.jobId}`);
      const total = s.total || 0;
      const done = (s.uploaded || 0) + (s.failed || 0) + (s.skipped || 0);
      const pct = total ? Math.round((done / total) * 100) : 100;
      showArcStatus(
        `归档中 ${done}/${total}（${s.uploaded || 0} 成功 / ${s.failed || 0} 失败）… `
        + `${s.current || ''} ${Math.round(s.file_percent || 0)}%`);
      if (s.status === 'running') {
        arcState.pollTimer = setTimeout(pollArcStatus, 800);
      } else {
        finishArc(s);
      }
    } catch (err) {
      showArcStatus(err.message || '查询进度失败', true);
      el.arcCancel.hidden = true;
      el.arcRun.disabled = false;
      el.arcScan.disabled = false;
    }
  };

  const finishArc = async (s) => {
    el.arcCancel.hidden = true;
    el.arcScan.disabled = false;
    el.arcRun.disabled = false;
    let text = `归档完成：成功 ${s.uploaded || 0} 个 / ${s.bytes_text || fmtSize(s.bytes || 0)}`;
    if (s.failed) text += `，失败 ${s.failed} 个`;
    if (s.skipped) text += `，跳过 ${s.skipped} 个`;
    if (s.deleted) text += `，已移入回收站 ${s.deleted} 个`;
    showArcStatus(text, !!s.failed);
    arcState.jobId = null;
    try {
      const data = await request('/api/archive/config');
      renderArcRecords(data.records);
    } catch { /* ignore */ }
    try {
      const sc = await request('/api/archive/scan', { method: 'POST' });
      arcState.items = sc.items || [];
      renderArcPreview(sc);
    } catch { /* ignore */ }
  };

  const cancelArc = async () => {
    if (!arcState.jobId) return;
    el.arcCancel.disabled = true;
    showArcStatus('正在取消…');
    try {
      await request(`/api/archive/cancel/${arcState.jobId}`, { method: 'POST' });
    } catch { /* ignore */ }
  };

  const forgetArc = async () => {
    if (!window.confirm('确定清空归档记录吗？清空后这些文件下次会重新上传到网盘。')) return;
    try {
      await request('/api/archive/forget', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rel: '' }),
      });
      showArcStatus('归档记录已清空');
      const sc = await request('/api/archive/scan', { method: 'POST' });
      arcState.items = sc.items || [];
      renderArcPreview(sc);
    } catch (err) {
      showArcStatus(err.message || '清空失败', true);
    }
  };

  document.querySelectorAll('input[name="arcProvider"]').forEach((r) => {
    r.addEventListener('change', () => {
      toggleArcProviderForm(r.value);
      try {
        request('/api/archive/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ provider: r.value }),
        });
      } catch { /* ignore */ }
    });
  });
  el.libArchive.addEventListener('click', openArchiveModal);
  el.archiveModalClose.addEventListener('click', () => {
    stopArcPoll();
    if (typeof el.archiveModal.close === 'function') el.archiveModal.close();
  });
  el.archiveModal.addEventListener('click', (e) => { if (e.target === el.archiveModal) el.archiveModal.close(); });
  el.arcSave.addEventListener('click', saveArcConfig);
  el.arcScan.addEventListener('click', scanArc);
  el.arcRun.addEventListener('click', runArc);
  el.arcCancel.addEventListener('click', cancelArc);
  el.arcForget.addEventListener('click', forgetArc);
  if (el.arcBaiduBtn) el.arcBaiduBtn.addEventListener('click', () => {
    if (node.baiduAuthUrl) openBaiduAuthInPage(node.baiduAuthUrl);
  });

  // ---- 库内保险箱（桌面版功能） ----
  let cryptoItems = [];
  let cryptoPollTimer = null;

  const openCryptoModal = async () => {
    if (!node.cryptoEnabled) return;
    if (typeof el.cryptoModal.showModal === 'function') el.cryptoModal.showModal();
    else el.cryptoModal.setAttribute('open', '');
    await refreshCrypto();
  };

  const setCryptoView = (view) => {
    el.cryptoView.querySelectorAll('[data-view]').forEach((d) => { d.hidden = d.dataset.view !== view; });
  };

  const setMsg = (elem, text, isErr) => {
    if (!elem) return;
    elem.hidden = !text;
    elem.textContent = text || '';
    elem.className = 'arc-msg' + (isErr ? ' is-err' : ' is-ok');
  };

  const refreshCrypto = async () => {
    let st;
    try { st = await request('/api/crypto/status'); }
    catch (e) { setMsg(el.cryptoStatus, '读取保险箱状态失败：' + (e.message || ''), true); return; }
    node.cryptoHasPass = !!st.has_pass;
    node.cryptoLocked = !!st.locked;
    setCryptoView(st.has_pass ? (st.locked ? 'unlock' : 'open') : 'set');
    if (!st.locked) loadCryptoList();
  };

  const loadCryptoList = async () => {
    if (node.cryptoLocked) return;
    let data;
    try { data = await request('/api/library'); }
    catch (e) { return; }
    cryptoItems = data.items || [];
    renderCryptoList();
  };

  const renderCryptoList = () => {
    const f = el.cryptoFilter ? el.cryptoFilter.value : 'all';
    const list = cryptoItems.filter((it) => {
      if (f === 'plain') return !it.encrypted;
      if (f === 'enc') return !!it.encrypted;
      return true;
    });
    el.cryptoList.replaceChildren();
    if (list.length === 0) {
      const li = document.createElement('li');
      li.className = 'arc-empty';
      li.textContent = '没有匹配的文件';
      el.cryptoList.appendChild(li);
      return;
    }
    list.forEach((it) => {
      const li = document.createElement('li');
      li.className = 'arc-item';
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.className = 'arc-item-cb';
      cb.dataset.id = it.id;
      const label = document.createElement('label');
      label.className = 'arc-item-label';
      const name = document.createElement('span');
      name.className = 'arc-item-name';
      name.textContent = (it.encrypted ? '🔒 ' : '') + (it.title || it.name);
      const meta = document.createElement('span');
      meta.className = 'arc-item-meta';
      meta.textContent = `${it.kind} · ${formatBytes(it.size)}`;
      label.append(name, meta);
      li.append(cb, label);
      el.cryptoList.appendChild(li);
    });
  };

  const reloadCurrentEncPlayer = () => {
    if (currentLibItem && currentLibItem.encrypted) {
      const m = el.libPlayer.querySelector('video, audio, img');
      if (m) m.src = libEncFileUrl(currentLibItem.id);
    }
  };

  const pollCryptoJob = (jobId, mode) => {
    if (cryptoPollTimer) clearInterval(cryptoPollTimer);
    el.cryptoJob.hidden = false;
    el.cryptoJob.className = 'arc-job';
    cryptoPollTimer = setInterval(async () => {
      let j;
      try { j = await request('/api/crypto/job/' + jobId); }
      catch (e) { clearInterval(cryptoPollTimer); return; }
      const done = j.done || 0, total = j.total || 0;
      let txt = `进度 ${done}/${total}` + (j.errors && j.errors.length ? ` · ${j.errors.length} 个出错` : '');
      el.cryptoJob.textContent = txt;
      if (j.status === 'completed' || j.status === 'failed' || j.status === 'canceled') {
        clearInterval(cryptoPollTimer);
        if (j.errors && j.errors.length) {
          txt += '\n' + j.errors.slice(0, 5).join('\n');
          el.cryptoJob.className = 'arc-job is-err';
        } else {
          el.cryptoJob.className = 'arc-job is-ok';
        }
        el.cryptoJob.textContent = txt;
        loadCryptoList();
        loadLibrary();
      }
    }, 800);
  };

  const cryptoRun = async (mode) => {
    const ids = Array.from(el.cryptoList.querySelectorAll('.arc-item-cb:checked')).map((cb) => cb.dataset.id);
    if (ids.length === 0) { setMsg(el.cryptoStatus, '请先勾选文件', true); return; }
    setMsg(el.cryptoStatus, '');
    try {
      const r = await request('/api/crypto/' + mode, { method: 'POST', body: { lib_ids: ids } });
      pollCryptoJob(r.job_id, mode);
    } catch (e) {
      setMsg(el.cryptoStatus, e.message || '操作失败', true);
      el.cryptoJob.hidden = true;
    }
  };

  // 按钮与输入绑定
  if (el.libCrypto) el.libCrypto.addEventListener('click', openCryptoModal);
  if (el.cryptoModalClose) el.cryptoModalClose.addEventListener('click', () => {
    if (cryptoPollTimer) clearInterval(cryptoPollTimer);
    if (typeof el.cryptoModal.close === 'function') el.cryptoModal.close();
  });
  if (el.cryptoModal) el.cryptoModal.addEventListener('click', (e) => { if (e.target === el.cryptoModal) el.cryptoModal.close(); });
  if (el.cryptoSetPass) el.cryptoSetPass.addEventListener('click', async () => {
    const pass = el.cryptoPass.value, confirm = el.cryptoConfirm.value;
    setMsg(el.cryptoSetMsg, '');
    if (!pass || pass.length < 4) { setMsg(el.cryptoSetMsg, '密码至少 4 位', true); return; }
    if (pass !== confirm) { setMsg(el.cryptoSetMsg, '两次输入不一致', true); return; }
    try {
      await request('/api/crypto/set-pass', { method: 'POST', body: { passwd: pass, confirm } });
      el.cryptoPass.value = ''; el.cryptoConfirm.value = '';
      setMsg(el.cryptoSetMsg, '已设置并解锁', false);
      node.cryptoHasPass = true; node.cryptoLocked = false;
      setCryptoView('open');
      loadCryptoList();
    } catch (e) { setMsg(el.cryptoSetMsg, e.message || '设置失败', true); }
  });
  if (el.cryptoUnlock) el.cryptoUnlock.addEventListener('click', async () => {
    const pass = el.cryptoUnlockPass.value;
    setMsg(el.cryptoUnlockMsg, '');
    try {
      await request('/api/crypto/unlock', { method: 'POST', body: { passwd: pass } });
      el.cryptoUnlockPass.value = '';
      node.cryptoLocked = false;
      setCryptoView('open');
      loadCryptoList();
      reloadCurrentEncPlayer();
    } catch (e) { setMsg(el.cryptoUnlockMsg, e.message || '解锁失败', true); }
  });
  if (el.cryptoLock) el.cryptoLock.addEventListener('click', async () => {
    try { await request('/api/crypto/lock', { method: 'POST' }); } catch (e) {}
    node.cryptoLocked = true;
    setCryptoView('unlock');
    el.cryptoList.replaceChildren();
    el.cryptoJob.hidden = true;
  });
  if (el.cryptoFilter) el.cryptoFilter.addEventListener('change', renderCryptoList);
  if (el.cryptoEncrypt) el.cryptoEncrypt.addEventListener('click', () => cryptoRun('encrypt'));
  if (el.cryptoDecrypt) el.cryptoDecrypt.addEventListener('click', () => cryptoRun('decrypt'));

  el.libRefresh.addEventListener('click', loadLibrary);
  el.libSearch.addEventListener('input', debounce(loadLibrary, 300));
  el.libPlatform.addEventListener('change', loadLibrary);
  el.libKind.addEventListener('change', loadLibrary);
  el.libModalClose.addEventListener('click', () => el.libModal.close());
  el.libModal.addEventListener('click', (e) => { if (e.target === el.libModal) el.libModal.close(); });
  el.libDelete.addEventListener('click', deleteLibItem);
  el.libSubtitle.addEventListener('click', toggleSubPanel);
  el.subPanelClose.addEventListener('click', () => { el.subPanel.hidden = true; });
  el.libCommentary.addEventListener('click', () => {
    if (!currentLibItem) return;
    // 预加载预览元数据，让「自动」画幅能拿到视频宽高判断横竖
    setupComPreview(`/api/library/file/${encodeURIComponent(currentLibItem.id)}`);
    createCommentary(
      { fileId: currentLibItem.id },
      { commentary: el.libCommentary, commentaryStatus: el.libCommentaryStatus, commentaryFile: el.libCommentaryFile },
      '',
    );
  });
  el.libProcess.addEventListener('click', toggleProcessPanel);
  el.processPanelClose.addEventListener('click', () => { el.processPanel.hidden = true; });
  el.processOp.addEventListener('change', renderProcessParams);
  el.processRun.addEventListener('click', runProcess);
  el.subProbe.addEventListener('click', probeSubtitles);
  el.subExtract.addEventListener('click', extractSubtitle);
  el.subTranslate.addEventListener('click', translateSubtitle);
  el.subBurn.addEventListener('click', burnSubtitle);

  // ---- LLM 服务商选择器 ----
  (async () => {
    // 加载提供商预设列表
    let providers = {};
    let defaultProvider = 'openai';
    try {
      const r = await request('/api/llm/providers');
      if (r.ok) {
        const d = await r.json();
        providers = d.providers || {};
        defaultProvider = d.default || 'openai';
      }
    } catch (e) { /* 未启用时静默退 */ }
    // 填充下拉菜单
    if (el.llmProvider) {
      el.llmProvider.innerHTML = '';
      for (const [k, v] of Object.entries(providers)) {
        const opt = document.createElement('option');
        opt.value = k;
        opt.textContent = v.name;
        el.llmProvider.appendChild(opt);
      }
      // 选自定义时显示 base_url 输入
      el.llmProvider.addEventListener('change', () => {
        const sel = el.llmProvider.value;
        el.llmBaseUrl.style.display = sel === 'custom' ? '' : 'none';
        // 选中预设后自动填 base_url 和 model
        const preset = providers[sel];
        if (preset) {
          if (preset.base_url) el.llmBaseUrl.value = preset.base_url;
          if (preset.default_model) el.llmModel.value = preset.default_model;
        }
      });
    }
    // 回填已保存的配置
    try {
      const r = await request('/api/llm/config');
      if (r.ok) {
        const cfg = await r.json();
        if (el.llmProvider) el.llmProvider.value = cfg.provider || defaultProvider;
        if (el.llmApiKey) el.llmApiKey.value = cfg.api_key || '';
        if (el.llmBaseUrl) el.llmBaseUrl.value = cfg.base_url || '';
        if (el.llmModel) el.llmModel.value = cfg.model || '';
        // 初始显示/隐藏 base_url
        if (el.llmBaseUrl) el.llmBaseUrl.style.display = (cfg.provider === 'custom') ? '' : 'none';
      }
    } catch (e) { /* */ }

    // 保存按钮
    if (el.llmSave) {
      el.llmSave.addEventListener('click', async () => {
        const body = {
          provider: el.llmProvider ? el.llmProvider.value : 'openai',
          api_key: el.llmApiKey ? el.llmApiKey.value.trim() : '',
          base_url: el.llmBaseUrl ? el.llmBaseUrl.value.trim() : '',
          model: el.llmModel ? el.llmModel.value.trim() : '',
        };
        try {
          const r = await request('/api/llm/config', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          });
          const d = await r.json();
          if (r.ok && d.ok) {
            if (el.llmStatus) {
              el.llmStatus.hidden = false;
              el.llmStatus.textContent = '✅ 已保存';
              setTimeout(() => { el.llmStatus.hidden = true; }, 2000);
            }
          } else {
            if (el.llmStatus) {
              el.llmStatus.hidden = false;
              el.llmStatus.style.color = '#e74c3c';
              el.llmStatus.textContent = '❌ 保存失败';
            }
          }
        } catch (e) {
          if (el.llmStatus) {
            el.llmStatus.hidden = false;
            el.llmStatus.style.color = '#e74c3c';
            el.llmStatus.textContent = '❌ 网络错误';
          }
        }
      });
    }
  })();

  // ---- 格式 / 片段加工（桌面版功能） ----
  // 操作类型定义：label 显示名、kinds 适用媒体类型、params 动态表单字段。
  const PROCESS_OPS = {
    audio:    { label: '提取音频', kinds: ['video', 'audio'], params: [
      { key: 'fmt', label: '格式', type: 'select', options: ['mp3', 'm4a', 'aac', 'opus', 'flac', 'wav'], def: 'mp3' },
      { key: 'bitrate', label: '码率', type: 'select', options: ['128k', '192k', '256k', '320k'], def: '192k' },
    ]},
    gif:      { label: '生成 GIF 动图', kinds: ['video'], params: [
      { key: 'start', label: '开始(秒)', type: 'number', def: 0 },
      { key: 'duration', label: '时长(秒)', type: 'number', def: 5 },
      { key: 'fps', label: '帧率', type: 'number', def: 12 },
      { key: 'width', label: '宽度(px)', type: 'number', def: 480 },
    ]},
    trim:     { label: '时间裁剪', kinds: ['video'], params: [
      { key: 'start', label: '开始(秒)', type: 'number', def: 0 },
      { key: 'end', label: '结束(秒，0=剪到末尾)', type: 'number', def: 0 },
      { key: 'reencode', label: '精确重编码', type: 'checkbox', def: true },
    ]},
    crop:     { label: '画面裁剪', kinds: ['video'], params: [
      { key: 'preset', label: '比例预设', type: 'select', options: ['自由', '9:16 竖屏', '1:1 方形', '4:3', '16:9'], def: '自由' },
      { key: 'crop_expr', label: 'Crop 表达式(自由时填，如 iw/2:ih:0:0；宽高需偶数)', type: 'text', def: '' },
    ]},
    compress: { label: '压缩', kinds: ['video'], params: [
      { key: 'scale_h', label: '目标高度', type: 'select', options: ['480', '720', '1080'], def: '720' },
      { key: 'crf', label: '质量(数值越大越压)', type: 'select', options: ['23', '28', '32'], def: '28' },
    ]},
    upscale:  { label: '放大 / 轻量超分', kinds: ['video'], params: [
      { key: 'factor', label: '放大倍率', type: 'select', options: ['1.5', '2', '4'], def: '2' },
      { key: 'sharpen', label: '锐化', type: 'checkbox', def: true },
    ]},
    frame:    { label: '抽帧封面（单张）', kinds: ['video'], params: [
      { key: 'at', label: '时间点(秒)', type: 'number', def: 1 },
      { key: 'fmt', label: '图片格式', type: 'select', options: ['jpg', 'png', 'webp'], def: 'jpg' },
      { key: 'width', label: '宽度(px，0=原始)', type: 'number', def: 0 },
    ]},
    frames:   { label: '批量抽帧（存子目录）', kinds: ['video'], params: [
      { key: 'start', label: '开始(秒)', type: 'number', def: 0 },
      { key: 'end', label: '结束(秒，0=到末尾)', type: 'number', def: 0 },
      { key: 'interval', label: '每隔几秒抽一帧', type: 'number', def: 1 },
      { key: 'limit', label: '最多抽多少帧', type: 'number', def: 100 },
      { key: 'fmt', label: '图片格式', type: 'select', options: ['jpg', 'png'], def: 'jpg' },
      { key: 'width', label: '宽度(px，0=原始)', type: 'number', def: 0 },
    ]},
    sheet:    { label: '预览图（九宫格拼图）', kinds: ['video'], params: [
      { key: 'rows', label: '行数', type: 'select', options: ['2', '3', '4', '5'], def: '3' },
      { key: 'cols', label: '列数', type: 'select', options: ['2', '3', '4', '5'], def: '4' },
      { key: 'width', label: '总宽度(px)', type: 'select', options: ['960', '1280', '1920'], def: '1280' },
    ]},
    ringtone: { label: '做铃声（片段+淡入淡出）', kinds: ['video', 'audio'], params: [
      { key: 'start', label: '开始(秒)', type: 'number', def: 0 },
      { key: 'duration', label: '时长(秒，iPhone 上限 40)', type: 'number', def: 30 },
      { key: 'fmt', label: '格式', type: 'select', options: ['m4r', 'm4a', 'mp3'], def: 'm4r' },
      { key: 'fade', label: '淡入淡出(秒，0=关闭)', type: 'number', def: 1 },
    ]},
    dewatermark: { label: '去水印', kinds: ['video'], params: [
      { key: 'show', label: '🔍 仅画框定位（先勾这个看位置对不对）', type: 'checkbox', def: false },
      { key: 'x', label: '水印 X 坐标(px)', type: 'number', def: 0 },
      { key: 'y', label: '水印 Y 坐标(px)', type: 'number', def: 0 },
      { key: 'w', label: '水印宽度(px)', type: 'number', def: 100 },
      { key: 'h', label: '水印高度(px)', type: 'number', def: 50 },
      { key: 'band', label: '模糊强度(1-100, 越大越柔和)', type: 'number', def: 10 },
    ]},
    ai_dewatermark: { label: '🤖 AI 去水印（需worker）', kinds: ['video'], params: [
      { key: 'x', label: '水印 X 坐标(px)', type: 'number', def: 0 },
      { key: 'y', label: '水印 Y 坐标(px)', type: 'number', def: 0 },
      { key: 'w', label: '水印宽度(px)', type: 'number', def: 120 },
      { key: 'h', label: '水印高度(px)', type: 'number', def: 60 },
      { key: 'band', label: '羽化宽度(0-20, 默认5)', type: 'number', def: 5 },
    ]},
  };

  // 比例预设 → 确保偶数的 crop 表达式（ffmpeg crop 要求宽高为偶）。
  const CROP_PRESETS = {
    '9:16 竖屏': 'trunc(ih*9/16/2)*2:trunc(ih/2)*2:(iw-trunc(ih*9/16/2)*2)/2:0',
    '1:1 方形': 'trunc(ih/2)*2:trunc(ih/2)*2:(iw-trunc(ih/2)*2)/2:(ih-trunc(ih/2)*2)/2',
    '4:3': 'trunc(ih*4/3/2)*2:trunc(ih/2)*2:(iw-trunc(ih*4/3/2)*2)/2:0',
    '16:9': 'trunc(iw/2)*2:trunc(iw*9/16/2)*2:0:(ih-trunc(iw*9/16/2)*2)/2',
  };

  const resetProcessPanel = () => {
    el.processPanel.hidden = true;
    el.processStatus.hidden = true;
    el.processParams.replaceChildren();
    if (!currentLibItem) return;
    const kind = currentLibItem.kind;
    el.processOp.replaceChildren();
    Object.entries(PROCESS_OPS).forEach(([op, cfg]) => {
      if (!cfg.kinds.includes(kind)) return;
      const o = document.createElement('option');
      o.value = op; o.textContent = cfg.label;
      el.processOp.appendChild(o);
    });
    if (el.processOp.options.length) renderProcessParams();
  };

  const showProcessStatus = (msg, isError) => {
    el.processStatus.hidden = false;
    el.processStatus.textContent = msg;
    el.processStatus.classList.toggle('is-error', !!isError);
  };

  function renderProcessParams() {
    const op = el.processOp.value;
    const cfg = PROCESS_OPS[op];
    el.processParams.replaceChildren();
    if (!cfg) return;
    cfg.params.forEach((p) => {
      const row = document.createElement('div');
      row.className = 'sub-row';
      const label = document.createElement('label');
      label.className = 'process-param';
      label.textContent = p.label;
      let input;
      if (p.type === 'select') {
        input = document.createElement('select');
        input.className = 'adv-input';
        p.options.forEach((o) => {
          const opt = document.createElement('option');
          opt.value = o; opt.textContent = o;
          if (o === p.def) opt.selected = true;
          input.appendChild(opt);
        });
      } else if (p.type === 'checkbox') {
        input = document.createElement('input');
        input.type = 'checkbox';
        input.checked = !!p.def;
      } else {
        input = document.createElement('input');
        input.type = p.type === 'number' ? 'number' : 'text';
        input.className = 'adv-input';
        if (p.def !== undefined) input.value = p.def;
      }
      input.dataset.key = p.key;
      label.appendChild(input);
      row.appendChild(label);
      el.processParams.appendChild(row);
    });
    if (op === 'crop') {
      const presetSel = el.processParams.querySelector('select[data-key="preset"]');
      const exprInput = el.processParams.querySelector('input[data-key="crop_expr"]');
      if (presetSel && exprInput) {
        presetSel.addEventListener('change', () => {
          exprInput.value = CROP_PRESETS[presetSel.value] || '';
        });
      }
    }
  };

  const collectParams = () => {
    const op = el.processOp.value;
    const cfg = PROCESS_OPS[op];
    const params = {};
    if (!cfg) return params;
    cfg.params.forEach((p) => {
      const node = el.processParams.querySelector(`[data-key="${p.key}"]`);
      if (!node) return;
      if (p.type === 'checkbox') params[p.key] = node.checked;
      else if (p.type === 'number') params[p.key] = Number(node.value);
      else params[p.key] = node.value;
    });
    return params;
  };

  function toggleProcessPanel() {
    el.processPanel.hidden = !el.processPanel.hidden;
    if (!el.processPanel.hidden) renderProcessParams();
  };

  const pollProcess = (jobId) => new Promise((resolve) => {
    let done = false;
    const finish = (r) => { if (!done) { done = true; clearInterval(timer); clearTimeout(guard); resolve(r); } };
    const timer = setInterval(async () => {
      try {
        const d = await request(`/api/process/${encodeURIComponent(jobId)}`);
        if (!el.processPanel.hidden) showProcessStatus('处理中…（' + d.status + '）', false);
        if (d.status === 'completed' || d.status === 'failed') finish(d);
      } catch (e) {
        finish({ status: 'failed', error: e.message });
      }
    }, 1500);
    // 兜底超时：10 分钟（超大视频压缩/放大可能很久）
    const guard = setTimeout(() => finish({ status: 'failed', error: '处理超时（超过 10 分钟）' }), 600000);
  });

  async function runProcess() {
    if (!currentLibItem) return;
    const op = el.processOp.value;
    if (!op) { showProcessStatus('请选择一个处理操作', true); return; }
    const params = collectParams();
    if (op === 'ringtone' && params.fmt === 'm4r' && Number(params.duration) > 40) {
      showProcessStatus('提示：iPhone 铃声上限 40 秒，超出可能无法导入（仍会生成）', true);
    }
    el.processRun.disabled = true;
    showProcessStatus('正在处理…（大视频可能要几分钟）', false);
    try {
      const data = await request('/api/process/run', {
        method: 'POST',
        body: JSON.stringify({ lib_id: currentLibItem.id, op, params }),
      });
      const result = await pollProcess(data.job_id);
      if (result.status === 'completed') {
        if (result.is_dir) {
          // 批量抽帧：产物是子目录，不进媒体库列表，留在面板里提示路径
          showProcessStatus(`已抽 ${result.count} 帧 → 下载目录/${result.name}/`, false);
        } else {
          if (typeof el.libModal.close === 'function') el.libModal.close();
          loadLibrary();
          showProcessStatus(`已生成：${result.name}（去「媒体库」刷新即可看到）`, false);
        }
      } else {
        showProcessStatus('处理失败：' + (result.error || '未知错误'), true);
      }
    } catch (e) {
      showProcessStatus(e.message || '处理请求失败', true);
    } finally {
      el.processRun.disabled = false;
    }
  };

  // ------------------------------------------------------------------ 批量处理 + 加工队列
  let selectedLibIds = new Set();
  let procQueueTimer = null;

  // 切换卡片勾选态
  const toggleLibSelect = (id, card, cb) => {
    if (selectedLibIds.has(id)) {
      selectedLibIds.delete(id);
      card.classList.remove('selected');
      if (cb) cb.checked = false;
    } else {
      selectedLibIds.add(id);
      card.classList.add('selected');
      if (cb) cb.checked = true;
    }
    updateBatchUI();
  };

  const updateBatchUI = () => {
    const n = selectedLibIds.size;
    el.libBatch.hidden = n === 0;
    el.libBatchCount.textContent = n > 0 ? `已选 ${n} 个` : '';
    el.libBatchProcess.hidden = n === 0;
  };

  // 批量处理：收集所有选中 ID 提交加工
  const runBatchProcess = async () => {
    const ids = [...selectedLibIds];
    if (!ids.length) return;
    // 确保 processOp 已填充（批量模式下可能没开过弹窗）
    if (!el.processOp.options.length) {
      Object.entries(PROCESS_OPS).forEach(([op, cfg]) => {
        if (!cfg.kinds.includes('video')) return;
        const o = document.createElement('option');
        o.value = op; o.textContent = cfg.label;
        el.processOp.appendChild(o);
      });
    }
    const op = el.processOp.value;
    const params = collectParams();
    el.libBatchProcess.disabled = true;
    el.libBatchProcess.textContent = '提交中…';
    try {
      const result = await request('/api/process/run', {
        method: 'POST',
        body: JSON.stringify({ lib_ids: ids, op, params }),
      });
      if (result.error) {
        alert(result.error);
        return;
      }
      showProcessStatus(`已提交 ${result.total || ids.length} 个任务`);
      // 清空选择并打开队列
      selectedLibIds.clear();
      updateBatchUI();
      // 清除卡片勾选态
      el.libGrid.querySelectorAll('.lib-card.selected').forEach((c) => c.classList.remove('selected'));
      el.libGrid.querySelectorAll('.lib-check').forEach((c) => c.checked = false);
      el.queuePanel.hidden = false;
      loadProcessQueue();
      startProcQueuePoll();
    } catch (e) {
      showProcessStatus(e.message || '批量提交失败', true);
    } finally {
      el.libBatchProcess.disabled = false;
      el.libBatchProcess.textContent = '批量处理';
    }
  };

  // 加工队列轮询
  const loadProcessQueue = async () => {
    if (!node.libraryEnabled) return;
    try {
      const data = await request('/api/process/queue');
      renderProcQueue(data);
    } catch { /* 忽略 */ }
  };

  const renderProcQueue = (data) => {
    el.queueConcurrency.value = data.concurrency;
    el.queueConcurrencyVal.textContent = data.concurrency;
    el.queueList.replaceChildren();
    el.queueEmpty.hidden = data.jobs.length > 0;
    data.jobs.forEach((j) => {
      const li = document.createElement('li');
      li.className = `queue-item st-${j.status}`;
      const steps = Array.isArray(j.steps) ? j.steps : [];
      const stepsHtml = steps.length ? `<div class="task-steps queue-item-steps">${steps.map((s) => {
        const statusClass = s.status === 'running' ? 'task-step--running' :
                            s.status === 'done' ? 'task-step--done' :
                            s.status === 'error' ? 'task-step--error' : 'task-step--pending';
        const icon = s.status === 'running' ? '●' :
                     s.status === 'done' ? '✓' :
                     s.status === 'error' ? '✕' : '○';
        const detail = s.detail ? `<span class="task-step-detail">${escHtml(String(s.detail))}</span>` : '';
        return `<div class="task-step ${statusClass}">
          <span class="task-step-dot">${icon}</span>
          <div class="task-step-body">
            <span class="task-step-name">${escHtml(s.name)}</span>
            ${detail}
          </div>
        </div>`;
      }).join('')}</div>` : '';
      const labels = { running: '运行中', completed: '完成', failed: '失败', pending: '排队中' };
      li.innerHTML = `
        <span class="queue-item-name">${escHtml(j.op || '加工')} · ${escHtml(j.name || j.job_id)}</span>
        ${j.error ? `<span class="queue-item-err">${escHtml(j.error)}</span>` : ''}
        <span class="queue-item-badge ${j.status}">${labels[j.status] || j.status}</span>
        ${stepsHtml}
      `;
      el.queueList.appendChild(li);
    });
  };

  const startProcQueuePoll = () => {
    stopProcQueuePoll();
    procQueueTimer = setInterval(loadProcessQueue, 2000);
  };

  const stopProcQueuePoll = () => {
    if (procQueueTimer) { clearInterval(procQueueTimer); procQueueTimer = null; }
  };

  // 事件绑定：复选框、全选/取消、批量处理按钮、队列面板开关、并发滑块
  el.libShowQueue.addEventListener('click', () => {
    el.queuePanel.hidden = !el.queuePanel.hidden;
    if (!el.queuePanel.hidden) { loadProcessQueue(); startProcQueuePoll(); }
  });
  el.queuePanelClose.addEventListener('click', () => {
    el.queuePanel.hidden = true;
    stopProcQueuePoll();
  });
  el.queueConcurrency.addEventListener('input', () => {
    el.queueConcurrencyVal.textContent = el.queueConcurrency.value;
  });
  el.queueConcurrency.addEventListener('change', async () => {
    await request('/api/process/concurrency', {
      method: 'POST',
      body: JSON.stringify({ n: parseInt(el.queueConcurrency.value) }),
    });
  });
  el.libSelectAll.addEventListener('click', () => {
    el.libGrid.querySelectorAll('.lib-card').forEach((card) => {
      const cb = card.querySelector('.lib-check');
      const id = cb?.dataset.libId;
      if (id && !selectedLibIds.has(id)) {
        selectedLibIds.add(id);
        card.classList.add('selected');
        if (cb) cb.checked = true;
      }
    });
    updateBatchUI();
  });
  el.libDeselectAll.addEventListener('click', () => {
    selectedLibIds.clear();
    el.libGrid.querySelectorAll('.lib-card.selected').forEach((c) => c.classList.remove('selected'));
    el.libGrid.querySelectorAll('.lib-check').forEach((c) => c.checked = false);
    updateBatchUI();
  });
  el.libBatchProcess.addEventListener('click', runBatchProcess);

  setInterval(loadQueue, 2500);  // 队列概览：持续轮询任务统计

  // ------------------------------------------------------------------ 种子下载（桌面版功能）
  // 把 magnet/.torrent 下载到本地媒体库；能力由 /api/nodes 的 torrent.enabled 控制。
  let torTimer = null;
  const fmtSpeed = (n) => formatBytes(n) + '/s';
  const fmtEta = (s) => {
    s = Number(s) || 0;
    if (s <= 0) return '—';
    if (s > 86400) return Math.round(s / 86400) + ' 天';
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    if (h) return `${h}时${m}分`;
    if (m) return `${m}分${sec}秒`;
    return `${sec}秒`;
  };

  const loadTorrents = async () => {
    if (!node.torrentEnabled) return;
    try {
      const data = await request('/api/torrents');
      renderTorrents(data.items || []);
    } catch (e) {
      if (el.torStatus) { el.torStatus.hidden = true; }
      if (el.torEmpty) { el.torEmpty.hidden = false; el.torEmpty.textContent = '读取种子列表失败：' + (e.message || '未知错误'); }
    }
  };

  const renderTorrents = (items) => {
    if (!el.torList) return;
    if (!node.torrentAvailable) {
      el.torList.replaceChildren();
      if (el.torEmpty) {
        el.torEmpty.hidden = false;
        el.torEmpty.textContent = '当前环境未安装 libtorrent，无法使用种子下载。请在桌面版（macOS 14+ / Linux / Windows，Python ≤3.13）执行 pip install "libtorrent==2.0.11" 后重启本应用。';
      }
      return;
    }
    if (el.torStatus) el.torStatus.hidden = true;
    if (el.torEmpty) el.torEmpty.hidden = items.length > 0;
    el.torList.replaceChildren();
    for (const t of items) el.torList.appendChild(renderTorrentCard(t));
  };

  const renderTorrentCard = (t) => {
    const card = document.createElement('div');
    card.className = 'tor-card';
    const pct = Math.round((t.progress || 0) * 100);
    const stateBadge = t.paused
      ? '⏸ 已暂停'
      : (t.state === 'seeding' ? '🌱 做种'
        : (t.state === 'finished' ? '✅ 完成'
          : (t.has_metadata ? '⬇️ 下载中' : '🔍 获取元信息')));
    const meta = [
      `${formatBytes(t.downloaded)} / ${formatBytes(t.size)}`,
      `${pct}%`,
      `↓${fmtSpeed(t.download_speed)} ↑${fmtSpeed(t.upload_speed)}`,
      `peers ${t.peers}`,
      `seeds ${t.seeds}`,
      `ETA ${fmtEta(t.eta)}`,
    ];
    let html = `<div class="tor-head"><div class="tor-name" title="${escHtml(t.name)}">${escHtml(t.name)}</div><div class="tor-state">${stateBadge}</div></div>`;
    html += `<div class="tor-bar"><div class="tor-bar-fill" style="width:${pct}%"></div></div>`;
    html += `<div class="tor-meta">${meta.map((m) => `<span>${m}</span>`).join('')}</div>`;
    if (t.error) html += `<div class="tor-error">⚠ ${escHtml(t.error)}</div>`;
    if (t.files && t.files.length) {
      html += `<div class="tor-files"><div class="tor-files-title">文件（取消勾选可跳过该文件下载）：</div>`;
      for (const f of t.files) {
        html += `<label class="tor-file"><input type="checkbox" data-tid="${escHtml(t.id)}" data-fidx="${f.index}" ${f.skipped ? '' : 'checked'}> <span class="tor-fname">${escHtml(f.name)}</span> <span class="tor-fsize">${formatBytes(f.size)} · ${Math.round((f.progress || 0) * 100)}%</span></label>`;
      }
      html += `</div>`;
    }
    html += `<div class="tor-actions">`;
    html += t.paused
      ? `<button class="btn btn-ghost btn-sm" data-tor-resume="${escHtml(t.id)}">▶ 继续</button>`
      : `<button class="btn btn-ghost btn-sm" data-tor-pause="${escHtml(t.id)}">⏸ 暂停</button>`;
    html += `<button class="btn btn-ghost btn-sm" data-tor-remove="${escHtml(t.id)}">🗑 移除</button></div>`;
    card.innerHTML = html;
    return card;
  };

  if (el.torList) {
    el.torList.addEventListener('click', async (e) => {
      const btn = e.target.closest('[data-tor-pause],[data-tor-resume],[data-tor-remove]');
      if (!btn) return;
      const id = btn.getAttribute('data-tor-pause') || btn.getAttribute('data-tor-resume') || btn.getAttribute('data-tor-remove');
      try {
        if (btn.hasAttribute('data-tor-pause')) await request(`/api/torrents/${id}/pause`, { method: 'POST' });
        else if (btn.hasAttribute('data-tor-resume')) await request(`/api/torrents/${id}/resume`, { method: 'POST' });
        else if (btn.hasAttribute('data-tor-remove')) {
          if (!confirm('确定移除该种子？已下载的文件不会被删除（如需一并删除文件请到媒体库操作）。')) return;
          await request(`/api/torrents/${id}/remove`, { method: 'POST', body: JSON.stringify({ delete_files: false }) });
        }
        await loadTorrents();
      } catch (err) { alert('操作失败：' + (err.message || err)); }
    });
    el.torList.addEventListener('change', async (e) => {
      const cb = e.target.closest('input[type=checkbox][data-tid]');
      if (!cb) return;
      const tid = cb.getAttribute('data-tid');
      const fidx = Number(cb.getAttribute('data-fidx'));
      const prio = cb.checked ? 4 : 0;
      try {
        await request(`/api/torrents/${tid}/files`, { method: 'POST', body: JSON.stringify({ priorities: { [fidx]: prio } }) });
        await loadTorrents();
      } catch (err) { alert('设置失败：' + (err.message || err)); }
    });
  }

  if (el.torAddBtn) {
    el.torAddBtn.addEventListener('click', async () => {
      if (!node.torrentAvailable) { alert('当前环境未安装 libtorrent，无法添加种子。'); return; }
      const uri = (el.torAddInput.value || '').trim();
      const file = el.torTorrentFile.files && el.torTorrentFile.files[0];
      if (!uri && !file) { alert('请粘贴 magnet 链接 / .torrent 网址，或选择一个 .torrent 文件'); return; }
      el.torAddBtn.disabled = true;
      try {
        if (file) {
          const fd = new FormData();
          fd.append('torrent', file);
          if (el.torSavePath && el.torSavePath.value.trim()) fd.append('save_path', el.torSavePath.value.trim());
          await request('/api/torrents/add-file', { method: 'POST', body: fd, headers: {} });
        } else {
          const body = { uri };
          if (el.torSavePath && el.torSavePath.value.trim()) body.save_path = el.torSavePath.value.trim();
          await request('/api/torrents/add', { method: 'POST', body: JSON.stringify(body) });
        }
        el.torAddInput.value = '';
        if (el.torTorrentFile) el.torTorrentFile.value = '';
        await loadTorrents();
      } catch (err) {
        alert('添加失败：' + (err.message || err));
      } finally {
        el.torAddBtn.disabled = false;
      }
    });
  }

  function startTorPoll() { if (!torTimer) torTimer = setInterval(loadTorrents, 2000); }
  function stopTorPoll() { if (torTimer) { clearInterval(torTimer); torTimer = null; } }

  request('/api/platforms')
    .then(({ platforms }) => renderPlatforms(platforms))
    .catch(() => { /* 平台清单获取失败不影响主流程 */ });

  request('/api/nodes')
    .then(({ region, peer, china_domains: domains, commentary_enabled, ads_enabled, convert, download, cloud, library, subscriptions, retention, archive, crypto, torrent, ai_dewatermark, authRequired }) => {
      node.authRequired = !!authRequired;
      if (node.authRequired && !localStorage.getItem('vdl_api_token')) {
        const t = (typeof prompt === 'function') ? prompt('该服务已启用访问令牌，请输入 API Token：') : null;
        if (t && t.trim()) localStorage.setItem('vdl_api_token', t.trim());
      }
      node.region = region || 'global';
      node.peer = peer || '';
      node.chinaDomains = domains || [];
      node.commentaryEnabled = !!commentary_enabled;
      node.adsEnabled = !!ads_enabled;
      el.adsSlot.hidden = !node.adsEnabled;
      node.convertSubRequired = !!(convert && convert.subscription_required);
      node.convertFreeDaily = (convert && convert.free_daily) || 3;
      node.downloadSubRequired = !!(download && download.subscription_required);
      node.downloadFreeDaily = (download && download.free_daily) || 10;
      const cloudInfo = cloud || {};
      node.cloudSubRequired = !!(cloudInfo && cloudInfo.subscription_required);
      node.cloudFreeDaily = (cloudInfo && cloudInfo.free_daily) || 5;
      node.cloudFreeUsed = 0;
      node.cloudProviders = (cloudInfo && cloudInfo.providers) || ['webdav'];
      node.baiduAvailable = !!(cloudInfo && cloudInfo.baidu_available);
      node.baiduAuthUrl = (cloudInfo && cloudInfo.baidu_auth_url) || '';
      node.libraryEnabled = !!(library && library.enabled);
      node.subscriptionsEnabled = !!(subscriptions && subscriptions.enabled);
      node.retentionEnabled = !!(retention && retention.enabled);
      node.trashAvailable = !!(retention && retention.trash_available);
      node.archiveEnabled = !!(archive && archive.enabled);
      node.archiveBaiduAvailable = !!(archive && archive.baidu_available);
      node.archiveConfigured = !!(archive && archive.configured);
      node.cryptoEnabled = !!(crypto && crypto.enabled);
      node.cryptoHasPass = !!(crypto && crypto.has_pass);
      node.cryptoLocked = !!(crypto && crypto.locked);
      node.torrentEnabled = !!(torrent && torrent.enabled);
      node.torrentAvailable = !!(torrent && torrent.available);
      node.aiDewatermarkGpu = !!(ai_dewatermark && ai_dewatermark.gpu);
      // 有 GPU → 标签显示加速；没有 → 提示 CPU 模式
      if (PROCESS_OPS.ai_dewatermark) {
        PROCESS_OPS.ai_dewatermark.label = node.aiDewatermarkGpu
          ? '🤖 AI 去水印（GPU 加速）'
          : '🤖 AI 去水印（CPU，较慢但任何电脑可跑）';
      }
      if (el.libCleanup) el.libCleanup.hidden = !node.retentionEnabled;
      if (el.libArchive) el.libArchive.hidden = !node.archiveEnabled;
      if (el.libCrypto) el.libCrypto.hidden = !node.cryptoEnabled;
      if (el.libShowQueue) el.libShowQueue.hidden = !node.libraryEnabled;
      if (el.tabTorrent) el.tabTorrent.hidden = !node.torrentEnabled;
      // 视频解说已提升为主功能，tab 始终显示；后端未启用时操作会提示 503。
      if (el.tabCommentary) el.tabCommentary.hidden = false;
      if (node.libraryEnabled || node.subscriptionsEnabled || node.torrentEnabled || node.commentaryEnabled) el.tabs.hidden = false;
      // 默认展示「已生成成片」列表：打开应用即停在解说成片视图，方便直接查看/下载成片。
      switchView('commentary');
      initSubUI();
      paintNodeBar();
    })
    .catch(() => { /* 取不到节点信息就退回单节点，全部走本机 */ });
  // 兜底：无论节点信息是否加载成功，启动后都默认停在解说成片视图。
  // （节点请求失败时 .then 不会执行，这里保证默认视图一定生效。）
  try { switchView('commentary'); } catch (_) {}
  // 启动即确保全局错误提示框隐藏，没错误就完全不显示
  try { clearError(); } catch (_) {}
})();
