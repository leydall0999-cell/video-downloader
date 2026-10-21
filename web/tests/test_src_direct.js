/**
 * 行为测试：源站直链保存（2026-10-21 零成本方案）。
 * 覆盖两块真实代码：
 *   1. srcDirectKind —— 平台白名单判断（域名/后缀/HLS），纯函数全分支；
 *   2. concurrentSaveFromSource —— 防盗链失效（403）/非 206/网络错时必须安静
 *      返回 UNSUPPORTED（调用方据此回退既有链路，用户无感）。
 * 依据是 2026-10-21 各平台真实直链实测矩阵（curl + 浏览器 fetch 双端）：
 *   快手 kwaicdn.com / 小红书 xhscdn.com = fetch 可直连；
 *   抖音 douyinvod.com 混合轨            = fetch 可直连（无 Referer 放行）；
 *   抖音 media-video/media-audio 分离单轨 = 不可直连（会存成无声/无画残废文件）；
 *   微博 weibocdn.com / video.weibo.com   = nav（新标签导航）；
 *   B站 upos                             = 防盗链 + DASH，不可用。
 */
const fs = require('fs');
const http = require('http');
const path = require('path');

const APP_JS = path.join(__dirname, '..', 'app.js');
const SRC = fs.readFileSync(APP_JS, 'utf8');

let pass = 0, fail = 0;
function ok(name, cond, extra) {
  if (cond) { pass++; console.log(`  ✓ ${name}`); }
  else { fail++; console.log(`  ✗ ${name}${extra ? '  -> ' + extra : ''}`); }
}

/** 从源码里按大括号配平截取一个函数（含 async 前缀）。 */
function extractFn(name) {
  const start = SRC.indexOf(`async function ${name}(`);
  if (start < 0) throw new Error(`找不到函数 ${name}`);
  let i = SRC.indexOf('{', SRC.indexOf(')', start));
  let depth = 0;
  for (let j = i; j < SRC.length; j++) {
    const ch = SRC[j];
    if (ch === '{') depth++;
    else if (ch === '}') {
      depth--;
      if (depth === 0) return SRC.slice(start, j + 1);
    }
  }
  throw new Error(`函数 ${name} 括号不配平`);
}

/** 截取普通 function（srcDirectKind / navSaveFromSource / triggerBlobDownload）。 */
function extractPlainFn(name) {
  const start = SRC.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`找不到函数 ${name}`);
  let i = SRC.indexOf('{', SRC.indexOf(')', start));
  let depth = 0;
  for (let j = i; j < SRC.length; j++) {
    const ch = SRC[j];
    if (ch === '{') depth++;
    else if (ch === '}') {
      depth--;
      if (depth === 0) return SRC.slice(start, j + 1);
    }
  }
  throw new Error(`函数 ${name} 括号不配平`);
}

const kindSrc = extractPlainFn('srcDirectKind');
const saveSrc = extractFn('concurrentSaveFromSource');
const blobSrc = extractPlainFn('triggerBlobDownload');

// 常量从源码取真值，防止测试与实现漂移
function extractConst(name) {
  const m = new RegExp(`const ${name}\\s*=\\s*(\\[[^\\]]*\\]|"[^"]*"|[0-9\\s*+]+);`).exec(SRC);
  if (!m) throw new Error(`找不到常量 ${name}`);
  // eslint-disable-next-line no-new-func
  return new Function(`return (${m[1]});`)();
}

console.log('=== 0. 源码抽取 ===');
ok('srcDirectKind 已抽取', kindSrc.length > 100);
ok('concurrentSaveFromSource 已抽取', saveSrc.length > 500);
ok('triggerBlobDownload 已抽取', blobSrc.length > 100);
console.log('    SRC_DIRECT_FETCH_HOSTS =', JSON.stringify(extractConst('SRC_DIRECT_FETCH_HOSTS')));
console.log('    SRC_DIRECT_NAV_HOSTS   =', JSON.stringify(extractConst('SRC_DIRECT_NAV_HOSTS')));

// —— 第 1 组：srcDirectKind 纯函数全分支 ——
const constSrc = ['SRC_DIRECT_FETCH_HOSTS', 'SRC_DIRECT_NAV_HOSTS', 'SRC_DIRECT_EXTS']
  .map((c) => `const ${c} = ${JSON.stringify(extractConst(c))};`).join('\n');
// eslint-disable-next-line no-new-func
const srcDirectKind = new Function(`${constSrc}\n${kindSrc}\nreturn srcDirectKind;`)();

console.log('=== 1. srcDirectKind 平台判定 ===');
ok('快手短链 host=v23-3.kwaicdn.com → fetch',
  srcDirectKind('https://v23-3.kwaicdn.com/upic/2026/07/30/21/BMjAyNjA3.mp4?pkey=AAXe55_x', false) === 'fetch');
