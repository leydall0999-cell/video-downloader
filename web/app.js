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
    engineBadge: $('engineBadge'),
    template: $('taskTemplate'),
    modal: $('platformModal'),
    modalGrid: $('platformModalGrid'),
    modalTitle: $('platformModalTitle'),
    modalClose: $('platformModalClose'),
    cookieInput: $('cookieInput'),
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
    sidebar: $('sidebar'),
    tabDownload: $('tabDownload'),
    sTabDownload: $('sTabDownload'),
    sTabLibrary: $('sTabLibrary'),
    sTabCommentary: $('sTabCommentary'),
    sTabUploadConvert: $('sTabUploadConvert'),
    sTabDw: $('sTabDw'),
    sTabBridge: $('sTabBridge'),
    sTabSubscribe: $('sTabSubscribe'),
    sTabTorrent: $('sTabTorrent'),
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
    cryptoSetMsg: $('cryptoSetMsg'),
    cryptoUnlockMsg: $('cryptoUnlockMsg'),
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
    llmReasoningEffort: $('llmReasoningEffort'),
    llmOffpeakOnly: $('llmOffpeakOnly'),
    llmSave: $('llmSave'),
    llmStatus: $('llmStatus'),
    // 视觉模型（片头检测 & 视觉理解共用）
    visionProvider: $('visionProvider'),
    visionApiKey: $('visionApiKey'),
    visionBaseUrl: $('visionBaseUrl'),
    visionModel: $('visionModel'),
    visionNote: $('visionNote'),
    visionRuntime: $('visionRuntime'),
    visionSignup: $('visionSignup'),
    visionSignupWrap: $('visionSignupWrap'),
    visionSave: $('visionSave'),
    visionStatus: $('visionStatus'),
    // 格式 / 片段加工（桌面版功能）
    libProcess: $('libProcess'),
    libCommentary: $('libCommentary'),
    libCommentaryStatus: $('libCommentaryStatus'),
    libCommentaryFile: $('libCommentaryFile'),
    // 解说成片独立标签页
    commentaryView: $('commentaryView'),
    comResults: $('comResults'),
    comEmpty: $('comEmpty'),
    comGrid: $('comGrid'),
    comHistory: $('comHistory'),
    comHistoryCount: $('comHistoryCount'),
    comHistoryToolbar: $('comHistoryToolbar'),
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
    comGenerateOneClick: $('comGenerateOneClick'),
    comLoudness: $('comLoudness'),
    comLoudnessVal: $('comLoudnessVal'),
    comLoudnessOff: $('comLoudnessOff'),
    comDuck: $('comDuck'),
    comDuckVal: $('comDuckVal'),
    comBoost: $('comBoost'),
    comBoostVal: $('comBoostVal'),
    comVolSave: $('comVolSave'),
    comVolReset: $('comVolReset'),
    comVolStatus: $('comVolStatus'),
    comVolPreview: $('comVolPreview'),
    comIntroHighlight: $('comIntroHighlight'),
    comIntroOutroMode: () => {
      const v = document.querySelector('input[name="comIntroOutroMode"]:checked');
      return v ? v.value : 'keep_no_narrate';
    },
    comRetainPct: $('comRetainPct'),
    comIntroSec: $('comIntroSec'),
    comOutroSec: $('comOutroSec'),
    comDramaStart: $('comDramaStart'),
    comDramaEnd: $('comDramaEnd'),
    comCorrectTranscript: $('comCorrectTranscript'),
    comVision: $('comVision'),
    comStepsPanel: $('comStepsPanel'),
    comStepsList: $('comStepsList'),
    comLogs: $('comLogs'),
    comRefresh: $('comRefresh'),
    comEnvStatus: $('comEnvStatus'),
    comExportJianying: $('comExportJianying'),
    comExportJianyingDir: $('comExportJianyingDir'),
    comExportJianyingDirWrap: $('comExportJianyingDirWrap'),
    comExportJianyingPick: $('comExportJianyingPick'),
    comTtsStatusBar: $('comTtsStatusBar'),
    comTtsStatusDot: $('comTtsStatusDot'),
    comTtsStatusText: $('comTtsStatusText'),
    comTtsProvider: $('comTtsProvider'),
    comBgm: $('comBgm'),
    comBgmVolume: $('comBgmVolume'),
    comBgmVolumeVal: $('comBgmVolumeVal'),
    comBgmVolWrap: $('comBgmVolWrap'),
    comBgmFileWrap: $('comBgmFileWrap'),
    comBgmFile: $('comBgmFile'),
    comBgmFilePick: $('comBgmFilePick'),
    comSubSize: $('comSubSize'),
    comSubSizeVal: $('comSubSizeVal'),
    comSubBorder: $('comSubBorder'),
    comSubBorderVal: $('comSubBorderVal'),
    comSubColor: $('comSubColor'),
    comSubPreview: $('comSubPreview'),
    comSubAspectHint: $('comSubAspectHint'),
    comMaxChars: $('comMaxChars'),
    comMaxCharsVal: $('comMaxCharsVal'),
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
    bridgeView: $('bridgeView'),
    ucBridgeLink: $('ucBridgeLink'),
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
    tabAppIntro: $('tabAppIntro'),
    appIntroView: $('appIntroView'),
    // AI 解说体验（NarratoAI 本地子进程 + iframe）
    tabNarrato: $('tabNarrato'),
    sTabNarrato: $('sTabNarrato'),
    narratoView: $('narratoView'),
    narratoFrame: $('narratoFrame'),
    narratoLoading: $('narratoLoading'),
    narratoKeyBox: $('narratoKeyBox'),
    narratoKeyInput: $('narratoKeyInput'),
    narratoKeySave: $('narratoKeySave'),
    narratoKeyHint: $('narratoKeyHint'),
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
    dwImgModelField: $('dwImgModelField'),
    dwImgInt8Field: $('dwImgInt8Field'),
    dwImgModel: $('dwImgModel'),
    dwImgInt8: $('dwImgInt8'),
    // 去水印放大弹窗
    dwImgModal: $('dwImgModal'),
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
    // 视频去水印
    dwModeVideo: $('dwModeVideo'),
    dwVideoPane: $('dwVideoPane'),
    dwVidFile: $('dwVidFile'),
    dwVidThumb: $('dwVidThumb'),
    dwVidSvg: $('dwVidSvg'),
    dwVidSelInfo: $('dwVidSelInfo'),
    dwVidClear: $('dwVidClear'),
    dwVidStart: $('dwVidStart'),
    dwVidEnd: $('dwVidEnd'),
    dwVidStartLabel: $('dwVidStartLabel'),
    dwVidEndLabel: $('dwVidEndLabel'),
    dwVidRangeHighlight: $('dwVidRangeHighlight'),
    dwVidRangeWrap: $('dwVidRangeWrap'),
    dwVidAddSeg: $('dwVidAddSeg'),
    dwVidSegList: $('dwVidSegList'),
    dwVidSegSummary: $('dwVidSegSummary'),
    dwVidSegTip: $('dwVidSegTip'),
    dwVidAddKf: $('dwVidAddKf'),
    dwVidAddKfAt: $('dwVidAddKfAt'),
    dwVidKfPanel: $('dwVidKfPanel'),
    dwVidKfList: $('dwVidKfList'),
    dwVidKfTitle: $('dwVidKfTitle'),
    dwVidKfTip: $('dwVidKfTip'),
    dwVidRes: $('dwVidRes'),
    dwVidTargetFps: $('dwVidTargetFps'),
    dwVidStride: $('dwVidStride'),
    dwVidSmooth: $('dwVidSmooth'),
    dwVidModel: $('dwVidModel'),
    dwVidInt8: $('dwVidInt8'),
    dwVidBtn: $('dwVidBtn'),
    dwVidStatus: $('dwVidStatus'),
    dwVidResult: $('dwVidResult'),
    dwVidMain: $('dwVidMain'),
    dwVidWork: $('dwVidWork'),
    dwVidRedo: $('dwVidRedo'),
    dwVidOut: $('dwVidOut'),
    dwVidOrig: $('dwVidOrig'),
    dwVidPlayer: $('dwVidPlayer'),
    dwVidTranscoding: $('dwVidTranscoding'),
    dwVidEmpty: $('dwVidEmpty'),
    dwVidCapOverlay: $('dwVidCapOverlay'),
    dwVidPreviewToggle: $('dwVidPreviewToggle'),
    dwVidPreviewToggleWrap: $('dwVidPreviewToggleWrap'),
    dwVidPlayerHead: $('dwVidPlayerHead'),
    dwVidFilmstripWrap: $('dwVidFilmstripWrap'),
    dwVidFsHint: $('dwVidFsHint'),
    dwVidBackToThumb: $('dwVidBackToThumb'),
    dwVidSetStart: $('dwVidSetStart'),
    dwVidSetEnd: $('dwVidSetEnd'),
    dwVidFilmstrip: $('dwVidFilmstrip'),
    dwVidFilmstripCursor: $('dwVidFilmstripCursor'),
    dwVidDownload: $('dwVidDownload'),
    dwVidPause: $('dwVidPause'),
    dwVidResume: $('dwVidResume'),
    dwVidCancel: $('dwVidCancel'),
    dwVidRunCtrls: $('dwVidRunCtrls'),
    processPanel: $('processPanel'),
    processPanelClose: $('processPanelClose'),
    processOp: $('processOp'),
    processParams: $('processParams'),
    processRun: $('processRun'),
    processStatus: $('processStatus'),
    chips: $('platformChips'),
    tabLibrary: $('tabLibrary'),
    ucLimitTip: $('ucLimitTip'),
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
    archiveEnabled: false,
    cryptoEnabled: false,
    cryptoHasPass: false,
    cryptoLocked: true,
  };
  /** 手动覆盖：null=自动判断，'cn'/'global'=用户强制指定 */
  let forcedRegion = null;
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
      // detail 可能是字符串、FastAPI 422 校验错误数组 [{loc,msg,type}] 或对象——统一压成人话
      let msg = payload.error || payload.detail || '请求失败，请稍后重试';
      if (Array.isArray(msg)) {
        msg = msg.map((d) => `${(d.loc || []).filter((p) => p !== 'body').join('.')}: ${d.msg}`).join('；');
      } else if (msg && typeof msg === 'object') {
        msg = msg.message || JSON.stringify(msg);
      }
      const err = { message: msg, hint: payload.hint || '', category: payload.category || '' };
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

  /**
   * 用本地 WebKit video 元素抽首帧到 JPEG blob，秒级返回，完全不走网络。
   *
   * 2026-08-29 实测大视频（数 GB）场景：thumbnail 接口从前要上传整个文件抽一帧，
   * 黑色画布要等好几 GB 传完才显示。这个本地版本走 WebKit 自带的视频解码器，
   * URL.createObjectURL(file) → <video> → seeked → canvas.drawImage → toBlob，
   * 整个流程 50-300ms 内完成，画面立刻点亮。
   *
   * 失败兜底：
   * - WebKit 解码抛出/timeout（罕见，HEVC/H.264 extreme profile / 损坏 mp4 才会）→
   *   抛错让调用方改走切片上传。
   *
   * 输出最大宽 1280 像素、JPEG 0.86，省内存还秒渲染。
   */
  const grabLocalVideoThumb = (file, signal) => {
    return new Promise((resolve, reject) => {
      // signal 已 abort 就直接返回错误，让调用方走兜底
      if (signal && signal.aborted) return reject(new Error('aborted'));
      const url = URL.createObjectURL(file);
      const v = document.createElement('video');
      v.muted = true;
      v.playsInline = true;
      v.preload = 'auto';
      // 不挂到 DOM（无样式，让浏览器后台解码）
      v.src = url;
      const cleanup = () => { try { URL.revokeObjectURL(url); } catch (_e) {} };
      let settled = false;
      const onAbort = () => { if (settled) return; settled = true; cleanup(); reject(new Error('aborted')); };
      if (signal) signal.addEventListener('abort', onAbort, { once: true });
      const fail = (e) => { if (settled) return; settled = true; if (signal) signal.removeEventListener('abort', onAbort); cleanup(); reject(e); };
      const succeed = (blob, w, h) => { if (settled) return; settled = true; if (signal) signal.removeEventListener('abort', onAbort); cleanup(); resolve({ blob, naturalWidth: w, naturalHeight: h }); };
      const timer = setTimeout(() => fail(new Error('local-thumb-timeout')), 12000);
      v.onerror = () => { clearTimeout(timer); fail(new Error('video-decode-error')); };
      v.onloadedmetadata = () => {
        const w = v.videoWidth, h = v.videoHeight;
        if (!w || !h) { clearTimeout(timer); fail(new Error('zero-dim')); return; }
        // seek 到略偏离 0 的位置，避免某些浏览器对 currentTime=0 的 seek 不触发 'seeked'
        try { v.currentTime = Math.min(0.05, (v.duration || 0.1) / 2); } catch (_e) { /* ignore */ }
        const onSeeked = () => {
          v.removeEventListener('seeked', onSeeked);
          clearTimeout(timer);
          try {
            // 下采样到 ≤1280 宽：4K / 8K 视频首帧不会是几十 MB，省内存秒渲染
            const scale = w > 1280 ? 1280 / w : 1;
            const cw = Math.round(w * scale), ch = Math.round(h * scale);
            const c = document.createElement('canvas');
            c.width = cw; c.height = ch;
            const ctx = c.getContext('2d');
            ctx.drawImage(v, 0, 0, cw, ch);
            // requestVideoFrameCallback 比 setTimeout 准（拿到的是真正"渲染帧"）
            const commit = () => c.toBlob((blob) => {
              if (blob) succeed(blob, w, h);
              else fail(new Error('toBlob-null'));
            }, 'image/jpeg', 0.86);
            if (typeof v.requestVideoFrameCallback === 'function') {
              v.requestVideoFrameCallback(() => commit());
            } else {
              setTimeout(commit, 60);
            }
          } catch (e) { fail(e); }
        };
        v.addEventListener('seeked', onSeeked, { once: true });
      };
    });
  };

  /**
   * Filmstrip 切片上传（20 帧拼图条）。
   * 大视频不再传整个文件，只传前 32 MB：faststart mp4 的关键帧密集，
   * 32 MB 足够 ffmpeg 选到 20 个时间戳抽帧；薄膜（moov 在尾部）罕见失败
   * 不再静默——返回 {ok:false, err:'...'} 让前端在 status 末尾显示。
   */
  const dwVidFetchFilmstrip = async (file, signal) => {
    const base = `${window.VDL_API_BASE || ''}`;
    const SLICE = 32 * 1024 * 1024;
    const slice = file.size > SLICE ? file.slice(0, SLICE) : file;
    const headers = { 'X-Device-Id': deviceId() };
    const tryOnce = async (payload, label) => {
      const fd = new FormData();
      fd.append('file', payload, file.name);
      const r = await fetch(`${base}/api/dw/video/filmstrip?frames=20`, {
        method: 'POST', body: fd, headers, signal,
      });
      if (!r.ok) return { ok: false, err: `${label} ${(await r.text().catch(() => '')) || r.status}` };
      const blob = await r.blob();
      const filmUrl = URL.createObjectURL(blob);
      const frames = parseInt(r.headers.get('X-Filmstrip-Frames') || '0', 10);
      const interval = parseFloat(r.headers.get('X-Filmstrip-Interval') || '0');
      return { ok: true, filmUrl, frames, interval };
    };
    try {
      const first = await tryOnce(slice, 'filmstrip 切片');
      if (first.ok) return first;
      // moov 切片兜不住（罕见）——再发整文件
      return await tryOnce(file, 'filmstrip 全文件');
    } catch (e) {
      return { ok: false, err: e.message || String(e) };
    }
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
    // fetch 超时保护（2026-08-24）：默认 120s（覆盖 VPS worker 85s 上限 + 余量），
    // 防止后端挂起时前端无限等待。大文件上传/下载可传 options.timeout=0 关闭
    // 或传更大值；请求超时抛可读错误而非静默卡死。
    const fetchTimeout = (options && options.timeout) || 120000;
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
  // 桌面端支持直接调用系统文件框选本地路径，跳过分片上传。
  // 注意：desktop-app.js 在 app.js 之后才加载，不能在模块初始化时取 window.VDL.desktop，必须动态检测。
  const ucDesktopNative = () => !!(window.VDL && window.VDL.desktop && typeof window.VDL.desktop.chooseFiles === 'function');
  const UC_MAX_CONCURRENT = 2; // 文件级上传并发
  const UC_CHUNK_SIZE = 32 * 1024 * 1024;       // 单片 32MB（默认）
  const UC_BIG_CHUNK_SIZE = 64 * 1024 * 1024;   // >2GB 文件单片 64MB（减少请求数，后端上限 64MB）
  const UC_CHUNK_CONCURRENCY = 8;               // 单文件分片并发路数（高 RTT 链路多连接并行提速，HTTP/2 无连接限制）
  const UC_CHUNK_RETRIES = 2;                   // 单片失败重试次数（网络抖动自动重传）
  const UC_POLL_INTERVAL = 1500;                // 转码状态轮询间隔 ms（批量/无损直转进度更实时）
  // 双端点混合上传：hanyuxz.top（Cloudflare 免费版对上传 POST 限速 ~5MB/s）与
  // Railway 原生域名（无 CF 限速层，直连源站）指向同一个后端、同一份分片存储，
  // 动态选路：每片发出前按两通道「最近 3 次成功分片平均吞吐」实时选更快通道，
  // 慢通道（跨境抖动/掉速）自然少被选中，不再拖累整体；单通道失败重试自动故障转移到另一通道。
  const UC_UPLOAD_ENDPOINTS = [location.origin, 'https://web-production-b9993.up.railway.app'];
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
    const name = (it.file && it.file.name) || it.name || 'converted';
    const stem = String(name).replace(/\.[^.]+$/, '') || 'converted';
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
  const fmtOptions = (val) => (node.convertTargets && node.convertTargets.length ? node.convertTargets : ['mp4','mov','mkv','webm','avi','flv','ts','m4v','wmv','3gp','mpeg','hevc','mp3','m4a','wav','flac','aac','opus','wma','mp2','gif'])
    .map(v => `<option value="${v}"${v===val?' selected':''}>${v.toUpperCase()}${AUDIO_ONLY_FMTS.includes(v)?'（仅音频）':''}${v==='gif'?'（前5秒）':''}${v==='hevc'?'（H.265 省空间）':''}</option>`)
    .join('');

  const renderUcList = () => {
    const list = ucState.list;
    el.ucCount.textContent = list.length ? `已添加 ${list.length} 个文件` : '尚未添加文件';
    el.ucClearBtn.hidden = list.length === 0;
    // 批量参数区常显；「开始批量转换」按钮：有已上传待转码项才可用（2026-08-23 批量统一开始）
    el.ucStartAllBtn.hidden = false;
    el.ucStartAllBtn.disabled = !list.some(it => it.status === 'uploaded' || it.status === 'failed');
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
      // 待转码 / 失败行：独立「开始转码 / 重新转码」按钮（用该行格式单独开始）
      const startHtml = it.status === 'uploaded'
        ? `<button type="button" class="uc-item-start" data-act="start" title="用该行已设置的格式开始转码">开始转码</button>`
        : it.status === 'failed'
          ? `<button type="button" class="uc-item-start" data-act="start" title="清除错误状态，按当前格式重新转码">重新转码</button>`
          : '';
      // 格式可改：uploading/uploaded 也能改（finish 提交时用最新值）；running/completed 锁定
      const targetDisabled = (it.status === 'running' || it.status === 'completed') ? 'disabled' : '';
      const targetTitle = it.status === 'completed' ? '已完成：格式已固定，如需其他格式请移除后重新添加'
                         : it.status === 'running' ? '转码中不可修改'
                         : '修改此行的目标格式（开始转码时生效）';
      const displayName = it.name || (it.file && it.file.name) || '未命名';
      const metaSpans = it.localPath
        ? `<span class="uc-local-badge" style="color:var(--brand);font-size:12px;">本地文件 · 免上传</span><span>→ ${it.target.toUpperCase()}</span>`
        : (it.file
            ? `<span>${ucFormatSize(it.file.size)}</span><span>→ ${it.target.toUpperCase()}</span>`
            : `<span>→ ${it.target.toUpperCase()}</span>`);
      return `
        <li class="uc-item ${statusCls}" data-id="${it.id}">
          <div class="uc-item-main">
            <div class="uc-item-name" title="${displayName}">${displayName}</div>
            <div class="uc-item-meta">
              ${metaSpans}
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

  const ucAddFiles = (list) => {
    const b = ucReadBulk();
    const hasLocal = ucState.list.some(x => x.localPath);
    const hasUpload = ucState.list.some(x => x.file);
    Array.from(list).forEach(f => {
      const isLocal = typeof f === 'string';
      if (isLocal) {
        if (hasUpload) return;
        const name = f.split(/[\\/]/).pop();
        ucState.list.push({
          id: ucState.nextId++, file: null, localPath: f, name,
          target: b.target, res: b.res, bitrate: b.bitrate,
          audio: b.audio, rotate: b.rotate, remux: b.remux, toLibrary: b.toLibrary,
          status: 'pending', jobId: null, progress: 0,
          errorMsg: '', downloadUrl: '', outputName: '', libraryId: null,
        });
      } else {
        if (hasLocal) return;
        ucState.list.push({
          id: ucState.nextId++, file: f, localPath: null, name: f.name,
          target: b.target, res: b.res, bitrate: b.bitrate,
          audio: b.audio, rotate: b.rotate, remux: b.remux, toLibrary: b.toLibrary,
          status: 'pending', jobId: null, progress: 0,
          errorMsg: '', downloadUrl: '', outputName: '', libraryId: null,
        });
      }
    });
    // 混合添加时过滤上传文件（桌面端本地优先）
    if (ucState.list.some(x => x.localPath) && ucState.list.some(x => x.file)) {
      el.ucStatus.textContent = '暂不支持同时混合本地文件与上传文件，已自动过滤后者';
      ucState.list = ucState.list.filter(x => x.localPath);
    }
    renderUcList();
    const localCount = ucState.list.filter(x => x.localPath).length;
    const uploadCount = ucState.list.filter(x => x.file).length;
    el.ucStatus.textContent = localCount
      ? `已添加 ${localCount} 个本地文件，可直接开始转换`
      : `已添加 ${uploadCount} 个文件，自动开始上传…`;
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
    xhr.timeout = 120000;   // 2 分钟单片超时（防后台 tab 限流/网络静默断网卡死）
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
    xhr.addEventListener('timeout', () => { cleanup(); reject(new Error('分片超时（2 分钟无响应，可能是网络断/后台 tab 限流）')); });
    xhr.addEventListener('abort', () => { cleanup(); reject(new Error('已取消')); });
    xhr.send(form);
  });

  // 上传 + 启动单个 job：32MB 分片 × 4 路并发，进度占 30%（转码从 30% 累加），实时速度显示
  // 桌面端本地文件直接跳过上传，状态置为 uploaded，由 ucFinishOne 调 /api/convert/local。
  const ucUploadOne = (item) => new Promise((resolve, reject) => {
    if (item.localPath) {
      item.status = 'uploaded';
      item.progress = 30;
      item.stage = '本地文件';
      item.speedText = ''; item.uploadedText = '';
      renderUcList();
      resolve({ status: 'uploaded' });
      return;
    }
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
              // 区分超时（2 分钟无响应，多为后台 tab 限流或网络静默断）：建议刷新后保持前台重传
              const hint = /超时/.test(e.message) ? '（建议保持上传页面在前台后重传）' : '';
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
  // 桌面端本地文件直接调 /api/convert/local，跳过分片 finish。
  const ucFinishOne = (item) => new Promise((resolve, reject) => {
    if (!item || item.status !== 'uploaded') { reject(new Error('状态不允许开始转码')); return; }
    item.status = 'running';
    item.progress = 30;
    item.stage = '';
    renderUcList();

    if (item.localPath) {
      request('/api/convert/local', {
        method: 'POST',
        body: JSON.stringify({
          local_path: item.localPath,
          target: item.target,
          resolution: item.res,
          bitrate: item.bitrate || '',
          audio: item.audio,
          rotate: +item.rotate || 0,
          remux: item.remux,
          to_library: item.toLibrary,
        }),
        headers: { 'Content-Type': 'application/json' },
      }).then(data => {
        if (data.job_id) {
          item.jobId = data.job_id;
          item.status = 'running';
          item.progress = 30;
          item.speedText = ''; item.uploadedText = '';
          renderUcList();
          resolve(data);
        } else {
          item.status = 'failed';
          item.errorMsg = data.detail || data.error || '本地转换请求失败';
          renderUcList();
          reject(new Error(item.errorMsg));
        }
      }).catch(err => {
        item.status = 'failed';
        item.errorMsg = (err && err.message) || '本地转换请求失败';
        renderUcList();
        reject(err);
      });
      return;
    }

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
            // finish 是一次性操作（合并时已 unlink 部分 parts），分片无法服务端重试，只能重传
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

  // ===== 视频/音频桥接（合并）：独立面板，桌面端直接传本地路径免上传 =====
  const MC_MAX_CONCURRENT = 3;   // 同时上传/处理 3 个文件
  // 注意：desktop-app.js 在 app.js 之后才加载，不能在模块初始化时取 window.VDL.desktop，必须动态检测。
  const mcDesktopNative = () => !!(window.VDL && window.VDL.desktop && typeof window.VDL.desktop.chooseFiles === 'function');
  // 输出格式按当前模式（视频/音频）动态切换
  const mcVideoFormats = [
    { v: 'mp4', t: 'MP4' }, { v: 'mov', t: 'MOV' }, { v: 'mkv', t: 'MKV' },
    { v: 'm4v', t: 'M4V' }, { v: 'ts', t: 'TS' }, { v: 'flv', t: 'FLV' }, { v: 'webm', t: 'WebM' },
  ];
  const mcAudioFormats = [
    { v: 'mp3', t: 'MP3' }, { v: 'm4a', t: 'M4A' }, { v: 'wav', t: 'WAV' }, { v: 'flac', t: 'FLAC' },
  ];
  const mcState = { list: [], nextId: 1, active: 0, polling: null, mode: 'video' };

  // 根据文件类型判定片段种类（video / audio）
  const _mcKindOf = (name, type) => {
    const n = (name || '').toLowerCase();
    if (/^audio\//i.test(type || '')) return 'audio';
    if (/\.(mp3|m4a|aac|wav|flac|ogg|opus|oga|wma)$/i.test(n)) return 'audio';
    return 'video';
  };
  const _mcNameOf = (f) => (f && f.name) || f || '';
  // 按当前所有非结果项的种类推导模式：全部音频→audio，否则→video
  const _mcComputeMode = () => {
    const items = mcState.list.filter(x => !x.isResult);
    if (items.length && items.every(x => x.kind === 'audio')) return 'audio';
    return 'video';
  };
  // 重建输出格式下拉（保留尽可能旧选择）
  const mcPopulateFormats = (mode) => {
    const list = mode === 'audio' ? mcAudioFormats : mcVideoFormats;
    const cur = mcOutFormat.value;
    mcOutFormat.innerHTML = list.map(o => `<option value="${o.v}">${o.t}</option>`).join('');
    if (list.some(o => o.v === cur)) mcOutFormat.value = cur;
  };
  // 文件集合变化后刷新模式与格式下拉
  const mcUpdateMode = () => {
    const mode = _mcComputeMode();
    if (mode !== mcState.mode) {
      mcState.mode = mode;
      mcPopulateFormats(mode);
      mcStatusEl.textContent = mode === 'audio' ? '已切换为「音频合并」模式（输出音频）' : '已切换为「视频合并」模式（输出视频）';
    }
  };
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
    const modeTxt = mcState.mode === 'audio' ? '（音频模式）' : '';
    mcCountEl.textContent = segs.length ? `已添加 ${segs.length} 个文件${modeTxt}` : '尚未添加文件';
    mcClearBtn.hidden = mcState.list.length === 0;
    const ready = mcState.list.filter(x => x.status === 'uploaded' && !x.isResult).length;
    mcMergeBtn.disabled = ready < 2;
    if (!mcState.list.length) { mcListEl.innerHTML = ''; return; }
    mcListEl.innerHTML = mcState.list.map((it, idx) => {
      const name = it.name || it.outputName || it.label || (it.isResult ? '合并结果' : '未命名文件');
      const statusText = it.isResult
        ? (it.status === 'running'
             ? (it.stage === '拼接中' ? '拼接中…' : (it.progress ? `拼接中 ${it.progress}%` : '拼接中…'))
             : it.status === 'completed' ? '完成 ✅' : '失败：' + (it.errorMsg || ''))
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
        <li class="uc-item ${cls}" data-id="${it.id}">
          <div class="uc-item-main">
            <div class="uc-item-name" title="${name}">${idx + 1}. ${name}</div>
            ${it.file ? `<div class="uc-item-meta"><span>${mcFormatSize(it.file.size)}</span></div>`
        : (it.localPath ? `<div class="uc-item-meta"><span class="uc-local-badge" style="color:var(--brand);font-size:12px;">本地文件 · 免上传</span></div>` : '')}
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

  // 添加文件：支持 FileList（网页/上传）或字符串数组本地绝对路径（桌面端免上传）
  const mcAddFiles = (list) => {
    const hasLocal = mcState.list.some(x => x.localPath);
    const hasUpload = mcState.list.some(x => x.file);
    Array.from(list).forEach(f => {
      const isLocal = typeof f === 'string';
      if (isLocal) {
        if (hasUpload) return; // 忽略：不允许混合
        const name = f.split(/[\\/]/).pop();
        mcState.list.push({ id: mcState.nextId++, name, localPath: f, file: null,
          kind: _mcKindOf(name), status: 'pending', segName: f,
          progress: 0, speedText: '', uploadedText: '', errorMsg: '', downloadUrl: '', outputName: '', jobId: null });
      } else {
        if (hasLocal) return; // 忽略：不允许混合
        const name = _mcNameOf(f);
        mcState.list.push({ id: mcState.nextId++, name, localPath: null, file: f,
          kind: _mcKindOf(name, f.type), status: 'pending', segName: null,
          progress: 0, speedText: '', uploadedText: '', errorMsg: '', downloadUrl: '', outputName: '', jobId: null });
      }
    });
    // 如果存在混合添加被忽略，提示一次
    if (mcState.list.some(x => x.localPath) && mcState.list.some(x => x.file)) {
      mcStatusEl.textContent = '暂不支持同时混合本地文件与上传文件，已自动过滤后者';
      mcState.list = mcState.list.filter(x => x.localPath || x.isResult);
    }
    mcRender();
    mcUpdateMode();
    const localCount = mcState.list.filter(x => x.localPath && !x.isResult).length;
    const uploadCount = mcState.list.filter(x => x.file && !x.isResult).length;
    if (localCount) mcStatusEl.textContent = `已添加 ${localCount} 个本地文件，可直接开始桥接`;
    else if (uploadCount) mcStatusEl.textContent = mcDesktopNative()
      ? `已添加 ${uploadCount} 个文件，自动上传中…（桌面端点「添加文件」按钮选本地文件可免上传）`
      : `已添加 ${uploadCount} 个文件，自动上传…`;
    mcPump();
  };

  // 上传单个片段（复用 ucUploadChunk）；末尾用 finish(mode=store) 落地为拼接素材
  const mcUploadOne = (item) => new Promise((resolve) => {
    item.status = 'uploading'; item.progress = 0; item.speedText = ''; item.uploadedText = '';
    item._removed = false; item._xhrs = new Set(); item._chStats = ucChStats();
    mcRender();
    const file = item.file;
    // 桥接的 HTTP 上传路径：与转码上传保持一致，>2GB 才用 64MB，否则 32MB
    // 桌面端本地文件已走 localPath 跳过上传，不会进入此函数
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
              const hint = /超时/.test(e.message) ? '（建议保持上传页面在前台后重传）' : '';
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
            if (/分片不完整|分片参数|文件超过|合并/.test(msg)) msg += '（请移除后重新添加）';
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
          if (!it.name) it.name = it.outputName;
          it.downloadUrl = `${window.VDL_API_BASE || ''}/api/convert/${it.jobId}/file?device=${encodeURIComponent(deviceId())}`;
          it.libraryId = st.library_id || null;
          mcRender();
        } else if (st.status === 'failed') {
          it.status = 'failed'; it.errorMsg = st.error || '未知错误';
          mcRender();
        }
      } catch (_e) { /* 忽略 */ }
    }));
  };

  const mcPump = () => {
    while (mcState.active < MC_MAX_CONCURRENT) {
      const next = mcState.list.find(x => !x.isResult && x.status === 'pending');
      if (!next) break;
      // 桌面端本地文件直接就绪，无需 HTTP 上传
      if (next.localPath) {
        next.status = 'uploaded';
        next.progress = 30;
        mcRender();
        continue;
      }
      mcState.active++;
      mcUploadOne(next).catch(() => { /* 失败已标记 */ }).finally(() => { mcState.active--; mcPump(); });
    }
  };

  mcAddBtn.addEventListener('click', () => {
    const native = mcDesktopNative();
    if (native) {
      window.VDL.desktop.chooseFiles().then(paths => {
        if (paths && paths.length) mcAddFiles(paths);
      }).catch(() => {});
    } else {
      mcFileInput.click();
    }
  });
  mcFileInput.addEventListener('change', (e) => {
    if (e.target.files && e.target.files.length) mcAddFiles(e.target.files);
    e.target.value = '';
  });
  // 初始化输出格式下拉（默认视频模式）
  mcPopulateFormats('video');
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
    if (!it || it.isResult) return;
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
    if (ready.length < 2) { mcStatusEl.textContent = '至少需要 2 个已就绪的文件'; return; }
    const body = {
      segments: ready.map(x => x.segName),
      out_format: mcOutFormat.value,
      out_name: mcOutName.value || 'merged',
      to_library: mcLibrary.checked,
      audio_only: mcState.mode === 'audio',
    };
    // 本地路径走 /api/concat/local，跳过上传直接合并
    const isLocal = ready.every(x => x.localPath);
    const endpoint = isLocal ? '/api/concat/local' : '/api/concat';
    request(endpoint, { method: 'POST', body: JSON.stringify(body), headers: { 'Content-Type': 'application/json' } })
      .then(data => {
        if (data.job_id) {
          mcState.list.push({ id: mcState.nextId++, isResult: true, label: '合并结果',
            name: mcOutName.value || '', status: 'running',
            jobId: data.job_id, progress: 30, stage: '', downloadUrl: '', outputName: '', errorMsg: '', libraryId: null });
          mcState.polling = setInterval(mcPoll, UC_POLL_INTERVAL);
          mcStatusEl.textContent = '拼接中…';
          mcRender();
        } else {
          mcStatusEl.textContent = data.detail || data.error || '拼接失败';
        }
      })
      .catch(() => { mcStatusEl.textContent = '拼接请求失败，请重试'; });
  });

  // 事件绑定
  el.ucAddBtn.addEventListener('click', () => {
    const native = ucDesktopNative();
    if (native) {
      window.VDL.desktop.chooseFiles().then(list => {
        if (list && list.length) ucAddFiles(list);
      }).catch(() => {});
    } else {
      el.ucFileInput.click();
    }
  });
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
      // 单行「开始转码 / 重新转码」：用该行最新设置的格式单独开始，不影响其他行
      const li = t.closest('.uc-item');
      const it = ucState.list.find(x => x.id === +li.dataset.id);
      if (!it) return;
      // 重置失败项状态 → 再走 ucFinishOne（2026-08-28：用户换格式后没法继续转的根因）
      if (it.status === 'failed') {
        it.status = 'uploaded';
        it.errorMsg = '';
        it.progress = 0;
        it.jobId = null;
        it.speedText = '';
        it.uploadedText = '';
        renderUcList();
      }
      if (it.status === 'uploaded') {
        // ★ 2026-08-28 兜底修复：localPath 路径项没有 it.file 对象，原代码 it.file.name 在用户
        // 走 native chooser 选了本地路径时会抛 null.name → "启动错误: TypeError: null is not an
        // object (evaluating 'it.file.name')"。统一走 it.name（构造时已 basename 兜底）。
        el.ucStatus.textContent = `开始转码：${it.name || (it.file && it.file.name) || '未知文件'}`;
        ucEnsurePolling();   // 立即启动轮询，进度实时可见
        ucFinishOne(it).catch(() => { /* 失败已在 ucFinishOne 标记 */ }).finally(() => ucPump());
      }
    }
  });
  // 转码完成后的「下载」按钮拦截：pywebview (WKWebView) 不会弹 <a download> 保存框，
  // 「下载」按钮拦截委托（转码列表 + 桥接列表共用 .uc-item-download）
  // 优先桌面原生桥接 save_convert_file_dialog，弹原生保存面板；无桥接（web/浏览器）时
  // 回退 fetch + Blob + <a download>。href 格式：/api/convert/{jobId}/file?device=...
  // 桥接（concat）任务与单文件转码任务共用同一份 app.CONVERT_JOBS（同进程内存），
  // 所以 launcher.read CONVERT_JOBS[jobId].out_path 两种场景都直接命中。
  function wireSaveConvertDownload(scopeEl) {
    scopeEl.addEventListener('click', async (e) => {
      const link = e.target.closest && e.target.closest('.uc-item-download');
      if (!link) return;
      const api = window.pywebview && window.pywebview.api;
      const href = link.getAttribute('href') || '';
      const filename = link.getAttribute('download') || 'converted';
      const mJob = href.match(/\/api\/convert\/([^/?#]+)/);
      const jobId = mJob ? mJob[1] : '';
      e.preventDefault();
      if (!jobId) return;
      const orig = link.textContent;

      if (api && api.save_convert_file_dialog) {
        link.textContent = '选择保存位置…';
        try {
          const res = await api.save_convert_file_dialog(jobId, filename);
          if (res === 'CANCELLED') {
            // 用户取消：恢复文案，无副作用
          } else if (typeof res === 'string' && res.startsWith('ERROR:')) {
            alert('保存失败：' + res.replace(/^ERROR:\s*/, ''));
          } else {
            link.textContent = '已保存 ✓';
            setTimeout(() => { link.textContent = orig; }, 4000);
            return;
          }
        } catch (err) {
          alert('保存失败：' + ((err && err.message) || '桥接调用失败'));
        }
        link.textContent = orig;
        return;
      }
      link.textContent = '下载中…';
      try {
        const resp = await fetch(href, { credentials: 'same-origin' });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
        link.textContent = '已触发下载 ✓';
        setTimeout(() => { link.textContent = orig; }, 3000);
      } catch (err) {
        alert('下载失败：' + ((err && err.message) || '网络错误'));
        link.textContent = orig;
      }
    });
  }
  // 单一来源：转码列表 + 桥接列表（mcListEl）两处都挂同一份拦截委托
  wireSaveConvertDownload(el.ucList);
  wireSaveConvertDownload(mcListEl);
  el.ucClearBtn.addEventListener('click', ucClearAll);
  el.ucBulkApplyBtn.addEventListener('click', ucApplyBulk);
  el.ucStartAllBtn.addEventListener('click', () => {
    // 批量开始转码：所有「已上传·待转码 + 失败」行统一重置并提交（2026-08-28 支持失败重试）
    const failed = ucState.list.filter(x => x.status === 'failed');
    failed.forEach(it => {
      it.status = 'uploaded';
      it.errorMsg = '';
      it.progress = 0;
      it.jobId = null;
      it.speedText = '';
      it.uploadedText = '';
    });
    renderUcList();
    const wait = ucState.list.filter(x => x.status === 'uploaded');
    if (!wait.length) { el.ucStatus.textContent = '没有可重试的项（先添加文件上传）'; return; }
    el.ucStatus.textContent = `批量转换中…（${wait.length} 个）`;
    ucEnsurePolling();   // 立即启动轮询，各进度实时可见
    wait.forEach(it => {
      ucFinishOne(it).catch(() => { /* 失败已在 ucFinishOne 标记 */ }).finally(() => ucPump());
    });
  });

  // ------------------------------------------------------------------ 去水印（需求文档模块二）

  // 图片 / PDF / 视频 子模式切换
  const dwSwitchPane = (mode) => {
    el.dwImgPane.hidden = mode !== 'img';
    el.dwPdfPane.hidden = mode !== 'pdf';
    el.dwVideoPane.hidden = mode !== 'video';
    el.dwModeImg.classList.toggle('is-active', mode === 'img');
    el.dwModePdf.classList.toggle('is-active', mode === 'pdf');
    el.dwModeVideo.classList.toggle('is-active', mode === 'video');
    el.dwImgStatus.textContent = '';
    el.dwPdfStatus.textContent = '';
    el.dwVidStatus.textContent = '';
  };
  el.dwModeImg.addEventListener('click', () => dwSwitchPane('img'));
  el.dwModePdf.addEventListener('click', () => dwSwitchPane('pdf'));
  el.dwModeVideo.addEventListener('click', () => dwSwitchPane('video'));

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
    // 阻止 <img> 浏览器原生图像拖拽——mousedown 后浏览器会启动 native image drag（看上去像「移动图片」），
    // 必须 draggable=false + 拦截 dragstart，否则 e.preventDefault() 在 mousedown 里无效。
    el.dwImgPreview.draggable = false;
    el.dwModalImg.draggable = false;
    el.dwImgPreview.addEventListener('dragstart', (e) => e.preventDefault(), { once: false });
    el.dwModalImg.addEventListener('dragstart', (e) => e.preventDefault(), { once: false });
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
    // 注意：选区事件必须绑在 img 上，而不是 canvas。
    // .dw-canvas / .dw-svg 都是 position:absolute + pointer-events:none，叠在 img 上
    // 仅作展示蒙层；它们不接收事件，事件需穿透到下面的 img 才能触发框选。
    // 若误把 mousedown 绑到 canvas，会因 pointer-events:none 而完全收不到点击。
    img.addEventListener('mousedown', (e) => {
      if (!img.src) return;
      // 弹窗里平移图片仅在显式「移动」模式 + 已放大时生效。
      // （旧逻辑：z>1 且起点落在已有加选区内也 pan——这会让「加选」模式下在已有选区上拖动时
      //   误把图片平移走，而非按用户本意加画新框。现已收紧到只能显式移动模式。）
      let startPan = false;
      if (target === 'modal' && dwDrawMode === 'pan' && dwModalZoom > 1.0001) {
        startPan = true;
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
    // 放宽前置校验：文件选择器选图、或拖拽/粘贴/回填导致预览图已加载，都能开灯箱
    const hasImg = el.dwImgFile.files[0] || (el.dwImgPreview.src && el.dwImgPreview.naturalWidth > 0);
    if (!hasImg) { el.dwImgStatus.textContent = '请先选择图片文件'; return; }
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
    // AI 模型可选项（仅当 engine=ai 时才有意义）；INT8 也只在 ai 引擎下生效
    if (form.get('engine') === 'ai') {
      if (el.dwImgModel) form.append('model', el.dwImgModel.value || 'lama');
      if (el.dwImgInt8) form.append('int8', el.dwImgInt8.checked ? '1' : '0');
    }
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
            // 自动滚到结果对比块：dw-layout 是两列 grid，dw-result 在其下方
            // 视口高度有限时（侧边栏撑满）结果区会落在视口外，手动滚避免「看不到成品」
            try { el.dwImgResult.scrollIntoView({ behavior: 'smooth', block: 'center' }); } catch (_e) {}
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
  const dwSyncEngineUi = () => {
    if (!el.dwImgEngine) return;
    const ai = el.dwImgEngine.value === 'ai';
    if (el.dwImgCvField) el.dwImgCvField.hidden = ai;
    if (el.dwImgRadiusField) el.dwImgRadiusField.hidden = ai;
    if (el.dwImgModelField) el.dwImgModelField.hidden = !ai;
    if (el.dwImgInt8Field) el.dwImgInt8Field.hidden = !ai;
  };
  if (el.dwImgEngine) {
    el.dwImgEngine.addEventListener('change', dwSyncEngineUi);
    dwSyncEngineUi();
  }
  // AI 模型 + INT8 持久化（与视频面板同一规则：跨会话保留用户选择）
  const DW_MODEL_KEY = 'vdl_dw_model';
  const DW_INT8_KEY_IMG = 'vdl_dw_int8_img';
  try {
    const saved = localStorage.getItem(DW_MODEL_KEY);
    if (saved && el.dwImgModel && Array.from(el.dwImgModel.options).some(o => o.value === saved)) {
      el.dwImgModel.value = saved;
    }
  } catch (_e) {}
  if (el.dwImgModel) {
    el.dwImgModel.addEventListener('change', () => {
      try { localStorage.setItem(DW_MODEL_KEY, el.dwImgModel.value); } catch (_e) {}
    });
  }
  // 视频面板 INT8 持久化（同已有 key 'vdl_dw_int8'），图片面板单独 key 避免彼此覆盖
  try {
    if (el.dwImgInt8) {
      const savedImg = localStorage.getItem(DW_INT8_KEY_IMG);
      if (savedImg !== null) el.dwImgInt8.checked = (savedImg === '1');
      el.dwImgInt8.addEventListener('change', () => {
        try { localStorage.setItem(DW_INT8_KEY_IMG, el.dwImgInt8.checked ? '1' : '0'); } catch (_e) {}
      });
    }
  } catch (_e) {}
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

  // -------------------------------------------------- 视频去水印（B 档：逐帧 LaMa + 邻帧中值平滑）
  const DWV_NS = 'http://www.w3.org/2000/svg';
  let dwVidSel = null;  // 归一化 {x,y,w,h}（相对视频显示框）

  const dwVidResize = () => {
    const v = el.dwVidThumb;
    if (!v || !v.clientWidth) return;
    const w = v.clientWidth, h = v.clientHeight;
    const svg = el.dwVidSvg;
    svg.setAttribute('width', w);
    svg.setAttribute('height', h);
    svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
    svg.style.width = w + 'px';
    svg.style.height = h + 'px';
    svg.style.left = v.offsetLeft + 'px';
    svg.style.top = v.offsetTop + 'px';
  };

  const dwVidDraw = () => {
    const svg = el.dwVidSvg;
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    if (!dwVidSel) {
      el.dwVidSelInfo.textContent = '尚未框选';
      return;
    }
    const v = el.dwVidThumb;
    const w = v.clientWidth, h = v.clientHeight;
    const x = dwVidSel.x * w, y = dwVidSel.y * h, rw = dwVidSel.w * w, rh = dwVidSel.h * h;
    const r = document.createElementNS(DWV_NS, 'rect');
    r.setAttribute('x', x); r.setAttribute('y', y);
    r.setAttribute('width', rw); r.setAttribute('height', rh);
    r.setAttribute('fill', 'rgba(76,217,100,0.25)');
    r.setAttribute('stroke', '#4cd964');
    r.setAttribute('stroke-width', '2');
    svg.appendChild(r);
    el.dwVidSelInfo.textContent = `已框选水印区 (${Math.round(dwVidSel.x * 100)}%, ${Math.round(dwVidSel.y * 100)}%, ${Math.round(dwVidSel.w * 100)}%, ${Math.round(dwVidSel.h * 100)}%)`;
  };

  // 转码播放源：任意格式 → 服务端 H.264+AAC → <video controls> 默认渲染。
  // 同文件（name+size）缓存 preview_id，避免重复上传与转码。
  //
  // 2026-08-30 用户明确反馈：「这是我要的导入素材未编辑前的效果」——
  // wkwebview <video controls> 自带的底栏（15s± / ▶ / 进度 / 音量 / 全屏）就是
  // 想要的，不需要画蛇添足。彻底回退到最简单方案：video.src + poster = thumbSrc。
  const dwVidMarkPlayable = (url, previewId) => {
    const base = `${window.VDL_API_BASE || ''}`;
    const thumbSrc = el.dwVidThumb && el.dwVidThumb.src;
    if (thumbSrc && !el.dwVidPlayer.poster) {
      el.dwVidPlayer.poster = thumbSrc;
    }
    el.dwVidPlayer.src = `${base}${url || '/api/dw/video/preview/' + previewId}`;
    el.dwVidPlayer.hidden = false;
    el.dwVidTranscoding.hidden = true;
    // 把整个画布标记为可播放（让 img 让位给 video），同时露出 filmstrip
    const wrap = el.dwVidPlayer && el.dwVidPlayer.parentElement;
    if (wrap) wrap.classList.add('is-playable');
    if (el.dwVidPlayerHead) el.dwVidPlayerHead.hidden = false;
    if (el.dwVidFilmstripWrap) el.dwVidFilmstripWrap.hidden = false;
    if (el.dwVidFsHint) el.dwVidFsHint.hidden = false;
    if (el.dwVidBackToThumb) el.dwVidBackToThumb.hidden = false;
    // video 加载好后按真实尺寸校准画布纵横比（避免 9:16 视频被 16:9 容器拉伸空白）
    const onLoaded = () => {
      const vw = el.dwVidPlayer.videoWidth, vh = el.dwVidPlayer.videoHeight;
      if (vw > 0 && vh > 0 && wrap) wrap.style.aspectRatio = `${vw} / ${vh}`;
      el.dwVidPlayer.removeEventListener('loadedmetadata', onLoaded);
    };
    el.dwVidPlayer.addEventListener('loadedmetadata', onLoaded);
    if (!el.dwVidStatus.textContent.includes('失败')) el.dwVidStatus.textContent = '';
    // 设计意图（2026-08-30 用户明确要求）：**不自动 play**。video 元素默认 paused，
    // 用户必须手动点底栏 ▶ 才会开始播放。转码完成 / 选框 / filmstrip seek 都不触发 play。
    // 这样用户在画面上拖框选水印时画面是静止的（first frame），框能精准对齐水印位置。
  };

  const dwVidStartPreviewTranscode = async (f) => {
    const cacheKey = `dwPrev:${f.name}:${f.size}`;
    const base0 = `${window.VDL_API_BASE || ''}`;
    const cachedId = (() => { try { return localStorage.getItem(cacheKey); } catch (_e) { return null; } })();
    if (cachedId) {
      // 缓存命中：先确认服务端该预览仍可用，可用则直接复用（跳过上传+转码）
      try {
        const st = await request('/api/dw/video/preview/' + cachedId + '/status');
        if (st.status === 'completed') {
          dwVidMarkPlayable(`/api/dw/video/preview/${cachedId}`, cachedId);
          return;
        }
      } catch (_e) { /* 缓存失效，重新转码 */ }
    }
    try {
      const form = new FormData();
      form.append('file', f);
      const data = await request('/api/dw/video/preview', { method: 'POST', body: form });
      const previewId = data.preview_id;
      try { localStorage.setItem(cacheKey, previewId); } catch (_e) {}  // 记下以复用
      el._dwPreviewPoll = setInterval(async () => {
        try {
          const st = await request('/api/dw/video/preview/' + previewId + '/status');
          if (st.status === 'completed') {
            clearInterval(el._dwPreviewPoll);
            el._dwPreviewPoll = null;
            dwVidMarkPlayable(`/api/dw/video/preview/${previewId}`, previewId);
          } else if (st.status === 'failed') {
            clearInterval(el._dwPreviewPoll);
            el._dwPreviewPoll = null;
            el.dwVidTranscoding.hidden = true;
            el.dwVidStatus.textContent = '播放源转码失败：' + (st.error || '未知错误');
          }
        } catch (_e) { /* 转码轮询继续 */ }
      }, 1500);
    } catch (e) {
      el.dwVidTranscoding.hidden = true;
      el.dwVidStatus.textContent = '播放源转码失败：' + (e.message || '未知错误');
    }
  };

  el.dwVidFile.addEventListener('change', async () => {
    const f = el.dwVidFile.files[0];
    if (!f) return;
    // 上传视频后，把「启用视频预览」开关展示给用户（默认未勾选 = 不转码）
    if (el.dwVidPreviewToggleWrap) el.dwVidPreviewToggleWrap.hidden = false;
// 选中视频即隐藏「请上传视频」占位层，画布进入工作态。
// 显示工作态 cap「原视频预览 · 在画面上拖框选水印」（默认 hidden，mousedown 拖框即隐藏）。
if (el.dwVidEmpty) el.dwVidEmpty.hidden = true;
if (el.dwVidCapOverlay) el.dwVidCapOverlay.hidden = false;
    const url = URL.createObjectURL(f);
    // 结果区「原视频」对比框用同一个 blob URL（input 视频本身就是原视频）
    el.dwVidOrig.src = url;
    el.dwVidOrig.muted = true;
    // 清旧选区 + 旧缩略图 + 旧 filmstrip
    dwVidSel = null;
    if (el._dwThumbUrl) { URL.revokeObjectURL(el._dwThumbUrl); el._dwThumbUrl = null; }
    el.dwVidThumb.removeAttribute('src');
    // 同步清掉上一段视频可能留存的 poster——避免换视频后首段缩略图错位到新视频
    if (el.dwVidPlayer) el.dwVidPlayer.removeAttribute('poster');
    if (el._dwFilmstripUrl) { URL.revokeObjectURL(el._dwFilmstripUrl); el._dwFilmstripUrl = null; }
    // filmstrip 缩略图条已移除（2026-08-31）：元素不存在时整段跳过，避免 null 报错
    if (el.dwVidFilmstrip) el.dwVidFilmstrip.removeAttribute('src');
    if (el.dwVidFilmstripCursor) el.dwVidFilmstripCursor.hidden = true;
// 播放器复位（等待新的转码）：画布回到「img + 转码遮罩」两态，video/底部控件全部隐藏。
// wrap 不再强制 16/9：清空内联 aspectRatio 让 thumb 一加载就同步覆盖为真实比例，
// 避免 9:16 视频被 16/9 容器拉宽；占位 9/16 由 CSS 默认撑出。
const wrap = el.dwVidThumb && el.dwVidThumb.parentElement;
if (wrap) wrap.classList.remove('is-playable');
wrap.style.aspectRatio = '';
el.dwVidPlayer.hidden = true;
el.dwVidPlayer.removeAttribute('src');
    // 视频预览默认开启（WKWebView 不转码播不了，所以"开箱即播"是默认体验）；
    // 用户取消勾选后才走"无转码"主链路：仅首帧 img 框选，无播放器。
    const wantPreview = !!(el.dwVidPreviewToggle && el.dwVidPreviewToggle.checked);
    el.dwVidTranscoding.hidden = !wantPreview;
    if (el.dwVidPlayerHead) el.dwVidPlayerHead.hidden = true;
    // 复位切换按钮文案：重新选视频后回到初始「回到首帧」语义（防止停在「▷ 播放预览」错位）
    if (el.dwVidBackToThumb) el.dwVidBackToThumb.textContent = '📐 回到首帧重新框选';
    if (el.dwVidFilmstripWrap) el.dwVidFilmstripWrap.hidden = true;
    if (el.dwVidFsHint) el.dwVidFsHint.hidden = true;
    if (el._dwPreviewPoll) { clearInterval(el._dwPreviewPoll); el._dwPreviewPoll = null; }
    el.dwVidResult.hidden = true;
    el.dwVidStatus.textContent = wantPreview
      ? '正在抽首帧 + 转码播放源…'
      : '正在抽首帧 + 加载时间线…（视频预览默认开启，如不需转码可取消上方勾选，跳过播放器）';
    // 3) 启动转码（任意格式 → H.264+AAC，让 WKWebView 都能播）—— opt-out 走「不转码」
    if (wantPreview) dwVidStartPreviewTranscode(f);
    // 4) 后台预热 AI 模型：选完视频即加载（最耗时步骤），用户框选期间完成，
    //    点击「开始去水印」时模型已在内存，不再出现「长时间 0 帧」的假死观感
    //    预热即按当前 INT8 开关 + 模型选择加载对应模型，与任务提交模式一致可避免切换重建
    try {
      const wf = new FormData();
      wf.append('int8', el.dwVidInt8.checked ? '1' : '0');
      if (el.dwVidModel) wf.append('model', el.dwVidModel.value || 'lama');
      fetch((window.VDL_API_BASE || '') + '/api/dw/ai/warmup', { method: 'POST', body: wf });
    } catch (_e) { /* 预热失败不影响主流程 */ }
    // 抽首帧（img 框选）与 filmstrip（点击跳转时间线）：
    //   之前用 Promise.all([thumb, film]) 等齐才往下走——filmstrip 要 decode 20 帧再 tile，
    //   通常 2-4s，卡住了 thumbnail 的 img 显示（thumbnail 只要 200-500ms）。
    //   现在 thumb 先 await，拿到立即显示；filmstrip 后台并行，迟到不影响用户框选。
    //   注意：不能走项目里 request() wrapper——它内部 _parseResponse 强制 .json()，
    //   会破坏 PNG 二进制；这里直接用原生 fetch，自己控制 .blob()
    //
    //   **2026-08-29 二次提速**：大视频（动辄数 GB）上传整个文件到 thumbnail 接口要几分钟，
    //   黑色画布等几 GB 传完才出现首帧 → 改三层兜底：
    //     ① 本地 WebKit video 元素 + canvas.drawImage 抽首帧（秒开，根本不走网络）
    //     ② 上传文件前 32 MB 切片让后端 ffmpeg 抽（覆盖 faststart 99% 视频）
    //     ③ 切片抽不到（moov 在尾部罕见情况）才发完整文件兜底
    //   filmstrip 同样改用切片上传，避免传整个大文件。
    const ctrl = new AbortController();
    const ctrlTs = Date.now();
    const timer = setTimeout(() => ctrl.abort(), 90000);
    const headers = { 'X-Device-Id': deviceId() };

    // ① 本地抽帧（秒级最佳 UX）
    let localThumb = null;
    try {
      localThumb = await grabLocalVideoThumb(f, ctrl.signal);
    } catch (_e) { /* 走切片兜底 */ }
    if (localThumb && !ctrl.signal.aborted) {
      const thumbUrl = URL.createObjectURL(localThumb.blob);
      el._dwThumbUrl = thumbUrl;
      // thumb.onload 触发时 naturalWidth/Height 已有值 → 同步校准 wrap 比例，避免先 9/16 后 9/16 的闪
      el.dwVidThumb.onload = () => {
        const w = el.dwVidThumb.naturalWidth, h = el.dwVidThumb.naturalHeight;
        if (w > 0 && h > 0) {
          const wwrap = el.dwVidThumb.parentElement;
          if (wwrap) wwrap.style.aspectRatio = `${w} / ${h}`;
        }
        dwVidResize(); dwVidDraw();
      };
      el.dwVidThumb.src = thumbUrl;
      el.dwVidStatus.textContent = `首帧 ${Date.now() - ctrlTs}ms（本地 WebKit 解码抽取）`;
      clearTimeout(timer);
    } else {
      // ② 切片上传
      const SLICE = 32 * 1024 * 1024;  // 32 MB（足够大多数 faststart mp4 拿到 moov+首段）
      const needSlice = f.size > SLICE;
      const slice = needSlice ? f.slice(0, SLICE) : f;
      try {
        const form1 = new FormData();
        form1.append('file', slice, f.name);
        const thumbR = await fetch((window.VDL_API_BASE || '') + '/api/dw/video/thumbnail', {
          method: 'POST', body: form1, headers, signal: ctrl.signal,
        });
        if (!thumbR.ok) {
          const txt = await thumbR.text().catch(() => '');
          el.dwVidStatus.textContent = '首帧切片提取失败：' + (txt || ('HTTP ' + thumbR.status));
          // ③ 兜底：完整文件
          if (!needSlice) throw new Error('thumb full-failed: ' + (txt || thumbR.status));
          const formFull = new FormData(); formFull.append('file', f);
          el.dwVidStatus.textContent = '首帧切片失败，自动转传完整文件…';
          const thumbR2 = await fetch((window.VDL_API_BASE || '') + '/api/dw/video/thumbnail', {
            method: 'POST', body: formFull, headers, signal: ctrl.signal,
          });
          if (!thumbR2.ok) {
            const t2 = await thumbR2.text().catch(() => '');
            el.dwVidStatus.textContent = '首帧提取失败：' + (t2 || ('HTTP ' + thumbR2.status));
            throw new Error('thumb-full-failed: ' + (t2 || thumbR2.status));
          }
          const blob2 = await thumbR2.blob();
          const tu2 = URL.createObjectURL(blob2);
          el._dwThumbUrl = tu2;
          el.dwVidThumb.onload = () => {
            const w = el.dwVidThumb.naturalWidth, h = el.dwVidThumb.naturalHeight;
            if (w > 0 && h > 0) {
              const wwrap = el.dwVidThumb.parentElement;
              if (wwrap) wwrap.style.aspectRatio = `${w} / ${h}`;
            }
            dwVidResize(); dwVidDraw();
          };
          el.dwVidThumb.src = tu2;
          el.dwVidStatus.textContent = '';
        } else {
          const blob = await thumbR.blob();
          const thumbUrl = URL.createObjectURL(blob);
          el._dwThumbUrl = thumbUrl;
          // 同源 thumb URL，imgProbe 没必要额外探测；直接在 <img> onload 里同步校准
          el.dwVidThumb.onload = () => {
            const w = el.dwVidThumb.naturalWidth, h = el.dwVidThumb.naturalHeight;
            if (w > 0 && h > 0) {
              const wwrap = el.dwVidThumb.parentElement;
              if (wwrap) wwrap.style.aspectRatio = `${w} / ${h}`;
            }
            dwVidResize(); dwVidDraw();
          };
          el.dwVidThumb.src = thumbUrl;
          el.dwVidStatus.textContent = `首帧切片 ${Math.round(blob.size / 1024)} KB（${Date.now() - ctrlTs}ms）`;
        }
      } catch (eFull) {
        el.dwVidStatus.textContent = '首帧加载失败：' + (eFull.message || '未知错误');
      } finally {
        clearTimeout(timer);
      }
    }

    // 后台并行：filmstrip（同样切片上传，绝大多数情况秒开；失败仅提示不影响主路径）
    // 2026-08-31：缩略图条已从 UI 移除 → 元素不存在就完全不发请求，省服务端 20 帧解码 + 切片上传
    if (!el.dwVidFilmstrip) return;
    dwVidFetchFilmstrip(f, ctrl.signal).then((res) => {
      if (!res || !res.ok) {
        const prev = el.dwVidStatus.textContent;
        if (prev && !prev.includes('失败')) el.dwVidStatus.textContent = prev + '　缩略图条失败：' + res.err;
        return;
      }
      // 撤销旧 url，绑新 url + 帧参数
      if (el._dwFilmstripUrl) { URL.revokeObjectURL(el._dwFilmstripUrl); }
      el._dwFilmstripUrl = res.filmUrl;
      el.dwVidFilmstrip.src = res.filmUrl;
      if (res.frames) el.dwVidFilmstrip.dataset.frames = String(res.frames);
      if (res.interval) el.dwVidFilmstrip.dataset.interval = String(res.interval);
    }).catch(() => {});
  });

  let dwVidDrag = false;
  // mousedown 监听移到共享 wrap 上：img 占主导 / video 接管后都能框选
  // ⚠️ 用 capture 阶段（先于 <video>）拦截 mousedown，再 stopPropagation。
  //    否则 video 上收到 mousedown 会触发 WKWebView 原生「点画面 → toggle play/pause」，
  //    用户每点一次选框都会把 video 播放状态切一次（点一次暂停、再点一次播放…）。
  //    跳过 video 底部 ~50px（controls 区）的点击 → 让 ▶/⏸ 按钮仍可手动操作。
  const dwVidWrap = el.dwVidThumb.parentElement;
  dwVidWrap.addEventListener('mousedown', (e) => {
    // 必须有可视内容（img.src 或 video.src 之一存在且 video 显示中）
    const hasThumb = !!el.dwVidThumb.src;
    const hasVideo = !!(el.dwVidPlayer && el.dwVidPlayer.src && !el.dwVidPlayer.hidden);
    if (!hasThumb && !hasVideo) return;
    // video 接管时跳过底部 ~50px（native controls 区，让 video 自己处理 play/seek）
    const inControls = hasVideo && e.offsetY > el.dwVidPlayer.clientHeight - 50;
    if (inControls) return;
    // 拦下事件，video 收不到 mousedown 就不会触发 click toggle（同时保留下面的拖框逻辑）
    e.stopPropagation();
    dwVidDrag = true;
    // 用户开始框选即隐藏 cap 角标：避免覆盖水印区（pointer-events:none 已保证不拦截，
    // 但视觉遮挡会让用户看不清目标水印就框不准）；换视频时 change handler 会重新显示
    if (el.dwVidCapOverlay && !el.dwVidCapOverlay.hidden) el.dwVidCapOverlay.hidden = true;
    const [nx, ny] = dwNormFromEvent(el.dwVidThumb, e.clientX, e.clientY);
    dwVidSel = { x: nx, y: ny, w: 0, h: 0 };
    e.preventDefault();
  }, { capture: true });
  document.addEventListener('mousemove', (e) => {
    if (!dwVidDrag) return;
    const [nx, ny] = dwNormFromEvent(el.dwVidThumb, e.clientX, e.clientY);
    const x0 = Math.min(dwVidSel.x, nx), y0 = Math.min(dwVidSel.y, ny);
    dwVidSel.x = x0;
    dwVidSel.y = y0;
    dwVidSel.w = Math.abs(nx - dwVidSel.x);
    dwVidSel.h = Math.abs(ny - dwVidSel.y);
    if (dwVidSel.w < 0.005) dwVidSel.w = 0.005;
    if (dwVidSel.h < 0.005) dwVidSel.h = 0.005;
    dwVidDraw();
  });
  document.addEventListener('mouseup', () => {
    if (dwVidDrag && dwVidSel && (dwVidSel.w <= 0.005 || dwVidSel.h <= 0.005)) {
      dwVidSel = null;  // 误点（几乎无面积）视为取消
    }
    dwVidDrag = false;
    dwVidDraw();
  });
  el.dwVidClear.addEventListener('click', () => { dwVidSel = null; dwVidDraw(); });
  window.addEventListener('resize', () => { if (!el.dwVideoPane.hidden) { dwVidResize(); dwVidDraw(); } });

  // -------------------------------------------------- 时间分段（Segment）：双滑块（拖拽绿色手柄选区间）
  // 三个元素 id 复用：dwVidStart / dwVidEnd（现在是 range）+ dwVidStartLabel / dwVidEndLabel（左右数值）
  let dwVidDuration = 0;  // 视频总时长（秒），从 dwVidOrig 的 metadata 取

  const dwVidUpdateRange = (active) => {
    if (!dwVidDuration || dwVidDuration <= 0) return;
    let s = parseFloat(el.dwVidStart.value);
    let e = parseFloat(el.dwVidEnd.value);
    // 互不交叉：被拖的那个保持，挤另一个；最小 0.5s 间隔
    const minGap = 0.5;
    if (active === el.dwVidStart && s > e - minGap) { s = Math.max(0, e - minGap); el.dwVidStart.value = s; }
    if (active === el.dwVidEnd && e < s + minGap) { e = Math.min(dwVidDuration, s + minGap); el.dwVidEnd.value = e; }
    // 高亮条
    const sPct = (s / dwVidDuration) * 100;
    const ePct = (e / dwVidDuration) * 100;
    el.dwVidRangeHighlight.style.left = sPct + '%';
    el.dwVidRangeHighlight.style.width = Math.max(0, ePct - sPct) + '%';
    // 数值标签
    el.dwVidStartLabel.textContent = s.toFixed(1) + 's';
    el.dwVidEndLabel.textContent = e.toFixed(1) + 's';
    // 状态提示 + 预估帧数（让用户选完区间立刻知道总工作量；区间内的帧才是真正要推理的）
    const isFull = (s <= 0 && e >= dwVidDuration);
    const fps = parseFloat(el.dwVidTargetFps.value) || 30;
    const stride = Math.max(1, parseInt(el.dwVidStride.value, 10) || 4);
    const totalEst = Math.round(dwVidDuration * fps);
    const span = isFull ? dwVidDuration : Math.max(0, e - s);
    // wave2 ②：时间稀疏 —— 实际 AI 推理帧 ≈ 区间内帧 / 推理间隔
    const inpaintEst = Math.round((span * fps) / stride);
    if (isFull) {
      // 紧凑 1 行（避免在 320px 侧栏里换行成 4 行） + title tooltip 看完整版
      el.dwVidSegTip.textContent = `整段 ${dwVidDuration.toFixed(1)}s @ ${fps}fps · ≈AI ${inpaintEst} 帧`;
      el.dwVidSegTip.title = `整段处理：${dwVidDuration.toFixed(1)}s @ ${fps}fps ≈ ${totalEst} 帧（AI 实际推理 ≈ ${inpaintEst} 帧，按${stride}倍间隔）`;
    } else {
      const pct = Math.round((span / dwVidDuration) * 100);
      // 紧凑 1 行：分别给关键数字，但用 · 分隔避免在窄栏里堆成多行
      el.dwVidSegTip.textContent =
        `${span.toFixed(1)}s · 占 ${pct}% · ≈AI ${inpaintEst} 帧 · 余 ${(dwVidDuration - span).toFixed(1)}s 复制`;
      el.dwVidSegTip.title =
        `推理 ${span.toFixed(1)}s（占全片 ${pct}%）@ ${fps}fps，每 ${stride} 帧推理 1 次 ≈ ${inpaintEst} 帧 AI；` +
        `其余 ${(dwVidDuration - span).toFixed(1)}s 由「区间外 copy」+「帧间插值」复用，ms 级完成，不耗算力`;
    }
  };

  el.dwVidOrig.addEventListener('loadedmetadata', () => {
    dwVidDuration = el.dwVidOrig.duration || 0;
    // 换视频 → 旧段的时间基准失效，清空避免与新的总时长不匹配
    if (dwVidSegments.length) { dwVidSegments = []; dwVidRenderSegments(); }
    el.dwVidStart.max = String(dwVidDuration);
    el.dwVidEnd.max = String(dwVidDuration);
    // 默认：start=0, end=duration（整段处理）；如有旧值则保留（<input type="range">的 value 在 innerHTML 重写时会重置为 max/2，所以这里手动设）
    el.dwVidStart.value = '0';
    el.dwVidEnd.value = String(dwVidDuration);
    dwVidUpdateRange(null);
  });
  el.dwVidStart.addEventListener('input', () => {
    dwVidUpdateRange(el.dwVidStart);
    // 手动拖滑块 = 已设过起点。用户后续点「设为结束」或手动 + 添加时就会触发自动添加
    dwVidStartSet = true;
  });
  el.dwVidEnd.addEventListener('input', () => {
    dwVidUpdateRange(el.dwVidEnd);
    dwVidEndSet = true;
  });
  // 输出帧率变化时也重算预估帧数（"帧数预估" 一目了然，让用户按帧数选 fps）
  el.dwVidTargetFps.addEventListener('change', () => dwVidUpdateRange(null));
  el.dwVidStride.addEventListener('change', () => dwVidUpdateRange(null));

  // INT8 动态量化开关：本地持久化（默认开，与后端默认一致），跨会话保留用户选择
  const DW_INT8_KEY = 'vdl_dw_int8';
  const DW_MODEL_KEY_VID = 'vdl_dw_model_vid';
  try {
    const savedInt8 = localStorage.getItem(DW_INT8_KEY);
    if (savedInt8 !== null) el.dwVidInt8.checked = (savedInt8 === '1');
  } catch (_e) { /* localStorage 不可用则沿用 HTML 默认（checked） */ }
  el.dwVidInt8.addEventListener('change', () => {
    try { localStorage.setItem(DW_INT8_KEY, el.dwVidInt8.checked ? '1' : '0'); } catch (_e) {}
  });
  // 视频面板 AI 模型选择持久化（与图片面板独立 key）
  try {
    if (el.dwVidModel) {
      const savedV = localStorage.getItem(DW_MODEL_KEY_VID);
      if (savedV && Array.from(el.dwVidModel.options).some(o => o.value === savedV)) {
        el.dwVidModel.value = savedV;
      }
      el.dwVidModel.addEventListener('change', () => {
        try { localStorage.setItem(DW_MODEL_KEY_VID, el.dwVidModel.value); } catch (_e) {}
      });
    }
  } catch (_e) {}

  // -------------------------------------------------- 多时间段（segments）管理
  // 每段 = { start, end, regions, label, keyframes? }，regions 为该段时框选的归一化区域
  // keyframes = [{ t: 秒, regions: [...] }, ...]（可选）：段内水印位置按时间线性插值（漂动水印）
  let dwVidSegments = [];
  let dwVidActiveSeg = null;  // 当前选中的段（点击段列表项设置），用于编辑关键帧
  let dwVidCurrentJob = null; // 当前进行中的任务 job_id（暂停/继续/取消用）
  // 用户是否设过 start / end（手动拖滑块或点「设为 X」均视为「设过」）。两个都为 true 且闭锁时，
  // 自动 push 当前区间到 dwVidSegments，去掉冗余的 + 添加按钮点击
  let dwVidStartSet = false;
  let dwVidEndSet = false;

  const dwVidRenderSegments = () => {
    const list = el.dwVidSegList;
    if (!list) return;
    list.innerHTML = '';
    dwVidSegments.forEach((seg, i) => {
      const li = document.createElement('li');
      li.className = 'dw-vid-segitem' + (i === dwVidActiveSeg ? ' seg-active' : '');
      li.title = '点击选中该段以编辑关键帧';
      const no = document.createElement('span');
      no.className = 'seg-no';
      no.textContent = `①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳`[i] || `${i + 1}.`;
      const tm = document.createElement('span');
      tm.className = 'seg-time';
      tm.textContent = `${seg.start.toFixed(1)}s–${seg.end.toFixed(1)}s`;
      const pos = document.createElement('span');
      pos.className = 'seg-pos';
      const kfCount = (seg.keyframes && seg.keyframes.length) || 0;
      pos.textContent = (seg.label || '') + (kfCount ? ` · ${kfCount}关键帧` : '');
      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'seg-del';
      del.title = '删除该时间段';
      del.textContent = '✕';
      del.addEventListener('click', (e) => {
        e.stopPropagation();
        dwVidSegments.splice(i, 1);
        if (dwVidActiveSeg === i) dwVidActiveSeg = null;
        else if (dwVidActiveSeg > i) dwVidActiveSeg -= 1;
        dwVidRenderSegments();
      });
      li.append(no, tm, pos, del);
      li.addEventListener('click', () => {
        dwVidActiveSeg = i;
        dwVidRenderSegments();
      });
      list.appendChild(li);
    });
    // 汇总
    if (el.dwVidSegSummary) {
      if (!dwVidSegments.length) {
        el.dwVidSegSummary.textContent = '未标记任何时间段 → 默认整段处理';
      } else {
        const cover = dwVidSegments.reduce((acc, s) => acc + Math.max(0, s.end - s.start), 0);
        const pct = dwVidDuration ? Math.round((cover / dwVidDuration) * 100) : 0;
        el.dwVidSegSummary.textContent = `已标记 ${dwVidSegments.length} 段，合计约 ${cover.toFixed(1)}s（占全片 ${pct}%）——其余帧直接复制`;
      }
    }
    // 时间轴上的已标记段色块
    const wrap = el.dwVidRangeWrap;
    if (wrap && dwVidDuration > 0) {
      wrap.querySelectorAll('.dw-vid-range-mark').forEach((n) => n.remove());
      dwVidSegments.forEach((seg) => {
        const mk = document.createElement('div');
        mk.className = 'dw-vid-range-mark';
        mk.style.left = (seg.start / dwVidDuration * 100) + '%';
        mk.style.width = Math.max(0, (seg.end - seg.start) / dwVidDuration * 100) + '%';
        wrap.appendChild(mk);
      });
    }
    dwVidRenderKfPanel();
  };

  // 关键帧面板：展示/编辑当前激活段的关键帧（漂动水印按时间插值）
  const dwVidRenderKfPanel = () => {
    const panel = el.dwVidKfPanel;
    if (!panel) return;
    if (dwVidActiveSeg == null || !dwVidSegments[dwVidActiveSeg]) {
      panel.hidden = true;
      return;
    }
    const seg = dwVidSegments[dwVidActiveSeg];
    panel.hidden = false;
    const kfs = seg.keyframes || [];
    el.dwVidKfTitle.textContent = `段 ${dwVidActiveSeg + 1} 关键帧（${kfs.length}）`;
    el.dwVidKfTip.textContent = kfs.length
      ? '把时间轴/播放拖到新位置，重新框选水印，再点下方按钮追加关键帧'
      : '该段为固定位置。要处理漂动水印：拖时间轴到目标点、重新框选，点下方按钮添加关键帧';
    const list = el.dwVidKfList;
    list.innerHTML = '';
    kfs.forEach((kf, i) => {
      const li = document.createElement('li');
      li.className = 'dw-kf-item';
      const t = document.createElement('span');
      t.className = 'kf-t';
      t.textContent = `${kf.t.toFixed(1)}s`;
      const r = document.createElement('span');
      r.className = 'kf-pos';
      const reg = (kf.regions && kf.regions[0]) || {};
      r.textContent = `(${Math.round((reg.x || 0) * 100)},${Math.round((reg.y || 0) * 100)},${Math.round((reg.w || 0) * 100)},${Math.round((reg.h || 0) * 100)})`;
      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'seg-del';
      del.title = '删除该关键帧';
      del.textContent = '✕';
      del.addEventListener('click', (e) => {
        e.stopPropagation();
        seg.keyframes.splice(i, 1);
        if (!seg.keyframes.length) seg.keyframes = undefined;
        dwVidRenderKfPanel();
        dwVidRenderSegments();
      });
      li.append(t, r, del);
      list.appendChild(li);
    });
  };

  // 添加关键帧：在激活段内，按当前时间轴位置 + 当前框选，追加一个关键帧
  const dwVidAddKeyframe = () => {
    if (!dwVidDuration) { el.dwVidStatus.textContent = '请先选择视频文件'; return; }
    if (dwVidActiveSeg == null || !dwVidSegments[dwVidActiveSeg]) {
      el.dwVidStatus.textContent = '请先点选一个时间段（或先“添加该区间”）再添加关键帧'; return;
    }
    if (!dwVidSel || dwVidSel.w <= 0 || dwVidSel.h <= 0) {
      el.dwVidStatus.textContent = '请先在预览上框选水印区域'; return;
    }
    const seg = dwVidSegments[dwVidActiveSeg];
    let t = dwVidSeekTime;
    if (!(t > 0)) {
      const s = parseFloat(el.dwVidStart.value) || 0;
      const e = parseFloat(el.dwVidEnd.value) || 0;
      t = (s + e) / 2;
    }
    t = Math.max(seg.start, Math.min(seg.end, t));  // clamp 到段区间
    const reg = {
      x: +dwVidSel.x.toFixed(4), y: +dwVidSel.y.toFixed(4),
      w: +dwVidSel.w.toFixed(4), h: +dwVidSel.h.toFixed(4), op: 'add',
    };
    if (!seg.keyframes) seg.keyframes = [];
    // 若原是固定位置（无关键帧）且有关键帧区起点，先把固定框转成起点关键帧，保证平滑
    if (seg.keyframes.length === 0 && seg.regions && seg.regions.length) {
      seg.keyframes.push({ t: +seg.start.toFixed(2), regions: seg.regions });
    }
    seg.keyframes.push({ t: +t.toFixed(2), regions: [reg] });
    seg.keyframes.sort((a, b) => a.t - b.t);
    dwVidRenderKfPanel();
    dwVidRenderSegments();
    el.dwVidStatus.textContent = `已在 ${t.toFixed(1)}s 为第 ${dwVidActiveSeg + 1} 段添加关键帧（共 ${seg.keyframes.length} 个）`;
  };
  if (el.dwVidAddKf) el.dwVidAddKf.addEventListener('click', dwVidAddKeyframe);
  if (el.dwVidAddKfAt) el.dwVidAddKfAt.addEventListener('click', dwVidAddKeyframe);

  // 把当前 start/end slider + dwVidSel 推进 dwVidSegments。auto=true 时 status 文案标「自动添加」。
  // 供 dwVidAddSeg（手动添加）和 dwVidTryAutoAdd（自动闭锁）共用。
  const dwVidPushSegFromSliders = (auto) => {
    if (!dwVidDuration) { el.dwVidStatus.textContent = '请先选择视频文件'; return false; }
    if (!dwVidSel || dwVidSel.w <= 0 || dwVidSel.h <= 0) {
      el.dwVidStatus.textContent = '请先在首帧预览上拖拽框选水印区域';
      return false;
    }
    const s = parseFloat(el.dwVidStart.value) || 0;
    const e = parseFloat(el.dwVidEnd.value) || 0;
    if (e <= s + 0.1) {
      el.dwVidStatus.textContent = '结束需晚于开始 ≥ 0.1s';
      return false;
    }
    dwVidSegments.push({
      start: s,
      end: e,
      regions: [{
        x: +dwVidSel.x.toFixed(4), y: +dwVidSel.y.toFixed(4),
        w: +dwVidSel.w.toFixed(4), h: +dwVidSel.h.toFixed(4), op: 'add',
      }],
      label: `区域(${Math.round(dwVidSel.x * 100)},${Math.round(dwVidSel.y * 100)},${Math.round(dwVidSel.w * 100)},${Math.round(dwVidSel.h * 100)})`,
    });
    dwVidActiveSeg = dwVidSegments.length - 1;
    dwVidRenderSegments();
    const tag = auto ? '✓ 自动添加' : '';
    el.dwVidStatus.textContent = `已添加第 ${dwVidSegments.length} 段（${s.toFixed(1)}s–${e.toFixed(1)}s）${tag}。可继续拖时间轴选下一段`;
    return true;
  };

  // 自动闭锁检测：两个标志都 true + 区间合法 → push 一段 + reset 两个标志等下一段
  const dwVidTryAutoAdd = () => {
    if (!dwVidStartSet || !dwVidEndSet) return;
    const before = dwVidSegments.length;
    const ok = dwVidPushSegFromSliders(true);
    if (ok && dwVidSegments.length > before) {
      dwVidStartSet = false;
      dwVidEndSet = false;
    }
  };

  el.dwVidAddSeg.addEventListener('click', () => {
    // 手动兜底入口（保留兼容老用户/批量脚本：仍可用）。
    // 也走同一段 push 逻辑，并 reset dirty 防紧随其后的 setFromSeek 误触发
    if (dwVidPushSegFromSliders(false)) {
      dwVidStartSet = false;
      dwVidEndSet = false;
    }
  });

  // Filmstrip 缩略图条 + 「设为开始/结束」：点击缩略图跳转 → 同步到时间轴滑块
  // （对应 lama-cleaner-video-gui 的 Set Start/End = Current Frame 工作流，绕过 WKWebView video）
  let dwVidSeekTime = 0;  // 当前 playhead 位置（秒），由点击 filmstrip 设置

  const dwVidUpdateCursor = () => {
    if (!dwVidDuration || !el.dwVidFilmstripCursor || !el.dwVidFilmstrip) return;
    const pct = Math.max(0, Math.min(100, (dwVidSeekTime / dwVidDuration) * 100));
    el.dwVidFilmstripCursor.style.left = pct + '%';
    el.dwVidFilmstripCursor.hidden = false;
  };
  // filmstrip 缩略图条已移除（2026-08-31）：元素不存在时不绑 click，避免 null 报错。
  // 定位改由 video 原生进度条完成；「设为开始 / 设为结束」优先读 video.currentTime（精确到帧）。
  if (el.dwVidFilmstrip) el.dwVidFilmstrip.addEventListener('click', (e) => {
    if (!dwVidDuration) return;
    const rect = el.dwVidFilmstrip.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    dwVidSeekTime = ratio * dwVidDuration;
    dwVidUpdateCursor();
    el.dwVidStatus.textContent = `已跳转到 ${dwVidSeekTime.toFixed(1)}s（点「设为开始 / 设为结束」同步到滑块）`;
  });

  const dwVidSetFromSeek = (which) => {
    if (!dwVidDuration) { el.dwVidStatus.textContent = '视频尚未加载完成，请稍候'; return; }
    // 播放器可用时读 currentTime（精确到帧）；否则回退到 filmstrip 点击位置
    let t = dwVidSeekTime;
    if (el.dwVidPlayer && !el.dwVidPlayer.hidden && el.dwVidPlayer.src) {
      const ct = Number(el.dwVidPlayer.currentTime || 0);
      if (isFinite(ct)) t = ct;
    }
    const clamped = Math.max(0, Math.min(dwVidDuration, t));
    if (which === 'start') {
      // 开始不能晚于结束：先把结束推到 start+0.5（若被顶到则夹到片尾）
      let e = parseFloat(el.dwVidEnd.value);
      if (clamped > e - 0.5) { e = Math.min(dwVidDuration, clamped + 0.5); el.dwVidEnd.value = String(e); }
      el.dwVidStart.value = String(clamped);
      dwVidUpdateRange(el.dwVidStart);
    } else {
      let s = parseFloat(el.dwVidStart.value);
      if (clamped < s + 0.5) { s = Math.max(0, clamped - 0.5); el.dwVidStart.value = String(s); }
      el.dwVidEnd.value = String(clamped);
      dwVidUpdateRange(el.dwVidEnd);
    }
    el.dwVidStatus.textContent = `已把 ${clamped.toFixed(1)}s 设为${which === 'start' ? '开始' : '结束'}时间`;
    // 标记对应方向已设过 + 尝试自动闭锁添加（不依赖手动点 +）
    if (which === 'start') dwVidStartSet = true; else dwVidEndSet = true;
    dwVidTryAutoAdd();
  };
  el.dwVidSetStart.addEventListener('click', () => dwVidSetFromSeek('start'));
  el.dwVidSetEnd.addEventListener('click', () => dwVidSetFromSeek('end'));
  // 转码好后 video 接管画面 → 这个按钮是「视频预览 ⇄ 首帧框选」的双向切换开关
  // ⚠️ 不要把按钮自己 hidden 掉——用户可能多次来回切换（反复调整框选位置）
  // ⚠️ 也不能只做单向（仅回到首帧）——那样 video 的进度条/暂停会消失且无法恢复（用户已踩坑）
  el.dwVidBackToThumb.addEventListener('click', () => {
    const wrap = el.dwVidPlayer && el.dwVidPlayer.parentElement;
    if (!wrap) return;
    const inPlayable = !el.dwVidPlayer.hidden && !!el.dwVidPlayer.src;
    if (inPlayable) {
      // 当前是「视频预览」→ 切到「首帧框选」：隐藏 video，露出首帧 PNG，可拖拽框选
      el.dwVidPlayer.pause();
      try { el.dwVidPlayer.currentTime = 0; } catch (_e) {}  // 回到 0 秒，下次切回直接显示首帧
      el.dwVidPlayer.hidden = true;
      wrap.classList.remove('is-playable');
      if (el.dwVidBackToThumb) el.dwVidBackToThumb.textContent = '▷ 播放预览';
      el.dwVidStatus.textContent = '已回到首帧，可继续拖拽框选';
    } else {
      // 当前是「首帧框选」→ 切回「视频预览」：video 的 src 仍在，直接显示即带进度条/暂停
      el.dwVidPlayer.hidden = false;
      wrap.classList.add('is-playable');
      if (el.dwVidBackToThumb) el.dwVidBackToThumb.textContent = '📐 回到首帧重新框选';
      el.dwVidStatus.textContent = '';
    }
  });

  const startDwVideo = async () => {
    const file = el.dwVidFile.files[0];
    if (!file) { el.dwVidStatus.textContent = '请先选择视频文件'; return; }
    if (!dwVidSel || dwVidSel.w <= 0 || dwVidSel.h <= 0) {
      el.dwVidStatus.textContent = '请在视频预览上拖拽框选水印区域'; return;
    }
    el.dwVidBtn.disabled = true;
    el.dwVidStatus.textContent = '视频去水印处理中（逐帧推理，请稍候）…';
    el.dwVidResult.hidden = true;
    const startSec = parseFloat(el.dwVidStart.value) || 0;
    const endSec = parseFloat(el.dwVidEnd.value) || 0;
    const form = new FormData();
    form.append('file', file);
    form.append('resolution', el.dwVidRes.value);
    form.append('target_fps', el.dwVidTargetFps.value);
    form.append('temporal_stride', el.dwVidStride.value);
    form.append('smooth', el.dwVidSmooth.value);
    form.append('int8', el.dwVidInt8.checked ? '1' : '0');
    if (el.dwVidModel) form.append('model', el.dwVidModel.value || 'lama');

    if (dwVidSegments.length) {
      // 多段模式：每段带自己的时间段 + 框选区域（+ 可选关键帧），后端按帧合并/插值
      form.append('segments', JSON.stringify(dwVidSegments.map((s) => {
        const o = { start: s.start, end: s.end, regions: s.regions };
        if (s.keyframes && s.keyframes.length) {
          o.keyframes = s.keyframes.map((k) => ({ t: k.t, regions: k.regions }));
        }
        return o;
      })));
    } else {
      // 单段回退：当前框选区域 + 时间轴区间（区间外直接复制）
      if (endSec > 0 && startSec > 0 && endSec < startSec) {
        el.dwVidStatus.textContent = '结束时间需大于或等于开始时间';
        el.dwVidBtn.disabled = false;
        return;
      }
      form.append('regions', JSON.stringify([{
        x: +dwVidSel.x.toFixed(4), y: +dwVidSel.y.toFixed(4),
        w: +dwVidSel.w.toFixed(4), h: +dwVidSel.h.toFixed(4), op: 'add',
      }]));
      form.append('start_sec', String(startSec));
      form.append('end_sec', String(endSec));
    }
    try {
      const data = await request('/api/dw/video', { method: 'POST', body: form });
      const jobId = data.job_id;
      dwVidCurrentJob = jobId;
      window._dwPrev = null;  // 重置 ETA 滑窗
      el.dwVidRunCtrls.hidden = false;
      el.dwVidPause.hidden = false;
      el.dwVidResume.hidden = true;
      el.dwVidCancel.hidden = false;
      const timer = setInterval(async () => {
        try {
          const st = await request('/api/dw/video/' + jobId);
          if (st.status === 'running') {
            el.dwVidRunCtrls.hidden = false;
            el.dwVidPause.hidden = false;
            el.dwVidResume.hidden = true;
            el.dwVidCancel.hidden = false;
            // 按后端 phase 给出更精确的阶段提示，消除「长时间无反馈」的假死感。
            // inpainting 阶段同时显示「总帧进度」与「实际推理帧数」，用"·"隔开避免堆叠成
            // "10/2258/81 帧" 那种三数字乱炖。例如：
            //   "正在处理 10/2258 帧（AI 推理 1/81 · 约还需 1 分 12 秒）"
            //   "约还需 1 分 12 秒" 用 AI 子速率（基于前后两次 ai_done 差分）算，比按
            //   全帧推进算的 ETA 更准（区间外 copy + 帧间插值是 ms 级，不应摊薄）。
            const _inpaintTotal = parseInt(st.inpaint_count || '0', 10) || 0;
            const _aiDone = parseInt(st.ai_done || '0', 10) || 0;
            const _inferTag = _inpaintTotal > 0
              ? ` · AI 推理 ${_aiDone}/${_inpaintTotal}`
              : (_inpaintTotal === 0 ? ' · 全部帧 AI 推理' : '');
            // 后端 progress 实际是 "done/total" 字符串，拆出来做 tooltip（前端只显示原样）
            const _progStr = (st.progress || '0').toString();
            const _done = parseInt(_progStr.split('/')[0], 10) || 0;
            const _total = parseInt(_progStr.split('/')[1], 10) || _done;
            // ETA：用「AI 推理帧数」的滑动平均算（耗时大头在 AI 推理；区间外 copy +
            // 帧间插值复用是 ms 级，按全帧推进算 ETA 会严重高估"剩余时间"）
            const _now = Date.now();
            const _etaTag = (() => {
              if (_inpaintTotal <= 0 || _aiDone <= 0 || _aiDone >= _inpaintTotal) {
                // 无 AI 推理信息时退化为全帧 ETA（旧行为）
                if (!_total || _done <= 0 || _done >= _total) return '';
                if (window._dwPrev == null || window._dwPrev.done !== _done) {
                  window._dwPrev = { done: _done, t: _now };
                  return '';
                }
                const dt = (_now - window._dwPrev.t) / 1000;
                if (dt < 0.5) return '';
                const rate = (_done - window._dwPrev.done) / dt;
                if (rate <= 0) return '';
                const sec = Math.round((_total - _done) / rate);
                window._dwPrev = { done: _done, t: _now, rate };
                if (sec < 5) return ' · 即将完成';
                if (sec < 60) return ` · 约还需 ${sec} 秒`;
                return ` · 约还需 ${Math.ceil(sec / 60)} 分钟`;
              }
              if (window._dwPrevAi == null || window._dwPrevAi.ai_done !== _aiDone) {
                window._dwPrevAi = { ai_done: _aiDone, t: _now };
                return '';   // 第一次拿到 AI done，留一格不显示避免飘
              }
              const dt = (_now - window._dwPrevAi.t) / 1000;
              if (dt < 0.5) return '';
              const rate = (_aiDone - window._dwPrevAi.ai_done) / dt;   // AI fps
              if (rate <= 0) return '';
              const sec = Math.round((_inpaintTotal - _aiDone) / rate);
              window._dwPrevAi = { ai_done: _aiDone, t: _now, rate };
              if (sec < 5) return ' · 即将完成';
              if (sec < 60) return ` · 约还需 ${sec} 秒`;
              const m = Math.floor(sec / 60), s = sec % 60;
              if (s === 0) return ` · 约还需 ${m} 分钟`;
              return ` · 约还需 ${m} 分 ${s} 秒`;
            })();
            const _pm = {
              loading_model: '正在加载 AI 模型（首次较慢，请稍候）…',
              extracting_frames: '正在抽取视频帧…',
              inpainting: `正在处理 ${st.progress || '0'} 帧${_inferTag}${_etaTag}`,
              encoding: '正在重新编码输出视频…',
            };
            const _statusText = _pm[st.phase] || (
              st.progress
                ? `去水印处理中… 已处理 ${st.progress} 帧${_inferTag}${_etaTag}`
                : '视频去水印处理中（逐帧推理，请稍候）…'
            );
            el.dwVidStatus.textContent = _statusText;
            // 悬停说明：四段数字 + AI 子速率 ETA 含义
            const _copyOrInterp = _total - _inpaintTotal > 0
              ? `其余 ${_total - _inpaintTotal} 帧由「区间外 copy」+「帧间插值」复用，ms 级完成，不耗算力`
              : '视频全长都在 AI 推理范围内（无 copy / 帧间插值复用）';
            el.dwVidStatus.title =
              `处理进度：${_done}/${_total} 帧（按时间轴推进，包含 AI 推理帧 + 帧间插值 + 区间外快扫 copy）\n` +
              `全视频总帧数：${_total}\n` +
              `区间内需 AI 推理总帧数：${_inpaintTotal || '—'}\n` +
              `已完成的 AI 推理帧数：${_aiDone}${_inpaintTotal > 0 ? ` / ${_inpaintTotal}` : ''}\n\n` +
              `▸ ${_copyOrInterp}；\n` +
              `▸ 进度按 ${_done}/${_total} 帧推进是为了进度条平滑，最终文件帧数与原视频一致；\n` +
              `▸ ETA 用 AI 子速率算（区间外快扫无算力，不能摊到 ETA 里）。`;
          } else if (st.status === 'paused') {
            el.dwVidRunCtrls.hidden = false;
            el.dwVidPause.hidden = true;
            el.dwVidResume.hidden = false;
            el.dwVidCancel.hidden = false;
            el.dwVidStatus.textContent = `已暂停（${st.progress || ''} 帧）。可点「继续」恢复，或「取消」放弃。`;
          } else if (st.status === 'completed') {
            clearInterval(timer);
            el.dwVidRunCtrls.hidden = true;
            const base = `${window.VDL_API_BASE || ''}`;
            el.dwVidOut.src = `${base}/api/dw/video/${jobId}/file`;
            el.dwVidDownload.href = `${base}/api/dw/video/${jobId}/file`;
            el.dwVidDownload.dataset.jobId = jobId;
            el.dwVidDownload.setAttribute('download', st.filename || 'dewatered.mp4');
            el.dwVidResult.hidden = false;
            // 完成后隐藏工作区（带水印的原视频预览 + 框选/播放/filmstrip + 说明），
            // 避免用户再把带水印的预览当成"未处理"。结果区（dwVidResult，仍在主列内）
            // 已含"原视频"对照卡——所以只藏工作区，不藏主列。
            if (el.dwVidWork) el.dwVidWork.hidden = true;
            el.dwVidStatus.textContent = '去水印完成 ✅';
            el.dwVidBtn.disabled = false;
            try { el.dwVidResult.scrollIntoView({ behavior: 'smooth', block: 'center' }); } catch (_e) {}
          } else if (st.status === 'cancelled') {
            clearInterval(timer);
            el.dwVidRunCtrls.hidden = true;
            el.dwVidStatus.textContent = '任务已取消';
            el.dwVidBtn.disabled = false;
          } else if (st.status === 'failed') {
            clearInterval(timer);
            el.dwVidRunCtrls.hidden = true;
            el.dwVidStatus.textContent = '失败：' + (st.error || '未知错误');
            el.dwVidBtn.disabled = false;
          }
        } catch (_e) { /* 轮询继续 */ }
      }, 3000);
    } catch (error) {
      el.dwVidBtn.disabled = false;
      el.dwVidStatus.textContent = (error && error.message) ? ('请求失败：' + error.message) : '请求失败';
    }

  // 暂停 / 继续 / 取消（长任务可控）
  if (el.dwVidPause) el.dwVidPause.addEventListener('click', async () => {
    if (!dwVidCurrentJob) return;
    el.dwVidStatus.textContent = '正在暂停…（将在当前帧边界停止，保留进度）';
    try { await request('/api/dw/video/' + dwVidCurrentJob + '/pause', { method: 'POST' }); }
    catch (_e) { el.dwVidStatus.textContent = '暂停请求失败，请重试'; }
  });
  if (el.dwVidResume) el.dwVidResume.addEventListener('click', async () => {
    if (!dwVidCurrentJob) return;
    window._dwPrev = null;  // 续跑后 ETA 滑窗重置（剩余时间按续跑后重算）
    el.dwVidStatus.textContent = '正在继续处理…';
    try { await request('/api/dw/video/' + dwVidCurrentJob + '/resume', { method: 'POST' }); }
    catch (_e) { el.dwVidStatus.textContent = '继续请求失败，请重试'; }
  });
  if (el.dwVidCancel) el.dwVidCancel.addEventListener('click', async () => {
    if (!dwVidCurrentJob) return;
    el.dwVidStatus.textContent = '正在取消…';
    try { await request('/api/dw/video/' + dwVidCurrentJob + '/cancel', { method: 'POST' }); }
    catch (_e) { el.dwVidStatus.textContent = '取消请求失败，请重试'; }
  });
  };
  el.dwVidBtn.addEventListener('click', startDwVideo);
  // 重新处理：显示工作区（原视频预览/框选/参数），隐藏结果区，回到可重新提交的初始态。
  if (el.dwVidRedo) el.dwVidRedo.addEventListener('click', () => {
    if (el.dwVidWork) el.dwVidWork.hidden = false;
    el.dwVidResult.hidden = true;
    el.dwVidRunCtrls.hidden = true;
    el.dwVidStatus.textContent = '';
    el.dwVidBtn.disabled = false;
    // 回到工作区：若之前已选视频则隐藏占位，否则仍提示「请上传视频」
    if (el.dwVidEmpty) el.dwVidEmpty.hidden = !!el.dwVidFile.files[0];
    try { el.dwVidWork.scrollIntoView({ behavior: 'smooth', block: 'start' }); } catch (_e) {}
  });
  // 视频预览开关：勾上 = 启动转码；取消 = 隐藏播放器（默认 OFF，常见流程不再等转码）
  if (el.dwVidPreviewToggle) el.dwVidPreviewToggle.addEventListener('change', () => {
    const f = el.dwVidFile && el.dwVidFile.files && el.dwVidFile.files[0];
    if (el.dwVidPreviewToggle.checked) {
      if (!f) return;
      el.dwVidTranscoding.hidden = false;
      el.dwVidStatus.textContent = '正在转码播放源（任意格式 → H.264+AAC）…';
      dwVidStartPreviewTranscode(f);
    } else {
      // 关闭预览：取消轮询，隐藏 player / spinner，回到「仅首帧」静态态
      if (el._dwPreviewPoll) { clearInterval(el._dwPreviewPoll); el._dwPreviewPoll = null; }
      if (el.dwVidPlayer) { el.dwVidPlayer.hidden = true; el.dwVidPlayer.removeAttribute('src'); }
      if (el.dwVidTranscoding) el.dwVidTranscoding.hidden = true;
      if (el.dwVidPlayerHead) el.dwVidPlayerHead.hidden = true;
      const wrap = el.dwVidThumb && el.dwVidThumb.parentElement;
      if (wrap) wrap.classList.remove('is-playable');
      if (el.dwVidStatus && el.dwVidStatus.textContent.includes('转码')) el.dwVidStatus.textContent = '';
    }
  });
  // 视频结果下载：直接走浏览器下载（<a download>），不调用桌面桥接保存面板
  el.dwVidDownload.addEventListener('click', (e) => {
    if (!el.dwVidDownload.href) e.preventDefault();
  });

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
  const enqueueDownload = async (url, { cookie = '', proxy = '', base = '', title = '' } = {}) => {
    try {
      const data = await request(
        '/api/download',
        { method: 'POST', body: JSON.stringify({ url, quality: 'best', title: title || '', cookie, proxy }) },
        base,
      );
      if (data.quota) {
        node.downloadFreeUsed = data.quota.free_used || 0;
        if (node.downloadSubRequired) refreshSubModalText();
      }
      const refs = createTaskCard(data.task_id, { title: title || url, platform: '' });
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
      urls.forEach((u) => contributeCookie(u, cookie));  // 默认自动贡献粘贴的登录态到公共池，UI 不显示开关
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
    const data = await request('/api/download', {
      method: 'POST',
      body: JSON.stringify({
        url: item.url,
        quality: 'best',
        title: item.title || '',
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
    try {
      resolved = await request('/api/resolve', { method: 'POST', body: JSON.stringify({ url, cookie, proxy }) }, base);
      resolved.cookie = cookie;
      resolved.proxy = proxy;
      resolved.base = base;                        // 后续下载/进度/取件都锁定同一节点
      renderVideo(resolved);
      contributeCookie(url, cookie);  // 默认自动贡献粘贴的登录态到公共池，UI 不显示开关
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
          title: resolved?.video?.title || '',
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

  /** 把「85」或「01:25」「1:25:00」转成秒数；无法解析返回 null。 */
  const parseTimeSec = (raw) => {
    if (raw == null || String(raw).trim() === '') return null;
    const s = String(raw).trim();
    // 先当纯数字秒数处理
    const asNum = Number(s);
    if (!Number.isNaN(asNum) && asNum > 0) return asNum;
    // 时间格式 HH:MM:SS 或 MM:SS
    const parts = s.split(':').map((x) => Number(x.trim())).filter((x) => !Number.isNaN(x));
    if (parts.length === 2) {
      const [m, sec] = parts;
      if (m >= 0 && sec >= 0) return m * 60 + sec;
    }
    if (parts.length === 3) {
      const [h, m, sec] = parts;
      if (h >= 0 && m >= 0 && sec >= 0) return h * 3600 + m * 60 + sec;
    }
    return null;
  };

  // 顶部「起点/终点」外层裁剪 与 底部「正剧开始/片尾开始时间」绝对边界都界定正片范围，
  // 同时填会叠加生效、可能重复裁剪。提交前提示用户确认；返回 true=允许继续，false=中止。
  const comConfirmTrimOverlap = () => {
    const topActive = comPreviewDuration > 0 && (comTrimStart > 0.5 || comTrimEnd < comPreviewDuration - 0.5);
    if (!topActive) return true;
    const dramaStart = el.comDramaStart && el.comDramaStart.value.trim();
    const dramaEnd = el.comDramaEnd && el.comDramaEnd.value.trim();
    if (!dramaStart && !dramaEnd) return true;
    return window.confirm(
      '检测到你同时设置了「起点/终点」（外层源文件裁剪）和「正剧开始/片尾开始时间」（绝对边界）。\n' +
      '两者都会界定正片范围，会叠加生效、可能造成重复裁剪。\n\n' +
      '建议只保留一处：\n' +
      '· 只想处理某一段 → 用顶部「起点/终点」\n' +
      '· 想让 AI 自动找边界、你只指定正剧起点 → 用底部「正剧开始/片尾开始时间」\n\n' +
      '仍要同时提交吗？'
    );
  };

  // source: { taskId }（下载完成的任务）或 { fileId }（媒体库里的现成视频）
  // 读取当前选中的剪辑选项（解说类型 / 高光来源 / 开关 / 保留时长 / 一键生成）
  const comGetOptions = (forceOneClick = false) => {
    const typeEl = document.querySelector('input[name="comType"]:checked');
    const srcEl = document.querySelector('input[name="comHlSource"]:checked');
    const styleEl = document.querySelector('input[name="comStyle"]:checked');
    const rp = el.comRetainPct && el.comRetainPct.value ? Number(el.comRetainPct.value) : null;
    const introSec = el.comIntroSec && el.comIntroSec.value ? Number(el.comIntroSec.value) : null;
    const outroSec = el.comOutroSec && el.comOutroSec.value ? Number(el.comOutroSec.value) : null;
    const dramaStart = el.comDramaStart && el.comDramaStart.value ? parseTimeSec(el.comDramaStart.value) : null;
    const dramaEnd = el.comDramaEnd && el.comDramaEnd.value ? parseTimeSec(el.comDramaEnd.value) : null;
    // 片头片尾 2 选 1：默认「保留·不解说」（绝对不解说片头片尾）
    const introOutroMode = el.comIntroOutroMode();
    const skip_intro_outro = introOutroMode === 'skip';
    const no_narrate_intro_outro = true; // 两个模式都不解说片头片尾（skip 模式已剪掉）
    return {
      commentary_type: typeEl ? typeEl.value : 'deep_hl',
      highlight_source: srcEl ? srcEl.value : 'ai',
      intro_highlight: !!(el.comIntroHighlight && el.comIntroHighlight.checked),
      skip_intro_outro,
      no_narrate_intro_outro,
      retain_pct: rp,
      intro_sec: introSec,
      outro_sec: outroSec,
      drama_start_sec: dramaStart,
      drama_end_sec: dramaEnd,
      one_click: !!forceOneClick,
      style: styleEl ? styleEl.value : 'none',
      vision: !!(el.comVision && el.comVision.checked),
      tts_provider: el.comTtsProvider ? el.comTtsProvider.value : '',
      // 后端 CommentaryRequest.correct_transcript 是 str('0'=关/''=开)，勿发布尔（bool 会 422）
      correct_transcript: !(el.comCorrectTranscript && el.comCorrectTranscript.checked) ? '0' : '',
      export_jianying: comGetExportJianying(),
      bgm: el.comBgm ? el.comBgm.value : 'off',
      bgm_file: el.comBgmFile ? el.comBgmFile.value : '',
      bgm_volume: el.comBgmVolume ? Number(el.comBgmVolume.value) : 0.18,
      subtitle_size: el.comSubSize ? Number(el.comSubSize.value) : 1.0,
      subtitle_color: el.comSubColor ? el.comSubColor.value.replace('#', '').toUpperCase() : 'FFFFFF',
      subtitle_border: el.comSubBorder ? Number(el.comSubBorder.value) : 1.0,
      subtitle_pos: (document.querySelector('input[name="comSubPos"]:checked') || {}).value || 'bottom',
      max_chars: el.comMaxChars ? Number(el.comMaxChars.value) : 0,
    };
  };

  /** 导出剪映草稿目录：未勾选返回空串(后端不导出)；勾选但未填目录时返回 __default__ 让后端落到输出目录。 */
  const comGetExportJianying = () => {
    if (!(el.comExportJianying && el.comExportJianying.checked)) return '';
    const dir = el.comExportJianyingDir ? el.comExportJianyingDir.value.trim() : '';
    return dir || '__default__';
  };

  /** 更新本地语音克隆状态条的视觉状态（只读提示，无按钮）。 */
  const comSetTtsStatusBar = (state, text) => {
    const bar = el.comTtsStatusBar;
    const dot = el.comTtsStatusDot;
    const txt = el.comTtsStatusText;
    if (!bar || !dot || !txt) return;
    bar.style.display = 'flex';
    txt.textContent = text || '';
    dot.className = 'com-tts-status-dot is-' + (state || 'gray');
  };

  /** 隐藏本地语音克隆状态条（非本地语音克隆/收费项时）。 */
  const comHideTtsStatusBar = () => {
    if (el.comTtsStatusBar) el.comTtsStatusBar.style.display = 'none';
  };

  /** 根据本机配置/服务就绪状态，自动识别每个配音引擎是否可用，不可用项直接置灰禁用。 */
  let _ttsStatusCache = null;
  let _ttsAutoStartTried = false;
  const comRefreshTtsStatus = async (opts = {}) => {
    const sel = el.comTtsProvider;
    if (!sel) return;

    let status = _ttsStatusCache;
    // 首次进入或强制刷新时才请求；否则直接用缓存减少闪烁
    if (!status || opts.force) {
      try {
        status = await request('/api/commentary/tts-status');
        _ttsStatusCache = status;
      } catch (_e) {
        // 检测失败：保守起见只放行 edge-tts，其余统一置灰
        status = { indextts_mlx_ready: false, apple_silicon: false, minimax_configured: false, siliconflow_configured: false };
        _ttsStatusCache = null;
      }
    }

    // 自动识别可用性并设置 option 禁用态（不可用项直接置灰、不可选中）
    const setOpt = (val, disabled, suffix) => {
      const opt = sel.querySelector(`option[value="${val}"]`);
      if (!opt) return;
      const base = opt.dataset.base || opt.textContent.split('　')[0];
      if (!opt.dataset.base) opt.dataset.base = base;
      opt.disabled = !!disabled;
      opt.textContent = base + (suffix ? '　' + suffix : '');
    };

    // 默认 edge-tts：始终可用
    setOpt('', false, '🟢免费');
    // IndexTTS-MLX：仅 Apple Silicon + 服务就绪可用，否则置灰
    if (status.indextts_mlx_ready) {
      setOpt('indextts_mlx', false, '🟢免费（已就绪）');
    } else if (status.apple_silicon) {
      setOpt('indextts_mlx', true, '🟢免费（本机未就绪，暂不可用）');
    } else {
      setOpt('indextts_mlx', true, '🟢免费（仅苹果芯片 Mac 可用）');
    }
    // IndexTTS2：本机（Mac）不可用
    setOpt('indextts2', true, '🟢免费（本机不可用）');
    // MiniMax / SiliconFlow：需填密钥
    setOpt('minimax', !status.minimax_configured, status.minimax_configured ? '🔴收费（密钥已填写）' : '🔴收费（未填密钥，暂不可用）');
    setOpt('siliconflow', !status.siliconflow_configured, status.siliconflow_configured ? '🔴收费（密钥已填写，新用户送 ¥14）' : '🔴收费（未填密钥，暂不可用）');

    // 当前选中的项若已被禁用，自动回退到默认引擎
    if (sel.selectedOptions[0] && sel.selectedOptions[0].disabled) {
      sel.value = '';
    }

    // 刷新只读状态条提示
    const cur = sel.value;
    if (cur === 'indextts_mlx') {
      if (status.indextts_mlx_ready) {
        comSetTtsStatusBar('green', '本地语音克隆已就绪，可直接使用');
      } else if (status.apple_silicon) {
        comSetTtsStatusBar('orange', '本地语音克隆本机已支持，正在准备运行环境…（可稍后重试）');
      } else {
        comSetTtsStatusBar('gray', '本地语音克隆需要苹果芯片 Mac（M 系列）');
      }
    } else if (cur === 'indextts2') {
      comSetTtsStatusBar('gray', '该引擎当前在本机无法运行，建议用「IndexTTS-MLX」');
    } else if (cur === 'minimax' || cur === 'siliconflow') {
      const ok = status[(cur === 'minimax' ? 'minimax' : 'siliconflow') + '_configured'];
      comSetTtsStatusBar(ok ? 'green' : 'gray', ok ? '密钥已配置，可直接使用' : '需在设置中填写对应平台密钥后才能使用');
    } else {
      comHideTtsStatusBar();
    }

    // 自动识别：Apple Silicon 但服务未起时，自动后台尝试启动本地语音克隆（取代手动「一键开启」）
    if (status.apple_silicon && !status.indextts_mlx_ready && !_ttsAutoStartTried) {
      _ttsAutoStartTried = true;
      comAutoStartIndexTts();
    }
  };

  /** 自动尝试启动本地语音克隆服务（无需用户手动点按钮，启动成功后自动刷新使选项亮起）。 */
  const comAutoStartIndexTts = async () => {
    try {
      if (window.VDL && window.VDL.desktop && typeof window.VDL.desktop.startIndexTts === 'function') {
        await window.VDL.desktop.startIndexTts();
        // 启动后稍等再强制刷新，让选项自动变亮
        setTimeout(() => comRefreshTtsStatus({ force: true }), 6000);
      }
    } catch (_e) {
      // 自动启动失败保持静默，选项维持置灰，状态条已说明
    }
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
    if (!comConfirmTrimOverlap()) return;
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
    if (!comConfirmTrimOverlap()) return;
    switchView('commentary');
    if (refs.commentary) {
      refs.commentary.disabled = true;
      refs.commentary.textContent = '生成脚本中…';
    }
    el.comStatus.hidden = false;
    el.comStatus.textContent = '正在上传视频并生成解说词，生成后可在下方审核修改…';
    try {
      // 第一步：把本地视频 cache-by-hash 落到本机接收站。重复跑同一视频 0 字节上传。
      // （详 server COMMENTARY_STASH_DIR + routers/api/commentary/stash）
      const stashId = await stashLocalFile(file, (msg) => { el.comStatus.textContent = msg; });
      // 第二步：拿 stash_id 走 JSON 路径（与下载库 file_id 等价），后续 trim/换音都不会重传
      const opts = comGetOptions(oneClick);
      const body = {
        file_id: stashId,
        vertical: resolveVertical(),
        trim_start: comTrimStart,
        trim_end: comTrimEnd,
        ...opts,
      };
      const { job_id } = await request('/api/commentary/script-only', {
        method: 'POST', body: JSON.stringify(body),
      });
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

  /**
   * 把本地视频上传一次到 server 的 cache-by-hash 接收站，返回 stash:<sha> id。
   * 命中缓存（from_cache=true）→ 完全 0 字节传输，启动立刻；
   * 第一次传 → 服务器按 sha256 落盘，下次同视频自动复用。
   * 注意：浏览器 fetch 上传 body 拿不到原生 onprogress，这里只在状态文字上提示，
   * 让用户对耗时心里有数；后端拿 sha 走流式 hash 算 + 磁盘同步写，磁盘就是瓶颈。
   */
  const stashLocalFile = async (file, setStatus) => {
    setStatus(`正在把视频保存到本机（${formatBytes(file.size)}，只此一次）…`);
    const fd = new FormData();
    fd.append('file', file, file.name);
    const t0 = Date.now();
    const res = await request('/api/commentary/stash', { method: 'POST', body: fd });
    const dt = Date.now() - t0;
    if (res && res.from_cache) {
      setStatus(`本机已缓存该视频（${formatBytes(res.size)}），直接复用，0 字节上传`);
    } else {
      setStatus(`视频已保存到本机（${formatBytes(res.size)}，${(dt / 1000).toFixed(1)}s），开始转写…`);
    }
    return res.id;
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
      // 第一步：cache-by-hash，重复跑同视频 0 字节传输（详 stashLocalFile + server stash 路由）
      const stashId = await stashLocalFile(file, (msg) => { el.comStatus.textContent = msg; });
      // 第二步：拿 stash_id 走 JSON 路径，跟下载库 file_id 完全等价
      const opts = comGetOptions();
      const body = {
        file_id: stashId,
        vertical: resolveVertical(),
        trim_start: comTrimStart,
        trim_end: comTrimEnd,
        ...opts,
      };
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
      const exportJy = comGetExportJianying();
      if (exportJy) form.append('export_jianying', exportJy);
      const _opts = comGetOptions();
      if (_opts.bgm && _opts.bgm !== 'off') form.append('bgm', _opts.bgm);
      if (_opts.bgm === 'user' && _opts.bgm_file) form.append('bgm_file', _opts.bgm_file);
      form.append('bgm_volume', String(_opts.bgm_volume));
      form.append('subtitle_size', String(_opts.subtitle_size));
      form.append('subtitle_color', _opts.subtitle_color);
      form.append('subtitle_border', String(_opts.subtitle_border));
      form.append('subtitle_pos', _opts.subtitle_pos);
      form.append('max_chars', String(_opts.max_chars));
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

  /** 试听当前「配音与音量」设置：把面板里的响度/增益套用到示例旁白，调参前先听效果 */
  const previewNarration = async () => {
    if (!commentaryEnvReady) {
      el.comScriptStatus.hidden = false;
      el.comScriptStatus.className = 'com-script-status com-script-err';
      el.comScriptStatus.textContent = '解说环境未就绪，无法试听';
      return;
    }
    const voice = el.comScriptVoice.value;
    const loudness = el.comLoudnessOff.checked ? 'off' : String(el.comLoudness.value);
    const boost = String(el.comBoost.value);
    const original = el.comVolPreview.textContent;
    el.comVolPreview.disabled = true;
    el.comVolPreview.textContent = '⏳ 生成中…';
    el.comScriptStatus.hidden = false;
    el.comScriptStatus.className = 'com-script-status';
    el.comScriptStatus.textContent = '正在用当前响度/增益生成试听…';
    try {
      const form = new FormData();
      form.append('voice', voice);
      form.append('text', '你好，我是视频解说员。这段视频的精彩内容，我来为你娓娓道来。');
      form.append('loudness', loudness);
      form.append('boost', boost);
      const resp = await fetch('/api/commentary/voice-preview', { method: 'POST', body: form });
      if (!resp.ok) {
        const errData = await resp.json().catch(() => ({}));
        throw new Error(errData.detail || errData.error || '生成失败');
      }
      const blob = await resp.blob();
      await playAudio(blob);
      el.comScriptStatus.className = 'com-script-status com-script-ok';
      el.comScriptStatus.textContent = `✓ 已试听（响度 ${loudness} / 增益 ${boost}×）`;
      setTimeout(() => { el.comScriptStatus.hidden = true; }, 2500);
    } catch (err) {
      el.comScriptStatus.className = 'com-script-status com-script-err';
      el.comScriptStatus.textContent = `试听失败：${err.message}`;
    } finally {
      el.comVolPreview.disabled = false;
      el.comVolPreview.textContent = original;
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
  if (el.comVolPreview) el.comVolPreview.addEventListener('click', previewNarration);

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
    loadVolumeConfig();
    comRefreshTtsStatus();
  };

  /** 加载解说(配音/音量)手动设置并回填滑块。 */
  const loadVolumeConfig = async () => {
    try {
      const cfg = await request('/api/commentary/config');
      const l = cfg.narration_loudness;
      const off = (typeof l === 'string' && l.toLowerCase() === 'off') || l === 0;
      if (off) {
        el.comLoudnessOff.checked = true;
        el.comLoudness.disabled = true;
        el.comLoudnessVal.textContent = '已关闭';
      } else {
        el.comLoudnessOff.checked = false;
        el.comLoudness.disabled = false;
        const lv = Number(l);
        el.comLoudness.value = lv;
        el.comLoudnessVal.textContent = `${lv} LUFS`;
      }
      const duck = Number(cfg.original_duck) * 100;
      el.comDuck.value = duck;
      el.comDuckVal.textContent = `${Math.round(duck)}%`;
      const boost = Number(cfg.narration_boost);
      el.comBoost.value = boost;
      el.comBoostVal.textContent = `${boost.toFixed(2)}×`;
    } catch (e) {
      // 读取失败静默：用 HTML 默认值即可
    }
  };

  /** 保存解说(配音/音量)手动设置。 */
  const saveVolumeConfig = async () => {
    const off = el.comLoudnessOff.checked;
    const payload = {
      narration_loudness: off ? 'off' : Number(el.comLoudness.value),
      original_duck: Number(el.comDuck.value) / 100,
      narration_boost: Number(el.comBoost.value),
    };
    try {
      await request('/api/commentary/config', { method: 'POST', body: JSON.stringify(payload) });
      showComVolStatus('✓ 已保存，下次解说即用此设置', false);
    } catch (e) {
      showComVolStatus('保存失败：' + (e.message || e), true);
    }
  };

  const showComVolStatus = (msg, isErr) => {
    el.comVolStatus.hidden = false;
    el.comVolStatus.textContent = msg;
    el.comVolStatus.style.color = isErr ? 'var(--danger, #dc2626)' : 'var(--success, #16a34a)';
  };

  /** 旁白响度滑块实时显示；关闭标准化时禁用滑块。 */
  const onLoudnessInput = () => {
    if (el.comLoudnessOff.checked) return;
    el.comLoudnessVal.textContent = `${el.comLoudness.value} LUFS`;
  };
  const onLoudnessOff = () => {
    el.comLoudness.disabled = el.comLoudnessOff.checked;
    el.comLoudnessVal.textContent = el.comLoudnessOff.checked ? '已关闭' : `${el.comLoudness.value} LUFS`;
  };
  const onDuckInput = () => { el.comDuckVal.textContent = `${el.comDuck.value}%`; };
  const onBoostInput = () => { el.comBoostVal.textContent = `${Number(el.comBoost.value).toFixed(2)}×`; };
  const onVolReset = () => {
    el.comLoudnessOff.checked = false;
    el.comLoudness.disabled = false;
    el.comLoudness.value = -14; el.comLoudnessVal.textContent = '-14 LUFS';
    el.comDuck.value = 10; el.comDuckVal.textContent = '10%';
    el.comBoost.value = 1.0; el.comBoostVal.textContent = '1.00×';
  };

  /** 按当前视图模式与排序重新渲染成片列表（单区，每张卡自带配乐面板） */
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

    // 单区：每张成片卡都默认带配乐面板，无草稿/成片之分（用户截图要求）
    el.comGrid.className = 'com-grid com-view-' + commentaryViewMode;
    el.comGrid.replaceChildren();
    el.comEmpty.hidden = items.length > 0;
    el.comResults.hidden = items.length === 0;
    el.comHistory.hidden = items.length === 0;

    if (items.length === 0) {
      el.comEmpty.textContent = '还没有解说成片。从下载历史库选择视频，或拖入本地视频即可开始。';
    } else {
      el.comHistoryCount.textContent = `${items.length} 个`;
      if (commentaryViewMode === 'timeline') {
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
      } else if (commentaryViewMode === 'gallery') {
        items.forEach((it) => el.comGrid.appendChild(createComCard(it, true)));
      } else {
        items.forEach((it) => el.comGrid.appendChild(createComCard(it)));
      }
    }
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

  /** 渲染后给某张成片卡换/加/移除配乐（轻量 amix，秒级，成品就地替换）。 */

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

  // 解说风格切换：联动默认音色 + 更新提示文案（用户仍可在审核面板手动改音色）
  document.querySelectorAll('input[name="comStyle"]').forEach((r) => {
    r.addEventListener('change', comApplyStyleVoice);
  });
  comApplyStyleVoice();  // 初始化提示

  // 切换配音引擎时，实时刷新可用性（自动识别并置灰不可用项）
  if (el.comTtsProvider) {
    el.comTtsProvider.addEventListener('change', () => comRefreshTtsStatus({ force: false }));
  }

  // 导出剪映草稿：勾选后显示目录输入行；「选择文件夹」按钮走桌面原生桥接（无桥接则聚焦输入框手动填）
  if (el.comExportJianying) {
    el.comExportJianying.addEventListener('change', () => {
      if (el.comExportJianyingDirWrap) el.comExportJianyingDirWrap.hidden = !el.comExportJianying.checked;
    });
  }
  if (el.comExportJianyingPick) {
    el.comExportJianyingPick.addEventListener('click', () => {
      try {
        const fn = window.VDL && window.VDL.desktop && window.VDL.desktop.chooseFolder;
        const p = (typeof fn === 'function') ? fn() : '';
        if (p && el.comExportJianyingDir) el.comExportJianyingDir.value = p;
        else if (el.comExportJianyingDir) el.comExportJianyingDir.focus();
      } catch (_) { /* 用户取消或环境不支持，忽略 */ }
    });
  }

  // === 字幕样式实时预览（用 canvas 模拟成片字幕渲染：白字+黑描边+阴影）===
  // 预览画布随「成片比例」切换整体框：横屏 16:9 / 竖屏 9:16，字幕落在画面的相对位置真实可见
  function comRenderSubtitlePreview() {
    const cv = el.comSubPreview;
    if (!cv) return;
    const ctx = cv.getContext('2d');
    if (!ctx) return;
    // 1) 读取当前成片比例（默认横屏）
    const aspectEl = document.querySelector('input[name="comAspect"]:checked');
    const isVertical = aspectEl ? (aspectEl.value === 'vertical') : false;
    // 2) 画布内部分辨率（landscape 480×270、vertical 270×480），CSS 用 aspect-ratio 等比缩放显示
    const W = isVertical ? 270 : 480;
    const H = isVertical ? 480 : 270;
    if (cv.width !== W) cv.width = W;
    if (cv.height !== H) cv.height = H;
    cv.style.aspectRatio = W + ' / ' + H;
    // 3) 更新比例提示
    if (el.comSubAspectHint) {
      el.comSubAspectHint.textContent = isVertical ? '竖屏 9:16 预览（抖音/视频号）' : '横屏 16:9 预览';
    }
    const w = cv.width, h = cv.height;
    ctx.clearRect(0, 0, w, h);
    // 背景：暗色模拟视频画面
    const grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, '#2a2a30');
    grad.addColorStop(1, '#0e0e10');
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, w, h);
    // 文本：字号按画布宽度比例（成片基准 ≈ 宽度 9%；与 _render_subtitle_png 视觉一致）
    const sizeRatio = el.comSubSize ? Number(el.comSubSize.value) : 1.0;
    const fontPx = Math.max(12, Math.round(w * 0.09 * sizeRatio));
    const borderRatio = el.comSubBorder ? Number(el.comSubBorder.value) : 1.0;
    const borderPx = Math.max(1, Math.round(w * 0.012 * borderRatio));
    const color = (el.comSubColor ? el.comSubColor.value : '#FFFFFF') || '#FFFFFF';
    const posEl = document.querySelector('input[name="comSubPos"]:checked');
    const pos = posEl ? posEl.value : 'bottom';
    const text = '这是字幕预览示例';
    ctx.font = `bold ${fontPx}px -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    const y = (pos === 'center') ? h * 0.5 : h * 0.82;
    // 阴影（贴近 _render_subtitle_png 的效果）
    ctx.shadowColor = 'rgba(0,0,0,0.85)';
    ctx.shadowBlur = borderPx * 2;
    ctx.shadowOffsetX = 0;
    ctx.shadowOffsetY = 1;
    // 描边
    ctx.lineJoin = 'round';
    ctx.lineWidth = borderPx;
    ctx.strokeStyle = '#000';
    ctx.strokeText(text, w / 2, y);
    // 填充
    ctx.shadowBlur = 0;
    ctx.shadowOffsetY = 0;
    ctx.fillStyle = color;
    ctx.fillText(text, w / 2, y);
  }

  // === 成片增强控件事件绑定（BGM / 字幕样式 / 解说长度）===
  if (el.comBgm) {
    el.comBgm.addEventListener('change', () => {
      const v = el.comBgm.value;
      if (el.comBgmVolWrap) el.comBgmVolWrap.hidden = (v === 'off');
      if (el.comBgmFileWrap) el.comBgmFileWrap.hidden = (v !== 'user');
    });
  }
  // 本地音乐选择：走 pywebview 桌面桥 chooseFiles（无桥接回退为聚焦输入框）
  if (el.comBgmFilePick) {
    el.comBgmFilePick.addEventListener('click', async () => {
      try {
        const fn = window.VDL && window.VDL.desktop && window.VDL.desktop.chooseFiles;
        let p = '';
        if (typeof fn === 'function') {
          const arr = await fn();
          p = (Array.isArray(arr) && arr.length) ? arr[0] : '';
        }
        if (p && el.comBgmFile) el.comBgmFile.value = p;
        else if (el.comBgmFile) el.comBgmFile.placeholder = '请把 mp3/wav 路径粘到这里';
      } catch (_) { /* 用户取消或环境不支持，忽略 */ }
    });
  }
  if (el.comBgmVolume) {
    el.comBgmVolume.addEventListener('input', () => {
      if (el.comBgmVolumeVal) el.comBgmVolumeVal.textContent = Math.round(Number(el.comBgmVolume.value) * 100) + '%';
    });
  }
  if (el.comSubSize) {
    el.comSubSize.addEventListener('input', () => {
      if (el.comSubSizeVal) el.comSubSizeVal.textContent = Number(el.comSubSize.value).toFixed(2) + '×';
      comRenderSubtitlePreview();
    });
  }
  if (el.comSubBorder) {
    el.comSubBorder.addEventListener('input', () => {
      if (el.comSubBorderVal) el.comSubBorderVal.textContent = Number(el.comSubBorder.value).toFixed(1) + '×';
      comRenderSubtitlePreview();
    });
  }
  if (el.comSubColor) {
    el.comSubColor.addEventListener('input', comRenderSubtitlePreview);
  }
  document.querySelectorAll('input[name="comSubPos"]').forEach((r) => {
    r.addEventListener('change', comRenderSubtitlePreview);
  });
  // 成片比例切换时，重绘预览（横屏/竖屏框型不同）
  document.querySelectorAll('input[name="comAspect"]').forEach((r) => {
    r.addEventListener('change', comRenderSubtitlePreview);
  });
  if (el.comMaxChars) {
    el.comMaxChars.addEventListener('input', () => {
      if (el.comMaxCharsVal) el.comMaxCharsVal.textContent = (Number(el.comMaxChars.value) === 0) ? '不限' : (Number(el.comMaxChars.value) + '字');
    });
  }
  // 初始化：根据默认值同步显隐与回显
  const _initBgm = el.comBgm ? el.comBgm.value : 'off';
  if (el.comBgmVolWrap) el.comBgmVolWrap.hidden = (_initBgm === 'off');
  if (el.comBgmFileWrap) el.comBgmFileWrap.hidden = (_initBgm !== 'user');
  if (el.comBgmVolume && el.comBgmVolumeVal) el.comBgmVolumeVal.textContent = Math.round(Number(el.comBgmVolume.value) * 100) + '%';
  if (el.comSubSize && el.comSubSizeVal) el.comSubSizeVal.textContent = Number(el.comSubSize.value).toFixed(2) + '×';
  if (el.comSubBorder && el.comSubBorderVal) el.comSubBorderVal.textContent = Number(el.comSubBorder.value).toFixed(1) + '×';
  if (el.comMaxChars && el.comMaxCharsVal) el.comMaxCharsVal.textContent = (Number(el.comMaxChars.value) === 0) ? '不限' : (Number(el.comMaxChars.value) + '字');
  comRenderSubtitlePreview();

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

  // 配音与音量：手动调节滑块 + 保存/重置
  el.comLoudness.addEventListener('input', onLoudnessInput);
  el.comLoudnessOff.addEventListener('change', onLoudnessOff);
  el.comDuck.addEventListener('input', onDuckInput);
  el.comBoost.addEventListener('input', onBoostInput);
  el.comVolSave.addEventListener('click', saveVolumeConfig);
  el.comVolReset.addEventListener('click', onVolReset);

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

  function switchView(view) {
    const isLib = view === 'library';
    const isSub = view === 'subscribe';
    const isTor = view === 'torrent';
    const isCom = view === 'commentary';
    const isUp = view === 'uploadconvert';
    const isDw = view === 'dw';
    const isAppIntro = view === 'appIntro';
    const isNarrato = view === 'narrato';
    const isBridge = view === 'bridge';
    el.downloadView.hidden = isLib || isSub || isTor || isCom || isUp || isDw || isAppIntro || isNarrato || isBridge;
    el.libraryView.hidden = !isLib;
    el.subscribeView.hidden = !isSub;
    el.torrentView.hidden = !isTor;
    el.commentaryView.hidden = !isCom;
    el.uploadConvertView.hidden = !isUp;
    el.dwView.hidden = !isDw;
    if (el.bridgeView) el.bridgeView.hidden = !isBridge;
    if (el.appIntroView) el.appIntroView.hidden = !isAppIntro;
    if (el.narratoView) el.narratoView.hidden = !isNarrato;
    if (el.tabDownload) el.tabDownload.classList.toggle('is-active', !isLib && !isSub && !isTor && !isCom && !isUp && !isDw && !isAppIntro && !isNarrato);
    if (el.tabLibrary) el.tabLibrary.classList.toggle('is-active', isLib);
    if (el.tabSubscribe) el.tabSubscribe.classList.toggle('is-active', isSub);
    if (el.tabTorrent) el.tabTorrent.classList.toggle('is-active', isTor);
    if (el.tabCommentary) el.tabCommentary.classList.toggle('is-active', isCom);
    if (el.tabUploadConvert) el.tabUploadConvert.classList.toggle('is-active', isUp);
    if (el.tabDw) el.tabDw.classList.toggle('is-active', isDw);
    if (el.tabAppIntro) el.tabAppIntro.classList.toggle('is-active', isAppIntro);
    if (el.tabNarrato) el.tabNarrato.classList.toggle('is-active', isNarrato);
    const _isDefault = !isLib && !isSub && !isTor && !isCom && !isUp && !isDw && !isAppIntro && !isNarrato && !isBridge;
    if (el.sTabDownload) el.sTabDownload.classList.toggle('is-active', _isDefault);
    if (el.sTabLibrary) el.sTabLibrary.classList.toggle('is-active', isLib);
    if (el.sTabSubscribe) el.sTabSubscribe.classList.toggle('is-active', isSub);
    if (el.sTabTorrent) el.sTabTorrent.classList.toggle('is-active', isTor);
    if (el.sTabCommentary) el.sTabCommentary.classList.toggle('is-active', isCom);
    if (el.sTabUploadConvert) el.sTabUploadConvert.classList.toggle('is-active', isUp);
    if (el.sTabDw) el.sTabDw.classList.toggle('is-active', isDw);
    if (el.sTabNarrato) el.sTabNarrato.classList.toggle('is-active', isNarrato);
    if (el.sTabBridge) el.sTabBridge.classList.toggle('is-active', isBridge);
    if (isLib) loadLibrary();
    if (isSub) loadSubscriptions();
    if (isCom) loadCommentary();
    if (isUp) { el.ucStatus.textContent = ''; }
    if (isDw) { el.dwImgStatus.textContent = ''; el.dwPdfStatus.textContent = ''; }
    if (isTor) { loadTorrents(); startTorPoll(); }
    else stopTorPoll();
    if (isNarrato) ensureNarrato();
    // 「支持 N 个平台」徽章（#engineBadge）只在下载模块可见，其它功能页隐藏
    if (el.engineBadge) el.engineBadge.hidden = !_isDefault;
  };

  // —— AI 解说体验（NarratoAI 本地子进程 + iframe）——
  let _narratoPolling = false;
  const ensureNarrato = async () => {
    try {
      const st = await request('/api/narrato/status');
      // 无 key 先让用户输入
      if (!st.has_key) {
        if (el.narratoKeyBox) el.narratoKeyBox.hidden = false;
        if (el.narratoLoading) { el.narratoLoading.hidden = false; el.narratoLoading.textContent = '请先粘贴 DeepSeek API Key 并保存，再启动 NarratoAI。'; }
        if (el.narratoFrame) el.narratoFrame.hidden = true;
        return;
      }
      if (el.narratoKeyBox) el.narratoKeyBox.hidden = true;
      // 未就绪则触发启动
      if (st.status !== 'ready') {
        await request('/api/narrato/ensure', { method: 'POST' });
      }
      pollNarratoReady();
    } catch (e) {
      if (el.narratoLoading) { el.narratoLoading.hidden = false; el.narratoLoading.textContent = '无法连接本地服务：' + (e.message || '未知错误'); }
    }
  };

  const pollNarratoReady = () => {
    if (_narratoPolling) return;
    _narratoPolling = true;
    const fmtElapsed = (s) => {
      if (!s) return '';
      if (s < 60) return `${s}秒`;
      return `${Math.floor(s / 60)}分${s % 60}秒`;
    };
    const tick = async () => {
      try {
        const st = await request('/api/narrato/status');
        if (st.status === 'ready') {
          if (el.narratoFrame) {
            el.narratoFrame.src = `http://${'127.0.0.1'}:${st.port}/`;
            el.narratoFrame.hidden = false;
          }
          if (el.narratoLoading) el.narratoLoading.hidden = true;
          if (el.narratoKeyHint) el.narratoKeyHint.textContent = '';
          _narratoPolling = false;
          return;
        }
        if (st.status === 'need_key') {
          if (el.narratoKeyBox) el.narratoKeyBox.hidden = false;
          if (el.narratoLoading) { el.narratoLoading.hidden = false; el.narratoLoading.textContent = '需要 DeepSeek API Key，请粘贴保存后重试。'; }
          _narratoPolling = false;
          return;
        }
        if (st.status === 'missing_dir') {
          if (el.narratoLoading) { el.narratoLoading.hidden = false; el.narratoLoading.textContent = '未找到 NarratoAI 目录：' + st.dir + '（可设 VDL_NARRATOAI_DIR 环境变量）'; }
          _narratoPolling = false;
          return;
        }
        if (st.status === 'need_uv') {
          if (el.narratoLoading) { el.narratoLoading.hidden = false; el.narratoLoading.textContent = st.msg || '需要安装 uv 启动器'; }
          _narratoPolling = false;
          return;
        }
        if (st.status === 'error' || st.status === 'launch_failed') {
          if (el.narratoLoading) {
            let txt = '启动失败：' + (st.last_error || st.msg || '未知错误');
            if (st.exit_code != null) txt += '（exit ' + st.exit_code + '）';
            if (st.log_tail && st.log_tail.length) txt += '\n\n' + st.log_tail.slice(-5).join('\n');
            el.narratoLoading.hidden = false;
            el.narratoLoading.textContent = txt;
          }
          _narratoPolling = false;
          return;
        }
        if (st.status === 'stopped') {
          if (el.narratoLoading) {
            let txt = 'NarratoAI 已停止。' + (st.last_error || '');
            if (st.log_tail && st.log_tail.length) txt += '\n\n' + st.log_tail.slice(-5).join('\n');
            el.narratoLoading.hidden = false;
            el.narratoLoading.textContent = txt;
          }
          _narratoPolling = false;
          return;
        }
        // 仍在启动中：细分 stage
        const elapsed = fmtElapsed(st.elapsed);
        let msg = '正在启动 NarratoAI';
        if (st.stage === 'syncing') msg = '正在安装依赖（uv sync），首次约 1-3 分钟' + (elapsed ? '，已用时 ' + elapsed : '') + '…';
        else if (st.stage === 'starting') msg = '依赖就绪，正在启动 Streamlit 服务' + (elapsed ? '，已用时 ' + elapsed : '') + '…';
        else msg = '正在启动 NarratoAI' + (elapsed ? '，已用时 ' + elapsed : '') + '…';
        if (st.log_tail && st.log_tail.length) msg += '\n\n' + st.log_tail.slice(-3).join('\n');
        if (el.narratoLoading) { el.narratoLoading.hidden = false; el.narratoLoading.textContent = msg; }
        setTimeout(tick, 2000);
      } catch (e) {
        if (el.narratoLoading) { el.narratoLoading.hidden = false; el.narratoLoading.textContent = '轮询失败：' + (e.message || '未知错误'); }
        _narratoPolling = false;
      }
    };
    tick();
  };

  const saveNarratoKey = async () => {
    const key = (el.narratoKeyInput.value || '').trim();
    if (!key) { if (el.narratoKeyHint) el.narratoKeyHint.textContent = '请粘贴 DeepSeek API Key'; return; }
    try {
      const r = await request('/api/narrato/key', { method: 'POST', body: JSON.stringify({ key }) });
      if (!r.ok) {
        if (el.narratoKeyHint) el.narratoKeyHint.textContent = '保存失败：' + (r.msg || '');
        return;
      }
      // 再查一次确认 key 真的落盘（防 TOML 缩进等写入异常）
      const st = await request('/api/narrato/status');
      if (!st.has_key) {
        if (el.narratoKeyHint) el.narratoKeyHint.textContent = '保存异常：key 未写入本地 config.toml，请重试';
        return;
      }
      if (el.narratoKeyHint) el.narratoKeyHint.textContent = '已保存，正在启动 NarratoAI…';
      ensureNarrato();
    } catch (e) {
      if (el.narratoKeyHint) el.narratoKeyHint.textContent = '保存失败：' + (e.message || '');
    }
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
  if (el.tabLibrary) el.tabLibrary.addEventListener('click', () => switchView('library'));
  if (el.tabCommentary) el.tabCommentary.addEventListener('click', () => switchView('commentary'));
  el.tabUploadConvert.addEventListener('click', () => switchView('uploadconvert'));
  if (el.ucBridgeLink) el.ucBridgeLink.addEventListener('click', (e) => { e.preventDefault(); switchView('bridge'); });
  el.tabDw.addEventListener('click', () => switchView('dw'));
  if (el.tabAppIntro) el.tabAppIntro.addEventListener('click', () => switchView('appIntro'));
  if (el.tabSubscribe) el.tabSubscribe.addEventListener('click', () => switchView('subscribe'));
  if (el.tabTorrent) el.tabTorrent.addEventListener('click', () => switchView('torrent'));
  if (el.tabNarrato) el.tabNarrato.addEventListener('click', () => switchView('narrato'));
  if (el.narratoKeySave) el.narratoKeySave.addEventListener('click', saveNarratoKey);

  // 侧栏（桌面端）：10 个 .sidebar-item 也触发同视图切换
  const _sidebarPairs = [
    [el.sTabDownload, 'download'],
    [el.sTabLibrary, 'library'],
    [el.sTabCommentary, 'commentary'],
    [el.sTabUploadConvert, 'uploadconvert'],
    [el.sTabBridge, 'bridge'],
    [el.sTabDw, 'dw'],
    [el.sTabSubscribe, 'subscribe'],
    [el.sTabTorrent, 'torrent'],
    [el.sTabNarrato, 'narrato'],
  ];
  for (const [btn, view] of _sidebarPairs) {
    if (btn) btn.addEventListener('click', () => switchView(view));
  }
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
        // 推理强度 / 避开峰时（省钱旋钮）
        if (el.llmReasoningEffort) el.llmReasoningEffort.value = (cfg.reasoning_effort || 'low');
        if (el.llmOffpeakOnly) el.llmOffpeakOnly.checked = !!cfg.offpeak_only;
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
          reasoning_effort: el.llmReasoningEffort ? el.llmReasoningEffort.value : 'low',
          offpeak_only: el.llmOffpeakOnly ? el.llmOffpeakOnly.checked : false,
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

  // ---- 视觉模型服务商选择器（片头检测 & 视觉理解共用） ----
  (async () => {
    let providers = {};
    let defaultProvider = 'auto';
    let platformStatus = null;  // 本机平台/本地 OCR 状态，用于针对性提示
    try {
      const r = await request('/api/vision/providers');
      if (r.ok) {
        const d = await r.json();
        providers = d.providers || {};
        defaultProvider = d.default || 'auto';
      }
    } catch (e) { /* 未启用时静默退 */ }

    // 拉取本机平台/本地 OCR 状态，渲染针对性提示（如 Apple Silicon 的 Ollama 视觉崩溃警告）
    try {
      const r = await request('/api/vision/status');
      if (r.ok) platformStatus = await r.json();
    } catch (e) { /* 忽略 */ }

    // 根据当前选中的 provider + 本机状态，渲染一段友好提示
    function renderVisionRuntime(provider) {
      const rt = el.visionRuntime;
      if (!rt || !platformStatus) return;
      const st = platformStatus;
      const parts = [];
      if (st.has_local_ocr) {
        parts.push('✅ 本机 Apple Vision OCR 可用：选「自动」即可免 Key、离线识别片头集数/片名卡。');
      } else if (st.platform === 'darwin') {
        parts.push('⚠️ 当前 Mac 未加载本地 OCR（缺少 Quartz），选「自动」将降级到音频检测；建议装 Ollama 或用云端 Key。');
      } else {
        parts.push('ℹ️ 非 Mac 环境无免费本地 OCR：选「自动」会降级到音频检测，建议用 Ollama 或云端 Key。');
      }
      // 仅当选中 Ollama 且本机是 Apple Silicon 时，强调多模态模型崩溃风险
      if (st.ollama_vision_known_issue && provider === 'ollama') {
        parts.push('⚠️ Apple Silicon（M1/M2/M3）上，部分 Ollama 版本运行 qwen2.5-vl 等多模态模型会因 Metal 后端崩溃（GGML_ASSERT）；若选 Ollama 失败，请改用「自动」或云端 provider。');
      }
      rt.textContent = parts.join(' ');
      const warn = (st.ollama_vision_known_issue && provider === 'ollama') || !st.has_local_ocr;
      rt.style.color = warn ? '#e67e22' : '#27ae60';
    }

    // 根据选中的 provider，联动显示其免费额度申请链接（仅云端 provider 有 signup_url）
    function renderVisionSignup(provider) {
      const wrap = el.visionSignupWrap;
      const a = el.visionSignup;
      if (!wrap || !a) return;
      const preset = providers[provider];
      if (preset && preset.signup_url) {
        a.href = preset.signup_url;
        // 取 provider 名中的简短中文/品牌名（去掉括号补充说明）
        const short = (preset.name.split('（')[0].split('(')[0]).trim();
        a.textContent = '申请 ' + short + ' 免费额度 →';
        wrap.hidden = false;
      } else {
        wrap.hidden = true;
      }
    }

    if (el.visionProvider) {
      el.visionProvider.innerHTML = '';
      for (const [k, v] of Object.entries(providers)) {
        const opt = document.createElement('option');
        opt.value = k;
        opt.textContent = v.name;
        el.visionProvider.appendChild(opt);
      }
      // 选中预设后自动填 base_url 和 model，并显示该 provider 的说明
      el.visionProvider.addEventListener('change', () => {
        const sel = el.visionProvider.value;
        const preset = providers[sel];
        if (preset) {
          if (preset.base_url) el.visionBaseUrl.value = preset.base_url;
          if (preset.default_model) el.visionModel.value = preset.default_model;
          if (el.visionNote && preset.note) el.visionNote.textContent = preset.note;
        }
        // Ollama / 自动 模式下 Key 非必需，隐藏占位提示差异
        const needsKey = preset ? !!preset.needs_key : true;
        el.visionApiKey.placeholder = needsKey ? 'sk-...（必填）' : 'sk-...（Ollama/自动模式可留空）';
        // 切换服务商后联动提示（尤其是 Apple Silicon 选 Ollama 的崩溃警示）
        renderVisionRuntime(sel);
        // 联动显示该服务商的免费额度申请链接
        renderVisionSignup(sel);
      });
    }

    // 回填已保存的配置
    try {
      const r = await request('/api/vision/config');
      if (r.ok) {
        const cfg = await r.json();
        if (el.visionProvider) el.visionProvider.value = cfg.provider || defaultProvider;
        if (el.visionApiKey) el.visionApiKey.value = cfg.api_key || '';
        if (el.visionBaseUrl) el.visionBaseUrl.value = cfg.base_url || '';
        if (el.visionModel) el.visionModel.value = cfg.model || '';
        const preset = providers[cfg.provider || defaultProvider];
        if (el.visionNote && preset && preset.note) el.visionNote.textContent = preset.note;
        // 用已保存的 provider 渲染提示
        renderVisionRuntime(cfg.provider || defaultProvider);
        // 联动显示已保存服务商的免费额度申请链接
        renderVisionSignup(cfg.provider || defaultProvider);
      }
    } catch (e) { /* */ }

    // 保存按钮
    if (el.visionSave) {
      el.visionSave.addEventListener('click', async () => {
        const body = {
          provider: el.visionProvider ? el.visionProvider.value : 'auto',
          api_key: el.visionApiKey ? el.visionApiKey.value.trim() : '',
          base_url: el.visionBaseUrl ? el.visionBaseUrl.value.trim() : '',
          model: el.visionModel ? el.visionModel.value.trim() : '',
        };
        try {
          const r = await request('/api/vision/config', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          });
          const d = await r.json();
          if (r.ok && d.ok) {
            if (el.visionStatus) {
              el.visionStatus.hidden = false;
              el.visionStatus.textContent = '✅ 已保存';
              setTimeout(() => { el.visionStatus.hidden = true; }, 2000);
            }
          } else {
            if (el.visionStatus) {
              el.visionStatus.hidden = false;
              el.visionStatus.style.color = '#e74c3c';
              el.visionStatus.textContent = '❌ 保存失败';
            }
          }
        } catch (e) {
          if (el.visionStatus) {
            el.visionStatus.hidden = false;
            el.visionStatus.style.color = '#e74c3c';
            el.visionStatus.textContent = '❌ 网络错误';
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
    .then(({ region, peer, china_domains: domains, commentary_enabled, ads_enabled, convert, download, cloud, library, subscriptions, retention, archive, crypto, torrent, ai_dewatermark, narrato, authRequired, profile }) => {
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
        ? convert.targets : ['mp4','mov','mkv','webm','mp3','m4a','wav','flac','gif'];
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
      node.aiDewatermarkEnabled = !!(ai_dewatermark && ai_dewatermark.enabled);
      node.aiDewatermarkGpu = !!(ai_dewatermark && ai_dewatermark.gpu);
      node.narratoEnabled = !!(narrato && narrato.enabled);
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
      // —— Route B：按 profile 双态控制 App 专属 tab ——
      // web 精简版（profile=web）：固定只保留四大入口（下载/视频处理/去水印/更多功能介绍）
      // app 桌面版（profile=app）：9 tab 全量，按能力精细控制显隐
      if (profile === 'web') {
        [
          'tabLibrary', 'tabCommentary', 'tabSubscribe', 'tabTorrent',
          'sTabLibrary', 'sTabCommentary', 'sTabSubscribe', 'sTabTorrent',
          // 始终保留：下载 / 格式转换 / 去水印（web 端核心 3 件 + 默认下载）
        ].forEach(id => { const t = document.getElementById(id); if (t) t.hidden = true; });
        if (el.tabDownload) el.tabDownload.hidden = false;
        if (el.tabUploadConvert) el.tabUploadConvert.hidden = false;
        if (el.tabDw) el.tabDw.hidden = false;
        if (el.tabAppIntro) el.tabAppIntro.hidden = false;
        // 侧栏：用外层 group 隐藏实现更清晰——未启用能力的 group 整组隐藏
        // 这里简单点：保留 sTabDownload/uploadconvert/dw 显示（默认 group 中第一项即可让侧栏可点）
        if (el.sTabDownload) el.sTabDownload.hidden = false;
        if (el.sTabUploadConvert) el.sTabUploadConvert.hidden = false;
        if (el.sTabDw) el.sTabDw.hidden = false;
      } else {
        // App 端：能力精细控制（tabTorrent/tabCommentary/tabDw/tabLibrary/tabSubscribe）
        if (el.tabTorrent) el.tabTorrent.hidden = !node.torrentEnabled;
        if (el.tabCommentary) el.tabCommentary.hidden = !node.commentaryEnabled;
        if (el.tabUploadConvert) el.tabUploadConvert.hidden = false; // 本地核心能力
        if (el.tabDw) el.tabDw.hidden = !node.aiDewatermarkEnabled;  // 依赖 AI 去水印能力
        if (el.tabLibrary) el.tabLibrary.hidden = !node.libraryEnabled;
        if (el.tabSubscribe) el.tabSubscribe.hidden = !node.subscriptionsEnabled;
        if (el.tabAppIntro) el.tabAppIntro.hidden = false; // 更多功能介绍桌面端也显示
        // 侧栏同 toggle（保持一一对应；侧栏无 tabAppIntro 对应——因为更多功能是顶 tab 独有）
        // 侧栏同 toggle（保持一一对应）
        if (el.sTabTorrent) el.sTabTorrent.hidden = !node.torrentEnabled;
        if (el.sTabCommentary) el.sTabCommentary.hidden = !node.commentaryEnabled;
        if (el.sTabUploadConvert) el.sTabUploadConvert.hidden = false;
        if (el.sTabDw) el.sTabDw.hidden = !node.aiDewatermarkEnabled;
        if (el.sTabLibrary) el.sTabLibrary.hidden = !node.libraryEnabled;
        if (el.sTabSubscribe) el.sTabSubscribe.hidden = !node.subscriptionsEnabled;
      }
      // AI 解说体验：仅桌面端（node.narratoEnabled）显示，网页版一律隐藏
      if (el.tabNarrato) el.tabNarrato.hidden = !node.narratoEnabled;
      if (el.sTabNarrato) el.sTabNarrato.hidden = !node.narratoEnabled;
      el.tabs.hidden = false; // 导航栏始终显示
      // 默认视图：始终停在下载
      switchView('download');
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
