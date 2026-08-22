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
        box.style.cssText = 'position:fixed;left:0;right:0;top:0;z-index:2147483647;background:#c0392b;color:#fff;font:12px/1.5 monospace;padding:8px 36px 8px 12px;white-space:pre-wrap;';
        const close = document.createElement('button');
        close.textContent = '×';
        close.style.cssText = 'position:absolute;right:8px;top:6px;border:none;background:transparent;color:#fff;font-size:16px;cursor:pointer;line-height:1;';
        close.onclick = () => box.remove();
        box.appendChild(close);
        (document.body || document.documentElement).appendChild(box);
      }
      const loc = e.filename ? (' @ ' + String(e.filename).split('/').pop() + ':' + e.lineno) : '';
      let msg = document.getElementById('bootErrMsg');
      if (!msg) { msg = document.createElement('div'); msg.id = 'bootErrMsg'; box.insertBefore(msg, box.firstChild); }
      msg.textContent = '启动错误: ' + (e.message || (e.error && e.error.message) || e.error) + loc;
      const stack = (e.error && e.error.stack) ? String(e.error.stack) : '';
      if (stack) {
        let pre = document.getElementById('bootErrStack');
        if (!pre) { pre = document.createElement('pre'); pre.id = 'bootErrStack'; pre.style.cssText = 'margin:4px 0 0;white-space:pre-wrap;opacity:.85;'; box.appendChild(pre); }
        pre.textContent = stack;
      }
    } catch (_) {}
  });
  // 兜底：若 IIFE 末尾因同步抛错未能设置默认视图，事件循环最后切到最安全的下载视图。
  // 注意：不能无条件切 commentary，否则网页版刷新会先闪一下「视频解说」再被覆盖。
  let bootViewSet = false;
  setTimeout(() => { try { if (!bootViewSet) switchView('download'); } catch (_) {} }, 0);

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
  const ACTIVE_STATES = ['pending', 'downloading', 'merging', 'paused', 'pausing'];
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
    alert: $('alertBox'),
    alertIcon: $('alertIcon'),
    alertBody: $('alertBody'),
    alertTitle: $('alertTitle'),
    alertHint: $('alertHint'),
    alertAction: $('alertAction'),
    alertDetail: $('alertDetail'),
    alertToggle: $('alertToggle'),
    resultPanel: $('resultPanel'),
    thumb: $('videoThumb'),
    duration: $('videoDuration'),
    title: $('videoTitle'),
    platform: $('videoPlatform'),
    uploader: $('videoUploader'),
    qualityGrid: $('qualityGrid'),
    hqTip: $('hqTip'),
    hqTipText: $('hqTipText'),
    downloadBtn: $('downloadBtn'),
    watchRow: $('watchRow'),
    watchBtn: $('watchBtn'),
    watchQuality: $('watchQuality'),
    watchModal: $('watchModal'),
    watchVideo: $('watchVideo'),
    watchTitle: $('watchTitle'),
    watchStatus: $('watchStatus'),
    watchClose: $('watchClose'),
    watchBack: $('watchBack'),
    quitAppBtn: $('quitAppBtn'),
    hideToDesktopBtn: $('hideToDesktopBtn'),
    tasksPanel: $('tasksPanel'),
    taskList: $('taskList'),
    badge: $('engineBadge'),
    template: $('taskTemplate'),
    modal: $('platformModal'),
    modalGrid: $('platformModalGrid'),
    modalTitle: $('platformModalTitle'),
    modalClose: $('platformModalClose'),
    cookieInput: $('cookieInput'),
    cookieContribute: $('cookieContribute'),
    proxyInput: $('proxyInput'),
    concurrentInput: $('concurrentInput'),
    downloaderSelect: $('downloaderSelect'),
    qualityBlock: $('qualityBlock'),
    extractSelect: $('extractSelect'),
    directHint: $('directHint'),
    serverFallbackBtn: $('serverFallbackBtn'),
    playlistPanel: $('playlistPanel'),
    playlistTitle: $('playlistTitle'),
    playlistMeta: $('playlistMeta'),
    playlistList: $('playlistList'),
    playlistDownloadBtn: $('playlistDownloadBtn'),
    playlistProgress: $('playlistProgress'),
    cookieHelp: $('cookieHelp'),
    cookieHelpCopy: $('cookieHelpCopy'),
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
    // 百度网盘浏览/下载
    tabBaidu: $('tabBaidu'),
    baiduModal: $('baiduModal'),
    baiduModalClose: $('baiduModalClose'),
    baiduDriveAuthBtn: $('baiduDriveAuthBtn'),
    baiduDriveStatus: $('baiduDriveStatus'),
    baiduDriveHint: $('baiduDriveHint'),
    baiduBreadcrumb: $('baiduBreadcrumb'),
    baiduList: $('baiduList'),
    baiduDlList: $('baiduDlList'),
    // 百度网盘「分享链接下载」
    baiduShareUrl: $('baiduShareUrl'),
    baiduSharePwd: $('baiduSharePwd'),
    baiduLoginStatus: $('baiduLoginStatus'),
    baiduShareListBtn: $('baiduShareListBtn'),
    baiduShareStatus: $('baiduShareStatus'),
    baiduShareList: $('baiduShareList'),
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
    // 上传转换（需求文档模块一）
    tabUploadConvert: $('tabUploadConvert'),
    uploadConvertView: $('uploadConvertView'),
    // ---- 上传转换视图（多文件批量）----
    ucAddBtn: $('ucAddBtn'),
    ucFileInput: $('ucFileInput'),
    ucClearBtn: $('ucClearBtn'),
    ucCount: $('ucCount'),
    ucList: $('ucList'),
    ucEmpty: $('ucEmpty'),
    ucBulk: $('ucBulk'),
    ucBulkTarget: $('ucBulkTarget'),
    ucBulkRes: $('ucBulkRes'),
    ucBulkBitrate: $('ucBulkBitrate'),
    ucBulkAudio: $('ucBulkAudio'),
    ucBulkRotate: $('ucBulkRotate'),
    ucBulkRemux: $('ucBulkRemux'),
    ucBulkLibrary: $('ucBulkLibrary'),
    ucBulkApplyBtn: $('ucBulkApplyBtn'),
    ucStartAllBtn: $('ucStartAllBtn'),
    ucStatus: $('ucStatus'),

    // 去水印（需求文档模块二）
    tabDw: $('tabDw'),
    dwView: $('dwView'),
    dwModeImg: $('dwModeImg'),
    dwModePdf: $('dwModePdf'),
    dwImgPane: $('dwImgPane'),
    dwImgFile: $('dwImgFile'),
    dwImgPreview: $('dwImgPreview'),
    dwImgCanvas: $('dwImgCanvas'),
    dwSelInfo: $('dwSelInfo'),
    dwImgMethod: $('dwImgMethod'),
    dwImgRadius: $('dwImgRadius'),
    dwImgBtn: $('dwImgBtn'),
    dwImgStatus: $('dwImgStatus'),
    dwImgResult: $('dwImgResult'),
    dwImgOrig: $('dwImgOrig'),
    dwImgOut: $('dwImgOut'),
    dwImgDownload: $('dwImgDownload'),
    dwPdfPane: $('dwPdfPane'),
    dwPdfFile: $('dwPdfFile'),
    dwPdfMode: $('dwPdfMode'),
    dwPdfRasterOpts: $('dwPdfRasterOpts'),
    dwPdfX: $('dwPdfX'),
    dwPdfY: $('dwPdfY'),
    dwPdfW: $('dwPdfW'),
    dwPdfH: $('dwPdfH'),
    dwPdfMethod: $('dwPdfMethod'),
    dwPdfRadius: $('dwPdfRadius'),
    dwPdfDpi: $('dwPdfDpi'),
    dwPdfBtn: $('dwPdfBtn'),
    dwPdfStatus: $('dwPdfStatus'),
    dwPdfResult: $('dwPdfResult'),
    dwPdfDownload: $('dwPdfDownload'),
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
  // 正在删除的任务 ID 集合：防止 syncMissingCards 在 DELETE 生效前把卡片重建回来
  const _deletingIds = new Set();

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
      const err = { message: payload.error || payload.detail || '请求失败，请稍后重试', hint: payload.hint || '', category: payload.category || '' };
      if (response.status === 402) err.subscribe = true;   // 免费额度耗尽，引导订阅
      throw err;
    }
    return payload;
  };

  /** 设备隔离 ID：sessionStorage 级（标签页独立，刷新保留，新标签页/新设备是新 ID）。
   *  同浏览器开两个标签页 = 两个互不可见的任务空间；手机与电脑自然隔离。 */
  const deviceId = () => {
    let id = '';
    try { id = sessionStorage.getItem('vdl_device_id') || ''; } catch (e) { /* 隐私模式可能抛错 */ }
    if (!id) {
      try {
        id = (crypto && crypto.randomUUID) ? crypto.randomUUID() : (Date.now().toString(36) + Math.random().toString(36).slice(2));
      } catch (e) {
        id = Date.now().toString(36) + Math.random().toString(36).slice(2);
      }
      try { sessionStorage.setItem('vdl_device_id', id); } catch (e) { /* 忽略 */ }
    }
    return id;
  };

  const request = async (path, options = {}, base = '') => {
    const headers = {};
    const subKey = localStorage.getItem('vdl_sub_key');
    if (subKey) headers['X-Subscription-Key'] = subKey;
    const apiToken = localStorage.getItem('vdl_api_token');
    if (apiToken) headers['X-Api-Key'] = apiToken;
    // 设备隔离（2026-08-22）：每标签页独立 device_id（sessionStorage），
    // 后端据此只返回本页面创建的任务——手机/其他页面完全看不到本页任务。
    headers['X-Device-Id'] = deviceId();
    // FormData（multipart 上传）不强制 Content-Type，交给浏览器设 boundary；
    // 其余默认 JSON。options.headers 仅做增强、不覆盖（避免丢失 token）。
    const isForm = options.body instanceof FormData;
    if (!isForm && !(options.headers && 'Content-Type' in options.headers)) {
      headers['Content-Type'] = 'application/json';
    }
    const merged = { ...headers, ...(options.headers || {}) };
    // file:// 模式下相对路径会解析到 file:// 协议（无法请求后端），
    // 此时使用 launcher 通过 evaluate_js 注入的 window.VDL_API_BASE（绝对 http 地址）。
    const apiBase = base || window.VDL_API_BASE || '';
    const doFetch = () => fetch(apiBase + path, { ...options, headers: merged });
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

  // 把 WebKit 原始网络错误（load failed / Failed to fetch / NetworkError）
  // 转成用户友好的中文提示，避免用户看到吓人的技术报错。
  const _friendlyNetworkError = (msg) => {
    const lower = String(msg || '').toLowerCase();
    if (lower === 'load failed' || lower === 'failed to fetch' || lower === 'networkerror'
        || lower.includes('load failed') || lower.includes('failed to fetch')
        || lower.includes('networkerror') || lower.includes('network error')) {
      return {
        message: '连接本地服务失败',
        hint: '请稍等 2~3 秒后重试；若仍失败，请完全退出应用（Cmd+Q）再重新打开，避免从 DMG 镜像里启动。'
      };
    }
    return null;
  };

  const showError = (message, hint = '', detail = '', category = '') => {
    let msg = String(message || '').trim();
    let h = String(hint || '').trim();
    if (!msg) { clearError(); return; }
    // 网络层原始错误 → 友好提示
    const net = _friendlyNetworkError(msg);
    if (net) { msg = net.message; h = h || net.hint; }

    // —— 按错误分类做视觉区分 + 针对性行动建议 ——
    // category 由后端 _friendly_error 产出（cookie_required / cdn_forbidden /
    // network / restricted / unknown 等），让同一个横幅对不同错误给出不同引导，
    // 而不是千篇一律的「解析失败」。
    const cat = String(category || '').trim();
    const ICON = { cookie_required: '🔑', cookie_invalid_or_expired: '🔑', cdn_forbidden: '🚫', restricted: '🔒', network: '🌐' };
    const ACCENT = { cookie_required: '#e0a33a', cookie_invalid_or_expired: '#e0a33a', cdn_forbidden: '#e2554f', restricted: '#9aa0a6', network: '#4a90d9' };
    // 先重置上一次的分类样式，避免串台
    el.alert.className = 'alert';
    el.alert.style.borderLeftColor = '';
    if (el.alertIcon) el.alertIcon.textContent = ICON[cat] || '!';
    if (ACCENT[cat]) el.alert.style.borderLeftColor = ACCENT[cat];
    // cookie 类错误：给一个「去粘贴 Cookie」按钮，一键展开高级选项并聚焦输入框
    if (el.alertAction) {
      const isCookie = cat === 'cookie_required' || cat === 'cookie_invalid_or_expired';
      if (isCookie) {
        el.alertAction.textContent = '去粘贴 Cookie';
        el.alertAction.hidden = false;
        el.alertAction.style.cssText = 'margin-top:.5rem;padding:.35rem .7rem;border:none;border-radius:6px;background:#e0a33a;color:#1b1b1b;font-size:12px;font-weight:600;cursor:pointer;';
        el.alertAction.onclick = () => {
          const adv = document.getElementById('advToggle');
          if (adv) adv.open = true;
          if (el.cookieInput) {
            try { el.cookieInput.focus(); el.cookieInput.scrollIntoView({ block: 'center', behavior: 'smooth' }); } catch (_) {}
          }
        };
      } else {
        el.alertAction.hidden = true;
        el.alertAction.onclick = null;
      }
    }

    el.alertTitle.textContent = msg;
    el.alertHint.textContent = h;
    el.alertHint.hidden = !h;
    // 完整原始错误：点击横幅可展开，便于看清"到底什么错"；同时打到控制台
    const hasDetail = !!String(detail || '').trim();
    if (el.alertDetail) {
      el.alertDetail.textContent = detail || '';
      el.alertDetail.hidden = true;
    }
    if (el.alertToggle) el.alertToggle.hidden = !hasDetail;
    el.alert.hidden = false;
    try { console.error('[VDL] ' + msg + (h ? '（' + h + '）' : ''), detail || ''); } catch (_) {}
  };

  const clearError = () => {
    el.alert.hidden = true;
    if (el.alertDetail) { el.alertDetail.hidden = true; el.alertDetail.textContent = ''; }
    if (el.alertToggle) el.alertToggle.hidden = true;
    // 一并重置分类样式/行动按钮，避免下次非分类错误仍残留旧样式
    el.alert.className = 'alert';
    el.alert.style.borderLeftColor = '';
    if (el.alertIcon) el.alertIcon.textContent = '!';
    if (el.alertAction) { el.alertAction.hidden = true; el.alertAction.onclick = null; }
  };
  document.getElementById('alertClose').addEventListener('click', clearError);
  // 点击横幅主体（除关闭按钮外）切换错误详情展开/收起。
  // 只有「有详情可看」时才允许切换——通过 alertToggle 是否可见判断（showError 里 hidden=!hasDetail）。
  // 旧的 `!el.alertDetail.hidden === false` 优先级 + 操作符有坑：hidden=false 时会卡死永远不切换。
  if (el.alertBody) {
    el.alertBody.addEventListener('click', (e) => {
      if (e.target.closest('.alert-close')) return;
      if (el.alertDetail && el.alertToggle && !el.alertToggle.hidden) {
        el.alertDetail.hidden = !el.alertDetail.hidden;
        el.alertToggle.textContent = el.alertDetail.hidden ? '点击展开错误详情' : '点击收起错误详情';
      }
    });
  }

  const setLoading = (loading) => {
    el.resolveBtn.classList.toggle('loading', loading);
    el.resolveBtn.disabled = loading;
    el.resolveBtn.querySelector('.btn-label').textContent = loading ? '解析中…' : '解析链接';
  };

  // ------------------------------------------------------------------ 渲染

  const MAX_VISIBLE_PLATFORMS = 16;

  const renderPlatforms = (platforms) => {
    allPlatforms = platforms;
    // 平台列表只保留 header 徽章入口（engineBadge 弹窗），输入区 chips 已移除
    if (el.chips) {
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
    }
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
    selectedQuality = qualities[0]?.key ?? 'best';

    el.qualityGrid.replaceChildren();

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
      const noteText = quality.approx_size
        ? `${quality.note} · 约 ${formatBytes(quality.approx_size)}`
        : quality.note;
      note.textContent = noteText;

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
    // 在线观看：按清晰度生成下拉，默认选最高画质
    const watchOpts = (video.watch_options && video.watch_options.length) ? video.watch_options : null;
    if (watchOpts) {
      el.watchQuality.hidden = false;
      el.watchQuality.replaceChildren();
      watchOpts.forEach((opt) => {
        const o = document.createElement("option");
        o.value = opt.key;
        o.textContent = opt.label;
        o.dataset.url = opt.url || "";
        o.dataset.hls = String(!!opt.is_hls);
        el.watchQuality.appendChild(o);
      });
      const first = watchOpts[0];
      el.watchBtn.dataset.url = first.url || "";
      el.watchBtn.dataset.hls = String(!!first.is_hls);
      el.watchRow.hidden = false;
    } else if (video.play_url) {
      // 兼容纯文件直链视频（无多清晰度 HLS）：只显示按钮、隐藏下拉
      el.watchQuality.hidden = true;
      el.watchQuality.replaceChildren();
      el.watchBtn.dataset.url = video.play_url;
      el.watchBtn.dataset.hls = String(!!video.is_hls);
      el.watchRow.hidden = false;
    } else {
      el.watchRow.hidden = true;
      el.watchQuality.hidden = false;
      el.watchBtn.dataset.url = "";
      el.watchBtn.dataset.hls = "false";
    }
    el.watchTitle.textContent = video.title || "在线观看";
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
      togglePause: node.querySelector('[data-toggle-pause]'),
      save: node.querySelector('[data-save]'),
      error: node.querySelector('[data-error]'),
      saveHint: node.querySelector('[data-save-hint]'),
      convertWrap: node.querySelector('[data-convert-wrap]'),
      convertTarget: node.querySelector('[data-convert-target]'),
      convertRes: node.querySelector('[data-convert-res]'),
      convertBtn: node.querySelector('[data-convert-btn]'),
      convertFile: node.querySelector('[data-convert-file]'),
      convertStatus: node.querySelector('[data-convert-status]'),
      convertProgress: node.querySelector('[data-convert-progress]'),
      convertProgressFill: node.querySelector('[data-convert-progress] .progress-fill'),
      convertQuota: node.querySelector('[data-convert-quota]'),
      cloud: node.querySelector('[data-cloud]'),
      cloudStatus: node.querySelector('[data-cloud-status]'),
      retry: node.querySelector('[data-retry]'),
      del: node.querySelector('[data-delete]'),
      watchBtn: node.querySelector('[data-watch]'),
      watchQuality: node.querySelector('[data-watch-quality]'),
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
    refs.togglePause.addEventListener('click', () => {
      const isPausedNow = refs.root.classList.contains('is-paused');
      if (isPausedNow) {
        // 意图：继续下载 → 立即反馈「继续中…」并短暂锁定，等后端/轮询转 downloading
        refs._opState = 'resuming';
        refs._opPauseUntil = Date.now() + POLL_FALLBACK_MS;
        refs.togglePause.textContent = '继续中…';
        refs.togglePause.disabled = true;
        refs.togglePause.title = '正在继续';
        refs.root.classList.remove('is-paused');
        resumeTask(taskId, refs.base || '');
      } else {
        // 意图：暂停下载 → 立即反馈「暂停中…」，等 yt-dlp 真正停止（pausing→paused）
        refs._opState = 'pausing';
        refs._opPauseUntil = Date.now() + 2500;
        refs.togglePause.textContent = '暂停中…';
        refs.togglePause.disabled = true;
        refs.togglePause.title = '正在暂停';
        refs.root.classList.add('is-paused');
        pauseTask(taskId, refs.base || '');
      }
    });
    refs.retry.addEventListener('click', () => retryTask(taskId, refs));
    refs.del.addEventListener('click', () => deleteTask(taskId, refs));
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
    // 新任务卡滚动到可见区，用户点击「开始下载」后立即看到进度
    try { node.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); } catch (e) { /* 忽略 */ }
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
    // 暂停/继续按钮状态机（含 pausing 过渡态 + 乐观意图锁，确保点击反馈连贯不闪烁/不消失）
    const st = task.status;
    const isPausing = st === 'pausing';
    const isPaused = st === 'paused';
    const downloading = st === 'downloading' || st === 'merging';
    // 乐观意图锁：点击后短时间内按钮以「用户意图」渲染，避免被尚未变化的轮询/SSE 刷回
    const opState = (refs._opPauseUntil && Date.now() < refs._opPauseUntil) ? refs._opState : null;
    refs.togglePause.hidden = !(downloading || isPaused || isPausing);
    if (opState === 'pausing' || isPausing) {
      refs.togglePause.textContent = '暂停中…';
      refs.togglePause.disabled = true;
      refs.togglePause.title = '正在暂停';
      refs.root.classList.add('is-paused');
    } else if (isPaused) {
      refs.togglePause.textContent = '▶ 继续';
      refs.togglePause.disabled = false;
      refs.togglePause.title = '继续下载';
      refs.root.classList.add('is-paused');
    } else if (opState === 'resuming') {
      refs.togglePause.textContent = '继续中…';
      refs.togglePause.disabled = true;
      refs.togglePause.title = '正在继续';
      refs.root.classList.remove('is-paused');
    } else {
      refs.togglePause.textContent = '⏸ 暂停';
      refs.togglePause.disabled = false;
      refs.togglePause.title = '暂停下载';
      refs.root.classList.remove('is-paused');
    }
    refs.cancel.hidden = isPaused || isPausing;
    refs.root.classList.toggle('is-active', active);
    refs.root.classList.toggle('is-done', task.status === 'completed');
    refs.root.classList.toggle('is-error', task.status === 'failed' || task.status === 'canceled');
    // 已完成任务：折叠过程/进度条/转换等冗余信息，只留标题+核心动作（保存到本机/网盘/删除）
    refs.root.classList.toggle('is-collapsed', task.status === 'completed');

    const failed = task.status === 'failed';
    refs.error.hidden = !failed;
    if (failed) {
      // 按错误分类给出图标 + 一句行动建议，让用户一看就知道下一步该干嘛
      // （cookie 类→去粘贴 Cookie；403→登录/会员/地区；网络→重试；受限→官方渠道）
      const cat = task.category || '';
      const CAT_ICON = { cookie_required: '🔑', cookie_invalid_or_expired: '🔑', cdn_forbidden: '🚫', restricted: '🔒', network: '🌐' };
      const CAT_TIP = {
        cookie_required: '需在「高级选项」粘贴该平台 Cookie 后重试',
        cookie_invalid_or_expired: 'Cookie 已失效，请重新登录并在「高级选项」粘贴后重试',
        cdn_forbidden: '服务器拒绝（403）：多为需登录 / 会员 / 地区限制',
        restricted: '该内容受版权或地区保护，无法下载',
        network: '网络不稳定，可点「重试 / 继续下载」再试一次',
      };
      const icon = CAT_ICON[cat] || '⚠️';
      const base = [task.error, task.hint].filter(Boolean).join(' — ');
      const tip = CAT_TIP[cat] ? `（${CAT_TIP[cat]}）` : '';
      refs.error.textContent = `${icon} ${base}${tip}`;
    } else {
      refs.error.textContent = '';
    }


    // 在线观看：任务面板也显示观看按钮（解析结果里的播放地址存入任务后可在此直接打开）
    const twUrl = task.play_url || refs._watchUrl || '';
    const twOpts = (task.watch_options && task.watch_options.length) ? task.watch_options : (refs._watchOpts || []);
    const twHls = task.is_hls ?? refs._watchHls ?? false;
    if (twUrl) {
      if (twOpts.length) {
        refs.watchQuality.replaceChildren();
        twOpts.forEach((o) => {
          const opt = document.createElement('option');
          opt.value = o.key; opt.textContent = o.label;
          opt.dataset.url = o.url || ''; opt.dataset.hls = String(!!o.is_hls);
          refs.watchQuality.appendChild(opt);
        });
        const first = twOpts[0];
        refs.watchBtn.dataset.url = first.url || '';
        refs.watchBtn.dataset.hls = String(!!first.is_hls);
        refs.watchQuality.hidden = false;
      } else {
        refs.watchQuality.hidden = true;
        refs.watchBtn.dataset.url = twUrl;
        refs.watchBtn.dataset.hls = String(twHls);
      }
      refs.watchBtn.hidden = false;
      if (!refs.watchBtn.dataset.boundWatch) {
        refs.watchBtn.dataset.boundWatch = '1';
        refs.watchBtn.addEventListener('click', () => openWatch({
          url: refs.watchBtn.dataset.url,
          taskId: refs.root.dataset.taskId,
          completed: refs.root.classList.contains('is-done'),
          base: refs.base || '',
        }));
        refs.watchQuality.addEventListener('change', () => {
          const o = refs.watchQuality.selectedOptions && refs.watchQuality.selectedOptions[0];
          if (o) {
            refs.watchBtn.dataset.url = o.dataset.url || '';
            refs.watchBtn.dataset.hls = o.dataset.hls || 'false';
          }
        });
      }
    } else {
      refs.watchBtn.hidden = true;
      refs.watchQuality.hidden = true;
    }

    // 失败 / 已取消的任务展示「重试 / 继续下载」按钮
    const canRetry = task.status === 'failed' || task.status === 'canceled';
    refs.retry.hidden = !canRetry;
    if (canRetry) {
      // 断点续传：工作目录残留部分文件时，按钮提示「继续下载」而非「重试」
      refs.retry.textContent = task.resumable ? '继续下载' : '重试';
      refs.retry.title = task.resumable
        ? '从上次中断处继续（已保留已下载部分，不会从头重下）'
        : '重新下载';
    }
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
    refs.save.href = `${refs.base || window.VDL_API_BASE || ''}/api/tasks/${task.task_id}/file?download=1&device=${encodeURIComponent(deviceId())}`;
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
    refs.convertStatus.classList.remove('is-progress');
    if (refs.convertProgress) {
      refs.convertProgress.hidden = false;
      refs.convertProgress.classList.add('is-indeterminate');
      if (refs.convertProgressFill) refs.convertProgressFill.style.width = '';
    }
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
            if (refs.convertProgress) {
              refs.convertProgress.classList.remove('is-indeterminate');
              if (refs.convertProgressFill) refs.convertProgressFill.style.width = '100%';
              setTimeout(() => { refs.convertProgress.hidden = true; }, 400);
            }
            refs.convertFile.href = `${base || window.VDL_API_BASE || ''}/api/convert/${jobId}/file?device=${encodeURIComponent(deviceId())}`;
            refs.convertFile.setAttribute('download', st.filename || 'converted');
            refs.convertFile.hidden = false;
            refs.convertStatus.textContent = '转换完成 ✅';
            refs.convertStatus.classList.remove('is-progress');
            refs.convertBtn.disabled = false;
          } else if (st.status === 'running') {
            const p = typeof st.progress === 'number' ? st.progress : 0;
            if (refs.convertProgress) {
              if (p > 0) {
                refs.convertProgress.classList.remove('is-indeterminate');
                if (refs.convertProgressFill) refs.convertProgressFill.style.width = p + '%';
              } else {
                refs.convertProgress.classList.add('is-indeterminate');
              }
            }
            refs.convertStatus.textContent = p > 0 ? `转码中 ${p}%` : '转码中…';
            refs.convertStatus.classList.add('is-progress');
          } else if (st.status === 'failed') {
            clearInterval(timer);
            if (refs.convertProgress) refs.convertProgress.hidden = true;
            refs.convertStatus.textContent = '转换失败：' + (st.error || '未知错误');
            refs.convertStatus.classList.remove('is-progress');
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

  // ------------------------------------------------------------------ 上传视频直接转码（多文件批量：每个文件独立一行 + 独立输出格式）
  // 设计：前端模拟批量，后端复用单文件 /api/upload-convert。状态用 pending/uploading/running/completed/failed。
  // 并发：最大 2 个同时转码（普通 ffmpeg 重任务，避免 CPU/带宽占满）。
  // 2026-08-24 上传提速：单文件分片并发（32MB/片 × 4 路，单片失败重试 2 次），
  // 文件级并发 2（避免多文件抢占带宽），大文件总连接数 = 2×4 = 8，HTTP/1.1 排队 HTTP/2 全并发。
  const UC_MAX_CONCURRENT = 2; // 文件级上传并发
  const UC_CHUNK_SIZE = 32 * 1024 * 1024;       // 单片 32MB
  const UC_CHUNK_CONCURRENCY = 4;               // 单文件分片并发路数
  const UC_CHUNK_RETRIES = 2;                   // 单片失败重试次数（网络抖动自动重传）
  const ucState = { list: [], nextId: 1, active: 0, polling: null };

  const ucFormatSize = (bytes) => {
    if (!bytes && bytes !== 0) return '';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(2) + ' MB';
    return (bytes / 1024 / 1024 / 1024).toFixed(2) + ' GB';
  };

  const ucFormatSpeed = (bps) => {
    if (!bps || bps <= 0) return '';
    if (bps > 1024 * 1024) return (bps / 1024 / 1024).toFixed(1) + ' MB/s';
    return (bps / 1024).toFixed(0) + ' KB/s';
  };

  const ucReadBulk = () => ({
    target: el.ucBulkTarget.value,
    res: el.ucBulkRes.value,
    bitrate: el.ucBulkBitrate.value.trim(),
    audio: el.ucBulkAudio.checked,
    rotate: el.ucBulkRotate.value,
    remux: el.ucBulkRemux.checked,
    toLibrary: el.ucBulkLibrary.checked,
  });

  const ucApplyBulk = () => {
    const b = ucReadBulk();
    let n = 0;
    ucState.list.forEach(it => {
      if (it.status === 'pending') {
        Object.assign(it, b); n++;
      }
    });
    renderUcList();
    el.ucStatus.textContent = n ? `已把批量参数应用到 ${n} 个未开始项` : '没有可应用的项（所有项都已开始/完成）';
  };

  // 输出格式下拉选项（格式列表来自节点配置，缺省回退硬编码；音频类标注「仅音频」）
  const AUDIO_ONLY_FMTS = ['mp3','m4a','aac','wav','flac','ogg','opus','wma','mp2'];
  const fmtOptions = (val) => (node.convertTargets.length ? node.convertTargets : ['mp4','mov','mkv','webm','avi','flv','ts','m4v','wmv','mpeg','3gp','ogv','hevc','mp3','m4a','aac','wav','flac','ogg','opus','wma','mp2','gif'])
    .map(v => `<option value="${v}"${v===val?' selected':''}>${v.toUpperCase()}${AUDIO_ONLY_FMTS.includes(v)?'（仅音频）':''}${v==='gif'?'（前5秒）':''}${v==='hevc'?'（H.265 省空间）':''}</option>`)
    .join('');

  const renderUcList = () => {
    const list = ucState.list;
    el.ucCount.textContent = list.length ? `已添加 ${list.length} 个文件` : '尚未添加文件';
    el.ucClearBtn.hidden = list.length === 0;
    // 批量参数区常显：允许在添加文件前先设好转换条件（2026-08-23）
    el.ucStartAllBtn.hidden = true; // 已自动开始，按钮不再需要
    el.ucStartAllBtn.disabled = !list.some(it => it.status === 'pending');
    // 批量「默认输出格式」下拉只填充一次（避免每次渲染重建导致选中/焦点被打断而闪烁）
    if (el.ucBulkTarget && !el.ucBulkTarget.dataset.inited) {
      el.ucBulkTarget.dataset.inited = '1';
      el.ucBulkTarget.innerHTML = fmtOptions(el.ucBulkTarget.value || 'mp4');
    }
    // 大小上限提示（来自节点配置；默认 10GB）
    if (el.ucLimitTip) {
      const mb = node.convertMaxUpload || 0;
      const gb = mb > 0 ? (mb / 1024 / 1024 / 1024).toFixed(1) : '10.0';
      el.ucLimitTip.textContent = `单个文件最大 ${gb}GB（大文件已自动分片并发上传提速）；超过上限请先「解析下载」再在任务卡片里点转换，服务器直转更快，无需上传。`;
    }

    if (!list.length) { el.ucList.innerHTML = ''; return; }

    el.ucList.innerHTML = list.map(it => {
      const statusText = {
        pending: '未开始',
        uploading: `上传中 ${it.progress||0}%${it.speedText ? ' · ' + it.speedText : ''}${it.uploadedText ? ' · ' + it.uploadedText : ''}`,
        running: it.progress ? `转码中 ${it.progress}%` : '转码中…',
        completed: '完成 ✅',
        failed: '失败：' + (it.errorMsg || ''),
      }[it.status] || it.status;
      const statusCls = it.status === 'pending' ? '' : 'is-' + it.status.replace('uploading','running');
      // 移除按钮：pending/failed/uploading 可移除（上传中=取消上传）；转码中禁用
      const disabled = it.status !== 'pending' && it.status !== 'failed' && it.status !== 'uploading' ? 'disabled' : '';
      const progressHtml = (it.status === 'running' || it.status === 'uploading')
        ? `<div class="progress"><div class="progress-fill" style="width:${it.progress||0}%"></div></div>` : '';
      const downloadHtml = it.status === 'completed' && it.downloadUrl
        ? `<a class="uc-item-download" href="${it.downloadUrl}" download="${it.outputName||'converted'}">下载</a>${it.libraryId ? ' · 已存媒体库' : ''}`
        : '';
      const targetDisabled = (it.status === 'running' || it.status === 'uploading' || it.status === 'completed') ? 'disabled' : '';
      return `
        <li class="uc-item ${statusCls}" data-id="${it.id}">
          <div class="uc-item-main">
            <div class="uc-item-name" title="${it.file.name}">${it.file.name}</div>
            <div class="uc-item-meta">
              <span>${ucFormatSize(it.file.size)}</span>
              <span>→ ${it.target.toUpperCase()}</span>
              ${it.res && it.res !== 'original' ? `<span>${it.res}p</span>` : ''}
              ${it.remux ? '<span>仅换容器</span>' : ''}
            </div>
            ${progressHtml}
            <div class="uc-item-status">${statusText}</div>
          </div>
          <div class="uc-item-side">
            <label class="sr-only" for="ucItemTarget-${it.id}">输出格式</label>
            <select id="ucItemTarget-${it.id}" data-act="target" ${targetDisabled}>${fmtOptions(it.target)}</select>
            ${downloadHtml}
            <button type="button" class="uc-item-remove" data-act="remove" title="从列表移除" ${disabled}>×</button>
          </div>
        </li>
      `;
    }).join('');
  };

  const ucAddFiles = (fileList) => {
    const b = ucReadBulk();
    Array.from(fileList).forEach(f => {
      ucState.list.push({
        id: ucState.nextId++,
        file: f,
        target: b.target, res: b.res, bitrate: b.bitrate,
        audio: b.audio, rotate: b.rotate, remux: b.remux, toLibrary: b.toLibrary,
        status: 'pending', jobId: null, progress: 0,
        errorMsg: '', downloadUrl: '', outputName: '', libraryId: null,
      });
    });
    renderUcList();
    // 2026-08-23 自动开始：添加文件即自动排队上传（受并发限制），无需手动点「开始转码」，
    // 上传完成自动提交转码；排队中的项仍可改批量参数并「应用到未开始」。
    el.ucStatus.textContent = `已添加 ${ucState.list.length} 个文件，自动开始上传转码…`;
    ucPump();
  };

  // 取消单个上传中任务：abort 分片 XHR + 通知后端清理已传分片 + 释放并发槽
  const ucCancelUpload = (it) => {
    it._removed = true;
    if (it._xhrs) it._xhrs.forEach(x => { try { x.abort(); } catch (e) { /* ignore */ } });
    if (it._uploadId) {
      const fd = new FormData();
      fd.append('upload_id', it._uploadId);
      fetch('/api/upload-chunk/abort', { method: 'POST', body: fd, headers: { 'X-Device-Id': deviceId() } }).catch(() => { /* 失败靠 24h 孤儿清理兜底 */ });
    }
  };

  const ucRemoveItem = (id) => {
    const it = ucState.list.find(x => x.id === id);
    if (!it) return;
    if (it.status === 'running') {
      el.ucStatus.textContent = '该项正在转码中，无法移除（请等待完成或失败）';
      return;
    }
    if (it.status === 'uploading') {
      ucCancelUpload(it);
      ucState.list = ucState.list.filter(x => x.id !== id);
      ucState.active = Math.max(0, ucState.active - 1);  // 释放并发槽（ucUploadOne 不会 resolve）
      renderUcList();
      el.ucStatus.textContent = '已取消上传并移除';
      ucPump();  // 拉下一个 pending
      return;
    }
    ucState.list = ucState.list.filter(x => x.id !== id);
    renderUcList();
  };

  const ucClearAll = () => {
    const running = ucState.list.filter(x => x.status === 'running').length;
    if (running) {
      el.ucStatus.textContent = `有 ${running} 个任务正在转码，请等待完成后再清空`;
      return;
    }
    // 取消所有上传中项，再清空
    const uploading = ucState.list.filter(x => x.status === 'uploading');
    uploading.forEach(ucCancelUpload);
    ucState.active = Math.max(0, ucState.active - uploading.length);
    ucState.list = [];
    renderUcList();
    el.ucStatus.textContent = uploading.length ? `已取消 ${uploading.length} 个上传并清空列表` : '已清空列表';
    ucPump();
  };

  // 上传单个分片（32MB；小文件=1 片，与整传等效）
  // onProgress(loaded) 让调用方合并 in-flight 字节算总进度，避免「长时间 0%」假卡死
  // xhrs(Set) 收集进行中的 XHR，供「上传中删除」时 abort
  const ucUploadChunk = (uploadId, index, total, blob, onProgress, xhrs) => new Promise((resolve, reject) => {
    const form = new FormData();
    form.append('upload_id', uploadId);
    form.append('index', index);
    form.append('total', total);
    form.append('file', blob, 'chunk');
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/upload-chunk');
    // 设备隔离：XHR 不走 request() 封装，需手动带设备 ID（否则 job 无归属，文件不隔离）
    xhr.setRequestHeader('X-Device-Id', deviceId());
    if (xhrs) xhrs.add(xhr);
    const cleanup = () => { if (xhrs) xhrs.delete(xhr); };
    if (onProgress) {
      xhr.upload.addEventListener('progress', (ev) => {
        if (ev.lengthComputable) onProgress(ev.loaded);
      });
    }
    xhr.addEventListener('load', () => {
      cleanup();
      if (xhr.status >= 200 && xhr.status < 300) resolve();
      else {
        let msg = '分片上传失败 HTTP ' + xhr.status;
        try { const d = JSON.parse(xhr.responseText || '{}'); if (d.detail) msg = d.detail; } catch (e) { /* ignore */ }
        reject(new Error(msg));
      }
    });
    xhr.addEventListener('error', () => { cleanup(); reject(new Error('网络错误')); });
    xhr.addEventListener('abort', () => { cleanup(); reject(new Error('已取消')); });
    xhr.send(form);
  });

  // 上传 + 启动单个 job：32MB 分片 × 4 路并发，进度占 30%（转码从 30% 累加），实时速度显示
  const ucUploadOne = (item) => new Promise((resolve, reject) => {
    item.status = 'uploading';
    item.progress = 0;
    item.speedText = '';
    item.uploadedText = '';
    item._removed = false;            // 上传中删除标记（abort 后不再重试/不再 finish）
    item._xhrs = new Set();           // 进行中的分片 XHR（删除时 abort）
    renderUcList();
    const file = item.file;
    const totalChunks = Math.max(1, Math.ceil(file.size / UC_CHUNK_SIZE));
    const uploadId = item._uploadId = 'uc' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
    let uploadedBytes = 0;          // 已成功分片的累计字节
    const done = new Set();          // 已成功分片 index（重试去重）
    const inFlight = new Map();      // 正在上传分片 index → 当前 loaded 字节（合并算进度，避免「长时间 0%」）
    const t0 = performance.now();
    let lastBytes = 0, lastT = t0;

    // 计算总已上传字节 = 已完成分片 + 所有 in-flight 分片当前 loaded
    const totalUploaded = () => {
      let t = uploadedBytes;
      for (const v of inFlight.values()) t += v;
      return t;
    };

    const updateProgress = () => {
      const now = performance.now();
      const tot = totalUploaded();
      const dt = (now - lastT) / 1000;
      if (dt > 0.4) {                // 0.4s 平滑窗口算瞬时速度
        item.speedText = ucFormatSpeed((tot - lastBytes) / dt);
        lastBytes = tot;
        lastT = now;
      }
      item.uploadedText = `${ucFormatSize(tot)} / ${ucFormatSize(file.size)}`;
      item.progress = Math.round(tot / file.size * 30);
      // 定向更新该行 DOM（进度条 + 状态文字），不重建整个列表——
      // 否则高频渲染会反复重建下拉/卡片导致闪烁与焦点丢失
      const li = document.querySelector(`.uc-item[data-id="${item.id}"]`);
      if (li) {
        const fill = li.querySelector('.progress-fill');
        if (fill) fill.style.width = `${item.progress || 0}%`;
        const st = li.querySelector('.uc-item-status');
        if (st) st.textContent = `上传中 ${item.progress || 0}%${item.speedText ? ' · ' + item.speedText : ''}${item.uploadedText ? ' · ' + item.uploadedText : ''}`;
      }
    };

    // 分片并发 worker 池：每片失败重试 UC_CHUNK_RETRIES 次，耗尽则整个文件失败
    let idx = 0;
    const workers = [];
    const worker = async () => {
      while (idx < totalChunks) {
        if (item._removed || item.status === 'failed') return;
        const i = idx++;
        const start = i * UC_CHUNK_SIZE;
        const end = Math.min(start + UC_CHUNK_SIZE, file.size);
        const blob = file.slice(start, end);
        let attempts = 0;
        for (;;) {
          try {
            await ucUploadChunk(uploadId, i, totalChunks, blob, (loaded) => {
              inFlight.set(i, loaded);  // 单片实时进度反馈
              updateProgress();
            }, item._xhrs);
            break;
          } catch (e) {
            if (item._removed) return;  // 用户已删除：直接退出，不重试不报错
            attempts++;
            if (attempts > UC_CHUNK_RETRIES) {
              item.status = 'failed';
              item.errorMsg = `分片 ${i + 1}/${totalChunks} 上传失败：${e.message}`;
              item.speedText = ''; item.uploadedText = '';
              renderUcList();
              reject(new Error(item.errorMsg));
              return;
            }
          }
        }
        inFlight.delete(i);
        if (!done.has(i)) { done.add(i); uploadedBytes += (end - start); }
        updateProgress();
      }
    };
    for (let w = 0; w < UC_CHUNK_CONCURRENCY; w++) workers.push(worker());

    // 全部分片完成 → finish 合并并提交转码 job
    Promise.allSettled(workers).then(async () => {
      if (item._removed || item.status === 'failed') return;
      try {
        const form = new FormData();
        form.append('upload_id', uploadId);
        form.append('total', totalChunks);
        form.append('filename', file.name);
        form.append('target', item.target);
        form.append('resolution', item.res);
        form.append('bitrate', item.bitrate || '');
        form.append('audio', item.audio ? 'true' : 'false');
        form.append('rotate', item.rotate);
        form.append('remux', item.remux ? 'true' : 'false');
        form.append('to_library', item.toLibrary ? 'true' : 'false');
        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/upload-chunk/finish');
        xhr.setRequestHeader('X-Device-Id', deviceId());
        xhr.timeout = 120000;  // finish 含合并+提交转码，CF/Railway 链路偶发 30s+ 慢响应，给浏览器 XHR 2 分钟兜底
        xhr.addEventListener('load', () => {
          try {
            const data = JSON.parse(xhr.responseText || '{}');
            if (xhr.status >= 200 && xhr.status < 300 && data.job_id) {
              item.jobId = data.job_id;
              item.status = 'running';
              item.progress = 30;
              item.speedText = ''; item.uploadedText = '';
              renderUcList();
              resolve(data);
            } else {
              item.status = 'failed';
              item.errorMsg = data.detail || data.error || ('HTTP ' + xhr.status);
              item.speedText = ''; item.uploadedText = '';
              renderUcList();
              reject(new Error(item.errorMsg));
            }
          } catch (e) {
            // responseText 非 JSON（HTML 错误页/空响应/被代理截断）—— 通常是 Cloudflare↔Railway 链路抖动，
            // 提示用户重新上传（finish 是合并+提交转码一次性操作，无法重试，只能重传）
            item.status = 'failed';
            item.errorMsg = '服务器响应中断（可能是网络/代理超时），请重新上传文件';
            renderUcList();
            reject(e);
          }
        });
        xhr.addEventListener('error', () => {
          item.status = 'failed';
          item.errorMsg = '网络错误（请重新上传文件）';
          renderUcList();
          reject(new Error('network'));
        });
        xhr.addEventListener('timeout', () => {
          item.status = 'failed';
          item.errorMsg = '上传完成但响应超时（请重新上传文件）';
          renderUcList();
          reject(new Error('finish timeout'));
        });
        xhr.send(form);
      } catch (e) {
        if (item.status !== 'failed') {
          item.status = 'failed';
          item.errorMsg = e.message || '上传失败';
          renderUcList();
          reject(e);
        }
      }
    });
  });

  // 启动下一个 pending 任务（受并发限制）
  const ucPump = () => {
    while (ucState.active < UC_MAX_CONCURRENT) {
      const next = ucState.list.find(x => x.status === 'pending');
      if (!next) break;
      ucState.active++;
      ucUploadOne(next)
        .catch(() => { /* 失败已在 ucUploadOne 标记 */ })
        .finally(() => {
          ucState.active--;
          ucPump(); // 拉下一个
        });
    }
    // 启停轮询定时器：没有 running 任务就停
    const hasRunning = ucState.list.some(x => x.status === 'running');
    if (hasRunning && !ucState.polling) {
      ucState.polling = setInterval(ucPollAll, 3000);
    } else if (!hasRunning && ucState.polling) {
      clearInterval(ucState.polling);
      ucState.polling = null;
      const done = ucState.list.filter(x => x.status === 'completed').length;
      const fail = ucState.list.filter(x => x.status === 'failed').length;
      const remain = ucState.list.filter(x => x.status === 'pending').length;
      const parts = [];
      if (done) parts.push(`${done} 完成`);
      if (fail) parts.push(`${fail} 失败`);
      if (remain) parts.push(`${remain} 未开始`);
      el.ucStatus.textContent = parts.length ? `批量结束：${parts.join('，')}` : '批量结束';
    }
  };

  // 轮询所有 running 任务的状态
  const ucPollAll = async () => {
    const running = ucState.list.filter(x => x.status === 'running' && x.jobId);
    await Promise.all(running.map(async (it) => {
      try {
        const st = await request('/api/convert/' + it.jobId);
        if (st.status === 'running') {
          const p = typeof st.progress === 'number' ? st.progress : 0;
          // 转码进度 30% → 100%
          it.progress = Math.max(30, Math.min(100, Math.round(30 + p * 0.7)));
          renderUcList();
        } else if (st.status === 'completed') {
          it.status = 'completed';
          it.progress = 100;
          it.outputName = st.filename || 'converted';
          it.downloadUrl = `${window.VDL_API_BASE || ''}/api/convert/${it.jobId}/file?device=${encodeURIComponent(deviceId())}`;
          it.libraryId = st.library_id || null;
          renderUcList();
        } else if (st.status === 'failed') {
          it.status = 'failed';
          it.errorMsg = st.error || '未知错误';
          renderUcList();
        }
      } catch (_e) { /* 单个轮询失败忽略 */ }
    }));
  };

  // 事件绑定
  el.ucAddBtn.addEventListener('click', () => el.ucFileInput.click());
  el.ucFileInput.addEventListener('change', () => {
    if (el.ucFileInput.files && el.ucFileInput.files.length) {
      ucAddFiles(el.ucFileInput.files);
      el.ucFileInput.value = ''; // 允许重复添加同名文件
    }
  });
  el.ucList.addEventListener('change', (e) => {
    const t = e.target;
    if (t.dataset.act === 'target') {
      const li = t.closest('.uc-item');
      const id = +li.dataset.id;
      const it = ucState.list.find(x => x.id === id);
      if (it) { it.target = t.value; renderUcList(); }
    }
  });
  el.ucList.addEventListener('click', (e) => {
    const t = e.target.closest('[data-act]');
    if (!t) return;
    if (t.dataset.act === 'remove') {
      const li = t.closest('.uc-item');
      ucRemoveItem(+li.dataset.id);
    }
  });
  el.ucClearBtn.addEventListener('click', ucClearAll);
  el.ucBulkApplyBtn.addEventListener('click', ucApplyBulk);
  el.ucStartAllBtn.addEventListener('click', () => {
    const hasPending = ucState.list.some(x => x.status === 'pending');
    if (!hasPending) { el.ucStatus.textContent = '没有可开始的项'; return; }
    el.ucStatus.textContent = '批量转换中…';
    ucPump();
  });

  // ------------------------------------------------------------------ 去水印（需求文档模块二）

  // 图片 / PDF 子模式切换
  const dwSwitchPane = (toImg) => {
    el.dwImgPane.hidden = !toImg;
    el.dwPdfPane.hidden = toImg;
    el.dwModeImg.classList.toggle('is-active', toImg);
    el.dwModePdf.classList.toggle('is-active', !toImg);
    el.dwImgStatus.textContent = '';
    el.dwPdfStatus.textContent = '';
  };
  el.dwModeImg.addEventListener('click', () => dwSwitchPane(true));
  el.dwModePdf.addEventListener('click', () => dwSwitchPane(false));

  // PDF 模式切换时展示/隐藏栅格化选项
  el.dwPdfMode.addEventListener('change', () => {
    el.dwPdfRasterOpts.hidden = el.dwPdfMode.value !== 'raster';
  });

  // 图片预览 + 框选区域
  // ---- 图片去水印：多选区（新建/加选/减选/撤销/清空） ----
  let dwSelections = [];   // [{x,y,w,h,op}] 归一化 0..1，op: 'add' | 'subtract'
  let dwDrawMode = 'new';  // 'new' | 'add' | 'subtract'
  let dwDragging = false, dwStartX = 0, dwStartY = 0, dwCur = null;

  const dwResizeCanvas = () => {
    const img = el.dwImgPreview, cv = el.dwImgCanvas;
    if (!img || !cv || !img.clientWidth) return;
    cv.width = img.clientWidth;
    cv.height = img.clientHeight;
    cv.style.width = img.clientWidth + 'px';
    cv.style.height = img.clientHeight + 'px';
    dwDrawCanvas();
  };
  const dwDrawCanvas = () => {
    const cv = el.dwImgCanvas;
    if (!cv) return;
    const ctx = cv.getContext('2d');
    ctx.clearRect(0, 0, cv.width, cv.height);
    const draw = (s, active) => {
      const add = !s.op || s.op === 'add';
      const x = s.x * cv.width, y = s.y * cv.height;
      const w = s.w * cv.width, h = s.h * cv.height;
      ctx.lineWidth = 2;
      ctx.setLineDash(active ? [6, 4] : []);
      ctx.strokeStyle = add ? '#2ecc71' : '#e74c3c';
      ctx.fillStyle = add ? 'rgba(46,204,113,.18)' : 'rgba(231,76,60,.18)';
      ctx.fillRect(x, y, w, h);
      ctx.strokeRect(x, y, w, h);
    };
    dwSelections.forEach((s) => draw(s, false));
    if (dwCur) draw(dwCur, true);
    ctx.setLineDash([]);
    const adds = dwSelections.filter((s) => !s.op || s.op === 'add').length;
    const subs = dwSelections.filter((s) => s.op === 'subtract').length;
    if (el.dwSelInfo) el.dwSelInfo.textContent = dwSelections.length ? `已选 ${adds} 加 / ${subs} 减` : '尚未框选';
  };
  const dwNormFromEvent = (clientX, clientY) => {
    const img = el.dwImgPreview, rect = img.getBoundingClientRect();
    const nx = Math.min(Math.max((clientX - rect.left) / rect.width, 0), 1);
    const ny = Math.min(Math.max((clientY - rect.top) / rect.height, 0), 1);
    return [nx, ny];
  };

  el.dwImgFile.addEventListener('change', () => {
    const f = el.dwImgFile.files[0];
    if (!f) return;
    const url = URL.createObjectURL(f);
    el.dwImgPreview.src = url;
    el.dwImgPreview.onload = () => { URL.revokeObjectURL(url); dwResizeCanvas(); };
    dwSelections = [];
    dwCur = null;
    dwDrawCanvas();
    el.dwImgResult.hidden = true;
    el.dwImgStatus.textContent = '';
  });

  el.dwImgPreview.addEventListener('mousedown', (e) => {
    if (!el.dwImgPreview.src) return;
    dwDragging = true;
    const [nx, ny] = dwNormFromEvent(e.clientX, e.clientY);
    dwStartX = nx; dwStartY = ny;
    if (dwDrawMode === 'new') dwSelections = [];
    dwCur = { x: nx, y: ny, w: 0, h: 0, op: dwDrawMode === 'subtract' ? 'subtract' : 'add' };
    dwDrawCanvas();
    e.preventDefault();
  });
  window.addEventListener('mousemove', (e) => {
    if (!dwDragging || !dwCur) return;
    const [nx, ny] = dwNormFromEvent(e.clientX, e.clientY);
    dwCur.x = Math.min(dwStartX, nx);
    dwCur.y = Math.min(dwStartY, ny);
    dwCur.w = Math.abs(nx - dwStartX);
    dwCur.h = Math.abs(ny - dwStartY);
    dwDrawCanvas();
  });
  window.addEventListener('mouseup', () => {
    if (!dwDragging) return;
    dwDragging = false;
    if (dwCur) {
      // 误点（区域过小）则丢弃
      if (dwCur.w > 0.004 && dwCur.h > 0.004) dwSelections.push(dwCur);
      dwCur = null;
    }
    dwDrawCanvas();
  });
  window.addEventListener('resize', dwResizeCanvas);

  // 选区工具按钮（新建/加选/减选/撤销/清空）
  (document.querySelectorAll('.dw-mode') || []).forEach((btn) => {
    btn.addEventListener('click', () => {
      if (btn.dataset.mode) {
        dwDrawMode = btn.dataset.mode;
        document.querySelectorAll('.dw-mode[data-mode]').forEach((b) => b.classList.toggle('is-active', b === btn));
      } else if (btn.dataset.act === 'undo') {
        dwSelections.pop();
        dwDrawCanvas();
      } else if (btn.dataset.act === 'clear') {
        dwSelections = [];
        dwDrawCanvas();
      }
    });
  });

  const startDwImage = async () => {
    const file = el.dwImgFile.files[0];
    if (!file) { el.dwImgStatus.textContent = '请先选择图片文件'; return; }
    const valid = dwSelections.filter((s) => s.w > 0 && s.h > 0);
    if (!valid.length) {
      el.dwImgStatus.textContent = '请在预览图上拖拽框选水印区域'; return;
    }
    el.dwImgBtn.disabled = true;
    el.dwImgStatus.textContent = '去水印处理中…';
    el.dwImgResult.hidden = true;
    const form = new FormData();
    form.append('file', file);
    form.append('regions', JSON.stringify(valid.map((s) => ({
      x: +s.x.toFixed(4), y: +s.y.toFixed(4),
      w: +s.w.toFixed(4), h: +s.h.toFixed(4),
      op: s.op || 'add',
    }))));
    form.append('method', el.dwImgMethod.value);
    form.append('radius', el.dwImgRadius.value);
    try {
      const data = await request('/api/dw/image', { method: 'POST', body: form });
      const jobId = data.job_id;
      const timer = setInterval(async () => {
        try {
          const st = await request('/api/dw/image/' + jobId);
          if (st.status === 'completed') {
            clearInterval(timer);
            el.dwImgOrig.src = URL.createObjectURL(file);
            el.dwImgOut.src = `${window.VDL_API_BASE || ''}/api/dw/image/${jobId}/file`;
            el.dwImgDownload.href = `${window.VDL_API_BASE || ''}/api/dw/image/${jobId}/file`;
            el.dwImgDownload.dataset.jobId = jobId;
            el.dwImgDownload.setAttribute('download', st.filename || 'dewatered');
            el.dwImgResult.hidden = false;
            el.dwImgStatus.textContent = '去水印完成 ✅';
            el.dwImgBtn.disabled = false;
          } else if (st.status === 'failed') {
            clearInterval(timer);
            el.dwImgStatus.textContent = '失败：' + (st.error || '未知错误');
            el.dwImgBtn.disabled = false;
          }
        } catch (_e) { /* 轮询继续 */ }
      }, 3000);
    } catch (error) {
      el.dwImgBtn.disabled = false;
      el.dwImgStatus.textContent = (error && error.message) ? ('请求失败：' + error.message) : '请求失败';
    }
  };
  el.dwImgBtn.addEventListener('click', startDwImage);

  const startDwPdf = async () => {
    const file = el.dwPdfFile.files[0];
    if (!file) { el.dwPdfStatus.textContent = '请先选择 PDF 文件'; return; }
    const mode = el.dwPdfMode.value;
    el.dwPdfBtn.disabled = true;
    el.dwPdfStatus.textContent = 'PDF 去水印处理中…';
    el.dwPdfResult.hidden = true;
    const form = new FormData();
    form.append('file', file);
    form.append('mode', mode);
    if (mode === 'raster') {
      const pct = (id) => Math.min(Math.max(parseFloat(el[id].value) || 0, 0), 100) / 100;
      form.append('x', pct('dwPdfX').toFixed(4));
      form.append('y', pct('dwPdfY').toFixed(4));
      form.append('w', pct('dwPdfW').toFixed(4));
      form.append('h', pct('dwPdfH').toFixed(4));
      form.append('method', el.dwPdfMethod.value);
      form.append('radius', el.dwPdfRadius.value);
      form.append('dpi', el.dwPdfDpi.value);
    }
    try {
      const data = await request('/api/dw/pdf', { method: 'POST', body: form });
      const jobId = data.job_id;
      const timer = setInterval(async () => {
        try {
          const st = await request('/api/dw/pdf/' + jobId);
          if (st.status === 'completed') {
            clearInterval(timer);
            el.dwPdfDownload.href = `${window.VDL_API_BASE || ''}/api/dw/pdf/${jobId}/file`;
            el.dwPdfDownload.dataset.jobId = jobId;
            el.dwPdfDownload.setAttribute('download', st.filename || 'dewatered.pdf');
            el.dwPdfResult.hidden = false;
            el.dwPdfStatus.textContent = '去水印完成 ✅';
            el.dwPdfBtn.disabled = false;
          } else if (st.status === 'failed') {
            clearInterval(timer);
            el.dwPdfStatus.textContent = '失败：' + (st.error || '未知错误');
            el.dwPdfBtn.disabled = false;
          }
        } catch (_e) { /* 轮询继续 */ }
      }, 3000);
    } catch (error) {
      el.dwPdfBtn.disabled = false;
      el.dwPdfStatus.textContent = (error && error.message) ? ('请求失败：' + error.message) : '请求失败';
    }
  };
  el.dwPdfBtn.addEventListener('click', startDwPdf);

  // 去水印结果下载：桌面端(pywebview/WKWebView) <a download> 不弹保存框，
  // 优先调用原生 Python 桥接 save_dw_file_dialog 弹出系统保存面板，由用户自选位置/重命名；
  // 桥接不可用(web/浏览器)时回退到 <a download>。jobId 优先从 data-job-id 读取，
  // 缺失时再从 href 正则兜底。
  const dwDownload = async (btn, kind) => {
    const href = btn.href || '';
    const jobId = btn.dataset.jobId || (() => {
      const m = href.match(/\/api\/dw\/(?:image|pdf)\/([^/?#]+)(?:\/file)?/);
      return m ? m[1] : null;
    })();
    const filename = btn.getAttribute('download') || (kind === 'image' ? 'dewatered.png' : 'dewatered.pdf');
    const api = window.pywebview && window.pywebview.api;
    // 桌面桥接可用 → 弹原生保存面板，用户自选位置写盘（最稳，绕开 WebView 下载限制）
    if (api && api.save_dw_file_dialog && jobId) {
      const orig = btn.textContent;
      btn.textContent = '选择保存位置…';
      btn.disabled = true;
      try {
        const res = await api.save_dw_file_dialog(jobId, kind, filename);
        if (res === 'CANCELLED') {
          btn.textContent = orig;  // 用户取消，恢复按钮
        } else if (typeof res === 'string' && res.startsWith('ERROR:')) {
          alert('保存失败：' + res.replace(/^ERROR:\s*/, ''));
          btn.textContent = orig;
        } else {
          btn.textContent = '已保存：' + res;  // 显示实际保存路径
          setTimeout(() => { btn.textContent = orig; }, 4000);
        }
      } catch (err) {
        alert('保存失败：' + (err && err.message ? err.message : err));
        btn.textContent = orig;
      } finally {
        btn.disabled = false;
      }
      return;
    }
    // 回退：web / 浏览器模式直接触发 <a download>
    if (jobId) {
      const a = document.createElement('a');
      a.href = href;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
    } else {
      alert('未找到去水印任务，无法下载。\nhref=' + href + '\njobId=' + String(jobId));
    }
  };
  el.dwImgDownload.addEventListener('click', (e) => {
    e.preventDefault();
    dwDownload(el.dwImgDownload, 'image');
  });
  el.dwPdfDownload.addEventListener('click', (e) => {
    e.preventDefault();
    dwDownload(el.dwPdfDownload, 'pdf');
  });

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
      if (el.cookieContribute.checked) {           // 默认勾选即贡献，共享登录态给其他人（取消勾选则不贡献）
        urls.forEach((u) => contributeCookie(u, cookie));
      }
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

    const source = new EventSource(`${base || window.VDL_API_BASE || ''}/api/tasks/${taskId}/events?device=${encodeURIComponent(deviceId())}`);
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

  /** 从任意文本中提取有效的 http(s) URL（处理用户粘贴带标题/参数的多行分享内容）。
   *  特殊处理：B站 vd_source 等查询参数单独成行时，合并到前一个 URL 末尾。
   */
  const extractUrls = (text) => {
    const raw = text.match(/https?:\/\/[^\s<>"')\]]+/g) || [];
    const merged = [];
    for (const t of raw) {
      if (/^[?&][a-zA-Z_]/.test(t) && merged.length) {
        // 查询参数独行（如 "vd_source=xxx"）→ 拼接到上一个 URL
        merged[merged.length - 1] += t;
      } else {
        merged.push(t);
      }
    }
    return merged.filter((u) => /\.[a-z]{2,}\/|:\/\/[^/]+\//.test(u)).map(_normalize_url);
  };

  /** 短链归一化：去掉分享时 App 拼在短码后的尾巴（"/Vlp/..." 等分享参数），
   * 否则后端解析会拿到非视频页（如 v.douyin.com/ZcevbN5jP8/Vlp/E@u.fO:4pm）。
   * 仅对已知短链平台截断到短码。 */
  const _normalize_url = (url) => {
    if (!url) return url;
    // 抖音短链：v.douyin.com / iesdouyin.com / m.douyin.com / www.douyin.com 的短链形式
    let m = url.match(/^https?:\/\/(?:[a-z0-9-]*\.)?douyin\.com\/([A-Za-z0-9_-]{8,18})(\/.*)?$/i);
    if (m && m[1]) return `https://v.douyin.com/${m[1]}/`;
    // 快手短链
    m = url.match(/^https?:\/\/(?:[a-z0-9-]*\.)?kuaishou\.com\/(?:short-video|f|video)\/([A-Za-z0-9_-]{6,20})(\/.*)?$/i);
    if (m && m[1]) return `https://www.kuaishou.com/short-video/${m[1]}`;
    // t.cn 微博短链：截到第一个非合法短码字符前（短码通常 7-10 位）
    m = url.match(/^https?:\/\/t\.cn\/([A-Za-z0-9_-]{6,12})/i);
    if (m && m[1]) return `https://t.cn/${m[1]}`;
    return url;
  };

  // 访客自愿贡献 Cookie 到公共池（火后即弃，不阻断主流程；后端会做域名白名单+验真+限频）
  const contributeCookie = (url, cookie) => {
    if (!url || !cookie) return;
    request('/api/cookie/contribute', {
      method: 'POST',
      body: JSON.stringify({ url, cookie }),
    }).catch(() => { /* 池写入失败不影响解析/下载结果 */ });
  };

  /** 判断链接是否是「歌单/专辑」（网易云歌单、榜单、喜马拉雅专辑）。
   * 注意：网易云分享链接常带 # 锚点（如 https://music.163.com/#/playlist?id=xx），
   * new URL() 会把 # 后归到 hash 不算 pathname——这里把 hash 拼到 path 一起查。 */
  const isPlaylistUrl = (url) => {
    try {
      const u = new URL(url);
      const host = u.hostname.replace(/^www\./, '').replace(/^m\./, '');
      const pathAndHash = u.pathname + (u.hash || '');
      if (host === 'music.163.com' || host === 'y.music.163.com') {
        return pathAndHash.includes('/playlist') || pathAndHash.includes('/discover/toplist');
      }
      if (host === 'ximalaya.com') {
        return u.pathname.includes('/album/');
      }
      return false;
    } catch (e) {
      return false;
    }
  };

  /** 解析歌单/专辑并渲染列表（替代单视频 renderVideo）。 */
  const handlePlaylist = async (url, base, cookie, proxy) => {
    try {
      const data = await request('/api/playlist', { method: 'POST', body: JSON.stringify({ url, cookie, proxy }) }, base);
      data.base = base;
      renderPlaylist(data);
    } catch (error) {
      resolved = null;
      showError(error.message || '歌单解析失败', error.hint);
    }
  };

  /** 渲染歌单/专辑列表 + 批量下载入口。 */
  const renderPlaylist = (data) => {
    const items = data.items || [];
    const free = items.filter((i) => i.url && !i.is_paid).length;
    const paid = items.length - free;
    el.playlistTitle.textContent = `${data.platform?.name || '歌单'}：${data.title || '(未命名)'}`;
    el.playlistMeta.textContent =
      `共 ${data.count || items.length} 集` +
      (paid > 0 ? `（其中会员 ${paid} 集，按合规要求不支持下载）` : '') +
      `。每集右侧有「下载」按钮可单独下，或点「批量下载全部」。`;
    el.playlistList.replaceChildren();
    items.forEach((item) => {
      const row = document.createElement('div');
      row.className = 'playlist-item';
      row.dataset.url = item.url || '';
      const idx = document.createElement('span');
      idx.className = 'pl-idx';
      idx.textContent = item.index || '';
      const t = document.createElement('span');
      t.className = 'pl-title';
      t.textContent = item.title || '(无标题)';
      row.appendChild(idx);
      row.appendChild(t);
      if (item.duration) {
        const d = document.createElement('span');
        d.className = 'pl-dur';
        d.textContent = formatDuration(item.duration);
        row.appendChild(d);
      }
      if (item.is_paid) {
        // 付费项：合规红线，不提供下载（标注会员并禁用）
        const p = document.createElement('span');
        p.className = 'pl-paid';
        p.textContent = '会员';
        p.title = '会员/付费内容按合规要求不支持下载';
        row.appendChild(p);
      } else if (item.url) {
        // 免费项：单曲下载按钮
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn btn-ghost pl-btn';
        btn.textContent = '下载';
        btn.title = '只下载这一集';
        btn.onclick = async (ev) => {
          ev.stopPropagation();
          if (btn.disabled) return;
          if (!(await ensureConsent())) return;
          btn.disabled = true;
          btn.textContent = '创建中…';
          try {
            item.platformName = item.platformName || data.platform?.name || '歌单';
            await createSingleDownload(item, data.base || '');
            btn.textContent = '已创建 ✓';
            row.classList.add('done');
          } catch (e) {
            btn.textContent = '失败';
            row.classList.add('fail');
            showError('创建下载任务失败', e.message || e.hint);
          }
        };
        row.appendChild(btn);
      }
      el.playlistList.appendChild(row);
    });
    // 隐藏单视频面板区块，只展示歌单
    el.qualityBlock.hidden = true;
    el.downloadBtn.hidden = true;
    el.watchRow.hidden = true;
    el.directHint.hidden = true;
    el.serverFallbackBtn.hidden = true;
    ['extractBlock', 'speedBlock'].forEach((id) => {
      const n = document.getElementById(id);
      if (n) n.hidden = true;
    });
    el.playlistPanel.hidden = false;
    el.playlistProgress.textContent = '';
    el.playlistDownloadBtn.disabled = false;
    el.playlistDownloadBtn.onclick = () => batchDownload(data);
    el.resultPanel.hidden = false;
  };

  /** 为歌单中的单曲创建一个下载任务（不依赖 resolved，独立 URL）。 */
  const createSingleDownload = async (item, base) => {
    const data = await request('/api/download', {
      method: 'POST',
      body: JSON.stringify({
        url: item.url,
        quality: 'best',
        cookie: '',
        proxy: '',
        extract_script: el.extractSelect ? el.extractSelect.value || '' : '',
        format_id: '',
        concurrent_fragments: 0,
        downloader: 'native',
        play_url: '',
        watch_options: [],
        is_hls: false,
      }),
    }, base);
    const taskId = data.task_id;
    if (data.quota) {
      node.downloadFreeUsed = data.quota.free_used || 0;
      if (node.downloadSubRequired) refreshSubModalText();
    }
    const refs = createTaskCard(taskId, {
      title: item.title || '(无标题)',
      platform: (item.platformName || '歌单'),
    });
    refs.base = base;
    trackTask(taskId, refs, base);
    return taskId;
  };

  /** 批量下载歌单（逐个创建任务，付费项跳过；间隔 150ms 防瞬时打爆）。 */
  const batchDownload = async (data) => {
    const base = data.base || '';
    const items = (data.items || []).filter((i) => i.url && !i.is_paid);
    if (!items.length) {
      showError('没有可下载的免费内容', '该歌单/专辑可能全部为付费内容');
      return;
    }
    if (!(await ensureConsent())) return;
    const platformName = data.platform?.name || '歌单';
    items.forEach((i) => { i.platformName = platformName; });
    el.playlistDownloadBtn.disabled = true;
    let done = 0, failed = 0;
    el.playlistProgress.textContent = `正在创建任务 0/${items.length}…`;
    for (const item of items) {
      try {
        await createSingleDownload(item, base);
        done++;
        const row = [...el.playlistList.children].find((r) => r.dataset.url === item.url);
        if (row) row.classList.add('done');
      } catch (e) {
        failed++;
        const row = [...el.playlistList.children].find((r) => r.dataset.url === item.url);
        if (row) row.classList.add('fail');
      }
      el.playlistProgress.textContent = `已创建 ${done}/${items.length} 个任务${failed ? `，失败 ${failed}` : ''}…`;
      await new Promise((r) => setTimeout(r, 150));
    }
    el.playlistProgress.textContent = failed
      ? `完成：成功创建 ${done} 个，失败 ${failed} 个（失败项已标红）`
      : `完成：已创建 ${done} 个下载任务`;
    el.playlistDownloadBtn.disabled = false;
  };

  const handleResolve = async (event) => {
    event.preventDefault();
    const raw = el.input.value.trim();
    const cookie = el.cookieInput.value.trim();
    const proxy = el.proxyInput.value.trim();
    if (!raw) {
      showError('请输入视频链接', '把视频页面的地址粘贴到输入框即可');
      return;
    }

    // 智能提取 URL（兼容带标题、参数分行的多行粘贴）
    let urls = extractUrls(raw);
    if (!urls.length) {
      // 提取不到任何 URL 时，回退到原始整段文字（向后兼容旧行为）
      urls = [raw];
    }

    // 单 URL → 解析；多 URL → 批量
    if (urls.length > 1) {
      await runBatch(urls, cookie, proxy);
      return;
    }
    const url = urls[0];
    clearError();
    setLoading(true);
    el.resultPanel.hidden = true;
    el.hqTip.hidden = true;   // 每次重新解析时重置「更高分辨率」提示，避免残留
    const base = baseFor(url);
    // 歌单/专辑链接 → 走 /api/playlist 列出全部曲目（网易云歌单/榜单、喜马拉雅专辑）
    if (isPlaylistUrl(url)) {
      await handlePlaylist(url, base, cookie, proxy);
      setLoading(false);
      return;
    }
    try {
      resolved = await request('/api/resolve', { method: 'POST', body: JSON.stringify({ url, cookie, proxy }) }, base);
      resolved.cookie = cookie;
      resolved.proxy = proxy;
      resolved.base = base;                        // 后续下载/进度/取件都锁定同一节点
      renderVideo(resolved);
      if (el.cookieContribute.checked) {           // 默认勾选即贡献，共享登录态给其他人（取消勾选则不贡献）
        contributeCookie(url, cookie);
      }
    } catch (error) {
      resolved = null;
      showError(error.message || '解析失败', error.hint, '', error.category);
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
          format_id: opts.format_id ?? '',
          concurrent_fragments: el.concurrentInput.value ? parseInt(el.concurrentInput.value, 10) || 0 : 0,
          downloader: el.downloaderSelect ? el.downloaderSelect.value || 'native' : 'native',
          play_url: resolved?.video?.play_url || '',
          watch_options: resolved?.video?.watch_options || [],
          is_hls: !!resolved?.video?.is_hls,
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
      // 存储观看数据供 paintTask 渲染任务面板观看按钮
      refs._watchUrl = resolved?.video?.play_url || '';
      refs._watchOpts = resolved?.video?.watch_options || [];
      refs._watchHls = !!resolved?.video?.is_hls;
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

  const handleDownload = async () => {
    if (resolved?.video?.direct_url) {
      // 直链直存：浏览器从源站拉文件，瞬时响应，无需 loading 态
      triggerDirectDownload(resolved.video.direct_url, resolved.video.title);
      return;
    }
    // 服务器下载：loading 态防重复点击（连点会建多个任务）；后端 90s 内命中
    // 解析缓存，「解析视频信息」步骤 <1s，这里反馈也要跟上。
    if (el.downloadBtn.dataset.submitting === '1') return;
    el.downloadBtn.dataset.submitting = '1';
    el.downloadBtn.disabled = true;
    const origLabel = el.downloadBtn.lastChild.textContent;
    el.downloadBtn.lastChild.textContent = '创建任务中…';
    try {
      await startDownload(selectedQuality);
    } finally {
      el.downloadBtn.dataset.submitting = '';
      el.downloadBtn.disabled = false;
      el.downloadBtn.lastChild.textContent = origLabel;
    }
  };

  const cancelTask = async (taskId, base = '') => {
    try {
      const r = await request(`/api/tasks/${taskId}`, { method: 'DELETE' }, base);
    } catch (error) {
      showError(error.message || '取消失败', error.hint);
    }
  };

  const pauseTask = async (taskId, base = '') => {
    try {
      const r = await request(`/api/tasks/${taskId}/pause`, { method: 'POST' }, base);
    } catch (error) {
      showError(error.message || '暂停失败', error.hint);
    }
  };

  const resumeTask = async (taskId, base = '') => {
    try {
      const r = await request(`/api/tasks/${taskId}/resume`, { method: 'POST' }, base);
    } catch (error) {
      showError(error.message || '继续失败', error.hint);
    }
  };

  // 任务重试：失败 / 已取消的任务重新加入下载队列
  const retryTask = async (taskId, refs) => {
    try {
      const r = await request(`/api/tasks/${taskId}/retry`, { method: 'POST' }, refs.base || '');
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
    if (!window.confirm(`确定删除这条任务记录吗？${fileNote}`)) {
      return;
    }
    try {
      _deletingIds.add(taskId);          // 标记「正在删除」，防止 syncMissingCards 重建
      const tr = trackers.get(taskId);     // 停掉该任务的 SSE/轮询
      if (tr) { tr.source?.close(); clearInterval(tr.timer); trackers.delete(taskId); }
      await request(`/api/tasks/${taskId}`, { method: 'DELETE' }, refs.base || '');
      refs.root.remove();                // 从 DOM 移除
      // 保留在 _deletingIds 约 10 秒，覆盖可能的延迟轮询
      setTimeout(() => _deletingIds.delete(taskId), 10000);
    } catch (error) {
      _deletingIds.delete(taskId);       // 失败则取消标记，卡片保持原样
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
      if (_deletingIds.has(t.task_id)) return;   // 正在删除，不重建
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
  // 在线观看：经后端 /api/stream/proxy 代理（带 Referer 绕开防盗链，原生 HLS 播放）
  el.watchBtn.addEventListener('click', () => openWatch({ url: el.watchBtn.dataset.url }));
  // 切换观看清晰度：更新播放用的 url / 是否 HLS，下次点「在线观看」即用所选清晰度
  el.watchQuality.addEventListener('change', () => {
    const o = el.watchQuality.selectedOptions && el.watchQuality.selectedOptions[0];
    if (!o) return;
    el.watchBtn.dataset.url = o.dataset.url || "";
    el.watchBtn.dataset.hls = o.dataset.hls || "false";
  });
  el.watchClose.addEventListener('click', closeWatch);
  el.watchBack.addEventListener('click', closeWatch);
  el.watchModal.addEventListener('click', (e) => { if (e.target === el.watchModal) closeWatch(); });
  // 注：「退出」/「返回桌面」按钮的初始化已迁至 web/js/desktop-app.js（桌面版专属脚本，
  // 仅 pywebview 环境加载）。web 端无原生窗口可退，相关逻辑不进入 web 加载集。
  function openWatch(opts = {}) {
    // 任务卡片已下载完成 → 直接播放本地文件；否则（解析面板 / 下载中）走源站实时流代理
    let src;
    let isLocal = false;
    if (opts.taskId && opts.completed) {
      const base = opts.base || window.VDL_API_BASE || "";
      src = base + "/api/tasks/" + encodeURIComponent(opts.taskId) + "/file";
      isLocal = true;
    } else if (opts.url) {
      const base = window.VDL_API_BASE || "";
      src = base + "/api/stream/proxy?u=" + encodeURIComponent(opts.url);
      const cookie = (el.cookieInput && el.cookieInput.value || "").trim();
      if (cookie) src += "&cookie=" + encodeURIComponent(cookie);
    } else {
      return;
    }
    el.watchTitle.textContent = (el.title && el.title.textContent) || "在线观看";
    el.watchStatus.textContent = isLocal ? "正在打开本地文件…" : "正在连接源站…";
    el.watchStatus.style.color = "#ffd479";
    el.watchModal.hidden = false;
    const v = el.watchVideo;
    v.onerror = v.onloadeddata = v.onplaying = null;
    v.src = src;
    v.play().catch(() => {});
    v.onloadeddata = v.onplaying = () => {
      el.watchStatus.textContent = isLocal
        ? "正在播放本地文件"
        : "正在播放（实时流，受单连接限速可能缓冲，建议下载后看）";
      el.watchStatus.style.color = "#9be29b";
    };
    v.onerror = () => {
      if (isLocal) {
        el.watchStatus.textContent = "本地文件播放失败，可改用「保存到本机」后用系统播放器打开";
      } else {
        el.watchStatus.textContent = "播放失败：源站拒绝或需登录 Cookie，请在「高级选项」粘贴浏览器 Cookie 后重试";
      }
      el.watchStatus.style.color = "#ff8a8a";
    };
  }
  function closeWatch() {
    const v = el.watchVideo;
    try { v.pause(); } catch (e) {}
    v.onerror = v.onloadeddata = v.onplaying = null;
    v.removeAttribute("src");
    try { v.load(); } catch (e) {}
    el.watchModal.hidden = true;
  }
  // 「复制操作指引」：一键复制 Cookie 获取步骤文本（便于照做或转发）
  el.cookieHelpCopy.addEventListener('click', async () => {
    const txt = (el.cookieHelp.innerText || '').replace(/\s+/g, ' ').trim();
    try {
      await navigator.clipboard.writeText(txt);
      el.cookieHelpCopy.textContent = '已复制 ✓';
    } catch (e) {
      el.cookieHelpCopy.textContent = '复制失败，请手动选择';
    }
    setTimeout(() => { el.cookieHelpCopy.textContent = '复制操作指引'; }, 1500);
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

  // 百度授权：用系统浏览器打开 OAuth 页（pywebview 不支持 window.open 弹窗）
  let _baiduAuthPoll = null;
  const openBaiduAuthInPage = async () => {
    // 必须每次请求带 state 的 URL（/api/cloud/baidu/auth_url 会生成新 state 写入
    // 服务端 _BAIDU_STATES；state=空串会让回调校验失败）。/api/version 里的
    // baidu_auth_url 字段不带 state（仅作启用标志），不能直接拿来跳转。
    let url = '';
    let errMsg = '';
    try {
      const r = await request('/api/cloud/baidu/auth_url', {}, '');
      if (r && r.auth_url) url = r.auth_url;
    } catch (e) {
      errMsg = (e && e.message) ? e.message : String(e);
    }
    if (!url) {
      // 常见原因：app 没重启（env 未加载新 config）/ config 字段缺失。后端 503 会经
      // request() 抛 {message: '该实例未配置百度网盘应用凭据'}，直接显示便于定位。
      const tip = errMsg || '请确认 config.json 已填好 4 个百度凭据且 app 已重启';
      el.cloudBaiduStatus.textContent = '获取百度授权链接失败：' + tip;
      if (el.baiduDriveStatus) el.baiduDriveStatus.textContent = '获取百度授权链接失败：' + tip;
      return;
    }
    // 委托桌面增强层用原生桥接在系统浏览器打开授权页；无桥接（含纯 web 端）
    // 时回退 window.open。window.VDL.desktop 仅在 desktop-app.js 加载后存在。
    let opened = false;
    const viaDesktop = window.VDL && window.VDL.desktop && window.VDL.desktop.openExternal(url);
    if (viaDesktop) {
      opened = true;
    } else {
      // web 端或原生桥接不可用：回退浏览器打开
      try {
        const w = window.open(url, '_blank');
        opened = !!w;
      } catch (e2) { opened = false; }
    }
    if (!opened) {
      el.cloudBaiduStatus.textContent = '无法打开授权页，请用浏览器访问：' + url;
      if (el.baiduDriveStatus) el.baiduDriveStatus.textContent = '无法打开授权页';
      return;
    }
    el.cloudBaiduStatus.textContent = '已打开系统浏览器，请完成百度账号登录…';
    if (el.baiduDriveStatus) el.baiduDriveStatus.textContent = '已打开系统浏览器，请完成百度账号登录…';
    // 轮询后端检测授权完成（百度回调写入 token 文件后本端点返回 logged_in）
    if (_baiduAuthPoll) clearInterval(_baiduAuthPoll);
    _baiduAuthPoll = setInterval(async () => {
      try {
        const r = await request('/api/cloud/baidu/token', {}, '');
        if (r && r.logged_in && r.access_token) {
          clearInterval(_baiduAuthPoll); _baiduAuthPoll = null;
          baiduToken = r.access_token;
          localStorage.setItem('vdl_baidu_token', r.access_token);
          el.cloudBaiduStatus.textContent = '已授权 ✓';
          if (el.baiduDriveStatus) {
            el.baiduDriveStatus.textContent = '已授权 ✓';
            el.baiduDriveStatus.className = 'baidu-status is-ok';
          }
          if (baiduModalOpen) loadBaiduList(currentBaiduPath);
        }
      } catch (e) { /* 轮询出错忽略 */ }
    }, 1500);
  };

  // 云盘弹窗事件绑定
  el.cloudModalClose.addEventListener('click', () => el.cloudModal.close());
  el.cloudModal.addEventListener('click', (e) => { if (e.target === el.cloudModal) el.cloudModal.close(); });
  el.cloudSave.addEventListener('click', startCloudSave);
  el.cloudModal.querySelectorAll('input[name=cloudProvider]').forEach((r) => r.addEventListener('change', syncCloudForm));
  el.cloudBaiduBtn.addEventListener('click', () => {
    if (!node.baiduAuthUrl) { el.cloudBaiduStatus.textContent = '该实例未启用百度网盘'; return; }
    openBaiduAuthInPage();
  });
  window.addEventListener('message', (e) => {
    if (e.origin !== location.origin) return;
    const d = e.data || {};
    if (d.source !== 'vdl-baidu') return;
    if (d.token) {
      baiduToken = d.token;
      localStorage.setItem('vdl_baidu_token', d.token);
      el.cloudBaiduStatus.textContent = '已授权 ✓';
      // 同步百度网盘浏览面板状态
      if (el.baiduDriveStatus) {
        el.baiduDriveStatus.textContent = '已授权 ✓';
        el.baiduDriveStatus.className = 'baidu-status is-ok';
      }
      // 关闭授权弹窗（回调页 1.5s 后也会自关闭）
      if (window._baiduAuthWin && !window._baiduAuthWin.closed) {
        try { window._baiduAuthWin.close(); } catch(e) {}
      }
      if (baiduModalOpen) loadBaiduList(currentBaiduPath);
    } else if (d.error) {
      el.cloudBaiduStatus.textContent = '授权失败：' + d.error;
      if (el.baiduDriveStatus) {
        el.baiduDriveStatus.textContent = '授权失败：' + d.error;
        el.baiduDriveStatus.className = 'baidu-status is-err';
      }
    }
  });

  // ── 百度网盘浏览/下载面板 ──
  let baiduModalOpen = false;
  let currentBaiduPath = '/';
  const baiduDlPollers = {};  // tid -> interval

  el.tabBaidu.addEventListener('click', () => {
    if (!node.baiduAvailable) { el.baiduDriveHint.textContent = '该实例未配置百度网盘凭据'; return; }
    baiduModalOpen = true;
    restoreBaiduToken();
    if (baiduToken) {
      el.baiduDriveStatus.textContent = '已授权 ✓';
      el.baiduDriveStatus.className = 'baidu-status is-ok';
    } else {
      el.baiduDriveStatus.textContent = '未授权';
      el.baiduDriveStatus.className = 'baidu-status';
      el.baiduList.innerHTML = '<p class="baidu-empty">请先点「授权百度网盘」。</p>';
    }
    el.baiduModal.showModal();
    if (baiduToken) loadBaiduList(currentBaiduPath);
  });
  el.baiduModalClose.addEventListener('click', () => el.baiduModal.close());
  el.baiduModal.addEventListener('click', (e) => { if (e.target === el.baiduModal) el.baiduModal.close(); });
  el.baiduModal.addEventListener('close', () => { baiduModalOpen = false; });

  // ── 百度网盘下载（baiduPCS-Go 适配器，独立于 OAuth 版百度面板）──
  const pcsModal = document.getElementById('pcsModal');
  const pcsStatusEl = document.getElementById('pcsStatus');
  const pcsCookiesEl = document.getElementById('pcsCookies');
  const pcsLoginBtn = document.getElementById('pcsLoginBtn');
  const pcsWhoEl = document.getElementById('pcsWho');
  const pcsShareUrlEl = document.getElementById('pcsShareUrl');
  const pcsSharePwdEl = document.getElementById('pcsSharePwd');
  const pcsTransferBtn = document.getElementById('pcsTransferBtn');
  const pcsLsBtn = document.getElementById('pcsLsBtn');
  const pcsShareStatusEl = document.getElementById('pcsShareStatus');
  const pcsListEl = document.getElementById('pcsList');
  const pcsDlListEl = document.getElementById('pcsDlList');
  const pcsModalClose = document.getElementById('pcsModalClose');
  const pcsQrImgEl = document.getElementById('pcsQrImg');
  const pcsQrStatusEl = document.getElementById('pcsQrStatus');
  const pcsQrRefreshEl = document.getElementById('pcsQrRefresh');
  let _pcsQrTimer = null;
  let _pcsQrSign = null;
  let _pcsQrActive = false;
  const _pcsPollers = {};

  const pcsFetch = async (url, body) => {
    const opt = { method: body ? 'POST' : 'GET', headers: { 'Content-Type': 'application/json' } };
    if (body) opt.body = JSON.stringify(body);
    try {
      const r = await fetch(url, opt);
      const text = await r.text();
      // 安全解析 JSON：响应可能不是 JSON（如服务器错误返回 HTML）
      try { return JSON.parse(text); }
      catch (_) {
        console.warn('[pcs] 非 JSON 响应', r.status, text.slice(0, 200));
        return { ok: false, message: '服务器响应异常（HTTP ' + r.status + '）', _raw_slice: text.slice(0, 300) };
      }
    } catch (e) {
      console.error('[pcs] fetch 失败', url, e);
      throw e; // 向上抛给调用方 catch
    }
  };

  async function pcsRefreshStatus() {
    try {
      const s = await pcsFetch('/api/pcs/status');
      let html = '';
      if (!s.binary_installed) {
        html = '⚙️ baiduPCS-Go 尚未安装，即将自动下载…';
        // 自动触发安装
        setTimeout(() => pcsEnsure(), 500);
      } else if (s.logged_in) {
        html = '✅ 工具已就绪 ｜ 已登录：' + (s.who || '');
      } else {
        html = '✅ 工具已就绪 ｜ ⚠️ 尚未登录（请先完成第①步）';
      }
      pcsStatusEl.innerHTML = html;
      pcsStatusEl.className = 'pcs-status' + (s.logged_in ? ' is-ok' : '');
      if (s.logged_in) { pcsWhoEl.textContent = '已登录 ✓'; pcsWhoEl.style.color = '#07c160'; }
      return s;
    } catch (e) {
      // 网络或解析错误时，静默提示并尝试安装（首次使用最常见的原因是二进制不存在）
      pcsStatusEl.textContent = '正在初始化 baiduPCS-Go…';
      pcsStatusEl.className = 'pcs-status';
      setTimeout(() => pcsEnsure(), 800);
      return null;
    }
  }

  // 确保二进制已安装；未安装则先下载（带进度）
  async function pcsEnsure() {
    try { var s = await pcsFetch('/api/pcs/status'); } catch(e) { s = {}; }
    if (s.binary_installed) return true;
    pcsStatusEl.textContent = '正在下载 baiduPCS-Go（首次约 30MB，请稍候）…';
    pcsStatusEl.className = 'pcs-status';
    try {
      const r = await pcsFetch('/api/pcs/install', {});
      await pcsRefreshStatus();
      if (!r.ok) {
        pcsStatusEl.textContent = '安装失败：' + (r.message || '未知');
        pcsStatusEl.className = 'pcs-status is-err';
        return false;
      }
      return true;
    } catch(e2) {
      pcsStatusEl.textContent = '安装请求失败：' + e2.message;
      pcsStatusEl.className = 'pcs-status is-err';
      return false;
    }
  }

  const pcsOpenWebBtn = document.getElementById('pcsOpenWebBtn');
  if (pcsOpenWebBtn) {
    pcsOpenWebBtn.addEventListener('click', () => {
      const url = 'https://pan.baidu.com/';
      // 委托桌面增强层原生打开；无桥接（含 web 端）回退浏览器
      const opened = window.VDL && window.VDL.desktop && window.VDL.desktop.openExternal(url);
      if (!opened) window.open(url, '_blank');
    });
  }

  // ── 扫码登录（二维码）──
  function pcsStopQr() {
    _pcsQrActive = false;
    if (_pcsQrTimer) { clearTimeout(_pcsQrTimer); _pcsQrTimer = null; }
  }

  async function pcsStartQr() {
    pcsStopQr();
    _pcsQrSign = null;
    _pcsQrActive = true;
    if (pcsQrStatusEl) { pcsQrStatusEl.textContent = '正在生成二维码…'; pcsQrStatusEl.className = 'pcs-qr-status'; }
    if (pcsQrImgEl) pcsQrImgEl.classList.add('is-hidden');
    try {
      const r = await pcsFetch('/api/pcs/qr/gen');
      if (!r.ok) {
        if (pcsQrStatusEl) { pcsQrStatusEl.textContent = '生成失败：' + (r.message || r.error || '未知'); pcsQrStatusEl.className = 'pcs-qr-status is-err'; }
        return;
      }
      _pcsQrSign = r.sign;
      if (pcsQrImgEl) { pcsQrImgEl.src = r.img; pcsQrImgEl.classList.remove('is-hidden'); }
      if (pcsQrStatusEl) { pcsQrStatusEl.textContent = '请用手机百度网盘 App 扫码'; pcsQrStatusEl.className = 'pcs-qr-status'; }
      // 顺序轮询：等上一次返回后再排下一次（避免长轮询请求重叠成风暴）
      pcsPollQr();
    } catch (e) {
      if (pcsQrStatusEl) { pcsQrStatusEl.textContent = '生成二维码出错：' + e.message; pcsQrStatusEl.className = 'pcs-qr-status is-err'; }
    }
  }

  async function pcsPollQr() {
    if (!_pcsQrActive || !_pcsQrSign) return;
    try {
      const r = await pcsFetch('/api/pcs/qr/poll?sign=' + encodeURIComponent(_pcsQrSign));
      const st = r.status;
      // 诊断：打印每次轮询结果到控制台（排查"卡在等待扫码"问题）
      console.log('[pcs] poll result:', JSON.stringify(r).slice(0, 300));
      if (st === 'waiting') {
        if (pcsQrStatusEl) { pcsQrStatusEl.textContent = '等待扫码…'; pcsQrStatusEl.className = 'pcs-qr-status'; }
      } else if (st === 'scanned') {
        if (pcsQrStatusEl) { pcsQrStatusEl.textContent = '已扫码，请在手机上确认'; pcsQrStatusEl.className = 'pcs-qr-status'; }
      } else if (st === 'expired') {
        pcsStopQr();
        if (pcsQrStatusEl) { pcsQrStatusEl.textContent = '二维码已过期，请点「刷新二维码」'; pcsQrStatusEl.className = 'pcs-qr-status is-err'; }
      } else if (st === 'confirmed') {
        pcsStopQr();
        const login = r.login || {};
        if (login.ok) {
          if (pcsQrStatusEl) { pcsQrStatusEl.textContent = '✓ 登录成功'; pcsQrStatusEl.className = 'pcs-qr-status is-ok'; }
          if (pcsWhoEl) { pcsWhoEl.textContent = '已登录 ✓'; pcsWhoEl.style.color = '#07c160'; }
          await pcsRefreshStatus();
        } else {
          if (pcsQrStatusEl) { pcsQrStatusEl.textContent = '✗ ' + (login.message || '登录失败'); pcsQrStatusEl.className = 'pcs-qr-status is-err'; pcsQrStatusEl.title = login.raw || ''; }
        }
      } else if (st === 'error') {
        // 后端明确报错（如超时、百度接口异常）→ 显示错误但继续轮询（不停止）
        if (pcsQrStatusEl) { pcsQrStatusEl.textContent = '⚠ ' + (r.message || '轮询异常'); pcsQrStatusEl.className = 'pcs-qr-status is-err'; }
      } else {
        // 兜底：status 缺失或未知值（后端返回了非预期格式、HTTP 错误、JSON 解析失败等）
        console.warn('[pcs] poll 返回未知状态', r);
        if (pcsQrStatusEl) {
          pcsQrStatusEl.textContent = '⚠ 轮询异常（' + (r.message || r._raw_slice ? (r.message || '').slice(0, 60) : '无响应') + '）';
          pcsQrStatusEl.className = 'pcs-qr-status is-err';
        }
      }
    } catch (e) {
      // 网络层完全失败（fetch 抛出异常）
      console.error('[pcs] poll fetch 异常', e);
      if (pcsQrStatusEl) pcsQrStatusEl.textContent = '⚠ 连接断开，重试中…';
    } finally {
      // 顺序轮询：上一轮结束（无论成功/失败/超时）后，间隔 1s 再发起下一轮。
      // 关键修复：后端是 60s 长轮询，若用 setInterval 会叠加成请求风暴。
      if (_pcsQrActive) {
        _pcsQrTimer = setTimeout(pcsPollQr, 1000);
      }
    }
  }

  if (pcsQrRefreshEl) pcsQrRefreshEl.addEventListener('click', pcsStartQr);

  // 新增：账号密码登录元素
  const pcsUsernameEl = document.getElementById('pcsUsername');
  const pcsPasswordEl = document.getElementById('pcsPassword');
  const pcsCookieLoginBtn = document.getElementById('pcsCookieLoginBtn');

  // 🔑 主登录按钮（账号密码）
  pcsLoginBtn.addEventListener('click', async () => {
    const username = (pcsUsernameEl && pcsUsernameEl.value) || '';
    const password = (pcsPasswordEl && pcsPasswordEl.value) || '';
    if (!username || !password) { pcsWhoEl.textContent = '请输入百度账号和密码'; pcsWhoEl.style.color = '#e64340'; return; }

    if (!(await pcsEnsure())) return;
    pcsLoginBtn.disabled = true;
    pcsLoginBtn.textContent = '登录中…';
    try {
      const r = await pcsFetch('/api/pcs/login-password', { username, password });
      if (r.ok) {
        pcsWhoEl.textContent = '✓ ' + (r.message || '登录成功');
        pcsWhoEl.style.color = '#07c160';
        pcsWhoEl.title = '';
      } else {
        pcsWhoEl.textContent = '✗ ' + (r.message || '失败');
        pcsWhoEl.style.color = '#e64340';
        pcsWhoEl.title = r.raw || '';
      }
      await pcsRefreshStatus();
    } catch (e) {
      console.error('[pcs login] 异常:', e);
      pcsWhoEl.textContent = '请求异常：' + (e.message || e);
      pcsWhoEl.style.color = '#e64340';
    } finally {
      pcsLoginBtn.disabled = false; pcsLoginBtn.textContent = '🔑 登录';
    }
  });

  // Cookie 登录（高级备选）
  if (pcsCookieLoginBtn) {
    pcsCookieLoginBtn.addEventListener('click', async () => {
      const raw = (pcsCookiesEl && pcsCookiesEl.value.trim()) || '';
      if (!raw) { pcsWhoEl.textContent = '请先粘贴 Cookie / BDUSS'; pcsWhoEl.style.color = '#e64340'; return; }
      if (!(await pcsEnsure())) return;
      pcsCookieLoginBtn.disabled = true;
      pcsCookieLoginBtn.textContent = '登录中…';
      try {
        const r = await pcsFetch('/api/pcs/login', { cookies: raw });
        if (r.ok) {
          pcsWhoEl.textContent = '✓ ' + (r.message || '登录成功');
          pcsWhoEl.style.color = '#07c160';
        } else {
          pcsWhoEl.textContent = '✗ ' + (r.message || '失败');
          pcsWhoEl.style.color = '#e64340';
          pcsWhoEl.title = r.raw || '';
        }
        await pcsRefreshStatus();
      } catch (e) {
        pcsWhoEl.textContent = '异常：' + e.message; pcsWhoEl.style.color = '#e64340';
      } finally {
        pcsCookieLoginBtn.disabled = false; pcsCookieLoginBtn.textContent = '用 Cookie 登录';
      }
    });
  }

  // 回车键触发登录
  [pcsUsernameEl, pcsPasswordEl].forEach(el => {
    if (el) el.addEventListener('keydown', e => { if (e.key === 'Enter') pcsLoginBtn.click(); });
  });

  async function pcsRenderList() {
    const r = await pcsFetch('/api/pcs/ls', { path: '/' });
    if (r.ok && r.items && r.items.length) {
      pcsListEl.innerHTML = r.items.map((it) => {
        const sz = it.size ? `（${(it.size / 1048576).toFixed(1)} MB）` : (it.is_dir ? '（目录）' : '');
        const path = '/' + it.name;
        return `<div class="baidu-list-item"><span class="bi-name">${it.name}${sz}</span>` +
          `<button type="button" class="btn btn-sm pcs-dl-btn" data-path="${encodeURIComponent(path)}" data-name="${encodeURIComponent(it.name)}">下载</button></div>`;
      }).join('');
    } else if (r.raw) {
      pcsListEl.innerHTML = `<pre class="pcs-raw">${pcsEscapeHtml(r.raw)}</pre>`;
    } else {
      pcsListEl.innerHTML = '<p class="baidu-empty">列出为空或失败。</p>';
    }
  }

  pcsTransferBtn.addEventListener('click', async () => {
    const url = pcsShareUrlEl.value.trim();
    const pwd = pcsSharePwdEl.value.trim();
    if (!url) { pcsShareStatusEl.textContent = '请先粘贴分享链接'; pcsShareStatusEl.className = 'pcs-status is-err'; return; }
    if (!(await pcsEnsure())) return;
    pcsTransferBtn.disabled = true; pcsTransferBtn.textContent = '转存中…';
    pcsShareStatusEl.textContent = '正在转存到你的网盘…'; pcsShareStatusEl.className = 'pcs-status';
    try {
      const r = await pcsFetch('/api/pcs/share/transfer', { url, pwd });
      if (r.ok) {
        pcsShareStatusEl.textContent = '✓ 转存成功，正在列出文件…'; pcsShareStatusEl.className = 'pcs-status is-ok';
        await pcsRenderList();
      } else {
        pcsShareStatusEl.textContent = '✗ 转存失败：' + (r.message || '未知'); pcsShareStatusEl.className = 'pcs-status is-err';
        if (r.raw) console.log('[pcs transfer]', r.raw);
      }
    } catch (e) {
      pcsShareStatusEl.textContent = '出错：' + e.message; pcsShareStatusEl.className = 'pcs-status is-err';
    } finally {
      pcsTransferBtn.disabled = false; pcsTransferBtn.textContent = '转存';
    }
  });

  pcsLsBtn.addEventListener('click', () => pcsRenderList());

  const pcsManualPathEl = document.getElementById('pcsManualPath');
  const pcsManualDlBtn = document.getElementById('pcsManualDlBtn');
  pcsManualDlBtn.addEventListener('click', () => {
    const path = (pcsManualPathEl.value || '').trim();
    if (!path) { pcsShareStatusEl.textContent = '请填写网盘路径'; pcsShareStatusEl.className = 'pcs-status is-err'; return; }
    const name = path.split('/').pop() || 'pcs_file';
    startPcsDownload(path, name, pcsManualDlBtn);
  });

  pcsListEl.addEventListener('click', (e) => {
    const btn = e.target.closest('.pcs-dl-btn');
    if (!btn) return;
    const path = decodeURIComponent(btn.dataset.path);
    const name = decodeURIComponent(btn.dataset.name);
    startPcsDownload(path, name, btn);
  });

  function startPcsDownload(path, name, btn) {
    pcsFetch('/api/pcs/download', { path, name }).then((r) => {
      if (!r.ok || !r.task_id) {
        pcsShareStatusEl.textContent = '提交下载失败：' + (r.detail || '未知'); pcsShareStatusEl.className = 'pcs-status is-err';
        return;
      }
      const tid = r.task_id;
      addPcsDlItem(tid, name);
      pollPcsTask(tid);
    });
  }

  function addPcsDlItem(tid, name) {
    const empty = pcsDlListEl.querySelector('.baidu-empty');
    if (empty) empty.remove();
    const div = document.createElement('div');
    div.className = 'baidu-dl-item';
    div.id = 'pcs-dl-' + tid;
    div.innerHTML = `<span class="di-name">${name}</span><span class="di-progress">排队中…</span>`;
    pcsDlListEl.appendChild(div);
  }

  function pollPcsTask(tid) {
    if (_pcsPollers[tid]) clearInterval(_pcsPollers[tid]);
    _pcsPollers[tid] = setInterval(async () => {
      try {
        const t = await pcsFetch('/api/pcs/task/' + tid);
        const div = document.getElementById('pcs-dl-' + tid);
        if (!div) return;
        const p = t.progress || {};
        let txt = '';
        if (t.status === 'downloading') txt = (p.percent ? p.percent.toFixed(1) + '% ' : '') + (p.line ? p.line.slice(0, 80) : '下载中…');
        else if (t.status === 'done') txt = '✓ ' + (t.message || '完成');
        else if (t.status === 'failed') txt = '✗ ' + (t.message || t.last || '失败');
        else txt = t.status || '处理中…';
        div.querySelector('.di-progress').textContent = txt;
        if (t.status === 'done' || t.status === 'failed') {
          clearInterval(_pcsPollers[tid]);
          div.querySelector('.di-progress').style.color = t.status === 'done' ? '#07c160' : '#e64340';
        }
      } catch (e) { /* ignore */ }
    }, 1000);
  }

  function pcsEscapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  document.getElementById('tabPcs').addEventListener('click', () => {
    if (typeof pcsModal.showModal === 'function') pcsModal.showModal();
    else pcsModal.setAttribute('open', '');
    pcsRefreshStatus();
    pcsStartQr();
    // 显示构建版本信息（防止跑错旧版）
    pcsFetch('/api/pcs/build-info').then(bi => {
      const el = document.getElementById('pcsBuildInfo');
      if (el) el.textContent = `· ${bi.hash || '?'} · ${bi.time || ''}`;
    }).catch((e) => {
      const el = document.getElementById('pcsBuildInfo');
      if (el) el.textContent = '· (build-info 不可用: ' + String(e).slice(0, 40) + ')';
      console.error('[pcs] build-info fetch failed:', e);
    });
  });
  pcsModalClose.addEventListener('click', () => { pcsStopQr(); pcsModal.close(); });
  pcsModal.addEventListener('click', (e) => { if (e.target === pcsModal) pcsModal.close(); });
  el.baiduDriveAuthBtn.addEventListener('click', () => {
    if (!node.baiduAuthUrl) { el.baiduDriveStatus.textContent = '该实例未启用百度网盘'; return; }
    openBaiduAuthInPage();
  });

  // ── 百度网盘「分享链接下载」（登录后转存到自己网盘再下）──
  // 智能解析用户粘贴的百度分享文本（"通过网盘分享的文件：xxx 链接：URL 提取码：abcd"）
  // → 自动分离出 URL 和提取码填回对应输入框，避免手动复制两端。
  const parseBaiduShareText = (text) => {
    const out = { url: '', pwd: '' };
    if (!text) return out;
    // URL：复用现有 extractUrls（支持多行 + 分行参数合并）
    const urls = extractUrls(text);
    if (urls.length) out.url = urls[0].replace(/[，。、；！？]+$/, '');  // 去末尾中文标点
    // 提取码：兼容「提取码：abcd」「密码：abcd」「Code：abcd」「code：abcd」「pwd：abcd」
    const m = text.match(/(?:提取码|密码|code|pwd|Code|Pwd)\s*[:：=]?\s*([A-Za-z0-9]{4,8})/i);
    if (m) out.pwd = m[1];
    return out;
  };
  // 输入框实时解析：粘贴完文本后自动把 URL/提取码填回正确位置
  el.baiduShareUrl.addEventListener('input', () => {
    const parsed = parseBaiduShareText(el.baiduShareUrl.value);
    if (parsed.url && parsed.url !== el.baiduShareUrl.value.trim()) {
      el.baiduShareUrl.value = parsed.url;
    }
    if (parsed.pwd && !el.baiduSharePwd.value.trim()) {
      el.baiduSharePwd.value = parsed.pwd;
    }
  });

  async function restoreBaiduToken() {
    // localStorage 优先；为空时回退本机服务端持久化的令牌（重启后免重复授权）
    if (baiduToken) return;
    try {
      const r = await fetch('/api/cloud/baidu/token');
      const d = await r.json();
      if (d && d.logged_in && d.access_token) {
        baiduToken = d.access_token;
        localStorage.setItem('vdl_baidu_token', d.access_token);
        if (el.baiduDriveStatus) {
          el.baiduDriveStatus.textContent = '已授权 ✓';
          el.baiduDriveStatus.className = 'baidu-status is-ok';
        }
        if (el.cloudBaiduStatus) el.cloudBaiduStatus.textContent = '已授权 ✓';
      }
    } catch { /* 忽略：离线或后端未启用 */ }
  }

  // 分享当前上下文（用于文件夹展开）
  let _shareCtx = { url: '', pwd: '', dir: '' };

  // 递归渲染分享列表（支持面包屑导航 + 点文件夹展开）
  async function renderShareList(url, pwd, subDir, pwdSynced) {
    _shareCtx = { url, pwd, dir: subDir || '' };
    el.baiduShareStatus.textContent = subDir ? `加载子目录：${subDir}…` : '加载中…';
    el.baiduShareStatus.className = 'baidu-share-status';
    el.baiduShareList.innerHTML = '<p class="baidu-empty">加载中…</p>';
    // 同步回输入框让用户看到解析结果
    if (!pwdSynced && url && url !== el.baiduShareUrl.value.trim()) el.baiduShareUrl.value = url;
    if (!pwdSynced && pwd && !el.baiduSharePwd.value.trim()) el.baiduSharePwd.value = pwd;
    try {
      const r = await fetch('/api/cloud/baidu/share/list', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url, pwd, dir: subDir || '' }),
      });
      const data = await r.json();
      if (!r.ok) {
        el.baiduShareStatus.textContent = '列出失败：' + (data.detail || r.status);
        el.baiduShareStatus.className = 'baidu-share-status is-err';
        el.baiduShareList.innerHTML = `<p class="baidu-empty">${data.detail || r.status}</p>`;
        return;
      }
      const list = data.list || [];
      // 面包屑
      let crumbs = '';
      if (subDir) {
        const parts = subDir.split('/').filter(Boolean);
        let acc = '';
        crumbs = '<a href="#" data-nav="root">根目录</a>';
        parts.forEach((seg, i) => {
          acc += '/' + seg;
          const isLast = i === parts.length - 1;
          crumbs += ' / ' + (isLast
            ? `<span>${seg}</span>`
            : `<a href="#" data-nav="${encodeURIComponent(acc)}">${seg}</a>`);
        });
      }
      if (!list.length) {
        el.baiduShareStatus.textContent = subDir ? `${subDir} 为空` : '该分享为空或链接已失效';
        el.baiduShareStatus.className = 'baidu-share-status';
        el.baiduShareList.innerHTML = (crumbs ? `<div class="baidu-crumbs">${crumbs}</div>` : '')
          + '<p class="baidu-empty">此目录下没有文件。</p>';
        return;
      }
      el.baiduShareStatus.textContent = `共 ${list.length} 项`;
      el.baiduShareStatus.className = 'baidu-share-status';
      el.baiduShareList.innerHTML = (crumbs ? `<div class="baidu-crumbs">${crumbs}</div>` : '')
        + list.map((it) => {
            const size = it.isdir ? '文件夹' : _fmtSize(it.size);
            const icon = it.isdir ? '📁' : '📄';
            const label = it.name || it.path || '(未命名)';
            const nameAction = it.isdir ? `data-open="${encodeURIComponent(it.path)}"` : '';
            const btn = it.isdir
              ? `<button type="button" class="btn btn-accent btn-sm open" ${nameAction}>展开</button>`
              : `<button type="button" class="btn btn-accent btn-sm dl" data-path="${encodeURIComponent(it.path)}" data-name="${encodeURIComponent(label)}">转存并下载</button>`;
            return `<div class="baidu-row" data-path="${encodeURIComponent(it.path)}" data-name="${encodeURIComponent(label)}" data-fsid="${it.fs_id || ''}">
              <span class="name" ${nameAction}>${icon} ${label}</span>
              <span class="size">${size}</span>
              <span class="dl">${btn}</span>
            </div>`;
          }).join('');
      // 文件夹点击/展开按钮
      el.baiduShareList.querySelectorAll('.open, .name[data-open]').forEach((el_) => {
        el_.addEventListener('click', (e) => {
          e.preventDefault();
          const p = decodeURIComponent(el_.dataset.open);
          renderShareList(url, pwd, p, true);
        });
      });
      // 面包屑导航
      el.baiduShareList.querySelectorAll('.baidu-crumbs a').forEach((a) => {
        a.addEventListener('click', (e) => {
          e.preventDefault();
          const target = decodeURIComponent(a.dataset.nav);
          renderShareList(url, pwd, target === 'root' ? '' : target, true);
        });
      });
      // 转存下载按钮
      // 保存 list 级别的 verify 结果（sekey/share_id/uk），下载时传入后端跳过重复 verify
      const _listSekey = data.sekey || '';
      const _listShareId = data.share_id != null ? data.share_id : null;
      const _listUk = data.uk != null ? data.uk : null;
      el.baiduShareList.querySelectorAll('.dl button').forEach((b) => {
        const itemFsId = b.closest('.baidu-row')?.dataset?.fsid || '';
        b.addEventListener('click', () => startBaiduShareDownload({
          path: decodeURIComponent(b.dataset.path),
          name: decodeURIComponent(b.dataset.name),
          url,
          pwd,
          _sekey: _listSekey,
          _share_id: _listShareId,
          _uk: _listUk,
          fs_id: itemFsId ? Number(itemFsId) : null,
        }));
      });
    } catch (err) {
      el.baiduShareStatus.textContent = '列出出错：' + err.message;
      el.baiduShareStatus.className = 'baidu-share-status is-err';
      el.baiduShareList.innerHTML = `<p class="baidu-empty">出错：${err.message}</p>`;
    }
  }

  el.baiduShareListBtn.addEventListener('click', () => {
    restoreBaiduToken();
    const parsed = parseBaiduShareText(el.baiduShareUrl.value || '');
    const url = parsed.url || (el.baiduShareUrl.value || '').trim();
    const pwd = parsed.pwd || (el.baiduSharePwd.value || '').trim();
    if (!url) { el.baiduShareStatus.textContent = '请先粘贴分享链接'; el.baiduShareStatus.className = 'baidu-share-status is-err'; return; }
    renderShareList(url, pwd, '', false);
  });

  // ── 百度网盘登录（唯一入口：app 内 WebView 真实登录）──
  // 已移除扫码登录入口：扫码的 BDUSS 与下载分享必需的 WebView 登录态是两回事，
  // 保留会让用户混淆（之前用户已踩坑）。现在只需在 app 内 WebView 登录一次，
  // cookie 持久化在 WKWebsiteDataStore，重启不丢，自动用于后续下载。
  async function refreshBaiduLoginStatus() {
    try {
      const r = await fetch('/api/cloud/baidu/qr/status');
      const d = await r.json();
      // 仅显示用户名（如果 OAuth 授权过），不再误显示为「app 内登录」状态
      if (d.logged_in && d.username) {
        el.baiduLoginStatus.textContent = '（账号：' + d.username + '）';
        el.baiduLoginStatus.style.color = '#888';
      } else {
        el.baiduLoginStatus.textContent = '';
      }
    } catch (e) { /* ignore */ }
  }

  // app 内 WebView 真实登录（零扩展依赖，登录态持久化）
  const baiduAppLoginBtn = document.getElementById('baiduAppLoginBtn');
  if (baiduAppLoginBtn) baiduAppLoginBtn.addEventListener('click', async () => {
    if (!(window.pywebview && window.pywebview.api && window.pywebview.api.baidu_login)) {
      alert('此功能仅在桌面版 VideoDownloader.app 内可用'); return;
    }
    baiduAppLoginBtn.disabled = true;
    const _origText = baiduAppLoginBtn.textContent;
    baiduAppLoginBtn.textContent = '正在打开登录窗口…';
    try {
      const r = await window.pywebview.api.baidu_login();
      // pywebview 6.x 桥接可能返回字符串（未自动 JSON.parse）—— 兼容处理
      let info = r;
      if (typeof r === 'string') {
        try { info = JSON.parse(r); } catch { info = null; }
      }
      const ok = !!(info && (info.ok || info.logged));
      el.baiduLoginStatus.textContent = ok ? '✓ 登录成功（已自动用于下载）' : '⚠ 登录未完成，请重试';
      el.baiduLoginStatus.style.color = ok ? '#07c160' : '#e64340';
    } catch (e) {
      el.baiduLoginStatus.textContent = '⚠ 登录出错：' + e.message;
      el.baiduLoginStatus.style.color = '#e64340';
    } finally {
      baiduAppLoginBtn.disabled = false;
      baiduAppLoginBtn.textContent = _origText;
    }
  });
  refreshBaiduLoginStatus();

  async function startBaiduShareDownload(item) {
    if (!baiduToken) { el.baiduShareStatus.textContent = '请先点「授权百度网盘」完成授权'; el.baiduShareStatus.className = 'baidu-share-status is-err'; return; }

    // ★ 策略 0：通过 WebView 注入 JS 预取 dlink（最可靠，等同油猴原理）
    const _doDownload = (prefetchedDlink) => {
      el.baiduShareStatus.textContent = prefetchedDlink ? '已获取直链，正在下载…' : '已提交，正在转存…';
      el.baiduShareStatus.className = 'baidu-share-status';
      fetch('/api/cloud/baidu/share/download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          url: item.url, pwd: item.pwd, path: item.path, name: item.name, token: baiduToken,
          sekey: item._sekey || '',
          share_id: item._share_id != null ? item._share_id : null,
          uk: item._uk != null ? item._uk : null,
          fs_id: item.fs_id != null ? item.fs_id : null,
          bduss: '',  // 高级 BDUSS 输入已删除，留字段兼容后端
          dlink: prefetchedDlink || '',  // ★ WebView 预取的直链（后端策略0）
        }),
      }).then((r) => r.json()).then((data) => {
        if (data.task_id) {
          addBaiduDlItem(data.task_id, item.name);
          pollBaiduTask(data.task_id);
          el.baiduShareStatus.textContent = '已加入下载队列：' + item.name;
          el.baiduShareStatus.className = 'baidu-share-status';
        } else if (data.detail) {
          el.baiduShareStatus.textContent = '下载失败：' + data.detail;
          el.baiduShareStatus.className = 'baidu-share-status is-err';
        }
      }).catch((err) => {
        el.baiduShareStatus.textContent = '下载出错：' + err.message;
        el.baiduShareStatus.className = 'baidu-share-status is-err';
      });
    };

    // 尝试通过 app 内 WebView 获取直链（仅桌面版有 pywebview.api）
    if (window.pywebview && window.pywebview.api && window.pywebview.api.get_baidu_dlink && item.fs_id) {
      el.baiduShareStatus.textContent = '正在通过 app 内浏览器获取下载直链…';
      try {
        const callGetDlink = async () => {
          const result = await window.pywebview.api.get_baidu_dlink(item.url, item.fs_id, item.pwd || '');
          // pywebview 6.x 桥接已自动 JSON.parse，result 可能是对象或字符串，兼容两种
          if (typeof result === 'object' && result !== null) return result;
          try { return JSON.parse(result); } catch { return { ok: false, error: '解析失败' }; }
        };
        let info = await callGetDlink();
        if (info && info.ok && info.dlink) {
          return _doDownload(info.dlink);  // ★ 拿到直链 → 策略0
        }
        // 未登录 / 登录态失效 / 无登录cookie → 静默打开 app 内登录窗口，登录成功后自动重试一次。
        // 不显示红色错误：用户体验上等价于「下载需要先登录一次」，自动弹出窗口就好。
        if (info && (info.error === 'NOT_LOGGED_IN' || info.error === 'NO_LOGIN_COOKIE')) {
          el.baiduShareStatus.textContent = info.error === 'NO_LOGIN_COOKIE'
            ? '检测到未登录百度网盘，请在弹出的窗口完成登录…'
            : '首次下载需登录百度网盘，请在弹出的窗口完成登录…';
          el.baiduShareStatus.className = 'baidu-share-status';
          let loginRes = null;
          try { loginRes = await window.pywebview.api.baidu_login(); } catch (le) { loginRes = null; }
          // 兼容桥接可能返回字符串：typeof + JSON.parse fallback
          let loginInfo = loginRes;
          if (typeof loginRes === 'string') {
            try { loginInfo = JSON.parse(loginRes); } catch { loginInfo = null; }
          }
          if (loginInfo && (loginInfo.ok || loginInfo.logged)) {
            el.baiduShareStatus.textContent = '登录成功，正在获取直链…';
            el.baiduShareStatus.className = 'baidu-share-status';
            const info2 = await callGetDlink();
            if (info2 && info2.ok && info2.dlink) {
              return _doDownload(info2.dlink);
            }
            info = info2 || info;
          }
        }
        // 其它错误：提示，不再降级浏览器
        const msg = (info && info.message) || ('WebView 获取失败：' + ((info && info.error) || '未知'));
        el.baiduShareStatus.textContent = '⚠ ' + msg;
        el.baiduShareStatus.className = 'baidu-share-status is-err';
        return;  // ★ 停止，绝不回退浏览器
      } catch (e) {
        el.baiduShareStatus.textContent = '⚠ WebView 获取异常：' + e.message;
        el.baiduShareStatus.className = 'baidu-share-status is-err';
        return;
      }
    }
    // 回退：无 WebView / 无 fs_id → 走原有 transfer/dlink/浏览器降级链路
    _doDownload('');
  }

  function _fmtSize(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
    return (n / 1024 / 1024 / 1024).toFixed(2) + ' GB';
  }

  function renderBaiduBreadcrumb() {
    const parts = currentBaiduPath.split('/').filter(Boolean);
    let acc = '';
    const crumbs = [{ label: '根目录', path: '/' }];
    parts.forEach((p) => { acc += '/' + p; crumbs.push({ label: p, path: acc }); });
    el.baiduBreadcrumb.innerHTML = crumbs.map((c, i) => {
      const sep = i ? '<span class="sep"> / </span>' : '';
      return `${sep}<span class="crumb" data-path="${encodeURIComponent(c.path)}">${c.label}</span>`;
    }).join('');
    el.baiduBreadcrumb.querySelectorAll('.crumb').forEach((elc) => {
      elc.addEventListener('click', () => loadBaiduList(decodeURIComponent(elc.dataset.path)));
    });
  }

  async function loadBaiduList(path) {
    if (!baiduToken) return;
    currentBaiduPath = path || '/';
    renderBaiduBreadcrumb();
    el.baiduList.innerHTML = '<p class="baidu-empty">加载中…</p>';
    try {
      const r = await fetch(`/api/cloud/baidu/list?path=${encodeURIComponent(currentBaiduPath)}&token=${encodeURIComponent(baiduToken)}`);
      const data = await r.json();
      if (!r.ok) {
        el.baiduList.innerHTML = `<p class="baidu-empty">加载失败：${data.detail || r.status}</p>`;
        return;
      }
      const list = data.list || [];
      if (!list.length) {
        el.baiduList.innerHTML = '<p class="baidu-empty">此目录为空。</p>';
        return;
      }
      el.baiduList.innerHTML = list.map((it) => {
        const icon = it.isdir ? '📁' : '📄';
        // 百度限制：第三方应用只能下载 /apps/ 目录，用户网盘任意路径文件直下会 errno=20020。
        // 因此这里只浏览，下载引导用户走「分享链接下载」（转存到 /apps/ 目录再下）。
        const dlBtn = it.isdir ? '' : `<span class="baidu-nodl" title="百度限制第三方应用不能直下网盘任意文件，请用上方「分享链接下载」">需用分享链接</span>`;
        return `<div class="baidu-row">
          <span class="icon">${icon}</span>
          <span class="name ${it.isdir ? 'folder' : ''}" ${it.isdir ? `data-go="${encodeURIComponent(it.path)}"` : ''}>${it.name}</span>
          <span class="size">${it.isdir ? '' : _fmtSize(it.size)}</span>
          <span class="dl">${dlBtn}</span>
        </div>`;
      }).join('');
      el.baiduList.querySelectorAll('.name.folder').forEach((n) => {
        n.addEventListener('click', () => loadBaiduList(decodeURIComponent(n.dataset.go)));
      });
    } catch (err) {
      el.baiduList.innerHTML = `<p class="baidu-empty">加载出错：${err.message}</p>`;
    }
  }

  function startBaiduDownload(item) {
    if (!baiduToken) { el.baiduDriveStatus.textContent = '请先授权'; return; }
    fetch('/api/cloud/baidu/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: baiduToken, fs_id: item.fs_id, path: item.path, name: item.name }),
    }).then((r) => r.json()).then((data) => {
      if (!data.task_id) { alert('发起下载失败：' + (data.detail || '未知错误')); return; }
      addBaiduDlItem(data.task_id, item.name);
      pollBaiduTask(data.task_id);
    }).catch((err) => alert('发起下载失败：' + err.message));
  }

  function addBaiduDlItem(tid, name) {
    if (el.baiduDlList.querySelector('.baidu-empty')) el.baiduDlList.innerHTML = '';
    const div = document.createElement('div');
    div.className = 'baidu-dl-item';
    div.id = 'baidudl-' + tid;
    div.innerHTML = `<div class="top"><span class="nm">${name}</span><span class="st">排队中…</span></div>
      <div class="baidu-dl-bar"><i></i></div>`;
    el.baiduDlList.prepend(div);
  }

  function pollBaiduTask(tid) {
    if (baiduDlPollers[tid]) clearInterval(baiduDlPollers[tid]);
    baiduDlPollers[tid] = setInterval(async () => {
      try {
        const r = await fetch(`/api/cloud/baidu/task/${tid}`);
        const t = await r.json();
        const div = document.getElementById('baidudl-' + tid);
        if (!div) return;
        const st = div.querySelector('.st');
        const bar = div.querySelector('.baidu-dl-bar > i');
        const total = Number(t.total) || 0;
        const pct = total ? Math.min(100, Math.round((Number(t.progress) / total) * 100)) : 0;
        bar.style.width = pct + '%';
        if (t.status === 'downloading') {
          st.textContent = `下载中 ${pct}%  (${_fmtSize(t.progress)} / ${_fmtSize(total)})`;
          st.className = 'st';
        } else if (t.status === 'completed') {
          st.textContent = `完成 ✓  ${_fmtSize(t.total || t.progress)}`;
          st.className = 'st is-ok';
          clearInterval(baiduDlPollers[tid]);
        } else if (t.status === 'failed') {
          st.textContent = '失败：' + (t.error || '未知');
          st.className = 'st is-err';
          clearInterval(baiduDlPollers[tid]);
        } else if (t.status === 'browser_fallback') {
          st.textContent = '请在浏览器中下载';
          st.className = 'st is-warn';
          clearInterval(baiduDlPollers[tid]);
          // pywebview 的 WKWebView 不支持 window.open（会被静默拦截），
          // 改用 Python 桥接的 open_external() 在系统浏览器打开百度原生分享页
          if (t.browser_url) {
            // 显示可点击链接，双重保险（即使自动打开失败也能手动点）
            let link = div.querySelector('a.baidu-open-link');
            if (!link) {
              link = document.createElement('a');
              link.className = 'baidu-open-link';
              link.target = '_blank';
              link.rel = 'noopener';
              link.style.cssText = 'display:inline-block;margin-top:6px;color:#4a90d9;font-size:.8rem;';
              div.appendChild(link);
            }
            link.href = t.browser_url;
            link.textContent = '↗ 点击在浏览器打开百度分享页下载';
            try {
              // 委托桌面增强层原生打开；无桥接（含 web 端）回退浏览器
              const opened = window.VDL && window.VDL.desktop && window.VDL.desktop.openExternal(t.browser_url);
              if (!opened) window.open(t.browser_url, '_blank');  // 浏览器模式回退
            } catch (e) {
              // 自动打开失败不致命，用户可点上面的链接手动打开
            }
          }
        } else {
          st.textContent = '排队中…';
        }
      } catch (err) {
        /* 忽略瞬时错误，下次轮询重试 */
      }
    }, 1000);
  }

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
    const isUp = view === 'uploadconvert';
    const isDw = view === 'dw';
    el.downloadView.hidden = isLib || isSub || isTor || isCom || isUp || isDw;
    el.libraryView.hidden = !isLib;
    el.subscribeView.hidden = !isSub;
    el.torrentView.hidden = !isTor;
    el.commentaryView.hidden = !isCom;
    el.uploadConvertView.hidden = !isUp;
    el.dwView.hidden = !isDw;
    el.tabDownload.classList.toggle('is-active', !isLib && !isSub && !isTor && !isCom && !isUp && !isDw);
    el.tabLibrary.classList.toggle('is-active', isLib);
    el.tabSubscribe.classList.toggle('is-active', isSub);
    el.tabTorrent.classList.toggle('is-active', isTor);
    el.tabCommentary.classList.toggle('is-active', isCom);
    el.tabUploadConvert.classList.toggle('is-active', isUp);
    el.tabDw.classList.toggle('is-active', isDw);
    if (isLib) loadLibrary();
    if (isSub) loadSubscriptions();
    if (isCom) loadCommentary();
    if (isUp) { el.ucStatus.textContent = ''; }
    if (isDw) { el.dwImgStatus.textContent = ''; el.dwPdfStatus.textContent = ''; }
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
  el.tabUploadConvert.addEventListener('click', () => switchView('uploadconvert'));
  el.tabDw.addEventListener('click', () => switchView('dw'));
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
    if (node.baiduAuthUrl) openBaiduAuthInPage();
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
    .then(({ region, peer, china_domains: domains, commentary_enabled, ads_enabled, convert, download, cloud, library, subscriptions, retention, archive, crypto, torrent, ai_dewatermark, authRequired, profile }) => {
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
      node.convertMaxUpload = (convert && convert.max_upload_bytes) || 0;
      node.convertTargets = (convert && Array.isArray(convert.targets) && convert.targets.length)
        ? convert.targets : ['mp4','mov','mkv','webm','avi','flv','ts','m4v','wmv','mpeg','3gp','ogv','mp3','m4a','aac','wav','flac','ogg','opus','gif'];
      node.downloadSubRequired = !!(download && download.subscription_required);
      node.downloadFreeDaily = (download && download.free_daily) || 10;
      const cloudInfo = cloud || {};
      node.cloudSubRequired = !!(cloudInfo && cloudInfo.subscription_required);
      node.cloudFreeDaily = (cloudInfo && cloudInfo.free_daily) || 5;
      node.cloudFreeUsed = 0;
      node.cloudProviders = (cloudInfo && cloudInfo.providers) || ['webdav'];
      node.baiduAvailable = !!(cloudInfo && cloudInfo.baidu_available);
      node.baiduAuthUrl = (cloudInfo && cloudInfo.baidu_auth_url) || '';
      el.tabBaidu.hidden = !node.baiduAvailable;
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
      node.profile = profile;
      // —— Route B：网页精简版（profile=web）按 profile 隐藏 App 专属 tab ——
      if (profile === 'web') {
        // 网页端保留：下载(核心) / 视频转换 / 去水印；隐藏其余 App 专属入口。
        // （convert / dewatermark router 已在 web-dev 挂载；缺依赖时优雅返回 503，不会 404）
        ['tabLibrary', 'tabCommentary', 'tabSubscribe', 'tabTorrent', 'tabBaidu', 'tabPcs']
          .forEach(id => { const t = document.getElementById(id); if (t) t.hidden = true; });
      } else {
        // App 端：沿用能力精细控制（修掉之前漏隐藏 library/subscribe/pcs 的 bug）
        if (el.tabTorrent) el.tabTorrent.hidden = !node.torrentEnabled;
        if (el.tabBaidu) el.tabBaidu.hidden = !node.baiduAvailable;
        if (el.tabCommentary) el.tabCommentary.hidden = !node.commentaryEnabled;
        if (el.tabUploadConvert) el.tabUploadConvert.hidden = false; // 本地核心能力
        if (el.tabDw) el.tabDw.hidden = !node.aiDewatermarkEnabled;  // 依赖 AI 去水印能力
        if (el.tabLibrary) el.tabLibrary.hidden = !node.libraryEnabled;
        if (el.tabSubscribe) el.tabSubscribe.hidden = !node.subscriptionsEnabled;
        if (el.tabPcs) el.tabPcs.hidden = !node.baiduAvailable;
      }
      el.tabs.hidden = false; // 至少有下载 tab，导航栏始终显示
      // 默认视图：网页精简版停在下载，App 端停在解说成片
      switchView(profile === 'web' ? 'download' : 'commentary');
      bootViewSet = true; // 标记初始化已设置视图，阻止 setTimeout 兜底覆盖
      initSubUI();
      paintNodeBar();
    })
    .catch(() => { /* 取不到节点信息就退回单节点，全部走本机 */ });
  // 兜底默认视图（节点信息未加载时）：停在核心下载视图，两个 profile 都不会 404。
  try { switchView('download'); bootViewSet = true; } catch (_) {}
  // 启动即确保全局错误提示框隐藏，没错误就完全不显示
  try { clearError(); } catch (_) {}

  // ------------------------------------------------------------------ 留言反馈（2026-08-23）
  // 右下角悬浮按钮 → 弹窗 → POST /api/feedback（request 自动带 X-Device-Id）
  const initFeedback = () => {
    const fab = document.getElementById('feedbackFab');
    const dlg = document.getElementById('feedbackDialog');
    const form = document.getElementById('feedbackForm');
    if (!fab || !dlg || !form) return;
    const content = document.getElementById('feedbackContent');
    const contact = document.getElementById('feedbackContact');
    const status = document.getElementById('feedbackStatus');
    const cancelBtn = document.getElementById('feedbackCancel');
    const submitBtn = document.getElementById('feedbackSubmit');
    const closeDlg = () => { try { dlg.close(); } catch (e) { dlg.removeAttribute('open'); } };
    const openDlg = () => { status.hidden = true; try { dlg.showModal(); } catch (e) { dlg.setAttribute('open', ''); } };
    fab.addEventListener('click', openDlg);
    if (cancelBtn) cancelBtn.addEventListener('click', closeDlg);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const text = (content.value || '').trim();
      if (!text) {
        status.textContent = '请先填写反馈内容';
        status.className = 'feedback-status err'; status.hidden = false;
        return;
      }
      if (submitBtn) submitBtn.disabled = true;
      status.className = 'feedback-status'; status.textContent = '提交中…'; status.hidden = false;
      try {
        await request('/api/feedback', {
          method: 'POST',
          body: JSON.stringify({ content: text, contact: (contact.value || '').trim() }),
        });
        status.className = 'feedback-status ok';
        status.textContent = '✅ 已收到你的反馈，感谢！';
        content.value = ''; contact.value = '';
        setTimeout(closeDlg, 1200);
      } catch (err) {
        status.className = 'feedback-status err';
        status.textContent = '提交失败：' + (err.message || '请稍后重试');
      } finally {
        if (submitBtn) submitBtn.disabled = false;
      }
    });
  };
  try { initFeedback(); } catch (_) { /* 反馈组件缺失不影响主流程 */ }

  // Phase 2：暴露共享 helper 到 window.VDL，供 web/js/desktop-app.js（桌面版专属脚本）复用。
  // 仅追加命名空间，不改变任何现有运行时行为；web 与 app 共享这些基础能力。
  window.VDL = Object.assign(window.VDL || {}, {
    el,
    $,
    escHtml,
    request,
    showError,
    createTaskCard,
    switchView,
  });
})();
