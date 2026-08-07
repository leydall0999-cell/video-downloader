/* 视频下载站 · 前端逻辑（无依赖）
 * 约定：所有动态文本一律使用 textContent 写入，杜绝 innerHTML 注入风险。 */
(() => {
  'use strict';

  const STATUS_TEXT = {
    pending: '排队中',
    downloading: '下载中',
    merging: '合并中',
    completed: '已完成',
    failed: '失败',
    canceled: '已取消',
  };
  const ACTIVE_STATES = ['pending', 'downloading', 'merging'];
  const POLL_FALLBACK_MS = 1500;

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
    proxyInput: $('proxyInput'),
    upsellBox: $('upsellBox'),
    qualityBlock: $('qualityBlock'),
    directHint: $('directHint'),
    serverFallbackBtn: $('serverFallbackBtn'),
    upsellMp3: $('upsellMp3'),
    upsellCloud: $('upsellCloud'),
    upsellStatus: $('upsellStatus'),
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
    batchInput: $('batchInput'),
    batchQuality: $('batchQuality'),
    batchConcurrency: $('batchConcurrency'),
    batchConcVal: $('batchConcVal'),
    batchBtn: $('batchBtn'),
    queueBar: $('queueBar'),
    cancelAllBtn: $('cancelAllBtn'),
    // 媒体库（桌面版功能）
    tabs: $('tabs'),
    tabDownload: $('tabDownload'),
    tabLibrary: $('tabLibrary'),
    downloadView: $('downloadView'),
    libraryView: $('libraryView'),
    libRefresh: $('libRefresh'),
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
  };

  /** 当前解析结果：{ url, platform, video, qualities, base } */
  let resolved = null;
  let selectedQuality = 'best';
  let allPlatforms = [];
  const trackers = new Map();

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

  const request = async (path, options = {}, base = '') => {
    const headers = { 'Content-Type': 'application/json' };
    const subKey = localStorage.getItem('vdl_sub_key');
    if (subKey) headers['X-Subscription-Key'] = subKey;
    const response = await fetch(base + path, { headers, ...options });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const err = { message: payload.error || payload.detail || '请求失败，请稍后重试', hint: payload.hint || '' };
      if (response.status === 402) err.subscribe = true;   // 免费额度耗尽，引导订阅
      throw err;
    }
    return payload;
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

  const formatDuration = (seconds) => {
    if (!seconds || seconds <= 0) return '';
    const total = Math.round(seconds);
    const parts = [Math.floor(total / 3600), Math.floor((total % 3600) / 60), total % 60];
    const trimmed = parts[0] ? parts : parts.slice(1);
    return trimmed.map((n, i) => (i === 0 ? String(n) : String(n).padStart(2, '0'))).join(':');
  };

  const formatEta = (seconds) => (seconds > 0 ? `剩余 ${formatDuration(seconds) || '<1s'}` : '');

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
    if (task.status === 'merging') return '正在合并音视频…';
    if (!task.total_bytes) return '正在建立连接…';
    const speed = task.speed > 0 ? `${formatBytes(task.speed)}/s` : '';
    return [`${formatBytes(task.downloaded_bytes)} / ${formatBytes(task.total_bytes)}`, speed, formatEta(task.eta)]
      .filter(Boolean)
      .join(' · ');
  };

  // ------------------------------------------------------------------ 提示

  const showError = (message, hint = '') => {
    el.alertTitle.textContent = message;
    el.alertHint.textContent = hint;
    el.alertHint.hidden = !hint;
    el.alert.hidden = false;
  };

  const clearError = () => { el.alert.hidden = true; };

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
      el.upsellBox.hidden = true;
      el.downloadBtn.lastChild.textContent = '直接保存到本机 ⬇';
      el.directHint.hidden = false;
      el.directHint.textContent = '✅ 检测到这是可直接下载的文件，已为你跳过服务器处理。点上方按钮即从源站保存到你的电脑，不经过我们的服务器。';
      el.serverFallbackBtn.hidden = false;
    } else {
      el.qualityBlock.hidden = false;
      el.upsellBox.hidden = false;
      el.downloadBtn.lastChild.textContent = '开始下载';
      el.directHint.hidden = true;
      el.directHint.textContent = '';
      el.serverFallbackBtn.hidden = true;
      renderQualities(data.qualities);
    }
    // 展示交叉引流卡片：转 MP3 已接通可直接用，存网盘待后续打通
    el.upsellStatus.hidden = true;
    el.upsellMp3.disabled = false;
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
      save: node.querySelector('[data-save]'),
      error: node.querySelector('[data-error]'),
      commentary: node.querySelector('[data-commentary]'),
      commentaryFile: node.querySelector('[data-commentary-file]'),
      commentaryStatus: node.querySelector('[data-commentary-status]'),
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
    };
    refs.cancel.addEventListener('click', () => cancelTask(taskId, refs.base || ''));
    refs.retry.addEventListener('click', () => retryTask(taskId, refs));
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
    refs.bar.style.width = `${task.progress}%`;
    refs.stats.textContent = buildStats(task);
    refs.cancel.hidden = !active;
    refs.root.classList.toggle('is-active', active);
    refs.root.classList.toggle('is-done', task.status === 'completed');
    refs.root.classList.toggle('is-error', task.status === 'failed' || task.status === 'canceled');

    const failed = task.status === 'failed';
    refs.error.hidden = !failed;
    refs.error.textContent = failed ? [task.error, task.hint].filter(Boolean).join(' — ') : '';
    // 失败 / 已取消的任务展示「重试」按钮
    refs.retry.hidden = !(task.status === 'failed' || task.status === 'canceled');

    if (task.status !== 'completed') return;
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

    // 本节点启用了解说增值功能：下载完成后展示「生成解说成片」入口
    if (node.commentaryEnabled) {
      refs.commentary.hidden = false;
      if (!refs.commentary.dataset.bound) {
        refs.commentary.dataset.bound = '1';
        refs.commentary.addEventListener('click', () => createCommentary(task.task_id, refs, refs.base || ''));
      }
    }

    // 下载完成后展示「存到网盘」入口（增值能力）：把文件上传到用户自己的网盘
    refs.cloud.hidden = false;
    if (!refs.cloud.dataset.bound) {
      refs.cloud.dataset.bound = '1';
      refs.cloud.addEventListener('click', () => openCloudModal(task.task_id, refs));
    }
    lastCompletedTask = task.task_id;
    lastCompletedRefs = refs;
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
      el.alertBox.hidden = false;
      el.alertTitle.textContent = `已提交 ${data.count} 个下载任务`
        + (data.skipped ? `（${data.skipped} 个链接无法识别已跳过）` : '');
      el.alertHint.textContent = '在下方「下载任务」列表查看进度；可调整「同时下载」数量控制并发。';
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
    } catch (error) {
      resolved = null;
      showError(error.message || '解析失败', error.hint);
    } finally {
      setLoading(false);
    }
  };

  /** 用指定清晰度发起一个下载任务；返回 taskId 或 null。被"开始下载"与"转 MP3"复用。 */
  const startDownload = async (quality) => {
    if (!resolved) return null;
    clearError();
    const base = resolved.base || '';
    try {
      const data = await request('/api/download', {
        method: 'POST',
        body: JSON.stringify({
          url: resolved.url,
          quality,
          cookie: resolved.cookie || '',
          proxy: resolved.proxy || '',
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
  // 下载完成的任务 → 点「生成解说成片」→ 后台调 commentary-pipeline/process.py → 回传成片。
  // 解说算力由独立 worker 承担，UI 只负责触发与轮询，不感知具体渲染过程。

  const createCommentary = async (taskId, refs, base = '') => {
    refs.commentary.disabled = true;
    refs.commentary.textContent = '生成中…';
    refs.commentaryStatus.hidden = false;
    refs.commentaryStatus.textContent = '正在生成解说成片，长视频可能需数分钟…';
    try {
      const { job_id } = await request('/api/commentary', {
        method: 'POST',
        body: JSON.stringify({ task_id: taskId, vertical: true }),
      }, base);

      const poll = setInterval(async () => {
        try {
          const st = await request(`/api/commentary/${job_id}`, {}, base);
          if (st.status === 'completed') {
            clearInterval(poll);
            refs.commentaryStatus.textContent = '解说成片已生成';
            refs.commentaryFile.href = `${base}/api/commentary/${job_id}/file`;
            refs.commentaryFile.setAttribute('download', '解说成片.mp4');
            refs.commentaryFile.hidden = false;
            refs.commentary.hidden = true;
          } else if (st.status === 'failed') {
            clearInterval(poll);
            refs.commentaryStatus.textContent = `生成失败：${st.error || '未知错误'}`;
            refs.commentary.disabled = false;
            refs.commentary.textContent = '重试生成解说';
          }
        } catch {
          /* 静默重试，下一轮轮询补上 */
        }
      }, 2500);
    } catch (err) {
      refs.commentaryStatus.hidden = false;
      refs.commentaryStatus.textContent = `无法开始：${err.message || '请稍后重试'}`;
      refs.commentary.disabled = false;
      refs.commentary.textContent = '生成解说成片';
    }
  };

  // ------------------------------------------------------------------ 初始化

  const toggleClearButton = () => { el.clearBtn.hidden = el.input.value.length === 0; };

  el.form.addEventListener('submit', handleResolve);
  el.batchBtn.addEventListener('click', () => {
    const urls = el.batchInput.value.split(/\s+/).map((s) => s.trim()).filter(Boolean);
    if (!urls.length) { showError('请先粘贴至少一个视频链接'); return; }
    runBatch(urls, el.cookieInput.value.trim(), el.proxyInput.value.trim());
  });
  el.batchConcurrency.addEventListener('input', () => { el.batchConcVal.textContent = el.batchConcurrency.value; });
  el.cancelAllBtn.addEventListener('click', cancelAll);
  el.downloadBtn.addEventListener('click', handleDownload);
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

  // 交叉引流入口：
  //  - 转 MP3 已接通：直接发起一个"仅音频"下载任务（复用下载框架），进度在下方任务列表展示
  //  - 存网盘 尚未接通，暂时保留占位提示
  const showUpsellPlaceholder = (label) => {
    el.upsellStatus.hidden = false;
    el.upsellStatus.textContent = `🚧 ${label}功能即将上线，敬请期待`;
  };
  el.upsellMp3.addEventListener('click', async () => {
    if (!resolved) return;
    const audioKey = resolved.qualities?.find((q) => /MP3|音频/.test(q.label))?.key || 'audio';
    const taskId = await startDownload(audioKey);
    if (taskId) {
      el.upsellStatus.hidden = false;
      el.upsellStatus.textContent = '已为你发起 MP3 音频提取任务，进度见下方「下载任务」列表 👇';
      el.upsellMp3.disabled = true;
    }
  });
  el.upsellCloud.addEventListener('click', () => {
    if (lastCompletedTask && lastCompletedRefs) {
      openCloudModal(lastCompletedTask, lastCompletedRefs);
      return;
    }
    el.upsellStatus.hidden = false;
    el.upsellStatus.textContent = '先等一个下载任务完成，再点它卡片上的「☁️ 存到网盘」即可。';
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

  // 云盘弹窗事件绑定
  el.cloudModalClose.addEventListener('click', () => el.cloudModal.close());
  el.cloudModal.addEventListener('click', (e) => { if (e.target === el.cloudModal) el.cloudModal.close(); });
  el.cloudSave.addEventListener('click', startCloudSave);
  el.cloudModal.querySelectorAll('input[name=cloudProvider]').forEach((r) => r.addEventListener('change', syncCloudForm));
  el.cloudBaiduBtn.addEventListener('click', () => {
    if (!node.baiduAuthUrl) { el.cloudBaiduStatus.textContent = '该实例未启用百度网盘'; return; }
    const w = window.open(node.baiduAuthUrl, 'baidu', 'width=600,height=720');
    if (!w) el.cloudBaiduStatus.textContent = '弹窗被拦截，请允许弹出窗口后重试';
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

  const switchView = (view) => {
    const isLib = view === 'library';
    const isSub = view === 'subscribe';
    el.downloadView.hidden = isLib || isSub;
    el.libraryView.hidden = !isLib;
    el.subscribeView.hidden = !isSub;
    el.tabDownload.classList.toggle('is-active', !isLib && !isSub);
    el.tabLibrary.classList.toggle('is-active', isLib);
    el.tabSubscribe.classList.toggle('is-active', isSub);
    if (isLib) loadLibrary();
    if (isSub) loadSubscriptions();
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
    card.className = 'lib-card';
    card.setAttribute('aria-label', item.title || item.name);

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
    fallback.textContent = item.kind === 'video' ? '🎬' : '🎵';
    thumbBox.appendChild(fallback);
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

    card.append(thumbBox, metaBox);
    card.addEventListener('click', () => openLibModal(item));
    return card;
  };

  const openLibModal = (item) => {
    currentLibItem = item;
    el.libPlayer.replaceChildren();
    if (item.kind === 'video') {
      const v = document.createElement('video');
      v.src = libFileUrl(item.id);
      v.controls = true;
      v.preload = 'metadata';
      v.className = 'lib-video';
      el.libPlayer.appendChild(v);
    } else {
      const a = document.createElement('audio');
      a.src = libFileUrl(item.id);
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
    el.libDownload.href = libFileUrl(item.id);
    el.libDownload.setAttribute('download', item.name || 'video');
    if (typeof el.libModal.showModal === 'function') el.libModal.showModal();
    else el.libModal.setAttribute('open', '');
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

  el.tabDownload.addEventListener('click', () => switchView('download'));
  el.tabLibrary.addEventListener('click', () => switchView('library'));
  el.tabSubscribe.addEventListener('click', () => switchView('subscribe'));
  el.subAddBtn.addEventListener('click', addSubscription);
  el.libRefresh.addEventListener('click', loadLibrary);
  el.libSearch.addEventListener('input', debounce(loadLibrary, 300));
  el.libPlatform.addEventListener('change', loadLibrary);
  el.libKind.addEventListener('change', loadLibrary);
  el.libModalClose.addEventListener('click', () => el.libModal.close());
  el.libModal.addEventListener('click', (e) => { if (e.target === el.libModal) el.libModal.close(); });
  el.libDelete.addEventListener('click', deleteLibItem);

  setInterval(loadQueue, 2500);  // 队列概览：持续轮询任务统计

  request('/api/platforms')
    .then(({ platforms }) => renderPlatforms(platforms))
    .catch(() => { /* 平台清单获取失败不影响主流程 */ });

  request('/api/nodes')
    .then(({ region, peer, china_domains: domains, commentary_enabled, ads_enabled, convert, download, cloud, library, subscriptions }) => {
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
      if (node.libraryEnabled || node.subscriptionsEnabled) el.tabs.hidden = false;
      initSubUI();
      paintNodeBar();
    })
    .catch(() => { /* 取不到节点信息就退回单节点，全部走本机 */ });
})();
