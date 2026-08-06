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
  };

  /** 当前解析结果：{ url, platform, video, qualities, base } */
  let resolved = null;
  let selectedQuality = 'best';
  let allPlatforms = [];
  const trackers = new Map();

  // -------------------------------------------------------------- 节点分流
  // 双节点部署时，国内站请求发往国内节点、海外站发往海外节点，各自直连目标站，
  // 免去跨境回源。单节点部署（peer 为空）时全部走本节点，行为与以前一致。

  const node = { region: 'global', peer: '', chinaDomains: [], commentaryEnabled: false, adsEnabled: false };
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

  const request = async (path, options = {}, base = '') => {
    const response = await fetch(base + path, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw { message: payload.error || payload.detail || '请求失败，请稍后重试', hint: payload.hint || '' };
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
    };
    refs.cancel.addEventListener('click', () => cancelTask(taskId, refs.base || ''));
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

    // 本节点启用了解说增值功能：下载完成后展示「生成解说成片」入口
    if (node.commentaryEnabled) {
      refs.commentary.hidden = false;
      if (!refs.commentary.dataset.bound) {
        refs.commentary.dataset.bound = '1';
        refs.commentary.addEventListener('click', () => createCommentary(taskId, refs, refs.base || ''));
      }
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
      const { task_id: taskId } = await request('/api/download', {
        method: 'POST',
        body: JSON.stringify({
          url: resolved.url,
          quality,
          cookie: resolved.cookie || '',
          proxy: resolved.proxy || '',
        }),
      }, base);
      const refs = createTaskCard(taskId, {
        title: resolved.video.title,
        platform: resolved.platform.name,
      });
      refs.base = base;
      trackTask(taskId, refs, base);
      return taskId;
    } catch (error) {
      showError(error.message || '创建下载任务失败', error.hint);
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
  el.upsellCloud.addEventListener('click', () => showUpsellPlaceholder('存网盘'));
  el.modalClose.addEventListener('click', () => el.modal.close());
  el.modal.addEventListener('click', (event) => {
    if (event.target === el.modal) el.modal.close();
  });
  el.badge.addEventListener('click', () => openPlatformModal(allPlatforms));

  request('/api/platforms')
    .then(({ platforms }) => renderPlatforms(platforms))
    .catch(() => { /* 平台清单获取失败不影响主流程 */ });

  request('/api/nodes')
    .then(({ region, peer, china_domains: domains, commentary_enabled, ads_enabled }) => {
      node.region = region || 'global';
      node.peer = peer || '';
      node.chinaDomains = domains || [];
      node.commentaryEnabled = !!commentary_enabled;
      node.adsEnabled = !!ads_enabled;
      el.adsSlot.hidden = !node.adsEnabled;
      paintNodeBar();
    })
    .catch(() => { /* 取不到节点信息就退回单节点，全部走本机 */ });
})();
