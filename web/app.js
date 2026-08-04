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
  };

  /** 当前解析结果：{ url, platform, video, qualities } */
  let resolved = null;
  let selectedQuality = 'best';
  let allPlatforms = [];
  const trackers = new Map();

  // ------------------------------------------------------------------ 工具

  const request = async (url, options = {}) => {
    const response = await fetch(url, {
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

    renderQualities(data.qualities);
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
    };
    refs.cancel.addEventListener('click', () => cancelTask(taskId));
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
    refs.save.href = `/api/tasks/${task.task_id}/file`;
    refs.save.setAttribute('download', task.filename || '');
    if (autoSave) refs.save.click();
  };

  /** 用 SSE 跟踪进度，浏览器不支持或连接断开时回退到轮询。 */
  const trackTask = (taskId, refs) => {
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
        handle(await request(`/api/tasks/${taskId}`));
      } catch {
        /* 静默重试，SSE 或下一轮轮询会补上 */
      }
    }, POLL_FALLBACK_MS);

    const source = new EventSource(`/api/tasks/${taskId}/events`);
    source.onmessage = (event) => handle(JSON.parse(event.data));
    source.onerror = () => source.close();

    trackers.set(taskId, { source, timer: poll });
  };

  // ------------------------------------------------------------------ 动作

  const handleResolve = async (event) => {
    event.preventDefault();
    const url = el.input.value.trim();
    if (!url) {
      showError('请输入视频链接', '把视频页面的地址粘贴到输入框即可');
      return;
    }
    clearError();
    setLoading(true);
    el.resultPanel.hidden = true;
    try {
      resolved = await request('/api/resolve', { method: 'POST', body: JSON.stringify({ url }) });
      renderVideo(resolved);
    } catch (error) {
      resolved = null;
      showError(error.message || '解析失败', error.hint);
    } finally {
      setLoading(false);
    }
  };

  const handleDownload = async () => {
    if (!resolved) return;
    clearError();
    el.downloadBtn.disabled = true;
    try {
      const { task_id: taskId } = await request('/api/download', {
        method: 'POST',
        body: JSON.stringify({ url: resolved.url, quality: selectedQuality }),
      });
      const refs = createTaskCard(taskId, {
        title: resolved.video.title,
        platform: resolved.platform.name,
      });
      trackTask(taskId, refs);
    } catch (error) {
      showError(error.message || '创建下载任务失败', error.hint);
    } finally {
      el.downloadBtn.disabled = false;
    }
  };

  const cancelTask = async (taskId) => {
    try {
      await request(`/api/tasks/${taskId}`, { method: 'DELETE' });
    } catch (error) {
      showError(error.message || '取消失败', error.hint);
    }
  };

  // ------------------------------------------------------------------ 初始化

  const toggleClearButton = () => { el.clearBtn.hidden = el.input.value.length === 0; };

  el.form.addEventListener('submit', handleResolve);
  el.downloadBtn.addEventListener('click', handleDownload);
  el.input.addEventListener('input', toggleClearButton);
  el.clearBtn.addEventListener('click', () => {
    el.input.value = '';
    toggleClearButton();
    clearError();
    el.resultPanel.hidden = true;
    el.input.focus();
  });
  el.modalClose.addEventListener('click', () => el.modal.close());
  el.modal.addEventListener('click', (event) => {
    if (event.target === el.modal) el.modal.close();
  });
  el.badge.addEventListener('click', () => openPlatformModal(allPlatforms));

  request('/api/platforms')
    .then(({ platforms }) => renderPlatforms(platforms))
    .catch(() => { /* 平台清单获取失败不影响主流程 */ });
})();
