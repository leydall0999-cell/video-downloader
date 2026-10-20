/**
 * 行为测试：从 web/app.js 抽出 probeTotalBytes / concurrentSaveFile，
 * 接本地支持 Range 的 HTTP 服务，验证 8 段并发下载后字节完全一致。
 *
 * 为什么这么测：这两个函数写在 IIFE 里，页面导出不了；但把它们按源码原样
 * 注入一个可控环境执行，测的就是**真实代码**，能抓出偏移算错、丢最后一段、
 * 并发写盘错位这类只有跑起来才暴露的 bug。
 */
const fs = require('fs');
const http = require('http');
const crypto = require('crypto');
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
  // 找到函数体起始的 '{'
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

const probeSrc = extractFn('probeTotalBytes');
const saveSrc = extractFn('concurrentSaveFile');

// 常量：从源码里取真值，避免测试和实现各写一份而漂移
function extractConst(name) {
  const m = new RegExp(`const ${name}\\s*=\\s*([0-9\\s*+]+);`).exec(SRC);
  if (!m) throw new Error(`找不到常量 ${name}`);
  // eslint-disable-next-line no-new-func
  return new Function(`return (${m[1]});`)();
}

const CONSTS = ['SEG_CONCURRENCY', 'SEG_MIN_BYTES', 'SEG_CHUNK_MIN'];
const constSrc = CONSTS.map((c) => `const ${c} = ${extractConst(c)};`).join('\n');

console.log('=== 0. 源码抽取 ===');
ok('probeTotalBytes 已抽取', probeSrc.length > 100);
ok('concurrentSaveFile 已抽取', saveSrc.length > 500);
CONSTS.forEach((c) => console.log(`    ${c} = ${extractConst(c)}`));

/** 在受控环境里执行抽取出来的源码。 */
function buildModule(env) {
  const fn = new Function(
    'window', 'deviceId', 'authBearerHeaders', 'fetch', 'console',
    `${constSrc}
${probeSrc}
${saveSrc}
return { probeTotalBytes, concurrentSaveFile };`
  );
  return fn(env.window, env.deviceId, env.authBearerHeaders, env.fetch, console);
}

/** 起一个支持 Range 的本地服务。 */
function makeServer(buffer, opts = {}) {
  const supportsRange = opts.supportsRange !== false;
  const server = http.createServer((req, res) => {
    const total = buffer.length;
    const range = req.headers.range;
    if (!supportsRange || !range) {
      res.writeHead(200, { 'Content-Length': total, 'Accept-Ranges': 'none' });
      res.end(buffer);
      return;
    }
    const m = /bytes=(\d+)-(\d*)/.exec(range);
    const start = parseInt(m[1], 10);
    const end = m[2] === '' ? total - 1 : Math.min(parseInt(m[2], 10), total - 1);
    if (start >= total) {
      res.writeHead(416, { 'Content-Range': `bytes */${total}` });
      res.end();
      return;
    }
    const slice = buffer.subarray(start, end + 1);
    res.writeHead(206, {
      'Content-Range': `bytes ${start}-${end}/${total}`,
      'Content-Length': slice.length,
      'Accept-Ranges': 'bytes',
      'Content-Type': 'application/octet-stream',
    });
    res.end(slice);
  });
  return server;
}

/** 内存版「写盘句柄」：记录每次 write 的 position，模拟 FileSystemWritableFileStream。 */
function makePicker(total, store) {
  return async function showSaveFilePicker(pickOpts) {
    store.suggestedName = pickOpts && pickOpts.suggestedName;
    const writable = {
      async write({ type, position, data }) {
        if (type !== 'write') throw new Error('unexpected write type: ' + type);
        if (!(data instanceof Uint8Array)) throw new Error('data 不是 Uint8Array');
        if (position < 0 || position + data.length > total) {
          throw new Error(`越界写: position=${position} len=${data.length} total=${total}`);
        }
        store.buf.set(data, position);
        store.writes++;
      },
      async close() { store.closed = true; },
      async abort() { store.aborted = true; },
    };
    return { createWritable: async () => writable };
  };
}

function sha(buf) { return crypto.createHash('sha256').update(buf).digest('hex'); }