ok('快手 http 也认 → fetch',
  srcDirectKind('http://v2.kwaicdn.com/a/b.mp4', false) === 'fetch');
ok('小红书 sns-video-v3.xhscdn.com → fetch',
  srcDirectKind('http://sns-video-v3.xhscdn.com/stream/1/110/268/01ead.mp4?sign=7ed8&t=6adc', false) === 'fetch');
ok('微博 f.video.weibocdn.com → nav',
  srcDirectKind('https://f.video.weibocdn.com/o0/evo9.mp4?label=mp4_1080p', false) === 'nav');
ok('微博 video.weibo.com（带 mp4 路径）→ nav',
  srcDirectKind('https://video.weibo.com/media/play/abc.mp4?vid=123', false) === 'nav');
ok('抖音混合轨（v26-web，路径无 .mp4 后缀）→ fetch',
  srcDirectKind('https://v26-web.douyinvod.com/a837/video/tos/cn/x/?a=6383&ch=26', false) === 'fetch');
ok('抖音混合轨（无后缀且无 query）→ fetch',
  srcDirectKind('https://v26-web.douyinvod.com/a837/video/tos/cn/x/', false) === 'fetch');
ok('抖音分离视频轨 media-video-avc1 单独 → 空（单轨无声，禁止直连）',
  srcDirectKind('https://v95-web-sz.douyinvod.com/9447fe5/6ad98/video/tos/cn/tos-cn-vd-0026/x/media-video-avc1/?a=2558&mime_type=video_mp4', false) === '');
ok('抖音分离音频轨 media-audio-und-mp4a → 空（单轨无画，禁止直连）',
  srcDirectKind('https://v95-web-sz.douyinvod.com/da5a4b3/6ad98/video/tos/cn/tos-cn-ve-15/x/media-audio-und-mp4a/?a=2558', false) === '');

// —— 抖音「双轨合并」新分支（2026-10-21）：视频轨 + 配套音频轨 ⇒ tracks ——
const DY_V = 'https://v95-web-sz.douyinvod.com/9447fe5/6ad98/video/tos/cn/tos-cn-vd-0026/x/media-video-avc1/?a=2558';
const DY_A = 'https://v95-web-sz.douyinvod.com/da5a4b3/6ad98/video/tos/cn/tos-cn-ve-15/x/media-audio-und-mp4a/?a=2558';
ok('抖音视频轨 + 配套音频轨 → tracks（浏览器内合并）',
  srcDirectKind(DY_V, false, DY_A) === 'tracks');
ok('抖音视频轨 + 缺音频轨 → 空（回退节点合并，绝不存无声文件）',
  srcDirectKind(DY_V, false, '') === '' && srcDirectKind(DY_V, false) === '');
ok('抖音视频轨 + 音频 URL 实为另一条视频轨 → 空（防配错轨）',
  srcDirectKind(DY_V, false, DY_V) === '');
ok('抖音音频轨当 play_url（即使给了音频轨）→ 空',
  srcDirectKind(DY_A, false, DY_A) === '');
ok('抖音双轨 + is_hls=true → 空',
  srcDirectKind(DY_V, true, DY_A) === '');
ok('抖音双轨 + 音频 URL 非 http → 空',
  srcDirectKind(DY_V, false, 'javascript:alert(1)') === '');
ok('快手带音频参数不影响原判定（非分离轨站点）',
  srcDirectKind('https://v23-3.kwaicdn.com/a/b.mp4', false, DY_A) === 'fetch');
ok('抖音相似域名不误放行（douyinvod.com.attacker.com）',
  srcDirectKind('https://douyinvod.com.attacker.com/a/b', false) === '');
ok('抖音 HLS（is_hls=true）→ 空',
  srcDirectKind('https://v26-web.douyinvod.com/a/b.m3u8', true) === '');
ok('B站 upos-sz-mirrorcos.bilivideo.com → 空（防盗链+DASH）',
  srcDirectKind('https://upos-sz-mirrorcos.bilivideo.com/upgcxcode/12/34/5678.m4s?sign=x', false) === '');
ok('HLS（is_hls=true）一律空，即使域名在白名单',
  srcDirectKind('https://v23-3.kwaicdn.com/a/b.m3u8', true) === '');
ok('后缀不在白名单（zip/png）→ 空',
  srcDirectKind('https://v23-3.kwaicdn.com/a/b.zip', false) === '' &&
  srcDirectKind('https://sns-video-v3.xhscdn.com/a/b.png', false) === '');
ok('空/非 http → 空',
  srcDirectKind('', false) === '' && srcDirectKind('ftp://x/a.mp4', false) === '' &&
  srcDirectKind('javascript:alert(1)', false) === '');
