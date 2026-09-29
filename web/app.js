/* 视频工坊 · 前端逻辑（无依赖）
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

  // 客户端错误上报：把前端 JS 运行期错误（含未处理的 Promise rejection）发到服务端
  // /api/client-error，落入结构化事件日志 —— 闭环「网站前端报错我们看不到记录」。
  // 自身任何异常都静默吞掉，绝不因上报逻辑再触发二次错误。
  let _vdlErrSent = 0;
  const _vdlErrCap = 50; // 单会话上限，防止错误风暴刷屏服务端
  function _vdlReportError(message, stack, category) {
    if (_vdlErrSent >= _vdlErrCap) return;
    _vdlErrSent++;
    try {
      const payload = {
        message: String(message || '').slice(0, 2000),
        stack: String(stack || '').slice(0, 3000),
        url: location.href.slice(0, 500),
        level: 'error',
        category: category || 'client_js',
      };
      const body = JSON.stringify(payload);
      if (navigator.sendBeacon) {
        navigator.sendBeacon('/api/client-error', new Blob([body], { type: 'application/json' }));
      } else {
        fetch('/api/client-error', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body, keepalive: true }).catch(() => {});
      }
    } catch (_) {}
  }
  window.addEventListener('error', (e) => {
    try {
      const stack = (e.error && e.error.stack) ? e.error.stack : '';
      const loc = e.filename ? (String(e.filename).split('/').pop() + ':' + e.lineno + ':' + e.colno) : '';
      _vdlReportError((e.message || (e.error && e.error.message) || e.error) + (loc ? ' @ ' + loc : ''), stack, 'client_js');
    } catch (_) {}
  });
  window.addEventListener('unhandledrejection', (e) => {
    try {
      const r = e.reason;
      const msg = (r && (r.message || r.reason)) ? (r.message || r.reason) : String(r);
      const stack = (r && r.stack) ? r.stack : '';
      _vdlReportError(msg, stack, 'unhandledrejection');
    } catch (_) {}
  });

  // 导出诊断信息：点击从 /api/diagnostic 拉取打包并下载为 JSON，便于用户报障时附带。
  const _exportDiagBtn = document.getElementById('exportDiagBtn');
  if (_exportDiagBtn) {
    _exportDiagBtn.addEventListener('click', async () => {
      const _orig = _exportDiagBtn.textContent;
      try {
        _exportDiagBtn.disabled = true;
        _exportDiagBtn.textContent = '生成中…';
        const r = await fetch('/api/diagnostic', { credentials: 'same-origin' });
        if (!r.ok) { window.alert('导出诊断失败：HTTP ' + r.status); return; }
        const data = await r.json();
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'vdl-diagnostic-' + new Date().toISOString().replace(/[:.]/g, '-') + '.json';
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 1000);
      } catch (err) {
        window.alert('导出诊断失败：' + (err && err.message ? err.message : err));
      } finally {
        _exportDiagBtn.disabled = false;
        _exportDiagBtn.textContent = _orig;
      }
    });
  }

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
    browserHlsBtn: $('browserHlsBtn'),
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
    // 视频处理板块内的子模块切换（格式转换 / 拼接）
    ucSubFormat: $('ucSubFormat'),
    ucSubMerge: $('ucSubMerge'),
    ucPane: $('ucPane'),
    mcPane: $('mcPane'),
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
    // —— 音乐转换（2026-09-11 自 app-dev 移植）——
    musicConvertView: $('musicConvertView'),
    musAddBtn: $('musAddBtn'), musFileInput: $('musFileInput'), musClearBtn: $('musClearBtn'),
    musCount: $('musCount'), musList: $('musList'), musBulk: $('musBulk'),
    musBulkTarget: $('musBulkTarget'), musBulkBitrate: $('musBulkBitrate'), musBulkLibrary: $('musBulkLibrary'),
    musBulkApplyBtn: $('musBulkApplyBtn'), musStartAllBtn: $('musStartAllBtn'), musStatus: $('musStatus'),
    // —— 图片转换 ——
    imageConvertView: $('imageConvertView'),
    imgAddBtn: $('imgAddBtn'), imgFileInput: $('imgFileInput'), imgClearBtn: $('imgClearBtn'),
    imgCount: $('imgCount'), imgList: $('imgList'), imgBulk: $('imgBulk'),
    imgBulkTarget: $('imgBulkTarget'), imgBulkQuality: $('imgBulkQuality'), imgBulkResize: $('imgBulkResize'),
    imgBulkFlatten: $('imgBulkFlatten'), imgBulkLibrary: $('imgBulkLibrary'),
    imgBulkApplyBtn: $('imgBulkApplyBtn'), imgStartAllBtn: $('imgStartAllBtn'), imgStatus: $('imgStatus'),
    // —— AI 字幕 ——
    subtitleView: $('subtitleView'),
    sbPickBtn: $('sbPickBtn'), sbFileInput: $('sbFileInput'), sbFileLabel: $('sbFileLabel'),
    sbModel: $('sbModel'), sbLang: $('sbLang'), sbFast: $('sbFast'), sbStartBtn: $('sbStartBtn'),
    sbProgressWrap: $('sbProgressWrap'), sbProgressFill: $('sbProgressFill'), sbStatus: $('sbStatus'),
    sbResult: $('sbResult'), sbMeta: $('sbMeta'), sbHelpBtn: $('sbHelpBtn'), sbHelpText: $('sbHelpText'),
    sbDlSrt: $('sbDlSrt'), sbDlTxt: $('sbDlTxt'),
    // —— 个人中心（2026-09-29 对齐 App）——
    profileView: $('profileView'),
    pfAuthBox: $('pfAuthBox'), pfAuthModeTitle: $('pfAuthModeTitle'),
    pfIdentifier: $('pfIdentifier'), pfPassword: $('pfPassword'),
    pfAuthSubmit: $('pfAuthSubmit'), pfAuthSwitch: $('pfAuthSwitch'), pfAuthStatus: $('pfAuthStatus'),
    pfUserBox: $('pfUserBox'),
    // 子导航 + 分面板（2026-09-30 对齐 App 个人中心）
    pfSubnav: $('pfSubnav'),
    pfPanelOverview: $('pfPanelOverview'), pfPanelPurchases: $('pfPanelPurchases'),
    pfPanelCredits: $('pfPanelCredits'), pfPanelSecurity: $('pfPanelSecurity'),
    pfSecEmail: $('pfSecEmail'),
    pfAvatar: $('pfAvatar'), pfAvatarImg: $('pfAvatarImg'), pfAvatarFallback: $('pfAvatarFallback'), pfAvatarInput: $('pfAvatarInput'),
    pfName: $('pfName'), pfTag: $('pfTag'), pfCreated: $('pfCreated'),
    pfMemberNone: $('pfMemberNone'), pfMemberCardList: $('pfMemberCardList'),
    pfCreditsTotal: $('pfCreditsTotal'), pfCreditsAi: $('pfCreditsAi'), pfCreditsAiNote: $('pfCreditsAiNote'), pfCreditsPerm: $('pfCreditsPerm'),
    pfUsageFilter: $('pfUsageFilter'), pfUsageQuotaHeader: $('pfUsageQuotaHeader'),
    pfUsageTable: $('pfUsageTable'),
    pfPurchasesFilter: $('pfPurchasesFilter'), pfPurchases: $('pfPurchases'),
    pfCreditsLog: $('pfCreditsLog'),
    pfLogoutBtn: $('pfLogoutBtn'),
    pfActivateCode: $('pfActivateCode'), pfActivateBtn: $('pfActivateBtn'), pfMemberStatus: $('pfMemberStatus'),
    // —— 登录强制（2026-09-28 对齐 App）：右上角账号按钮 + 登录弹窗 ——
    authHeaderBtn: $('authHeaderBtn'),
    authModal: $('authModal'), authModalTitle: $('authModalTitle'), authModalHint: $('authModalHint'),
    authModalClose: $('authModalClose'),
    amIdentifier: $('amIdentifier'), amPassword: $('amPassword'),
    amSubmit: $('amSubmit'), amSwitch: $('amSwitch'), amStatus: $('amStatus'),
    // —— 个人中心 · 账号安全（改密 / 忘记密码 / 注销）——
    pfCurPw: $('pfCurPw'), pfNewPw: $('pfNewPw'), pfChangePwBtn: $('pfChangePwBtn'),
    pfChangePwForm: $('pfChangePwForm'), pfChangePwToggle: $('pfChangePwToggle'), pfChangePwCancel: $('pfChangePwCancel'),
    pfForgotBtn: $('pfForgotBtn'), pfResetBox: $('pfResetBox'),
    pfResetIdent: $('pfResetIdent'), pfResetCode: $('pfResetCode'), pfResetPw: $('pfResetPw'),
    pfResetSendBtn: $('pfResetSendBtn'), pfResetSubmit: $('pfResetSubmit'),
    pfSecurityStatus: $('pfSecurityStatus'), pfDeactivateBtn: $('pfDeactivateBtn'),

    // 去水印（需求文档模块二）
    tabDw: $('tabDw'),
    dwView: $('dwView'),
    tabAppIntro: $('tabAppIntro'),
    tabMusicConvert: $('tabMusicConvert'), tabImageConvert: $('tabImageConvert'),
    tabSubtitle: $('tabSubtitle'), tabProfile: $('tabProfile'),
    appIntroView: $('appIntroView'),
    dwModeImg: $('dwModeImg'),
    dwModePdf: $('dwModePdf'),
    dwImgPane: $('dwImgPane'),
    dwImgFile: $('dwImgFile'),
    dwPreviewWrap: $('dwPreviewWrap'),
    dwImgPreview: $('dwImgPreview'),
    dwImgSvg: $('dwImgSvg'),
    dwImgCanvas: $('dwImgCanvas'),
    dwExpandBtn: $('dwExpandBtn'),
    dwExpandBtn2: $('dwExpandBtn2'),
    dwSelInfo: $('dwSelInfo'),
    dwZoomIn: $('dwZoomIn'),
    dwZoomOut: $('dwZoomOut'),
    dwZoomFit: $('dwZoomFit'),
    dwZoomLabel: $('dwZoomLabel'),
    dwImgMethod: $('dwImgMethod'),
    dwImgEngine: $('dwImgEngine'),
    dwImgCvField: $('dwImgCvField'),
    dwImgRadiusField: $('dwImgRadiusField'),
    // 去水印放大弹窗
    dwImgModal: $('dwImgModal'),
    // 结果预览灯箱 + 重新加工
    dwResultLightbox: $('dwResultLightbox'),
    dwResultLightboxImg: $('dwResultLightboxImg'),
    dwResultLightboxCap: $('dwResultLightboxCap'),
    dwResultLightboxClose: $('dwResultLightboxClose'),
    dwImgRedo: $('dwImgRedo'),
    dwModalClose: $('dwModalClose'),
    dwModalDone: $('dwModalDone'),
    dwModalImg: $('dwModalImg'),
    dwModalSvg: $('dwModalSvg'),
    dwModalCanvas: $('dwModalCanvas'),
    dwModalZoomIn: $('dwModalZoomIn'),
    dwModalZoomOut: $('dwModalZoomOut'),
    dwModalZoomFit: $('dwModalZoomFit'),
    dwModalZoomLabel: $('dwModalZoomLabel'),
    dwModalSelInfo: $('dwModalSelInfo'),
    dwModalPreviewWrap: $('dwModalPreviewWrap'),
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

  // 记忆用户粘贴过的会话 Cookie（localStorage 本机存储，免每次重粘）。
  // 注意：只存用户自己主动粘贴的 Cookie；「贡献公共池」的共享逻辑不受影响。
  try {
    const savedCookie = localStorage.getItem('vdl_cookie');
    if (savedCookie && el.cookieInput && !el.cookieInput.value) {
      el.cookieInput.value = savedCookie;
    }
  } catch (e) { /* localStorage 不可用（隐私模式等）时静默跳过 */ }

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
    libraryEnabled: false,
    subscriptionsEnabled: false,
    retentionEnabled: false,
    trashAvailable: false,
    cryptoEnabled: false,
    cryptoHasPass: false,
    cryptoLocked: true,
  };
  // 启动即回填上次已知的对端信息（2026-09-27）。
  // 背景：`node` 只在本页加载时取一次，页面开着不关就永远停在那一刻的认知。
  // 若服务器侧后来才配上对端（VDL_PEER_ENDPOINT），老页面仍以为自己是「本机直连」，
  // 于是把 YouTube 请求发给国内节点 —— 国内节点到不了 YouTube（TLS SNI 被重置），
  // 用户只看到难懂的 `Connection reset by peer`，而换台机器/新开页面却正常。
  // 故落盘 + 回填：即便本次 /api/nodes 失败，也不会丢掉对端。
  try {
    const cachedPeer = localStorage.getItem('vdl_peer');
    const cachedRegion = localStorage.getItem('vdl_region');
    if (cachedPeer) node.peer = cachedPeer;
    if (cachedRegion) node.region = cachedRegion;
  } catch (e) { /* 隐私模式可能抛错 */ }

  /** 手动覆盖：null=自动判断，'cn'/'global'=用户强制指定 */
  let forcedRegion = null;

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
      // 下载强制登录（2026-09-28 对齐 App）：服务端 403 + 登录文案 → 前端弹登录框
      if (response.status === 403 && /登录/.test(err.message)) err.needLogin = true;
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
    // 登录态 bearer（2026-09-28）：默认全量携带，服务端据此把下载/配额归因到账号
    // （V1 免费下载 10 次/日按账号计——不带头就会被当成匿名，计数落不到用户头上）。
    // 未登录时为 undefined，服务端按匿名处理，行为与从前一致。
    const authToken = localStorage.getItem('vdl_auth_token');
    if (authToken) headers['Authorization'] = 'Bearer ' + authToken;
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
    // fetch 超时保护（2026-08-24）：默认 120s（覆盖 VPS worker 85s 上限 + 余量），
    // 防止后端挂起时前端无限等待。大文件上传/下载可传 options.timeout=0 关闭
    // 或传更大值；请求超时抛可读错误而非静默卡死。
    const fetchTimeout = (options && options.timeout) || 120000;
    // 下载强制登录（2026-09-28 对齐 App）：未登录直接拦在本地，弹登录框、不发请求。
    // request 定义早于登录弹窗代码，故经 window 钩子解耦（弹窗代码稍后挂载）。
    if ((path === '/api/download' || path === '/api/batch') && !localStorage.getItem('vdl_auth_token')) {
      try { if (window.__vdlOpenAuthModal) window.__vdlOpenAuthModal(); } catch (_e) { /* ignore */ }
      throw { needLogin: true, message: '下载前请先登录账号（免费账号每日 10 次下载额度，注册即得）', hint: '' };
    }
    const doFetch = () => {
      if (!fetchTimeout) return fetch(apiBase + path, { ...options, headers: merged });
      const ctrl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
      if (!ctrl) return fetch(apiBase + path, { ...options, headers: merged });
      const timer = setTimeout(() => ctrl.abort(), fetchTimeout);
      return fetch(apiBase + path, { ...options, headers: merged, signal: ctrl.signal })
        .finally(() => clearTimeout(timer));
    };
    let response;
    try {
      response = await doFetch();
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
    } catch (e) {
      // fetch 超时（AbortError）→ 转成可读提示；DOMException 在部分浏览器无 name
      if (e && (e.name === 'AbortError' || e.code === 20 || String(e.message || '').includes('aborted'))) {
        const secs = Math.round(fetchTimeout / 1000) || 120;
        throw { message: '请求超时（超过 ' + secs + 's），请重试或检查网络', hint: '解析服务较慢或网络不稳定，稍后重试。', category: 'timeout' };
      }
      throw e;
    }
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
  // ⚠️ 文案按运行环境区分（2026-09-29）：「本地服务/Cmd+Q/DMG」只对桌面壳有意义，
  // Chrome 打开的网页版网络失败曾被误引导成重启桌面应用，用户完全无所适从。
  const _isDesktopShell = () => !!(window.pywebview || window.__VDL_DESKTOP_SHELL);
  const _friendlyNetworkError = (msg) => {
    const lower = String(msg || '').toLowerCase();
    if (lower === 'load failed' || lower === 'failed to fetch' || lower === 'networkerror'
        || lower.includes('load failed') || lower.includes('failed to fetch')
        || lower.includes('networkerror') || lower.includes('network error')) {
      if (_isDesktopShell()) {
        return {
          message: '连接本地服务失败',
          hint: '请稍等 2~3 秒后重试；若仍失败，请完全退出应用（Cmd+Q）再重新打开，避免从 DMG 镜像里启动。'
        };
      }
      return {
        message: '网络连接失败',
        hint: '请检查网络后重试；海外链接可点输入框下方「切换线路」后重试，解析会自动改走可用的线路。'
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
        // 登录态已失效 → 清掉本机记忆的 Cookie，避免下次继续带着失效值重试
        if (cat === 'cookie_invalid_or_expired') {
          try { localStorage.removeItem('vdl_cookie'); } catch (e) {}
        }
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
      // 原画档角标：只做「讲清楚档位」这件事，不做拦截。
      // 硬闸门需要后端按画质鉴权，而当前 node.subscribed 只在额度响应里被置真、
      // 从没有被置假过，据此拦截会误伤已付费用户 → 本轮只展示，不 gate。
      if (quality.pro) {
        const badge = document.createElement('span');
        badge.className = 'quality-pro';
        badge.textContent = 'PRO';
        badge.title = '原画档 · 会员权益';
        label.append(badge);
      }
      const note = document.createElement('small');
      const noteText = quality.approx_size
        ? `${quality.note} · 约 ${formatBytes(quality.approx_size)}`
        : quality.note;
      note.textContent = quality.pro ? `${noteText} · 原画需会员` : noteText;

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
      // 直链透传：跳过清晰度选择与服务器任务队列，走浏览器侧分片下载引擎
      el.qualityBlock.hidden = true;
      el.downloadBtn.lastChild.textContent = '直接保存到本机 ⬇';
      el.directHint.hidden = false;
      el.directHint.textContent = '✅ 检测到可直接下载的文件。点上方按钮即分片加速下载（10MB/片 · 3 路并发 · 自动重试），有实时进度、可再点一次取消；加速不可用时自动降级为浏览器直连。';
      el.serverFallbackBtn.hidden = false;
      if (el.browserHlsBtn) el.browserHlsBtn.hidden = true;
    } else {
      el.qualityBlock.hidden = false;
      el.downloadBtn.lastChild.textContent = '开始下载';
      el.directHint.hidden = true;
      el.directHint.textContent = '';
      el.serverFallbackBtn.hidden = true;
      renderQualities(data.qualities);
      // HLS 源额外给一条「浏览器内合成」：产物 .ts/.mp4，全程走用户带宽，
      // 省下服务器出口流量与磁盘（HLS 在服务端还要 ffmpeg 合并）。属于可选加速项，
      // 所以默认收起、只在确认可用时露出。直播没有终点；加密流要取到清单才知道，
      // 由 triggerBrowserHlsDownload 的报错兜底。
      // 注意 el 判空：CF 可能仍给出旧的 index.html（没有这个按钮）而 app.js 已是新版。
      if (el.browserHlsBtn) {
        const hasHlsUrl = (video.watch_options || []).some((o) => o && o.is_hls && o.url);
        el.browserHlsBtn.hidden = !(video.is_hls && !video.is_live && hasHlsUrl);
      }
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

    // 任务离开完成态后，必须隐藏完成态专属入口，避免重试/失败后仍显示转换/保存入口
    if (task.status !== 'completed') {
      refs.save.hidden = true;
      refs.saveHint.hidden = true;
      refs.convertWrap.hidden = true;
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
  const UC_CHUNK_SIZE = 32 * 1024 * 1024;       // 单片 32MB（默认）
  const UC_BIG_CHUNK_SIZE = 64 * 1024 * 1024;   // >2GB 文件单片 64MB（减少请求数，后端上限 64MB）
  const UC_CHUNK_CONCURRENCY = 2;               // 单文件分片并发路数（2026-08-28：CF 免费方案对 8 路并发大文件 POST 触发 Heavy Upload Limiter，每路降速到 ~200KB/s；降到 2 路绕过限流，单条 32MB 可跑 1.3MB/s）
  const UC_CHUNK_RETRIES = 2;                   // 单片失败重试次数（网络抖动自动重传）
  const UC_POLL_INTERVAL = 1500;                // 转码状态轮询间隔 ms（批量/无损直转进度更实时）
  // 上传端点：只保留同源一个（2026-09-29）。
  // 历史：曾用 `[location.origin, 'https://web-production-b9993.up.railway.app']` 做
  // 「双端点混合上传」——Railway 原生域无 CF 限速层，且与 CF 域**指向同一后端、同一份
  // 分片存储**，所以按通道吞吐动态选路是安全的。
  // 但该 Railway 应用自 2026-09-11 起已不存在（边缘 Application not found / 连接直接失败），
  // 而选路在「样本不足（<4 片）」阶段按奇偶分流 ⇒ **每个奇数下标分片都被发到死主机**：
  // 用户看到的就是「分片 2/8 上传失败」（2、4、6… 必失败，重试还会再撞一次死主机）。
  // 🔴 红线：任何新增端点必须与主站**同后端、同分片存储**，否则分片会落到另一个节点的
  //    磁盘上，finish 时必然报「分片不完整」——宁可不加，也不要加一个不同源的端点。
  const UC_UPLOAD_ENDPOINTS = [location.origin];
  // 通道质量统计（每文件独立）：最近成功分片的平均吞吐 bytes/ms，用于动态选路
  const ucChStats = () => ({
    samples: [[], []],
    total: 0,
    add(ci, bytes, ms) {
      const arr = this.samples[ci];
      arr.push({ bytes, ms });
      if (arr.length > 3) arr.shift();
      this.total++;
    },
    avg(ci) {
      const arr = this.samples[ci];
      if (!arr.length) return 0;
      let sb = 0, sm = 0;
      for (const s of arr) { sb += s.bytes; sm += s.ms; }
      return sm > 0 ? sb / sm : 0;
    },
  });
  // 动态选路：样本不足（<4 片）先按奇偶分流顺便采集；样本充足后 80% 走更快通道、
  // 20% 探索另一条（防抖动瞬间误判后锁死慢通道，让其有机会恢复并被重新采样）。
  // 重试（attempt>0）固定切到另一条通道（故障转移，不重试同一条坏链路）。
  const ucPickEndpoint = (item, i, attempt) => {
    const n = UC_UPLOAD_ENDPOINTS.length;
    if (attempt > 0) return UC_UPLOAD_ENDPOINTS[(i + 1) % n];
    const st = item._chStats;
    if (!st || st.total < 4) return UC_UPLOAD_ENDPOINTS[i % n];
    const faster = st.avg(0) >= st.avg(1) ? 0 : 1;
    if (Math.random() < 0.8) return UC_UPLOAD_ENDPOINTS[faster];
    return UC_UPLOAD_ENDPOINTS[1 - faster];
  };
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

  // 转换产物文件名：`[格式]原文件名.扩展名`（目标扩展名映射）
  const UC_EXT_OF = { mp4:'mp4', mov:'mov', mkv:'mkv', webm:'webm', avi:'avi', flv:'flv', ts:'ts',
                      m4v:'m4v', wmv:'wmv', mpeg:'mpg', '3gp':'3gp', ogv:'ogv', hevc:'mp4',
                      mp3:'mp3', m4a:'m4a', aac:'aac', wav:'wav', flac:'flac', ogg:'ogg',
                      opus:'opus', wma:'wma', mp2:'mp2', gif:'gif' };
  const ucBuildOutputName = (it) => {
    const stem = ((it.file && it.file.name) || '').replace(/\.[^.]+$/, '') || 'converted';
    const ext = UC_EXT_OF[it.target] || it.target;
    return `[${it.target.toUpperCase()}]${stem}.${ext}`;
  };

  // 确保转码轮询在跑（批量/单行开始后立即启动，不等上传回调）
  const ucEnsurePolling = () => {
    if (!ucState.polling) ucState.polling = setInterval(ucPollAll, UC_POLL_INTERVAL);
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
    // 批量参数区常显；「开始批量转换」按钮：有已上传待转码项才可用（2026-08-23 批量统一开始）
    el.ucStartAllBtn.hidden = false;
    el.ucStartAllBtn.disabled = !list.some(it => it.status === 'uploaded');
    // 批量「默认输出格式」下拉只填充一次（避免每次渲染重建导致选中/焦点被打断而闪烁）
    if (el.ucBulkTarget && !el.ucBulkTarget.dataset.inited) {
      el.ucBulkTarget.dataset.inited = '1';
      el.ucBulkTarget.innerHTML = fmtOptions(el.ucBulkTarget.value || 'mp4');
    }
    // 大小上限提示（2026-08-28：10GB → 1GB，CF 免费方案对大文件并发上传限流严重）
    if (el.ucLimitTip) {
      el.ucLimitTip.textContent = `单个文件最大 1GB（大文件已自动分片并发上传提速）；想体验更大文件，体验免上传,一键转换,请前往桌面版端`;
    }

    if (!list.length) { el.ucList.innerHTML = ''; return; }

    el.ucList.innerHTML = list.map(it => {
      const statusText = {
        pending: '未开始',
        uploading: `上传中 ${it.progress||0}%${it.speedText ? ' · ' + it.speedText : ''}${it.uploadedText ? ' · ' + it.uploadedText : ''}`,
        uploaded: '已上传，待转码',
        running: it.stage === '无损直转' ? '无损直转中…'
               : it.stage === '排队中' ? '排队中…'
               : (it.progress ? `转码中 ${it.progress}%` : '转码中…'),
        completed: '完成 ✅',
        failed: '失败：' + (it.errorMsg || ''),
      }[it.status] || it.status;
      const statusCls = it.status === 'pending' ? '' : 'is-' + it.status.replace('uploading','running');
      // 移除按钮：pending/failed/uploading/uploaded 可移除（上传/待转码=取消+清分片）；转码中禁用
      const disabled = !['pending','failed','uploading','uploaded'].includes(it.status) ? 'disabled' : '';
      const progressHtml = (it.status === 'running' || it.status === 'uploading')
        ? `<div class="progress"><div class="progress-fill" style="width:${it.progress||0}%"></div></div>` : '';
      const downloadHtml = it.status === 'completed' && it.downloadUrl
        ? `<a class="uc-item-download" href="${it.downloadUrl}" download="${it.outputName||'converted'}">下载</a>${it.libraryId ? ' · 已存媒体库' : ''}`
        : '';
      // 待转码行：独立「开始转码」按钮（用该行格式单独开始，不影响批量）
      const startHtml = it.status === 'uploaded'
        ? `<button type="button" class="uc-item-start" data-act="start" title="用该行已设置的格式开始转码">开始转码</button>`
        : '';
      // 格式可改：uploading/uploaded 也能改（finish 提交时用最新值）；running/completed 锁定
      const targetDisabled = (it.status === 'running' || it.status === 'completed') ? 'disabled' : '';
      const targetTitle = it.status === 'completed' ? '已完成：格式已固定，如需其他格式请移除后重新添加'
                         : it.status === 'running' ? '转码中不可修改'
                         : '修改此行的目标格式（开始转码时生效）';
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
            <select id="ucItemTarget-${it.id}" data-act="target" ${targetDisabled} title="${targetTitle}">${fmtOptions(it.target)}</select>
            ${startHtml}
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
    // 自动开始上传（2026-08-23）；上传完成后停在「待转码」，可逐行设格式再点开始转码（单行/批量）
    el.ucStatus.textContent = `已添加 ${ucState.list.length} 个文件，自动开始上传…`;
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
    if (it.status === 'uploading' || it.status === 'uploaded') {
      ucCancelUpload(it);   // 取消/待转码：abort 分片（如有）+ 通知后端清理已传分片
      ucState.list = ucState.list.filter(x => x.id !== id);
      if (it.status === 'uploading') {
        ucState.active = Math.max(0, ucState.active - 1);  // uploading 时 promise 未结束，手动释放槽
      }
      renderUcList();
      el.ucStatus.textContent = '已取消并移除';
      ucPump();  // 拉下一个 pending / 刷新批量按钮状态
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
    // 取消所有上传中 + 待转码项（清理分片），再清空
    const uploading = ucState.list.filter(x => x.status === 'uploading');
    const uploaded = ucState.list.filter(x => x.status === 'uploaded');
    uploading.concat(uploaded).forEach(ucCancelUpload);
    ucState.active = Math.max(0, ucState.active - uploading.length);  // 仅 uploading 占用并发槽
    ucState.list = [];
    renderUcList();
    const n = uploading.length + uploaded.length;
    el.ucStatus.textContent = n ? `已取消 ${n} 个任务并清空列表` : '已清空列表';
    ucPump();
  };

  // 后台 tab 限流自动续传：document 切回前台时，若某个 uploading 项的 in-flight 分片
  // 超过 8 秒没 progress（被浏览器后台压低吞吐），主动 abort 让 worker 内部重试
  // （attempts++ 切到另一条通道：ucPickEndpoint attempt>0 切到另一端点）。
  // 后端 /api/upload-chunk 按 (upload_id, index) 覆盖写入，abort+retry 不会产生脏数据。
  const ucLastProgressByItem = new WeakMap();
  if (typeof document !== 'undefined' && !window.__vdl_ucVisBound) {
    window.__vdl_ucVisBound = true;
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState !== 'visible') return;
      const now = Date.now();
      for (const item of (ucState.list || [])) {
        if (!item || item.status !== 'uploading' || !item._xhrs || !item._xhrs.size) continue;
        const lastT = ucLastProgressByItem.get(item) || 0;
        if (now - lastT > 8000) {
          for (const xhr of item._xhrs) { try { xhr.abort(); } catch (_) {} }
        }
      }
    });
  }

  // 上传单个分片（32MB；小文件=1 片，与整传等效）
  // endpoint: 上传目标（双端点混合上传时按分片轮询/故障转移选择；默认同源）
  // onProgress(loaded) 让调用方合并 in-flight 字节算总进度，避免「长时间 0%」假卡死
  // xhrs(Set) 收集进行中的 XHR，供「上传中删除」时 abort
  const ucUploadChunk = (uploadId, index, total, blob, onProgress, xhrs, endpoint) => new Promise((resolve, reject) => {
    const form = new FormData();
    form.append('upload_id', uploadId);
    form.append('index', index);
    form.append('total', total);
    form.append('file', blob, 'chunk');
    const xhr = new XMLHttpRequest();
    xhr.open('POST', (endpoint || location.origin) + '/api/upload-chunk');
    // 设备隔离：XHR 不走 request() 封装，需手动带设备 ID（否则 job 无归属，文件不隔离）
    xhr.setRequestHeader('X-Device-Id', deviceId());
    xhr.timeout = 300000;   // 5 分钟单片超时（防后台 tab 限流/网络静默断网卡死；后台 tab 限流可压到 ~100KB/s，32MB 分片需 5+ 分钟）
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
        let fromServer = false;
        try { const d = JSON.parse(xhr.responseText || '{}'); if (d.detail) { msg = d.detail; fromServer = true; } } catch (e) { /* ignore */ }
        // 413 分两种，别混：应用自己的 413 一定带 JSON detail（「单个分片超过大小上限」等）；
        // 非 JSON 的 413 = 被网关（nginx/CF）在到达应用前就拒了 —— 即「单次上传体积超限」，
        // 这时要说清是网关而不是文件本身有问题，否则会误判成「视频太大不能传」。
        if (xhr.status === 413 && !fromServer) msg = '上传被网关拒绝（HTTP 413·单次体积超限）';
        reject(new Error(msg));
      }
    });
    xhr.addEventListener('error', () => { cleanup(); reject(new Error('网络错误')); });
    xhr.addEventListener('timeout', () => { cleanup(); reject(new Error('分片超时（5 分钟无响应，可能是网络断/后台 tab 限流；切回前台后会自动续传）')); });
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
    item._chStats = ucChStats();      // 双通道质量统计（动态选路用）
    renderUcList();
    const file = item.file;
    // >2GB 大文件用 64MB 分片（减少请求数）；否则 32MB
    const chunkSize = file.size > 2 * 1024 * 1024 * 1024 ? UC_BIG_CHUNK_SIZE : UC_CHUNK_SIZE;
    const totalChunks = Math.max(1, Math.ceil(file.size / chunkSize));
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
      ucLastProgressByItem.set(item, Date.now());  // visibilitychange 自动续传判据
    };

    // 分片并发 worker 池：每片失败重试 UC_CHUNK_RETRIES 次，耗尽则整个文件失败
    let idx = 0;
    const workers = [];
    const worker = async () => {
      while (idx < totalChunks) {
        if (item._removed || item.status === 'failed') return;
        const i = idx++;
        const start = i * chunkSize;
        const end = Math.min(start + chunkSize, file.size);
        const blob = file.slice(start, end);
        let attempts = 0;
        for (;;) {
          try {
            const ep = ucPickEndpoint(item, i, attempts);   // 动态选路（快通道优先，重试换端）
            const stT = performance.now();
            await ucUploadChunk(uploadId, i, totalChunks, blob, (loaded) => {
              inFlight.set(i, loaded);  // 单片实时进度反馈
              updateProgress();
            }, item._xhrs, ep);
            const elT = performance.now();
            // 记录该通道最近一次成功分片的吞吐样本（bytes/ms），供后续分片选路
            item._chStats.total++;
            item._chStats.add(UC_UPLOAD_ENDPOINTS.indexOf(ep), end - start, elT - stT);
            break;
          } catch (e) {
            if (item._removed) return;  // 用户已删除：直接退出，不重试不报错
            attempts++;
            if (attempts > UC_CHUNK_RETRIES) {
              item.status = 'failed';
              // 区分超时（多因后台 tab 限流）：visibilitychange 切回前台会自动续传
              const hint = /超时/.test(e.message) ? '（切回前台后分片可自动续传）' : '';
              item.errorMsg = `分片 ${i + 1}/${totalChunks} 上传失败：${e.message}${hint}`;
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

    // 全部分片上传完成 → 停在「已上传·待转码」，等用户点开始转码（单行或批量）
    Promise.allSettled(workers).then(() => {
      if (item._removed || item.status === 'failed') return;
      item.status = 'uploaded';       // 已上传·待转码（新状态）
      item.progress = 30;
      item.speedText = ''; item.uploadedText = '';
      item._totalChunks = totalChunks; // 供 ucFinishOne 提交转码时使用
      renderUcList();
      resolve({ status: 'uploaded' });
    });
  });

  // 提交转码：调 finish 合并分片 + 启动 job（用该行最新设置的格式/参数；finish 一次性，失败需重传）
  const ucFinishOne = (item) => new Promise((resolve, reject) => {
    if (!item || item.status !== 'uploaded') { reject(new Error('状态不允许开始转码')); return; }
    item.status = 'running';
    item.progress = 30;
    item.stage = '';
    renderUcList();
    const form = new FormData();
    form.append('upload_id', item._uploadId);
    form.append('total', item._totalChunks || 1);
    form.append('filename', item.file.name);
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
    const _authTok = localStorage.getItem('vdl_auth_token');   // 2026-09-29 服务端功能门禁：finish 须登录
    if (_authTok) xhr.setRequestHeader('Authorization', 'Bearer ' + _authTok);
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
          let msg = data.detail || data.error || ('HTTP ' + xhr.status);
          if (/分片不完整|分片参数|文件超过|合并/.test(msg)) {
            // finish 是合并 + 启动转码的一次性操作；服务端的「分片不完整」意味着
            // 之前那次上传因后台 tab 限流 / 2min 超时没传完所有分片，但后端按
            // (upload_id, index) 覆盖写是幂等的 → 自动从 0 重传所有分片即可
            // （已传的会被覆盖，不会出现拼接错位）。最多自动重试 2 次仍失败
            // 才提示用户手动移除。
            item._retryCount = item._retryCount || 0;
            if (item._retryCount < 2 && item._uploadId) {
              item._retryCount++;
              item.status = 'pending';
              item.errorMsg = `分片未传完（${msg.match(/（([0-9]+)\/([0-9]+)）/)?.[1] || '?'}/${msg.match(/（([0-9]+)\/([0-9]+)）/)?.[2] || '?'}），已自动重传…`;
              item.speedText = ''; item.uploadedText = ''; item.progress = 0;
              renderUcList();
              // 异步重跑上传流程（保留 _uploadId，worker 池从 0 重传所有分片）
              setTimeout(() => ucUploadOne(item), 200);
              resolve({ status: 'retrying' });
              return;
            }
            msg += '（请移除此行后重新添加文件上传）';
          }
          item.errorMsg = msg;
          item.speedText = ''; item.uploadedText = '';
          renderUcList();
          reject(new Error(msg));
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
      ucState.polling = setInterval(ucPollAll, UC_POLL_INTERVAL);
    } else if (!hasRunning && ucState.polling) {
      clearInterval(ucState.polling);
      ucState.polling = null;
      const done = ucState.list.filter(x => x.status === 'completed').length;
      const fail = ucState.list.filter(x => x.status === 'failed').length;
      const remain = ucState.list.filter(x => x.status === 'pending').length;
      const wait = ucState.list.filter(x => x.status === 'uploaded').length;
      const parts = [];
      if (done) parts.push(`${done} 完成`);
      if (fail) parts.push(`${fail} 失败`);
      if (wait) parts.push(`${wait} 待转码`);
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
          it.stage = st.stage || '排队中';   // 排队/无损直转/转码中 区分显示
          renderUcList();
        } else if (st.status === 'completed') {
          it.status = 'completed';
          it.progress = 100;
          it.outputName = ucBuildOutputName(it);   // `[格式]原文件名.扩展名`，一眼可辨参数与来源
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

  // ===== 视频拼接（简单无损合并）：复用分片上传，独立面板 =====
  const MC_MAX_CONCURRENT = 2;
  const mcState = { list: [], nextId: 1, active: 0, polling: null };
  const mcListEl = document.getElementById('mcList');
  const mcCountEl = document.getElementById('mcCount');
  const mcStatusEl = document.getElementById('mcStatus');
  const mcAddBtn = document.getElementById('mcAddBtn');
  const mcFileInput = document.getElementById('mcFileInput');
  const mcClearBtn = document.getElementById('mcClearBtn');
  const mcOutFormat = document.getElementById('mcOutFormat');
  const mcOutName = document.getElementById('mcOutName');
  const mcLibrary = document.getElementById('mcLibrary');
  const mcMergeBtn = document.getElementById('mcMergeBtn');
  const mcFormatSize = ucFormatSize;

  const mcRender = () => {
    const segs = mcState.list.filter(x => !x.isResult);
    mcCountEl.textContent = segs.length ? `已添加 ${segs.length} 个片段` : '尚未添加片段';
    mcClearBtn.hidden = mcState.list.length === 0;
    const ready = mcState.list.filter(x => x.status === 'uploaded' && !x.isResult).length;
    const anyMerging = mcState.list.some(x => x.isResult && x.status === 'running');
    mcMergeBtn.disabled = ready < 2 || anyMerging;   // 拼接进行中禁用，防重复点出多个「合并结果」
    if (!mcState.list.length) { mcListEl.innerHTML = ''; return; }
    mcListEl.innerHTML = mcState.list.map((it, idx) => {
      const name = it.file ? it.file.name
        : (it.isResult && it.status === 'completed' && it.outputName ? it.outputName : it.label);
      const statusText = it.isResult
        ? (it.status === 'running'
             ? (it.stage === '拼接中' ? '拼接中…' : (it.progress ? `拼接中 ${it.progress}%` : '拼接中…'))
             : it.status === 'completed' ? (it.stale ? '完成 ✅ · 上次结果' : '完成 ✅') : '失败：' + (it.errorMsg || ''))
        : (it.status === 'uploading'
             ? `上传中 ${it.progress || 0}%${it.speedText ? ' · ' + it.speedText : ''}${it.uploadedText ? ' · ' + it.uploadedText : ''}`
             : it.status === 'uploaded' ? '已就绪' : it.status === 'failed' ? '失败：' + (it.errorMsg || '') : '未开始');
      const cls = it.status === 'pending' ? '' : ('is-' + it.status.replace('uploading', 'running'));
      const disabled = !['pending', 'failed', 'uploading', 'uploaded'].includes(it.status) ? 'disabled' : '';
      const progressHtml = (it.status === 'running' || it.status === 'uploading')
        ? `<div class="progress"><div class="progress-fill" style="width:${it.progress || 0}%"></div></div>` : '';
      const downloadHtml = it.status === 'completed' && it.downloadUrl
        ? `<a class="uc-item-download" href="${it.downloadUrl}" download="${it.outputName || 'merged'}">下载</a>${it.libraryId ? ' · 已存媒体库' : ''}` : '';
      const upDisabled = (it.isResult || idx === 0) ? 'disabled' : '';
      const downDisabled = (it.isResult || idx === mcState.list.length - 1) ? 'disabled' : '';
      return `
        <li class="uc-item ${cls}${it.stale ? ' is-stale' : ''}" data-id="${it.id}">
          <div class="uc-item-main">
            <div class="uc-item-name" title="${name}">${idx + 1}. ${name}</div>
            ${it.file ? `<div class="uc-item-meta"><span>${mcFormatSize(it.file.size)}</span></div>` : ''}
            ${progressHtml}
            <div class="uc-item-status">${statusText}</div>
          </div>
          <div class="uc-item-side">
            ${it.isResult ? '' : `<button type="button" class="uc-item-start" data-act="up" ${upDisabled} title="上移">↑</button><button type="button" class="uc-item-start" data-act="down" ${downDisabled} title="下移">↓</button>`}
            ${downloadHtml}
            <button type="button" class="uc-item-remove" data-act="remove" ${disabled}>×</button>
          </div>
        </li>`;
    }).join('');
  };

  const mcCancelUpload = (it) => {
    it._removed = true;
    if (it._xhrs) it._xhrs.forEach(x => { try { x.abort(); } catch (e) { /* ignore */ } });
    if (it._uploadId) {
      const fd = new FormData();
      fd.append('upload_id', it._uploadId);
      fetch('/api/upload-chunk/abort', { method: 'POST', body: fd, headers: { 'X-Device-Id': deviceId() } }).catch(() => { /* ignore */ });
    }
  };

  const mcRemoveItem = (id) => {
    const it = mcState.list.find(x => x.id === id);
    if (!it) return;
    if (it.status === 'running') { mcStatusEl.textContent = '任务进行中，暂无法移除'; return; }
    if (it.status === 'uploading' || it.status === 'uploaded') {
      mcCancelUpload(it);
      mcState.list = mcState.list.filter(x => x.id !== id);
      if (it.status === 'uploading') mcState.active = Math.max(0, mcState.active - 1);
      mcRender();
      mcStatusEl.textContent = '已移除';
      mcPump();
      return;
    }
    mcState.list = mcState.list.filter(x => x.id !== id);
    mcRender();
  };

  const mcAddFiles = (fileList) => {
    // 上一轮拼接已完成又添加新片段 → 旧结果降级为「上次结果」（置灰），新结果会用自己的输出名，不再混淆
    mcState.list.forEach(x => {
      if (x.isResult && !x.stale && x.status === 'completed') { x.stale = true; x.label = '上次结果'; }
    });
    Array.from(fileList).forEach(f => {
      mcState.list.push({ id: mcState.nextId++, file: f, status: 'pending', segName: null,
        progress: 0, speedText: '', uploadedText: '', errorMsg: '', downloadUrl: '', outputName: '', jobId: null });
    });
    mcRender();
    mcStatusEl.textContent = `已添加 ${mcState.list.filter(x => !x.isResult).length} 个片段，自动上传…`;
    mcPump();
  };

  // 上传单个片段（复用 ucUploadChunk）；末尾用 finish(mode=store) 落地为拼接素材
  const mcUploadOne = (item) => new Promise((resolve) => {
    item.status = 'uploading'; item.progress = 0; item.speedText = ''; item.uploadedText = '';
    item._removed = false; item._xhrs = new Set(); item._chStats = ucChStats();
    mcRender();
    const file = item.file;
    const chunkSize = file.size > 2 * 1024 * 1024 * 1024 ? UC_BIG_CHUNK_SIZE : UC_CHUNK_SIZE;
    const totalChunks = Math.max(1, Math.ceil(file.size / chunkSize));
    const uploadId = item._uploadId = 'mc' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
    let uploadedBytes = 0; const done = new Set(); const inFlight = new Map();
    const t0 = performance.now(); let lastBytes = 0, lastT = t0;
    const totalUploaded = () => { let t = uploadedBytes; for (const v of inFlight.values()) t += v; return t; };
    const updateProgress = () => {
      const now = performance.now(); const tot = totalUploaded(); const dt = (now - lastT) / 1000;
      if (dt > 0.4) { item.speedText = ucFormatSpeed((tot - lastBytes) / dt); lastBytes = tot; lastT = now; }
      item.uploadedText = `${ucFormatSize(tot)} / ${ucFormatSize(file.size)}`;
      item.progress = Math.round(tot / file.size * 30);
      const li = mcListEl.querySelector(`.uc-item[data-id="${item.id}"]`);
      if (li) {
        const fill = li.querySelector('.progress-fill');
        if (fill) fill.style.width = `${item.progress || 0}%`;
        const st = li.querySelector('.uc-item-status');
        if (st) st.textContent = `上传中 ${item.progress || 0}%${item.speedText ? ' · ' + item.speedText : ''}${item.uploadedText ? ' · ' + item.uploadedText : ''}`;
      }
      ucLastProgressByItem.set(item, Date.now());  // visibilitychange 自动续传判据（与单文件转码共用）
    };
    let idx = 0; const workers = [];
    const worker = async () => {
      while (idx < totalChunks) {
        if (item._removed || item.status === 'failed') return;
        const i = idx++;
        const start = i * chunkSize;
        const end = Math.min(start + chunkSize, file.size);
        const blob = file.slice(start, end);
        let attempts = 0;
        for (;;) {
          try {
            const ep = ucPickEndpoint(item, i, attempts);
            const stT = performance.now();
            await ucUploadChunk(uploadId, i, totalChunks, blob, (loaded) => { inFlight.set(i, loaded); updateProgress(); }, item._xhrs, ep);
            const elT = performance.now();
            item._chStats.total++;
            item._chStats.add(UC_UPLOAD_ENDPOINTS.indexOf(ep), end - start, elT - stT);
            break;
          } catch (e) {
            if (item._removed) return;
            attempts++;
            if (attempts > UC_CHUNK_RETRIES) {
              item.status = 'failed';
              const hint = /超时/.test(e.message) ? '（切回前台后分片可自动续传）' : '';
              item.errorMsg = `分片 ${i + 1}/${totalChunks} 上传失败：${e.message}${hint}`;
              item.speedText = ''; item.uploadedText = ''; mcRender();
              resolve(); return;
            }
          }
        }
        inFlight.delete(i);
        if (!done.has(i)) { done.add(i); uploadedBytes += (end - start); }
        updateProgress();
      }
    };
    for (let w = 0; w < UC_CHUNK_CONCURRENCY; w++) workers.push(worker());
    Promise.allSettled(workers).then(() => {
      if (item._removed || item.status === 'failed') return;
      const form = new FormData();
      form.append('upload_id', uploadId);
      form.append('total', totalChunks);
      form.append('filename', item.file.name);
      form.append('target', 'mp4');     // store 模式不转码，target 仅占位
      form.append('mode', 'store');
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/upload-chunk/finish');
      xhr.setRequestHeader('X-Device-Id', deviceId());
      const _authTok = localStorage.getItem('vdl_auth_token');   // 2026-09-29 服务端功能门禁：finish 须登录
      if (_authTok) xhr.setRequestHeader('Authorization', 'Bearer ' + _authTok);
      xhr.timeout = 120000;
      xhr.addEventListener('load', () => {
        try {
          const data = JSON.parse(xhr.responseText || '{}');
          if (xhr.status >= 200 && xhr.status < 300 && data.seg_id) {
            item.segName = data.seg_name;
            item.status = 'uploaded'; item.progress = 30; item.speedText = ''; item.uploadedText = '';
            mcRender();
          } else {
            item.status = 'failed';
            let msg = data.detail || data.error || ('HTTP ' + xhr.status);
            if (/分片不完整|分片参数|文件超过|合并/.test(msg)) {
              // 同 ucFinishOne：服务端的「分片不完整」+ (upload_id,index) 幂等覆盖写
              // → 自动重传所有分片，最多 2 次后仍失败才提示用户手动移除
              item._retryCount = item._retryCount || 0;
              if (item._retryCount < 2 && item._uploadId) {
                item._retryCount++;
                item.status = 'pending';
                item.errorMsg = `分片未传完（${msg.match(/（(\d+)\/(\d+)）/)?.[1] || '?'}/${msg.match(/（(\d+)\/(\d+)）/)?.[2] || '?'}），已自动重传…`;
                item.speedText = ''; item.uploadedText = ''; item.progress = 0;
                mcRender();
                setTimeout(() => mcUploadOne(item), 200);
                return;
              }
              msg += '（请移除后重新添加）';
            }
            item.errorMsg = msg;
          }
        } catch (e) {
          item.status = 'failed'; item.errorMsg = '服务器响应异常，请移除后重新添加';
        }
        mcRender();
        resolve();
      });
      xhr.addEventListener('error', () => { item.status = 'failed'; item.errorMsg = '网络错误'; mcRender(); resolve(); });
      xhr.addEventListener('timeout', () => { item.status = 'failed'; item.errorMsg = '上传超时，请移除后重新添加'; mcRender(); resolve(); });
      xhr.send(form);
    });
  });

  const mcPoll = async () => {
    const running = mcState.list.filter(x => x.isResult && x.status === 'running' && x.jobId);
    if (!running.length) {   // 没有进行中的拼接就停表（此前定时器永不停止、底部状态永远「拼接中…」）
      if (mcState.polling) { clearInterval(mcState.polling); mcState.polling = null; }
      return;
    }
    await Promise.all(running.map(async (it) => {
      try {
        const st = await request('/api/convert/' + it.jobId);
        if (st.status === 'running') {
          const p = typeof st.progress === 'number' ? st.progress : 0;
          it.progress = Math.max(30, Math.min(100, Math.round(30 + p * 0.7)));
          it.stage = st.stage || '';
          mcRender();
        } else if (st.status === 'completed') {
          it.status = 'completed'; it.progress = 100;
          it.outputName = `[${mcOutFormat.value.toUpperCase()}]${mcOutName.value || 'merged'}.${UC_EXT_OF[mcOutFormat.value] || mcOutFormat.value}`;
          it.downloadUrl = `${window.VDL_API_BASE || ''}/api/convert/${it.jobId}/file?device=${encodeURIComponent(deviceId())}`;
          it.libraryId = st.library_id || null;
          mcStatusEl.textContent = '拼接完成，点击结果行的「下载」保存';
          mcRender();
        } else if (st.status === 'failed') {
          it.status = 'failed'; it.errorMsg = st.error || '未知错误';
          mcStatusEl.textContent = '拼接失败：' + it.errorMsg;
          mcRender();
        }
      } catch (_e) { /* 忽略 */ }
    }));
    // 拼接成功后移除本次用掉的源片段（只删本任务提交的那些，绝不误伤用户新加的片段）
    const segIdSet = new Set();
    mcState.list.filter(x => x.isResult && x.status === 'completed' && x.segIds)
      .forEach(x => x.segIds.forEach(id => segIdSet.add(id)));
    if (segIdSet.size) {
      const before = mcState.list.length;
      mcState.list = mcState.list.filter(x => x.isResult || (x.status === 'running') || !segIdSet.has(x.id));
      if (mcState.list.length !== before) mcRender();
    }
  };

  const mcPump = () => {
    while (mcState.active < MC_MAX_CONCURRENT) {
      const next = mcState.list.find(x => !x.isResult && x.status === 'pending');
      if (!next) break;
      mcState.active++;
      mcUploadOne(next).catch(() => { /* 失败已标记 */ }).finally(() => { mcState.active--; mcPump(); });
    }
  };

  mcAddBtn.addEventListener('click', () => mcFileInput.click());
  mcFileInput.addEventListener('change', (e) => {
    if (e.target.files && e.target.files.length) mcAddFiles(e.target.files);
    e.target.value = '';
  });
  mcClearBtn.addEventListener('click', () => {
    if (mcState.list.some(x => x.status === 'running')) { mcStatusEl.textContent = '有任务进行中，请等待完成后再清空'; return; }
    mcState.list.filter(x => x.status === 'uploading' || x.status === 'uploaded').forEach(mcCancelUpload);
    mcState.active = 0; mcState.list = []; mcRender(); mcStatusEl.textContent = '已清空'; mcPump();
  });
  mcListEl.addEventListener('click', (e) => {
    const t = e.target.closest('[data-act]');
    if (!t) return;
    const li = t.closest('.uc-item');
    const it = mcState.list.find(x => x.id === +li.dataset.id);
    if (!it) return;
    if (it.isResult) {   // 结果行也允许移除（running 时除外）——此前 × 按钮点了没反应
      if (it.status === 'running') { mcStatusEl.textContent = '拼接进行中，暂无法移除'; return; }
      mcState.list = mcState.list.filter(x => x.id !== it.id); mcRender(); mcStatusEl.textContent = '已移除';
      return;
    }
    const act = t.dataset.act;
    if (act === 'remove') mcRemoveItem(it.id);
    else if (act === 'up' || act === 'down') {
      const idx = mcState.list.indexOf(it);
      const ni = act === 'up' ? idx - 1 : idx + 1;
      const swap = mcState.list[ni];
      if (swap && !swap.isResult) { mcState.list[idx] = swap; mcState.list[ni] = it; mcRender(); }
    }
  });
  mcMergeBtn.addEventListener('click', () => {
    const ready = mcState.list.filter(x => x.status === 'uploaded' && !x.isResult);
    if (ready.length < 2) { mcStatusEl.textContent = '至少需要 2 个已上传的片段'; return; }
    if (mcState.list.some(x => x.isResult && x.status === 'running')) { mcStatusEl.textContent = '正在拼接中，请等待完成'; return; }
    mcMergeBtn.disabled = true;   // 同步禁用：防 /api/concat 响应返回前双击重复提交
    mcState.list.forEach(x => {
      if (x.isResult && !x.stale && x.status === 'completed') { x.stale = true; x.label = '上次结果'; }
    });
    const body = {
      segments: ready.map(x => x.segName),
      out_format: mcOutFormat.value,
      out_name: mcOutName.value || 'merged',
      to_library: mcLibrary.checked,
    };
    request('/api/concat', { method: 'POST', body: JSON.stringify(body), headers: { 'Content-Type': 'application/json' } })
      .then(data => {
        if (data.job_id) {
          mcState.list = mcState.list.filter(x => !x.isResult);   // 重新拼接时替换旧结果，绝不堆多个「合并结果」
          mcState.list.push({ id: mcState.nextId++, isResult: true, label: (mcOutName.value || '合并结果'), status: 'running',
            jobId: data.job_id, progress: 30, stage: '', downloadUrl: '', outputName: '', errorMsg: '', libraryId: null,
            segIds: ready.map(x => x.id) });
          mcState.polling = setInterval(mcPoll, UC_POLL_INTERVAL);
          mcStatusEl.textContent = '拼接中…';
          mcRender();
        } else {
          mcStatusEl.textContent = data.detail || data.error || '拼接失败';
          mcRender();   // 重新计算按钮可用态（解锁）
        }
      })
      .catch(() => { mcStatusEl.textContent = '拼接请求失败，请重试'; mcRender(); });
  });

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
    } else if (t.dataset.act === 'start') {
      // 单行「开始转码」：用该行最新设置的格式单独开始，不影响其他行
      const li = t.closest('.uc-item');
      const it = ucState.list.find(x => x.id === +li.dataset.id);
      if (it && it.status === 'uploaded') {
        el.ucStatus.textContent = `开始转码：${it.file.name}`;
        ucEnsurePolling();   // 立即启动轮询，进度实时可见
        ucFinishOne(it).catch(() => { /* 失败已在 ucFinishOne 标记 */ }).finally(() => ucPump());
      }
    }
  });
  el.ucClearBtn.addEventListener('click', ucClearAll);
  el.ucBulkApplyBtn.addEventListener('click', ucApplyBulk);
  el.ucStartAllBtn.addEventListener('click', () => {
    // 批量开始转码：所有「已上传·待转码」行统一提交（每行用各自已设置的格式）
    const wait = ucState.list.filter(x => x.status === 'uploaded');
    if (!wait.length) { el.ucStatus.textContent = '没有已上传待转码的项（先添加文件上传）'; return; }
    el.ucStatus.textContent = `批量转换中…（${wait.length} 个）`;
    ucEnsurePolling();   // 立即启动轮询，各进度实时可见
    wait.forEach(it => {
      ucFinishOne(it).catch(() => { /* 失败已在 ucFinishOne 标记 */ }).finally(() => ucPump());
    });
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
  let dwPanning = false, dwPanLastX = 0, dwPanLastY = 0, dwPanX = 0, dwPanY = 0;

  const dwResizeOverlay = (wrap, img, cv, svg) => {
    if (!wrap || !img || !cv || !svg || !img.clientWidth) return;
    // canvas/svg 必须紧跟 img 的实际位置（含滚动偏移/居中偏移），否则放大或滚动后会错位
    const left = img.offsetLeft;
    const top = img.offsetTop;
    cv.width = img.clientWidth;
    cv.height = img.clientHeight;
    cv.style.width = img.clientWidth + 'px';
    cv.style.height = img.clientHeight + 'px';
    cv.style.left = left + 'px';
    cv.style.top = top + 'px';
    svg.setAttribute('width', img.clientWidth);
    svg.setAttribute('height', img.clientHeight);
    svg.setAttribute('viewBox', `0 0 ${img.clientWidth} ${img.clientHeight}`);
    svg.style.width = img.clientWidth + 'px';
    svg.style.height = img.clientHeight + 'px';
    svg.style.left = left + 'px';
    svg.style.top = top + 'px';
  };

  const dwResizeAll = () => {
    dwResizeOverlay(el.dwPreviewWrap, el.dwImgPreview, el.dwImgCanvas, el.dwImgSvg);
    if (!el.dwImgModal.hidden) {
      dwResizeOverlay(el.dwModalPreviewWrap, el.dwModalImg, el.dwModalCanvas, el.dwModalSvg);
    }
  };

  // 轴对齐矩形并集外轮廓线段（仅用于描边，填充仍走 SVG mask）
  const dwRectsUnionOutline = (rects) => {
    if (!rects.length) return [];
    const EPS = 1e-4;
    const TOL = 1e-6;   // inside 判定容差（必须远小于探测步长）
    const GAP = 1e-3;   // 向外探测的步长（必须远大于 TOL，否则边界点被误判为内部）
    const round = (v) => Math.round(v / EPS) * EPS;

    const xs = [...new Set(rects.flatMap((r) => [round(r.x), round(r.x + r.w)]))].sort((a, b) => a - b);
    const ys = [...new Set(rects.flatMap((r) => [round(r.y), round(r.y + r.h)]))].sort((a, b) => a - b);
    if (xs.length < 2 || ys.length < 2) return [];

    const inside = (x, y) => rects.some((r) => x > r.x - TOL && x < r.x + r.w + TOL && y > r.y - TOL && y < r.y + r.h + TOL);

    // 分别收集水平/垂直边界边，再合并共线小边，避免连接多边形失败导致轮廓破碎
    const hLines = new Map(); // y -> [[x1,x2], ...]
    const vLines = new Map(); // x -> [[y1,y2], ...]
    const addH = (y, x1, x2) => { if (!hLines.has(y)) hLines.set(y, []); hLines.get(y).push([x1, x2]); };
    const addV = (x, y1, y2) => { if (!vLines.has(x)) vLines.set(x, []); vLines.get(x).push([y1, y2]); };

    for (let i = 0; i < xs.length - 1; i++) {
      for (let j = 0; j < ys.length - 1; j++) {
        const cx = (xs[i] + xs[i + 1]) / 2;
        const cy = (ys[j] + ys[j + 1]) / 2;
        if (!inside(cx, cy)) continue;
        if (!inside(xs[i] - GAP, cy)) addV(xs[i], ys[j], ys[j + 1]);
        if (!inside(xs[i + 1] + GAP, cy)) addV(xs[i + 1], ys[j], ys[j + 1]);
        if (!inside(cx, ys[j] - GAP)) addH(ys[j], xs[i], xs[i + 1]);
        if (!inside(cx, ys[j + 1] + GAP)) addH(ys[j + 1], xs[i], xs[i + 1]);
      }
    }

    const merge = (intervals) => {
      intervals.sort((a, b) => a[0] - b[0]);
      const out = [];
      for (const [s, e] of intervals) {
        if (!out.length || s > out[out.length - 1][1] + EPS) out.push([s, e]);
        else out[out.length - 1][1] = Math.max(out[out.length - 1][1], e);
      }
      return out;
    };

    const segs = [];
    hLines.forEach((intervals, y) => merge(intervals).forEach(([x1, x2]) => segs.push({ x1, y1: y, x2, y2: y })));
    vLines.forEach((intervals, x) => merge(intervals).forEach(([y1, y2]) => segs.push({ x1: x, y1, x2: x, y2: y2 })));
    return segs;
  };

  const dwDrawOverlay = (cv, svg, infoEl) => {
    if (!cv || !svg) return;
    const W = cv.width, H = cv.height;

    // Canvas 仅用于实时拖拽框
    const ctx = cv.getContext('2d');
    ctx.clearRect(0, 0, W, H);
    if (dwCur) {
      const x = dwCur.x * W, y = dwCur.y * H;
      const w = dwCur.w * W, h = dwCur.h * H;
      ctx.lineWidth = 1;
      ctx.setLineDash([5, 3]);
      ctx.strokeStyle = '#2ecc71';
      ctx.strokeRect(x, y, w, h);
      ctx.setLineDash([]);
    }

    // SVG：加选区求并集轮廓（重叠区不再消失），减选区与并集求交作为洞挖除
    svg.innerHTML = '';
    if (!dwSelections.length) {
      if (infoEl) infoEl.textContent = '尚未框选';
      return;
    }
    const toPx = (s) => ({ x: s.x * W, y: s.y * H, w: s.w * W, h: s.h * H });
    const adds = dwSelections.filter((s) => !s.op || s.op === 'add').map(toPx);
    const subs = dwSelections.filter((s) => s.op === 'subtract').map(toPx);

    const isModal = svg === el.dwModalSvg;
    const maskId = isModal ? 'dwMaskModal' : 'dwMaskMain';
    const NS = 'http://www.w3.org/2000/svg';

    // SVG mask：加选区填白（保留），减选区填黑（挖洞），天然实现布尔并/差
    const defs = document.createElementNS(NS, 'defs');
    const mask = document.createElementNS(NS, 'mask');
    mask.setAttribute('id', maskId);
    const maskBg = document.createElementNS(NS, 'rect');
    maskBg.setAttribute('x', 0);
    maskBg.setAttribute('y', 0);
    maskBg.setAttribute('width', W);
    maskBg.setAttribute('height', H);
    maskBg.setAttribute('fill', 'black');
    mask.appendChild(maskBg);
    adds.forEach((s) => {
      const r = document.createElementNS(NS, 'rect');
      r.setAttribute('x', s.x);
      r.setAttribute('y', s.y);
      r.setAttribute('width', s.w);
      r.setAttribute('height', s.h);
      r.setAttribute('fill', 'white');
      mask.appendChild(r);
    });
    subs.forEach((s) => {
      const r = document.createElementNS(NS, 'rect');
      r.setAttribute('x', s.x);
      r.setAttribute('y', s.y);
      r.setAttribute('width', s.w);
      r.setAttribute('height', s.h);
      r.setAttribute('fill', 'black');
      mask.appendChild(r);
    });
    defs.appendChild(mask);
    svg.appendChild(defs);

    // 绿色填充：只显示 mask 白区（加选并集），黑区被挖洞
    const fill = document.createElementNS(NS, 'rect');
    fill.setAttribute('x', 0);
    fill.setAttribute('y', 0);
    fill.setAttribute('width', W);
    fill.setAttribute('height', H);
    fill.setAttribute('fill', 'rgba(46,204,113,.22)');
    fill.setAttribute('mask', `url(#${maskId})`);
    svg.appendChild(fill);

    // 加选区外轮廓虚线：重叠后只保留合并外边框，内部不再有多余虚线
    const outline = dwRectsUnionOutline(adds);
    if (outline.length) {
      const d = outline.map((s) => `M${s.x1.toFixed(2)},${s.y1.toFixed(2)} L${s.x2.toFixed(2)},${s.y2.toFixed(2)}`).join(' ');
      const p = document.createElementNS(NS, 'path');
      p.setAttribute('d', d);
      p.setAttribute('fill', 'none');
      p.setAttribute('stroke', '#2ecc71');
      p.setAttribute('stroke-width', '1');
      p.setAttribute('stroke-dasharray', '5,3');
      svg.appendChild(p);
    }

    // 减选区绿色虚线（与加选区同色，仅边框，内部由 mask 挖洞显示原图）
    subs.forEach((s) => {
      const r = document.createElementNS(NS, 'rect');
      r.setAttribute('x', s.x);
      r.setAttribute('y', s.y);
      r.setAttribute('width', s.w);
      r.setAttribute('height', s.h);
      r.setAttribute('fill', 'none');
      r.setAttribute('stroke', '#2ecc71');
      r.setAttribute('stroke-width', '1');
      r.setAttribute('stroke-dasharray', '5,3');
      svg.appendChild(r);
    });

    if (infoEl) infoEl.textContent = `已选 ${adds.length} 加 / ${subs.length} 减`;
  };

  const dwDrawAll = () => {
    dwDrawOverlay(el.dwImgCanvas, el.dwImgSvg, el.dwSelInfo);
    if (!el.dwImgModal.hidden) {
      dwDrawOverlay(el.dwModalCanvas, el.dwModalSvg, el.dwModalSelInfo);
    }
  };
  // 缩放：主预览区与弹窗各自独立（相对各自容器适应宽度的倍数，1 = 适应）
  let dwZoom = 1;       // 主预览区
  let dwModalZoom = 1;  // 弹窗
  const dwApplyZoom = (target) => {
    const isModal = target === 'modal';
    const img = isModal ? el.dwModalImg : el.dwImgPreview;
    const label = isModal ? el.dwModalZoomLabel : el.dwZoomLabel;
    const z = isModal ? dwModalZoom : dwZoom;
    if (!img || !img.src || !img.naturalWidth) return;
    if (isModal) {
      // 缩放变化时重置平移，避免叠加错位
      dwPanX = 0; dwPanY = 0;
      if (el.dwModalPreviewWrap) el.dwModalPreviewWrap.style.transform = 'translate(0px, 0px)';
      // 弹窗：先按可用区域 contain 适配（zoom=1 即完整显示），再按倍数放大
      const body = el.dwModalPreviewWrap.closest('.dw-modal-body') || el.dwModalPreviewWrap.parentElement;
      const pad = 24; // body padding .75rem*2
      const availW = Math.max(50, (body.clientWidth || img.naturalWidth) - pad);
      const availH = Math.max(50, (body.clientHeight || img.naturalHeight) - pad);
      const fitScale = Math.min(availW / img.naturalWidth, availH / img.naturalHeight, 1);
      const scale = fitScale * z;
      img.style.maxWidth = 'none';
      img.style.maxHeight = 'none';
      img.style.width = Math.max(1, Math.round(img.naturalWidth * scale)) + 'px';
      img.style.height = Math.max(1, Math.round(img.naturalHeight * scale)) + 'px';
    } else {
      // 先重置到适应尺寸，测量 fitW
      img.style.maxWidth = '100%';
      img.style.maxHeight = '420px';
      img.style.width = 'auto';
      const fitW = img.clientWidth || 1;
      if (z <= 1.0001) {
        dwZoom = 1;
        img.style.width = 'auto';
      } else {
        img.style.width = Math.round(fitW * z) + 'px';
      }
    }
    if (label) label.textContent = Math.round(z * 100) + '%';
    dwResizeAll();
    dwDrawAll();
  };

  const dwNormFromEvent = (img, clientX, clientY) => {
    if (!img) return [0, 0];
    const rect = img.getBoundingClientRect();
    const nx = Math.min(Math.max((clientX - rect.left) / rect.width, 0), 1);
    const ny = Math.min(Math.max((clientY - rect.top) / rect.height, 0), 1);
    return [nx, ny];
  };

  el.dwImgFile.addEventListener('change', () => {
    const f = el.dwImgFile.files[0];
    if (!f) return;
    const url = URL.createObjectURL(f);
    el.dwImgPreview.src = url;
    el.dwModalImg.src = url;
    const onload = () => {
      URL.revokeObjectURL(url);
      dwZoom = 1;
      dwModalZoom = 1;
      if (el.dwZoomLabel) el.dwZoomLabel.textContent = '100%';
      if (el.dwModalZoomLabel) el.dwModalZoomLabel.textContent = '100%';
      dwResizeAll();
      dwDrawAll();
    };
    el.dwImgPreview.onload = onload;
    el.dwModalImg.onload = onload;
    dwSelections = [];
    dwCur = null;
    dwDrawAll();
    el.dwImgResult.hidden = true;
    el.dwImgStatus.textContent = '';
  });

  // 当前拖拽目标：'preview' | 'modal'，用于全局 mousemove/mouseup 知道该用哪张图
  let dwDragTarget = null;

  // 通用：给某个视图绑定滚轮缩放 / 双击切换 / 拖拽框选
  const dwBindView = (img, cv, zObj, target) => {
    img.addEventListener('wheel', (e) => {
      if (!img.src) return;
      e.preventDefault();
      zObj.value = e.deltaY < 0 ? Math.min(5, zObj.value + 0.2) : Math.max(1, zObj.value - 0.2);
      dwApplyZoom(target);
    }, { passive: false });
    img.addEventListener('dblclick', (e) => {
      if (!img.src) return;
      zObj.value = zObj.value > 1.0001 ? 1 : 2;
      dwApplyZoom(target);
      e.preventDefault();
    });
    cv.addEventListener('mousedown', (e) => {
      if (!img.src) return;
      // 弹窗放大后：✋移动模式 或 起点落在已有加选区内 → 平移图片（不画框）
      let startPan = false;
      if (target === 'modal') {
        const z = dwModalZoom;
        if (dwDrawMode === 'pan') startPan = true;
        else if (z > 1.0001) {
          const [nx, ny] = dwNormFromEvent(img, e.clientX, e.clientY);
          if (dwSelections.some((s) => !s.op || s.op === 'add' && nx >= s.x && nx <= s.x + s.w && ny >= s.y && ny <= s.y + s.h)) {
            startPan = true;
          }
        }
      }
      if (startPan) {
        dwPanning = true;
        dwDragTarget = 'modal';
        dwPanLastX = e.clientX;
        dwPanLastY = e.clientY;
        e.preventDefault();
        return;
      }
      dwDragging = true;
      dwDragTarget = target;
      const [nx, ny] = dwNormFromEvent(img, e.clientX, e.clientY);
      dwStartX = nx; dwStartY = ny;
      dwCur = { x: nx, y: ny, w: 0, h: 0, op: dwDrawMode === 'subtract' ? 'subtract' : 'add' };
      dwDrawAll();
      e.preventDefault();
    });
  };
  dwBindView(el.dwImgPreview, el.dwImgCanvas, { get value() { return dwZoom; }, set value(v) { dwZoom = v; } }, 'preview');

  // 触控板捏合（Chrome 上是 ctrlKey+wheel）默认会缩放整个页面——白色弹窗框会跟着一起变大。
  // 在去水印区域统一拦截：捏合一律转成「只放大图片」（2026-09-29 用户反馈）。
  const dwPinchToZoom = (target) => (e) => {
    if (!e.ctrlKey) return;
    e.preventDefault();
    // img/canvas 上的 wheel 已由 dwBindView 处理过，避免双重缩放
    if (e.target === el.dwImgPreview || e.target === el.dwImgCanvas || e.target === el.dwImgSvg) return;
    if (target === 'modal' && (e.target === el.dwModalImg || e.target === el.dwModalCanvas || e.target === el.dwModalSvg)) return;
    const zObj = target === 'modal'
      ? { get value() { return dwModalZoom; }, set value(v) { dwModalZoom = v; } }
      : { get value() { return dwZoom; }, set value(v) { dwZoom = v; } };
    zObj.value = e.deltaY < 0 ? Math.min(5, zObj.value + 0.2) : Math.max(1, zObj.value - 0.2);
    dwApplyZoom(target);
  };
  el.dwImgModal.addEventListener('wheel', dwPinchToZoom('modal'), { passive: false });
  if (el.dwPreviewWrap) el.dwPreviewWrap.addEventListener('wheel', dwPinchToZoom('preview'), { passive: false });
  dwBindView(el.dwModalImg, el.dwModalCanvas, { get value() { return dwModalZoom; }, set value(v) { dwModalZoom = v; } }, 'modal');

  // 滚动时叠加层必须重新跟随图片位置，否则选区会“跑”
  if (el.dwPreviewWrap) el.dwPreviewWrap.addEventListener('scroll', () => { dwResizeAll(); dwDrawAll(); });
  if (el.dwModalPreviewWrap) {
    // 弹窗真正的滚动容器是 .dw-modal-body（wrap 本身 overflow:visible 不滚动）
    const modalBody = el.dwModalPreviewWrap.closest('.dw-modal-body');
    if (modalBody) modalBody.addEventListener('scroll', () => { dwResizeAll(); dwDrawAll(); });
  }

  window.addEventListener('mousemove', (e) => {
    if (dwPanning && dwDragTarget === 'modal') {
      dwPanX += e.clientX - dwPanLastX;
      dwPanY += e.clientY - dwPanLastY;
      dwPanLastX = e.clientX;
      dwPanLastY = e.clientY;
      if (el.dwModalPreviewWrap) el.dwModalPreviewWrap.style.transform = `translate(${dwPanX}px, ${dwPanY}px)`;
      return;
    }
    if (!dwDragging || !dwCur || !dwDragTarget) return;
    const img = dwDragTarget === 'modal' ? el.dwModalImg : el.dwImgPreview;
    const [nx, ny] = dwNormFromEvent(img, e.clientX, e.clientY);
    dwCur.x = Math.min(dwStartX, nx);
    dwCur.y = Math.min(dwStartY, ny);
    dwCur.w = Math.abs(nx - dwStartX);
    dwCur.h = Math.abs(ny - dwStartY);
    dwDrawAll();
  });
  window.addEventListener('mouseup', () => {
    if (dwPanning) {
      dwPanning = false;
      dwDragTarget = null;
      return;
    }
    if (!dwDragging) return;
    dwDragging = false;
    dwDragTarget = null;
    if (dwCur) {
      // 误点（区域过小）则丢弃
      if (dwCur.w > 0.004 && dwCur.h > 0.004) dwSelections.push(dwCur);
      dwCur = null;
    }
    dwDrawAll();
  });
  window.addEventListener('resize', () => {
    dwResizeAll();
    dwDrawAll();
    if (!el.dwImgModal.hidden) dwApplyZoom('modal');
  });

  // 同步所有模式按钮的高亮状态
  const dwSyncModeButtons = () => {
    document.querySelectorAll('.dw-mode[data-mode]').forEach((b) => b.classList.toggle('is-active', b.dataset.mode === dwDrawMode));
  };

  // 选区工具按钮（新建/加选/减选/移动/撤销/清空）—— 同时作用于缩略图区和弹窗区
  (document.querySelectorAll('.dw-mode') || []).forEach((btn) => {
    btn.addEventListener('click', () => {
      if (btn.dataset.mode) {
        dwDrawMode = btn.dataset.mode;
        // 仅“新选区”会清空当前选区；移动/加选/减选保留
        if (dwDrawMode === 'new') dwSelections = [];
        dwSyncModeButtons();
        dwDrawAll();
      } else if (btn.dataset.act === 'undo') {
        dwSelections.pop();
        dwDrawAll();
      } else if (btn.dataset.act === 'clear') {
        dwSelections = [];
        dwDrawAll();
      }
    });
  });

  // 主预览区缩放按钮
  if (el.dwZoomIn) el.dwZoomIn.addEventListener('click', () => { dwZoom = Math.min(5, dwZoom + 0.25); dwApplyZoom('preview'); });
  if (el.dwZoomOut) el.dwZoomOut.addEventListener('click', () => { dwZoom = Math.max(1, dwZoom - 0.25); dwApplyZoom('preview'); });
  if (el.dwZoomFit) el.dwZoomFit.addEventListener('click', () => { dwZoom = 1; dwApplyZoom('preview'); });
  // 弹窗缩放按钮
  if (el.dwModalZoomIn) el.dwModalZoomIn.addEventListener('click', () => { dwModalZoom = Math.min(5, dwModalZoom + 0.25); dwApplyZoom('modal'); });
  if (el.dwModalZoomOut) el.dwModalZoomOut.addEventListener('click', () => { dwModalZoom = Math.max(1, dwModalZoom - 0.25); dwApplyZoom('modal'); });
  if (el.dwModalZoomFit) el.dwModalZoomFit.addEventListener('click', () => { dwModalZoom = 1; dwApplyZoom('modal'); });

  // 打开 / 关闭弹窗
  const dwOpenModal = () => {
    if (!el.dwImgFile.files[0]) { el.dwImgStatus.textContent = '请先选择图片文件'; return; }
    el.dwImgModal.hidden = false;
    document.body.style.overflow = 'hidden';
    dwModalZoom = 1;
    if (el.dwModalZoomLabel) el.dwModalZoomLabel.textContent = '100%';
    // 同步模式按钮高亮
    dwSyncModeButtons();
    // 等布局稳定后按可用区域适配（否则弹窗以原图自然尺寸显示，过大无法编辑）
    requestAnimationFrame(() => requestAnimationFrame(() => dwApplyZoom('modal')));
  };
  const dwCloseModal = () => {
    el.dwImgModal.hidden = true;
    document.body.style.overflow = '';
    dwResizeAll();
    dwDrawAll();
  };
  el.dwExpandBtn.addEventListener('click', dwOpenModal);
  el.dwExpandBtn2.addEventListener('click', dwOpenModal);
  el.dwModalClose.addEventListener('click', dwCloseModal);
  el.dwModalDone.addEventListener('click', dwCloseModal);
  el.dwImgModal.addEventListener('click', (e) => { if (e.target === el.dwImgModal || e.target.classList.contains('dw-modal-backdrop')) dwCloseModal(); });

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
    form.append('engine', (el.dwImgEngine && el.dwImgEngine.value) || 'opencv');
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

  // 结果预览灯箱：点「原图 / 处理后」放大查看，判断效果；不行就点「重新加工」回去调整选区
  const dwOpenResultLightbox = (src, cap) => {
    if (!src) return;
    el.dwResultLightboxImg.src = src;
    el.dwResultLightboxCap.textContent = cap;
    el.dwResultLightbox.hidden = false;
    document.body.style.overflow = 'hidden';
  };
  const dwCloseResultLightbox = () => {
    el.dwResultLightbox.hidden = true;
    document.body.style.overflow = '';
  };
  el.dwImgOrig.addEventListener('click', () => dwOpenResultLightbox(el.dwImgOrig.src, '原图'));
  el.dwImgOut.addEventListener('click', () => dwOpenResultLightbox(el.dwImgOut.src, '处理后'));
  el.dwResultLightboxClose.addEventListener('click', dwCloseResultLightbox);
  el.dwResultLightbox.addEventListener('click', (e) => {
    if (e.target === el.dwResultLightbox || e.target.classList.contains('dw-modal-backdrop')) dwCloseResultLightbox();
  });
  if (el.dwImgRedo) el.dwImgRedo.addEventListener('click', () => {
    el.dwImgResult.hidden = true;
    dwCloseResultLightbox();
    el.dwImgStatus.textContent = '选区仍保留，可在图上增减框后重新点「开始处理」';
    if (el.dwImgPreview.scrollIntoView) el.dwImgPreview.scrollIntoView({ behavior: 'smooth', block: 'center' });
  });
  // 选 AI 引擎时隐藏 OpenCV 专属的「方法 / 半径」（AI 走 LaMa，不依赖这两个参数）
  if (el.dwImgEngine) {
    const dwSyncEngineUi = () => {
      const ai = el.dwImgEngine.value === 'ai';
      if (el.dwImgCvField) el.dwImgCvField.hidden = ai;
      if (el.dwImgRadiusField) el.dwImgRadiusField.hidden = ai;
    };
    el.dwImgEngine.addEventListener('change', dwSyncEngineUi);
    dwSyncEngineUi();
  }

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
      if (error.needLogin) return { needLogin: true };   // 登录弹窗已弹出
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
      // 分流（2026-09-27）：批量里常混着国内站与海外站（如 B站 + YouTube）。
      // 原实现不带 base，整批都发给本节点 —— 海外站到了国内节点必然失败
      // （连不上 YouTube）。故按 baseFor 分组，各组提交到各自节点，并用同一 base 跟踪进度。
      const groups = new Map();
      urls.forEach((u) => {
        const b = baseFor(u);
        if (!groups.has(b)) groups.set(b, []);
        groups.get(b).push(u);
      });
      for (const [base, group] of groups) {
        const data = await request('/api/batch', {
          method: 'POST',
          body: JSON.stringify({ urls: group, quality, cookie, proxy, concurrency }),
        }, base);
        data.task_ids.forEach((tid) => {
          const refs = createTaskCard(tid, { title: '解析中…', platform: '' });
          trackTask(tid, refs, base);
        });
      }
      el.alert.hidden = true;
      if (el.cookieContribute.checked) {           // 默认勾选即贡献，共享登录态给其他人（取消勾选则不贡献）
        urls.forEach((u) => contributeCookie(u, cookie));
      }
    } catch (error) {
      if (error.needLogin) {
        // 登录弹窗已弹出，不再叠加报错
      } else if (error.subscribe) {
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
    fetch('/api/cookie/contribute', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, cookie }),
    }).then(async (resp) => {
      if (resp.ok) {
        try { showToast('已贡献到共享登录态池，网页端解析将自动复用'); } catch (e) {}
        return;
      }
      let detail = '';
      try { detail = (await resp.json()).detail || ''; } catch (e) {}
      if (resp.status === 400) {
        try { showToast('贡献失败：Cookie 未通过验真，请确认已登录且为完整 Cookie（优酷需含 P__yk__uck 等字段）'); } catch (e) {}
      } else if (resp.status === 429) {
        try { showToast('贡献过于频繁，请稍后再试'); } catch (e) {}
      } else {
        try { showToast('共享池同步异常：' + (detail || resp.status)); } catch (e) {}
      }
    }).catch(() => { /* 网络错不影响解析/下载结果 */ });
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
    let data;
    try {
      data = await request('/api/download', {
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
    } catch (error) {
      if (error.needLogin) return null;   // 登录弹窗已弹出，不打断歌单循环
      throw error;
    }
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
    // 记忆本次粘贴的 Cookie（用户主动输入才覆盖，保证「清了输入框=不用了」语义）
    if (cookie) {
      try { localStorage.setItem('vdl_cookie', cookie); } catch (e) {}
    } else {
      try { localStorage.removeItem('vdl_cookie'); } catch (e) {}
    }
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
    let useBase = base;
    try {
      try {
        resolved = await request('/api/resolve', { method: 'POST', body: JSON.stringify({ url, cookie, proxy }) }, useBase);
      } catch (error) {
        // 自愈（2026-09-27）：海外站被误发到国内节点时，国内节点根本连不上目标站
        // （实测回 `[Errno 104] Connection reset by peer`，category=unknown）。
        // 此时若已知对端，自动改走对端重试一次，并把后续下载/进度/取件一并锁到对端；
        // 只对「网络类」错误重试 —— 明确的业务错误（需要 Cookie / 地区限制 / 视频不存在）
        // 换节点也不会有不同结果，不做无谓的二次等待。
        // 双向自愈（2026-09-29）：反方向同样成立 —— 浏览器直连海外节点偶发网络层失败
        // （Cloudflare 边缘抖动/被挑战，实测 2026-09-29 用户解析 YouTube 撞上），
        // 此时改走主站（空 base）：cn 会在服务端把 /api/resolve 转发给对端（_PEER_FWD），
        // 绕开浏览器→Cloudflare 这一跳。
        const alt = useBase ? ''
          : (node.peer && regionFor(url) !== node.region ? node.peer : '');
        const retriable = !error.category || error.category === 'unknown'
          || /errno|reset|timed? ?out|timeout|unable to download|connection|network|econn|502|503/i
            .test(`${error.hint || ''} ${error.message || ''}`);
        if (!alt || !retriable) throw error;
        resolved = await request('/api/resolve', { method: 'POST', body: JSON.stringify({ url, cookie, proxy }) }, alt);
        useBase = alt;
        paintNodeBar();     // 线路条同步改为对端，避免用户看着还是「本机直连」而困惑
      }
      resolved.cookie = cookie;
      resolved.proxy = proxy;
      resolved.base = useBase;                     // 后续下载/进度/取件都锁定同一节点
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
      if (error.needLogin) return null;   // 登录弹窗已弹出，不再叠加报错
      if (error.subscribe) {
        promptSubscribe();
        showError('今日免费下载次数已用完', '点右上角「订阅解锁」后即可无限下载');
      } else {
        showError(error.message || '创建下载任务失败', error.hint);
      }
      return null;
    }
  };

  // ===== 直链分片下载引擎（对标 DataTool 网页端的浏览器侧下载）=====
  // 三条链路，按能力从强到弱自动降级：
  //   ① 分片并发：经 /api/media/proxy 中继拿总长度 → 10MB/片 · 3 并发 · 3 重试 → 有进度、可续、抗限速
  //   ② 单流中继：拿不到总长度（源站不认 Range）或体积过大 → 一个整文件请求，仍有进度
  //   ③ 浏览器直连：中继整条不可用（服务器过载 / 源站拒绝服务器 IP）→ <a download> 裸下，零服务器带宽
  // 为什么不直接 ①：跨域 fetch 读不到 CDN 的 Content-Length/Content-Range（CDN 不回 CORS 头），
  // 没有总长度就切不了片，所以必须先过中继。中继在解析同一节点上跑，直链的 IP 绑定/签名天然一致。
  const _DL_CHUNK = 10 * 1024 * 1024;        // 10MB / 片（与 DataTool 同档）
  const _DL_PARALLEL = 3;                    // 3 路并发（与 DataTool 同档）
  const _DL_RETRY = 3;                       // 每片最多 3 次
  const _DL_MIN_CHUNKED = 4 * 1024 * 1024;   // < 4MB 不值当分片，走单流
  const _DL_MAX_CHUNKS = 400;                // 分片上限（≈4GB）：超出走单流，避免分片数组把内存吃爆
  let _dlAbort = null;
  let _dlBusy = false;
  let _dlLastPaint = 0;

  const _dlRelayUrl = (url, base) =>
    `${base || ''}/api/media/proxy?u=${encodeURIComponent(url)}`;

  // ===== 远端失败自动换路由重试 =====
  // 浏览器侧拿不到底层错误码：TLS 握手失败、连接重置、DNS 失败在 fetch 里统统是
  // `TypeError: Failed to fetch`。所以只能按**可观测信号**分类，不同类别策略不同：
  //   · network —— 网络层（TLS 抖动 / 连接重置 / DNS）。**先在同一条路由上原地重试**，
  //                这类抖动常自愈；换路由解决不了本机的偶发问题，只会白白多打一次对端。
  //   · missing —— 404/405：对端**没有这个端点**（老分支常见）。原地重试纯属浪费，
  //                直接换路由，并把该节点标记为长期不可用（端点缺失是节点属性，与源站无关）。
  //   · forbid  —— 401/403：防盗链 / 签名不匹配。换路由可能有效（IP 签名换出口就变了），
  //                但它是**源站维度**的，按 host 记。
  //   · upstream—— 5xx：上游（CDN / 对端代理）自己失败，换路由有机会绕开；也可能自愈，短 TTL。
  //   · abort   —— 用户取消，任何重试都不该发生。
  const _dlErrKind = (err) => {
    if (!err) return 'unknown';
    if (err.name === 'AbortError') return 'abort';
    const m = String((err && err.message) || err);
    if (/\b(404|405)\b|Not Found/i.test(m)) return 'missing';
    if (/\b(401|403)\b|Forbidden|Unauthorized/i.test(m)) return 'forbid';
    if (/\b5\d\d\b/i.test(m)) return 'upstream';
    if (/Failed to fetch|NetworkError|TypeError|network/i.test(m)) return 'network';
    return 'unknown';
  };

  const _DL_ROUTE_TTL = {           // 各类失败把某条路由"判死"多久（毫秒）
    missing: 600000,                // 端点不存在 → 这次会话基本不用再试
    forbid: 300000,
    upstream: 60000,
    network: 30000,
    unknown: 30000,
  };
  // 端点缺失（missing）是**节点属性**，换任何源站都一样 ⇒ key 不带 host；
  // 其余是源站/链路属性 ⇒ key 带 host，避免一个源站的问题株连到别的源站。
  const _DL_ROUTE_HOST_SCOPED = { missing: false, forbid: true, upstream: true, network: true, unknown: true };
  const _DL_ROUTE_NET_RETRY = 1;    // 同一条路由上，网络类错误的原地重试次数
  const _DL_ROUTE_SWITCH_MAX = 2;   // 单次下载最多切几次路由（防止来回抖动）

  const _dlRouteExhausted = new Map();   // key -> 解禁时间戳
  const _dlHostOf = (url) => {
    try { return new URL(url).hostname || ''; } catch (_e) { return ''; }
  };
  const _dlRouteKey = (kind, b, host) =>
    `${kind}|${b || ''}|${(_DL_ROUTE_HOST_SCOPED[kind] === false) ? '' : (host || '')}`;
  const _dlRouteDead = (kind, b, host) => {
    const until = _dlRouteExhausted.get(_dlRouteKey(kind, b, host));
    if (!until) return false;
    if (Date.now() > until) { _dlRouteExhausted.delete(_dlRouteKey(kind, b, host)); return false; }
    return true;
  };

  /**
   * 按候选路由依次取一次资源。成功即返回，并把选中的 base 写回 state.base
   * （后续请求沿用，**不再逐个试探**）；失败先按 _dlErrKind 决定「原地重试」还是
   * 「换路由」，再把判死的路由写进 _dlRouteExhausted —— 这就是防抖的关键：
   * 对端一旦被确认为缺端点，后面几十上百个分片请求都不会再去敲它。
   */
  const _dlRouteFetch = async (url, bases, state, doFetch, signal) => {
    const host = _dlHostOf(url);
    const order = [state.base, ...bases.filter((b) => b !== state.base)];
    let lastErr = null;
    for (const b of order) {
      if (_dlRouteDead('missing', b, host)) continue;
      let netTry = 0;
      for (;;) {
        try {
          const out = await doFetch(_dlRelayUrl(url, b), signal);
          if (b !== state.base) {
            state.switches = (state.switches || 0) + 1;
            state.base = b;
          }
          return out;
        } catch (err) {
          if (err && err.name === 'AbortError') throw err;
          lastErr = err;
          const kind = _dlErrKind(err);
          if (kind === 'network' && netTry < _DL_ROUTE_NET_RETRY) { netTry += 1; continue; }
          // network 类不判死：它多半是本机/链路的偶发抖动，不是这条路由的错
          if (kind !== 'network') {
            _dlRouteExhausted.set(_dlRouteKey(kind, b, host), Date.now() + (_DL_ROUTE_TTL[kind] || 30000));
          }
          break;
        }
      }
      if ((state.switches || 0) >= _DL_ROUTE_SWITCH_MAX) break;
    }
    throw lastErr || new Error('中继不可用');
  };

  const _dlExtFromUrl = (url) => {
    const m = /\.(mp4|webm|mov|mkv|flv|m4a|mp3|aac|ogg|wav)(?:[?#]|$)/i.exec(url || '');
    return m ? `.${m[1].toLowerCase()}` : '';
  };

  const _dlWithExt = (title, contentType, url = '') => {
    const name = (title || 'video').trim() || 'video';
    if (/\.[a-z0-9]{2,5}$/i.test(name)) return name;      // 已有扩展名，尊重原样
    const map = {
      'video/mp4': '.mp4', 'video/webm': '.webm', 'video/quicktime': '.mov',
      'video/x-matroska': '.mkv', 'video/x-flv': '.flv',
      'audio/mpeg': '.mp3', 'audio/mp4': '.m4a', 'audio/aac': '.aac',
      'audio/ogg': '.ogg', 'audio/wav': '.wav', 'audio/x-wav': '.wav',
    };
    const byType = map[(contentType || '').split(';')[0].trim().toLowerCase()] || '';
    return name + (byType || _dlExtFromUrl(url));
  };

  // 取消：再点一次同一按钮
  const _dlCancel = () => {
    if (_dlBusy && _dlAbort) {
      el.directHint.textContent = '正在取消…';
      _dlAbort.abort();
      return true;
    }
    return false;
  };

  // 探测源站总长度与类型：Range: bytes=0-0 → 206 + Content-Range: bytes 0-0/TOTAL
  const _dlProbe = async (relay, signal) => {
    const r = await fetch(relay, { headers: { Range: 'bytes=0-0' }, signal, cache: 'no-store' });
    if (r.status !== 206 && !r.ok) {
      let detail = '';
      try { detail = (await r.json()).detail || ''; } catch (_e) { /* 非 JSON 响应 */ }
      throw new Error(detail || `源站返回 ${r.status}`);
    }
    const m = /bytes\s+\d+-\d+\/(\d+)/i.exec(r.headers.get('Content-Range') || '');
    const type = (r.headers.get('Content-Type') || 'video/mp4').split(';')[0].trim();
    // 必须把 body 取消掉，否则这条 206 连接一直占着并发额度
    try { await r.body?.cancel(); } catch (_e) { /* 已结束 */ }
    if (m) return { size: parseInt(m[1], 10) || 0, type, ranged: true };
    const cl = parseInt(r.headers.get('Content-Length') || '0', 10) || 0;
    return { size: r.status === 200 ? cl : 0, type, ranged: r.status === 206 };
  };

  const _dlChunk = async (relay, start, end, signal) => {
    const want = end - start + 1;
    let lastErr = null;
    for (let attempt = 1; attempt <= _DL_RETRY; attempt += 1) {
      try {
        const r = await fetch(relay, {
          headers: { Range: `bytes=${start}-${end}` },
          signal,
          cache: 'no-store',
        });
        if (r.status !== 206) {
          if (r.status === 200) {
            // 源站忽略 Range 直接甩回整文件。这里**不能**去读 body ——
            // 那等于把一个完整视频当 JSON 解析，白白吃满内存。
            try { await r.body?.cancel(); } catch (_e) { /* 已结束 */ }
            throw new Error('源站不支持分段');
          }
          let detail = '';
          try { detail = (await r.json()).detail || ''; } catch (_e) { /* 非 JSON 响应 */ }
          throw new Error(detail || `HTTP ${r.status}`);
        }
        const buf = new Uint8Array(await r.arrayBuffer());
        // 长度必须与请求区间**严格相等**：短了会留空洞（成品损坏），长了说明源站没按 Range 回。
        // 宁可重试、再退单流，也绝不把错位的数据拼进成品 —— 静默损坏比报错更难查。
        if (buf.byteLength !== want) {
          throw new Error(`分片长度不符（期望 ${want}，收到 ${buf.byteLength}）`);
        }
        return buf;
      } catch (err) {
        if (err.name === 'AbortError') throw err;
        lastErr = err;
      }
      if (attempt < _DL_RETRY) {
        await new Promise((resolve) => setTimeout(resolve, 400 * attempt));
      }
    }
    throw lastErr || new Error('分片下载失败');
  };

  const _dlPaint = (state) => {
    const now = performance.now();
    if (now - _dlLastPaint < 120) return;   // 单流可能几十 KB 一次回调，节流重绘
    _dlLastPaint = now;
    const sec = Math.max(0.3, (now - state.t0) / 1000);
    const speed = state.done / sec;
    // 按钮文案由调用方决定（直链下载按钮含 SVG，只能改末位文本节点；HLS 合成按钮是纯文本）
    const setLabel = state.setLabel || ((text) => {
      const node = el.downloadBtn.lastChild;
      if (node) node.textContent = text;
    });
    // HLS 合成：总字节数事先未知（清单里不写大小），进度只能按「片」算
    if (state.segCount) {
      const spct = Math.min(100, Math.floor((state.segDone / state.segCount) * 100));
      el.directHint.textContent =
        `⬇ 合成中 ${spct}% · 第 ${state.segDone}/${state.segCount} 片 · 已接收 ${formatBytes(state.done)}`
        + ` · ${formatBytes(Math.round(speed))}/s（再点一次按钮可取消）`;
      setLabel(`合成中 ${spct}%…（点此取消）`);
      return;
    }
    const pct = state.total ? Math.min(100, Math.floor((state.done / state.total) * 100)) : 0;
    const eta = state.total && speed > 0 ? (state.total - state.done) / speed : 0;
    el.directHint.textContent = state.total
      ? `⬇ 下载中 ${pct}% · ${formatBytes(state.done)}/${formatBytes(state.total)} · ${formatBytes(Math.round(speed))}/s`
        + `${eta ? ` · ${formatEta(eta)}` : ''}（再点一次按钮可取消）`
      : `⬇ 下载中 · 已接收 ${formatBytes(state.done)} · ${formatBytes(Math.round(speed))}/s（再点一次按钮可取消）`;
    setLabel(state.total ? `下载中 ${pct}%…（点此取消）` : '下载中…（点此取消）');
  };

  const _dlSave = (blob, title, url = '', forceExt = '') => {
    const objUrl = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = objUrl;
    if (forceExt) {
      // HLS 合成的容器由分片扩展名决定（.ts / .mp4），比标题里可能带的旧后缀可靠 —— 强制覆盖
      const stripped = (title || 'video').trim().replace(/\.(mp4|webm|mov|mkv|flv|m4a|mp3|aac|ogg|wav|ts|m3u8)$/i, '');
      a.download = `${stripped || 'video'}${forceExt}`;
    } else {
      a.download = _dlWithExt(title, blob.type, url);
    }
    document.body.appendChild(a);
    a.click();
    a.remove();
    // 立刻 revoke 会让部分浏览器下载中断，延后释放
    setTimeout(() => URL.revokeObjectURL(objUrl), 120000);
  };

  const _dlAnchorDownload = (url, title) => {
    const a = document.createElement('a');
    a.href = url;
    if (title) a.download = _dlWithExt(title, '', url);
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    a.remove();
  };

  // 中继基址候选：优先「解析锁定节点」（海外直链的 IP 签名/防盗链只对那个节点有效），
  // 失败再回落主站。为什么需要回落 —— 对端节点可能是老版本、还没有本端点（实测香港
  // 节点的 routers/core.py 就是更早的分支，连 /api/stream/proxy 都没有），而主站对
  // **非墙海外源**仍能中转（走 VDL_PROXY）。回落**只在探测阶段**发生：已经下了半截
  // 再换节点等于白烧流量。
  const _dlProbeWithFallback = async (url, base, signal) => {
    const state = { base: base || '', switches: 0 };
    const probe = await _dlRouteFetch(url, base ? [base, ''] : [''], state, _dlProbe, signal);
    return { relay: _dlRelayUrl(url, state.base), probe };
  };

  const _DL_CHUNK_FALLBACK_MAX = 512 * 1024 * 1024;   // 分片失败后最多重下这么多（见 _dlRun）

  // ① 分片并发。每片必须整片到手，否则宁可整体失败也不拼出带空洞的文件。
  const _dlRunChunked = async (relay, total, type, signal, onProgress) => {
    const count = Math.ceil(total / _DL_CHUNK);
    const parts = new Array(count);
    let done = 0;
    let next = 0;
    const worker = async () => {
      for (;;) {
        const i = next;
        next += 1;
        if (i >= count) return;
        const start = i * _DL_CHUNK;
        const end = Math.min(total, start + _DL_CHUNK) - 1;
        parts[i] = await _dlChunk(relay, start, end, signal);
        done += parts[i].byteLength;
        onProgress(done, total);
      }
    };
    await Promise.all(Array.from({ length: Math.min(_DL_PARALLEL, count) }, worker));
    return new Blob(parts, { type });
  };

  // ② 单流中继：源站不认 Range，或体积超过分片上限（保护内存）
  const _dlRunStream = async (relay, total, type, signal, onProgress) => {
    const r = await fetch(relay, { signal, cache: 'no-store' });
    if (!r.ok) {
      let detail = '';
      try { detail = (await r.json()).detail || ''; } catch (_e) { /* 非 JSON 响应 */ }
      throw new Error(detail || `源站返回 ${r.status}`);
    }
    const len = parseInt(r.headers.get('Content-Length') || '0', 10) || total || 0;
    const reader = r.body.getReader();
    const chunks = [];
    let got = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      got += value.byteLength;
      onProgress(got, len);
    }
    return new Blob(chunks, { type });
  };

  const _dlRun = async (url, { base, title, signal, onProgress }) => {
    const { relay, probe } = await _dlProbeWithFallback(url, base, signal);
    const total = probe.size || 0;
    const canChunk = probe.ranged && total >= _DL_MIN_CHUNKED && total <= _DL_MAX_CHUNKS * _DL_CHUNK;
    if (canChunk) {
      try {
        return await _dlRunChunked(relay, total, probe.type, signal, onProgress);
      } catch (err) {
        if (err && err.name === 'AbortError') throw err;
        // 分片失败（源站裁短 Range / 某片反复 5xx）：小文件就地退单流重来；
        // 大文件不再重下一遍 —— 交给上层降级为浏览器直连，别白烧用户流量与服务器带宽。
        if (total > _DL_CHUNK_FALLBACK_MAX) throw err;
        onProgress(0, total);
      }
    }
    return _dlRunStream(relay, total, probe.type, signal, onProgress);
  };

  // ===== 浏览器内 HLS 合成（对标 DataTool 网页端的浏览器侧 m3u8 → MP4）=====
  // 服务端那条路要落盘、要 ffmpeg 合并/转码，且成品还要再发给用户一次
  // （出口流量 = 分片拉回 + 成品发出，约两趟）；本路径把合并放在浏览器里做，
  // 服务器只做字节中继转发（一趟），不落盘、不转码、不计下载额度。
  // 清单与分片都必须经 /api/media/proxy 中继：HLS 分片同样带 IP 绑定签名
  // 与 Referer 防盗链，直连浏览器既跨域又会被 403。
  //
  // 产物容器按分片扩展名决定（**不做转码**，浏览器内 ffmpeg.wasm 光 core 就 30MB+，不值当）：
  //   · fMP4 / CMAF（有 #EXT-X-MAP，或分片是 .m4s/.mp4）→ 拼成 .mp4
  //   · MPEG-TS（分片是 .ts）→ 拼成 .ts（VLC / PotPlayer / ffmpeg 都能直接播）
  // 明确不支持，命中即抛错并由上层提示「改用服务器下载」：
  //   · 加密流（#EXT-X-KEY）—— 需实现 AES-128 / SAMPLE-AES；
  //   · 直播流（媒体清单无 #EXT-X-ENDLIST）—— 没有终点，永远拼不完；
  //   · DASH / 音视频分离 —— 那类不走 m3u8，属于服务端队列的活。
  const _HLS_MAX_SEGMENTS = 4000;      // 异常大的清单直接放弃，别把请求数与内存打爆

  // 相对地址必须相对**清单自己的 URL** 解析，不能相对中继地址（否则会拼出假路径）。
  // 解析失败**必须抛错**：静默退回相对地址只会让中继收到一个非 http(s) 的 u 参数，
  // 最后变成一个莫名其妙的 400，比直接说「清单里的地址无法解析」难查得多。
  const _hlsAbs = (uri, baseUrl) => {
    try {
      return new URL(uri, baseUrl).href;
    } catch (_e) {
      throw new Error(`清单里的地址无法解析：${String(uri).slice(0, 80)}`);
    }
  };

  const _dlFetchText = async (relay, signal) => {
    const r = await fetch(relay, { signal, cache: 'no-store' });
    if (!r.ok) {
      let detail = '';
      try { detail = (await r.json()).detail || ''; } catch (_e) { /* 非 JSON 响应 */ }
      throw new Error(detail || `清单读取失败（HTTP ${r.status}）`);
    }
    return r.text();
  };

  const _dlFetchBin = async (relay, signal) => {
    const r = await fetch(relay, { signal, cache: 'no-store' });
    if (!r.ok) {
      let detail = '';
      try { detail = (await r.json()).detail || ''; } catch (_e) { /* 非 JSON 响应 */ }
      throw new Error(detail || `分片读取失败（HTTP ${r.status}）`);
    }
    return new Uint8Array(await r.arrayBuffer());
  };

  /** 解析 m3u8。baseUrl 必须是清单自身地址（用于把相对 URI 解析成绝对地址）。 */
  const _dlParseM3u8 = (text, baseUrl) => {
    const variants = [];
    const segments = [];
    let init = null;
    let encrypted = false;
    let ended = false;
    let pending = null;
    for (const raw of String(text || '').split(/\r?\n/)) {
      const line = raw.trim();
      if (!line) continue;
      if (line.startsWith('#')) {
        if (line.startsWith('#EXT-X-KEY')) {
          // METHOD=NONE 表示自此不再加密，等价于「没加密」
          if (!/METHOD\s*=\s*NONE/i.test(line)) encrypted = true;
        } else if (line.startsWith('#EXT-X-MAP')) {
          const m = /URI\s*=\s*"([^"]*)"/i.exec(line);
          if (m) init = _hlsAbs(m[1], baseUrl);
        } else if (line.startsWith('#EXT-X-ENDLIST')) {
          ended = true;
        } else if (line.startsWith('#EXT-X-STREAM-INF')) {
          const h = /RESOLUTION\s*=\s*\d+x(\d+)/i.exec(line);
          const bw = /BANDWIDTH\s*=\s*(\d+)/i.exec(line);
          pending = {
            height: h ? parseInt(h[1], 10) : 0,
            bandwidth: bw ? parseInt(bw[1], 10) : 0,
          };
        }
        continue;
      }
      if (pending) {                    // 紧跟 #EXT-X-STREAM-INF 的非注释行 = 该变体的地址
        pending.uri = _hlsAbs(line, baseUrl);
        variants.push(pending);
        pending = null;
      } else {
        segments.push(_hlsAbs(line, baseUrl));
      }
    }
    if (variants.length) return { kind: 'master', variants };
    // 分片扩展名 → 成品容器（同一清单内容器一致，看第一片就够）
    const leaf = (segments[0] || '').split(/[?#]/)[0].split('/').pop().toLowerCase();
    const dot = leaf.lastIndexOf('.');
    return {
      kind: 'media',
      segments,
      init,
      encrypted,
      live: !ended,                     // 媒体清单没有 ENDLIST ⇒ 直播/未完结，拼不完整
      segExt: dot > 0 ? leaf.slice(dot + 1) : '',
    };
  };

  /** master 清单里挑一路变体：有目标高度就挑「不超过它」的最高档，否则挑码率最高档。 */
  const _dlPickVariant = (variants, wantHeight) => {
    const list = variants.slice().sort((a, b) => (a.height - b.height) || (a.bandwidth - b.bandwidth));
    if (wantHeight > 0) {
      let best = null;
      for (const v of list) {
        if (v.height && v.height <= wantHeight) best = v;
      }
      return best || list[0];
    }
    return list[list.length - 1];
  };

  /** 成品容器：有初始化段或分片是 fMP4/CMAF → .mp4；否则按 MPEG-TS → .ts。 */
  const _dlHlsContainer = (pl) => {
    const e = (pl.segExt || '').toLowerCase();
    if (pl.init || e === 'm4s' || e === 'mp4' || e === 'cmfv' || e === 'cmfa') {
      return { ext: '.mp4', type: 'video/mp4' };
    }
    return { ext: '.ts', type: 'video/mp2t' };
  };

  /** 浏览器内合成主流程。wantHeight 来自当前选中的下载画质（0 = 最高档）。 */
  const _dlRunHls = async (m3u8Url, base, { signal, wantHeight = 0, onProgress }) => {
    // 中继基址候选：优先「解析锁定节点」，失败回落主站。为什么必须回落 —— 海外链接的
    // base 指向对端（香港），而**对端常是更老的分支、没有 /api/media/proxy**
    // （实测 2026-09-27：不回落时海外 HLS 一律拿到 404「Not Found」）。
    // 回落只在**第一条请求**做一次，之后整条链路都用选定的 base：
    // 两边都能取到同一份分片，中途换节点没有意义，只会让地址来源变得难以复现。
    const bases = base ? [base, ''] : [''];
    // state.base 是"当前生效的路由"，成功的那条会被写回并被后续所有请求沿用；
    // 中途某条请求失败时按 _dlErrKind 换路由，换成功也写回 state.base（不来回切）。
    const state = { base: base || '', switches: 0 };
    const fetchText = (u) => _dlRouteFetch(u, bases, state, _dlFetchText, signal);
    const fetchBin = (u) => _dlRouteFetch(u, bases, state, _dlFetchBin, signal);

    let url = m3u8Url;
    let pl = _dlParseM3u8(await fetchText(m3u8Url), url);
    if (pl.kind === 'master') {
      if (!pl.variants.length) throw new Error('清单里没有可用的清晰度');
      url = _dlPickVariant(pl.variants, wantHeight).uri;
      pl = _dlParseM3u8(await fetchText(url), url);
    }
    if (pl.kind !== 'media' || !pl.segments.length) throw new Error('清单里没有分片');
    if (pl.encrypted) throw new Error('该流已加密，浏览器无法解密');
    if (pl.live) throw new Error('直播流没有终点');
    if (pl.segments.length > _HLS_MAX_SEGMENTS) throw new Error(`分片过多（${pl.segments.length} 段）`);
    const { ext, type } = _dlHlsContainer(pl);
    const offset = pl.init ? 1 : 0;
    const parts = new Array(pl.segments.length + offset);
    let bytes = 0;
    if (pl.init) {
      // 初始化段必须排在最前：fMP4 少了它整个文件无法解码
      parts[0] = await fetchBin(pl.init);
      bytes += parts[0].byteLength;
    }
    let next = 0;
    let segDone = 0;
    const worker = async () => {
      for (;;) {
        const i = next;
        next += 1;
        if (i >= pl.segments.length) return;
        const buf = await fetchBin(pl.segments[i]);
        parts[i + offset] = buf;
        bytes += buf.byteLength;
        segDone += 1;
        onProgress(bytes, segDone, pl.segments.length);
      }
    };
    await Promise.all(
      Array.from({ length: Math.min(_DL_PARALLEL, pl.segments.length) }, worker),
    );
    return { blob: new Blob(parts, { type }), ext };
  };

  /** 当前选中下载画质 → 可用的播放地址（用 watch_options 的真实地址最稳）。 */
  const _dlHlsSourceFor = (qualityKey) => {
    const opts = Array.from(el.watchQuality.options || []).filter((o) => o.dataset.url);
    if (!opts.length) return null;
    const num = (o) => parseInt(o.value, 10);
    const want = parseInt(qualityKey, 10);
    if (Number.isFinite(want)) {
      const exact = opts.find((o) => num(o) === want);
      if (exact) return exact;
      const numbered = opts.filter((o) => Number.isFinite(num(o)));
      const lower = numbered.filter((o) => num(o) <= want).sort((a, b) => num(b) - num(a))[0];
      if (lower) return lower;
      // 目标档低于所有可选档（平台没提供这么低的分辨率）→ 给**最低**档。
      // 不能给最高档：用户要的是「小」，静默升档属于欺骗。
      if (numbered.length) return numbered.slice().sort((a, b) => num(a) - num(b))[0];
    }
    // best / auto / audio / webm 等：挑高度最高的 HLS 档（优先 HLS，没有才退渐进式）
    const hls = opts.filter((o) => o.dataset.hls === 'true');
    return (hls.length ? hls : opts).slice().sort((a, b) => (num(b) || 0) - (num(a) || 0))[0] || null;
  };

  const triggerBrowserHlsDownload = async () => {
    if (_dlCancel()) return;                 // 正在合成 → 本次点击视为取消
    const src = _dlHlsSourceFor(selectedQuality);
    if (!src || !src.dataset.url) {
      el.directHint.textContent = '当前画质没有可用于浏览器合成的地址，请改用上方「开始下载」走服务器。';
      return;
    }
    const m3u8Url = src.dataset.url;
    const title = (resolved && resolved.video && resolved.video.title) || 'video';
    const base = (resolved && resolved.base) || '';
    _dlBusy = true;
    _dlAbort = new AbortController();
    const signal = _dlAbort.signal;
    const btn = el.browserHlsBtn;
    const orig = btn.textContent;
    btn.disabled = false;                    // 保持可点：它是唯一的取消入口
    const t0 = performance.now();
    const state = {
      done: 0, total: 0, t0, segDone: 0, segCount: 0,
      setLabel: (text) => { btn.textContent = text; },
    };
    try {
      const { blob, ext } = await _dlRunHls(m3u8Url, base, {
        signal,
        wantHeight: parseInt(selectedQuality, 10) || 0,
        onProgress: (bytes, segDone, segCount) => {
          state.done = bytes;
          state.segDone = segDone;
          state.segCount = segCount;
          _dlPaint(state);
        },
      });
      _dlSave(blob, title, m3u8Url, ext);
      el.directHint.textContent =
        `✅ 已在浏览器内合成并保存（${formatBytes(blob.size)}，${ext}）。服务器只做中继转发，未落盘、未转码。`;
    } catch (err) {
      if (err && err.name === 'AbortError') {
        el.directHint.textContent = '已取消合成。再次点击可重试。';
        return;
      }
      el.directHint.textContent =
        `⚠ 浏览器内合成不可用（${err.message || err}）。请用上方「开始下载」走服务器（服务器会合并/转码成 MP4）。`;
    } finally {
      _dlBusy = false;
      _dlAbort = null;
      btn.textContent = orig;
    }
  };

  const triggerDirectDownload = async (url, title, base = '') => {
    if (_dlCancel()) return;                 // 正在下载 → 本次点击视为取消
    if (!url) return;
    _dlBusy = true;
    _dlAbort = new AbortController();
    const signal = _dlAbort.signal;
    const label = el.downloadBtn.lastChild;
    const origLabel = label ? label.textContent : '';
    // 保持按钮可点：它是唯一的「取消」入口
    el.downloadBtn.disabled = false;
    el.serverFallbackBtn.hidden = true;
    const t0 = performance.now();
    const state = { done: 0, total: 0, t0 };
    try {
      const blob = await _dlRun(url, {
        base,
        title,
        signal,
        onProgress: (done, total) => {
          state.done = done;
          state.total = total;
          _dlPaint(state);
        },
      });
      _dlSave(blob, title, url);
      el.directHint.textContent =
        `✅ 已保存到本机（${formatBytes(blob.size)}）。文件从源站直取、不落我们的服务器磁盘，也不计入服务器下载额度。`;
    } catch (err) {
      if (err && err.name === 'AbortError') {
        el.directHint.textContent = '已取消下载。再次点击可直接重试。';
        return;
      }
      // 中继不可用 → 退回浏览器直连源站（零服务器带宽，代价是没进度）
      el.directHint.textContent =
        `⚠ 加速下载不可用（${err.message || err}），已改用浏览器直接下载。若仍失败，请点下方「改用服务器下载」。`;
      _dlAnchorDownload(url, title);
    } finally {
      _dlBusy = false;
      _dlAbort = null;
      const l = el.downloadBtn.lastChild;
      if (l) l.textContent = origLabel || '直接保存到本机 ⬇';
    }
  };

  const handleDownload = async () => {
    if (resolved?.video?.direct_url) {
      // 直链直存：走分片下载引擎（失败自动降级），锁定解析所在节点
      triggerDirectDownload(resolved.video.direct_url, resolved.video.title, resolved.base || '');
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
  // 浏览器内合成（HLS）：再点一次即取消，语义与「直接保存到本机」一致
  if (el.browserHlsBtn) el.browserHlsBtn.addEventListener('click', () => { triggerBrowserHlsDownload(); });
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
    el.subModalSub.textContent = parts.length
      ? `免费用户：${parts.join('；')}。订阅后全部无限使用。`
      : '订阅后解锁全部增值能力，无限使用。';
  };

  const initSubUI = () => {
    if (!node.convertSubRequired && !node.downloadSubRequired) return;
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

  // 2026-09-21：导航栏可见性**不再**依赖 /api/nodes 的成败。
  // 该请求一旦失败（跨境链路抖动 / 代理 / CF 边缘错误），`.catch()` 原本完全静默，
  // 而 `<nav id="tabs" hidden>` 只在成功回调里被解开 → 整个导航栏永久隐藏，
  // 表现为「网页版只剩下载，去水印/音乐转换/图片转换/AI 字幕/个人中心凭空消失」，
  // 且页面不给任何提示。故由必定会被调用的 switchView 兜底（幂等）。
  function applyWebTabs() {
    // 桌面版专属 tab 在网页精简版一律隐藏（不受后端 profile 影响，保持原语义）
    ['tabLibrary', 'tabCommentary', 'tabSubscribe', 'tabTorrent']
      .forEach((id) => { const t = document.getElementById(id); if (t) t.hidden = true; });
    if (el.tabs) el.tabs.hidden = false;
  }

  function switchView(view) {
    applyWebTabs();
    const isLib = view === 'library';
    const isSub = view === 'subscribe';
    const isTor = view === 'torrent';
    const isCom = view === 'commentary';
    const isUp = view === 'uploadconvert';
    const isDw = view === 'dw';
    const isAppIntro = view === 'appIntro';
    const isMusic = view === 'musicconvert';   // 音乐转换（2026-09-11 自 app-dev 移植）
    const isImage = view === 'imageconvert';   // 图片转换
    const isSt = view === 'subtitle';          // AI 字幕（区别于订阅 isSub）
    const isProfile = view === 'profile';      // 个人中心
    const isAnyExtra = isMusic || isImage || isSt || isProfile;
    el.downloadView.hidden = isLib || isSub || isTor || isCom || isUp || isDw || isAppIntro || isAnyExtra;
    el.libraryView.hidden = !isLib;
    el.subscribeView.hidden = !isSub;
    el.torrentView.hidden = !isTor;
    el.commentaryView.hidden = !isCom;
    el.uploadConvertView.hidden = !isUp;
    el.dwView.hidden = !isDw;
    el.appIntroView.hidden = !isAppIntro;
    if (el.musicConvertView) el.musicConvertView.hidden = !isMusic;
    if (el.imageConvertView) el.imageConvertView.hidden = !isImage;
    if (el.subtitleView) el.subtitleView.hidden = !isSt;
    if (el.profileView) el.profileView.hidden = !isProfile;
    if (el.tabDownload) el.tabDownload.classList.toggle('is-active', !isLib && !isSub && !isTor && !isCom && !isUp && !isDw && !isAppIntro && !isAnyExtra);
    if (el.tabLibrary) el.tabLibrary.classList.toggle('is-active', isLib);
    if (el.tabSubscribe) el.tabSubscribe.classList.toggle('is-active', isSub);
    if (el.tabTorrent) el.tabTorrent.classList.toggle('is-active', isTor);
    if (el.tabCommentary) el.tabCommentary.classList.toggle('is-active', isCom);
    if (el.tabUploadConvert) el.tabUploadConvert.classList.toggle('is-active', isUp);
    if (el.tabDw) el.tabDw.classList.toggle('is-active', isDw);
    if (el.tabAppIntro) el.tabAppIntro.classList.toggle('is-active', isAppIntro);
    if (el.tabMusicConvert) el.tabMusicConvert.classList.toggle('is-active', isMusic);
    if (el.tabImageConvert) el.tabImageConvert.classList.toggle('is-active', isImage);
    if (el.tabSubtitle) el.tabSubtitle.classList.toggle('is-active', isSt);
    if (el.tabProfile) el.tabProfile.classList.toggle('is-active', isProfile);
    if (isLib) loadLibrary();
    if (isSub) loadSubscriptions();
    if (isCom) loadCommentary();
    if (isUp) { el.ucStatus.textContent = ''; }
    if (isSt) { el.sbStatus.textContent = ''; }
    if (isProfile) pfLoad();
    if (isDw) { el.dwImgStatus.textContent = ''; el.dwPdfStatus.textContent = ''; }
    if (isTor) { loadTorrents(); startTorPoll(); }
    else stopTorPoll();
  };

  // ====================================================================== 
  // ===== 2026-09-11 自 app-dev 移植：音乐转换 / 图片转换 / AI 字幕 =====
  // ======================================================================

  // ===== 音乐格式转换（独立 tab，复用音频转码后端 /api/upload-chunk + /api/convert）=====
  const MUS_AUDIO_FMTS = ['mp3', 'm4a', 'wav', 'flac', 'aac', 'opus', 'wma', 'mp2'];
  const MUS_CHUNK_SIZE = 32 * 1024 * 1024;
  const MUS_BIG_CHUNK_SIZE = 64 * 1024 * 1024;
  const musState = { list: [], nextId: 1, pollTimer: null };

  const musDesktopNative = () => !!(window.VDL && window.VDL.desktop && typeof window.VDL.desktop.chooseFiles === 'function');
  const musFormatSize = (b) => {
    if (b >= 1024 * 1024 * 1024) return (b / 1024 / 1024 / 1024).toFixed(2) + ' GB';
    if (b >= 1024 * 1024) return (b / 1024 / 1024).toFixed(1) + ' MB';
    if (b >= 1024) return (b / 1024).toFixed(0) + ' KB';
    return b + ' B';
  };
  const musBuildOutputName = (it) => {
    const base = (it.name || (it.file && it.file.name) || 'audio').replace(/\.[^.]+$/, '');
    return `[${it.target}]${base}.${it.target}`;
  };
  const musEnsurePolling = () => {
    if (musState.pollTimer) return;
    musState.pollTimer = setInterval(musPollAll, UC_POLL_INTERVAL || 1500);
  };
  const musStopPolling = () => { if (musState.pollTimer) { clearInterval(musState.pollTimer); musState.pollTimer = null; } };

  const musRender = () => {
    const list = musState.list;
    el.musCount.textContent = list.length ? `已添加 ${list.length} 个文件` : '尚未添加文件';
    el.musClearBtn.hidden = list.length === 0;
    el.musStartAllBtn.disabled = !list.some(it => it.status === 'uploaded' || it.status === 'failed' || it.status === 'pending');
    if (!list.length) { el.musList.innerHTML = ''; return; }
    el.musList.innerHTML = list.map(it => {
      const statusText = {
        pending: '未开始',
        uploading: `上传中 ${it.progress || 0}%${it.speedText ? ' · ' + it.speedText : ''}${it.uploadedText ? ' · ' + it.uploadedText : ''}`,
        uploaded: '已上传，待转码',
        running: it.stage === '无损直转' ? '无损直转中…' : it.stage === '排队中' ? '排队中…' : (it.progress ? `转码中 ${it.progress}%` : '转码中…'),
        completed: '完成 ✅',
        failed: '失败：' + (it.errorMsg || ''),
      }[it.status] || it.status;
      const statusCls = it.status === 'pending' ? '' : 'is-' + it.status.replace('uploading', 'running');
      const disabled = !['pending', 'failed', 'uploading', 'uploaded'].includes(it.status) ? 'disabled' : '';
      const progressHtml = (it.status === 'running' || it.status === 'uploading')
        ? `<div class="progress"><div class="progress-fill" style="width:${it.progress || 0}%"></div></div>` : '';
      const downloadHtml = it.downloadUrl && !['running', 'uploading'].includes(it.status)
        ? `<a class="uc-item-download" href="${it.downloadUrl}" download="${it.outputName || 'converted'}">下载</a>${it.libraryId ? ' · 已存媒体库' : ''}`
        : '';
      const reeditHtml = it.status === 'completed'
        ? `<button type="button" class="uc-item-start" data-act="reedit" title="恢复该行为可编辑状态：可改格式、重新转码（旧结果在重新转码前仍可下载）">重新编辑</button>`
        : '';
      const startHtml = it.status === 'uploaded'
        ? `<button type="button" class="uc-item-start" data-act="start" title="用该行已设置的格式开始转码">开始转码</button>`
        : it.status === 'failed'
          ? `<button type="button" class="uc-item-start" data-act="start" title="清除错误状态，按当前格式重新转码">重新转码</button>`
          : it.status === 'pending'
            ? `<button type="button" class="uc-item-start" data-act="start" title="先上传该文件，再按当前格式转码">开始转码</button>`
            : '';
      const targetDisabled = (it.status === 'running' || it.status === 'completed') ? 'disabled' : '';
      const displayName = it.name || (it.file && it.file.name) || '未命名';
      const metaSpans = it.localPath
        ? `<span style="color:var(--brand);font-size:12px;">本地文件 · 免上传</span><span>→ ${it.target.toUpperCase()}</span>`
        : (it.file ? `<span>${musFormatSize(it.file.size)}</span><span>→ ${it.target.toUpperCase()}</span>` : `<span>→ ${it.target.toUpperCase()}</span>`);
      const opts = MUS_AUDIO_FMTS.map(v => `<option value="${v}"${v === it.target ? ' selected' : ''}>${v.toUpperCase()}</option>`).join('');
      return `<li class="uc-item ${statusCls}" data-id="${it.id}">
        <div class="uc-item-main">
          <div class="uc-item-name" title="${displayName}">${displayName}</div>
          <div class="uc-item-meta">${metaSpans}</div>
          ${progressHtml}
          <div class="uc-item-status">${statusText}</div>
        </div>
        <div class="uc-item-side">
          <label class="sr-only" for="musItemTarget-${it.id}">输出格式</label>
          <select id="musItemTarget-${it.id}" data-act="target" ${targetDisabled} title="修改此行的目标格式（开始转码时生效）">${opts}</select>
          ${startHtml}
          ${reeditHtml}
          ${downloadHtml}
          <button type="button" class="uc-item-remove" data-act="remove" title="从列表移除" ${disabled}>×</button>
        </div>
      </li>`;
    }).join('');
  };

  const musAddFiles = (list) => {
    const b = { target: el.musBulkTarget.value || 'mp3', audio_bitrate: el.musBulkBitrate.value || '', toLibrary: el.musBulkLibrary.checked };
    Array.from(list || []).forEach(f => {
      const isLocal = typeof f === 'string';
      musState.list.push({
        id: musState.nextId++,
        file: isLocal ? null : f,
        localPath: isLocal ? f : null,
        name: isLocal ? (f.split(/[\\/]/).pop()) : f.name,
        target: b.target, audio_bitrate: b.audio_bitrate, toLibrary: b.toLibrary,
        status: isLocal ? 'uploaded' : 'pending',
        jobId: null, progress: isLocal ? 30 : 0, stage: isLocal ? '本地文件' : '',
        errorMsg: '', downloadUrl: '', outputName: '', libraryId: null,
        speedText: '', uploadedText: '', _removed: false, _xhrs: null, _uploadId: null,
      });
    });
    musRender();
    const localCount = musState.list.filter(x => x.localPath).length;
    const uploadCount = musState.list.filter(x => x.file).length;
    el.musStatus.textContent = localCount
      ? `已添加 ${localCount} 个本地文件，可直接开始转换`
      : `已添加 ${uploadCount} 个文件，点「开始转换」上传并转码`;
  };

  // 上传单个文件（32MB 分片，复用 ucUploadChunk）；本地文件跳过上传
  const musUploadOne = (item) => new Promise((resolve, reject) => {
    if (item.localPath) { item.status = 'uploaded'; item.progress = 30; item.stage = '本地文件'; musRender(); resolve(); return; }
    item.status = 'uploading'; item.progress = 0; item._removed = false; item._xhrs = new Set(); musRender();
    const file = item.file;
    const chunkSize = file.size > 2 * 1024 * 1024 * 1024 ? MUS_BIG_CHUNK_SIZE : MUS_CHUNK_SIZE;
    const totalChunks = Math.max(1, Math.ceil(file.size / chunkSize));
    const uploadId = item._uploadId = 'mus' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
    item._totalChunks = totalChunks;   // finish 提交时要用总分片数（漏设会报「分片不完整」，>32MB 必现）
    const done = new Set();
    const xhrs = item._xhrs;
    let uploadedBytes = 0;
    const pump = async () => {
      for (let idx = 0; idx < totalChunks; idx++) {
        if (done.has(idx) || item._removed) continue;
        const start = idx * chunkSize, end = Math.min(start + chunkSize, file.size);
        const blob = file.slice(start, end);
        try {
          await ucUploadChunk(uploadId, idx, totalChunks, blob, null, xhrs);
        } catch (e1) {
          if (item._removed) { reject(new Error('已取消')); return; }
          try { await ucUploadChunk(uploadId, idx, totalChunks, blob, null, xhrs); }
          catch (e2) { item.status = 'failed'; item.errorMsg = '上传失败：' + (e2 && e2.message || '网络错误'); musRender(); reject(e2); return; }
        }
        done.add(idx);
        uploadedBytes += (end - start);
        item.progress = Math.min(99, Math.round(uploadedBytes / file.size * 100));
        item.uploadedText = `${done.size}/${totalChunks}`;
        musRender();
      }
      if (item._removed) { reject(new Error('已取消')); return; }
      item.status = 'uploaded'; item.progress = 30; item.stage = '已上传'; item.uploadedText = ''; musRender(); resolve();
    };
    pump();
  });

  // 提交转码：finish 合并分片 + 启动 job（音乐转换强制 audio=true + audio_bitrate）
  // 桌面端本地文件直接调 /api/convert/local，跳过分片 finish（避免 upload_id/total 校验失败）。
  const musFinishOne = (item) => new Promise((resolve, reject) => {
    if (!item || item.status !== 'uploaded') { reject(new Error('状态不允许开始转码')); return; }
    item.status = 'running'; item.progress = 30; item.stage = ''; musRender();

    if (item.localPath) {
      request('/api/convert/local', {
        method: 'POST',
        body: JSON.stringify({
          local_path: item.localPath,
          target: item.target,
          resolution: '',
          bitrate: '',
          audio: true,
          rotate: 0,
          remux: false,
          to_library: !!item.toLibrary,
          audio_bitrate: item.audio_bitrate || '',
        }),
        headers: { 'Content-Type': 'application/json' },
      }).then(data => {
        if (data.job_id) {
          item.jobId = data.job_id; item.status = 'running'; item.progress = 30;
          musEnsurePolling();   // 双保险：无论定时器此前是否被误停，job_id 到手立刻确保轮询在跑
          musRender(); resolve(data);
        } else {
          item.status = 'failed'; item.errorMsg = data.detail || data.error || '本地转换请求失败'; musRender(); reject(new Error(item.errorMsg));
        }
      }).catch(err => {
        item.status = 'failed'; item.errorMsg = (err && err.message) || '本地转换请求失败'; musRender(); reject(err);
      });
      return;
    }

    const form = new FormData();
    form.append('upload_id', item._uploadId);
    form.append('total', item._totalChunks || 1);
    form.append('filename', item.file ? item.file.name : (item.name || 'audio'));
    form.append('target', item.target);
    form.append('audio', 'true');           // 音乐转换：强制仅音频
    form.append('audio_bitrate', item.audio_bitrate || '');
    form.append('resolution', '');
    form.append('bitrate', '');
    form.append('rotate', '0');
    form.append('remux', 'false');
    form.append('to_library', item.toLibrary ? 'true' : 'false');
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/upload-chunk/finish');
    xhr.setRequestHeader('X-Device-Id', deviceId());
    const _authTok = localStorage.getItem('vdl_auth_token');   // 2026-09-29 服务端功能门禁：finish 须登录
    if (_authTok) xhr.setRequestHeader('Authorization', 'Bearer ' + _authTok);
    xhr.timeout = 120000;  // 音乐转换
    xhr.addEventListener('load', () => {
      try {
        const data = JSON.parse(xhr.responseText || '{}');
        if (xhr.status >= 200 && xhr.status < 300 && data.job_id) {
          item.jobId = data.job_id; item.status = 'running'; item.progress = 30;
          item._uploadId = null; item._totalChunks = null;   // 分片已被 finish 合并消化，之后重转需重传
          musEnsurePolling();   // 双保险：job_id 到手立刻确保轮询在跑（防 tick 自杀停表后无人重启）
          musRender(); resolve(data);
        } else {
          item.status = 'failed';
          item._uploadId = null; item._totalChunks = null;   // 分片状态已不可信（可能被消化/不完整），下次重新转码走重传
          item.errorMsg = data.detail || data.error || ('HTTP ' + xhr.status);
          musRender(); reject(new Error(item.errorMsg));
        }
      } catch (e) {
        item.status = 'failed'; item.errorMsg = '服务器响应异常（可能是网络/代理超时）'; musRender(); reject(e);
      }
    });
    xhr.addEventListener('error', () => { item.status = 'failed'; item._uploadId = null; item._totalChunks = null; item.errorMsg = '网络错误'; musRender(); reject(new Error('network')); });
    xhr.addEventListener('timeout', () => { item.status = 'failed'; item._uploadId = null; item._totalChunks = null; item.errorMsg = '响应超时（请重试）'; musRender(); reject(new Error('timeout')); });
    xhr.send(form);
  });

  // 免重传重转：复用服务端保留的源文件（finish 合并产物，2h TTL）按新格式再次转码。
  // 410/404（源过期/任务不存在）时清 _srcJobId 并回退为重新上传。
  const musReconvert = (item) => new Promise((resolve, reject) => {
    if (!item || !item._srcJobId) { reject(new Error('无源任务')); return; }
    item.status = 'running'; item.progress = 30; item.stage = ''; musRender();
    const form = new FormData();
    form.append('job_id', item._srcJobId);
    form.append('target', item.target);
    form.append('audio_bitrate', item.audio_bitrate || '');
    form.append('to_library', item.toLibrary ? 'true' : 'false');
    const _authTok = localStorage.getItem('vdl_auth_token');
    fetch('/api/convert/reconvert', { method: 'POST', body: form, headers: { 'X-Device-Id': deviceId(), ...( _authTok ? { Authorization: 'Bearer ' + _authTok } : {}) } })
      .then(r => r.json().then(data => ({ ok: r.ok, status: r.status, data })))
      .then(({ ok, status, data }) => {
        if (ok && data.job_id) {
          item.jobId = data.job_id; item._srcJobId = data.job_id;   // 新任务的源文件即同一份，可继续链式重转
          item.status = 'running'; item.progress = 30;
          musEnsurePolling();
          musRender(); resolve(data);
        } else if (status === 410 || status === 404) {
          item._srcJobId = null;
          item.status = 'pending'; item.progress = 0; musRender();
          reject(new Error(data.detail || '源文件已过期'));
        } else {
          item.status = 'failed'; item.errorMsg = data.detail || data.error || ('HTTP ' + status); musRender(); reject(new Error(item.errorMsg));
        }
      })
      .catch(err => { item.status = 'failed'; item._srcJobId = null; item.errorMsg = '网络错误'; musRender(); reject(err); });
  });

  const musPollAll = async () => {
    const running = musState.list.filter(x => x.status === 'running' && x.jobId);
    // 「活口」守卫：只要还有上传中 / 已开转码但 job_id 未返回的项，就绝不能停表。
    // 2026-09-29 踩坑：批量开始后第一个 tick（1.5s）发现「无 running 项」就自杀式停表，
    // 而此时文件还在上传、job_id 还没回来 → 之后没人再重启轮询，UI 永久卡在「转码中 30%」
    // （服务端其实 16s 就转完了，线上实证 0 次 /api/convert/{id} 轮询）。
    const live = musState.list.some(x => x.status === 'uploading' || x.status === 'running');
    if (!running.length && !live) { musStopPolling(); return; }
    await Promise.all(running.map(async (it) => {
      try {
        const st = await request('/api/convert/' + it.jobId);
        if (st.status === 'running') {
          const p = typeof st.progress === 'number' ? st.progress : 0;
          it.progress = Math.max(30, Math.min(100, Math.round(30 + p * 0.7)));
          it.stage = st.stage || '排队中';
          musRender();
        } else if (st.status === 'completed') {
          it.status = 'completed'; it.progress = 100;
          it.outputName = musBuildOutputName(it);
          it.downloadUrl = `${window.VDL_API_BASE || ''}/api/convert/${it.jobId}/file?device=${encodeURIComponent(deviceId())}`;
          it.libraryId = st.library_id || null;
          musRender();
        } else if (st.status === 'failed') {
          it.status = 'failed'; it.errorMsg = st.error || '未知错误'; musRender();
        }
      } catch (_e) { /* 单个轮询失败忽略 */ }
    }));
  };

  const musCancelUpload = (it) => {
    it._removed = true;
    if (it._xhrs) it._xhrs.forEach(x => { try { x.abort(); } catch (e) { /* ignore */ } });
    if (it._uploadId) {
      const fd = new FormData();
      fd.append('upload_id', it._uploadId);
      fetch('/api/upload-chunk/abort', { method: 'POST', body: fd, headers: { 'X-Device-Id': deviceId() } }).catch(() => { /* 失败靠 24h 孤儿清理兜底 */ });
    }
  };

  // 事件绑定
  el.musAddBtn.addEventListener('click', () => {
    if (musDesktopNative()) {
      window.VDL.desktop.chooseFiles().then(list => { if (list && list.length) musAddFiles(list); }).catch(() => {});
    } else {
      el.musFileInput.click();
    }
  });
  el.musFileInput.addEventListener('change', () => {
    if (el.musFileInput.files && el.musFileInput.files.length) { musAddFiles(el.musFileInput.files); el.musFileInput.value = ''; }
  });
  el.musList.addEventListener('change', (e) => {
    const t = e.target;
    if (t.dataset.act === 'target') {
      const li = t.closest('.uc-item'); const id = +li.dataset.id;
      const it = musState.list.find(x => x.id === id);
      if (it) { it.target = t.value; musRender(); }
    }
  });
  el.musList.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-act]'); if (!btn) return;
    const li = btn.closest('.uc-item'); const id = +li.dataset.id;
    const it = musState.list.find(x => x.id === id); if (!it) return;
    const act = btn.dataset.act;
    if (act === 'remove') {
      musCancelUpload(it);
      musState.list = musState.list.filter(x => x.id !== id);
      musRender();
    } else if (act === 'start') {
      if (it.status === 'failed') { it.status = 'uploaded'; it.errorMsg = ''; it.progress = 0; it.jobId = null; }
      musEnsurePolling();
      if (it.localPath) {
        if (it.status !== 'uploaded') { it.status = 'uploaded'; it.progress = 30; it.stage = '本地文件'; musRender(); }
        musFinishOne(it).catch(() => {});
      } else if (it._srcJobId) {
        // 优先免重传：服务端还保留着源文件（2h 内），按新格式直接重转；过期则回退重传
        musReconvert(it).catch(() => {
          if (it.file) musUploadOne(it).then(() => musFinishOne(it)).catch(() => {});
        });
      } else if (it._uploadId) {
        musFinishOne(it).catch(() => {});            // 已上传（含重新转码）
      } else {
        musUploadOne(it).then(() => musFinishOne(it)).catch(() => {});  // 未上传/分片已被 finish 消化：先上传再转
      }
    } else if (act === 'reedit') {
      // 重新编辑：已完成行恢复可编辑（可改格式/重新转码），保留旧结果下载链接直到新结果产出。
      // 优先免重传：记下源任务 job_id，服务端保留的源文件 2h 内可直接重转；
      // 分片在 finish 合并时已被服务端删除（p.unlink），_uploadId 必须清掉（复用必报「分片不完整 (0/1)」）。
      it._srcJobId = it.jobId || null;
      it.status = it.localPath ? 'uploaded' : 'pending';
      it.errorMsg = ''; it.jobId = null; it.progress = it.localPath ? 30 : 0; it.stage = it.localPath ? '本地文件' : '';
      it._uploadId = null; it._totalChunks = null; it._xhrs = null;
      musRender();
    }
  });
  el.musClearBtn.addEventListener('click', () => {
    musState.list.forEach(it => musCancelUpload(it));
    musState.list = []; musRender(); el.musStatus.textContent = '';
  });
  el.musBulkApplyBtn.addEventListener('click', () => {
    const b = { target: el.musBulkTarget.value || 'mp3', audio_bitrate: el.musBulkBitrate.value || '', toLibrary: el.musBulkLibrary.checked };
    let n = 0;
    musState.list.forEach(it => {
      if (['pending', 'uploaded', 'failed'].includes(it.status)) { it.target = b.target; it.audio_bitrate = b.audio_bitrate; it.toLibrary = b.toLibrary; n++; }
    });
    musRender();
    el.musStatus.textContent = n ? `已应用到 ${n} 个项` : '没有可应用的项（所有项都已开始/完成）';
  });
  el.musStartAllBtn.addEventListener('click', () => {
    musState.list.forEach(it => { if (it.status === 'failed') { it.status = 'uploaded'; it.errorMsg = ''; it.progress = 0; it.jobId = null; } });
    const wait = musState.list.filter(x => x.status === 'uploaded' || x.status === 'pending');
    if (!wait.length) { el.musStatus.textContent = '没有可开始的项（先添加文件）'; return; }
    el.musStatus.textContent = `批量转换中…（${wait.length} 个）`;
    musEnsurePolling();
    wait.forEach(it => {
      if (it.localPath) {
        musFinishOne(it).catch(() => {});            // 本地文件免上传
      } else if (it.status === 'uploaded') {
        musFinishOne(it).catch(() => {});            // 已上传（如重新转码）
      } else {
        musUploadOne(it).then(() => musFinishOne(it)).catch(() => {});  // 网页文件：先上传再 finish
      }
    });
  });

  // ===== 图片格式转换（独立 tab，LGPL ffmpeg 单帧转码 PNG/JPG/WebP/BMP/TIFF/GIF）=====
  const IMG_FMTS = ['png', 'jpg', 'webp', 'bmp', 'tiff', 'gif'];
  const IMG_CHUNK_SIZE = 32 * 1024 * 1024;
  const imgState = { list: [], nextId: 1, pollTimer: null };

  const imgDesktopNative = () => !!(window.VDL && window.VDL.desktop && typeof window.VDL.desktop.chooseFiles === 'function');
  const imgFormatSize = (b) => {
    if (b >= 1024 * 1024 * 1024) return (b / 1024 / 1024 / 1024).toFixed(2) + ' GB';
    if (b >= 1024 * 1024) return (b / 1024 / 1024).toFixed(1) + ' MB';
    if (b >= 1024) return (b / 1024).toFixed(0) + ' KB';
    return b + ' B';
  };
  const imgBuildOutputName = (it) => {
    const base = (it.name || (it.file && it.file.name) || 'image').replace(/\.[^.]+$/, '');
    return `[${it.target.toUpperCase()}]${base}.${it.target}`;
  };
  const imgEnsurePolling = () => {
    if (imgState.pollTimer) return;
    imgState.pollTimer = setInterval(imgPollAll, UC_POLL_INTERVAL || 1500);
  };
  const imgStopPolling = () => { if (imgState.pollTimer) { clearInterval(imgState.pollTimer); imgState.pollTimer = null; } };

  const imgRender = () => {
    const list = imgState.list;
    el.imgCount.textContent = list.length ? `已添加 ${list.length} 张图片` : '尚未添加图片';
    el.imgClearBtn.hidden = list.length === 0;
    el.imgStartAllBtn.disabled = !list.some(it => it.status === 'uploaded' || it.status === 'failed' || it.status === 'pending');
    if (!list.length) { el.imgList.innerHTML = ''; return; }
    el.imgList.innerHTML = list.map(it => {
      const statusText = {
        pending: '未开始',
        uploading: `上传中 ${it.progress || 0}%${it.speedText ? ' · ' + it.speedText : ''}${it.uploadedText ? ' · ' + it.uploadedText : ''}`,
        uploaded: '已上传，待转换',
        running: it.stage === '排队中' ? '排队中…' : (it.progress ? `转换中 ${it.progress}%` : '转换中…'),
        completed: '完成 ✅',
        failed: '失败：' + (it.errorMsg || ''),
      }[it.status] || it.status;
      const statusCls = it.status === 'pending' ? '' : 'is-' + it.status.replace('uploading', 'running');
      const disabled = !['pending', 'failed', 'uploading', 'uploaded'].includes(it.status) ? 'disabled' : '';
      const progressHtml = (it.status === 'running' || it.status === 'uploading')
        ? `<div class="progress"><div class="progress-fill" style="width:${it.progress || 0}%"></div></div>` : '';
      const downloadHtml = it.status === 'completed' && it.downloadUrl
        ? `<a class="uc-item-download" href="${it.downloadUrl}" download="${it.outputName || 'converted'}">下载</a>${it.libraryId ? ' · 已存媒体库' : ''}`
        : '';
      const startHtml = it.status === 'uploaded'
        ? `<button type="button" class="uc-item-start" data-act="start" title="用该行已设置的格式开始转换">开始转换</button>`
        : it.status === 'failed'
          ? `<button type="button" class="uc-item-start" data-act="start" title="清除错误状态，按当前格式重新转换">重新转换</button>`
          : '';
      const targetDisabled = (it.status === 'running' || it.status === 'completed') ? 'disabled' : '';
      const displayName = it.name || (it.file && it.file.name) || '未命名';
      const ctrl = [];
      if (it.resize) ctrl.push(`≤${it.resize}px`);
      if (it.quality) ctrl.push(`Q${it.quality}`);
      const ctrlText = ctrl.length ? ` · ${ctrl.join(' · ')}` : '';
      const metaSpans = it.localPath
        ? `<span style="color:var(--brand);font-size:12px;">本地文件 · 免上传</span><span>→ ${it.target.toUpperCase()}${ctrlText}</span>`
        : (it.file ? `<span>${imgFormatSize(it.file.size)}</span><span>→ ${it.target.toUpperCase()}${ctrlText}</span>` : `<span>→ ${it.target.toUpperCase()}${ctrlText}</span>`);
      const opts = IMG_FMTS.map(v => `<option value="${v}"${v === it.target ? ' selected' : ''}>${v.toUpperCase()}</option>`).join('');
      return `<li class="uc-item ${statusCls}" data-id="${it.id}">
        <div class="uc-item-main">
          <div class="uc-item-name" title="${displayName}">${displayName}</div>
          <div class="uc-item-meta">${metaSpans}</div>
          ${progressHtml}
          <div class="uc-item-status">${statusText}</div>
        </div>
        <div class="uc-item-side">
          <label class="sr-only" for="imgItemTarget-${it.id}">输出格式</label>
          <select id="imgItemTarget-${it.id}" data-act="target" ${targetDisabled} title="修改此行的目标格式（开始转换时生效）">${opts}</select>
          ${startHtml}
          ${downloadHtml}
          <button type="button" class="uc-item-remove" data-act="remove" title="从列表移除" ${disabled}>×</button>
        </div>
      </li>`;
    }).join('');
  };

  const imgAddFiles = (list) => {
    const b = {
      target: el.imgBulkTarget.value || 'png',
      quality: parseInt(el.imgBulkQuality.value, 10) || 0,
      resize: parseInt(el.imgBulkResize.value, 10) || 0,
      flatten: el.imgBulkFlatten.checked,
      toLibrary: el.imgBulkLibrary.checked,
    };
    Array.from(list || []).forEach(f => {
      const isLocal = typeof f === 'string';
      imgState.list.push({
        id: imgState.nextId++,
        file: isLocal ? null : f,
        localPath: isLocal ? f : null,
        name: isLocal ? (f.split(/[\\/]/).pop()) : f.name,
        target: b.target, quality: b.quality, resize: b.resize,
        flatten: b.flatten, toLibrary: b.toLibrary,
        status: isLocal ? 'uploaded' : 'pending',
        jobId: null, progress: isLocal ? 30 : 0, stage: isLocal ? '本地文件' : '',
        errorMsg: '', downloadUrl: '', outputName: '', libraryId: null,
        speedText: '', uploadedText: '', _removed: false, _xhrs: null, _uploadId: null,
      });
    });
    imgRender();
    const localCount = imgState.list.filter(x => x.localPath).length;
    const uploadCount = imgState.list.filter(x => x.file).length;
    el.imgStatus.textContent = localCount
      ? `已添加 ${localCount} 张本地图片，可直接开始转换`
      : `已添加 ${uploadCount} 张图片，点「开始转换」上传并转换`;
  };

  // 上传单张图片（32MB 分片，复用 ucUploadChunk）；本地文件跳过上传
  const imgUploadOne = (item) => new Promise((resolve, reject) => {
    if (item.localPath) { item.status = 'uploaded'; item.progress = 30; item.stage = '本地文件'; imgRender(); resolve(); return; }
    item.status = 'uploading'; item.progress = 0; item._removed = false; item._xhrs = new Set(); imgRender();
    const file = item.file;
    const chunkSize = IMG_CHUNK_SIZE;
    const totalChunks = Math.max(1, Math.ceil(file.size / chunkSize));
    const uploadId = item._uploadId = 'img' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
    item._totalChunks = totalChunks;   // finish 提交时要用总分片数（漏设会报「分片不完整」）
    const done = new Set();
    const xhrs = item._xhrs;
    let uploadedBytes = 0;
    const pump = async () => {
      for (let idx = 0; idx < totalChunks; idx++) {
        if (done.has(idx) || item._removed) continue;
        const start = idx * chunkSize, end = Math.min(start + chunkSize, file.size);
        const blob = file.slice(start, end);
        try {
          await ucUploadChunk(uploadId, idx, totalChunks, blob, null, xhrs);
        } catch (e1) {
          if (item._removed) { reject(new Error('已取消')); return; }
          try { await ucUploadChunk(uploadId, idx, totalChunks, blob, null, xhrs); }
          catch (e2) { item.status = 'failed'; item.errorMsg = '上传失败：' + (e2 && e2.message || '网络错误'); imgRender(); reject(e2); return; }
        }
        done.add(idx);
        uploadedBytes += (end - start);
        item.progress = Math.min(99, Math.round(uploadedBytes / file.size * 100));
        item.uploadedText = `${done.size}/${totalChunks}`;
        imgRender();
      }
      if (item._removed) { reject(new Error('已取消')); return; }
      item.status = 'uploaded'; item.progress = 30; item.stage = '已上传'; item.uploadedText = ''; imgRender(); resolve();
    };
    pump();
  });

  // 提交转换：本地文件走 /api/convert/local（is_image=true），网页文件走分片 finish（is_image=true）。
  // gif 目标在图片 tab 是「图片→gif 单帧」，必须带 is_image 区分视频 tab 的「视频→动图」。
  const imgFinishOne = (item) => new Promise((resolve, reject) => {
    if (!item || item.status !== 'uploaded') { reject(new Error('状态不允许开始转换')); return; }
    item.status = 'running'; item.progress = 30; item.stage = ''; imgRender();

    if (item.localPath) {
      request('/api/convert/local', {
        method: 'POST',
        body: JSON.stringify({
          local_path: item.localPath,
          target: item.target,
          resolution: '',
          bitrate: '',
          audio: true,
          rotate: 0,
          remux: false,
          to_library: !!item.toLibrary,
          image_quality: item.quality || 0,
          resize: item.resize || 0,
          flatten_alpha: item.flatten !== false,
          is_image: true,
        }),
        headers: { 'Content-Type': 'application/json' },
      }).then(data => {
        if (data.job_id) {
          item.jobId = data.job_id; item.status = 'running'; item.progress = 30;
          imgEnsurePolling();   // 双保险：job_id 到手立刻确保轮询在跑
          imgRender(); resolve(data);
        } else {
          item.status = 'failed'; item.errorMsg = data.detail || data.error || '本地转换请求失败'; imgRender(); reject(new Error(item.errorMsg));
        }
      }).catch(err => {
        item.status = 'failed'; item.errorMsg = (err && err.message) || '本地转换请求失败'; imgRender(); reject(err);
      });
      return;
    }

    const form = new FormData();
    form.append('upload_id', item._uploadId);
    form.append('total', item._totalChunks || 1);
    form.append('filename', item.file ? item.file.name : (item.name || 'image'));
    form.append('target', item.target);
    form.append('audio', 'false');
    form.append('audio_bitrate', '');
    form.append('resolution', '');
    form.append('bitrate', '');
    form.append('rotate', '0');
    form.append('remux', 'false');
    form.append('to_library', item.toLibrary ? 'true' : 'false');
    form.append('image_quality', String(item.quality || 0));
    form.append('resize', String(item.resize || 0));
    form.append('flatten_alpha', item.flatten === false ? 'false' : 'true');
    form.append('is_image', 'true');
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/upload-chunk/finish');
    xhr.setRequestHeader('X-Device-Id', deviceId());
    const _authTok = localStorage.getItem('vdl_auth_token');   // 2026-09-29 服务端功能门禁：finish 须登录
    if (_authTok) xhr.setRequestHeader('Authorization', 'Bearer ' + _authTok);
    xhr.timeout = 120000;
    xhr.addEventListener('load', () => {
      try {
        const data = JSON.parse(xhr.responseText || '{}');
        if (xhr.status >= 200 && xhr.status < 300 && data.job_id) {
          item.jobId = data.job_id; item.status = 'running'; item.progress = 30;
          imgEnsurePolling();   // 双保险：job_id 到手立刻确保轮询在跑
          imgRender(); resolve(data);
        } else {
          item.status = 'failed';
          item.errorMsg = data.detail || data.error || ('HTTP ' + xhr.status);
          imgRender(); reject(new Error(item.errorMsg));
        }
      } catch (e) {
        item.status = 'failed'; item.errorMsg = '服务器响应异常（可能是网络/代理超时）'; imgRender(); reject(e);
      }
    });
    xhr.addEventListener('error', () => { item.status = 'failed'; item.errorMsg = '网络错误'; imgRender(); reject(new Error('network')); });
    xhr.addEventListener('timeout', () => { item.status = 'failed'; item.errorMsg = '响应超时（请重试）'; imgRender(); reject(new Error('timeout')); });
    xhr.send(form);
  });

  const imgPollAll = async () => {
    const running = imgState.list.filter(x => x.status === 'running' && x.jobId);
    // 「活口」守卫（与 musPollAll 同源修法）：上传中/finish 在途时绝不停表，防首个 tick 自杀
    const live = imgState.list.some(x => x.status === 'uploading' || x.status === 'running');
    if (!running.length && !live) { imgStopPolling(); return; }
    await Promise.all(running.map(async (it) => {
      try {
        const st = await request('/api/convert/' + it.jobId);
        if (st.status === 'running') {
          const p = typeof st.progress === 'number' ? st.progress : 0;
          it.progress = Math.max(30, Math.min(100, Math.round(30 + p * 0.7)));
          it.stage = st.stage || '排队中';
          imgRender();
        } else if (st.status === 'completed') {
          it.status = 'completed'; it.progress = 100;
          it.outputName = imgBuildOutputName(it);
          it.downloadUrl = `${window.VDL_API_BASE || ''}/api/convert/${it.jobId}/file?device=${encodeURIComponent(deviceId())}`;
          it.libraryId = st.library_id || null;
          imgRender();
        } else if (st.status === 'failed') {
          it.status = 'failed'; it.errorMsg = st.error || '未知错误'; imgRender();
        }
      } catch (_e) { /* 单个轮询失败忽略 */ }
    }));
  };

  const imgCancelUpload = (it) => {
    it._removed = true;
    if (it._xhrs) it._xhrs.forEach(x => { try { x.abort(); } catch (e) { /* ignore */ } });
    if (it._uploadId) {
      const fd = new FormData();
      fd.append('upload_id', it._uploadId);
      fetch('/api/upload-chunk/abort', { method: 'POST', body: fd, headers: { 'X-Device-Id': deviceId() } }).catch(() => { /* 失败靠 24h 孤儿清理兜底 */ });
    }
  };

  // 事件绑定
  el.imgAddBtn.addEventListener('click', () => {
    if (imgDesktopNative()) {
      window.VDL.desktop.chooseFiles().then(list => { if (list && list.length) imgAddFiles(list); }).catch(() => {});
    } else {
      el.imgFileInput.click();
    }
  });
  el.imgFileInput.addEventListener('change', () => {
    if (el.imgFileInput.files && el.imgFileInput.files.length) { imgAddFiles(el.imgFileInput.files); el.imgFileInput.value = ''; }
  });
  el.imgList.addEventListener('change', (e) => {
    const t = e.target;
    if (t.dataset.act === 'target') {
      const li = t.closest('.uc-item'); const id = +li.dataset.id;
      const it = imgState.list.find(x => x.id === id);
      if (it) { it.target = t.value; imgRender(); }
    }
  });
  el.imgList.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-act]'); if (!btn) return;
    const li = btn.closest('.uc-item'); const id = +li.dataset.id;
    const it = imgState.list.find(x => x.id === id); if (!it) return;
    const act = btn.dataset.act;
    if (act === 'remove') {
      imgCancelUpload(it);
      imgState.list = imgState.list.filter(x => x.id !== id);
      imgRender();
    } else if (act === 'start') {
      if (it.status === 'failed') { it.status = 'uploaded'; it.errorMsg = ''; it.progress = 0; it.jobId = null; }
      imgEnsurePolling();
      imgFinishOne(it).catch(() => {});
    }
  });
  el.imgClearBtn.addEventListener('click', () => {
    imgState.list.forEach(it => imgCancelUpload(it));
    imgState.list = []; imgRender(); el.imgStatus.textContent = '';
  });
  el.imgBulkApplyBtn.addEventListener('click', () => {
    const b = {
      target: el.imgBulkTarget.value || 'png',
      quality: parseInt(el.imgBulkQuality.value, 10) || 0,
      resize: parseInt(el.imgBulkResize.value, 10) || 0,
      flatten: el.imgBulkFlatten.checked,
      toLibrary: el.imgBulkLibrary.checked,
    };
    let n = 0;
    imgState.list.forEach(it => {
      if (['pending', 'uploaded', 'failed'].includes(it.status)) {
        it.target = b.target; it.quality = b.quality; it.resize = b.resize;
        it.flatten = b.flatten; it.toLibrary = b.toLibrary; n++;
      }
    });
    imgRender();
    el.imgStatus.textContent = n ? `已应用到 ${n} 个项` : '没有可应用的项（所有项都已开始/完成）';
  });
  el.imgStartAllBtn.addEventListener('click', () => {
    imgState.list.forEach(it => { if (it.status === 'failed') { it.status = 'uploaded'; it.errorMsg = ''; it.progress = 0; it.jobId = null; } });
    const wait = imgState.list.filter(x => x.status === 'uploaded' || x.status === 'pending');
    if (!wait.length) { el.imgStatus.textContent = '没有可开始的项（先添加图片）'; return; }
    el.imgStatus.textContent = `批量转换中…（${wait.length} 张）`;
    imgEnsurePolling();
    wait.forEach(it => {
      if (it.localPath) {
        imgFinishOne(it).catch(() => {});            // 本地文件免上传
      } else if (it.status === 'uploaded') {
        imgFinishOne(it).catch(() => {});            // 已上传（如重新转换）
      } else {
        imgUploadOne(it).then(() => imgFinishOne(it)).catch(() => {});  // 网页文件：先上传再 finish
      }
    });
  });

  // ===== 本地视频字幕提取（faster-whisper ASR，MIT；VAD 逐句精准分段 → SRT/TXT）=====
  const sbDesktopNative = () => !!(window.VDL && window.VDL.desktop && typeof window.VDL.desktop.chooseFiles === 'function');
  const sbState = { jobId: null, timer: null, path: '', name: '', srtName: '', txtName: '', file: null };
  const sbSetStatus = (text) => { el.sbStatus.textContent = text; };
  const sbStopPolling = () => { if (sbState.timer) { clearInterval(sbState.timer); sbState.timer = null; } };

  const sbSetFile = (pathOrName) => {
    // 仅接受桌面 chooseFiles 返回的本地绝对路径（ASR 直接读本机文件，免上传）
    if (typeof pathOrName !== 'string' || !pathOrName) return;
    sbState.path = pathOrName;
    sbState.file = null;
    sbState.name = pathOrName.split(/[\\/]/).pop();
    sbState.jobId = null;
    el.sbFileLabel.textContent = sbState.name;
    el.sbStartBtn.disabled = false;
    el.sbResult.hidden = true;
    sbSetStatus('');
  };

  // 网页版：选择文件 → 分片上传 → /api/subtitle/finish（2026-09-11 新增）
  const sbSetWebFile = (f) => {
    sbState.file = f;
    sbState.path = '';
    sbState.name = f.name;
    sbState.jobId = null;
    el.sbFileLabel.textContent = `${f.name}（${ucFormatSize(f.size)}·网页上传）`;
    el.sbStartBtn.disabled = false;
    el.sbResult.hidden = true;
    sbSetStatus('');
  };

  // 分片上传所选音/视频并提交字幕任务：32MB/片 × 并发，上传完调 finish 合并+提交
  const sbUploadAndStart = () => {
    const file = sbState.file;
    if (!file) return;
    el.sbResult.hidden = true;
    el.sbProgressWrap.hidden = false;
    el.sbProgressFill.style.width = '0%';
    sbSetStatus('上传中…');
    const chunkSize = 32 * 1024 * 1024;
    const totalChunks = Math.max(1, Math.ceil(file.size / chunkSize));
    const uploadId = 'sb' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
    const xhrs = new Set();
    const done = new Set();
    let uploadedBytes = 0;
    const pump = async () => {
      for (let idx = 0; idx < totalChunks; idx++) {
        if (done.has(idx)) continue;
        const start = idx * chunkSize, end = Math.min(start + chunkSize, file.size);
        const blob = file.slice(start, end);
        try { await ucUploadChunk(uploadId, idx, totalChunks, blob, null, xhrs); }
        catch (e1) {
          try { await ucUploadChunk(uploadId, idx, totalChunks, blob, null, xhrs); }
          catch (e2) { sbSetStatus('上传失败：' + (e2 && e2.message || '网络错误')); return; }
        }
        done.add(idx);
        uploadedBytes += (end - start);
        el.sbProgressFill.style.width = Math.min(9, Math.round(uploadedBytes / file.size * 9)) + '%';
        sbSetStatus(`上传中 ${done.size}/${totalChunks} 片`);
      }
      sbSetStatus('提交识别任务…');
      const fd = new FormData();
      fd.append('upload_id', uploadId);
      fd.append('total', String(totalChunks));
      fd.append('filename', file.name || 'video.mp4');
      fd.append('model_size', el.sbModel.value || 'small');
      fd.append('language', el.sbLang.value || '');
      fd.append('fast', el.sbFast && el.sbFast.checked ? 'true' : 'false');
      const xhr = new XMLHttpRequest();
      xhr.open('POST', (window.VDL_API_BASE || '') + '/api/subtitle/finish');
      xhr.setRequestHeader('X-Device-Id', deviceId());
      const _authTok = localStorage.getItem('vdl_auth_token');   // 2026-09-29 服务端功能门禁：finish 须登录
      if (_authTok) xhr.setRequestHeader('Authorization', 'Bearer ' + _authTok);
      xhr.timeout = 120000;
      xhr.addEventListener('load', () => {
        try {
          const data = JSON.parse(xhr.responseText || '{}');
          if (xhr.status >= 200 && xhr.status < 300 && data.job_id) {
            sbState.jobId = data.job_id;
            sbSetStatus('排队中…');
            if (sbState.timer) clearInterval(sbState.timer);
            sbState.timer = setInterval(sbPoll, 2000);
          } else {
            el.sbProgressWrap.hidden = true;
            sbSetStatus('提交失败：' + (data.detail || data.error || ('HTTP ' + xhr.status)));
          }
        } catch (_e) { sbSetStatus('提交失败：响应解析错误'); }
      });
      xhr.addEventListener('error', () => { sbSetStatus('提交失败：网络错误'); });
      xhr.send(fd);
    };
    pump();
  };

  const sbPoll = async () => {
    if (!sbState.jobId) return;
    try {
      const st = await request('/api/subtitle/' + sbState.jobId);
      if (st.status === 'running') {
        el.sbProgressWrap.hidden = false;
        el.sbProgressFill.style.width = (st.progress || 0) + '%';
        sbSetStatus(`${st.stage || '处理中'} ${st.progress || 0}%`);
      } else if (st.status === 'completed') {
        sbStopPolling();
        el.sbProgressWrap.hidden = true;
        el.sbProgressFill.style.width = '100%';
        sbState.srtName = st.srt_name || 'subtitle.srt';
        sbState.txtName = st.txt_name || 'subtitle.txt';
        el.sbMeta.textContent = `共 ${st.lines || 0} 句 · 语言 ${st.language || 'auto'} · ${st.cpu_threads || 4} 线程`;
        el.sbResult.hidden = false;
        sbSetStatus('完成 ✅');
      } else if (st.status === 'failed') {
        sbStopPolling();
        el.sbProgressWrap.hidden = true;
        sbSetStatus('失败：' + (st.error || '未知错误'));
      }
    } catch (_e) { /* 单次轮询失败忽略 */ }
  };

  // 桌面端走原生保存面板（WKWebView 拦截 <a download>）；网页端降级 blob
  const sbSave = async (kind) => {
    if (!sbState.jobId) return;
    const base = (kind === 'srt' ? sbState.srtName : sbState.txtName) || ('subtitle.' + kind);
    const suggested = base.replace(/\.[^.]+$/, '') + '.' + kind;
    try {
      // 设备隔离：必须带 X-Device-Id（与提交时一致），否则后端 404「任务不存在」
      const resp = await fetch(`${window.VDL_API_BASE || ''}/api/subtitle/${sbState.jobId}/file?kind=${kind}&device=${encodeURIComponent(deviceId())}`,
        { headers: { 'X-Device-Id': deviceId() } });
      if (!resp.ok) { sbSetStatus('下载失败：HTTP ' + resp.status); return; }
      const text = await resp.text();
      if (window.pywebview && window.pywebview.api && typeof window.pywebview.api.save_text_file_dialog === 'function') {
        await window.pywebview.api.save_text_file_dialog(text, suggested);
      } else {
        const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = suggested;
        a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 5000);
      }
    } catch (e) { sbSetStatus('下载失败：' + (e && e.message || e)); }
  };

  el.sbPickBtn.addEventListener('click', () => {
    if (sbDesktopNative()) {
      window.VDL.desktop.chooseFiles().then(list => {
        if (list && list.length) sbSetFile(list[0]);
      }).catch(() => {});
    } else {
      el.sbFileInput.click();
    }
  });
  el.sbFileInput.addEventListener('change', () => {
    const f = el.sbFileInput.files && el.sbFileInput.files[0];
    if (f) sbSetWebFile(f);   // 2026-09-11：网页模式走分片上传，不再禁用
  });
  el.sbStartBtn.addEventListener('click', () => {
    if (sbState.file) { sbUploadAndStart(); return; }   // 网页：上传后提交
    if (!sbState.path) return;
    el.sbResult.hidden = true;
    el.sbProgressWrap.hidden = false;
    el.sbProgressFill.style.width = '0%';
    sbSetStatus('提交中…');
    request('/api/subtitle/extract', {
      method: 'POST',
      body: JSON.stringify({
        local_path: sbState.path,
        model_size: el.sbModel.value || 'small',
        language: el.sbLang.value || '',
        fast: !!(el.sbFast && el.sbFast.checked),
      }),
      headers: { 'Content-Type': 'application/json' },
    }).then(data => {
      sbState.jobId = data.job_id;
      sbSetStatus('排队中…');
      if (sbState.timer) clearInterval(sbState.timer);
      sbState.timer = setInterval(sbPoll, 2000);
    }).catch(err => sbSetStatus('提交失败：' + (err && err.message || err)));
  });
  el.sbDlSrt.addEventListener('click', () => sbSave('srt'));
  el.sbDlTxt.addEventListener('click', () => sbSave('txt'));
  // 右上角「?」：悬停即显示 SRT/TXT 区别说明（移开即收；滑入说明框可继续阅读）
  if (el.sbHelpBtn && el.sbHelpText) {
    let sbHelpTimer = null;
    const sbHelpShow = (show) => {
      el.sbHelpText.hidden = !show;
      el.sbHelpBtn.setAttribute('aria-expanded', show ? 'true' : 'false');
    };
    el.sbHelpBtn.addEventListener('mouseenter', () => { clearTimeout(sbHelpTimer); sbHelpShow(true); });
    el.sbHelpBtn.addEventListener('mouseleave', () => { sbHelpTimer = setTimeout(() => sbHelpShow(false), 250); });
    el.sbHelpText.addEventListener('mouseenter', () => clearTimeout(sbHelpTimer));
    el.sbHelpText.addEventListener('mouseleave', () => sbHelpShow(false));
  }

  // ======================================================================
  // ===== 个人中心（网页版轻量版，2026-09-11）：登录/注册 + 会员状态 =====
  // ======================================================================

  const pfToken = () => localStorage.getItem('vdl_auth_token') || '';
  const pfAuthHeaders = () => (pfToken() ? { 'Authorization': 'Bearer ' + pfToken() } : {});
  let pfAuthIsRegister = false;

  // —— 子导航：个人资料 / 购买记录 / 积分流水 / 账号安全（2026-09-30 对齐 App 个人中心）——
  let _pfPanelName = 'overview';
  const pfPanelNodes = () => ({
    overview: el.pfPanelOverview, purchases: el.pfPanelPurchases,
    credits: el.pfPanelCredits, security: el.pfPanelSecurity,
  });
  const pfShowPanel = (name) => {
    const nodes = pfPanelNodes();
    if (!nodes[name]) name = 'overview';
    _pfPanelName = name;
    Object.keys(nodes).forEach((k) => { if (nodes[k]) nodes[k].hidden = (k !== name); });
    if (el.pfSubnav) {
      el.pfSubnav.querySelectorAll('.pf-subnav-btn').forEach((b) => {
        b.classList.toggle('is-active', b.dataset.pfpanel === name);
      });
    }
  };

  // —— 个人中心辅助（对齐 App：时间/套餐名/类型/流水 delta/脱敏）——
  const pfFmtDate = (ts, withTime) => {
    if (!ts) return '--';
    try {
      const d = new Date(ts * 1000);
      if (withTime) {
        const pad = (n) => String(n).padStart(2, '0');
        return `${d.getFullYear()}/${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
      }
      return d.toLocaleDateString('zh-CN');
    } catch (_e) { return String(ts); }
  };
  const pfPlanName = (code) => {
    const MAP = {
      download_month: '下载会员·月卡', download_half_year: '下载会员·180天', download_quarter: '下载会员·季卡', download_year: '下载会员·年卡',
      ai_5500: 'AI会员·积分包', ai_15000: 'AI会员·月卡', ai_40000: 'AI会员·季卡', ai_150000: 'AI会员·年卡',
      credits_5000: '积分包 5000', credits_15000: '积分包 15000', credits_50000: '积分包 50000',
    };
    return MAP[code] || code || '未知套餐';
  };
  const pfPurchaseType = (code) => {
    if (!code) return '其他';
    if (code.startsWith('download_')) return '下载会员';
    if (code.startsWith('ai_')) return 'AI 会员';
    if (code.startsWith('credits_')) return '积分包';
    return '其他';
  };
  const pfCreditDelta = (h) => {
    if (h.type === 'spend') return -Number(h.amount || 0);
    if (h.type === 'admin_adjust') {
      const m = String(h.code || '').match(/admin_adjust:([+-]?\d+)/);
      return m ? parseInt(m[1], 10) : 0;
    }
    return 0;
  };

  // —— 使用统计表 ——
  let _pfUsagePeriod = 'today';
  const pfRenderUsage = (features, period) => {
    if (!el.pfUsageTable) return;
    const headers = { today: '体验剩余', '3d': '近三日使用', '7d': '近七日使用', month: '本月使用' };
    if (el.pfUsageQuotaHeader) el.pfUsageQuotaHeader.textContent = headers[period] || headers.today;
    if (el.pfUsageFilter) {
      el.pfUsageFilter.querySelectorAll('.pf-chip').forEach((btn) => {
        btn.classList.toggle('is-active', btn.dataset.pfperiod === period);
      });
    }
    const tbody = el.pfUsageTable.querySelector('tbody');
    const list = Array.isArray(features) && features.length ? features : [];
    if (!list.length) { tbody.innerHTML = '<tr><td colspan="4" class="pf-empty-cell">暂无使用</td></tr>'; return; }
    const isPeriod = period !== 'today';
    tbody.innerHTML = list.map((f) => {
      const name = escHtml(f.name || f.key || '—');
      const unit = escHtml(f.unit || '');
      let quota = '—';
      if (f.unlimited) {
        quota = '<span class="pu-tag pu-tag-unlimited">不限</span>';
      } else if (isPeriod) {
        quota = `<span class="pu-num">${Number(f.period_used || 0)} ${unit}</span>`;
      } else if (f.daily_limit != null && f.daily_limit >= 0) {
        const rem = f.daily_remaining != null ? f.daily_remaining : Math.max(0, f.daily_limit - (f.daily_used || 0));
        quota = `<span class="pu-tag">今日剩余</span><span class="pu-num">${rem} / ${f.daily_limit} ${unit}</span>`;
      }
      const balance = f.balance != null ? `<span class="pu-num">${f.balance} ${unit}</span>` : '—';
      let cost = '—';
      if (f.credit_cost != null && f.credit_cost > 0) {
        cost = `<span class="pu-num">${f.credit_cost}</span> 积分/${unit}`;
      } else if (!f.unlimited && f.credit_cost === 0) {
        cost = '<span class="pu-tag pu-tag-free">免费</span>';
      }
      return `<tr><td>${name}</td><td>${quota}</td><td>${balance}</td><td>${cost}</td></tr>`;
    }).join('');
  };

  // —— 购买记录（类型筛选）——
  let _pfPurchaseFilter = 'all';
  let _pfPurchaseCache = [];
  let _pfMemberStatus = null;
  const pfPurchaseFilterMatch = (h, filter) => {
    if (filter === 'all') return true;
    const code = h.code || '';
    if (filter === 'download') return code.startsWith('download_');
    if (filter === 'ai') return code.startsWith('ai_');
    if (filter === 'credits') return code.startsWith('credits_');
    return true;
  };
  const pfRenderPurchases = () => {
    if (!el.pfPurchases) return;
    const filter = _pfPurchaseFilter || 'all';
    if (el.pfPurchasesFilter) {
      el.pfPurchasesFilter.querySelectorAll('.pf-chip').forEach((btn) => {
        btn.classList.toggle('is-active', btn.dataset.pfilter === filter);
      });
    }
    const filtered = (_pfPurchaseCache || []).filter((h) => pfPurchaseFilterMatch(h, filter));
    const HEAD = '<table class="pf-table"><thead><tr><th>时间</th><th>商品名称</th><th>类型</th><th>权益到期</th><th>来源</th></tr></thead>';
    if (!filtered.length) {
      const emptyMsg = (_pfPurchaseCache && _pfPurchaseCache.length) ? '没有符合条件的订单' : '暂无订单记录';
      el.pfPurchases.innerHTML = `${HEAD}<tbody><tr><td colspan="5" class="pf-empty-cell">${escHtml(emptyMsg)}</td></tr></tbody></table>`;
      return;
    }
    const dl = (_pfMemberStatus && _pfMemberStatus.download_member) || {};
    const ai = (_pfMemberStatus && _pfMemberStatus.ai_member) || {};
    const rows = filtered.map((h) => {
      const t = h.at ? pfFmtDate(h.at, true) : '—';
      const name = escHtml(pfPlanName(h.code));
      const kind = escHtml(pfPurchaseType(h.code));
      const via = h.via === 'ui_test' ? '激活码' : escHtml(h.via || '—');
      let expire = '—';
      if (h.code && h.code.startsWith('download_')) {
        expire = dl.active ? `至 ${pfFmtDate(dl.expire_at)}` : '已过期';
      } else if (h.code && h.code.startsWith('ai_')) {
        expire = ai.active ? `至 ${pfFmtDate(ai.expire_at)}` : '已过期';
      } else if (h.code && h.code.startsWith('credits_')) {
        expire = '永久';
      }
      return `<tr><td>${escHtml(t)}</td><td>${name}</td><td>${kind}</td><td>${escHtml(expire)}</td><td>${via}</td></tr>`;
    }).join('');
    el.pfPurchases.innerHTML = `${HEAD}<tbody>${rows}</tbody></table>`;
  };

  // —— 积分流水 ——
  const pfRenderCreditsLog = (list) => {
    if (!el.pfCreditsLog) return;
    const HEAD = '<table class="pf-table"><thead><tr><th>时间</th><th>变动积分</th><th>变动后余额</th><th>类型</th><th>业务 / 备注</th></tr></thead>';
    if (!list || !list.length) { el.pfCreditsLog.innerHTML = `${HEAD}<tbody><tr><td colspan="5" class="pf-empty-cell">暂无积分流水</td></tr></tbody></table>`; return; }
    const rows = list.map((h) => {
      const t = h.at ? pfFmtDate(h.at, true) : '—';
      const delta = pfCreditDelta(h);
      const deltaTxt = (delta > 0 ? '+' : '') + delta;
      const cls = delta > 0 ? 'pf-num plus' : (delta < 0 ? 'pf-num minus' : 'pf-num');
      const balance = h.balance_after != null ? Number(h.balance_after) : '—';
      let kind = '其他';
      if (h.type === 'spend') kind = '消耗';
      else if (h.type === 'admin_adjust') kind = delta >= 0 ? '充值' : '扣减';
      const remark = escHtml(h.reason || h.via || '—');
      return `<tr><td>${escHtml(t)}</td><td class="${cls}">${escHtml(deltaTxt)}</td><td class="pf-num">${balance}</td><td>${escHtml(kind)}</td><td>${remark}</td></tr>`;
    }).join('');
    el.pfCreditsLog.innerHTML = `${HEAD}<tbody>${rows}</tbody></table>`;
  };

  // —— 头像 ——
  const pfRenderAvatar = (url) => {
    if (!el.pfAvatarImg) return;
    if (url) { el.pfAvatarImg.src = url; el.pfAvatarImg.hidden = false; if (el.pfAvatarFallback) el.pfAvatarFallback.hidden = true; }
    else { el.pfAvatarImg.removeAttribute('src'); el.pfAvatarImg.hidden = true; if (el.pfAvatarFallback) el.pfAvatarFallback.hidden = false; }
  };

  // —— 主渲染：me（身份）+ member（会员/积分）+ prof（profile 聚合）——
  const pfRender = (me, member, prof) => {
    const logged = !!(me && me.ok);
    el.pfAuthBox.hidden = logged;
    el.pfUserBox.hidden = !logged;
    renderAuthHeader();
    if (!logged) return;
    // 总览头
    if (el.pfName) el.pfName.textContent = me.identifier || me.user_id || '已登录';
    if (el.pfTag) {
      el.pfTag.textContent = me.is_admin ? '👑 超级管理员' : '普通用户';
      el.pfTag.classList.toggle('is-admin', !!me.is_admin);
    }
    const ct = (prof && prof.created_at) || me.created_at || 0;
    if (el.pfCreated) el.pfCreated.textContent = ct ? pfFmtDate(ct, true) : '—';
    pfRenderAvatar(prof && prof.avatar_url);
    if (el.pfSecEmail) el.pfSecEmail.textContent = me.identifier || me.user_id || '—';
    // 会员状态卡
    if (member) {
      const dl = member.download_member || {}, ai = member.ai_member || {};
      const memberRows = [];
      if (dl.active) memberRows.push(`<div class="pf-row"><span>下载会员</span><span>${escHtml('至 ' + pfFmtDate(dl.expire_at))}</span></div>`);
      if (ai.active) memberRows.push(`<div class="pf-row"><span>AI 会员</span><span>${escHtml('至 ' + pfFmtDate(ai.expire_at))}</span></div>`);
      if (el.pfMemberCardList) {
        el.pfMemberCardList.innerHTML = memberRows.join('');
        el.pfMemberCardList.hidden = !memberRows.length;
      }
      if (el.pfMemberNone) el.pfMemberNone.hidden = !!memberRows.length;
      // 积分分池
      const aiLeft = Number(ai.credits_left != null ? ai.credits_left : 0);
      const perm = Number(member.permanent_credits || 0);
      if (el.pfCreditsTotal) el.pfCreditsTotal.textContent = String(Number(member.credits_total || 0));
      if (el.pfCreditsAi) el.pfCreditsAi.textContent = String(aiLeft);
      if (el.pfCreditsPerm) el.pfCreditsPerm.textContent = String(perm);
      if (el.pfCreditsAiNote) el.pfCreditsAiNote.textContent = ai.active
        ? `有效期至 ${pfFmtDate(ai.expire_at)}，到期清零`
        : '有效期随 AI 会员到期日，到期清零';
    } else {
      if (el.pfMemberNone) el.pfMemberNone.hidden = false;
      if (el.pfMemberCardList) { el.pfMemberCardList.innerHTML = ''; el.pfMemberCardList.hidden = true; }
      if (el.pfCreditsTotal) el.pfCreditsTotal.textContent = '—';
      if (el.pfCreditsAi) el.pfCreditsAi.textContent = '—';
      if (el.pfCreditsPerm) el.pfCreditsPerm.textContent = '—';
    }
    // 记录三表
    const credits = (prof && prof.credit_history) || [];
    if (prof && prof.ok) {
      let running = Number((member && member.credits_total) || 0);
      for (let i = credits.length - 1; i >= 0; i--) {
        const h = credits[i];
        if (h.balance_after == null) h.balance_after = running;
        running -= pfCreditDelta(h);
      }
    }
    _pfMemberStatus = member || null;
    _pfPurchaseCache = (prof && prof.purchases) || [];
    try { pfRenderUsage(prof && prof.usage_features, (prof && prof.usage_period) || _pfUsagePeriod || 'today'); } catch (e) { console.error('[profile] usage render failed', e); }
    try { pfRenderPurchases(); } catch (e) { console.error('[profile] purchases render failed', e); }
    try { pfRenderCreditsLog(credits); } catch (e) { console.error('[profile] credits render failed', e); }
  };

  const pfLoad = async () => {
    if (!pfToken()) { pfRender(null, null, null); return; }
    let me = null, member = null, prof = null;
    try {
      me = await request('/api/auth/me', { headers: pfAuthHeaders() });
      if (!me.ok) { localStorage.removeItem('vdl_auth_token'); me = null; }
    } catch (_e) { /* ignore */ }
    try { member = await request('/api/member/status', { headers: pfAuthHeaders() }); } catch (_e) { /* ignore */ }
    try { prof = await request('/api/account/profile?usage_period=' + encodeURIComponent(_pfUsagePeriod || 'today'), { headers: pfAuthHeaders() }); } catch (_e) { /* ignore */ }
    pfRender(me, member, prof);
  };

  const pfSetAuthStatus = (t) => { el.pfAuthStatus.textContent = t || ''; };

  const pfDoAuth = async () => {
    const ident = el.pfIdentifier.value.trim();
    const pw = el.pfPassword.value;
    if (!ident || !pw) { pfSetAuthStatus('请填写账号和密码'); return; }
    pfSetAuthStatus(pfAuthIsRegister ? '注册中…' : '登录中…');
    try {
      const ep = pfAuthIsRegister ? '/api/auth/register' : '/api/auth/login';
      const data = await request(ep, {
        method: 'POST',
        body: JSON.stringify({ identifier: ident, password: pw }),
      });
      if (data.ok && data.token) {
        localStorage.setItem('vdl_auth_token', data.token);
        pfSetAuthStatus('');
        pfLoad();
        _replayGatedAction();   // 登录/注册前被门禁拦下的功能按钮，成功后自动补点一次
      } else {
        pfSetAuthStatus(data.error || '操作失败');
      }
    } catch (e) {
      pfSetAuthStatus('请求失败：' + (e && e.message || e));
    }
  };

  el.pfAuthSubmit.addEventListener('click', pfDoAuth);
  el.pfPassword.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') pfDoAuth(); });
  el.pfAuthSwitch.addEventListener('click', () => {
    pfAuthIsRegister = !pfAuthIsRegister;
    el.pfAuthModeTitle.textContent = pfAuthIsRegister ? '注册新账号' : '登录账号';
    el.pfAuthSubmit.textContent = pfAuthIsRegister ? '注册' : '登录';
    el.pfAuthSwitch.textContent = pfAuthIsRegister ? '已有账号？登录' : '没有账号？注册';
    pfSetAuthStatus('');
  });
  el.pfLogoutBtn.addEventListener('click', () => {
    localStorage.removeItem('vdl_auth_token');
    el.pfIdentifier.value = ''; el.pfPassword.value = '';
    if (el.pfChangePwForm) el.pfChangePwForm.hidden = true;
    if (el.pfResetBox) el.pfResetBox.hidden = true;
    pfRender(null, null);
    renderAuthHeader();
  });
  // 子导航切换 + 总览里的「查看订单 / 积分流水」快捷跳转
  if (el.pfSubnav) {
    el.pfSubnav.addEventListener('click', (e) => {
      const btn = e.target.closest('.pf-subnav-btn');
      if (btn && btn.dataset.pfpanel) pfShowPanel(btn.dataset.pfpanel);
    });
  }
  if (el.pfUserBox) {
    el.pfUserBox.addEventListener('click', (e) => {
      const jump = e.target.closest('[data-goto]');
      if (jump && jump.dataset && jump.dataset.goto) pfShowPanel(jump.dataset.goto);
    });
  }
  pfShowPanel('overview');
  // 使用统计周期筛选：只重拉 profile（usage_period 变化）
  if (el.pfUsageFilter) {
    el.pfUsageFilter.addEventListener('click', async (e) => {
      const btn = e.target.closest('.pf-chip');
      if (!btn) return;
      const p = btn.dataset.pfperiod;
      if (!p || p === _pfUsagePeriod) return;
      _pfUsagePeriod = p;
      el.pfUsageFilter.querySelectorAll('.pf-chip').forEach((b) => b.classList.toggle('is-active', b.dataset.pfperiod === p));
      try {
        const prof = await request('/api/account/profile?usage_period=' + encodeURIComponent(p), { headers: pfAuthHeaders() });
        if (prof && prof.ok) pfRenderUsage(prof.usage_features, prof.usage_period || p);
      } catch (_err) { /* 静默：保留当前表格 */ }
    });
  }
  // 购买记录类型筛选（纯前端）
  if (el.pfPurchasesFilter) {
    el.pfPurchasesFilter.addEventListener('click', (e) => {
      const btn = e.target.closest('.pf-chip');
      if (!btn) return;
      const f = btn.dataset.pfilter;
      if (!f || f === _pfPurchaseFilter) return;
      _pfPurchaseFilter = f;
      pfRenderPurchases();
    });
  }
  // 头像：点击选择图片 → base64 上传 /api/account/avatar
  if (el.pfAvatar && el.pfAvatarInput) {
    const pfPickAvatar = () => el.pfAvatarInput.click();
    el.pfAvatar.addEventListener('click', pfPickAvatar);
    el.pfAvatar.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); pfPickAvatar(); } });
    el.pfAvatarInput.addEventListener('change', async () => {
      const f = el.pfAvatarInput.files && el.pfAvatarInput.files[0];
      if (!f) return;
      if (f.size > 2 * 1024 * 1024) { el.pfMemberStatus.textContent = '头像图片不能超过 2MB'; return; }
      el.pfMemberStatus.textContent = '头像上传中…';
      const toDataUrl = (file) => new Promise((resolve, reject) => {
        const r = new FileReader();
        r.onload = () => resolve(String(r.result || ''));
        r.onerror = () => reject(new Error('读取失败'));
        r.readAsDataURL(file);
      });
      try {
        const dataUrl = await toDataUrl(f);
        const data = await request('/api/account/avatar', {
          method: 'POST',
          headers: Object.assign({ 'Content-Type': 'application/json' }, pfAuthHeaders()),
          body: JSON.stringify({ image: dataUrl }),
        });
        if (data.ok) {
          el.pfMemberStatus.textContent = '头像已更新';
          pfRenderAvatar(data.avatar_url || dataUrl);
        } else {
          el.pfMemberStatus.textContent = data.error || '头像上传失败';
        }
      } catch (err) {
        el.pfMemberStatus.textContent = '头像上传失败：' + (err && err.message || err);
      } finally {
        el.pfAvatarInput.value = '';
      }
    });
  }
  el.pfActivateBtn.addEventListener('click', async () => {
    const code = el.pfActivateCode.value.trim();
    if (!code) { el.pfMemberStatus.textContent = '请输入激活码'; return; }
    if (!pfToken()) { el.pfMemberStatus.textContent = '请先登录'; return; }
    el.pfMemberStatus.textContent = '激活中…';
    try {
      const data = await request('/api/member/activate', {
        method: 'POST',
        body: JSON.stringify({ code }),
        headers: pfAuthHeaders(),
      });
      if (data.ok) {
        el.pfMemberStatus.textContent = '激活成功 ✅';
        el.pfActivateCode.value = '';
        pfLoad();
      } else {
        el.pfMemberStatus.textContent = data.error || '激活失败';
      }
    } catch (e) {
      el.pfMemberStatus.textContent = '请求失败：' + (e && e.message || e);
    }
  });

  // ======================================================================
  // ===== 登录强制 + 账号安全（2026-09-28 对齐 App）=====
  //  · 下载必须登录：未登录点下载 → 本地拦截并弹登录框（request() 里挂钩子），
  //    服务端 403 兜底（needLogin 错误在各下载入口静默处理，不重复报错）。
  //  · 右上角账号按钮：未登录弹登录框，已登录跳个人中心。
  //  · 个人中心补齐 App 能力：修改密码 / 忘记密码（邮箱验证码）/ 注销账号。
  // ======================================================================

  // —— 右上角账号按钮（未登录「登录 / 注册」，已登录「👤 账号」跳个人中心）——
  const renderAuthHeader = () => {
    if (!el.authHeaderBtn) return;
    if (pfToken()) {
      el.authHeaderBtn.textContent = '👤 账号';
      el.authHeaderBtn.title = '已登录，点击查看账号详情';
    } else {
      el.authHeaderBtn.textContent = '登录 / 注册';
      el.authHeaderBtn.title = '登录 / 注册账号（下载需登录）';
    }
  };

  // —— 登录弹窗（下载被拦 / 右上角入口共用）——
  let amIsRegister = false;
  const openAuthModal = (hint) => {
    if (el.authModalHint && hint) el.authModalHint.textContent = hint;
    if (el.amStatus) el.amStatus.textContent = '';
    try { el.authModal.showModal(); } catch (_e) { /* 已打开 */ }
    renderAuthHeader();
  };
  // request() 定义早于本段，经 window 钩子解耦调用
  window.__vdlOpenAuthModal = () => openAuthModal('下载需要登录账号；免费账号每日 10 次下载额度，注册即得。');

  const closeAuthModal = () => { try { el.authModal.close(); } catch (_e) { /* 本来就没开 */ } };
  const amSetStatus = (t) => { el.amStatus.textContent = t || ''; };

  const amDoAuth = async () => {
    const ident = el.amIdentifier.value.trim();
    const pw = el.amPassword.value;
    if (!ident || !pw) { amSetStatus('请填写账号和密码'); return; }
    amSetStatus(amIsRegister ? '注册中…' : '登录中…');
    try {
      const ep = amIsRegister ? '/api/auth/register' : '/api/auth/login';
      const data = await request(ep, { method: 'POST', body: JSON.stringify({ identifier: ident, password: pw }) });
      if (data.ok && data.token) {
        localStorage.setItem('vdl_auth_token', data.token);
        amSetStatus('');
        closeAuthModal();
        renderAuthHeader();
        pfLoad();
        _replayGatedAction();   // 登录/注册前被门禁拦下的功能按钮，成功后自动补点一次
      } else {
        amSetStatus(data.error || '操作失败');
      }
    } catch (e) {
      amSetStatus('请求失败：' + (e && e.message || e));
    }
  };
  el.amSubmit.addEventListener('click', amDoAuth);
  el.amPassword.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') amDoAuth(); });
  el.amSwitch.addEventListener('click', () => {
    amIsRegister = !amIsRegister;
    el.authModalTitle.textContent = amIsRegister ? '注册新账号' : '登录账号';
    el.amSubmit.textContent = amIsRegister ? '注册' : '登录';
    el.amSwitch.textContent = amIsRegister ? '已有账号？登录' : '没有账号？注册';
    amSetStatus('');
  });
  el.authModalClose.addEventListener('click', closeAuthModal);
  if (el.authHeaderBtn) {
    el.authHeaderBtn.addEventListener('click', () => {
      if (pfToken()) switchView('profile'); else openAuthModal();
    });
  }
  renderAuthHeader();

  // —— 功能级登录门禁（2026-09-29 对齐 App 2026-09-26 版本：所有功能须登录才能使用）——
  // 捕获阶段委托拦截「真正执行」的按钮：捕获先于按钮自身 bubble 监听，stopPropagation
  // 即可整体阻断原 handler，不必逐个改既有 click 绑定。下载另有 request()/服务端 403 兜底。
  let _loginPromptAt = 0;      // 并发节流：3 秒内只弹一次登录框
  let _pendingGatedId = '';    // 登录前想用的功能按钮 id，登录成功后自动补点一次
  const _notifyNeedLogin = (msg) => {
    const now = Date.now();
    if (now - _loginPromptAt < 3000) return;
    _loginPromptAt = now;
    openAuthModal(msg || '请先登录或注册账号后使用该功能');
  };
  // 只挂「真正执行（会产出结果）」的按钮；前置步骤（解析链接、选择文件）不弹登录 —— 与 App 决策一致。
  const _LOGIN_GATED_ACTIONS = {
    batchBtn: '批量下载',
    downloadBtn: '下载',
    ucStartAllBtn: '视频格式转换',
    musStartAllBtn: '音乐格式转换',
    imgStartAllBtn: '图片格式转换',
    mcMergeBtn: '视频/音频拼接',
    sbStartBtn: '本地字幕提取',
    dwImgBtn: '图片去水印',
    dwPdfBtn: 'PDF 去水印',
    subExtract: '字幕提取',
    subBurn: '字幕烧录',
    comGenerateScript: '视频解说',
    comScriptRender: '解说渲染成片',
    subAddBtn: '订阅追更',
    torAddBtn: '种子下载',
    processRun: '队列处理',
    libBatchProcess: '媒体库批量处理',
    libCleanup: '媒体库自动清理',
  };
  const _LOGIN_GATE_SELECTOR = Object.keys(_LOGIN_GATED_ACTIONS).map((id) => '#' + id).join(',');
  document.addEventListener('click', (e) => {
    try {
      const t = e.target;
      if (!t || typeof t.closest !== 'function') return;
      const hit = t.closest(_LOGIN_GATE_SELECTOR);
      if (!hit) return;
      if (pfToken()) return;                   // 已登录：放行，走原有逻辑
      const label = _LOGIN_GATED_ACTIONS[hit.id] || '该功能';
      _pendingGatedId = hit.id;
      e.preventDefault();
      e.stopPropagation();                     // 阻断按钮自身的 click handler
      _notifyNeedLogin('请先登录或注册账号，即可使用' + label);
    } catch (_e) { /* 守卫异常不阻塞页面 */ }
  }, true);
  // 登录成功后自动补点一次之前被拦下的功能按钮（对齐 App「登录后继续」体验）
  const _replayGatedAction = () => {
    const id = _pendingGatedId;
    _pendingGatedId = '';
    if (!id) return;
    setTimeout(() => {
      const node = document.getElementById(id);
      if (node && !node.disabled) { try { node.click(); } catch (_e) {} }
    }, 150);
  };

  // —— 账号安全：修改密码（需当前密码；服务端会把新密码同步云端授权中心）——
  const pfSecStatus = (t) => { el.pfSecurityStatus.textContent = t || ''; };
  // 账号安全：改密表单折叠开关（对齐 App「更改密码」按钮展开表单）
  if (el.pfChangePwToggle && el.pfChangePwForm) {
    el.pfChangePwToggle.addEventListener('click', () => {
      el.pfChangePwForm.hidden = !el.pfChangePwForm.hidden;
      if (!el.pfChangePwForm.hidden && el.pfResetBox) el.pfResetBox.hidden = true;
      pfSecStatus('');
    });
  }
  if (el.pfChangePwCancel && el.pfChangePwForm) {
    el.pfChangePwCancel.addEventListener('click', () => { el.pfChangePwForm.hidden = true; pfSecStatus(''); });
  }
  el.pfChangePwBtn.addEventListener('click', async () => {
    if (!pfToken()) { pfSecStatus('请先登录'); return; }
    const cur = el.pfCurPw.value;
    const nw = el.pfNewPw.value;
    if (!cur || !nw) { pfSecStatus('请输入当前密码和新密码'); return; }
    if (nw.length < 6) { pfSecStatus('新密码至少 6 位'); return; }
    pfSecStatus('提交中…');
    try {
      const data = await request('/api/auth/change-password', {
        method: 'POST',
        body: JSON.stringify({ current_password: cur, new_password: nw }),
        headers: pfAuthHeaders(),
      });
      if (data.ok) {
        pfSecStatus('密码已修改 ✅' + (data.notice ? '（' + data.notice + '）' : ''));
        el.pfCurPw.value = '';
        el.pfNewPw.value = '';
        if (el.pfChangePwForm) el.pfChangePwForm.hidden = true;
      } else {
        pfSecStatus(data.error || '修改失败');
      }
    } catch (e) {
      pfSecStatus('请求失败：' + (e && e.message || e));
    }
  });

  // —— 忘记密码：邮箱验证码重置（未登录也可用）——
  let pfResetCountdown = 0;
  el.pfForgotBtn.addEventListener('click', () => {
    el.pfResetBox.hidden = !el.pfResetBox.hidden;
    if (!el.pfResetBox.hidden) {
      if (el.pfChangePwForm) el.pfChangePwForm.hidden = true;
      if (!el.pfResetIdent.value && el.pfIdentifier.value) {
        el.pfResetIdent.value = el.pfIdentifier.value;
      }
    }
  });
  el.pfResetSendBtn.addEventListener('click', async () => {
    const ident = el.pfResetIdent.value.trim();
    if (!ident) { pfSecStatus('请输入注册时的邮箱 / 手机号'); return; }
    pfSecStatus('验证码发送中…');
    try {
      const data = await request('/api/auth/reset-code', {
        method: 'POST', body: JSON.stringify({ identifier: ident }),
      });
      if (data.ok) {
        pfSecStatus('验证码已发送，请查收邮箱（5 分钟内有效）');
        pfResetCountdown = 60;
        el.pfResetSendBtn.disabled = true;
        const timer = setInterval(() => {
          pfResetCountdown -= 1;
          if (pfResetCountdown <= 0) {
            clearInterval(timer);
            el.pfResetSendBtn.disabled = false;
            el.pfResetSendBtn.textContent = '发送验证码';
          } else {
            el.pfResetSendBtn.textContent = `重发(${pfResetCountdown}s)`;
          }
        }, 1000);
      } else {
        pfSecStatus(data.error || '发送失败');
      }
    } catch (e) {
      pfSecStatus('请求失败：' + (e && e.message || e));
    }
  });
  el.pfResetSubmit.addEventListener('click', async () => {
    const ident = el.pfResetIdent.value.trim();
    const code = el.pfResetCode.value.trim();
    const pw = el.pfResetPw.value;
    if (!ident || !code || !pw) { pfSecStatus('请填写账号、验证码和新密码'); return; }
    if (pw.length < 6) { pfSecStatus('新密码至少 6 位'); return; }
    pfSecStatus('重置中…');
    try {
      const data = await request('/api/auth/reset', {
        method: 'POST', body: JSON.stringify({ identifier: ident, code, password: pw }),
      });
      if (data.ok) {
        pfSecStatus('密码已重置，请用新密码重新登录');
        el.pfResetBox.hidden = true;
        // 旧 token 可能已失效，强制重新登录
        localStorage.removeItem('vdl_auth_token');
        el.pfCurPw.value = '';
        el.pfNewPw.value = '';
        renderAuthHeader();
        pfRender(null, null);
      } else {
        pfSecStatus(data.error || '重置失败');
      }
    } catch (e) {
      pfSecStatus('请求失败：' + (e && e.message || e));
    }
  });

  // —— 注销账号（软删除，二次确认；对齐 App 的危险区）——
  el.pfDeactivateBtn.addEventListener('click', async () => {
    if (!pfToken()) { pfSecStatus('请先登录'); return; }
    const ok = await showConfirm('确定注销账号吗？注销后将无法使用该邮箱登录或重新注册，会员与积分一并失效，此操作不可恢复。', { okText: '确认注销', danger: true });
    if (!ok) return;
    pfSecStatus('注销中…');
    try {
      const data = await request('/api/account/deactivate', { method: 'POST', headers: pfAuthHeaders() });
      if (data.ok) {
        localStorage.removeItem('vdl_auth_token');
        el.pfIdentifier.value = '';
        el.pfPassword.value = '';
        el.pfCurPw.value = '';
        el.pfNewPw.value = '';
        pfRender(null, null);
        renderAuthHeader();
        pfSecStatus('');
      } else {
        pfSecStatus(data.error || '注销失败');
      }
    } catch (e) {
      pfSecStatus('请求失败：' + (e && e.message || e));
    }
  });

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
  if (el.tabLibrary) el.tabLibrary.addEventListener('click', () => switchView('library'));
  if (el.tabCommentary) el.tabCommentary.addEventListener('click', () => switchView('commentary'));
  el.tabUploadConvert.addEventListener('click', () => switchView('uploadconvert'));
  // —— 2026-09-11 新增 tab：音乐转换 / 图片转换 / AI 字幕 / 个人中心 ——
  if (el.tabMusicConvert) el.tabMusicConvert.addEventListener('click', () => switchView('musicconvert'));
  if (el.tabImageConvert) el.tabImageConvert.addEventListener('click', () => switchView('imageconvert'));
  if (el.tabSubtitle) el.tabSubtitle.addEventListener('click', () => switchView('subtitle'));
  if (el.tabProfile) el.tabProfile.addEventListener('click', () => switchView('profile'));
  // 视频处理板块内：格式转换 / 拼接 两个并列子模块切换
  const ucSwitchSub = (which) => {
    const fmt = which === 'format';
    el.ucPane.hidden = !fmt;
    el.mcPane.hidden = fmt;
    el.ucSubFormat.classList.toggle('is-active', fmt);
    el.ucSubMerge.classList.toggle('is-active', !fmt);
  };
  el.ucSubFormat.addEventListener('click', () => ucSwitchSub('format'));
  el.ucSubMerge.addEventListener('click', () => ucSwitchSub('merge'));
  el.tabDw.addEventListener('click', () => switchView('dw'));
  el.tabAppIntro.addEventListener('click', () => switchView('appIntro'));
  if (el.tabSubscribe) el.tabSubscribe.addEventListener('click', () => switchView('subscribe'));
  if (el.tabTorrent) el.tabTorrent.addEventListener('click', () => switchView('torrent'));
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

  // 节点信息获取（含重试）：跨境链路 / CF 边缘抖动很常见，而单次失败就永久退化
  // 成「本机直连」代价极大 —— 海外站会直接不可用（国内节点到不了 YouTube）。
  // 故重试两次再认输；仍失败时还有上面的 localStorage 回填兜底。
  const fetchNodes = async (attempt = 0) => {
    try {
      return await request('/api/nodes');
    } catch (err) {
      if (attempt >= 2) throw err;
      await new Promise((r) => setTimeout(r, 700 * (attempt + 1)));
      return fetchNodes(attempt + 1);
    }
  };

  fetchNodes()
    .then(({ region, peer, china_domains: domains, commentary_enabled, ads_enabled, convert, download, library, subscriptions, retention, crypto, torrent, ai_dewatermark, authRequired, profile }) => {
      node.authRequired = !!authRequired;
      if (node.authRequired && !localStorage.getItem('vdl_api_token')) {
        const t = (typeof prompt === 'function') ? prompt('该服务已启用访问令牌，请输入 API Token：') : null;
        if (t && t.trim()) localStorage.setItem('vdl_api_token', t.trim());
      }
      node.region = region || 'global';
      node.peer = peer || '';
      // 落盘：下次打开（或本次请求失败）时先回填，避免退化成「全部走本机」。
      // 服务器真正降级为单节点时会返回空 peer，此处会同步清掉缓存，不会残留误判。
      try {
        localStorage.setItem('vdl_peer', node.peer);
        localStorage.setItem('vdl_region', node.region);
      } catch (e) { /* 隐私模式可能抛错 */ }
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
      node.libraryEnabled = !!(library && library.enabled);
      node.subscriptionsEnabled = !!(subscriptions && subscriptions.enabled);
      node.retentionEnabled = !!(retention && retention.enabled);
      node.trashAvailable = !!(retention && retention.trash_available);
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
      if (el.libCrypto) el.libCrypto.hidden = !node.cryptoEnabled;
      if (el.libShowQueue) el.libShowQueue.hidden = !node.libraryEnabled;
      node.profile = profile;
      // —— Route B：网页精简版（profile=web）按 profile 隐藏 App 专属 tab ——
      // 2026-09-11 起网页版扩展为八大入口（新增音乐/图片转换、AI 字幕、个人中心），
      // 此处仍隐藏未移植的 App 专属 tab，不受后端 profile 影响。
      [
      'tabLibrary', 'tabCommentary', 'tabSubscribe', 'tabTorrent'
      ].forEach(id => { const t = document.getElementById(id); if (t) t.hidden = true; });
      if (el.tabDownload) el.tabDownload.hidden = false;
      if (el.tabUploadConvert) el.tabUploadConvert.hidden = false;
      if (el.tabDw) el.tabDw.hidden = false;
      if (el.tabAppIntro) el.tabAppIntro.hidden = false;
      if (el.tabMusicConvert) el.tabMusicConvert.hidden = false;
      if (el.tabImageConvert) el.tabImageConvert.hidden = false;
      if (el.tabSubtitle) el.tabSubtitle.hidden = false;
      if (el.tabProfile) el.tabProfile.hidden = false;
      el.tabs.hidden = false; // 导航栏始终显示
      // 默认视图：始终停在下载（支持 #view=xxx 直达指定视图，如 #view=subtitle）
      const _hashView = (location.hash || '').replace(/^#view=/, '');
      switchView(_hashView || 'download');
      bootViewSet = true; // 标记初始化已设置视图，阻止 setTimeout 兜底覆盖
      initSubUI();
      paintNodeBar();
    })
    .catch(() => {
      /* 取不到节点信息就退回单节点，全部走本机。
         2026-09-21：此处必须补一次 applyWebTabs() —— 导航栏不能再因为这一次失败而消失。 */
      try { applyWebTabs(); } catch (_) {}
    });

  // 对端信息定期回填（2026-09-27）：标签页常年不关是常态（实测有页面开着 10 小时以上），
  // 服务器侧新增/变更对端不会自动反映到页面内存里，会一直以「本机直连」把海外站
  // 发到国内节点而必然失败。每 10 分钟静默刷新一次，并在切回本标签页时立刻刷一次
  // （多标签/移动端切前后台最常触发）。只在真正变化时重绘线路条，平时零副作用。
  const refreshNodeInfo = async () => {
    try {
      const { region, peer } = await request('/api/nodes');
      const nextPeer = peer || '';
      const nextRegion = region || 'global';
      const changed = (nextPeer !== node.peer) || (nextRegion !== node.region);
      node.peer = nextPeer;
      node.region = nextRegion;
      try {
        localStorage.setItem('vdl_peer', nextPeer);
        localStorage.setItem('vdl_region', nextRegion);
      } catch (e) { /* ignore */ }
      if (changed) paintNodeBar();
    } catch (e) { /* 静默：刷新失败保留现状，不影响已能用的链路 */ }
  };
  setInterval(refreshNodeInfo, 10 * 60 * 1000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshNodeInfo(); });
  // 兜底默认视图（节点信息未加载时）：停在核心下载视图，两个 profile 都不会 404。
  // 支持 #view=xxx 直达（音乐转换/图片转换/字幕/个人中心等）
  try {
    const _hashView0 = (location.hash || '').replace(/^#view=/, '');
    switchView(_hashView0 || 'download');
    bootViewSet = true;
  } catch (_) {}
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