async function run() {
  const SIZE = 21 * 1024 * 1024 + 37;   // 21MB+37B：非整除，专治「最后一段算错」
  console.log(`\n=== 1. 端到端：${SIZE} 字节随机文件 ===`);
  const original = crypto.randomBytes(SIZE);
  const server = makeServer(original);
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const port = server.address().port;
  const url = `http://127.0.0.1:${port}/file?download=1&device=test`;

  const store = { buf: Buffer.alloc(SIZE), writes: 0, closed: false, aborted: false };
  const progress = [];
  const env = {
    window: { showSaveFilePicker: makePicker(SIZE, store) },
    deviceId: () => 'test-device',
    authBearerHeaders: () => ({}),
    fetch: globalThis.fetch,
  };
  const m = buildModule(env);

  const t0 = Date.now();
  const ret = await m.concurrentSaveFile(url, 'demo.mp4', {
    onProgress: (done, total) => progress.push([done, total]),
  });
  const ms = Date.now() - t0;
  server.close();

  ok('返回 OK', ret === 'OK', ret);
  ok('文件句柄已 close', store.closed === true);
  ok('未走 abort', store.aborted === false);
  ok('写盘次数 > 0', store.writes > 0, `writes=${store.writes}`);
  ok('字节完全一致（sha256）', sha(store.buf) === sha(original),
    `期望 ${sha(original).slice(0, 12)} 实际 ${sha(store.buf).slice(0, 12)}`);
  ok('文件名透传给保存面板', store.suggestedName === 'demo.mp4', store.suggestedName);
  ok('进度回调收到数据', progress.length > 0, `${progress.length} 次`);
  if (progress.length) {
    const last = progress[progress.length - 1];
    ok('末次进度 = 总量', last[0] === SIZE && last[1] === SIZE, JSON.stringify(last));
    const mono = progress.every((p, i) => i === 0 || p[0] >= progress[i - 1][0]);
    ok('已下载量单调递增', mono);
  }
  console.log(`    (${ms}ms, ${Math.round(SIZE / 1024 / 1024)}MB 本机回环)`);

  console.log('\n=== 2. 服务端不支持 Range → 必须回退 ===');
  {
    const buf = crypto.randomBytes(9 * 1024 * 1024);
    const srv = makeServer(buf, { supportsRange: false });
    await new Promise((r) => srv.listen(0, '127.0.0.1', r));
    const u = `http://127.0.0.1:${srv.address().port}/f`;
    const st = { buf: Buffer.alloc(buf.length), writes: 0, closed: false, aborted: false };
    const mm = buildModule({
      window: { showSaveFilePicker: makePicker(buf.length, st) },
      deviceId: () => 'd', authBearerHeaders: () => ({}), fetch: globalThis.fetch,
    });
    const r = await mm.concurrentSaveFile(u, 'x.bin', { onProgress: () => {} });
    srv.close();
    ok('返回 UNSUPPORTED', r === 'UNSUPPORTED', r);
    ok('没碰过文件句柄', st.writes === 0 && st.closed === false);
  }

  console.log('\n=== 3. 小文件（< 8MB）→ SKIP，不折腾 ===');
  {
    const buf = crypto.randomBytes(3 * 1024 * 1024);
    const srv = makeServer(buf);
    await new Promise((r) => srv.listen(0, '127.0.0.1', r));
    const u = `http://127.0.0.1:${srv.address().port}/f`;
    const mm = buildModule({
      window: { showSaveFilePicker: async () => { throw new Error('不该调用'); } },
      deviceId: () => 'd', authBearerHeaders: () => ({}), fetch: globalThis.fetch,
    });
    const r = await mm.concurrentSaveFile(u, 'x.bin', { onProgress: () => {} });
    srv.close();
    ok('返回 SKIP', r === 'SKIP', r);
  }

  console.log('\n=== 4. 用户取消保存面板 → CANCELLED ===');
  {
    const buf = crypto.randomBytes(9 * 1024 * 1024);
    const srv = makeServer(buf);
    await new Promise((r) => srv.listen(0, '127.0.0.1', r));
    const u = `http://127.0.0.1:${srv.address().port}/f`;
    const mm = buildModule({
      window: {
        showSaveFilePicker: async () => {
          const e = new Error('cancelled'); e.name = 'AbortError'; throw e;
        },
      },
      deviceId: () => 'd', authBearerHeaders: () => ({}), fetch: globalThis.fetch,
    });
    const r = await mm.concurrentSaveFile(u, 'x.bin', { onProgress: () => {} });
    srv.close();
    ok('返回 CANCELLED', r === 'CANCELLED', r);
  }

  console.log('\n=== 5. 无 showSaveFilePicker（老浏览器）→ UNSUPPORTED ===');
  {
    const mm = buildModule({
      window: {}, deviceId: () => 'd', authBearerHeaders: () => ({}), fetch: globalThis.fetch,
    });
    const r = await mm.concurrentSaveFile('http://127.0.0.1:1/none', 'x', { onProgress: () => {} });
    ok('返回 UNSUPPORTED', r === 'UNSUPPORTED', r);
  }

  console.log('\n=== 6. 分段请求被拒（服务端只给 200）→ 抛错让上层回退 ===');
  {
    const buf = crypto.randomBytes(9 * 1024 * 1024);
    // 探测时假装支持 Range，实际分段请求返回 200
    let first = true;
    const srv = http.createServer((req, res) => {
      const total = buf.length;
      const range = req.headers.range;
      if (first && range) {
        first = false;
        res.writeHead(206, {
          'Content-Range': `bytes 0-0/${total}`, 'Content-Length': 1, 'Accept-Ranges': 'bytes',
        });
        res.end(buf.subarray(0, 1));
        return;
      }
      res.writeHead(200, { 'Content-Length': total });
      res.end(buf);
    });
    await new Promise((r) => srv.listen(0, '127.0.0.1', r));
    const u = `http://127.0.0.1:${srv.address().port}/f`;
    const st = { buf: Buffer.alloc(buf.length), writes: 0, closed: false, aborted: false };
    const mm = buildModule({
      window: { showSaveFilePicker: makePicker(buf.length, st) },
      deviceId: () => 'd', authBearerHeaders: () => ({}), fetch: globalThis.fetch,
    });
    let threw = false, aborted = false;
    try {
      await mm.concurrentSaveFile(u, 'x.bin', { onProgress: () => {} });
    } catch (_) { threw = true; }
    aborted = st.aborted;
    srv.close();
    ok('抛出异常（上层据此回退整页下载）', threw);
    ok('半成品被 abort 丢弃（不留残缺文件）', aborted === true);
    ok('没有 close（否则会落一个坏文件）', st.closed === false);
  }

  console.log(`\n===== 结果：${pass} passed, ${fail} failed =====`);
  process.exit(fail ? 1 : 0);
}

run().catch((e) => { console.error('测试崩溃:', e); process.exit(1); });