ok('相似域名不误放行（evil-kwaicdn.com.attacker.com）',
  srcDirectKind('https://kwaicdn.com.attacker.com/a.mp4', false) === '');

// —— 第 2 组：concurrentSaveFromSource 失败必须安静回退 ——
// 受控环境：mock fetch / window / document，跑真实函数源码
function runSaveWith(fetchImpl) {
  const env = {
    fetch: fetchImpl,
    window: {},
    document: {
      createElement: () => ({ style: {}, click() {}, remove() {}, set href(_) {}, get href() { return ''; } }),
      body: { appendChild() {} },
    },
    URL: { createObjectURL: () => 'blob:fake', revokeObjectURL() {} },
    setTimeout,
    SEG_MIN_BYTES: extractConst('SEG_MIN_BYTES'),
    SEG_CONCURRENCY: extractConst('SEG_CONCURRENCY'),
    SEG_CHUNK_MIN: extractConst('SEG_CHUNK_MIN'),
  };
  // eslint-disable-next-line no-new-func
  const factory = new Function('env', `
    const fetch = env.fetch, window = env.window, document = env.document,
          URL = env.URL, setTimeout = env.setTimeout;
    const SEG_MIN_BYTES = env.SEG_MIN_BYTES, SEG_CONCURRENCY = env.SEG_CONCURRENCY,
          SEG_CHUNK_MIN = env.SEG_CHUNK_MIN;
    ${saveSrc}
    ${blobSrc}
    return concurrentSaveFromSource;
  `);
  return factory(env);
}

console.log('=== 2. concurrentSaveFromSource 失败回退 ===');

(async () => {
  // 2a. 防盗链 403（B站式）/ 直链失效：探测就 403 → UNSUPPORTED，绝不抛
  const save403 = runSaveWith(async () => ({ status: 403, headers: { get: () => '' } }));
  ok('403（防盗链）→ UNSUPPORTED', await save403('https://upos-sz-mirrorcos.bilivideo.com/x.m4s', 'a.mp4', {}) === 'UNSUPPORTED');

  // 2b. 链接过期 404 → UNSUPPORTED
  const save404 = runSaveWith(async () => ({ status: 404, headers: { get: () => '' } }));
  ok('404（链接过期）→ UNSUPPORTED', await save404('https://v23-3.kwaicdn.com/x.mp4', 'a.mp4', {}) === 'UNSUPPORTED');

  // 2c. 网络错误（平台不可达）→ UNSUPPORTED，不 throw
  const saveNetErr = runSaveWith(async () => { throw new Error('network down'); });
  let netRet = 'THREW';
  try { netRet = await saveNetErr('https://sns-video-v3.xhscdn.com/x.mp4', 'a.mp4', {}); } catch (_) {}
  ok('网络错误 → UNSUPPORTED 不抛', netRet === 'UNSUPPORTED');

  // 2d. 不支持 Range（200 无 Content-Range）→ UNSUPPORTED
  const save200 = runSaveWith(async () => ({ status: 200, headers: { get: () => 'video/mp4' } }));
  ok('200 无 Range → UNSUPPORTED', await save200('https://v23-3.kwaicdn.com/x.mp4', 'a.mp4', {}) === 'UNSUPPORTED');

  // 2e. 小文件（<8MB）走 Blob 路径：探测 206 + 整体 fetch ok → triggerBlobDownload 被调用
  let blobCalled = '';
  const smallBody = new Blob([new Uint8Array(1024)]);
  const saveSmall = runSaveWith(async (url, opts) => {
    if (opts && opts.headers && opts.headers.Range === 'bytes=0-0') {
      return { status: 206, headers: { get: (h) => (h === 'Content-Range' ? 'bytes 0-0/1048576' : '') } };
    }
    return { ok: true, status: 200, blob: async () => smallBody };
  });
  // 重新包一层以捕获 triggerBlobDownload 的 filename
  const saveSmallNamed = runSaveWith(async (url, opts) => {
    if (opts && opts.headers && opts.headers.Range === 'bytes=0-0') {
      return { status: 206, headers: { get: (h) => (h === 'Content-Range' ? 'bytes 0-0/1048576' : '') } };
    }
    blobCalled = url;
    return { ok: true, status: 200, blob: async () => smallBody };
  });
  const smallRet = await saveSmallNamed('https://v23-3.kwaicdn.com/small.mp4', '小视频.mp4', {});
  ok('小文件直链 → OK 且走了整体 fetch', smallRet === 'OK' && blobCalled.includes('small.mp4'));

  const TOTAL = 20 * 1024 * 1024;   // 20MB → parts = min(8, ceil(20/2)) = 8 段
  const SEG = Math.ceil(TOTAL / 8);
  const mockReader = (len) => {
    let sent = false;
    return {
      read: async () => {
        if (sent) return { done: true, value: undefined };
        sent = true;
        return { done: false, value: new Uint8Array(len) };
      },
    };
  };
  // 2f. 大文件分段路径：探测 206 后 mock showSaveFilePicker + writable + 每段 206 流式
  //     （Node 无 ReadableStream getReader，用最小 mock 验证并发分段逻辑跑通）
  const mockBigFetch = async (url, opts) => {
    if (opts && opts.headers && opts.headers.Range === 'bytes=0-0') {
      return { status: 206, headers: { get: (h) => (h === 'Content-Range' ? `bytes 0-0/${TOTAL}` : '') } };
    }
    const rng = /bytes=(\d+)-(\d+)/.exec((opts && opts.headers && opts.headers.Range) || '');
    const start = rng ? parseInt(rng[1], 10) : 0;
    const end = rng ? parseInt(rng[2], 10) : TOTAL - 1;
    return { status: 206, body: { getReader: () => mockReader(end - start + 1) } };
  };
  let writes = [];
  let savePickerSuggested = '';
  function runSaveWithPicker(fetchImpl, onPick) {
    const env = {
      fetch: fetchImpl,
      window: { showSaveFilePicker: async (opts) => { onPick(opts); return { createWritable: async () => ({ write: async (w) => { writes.push(w); }, close: async () => {}, abort: async () => {} }) }; } },
      document: { createElement: () => ({ style: {}, click() {}, remove() {}, set href(_) {}, get href() { return ''; } }), body: { appendChild() {} } },
      URL: { createObjectURL: () => 'blob:fake', revokeObjectURL() {} },
      setTimeout,
      SEG_MIN_BYTES: extractConst('SEG_MIN_BYTES'),
      SEG_CONCURRENCY: extractConst('SEG_CONCURRENCY'),
      SEG_CHUNK_MIN: extractConst('SEG_CHUNK_MIN'),
    };
    // eslint-disable-next-line no-new-func
    const factory = new Function('env', `
      const fetch = env.fetch, window = env.window, document = env.document,
            URL = env.URL, setTimeout = env.setTimeout;
      const SEG_MIN_BYTES = env.SEG_MIN_BYTES, SEG_CONCURRENCY = env.SEG_CONCURRENCY,
            SEG_CHUNK_MIN = env.SEG_CHUNK_MIN;
      ${saveSrc}
      ${blobSrc}
      return concurrentSaveFromSource;
    `);
    return factory(env);
  }
  const bigFn = runSaveWithPicker(mockBigFetch, (opts) => { savePickerSuggested = opts.suggestedName; });
  const bigRet = await bigFn('https://v23-3.kwaicdn.com/big.mp4', '大视频.mp4', {});
  ok('大文件分段 → OK', bigRet === 'OK');
  ok('保存面板 suggestedName 用的是 task.filename', savePickerSuggested === '大视频.mp4');
  ok('8 段全部写盘', writes.length === 8, `实际 ${writes.length} 段`);
  let totalWritten = 0;
  let offsetsOk = true;
  writes.forEach((w, i) => {
    totalWritten += w.data.length;
    if (w.position !== i * SEG) offsetsOk = false;
  });
  ok('每段写到正确偏移', offsetsOk);
  ok('写盘总量 = 文件大小', totalWritten === TOTAL, `实际 ${totalWritten}`);

  // —— 第 3 组：抖音双轨接线守卫（字段/调用链被改断就会静默退化成「存无声文件」）——
  console.log('=== 3. 抖音双轨接线 ===');
  ok('srcDirectKind 调用点传入了 audio_play_url',
    /srcDirectKind\(task\.play_url \|\| '', task\.is_hls, task\.audio_play_url \|\| ''\)/.test(SRC));
  ok('下载请求体带上 audio_play_url',
    /audio_play_url: resolved\?\.video\?\.audio_play_url \|\| ''/.test(SRC));
  ok('tracks 分支调用 saveFromTwoTracks（不是单轨 concurrentSaveFromSource）',
    /saveFromTwoTracks\(task\.play_url, task\.audio_play_url/.test(SRC));
  ok('合并所需的三个脚本都在懒加载列表里',
    /js\/vendor_mp4box\.min\.js/.test(SRC) && /js\/vendor_mp4muxer\.min\.js/.test(SRC) && /js\/video-remux\.js/.test(SRC));
  ok('合并模块与两个 vendor 库确实存在',
    ['js/vendor_mp4box.min.js', 'js/vendor_mp4muxer.min.js', 'js/video-remux.js']
      .every((f) => fs.existsSync(path.join(__dirname, '..', f)) && fs.statSync(path.join(__dirname, '..', f)).size > 1000));

  console.log(`\n结果：${pass} 通过 / ${fail} 失败`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error('测试运行异常:', e); process.exit(1); });
