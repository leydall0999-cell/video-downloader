/**
 * remux_e2e.mjs —— 抖音双轨「浏览器内合并」真实端到端验证
 *
 * 为什么需要它：web/js/video-remux.js 依赖真实浏览器的 mp4box + Blob，
 * 纯 Node 单测跑不出真行为（WebCodecs/Blob/脚本加载都不在 Node 里）。
 * 所以这里真的拉起 Chrome for Testing，在页面里跑合并，再把产物写盘交给 ffprobe 验收。
 *
 * 用法：
 *   # 1) 线上真实直链（推荐，验证 CDN 防盗链 + CORS + 合并全链路）
 *   node web/tests/remux_e2e.mjs --video "<video_url>" --audio "<audio_url>"
 *   # 2) 本地两条轨文件
 *   node web/tests/remux_e2e.mjs --vfile /tmp/dy_v.mp4 --afile /tmp/dy_a.mp4
 *   # 可选：--out /tmp/muxout（默认），--keep（不删 profile）
 *
 * 依赖：本机有 Chrome for Testing（默认 /tmp/cft，可用 --chrome 指定）。
 */
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WEB = path.resolve(HERE, '..');

function arg(name, dflt) {
  const i = process.argv.indexOf('--' + name);
  return i > 0 && process.argv[i + 1] ? process.argv[i + 1] : dflt;
}
const VIDEO = arg('video', '');
const AUDIO = arg('audio', '');
const VFILE = arg('vfile', '');
const AFILE = arg('afile', '');
const OUT = arg('out', '/tmp/muxout');
const CHROME = arg('chrome', '/tmp/cft/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing');

if (!(VIDEO && AUDIO) && !(VFILE && AFILE)) {
  console.error('需要 --video/--audio 或 --vfile/--afile');
  process.exit(2);
}
fs.mkdirSync(OUT, { recursive: true });

const PORT = 8791, CDP = 9400 + Math.floor(Math.random() * 300);
const PROFILE = `/tmp/remux-e2e-${CDP}`;
fs.rmSync(PROFILE, { recursive: true, force: true });

// 用真实抖音文件算扩展名无关紧要；这里只提供页面与库，媒体源走真实 URL 或本地文件流
const server = http.createServer((req, res) => {
  const u = req.url.split('?')[0];
  if (u === '/' || u === '/index.html') {
    res.writeHead(200, { 'content-type': 'text/html' });
    return res.end('<!doctype html><meta charset=utf8><title>remux e2e</title>' +
      '<script src="/js/vendor_mp4box.min.js"></script>' +
      '<script src="/js/vendor_mp4muxer.min.js"></script>' +
      '<script src="/js/video-remux.js"></script><h1>ok</h1>');
  }
  if ((u === '/v.bin' || u === '/a.bin') && VFILE) {
    const f = u === '/v.bin' ? VFILE : AFILE;
    const st = fs.statSync(f);
    res.writeHead(200, { 'content-type': 'video/mp4', 'content-length': st.size });
    return fs.createReadStream(f).pipe(res);
  }
  const f = path.join(WEB, u.replace(/^\//, ''));
  if (f.startsWith(WEB) && fs.existsSync(f) && fs.statSync(f).isFile()) {
    res.writeHead(200, { 'content-type': 'application/javascript' });
    return fs.createReadStream(f).pipe(res);
  }
  res.writeHead(404); res.end('no');
});
await new Promise((r) => server.listen(PORT, '127.0.0.1', r));

const chrome = spawn(CHROME, [
  `--user-data-dir=${PROFILE}`, '--no-sandbox', '--no-first-run', '--no-default-browser-check',
  '--disable-gpu', `--remote-debugging-port=${CDP}`, 'about:blank',
], { stdio: ['ignore', 'pipe', 'pipe'] });
let clog = '';
chrome.stdout.on('data', (d) => { clog += d; });
chrome.stderr.on('data', (d) => { clog += d; });
const bye = () => { try { chrome.kill('SIGKILL'); } catch (_) {} };
process.on('exit', bye);

let ver = null;
for (let i = 0; i < 80; i++) {
  try { ver = await (await fetch(`http://127.0.0.1:${CDP}/json/version`)).json(); break; }
  catch (_) { await new Promise((r) => setTimeout(r, 250)); }
}
if (!ver) { console.log('CHROME 未起来：', clog.slice(-400)); process.exit(1); }

let mid = 0; const pending = new Map(); const logs = [];
const ws = new WebSocket(ver.webSocketDebuggerUrl);
await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
const send = (m, p, s) => {
  const id = ++mid; const o = { id, method: m, params: p || {} };
  if (s) o.sessionId = s;
  ws.send(JSON.stringify(o));
  return new Promise((res) => pending.set(id, res));
};
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
  if (m.method === 'Runtime.exceptionThrown') logs.push('EXC ' + String(m.params.exceptionDetails?.exception?.description || '').slice(0, 200));
};

await send('Browser.setDownloadBehavior', { behavior: 'allow', downloadPath: OUT });
const c = await send('Target.createTarget', { url: `http://127.0.0.1:${PORT}/` });
const a = await send('Target.attachToTarget', { targetId: c.result.targetId, flatten: true });
const s = a.result.sessionId;
await send('Runtime.enable', {}, s);
await new Promise((r) => setTimeout(r, 700));

const ev = async (expr) => {
  const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true }, s);
  const rr = r.result || {};
  if (rr.exceptionDetails) return { __err: rr.exceptionDetails.exception?.description || 'eval error' };
  return rr.result?.value;
};

const VURL = VIDEO || `http://127.0.0.1:${PORT}/v.bin`;
const AURL = AUDIO || `http://127.0.0.1:${PORT}/a.bin`;
const t0 = Date.now();
const ret = await ev(`(async () => {
  const t0 = performance.now();
  const get = async (u) => {
    const r = await fetch(u, { credentials: 'omit', referrerPolicy: 'no-referrer', cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status + ' @ ' + u.slice(0, 60));
    return new Uint8Array(await r.arrayBuffer());
  };
  try {
    const v = await get(${JSON.stringify(VURL)});
    const a = await get(${JSON.stringify(AURL)});
    const r = await VDLRemux.remux(v, a);
    window.__blob = r.blob;
    const el = document.createElement('video');
    el.muted = true; el.src = URL.createObjectURL(r.blob);
    document.body.appendChild(el);
    const meta = await new Promise((res) => {
      const t = setTimeout(() => res({ timeout: true }), 8000);
      el.onloadedmetadata = () => { clearTimeout(t); res({ dur: el.duration, w: el.videoWidth, h: el.videoHeight }); };
      el.onerror = () => { clearTimeout(t); res({ playErr: (el.error && el.error.code) || 'err' }); };
      el.load();
    });
    if (!meta.timeout && !meta.playErr) { try { await el.play(); meta.playing = !el.paused; } catch (e) { meta.playing = 'blocked'; } }
    return { ok: true, vBytes: v.length, aBytes: a.length, outBytes: r.blob.size,
             w: r.width, h: r.height, durUs: r.durationUs, vs: r.videoSamples, as: r.audioSamples,
             meta: meta, ms: Math.round(performance.now() - t0) };
  } catch (e) { return { ok: false, err: String(e && e.message || e) }; }
})()`);

console.log(JSON.stringify(ret, null, 1));
console.log('总耗时 ms:', Date.now() - t0);

if (ret && ret.ok) {
  await ev(`(() => { const u = URL.createObjectURL(window.__blob); const a = document.createElement('a');
    a.href = u; a.download = 'remux_out.mp4'; document.body.appendChild(a); a.click(); })()`);
  await new Promise((r) => setTimeout(r, 3500));
  const f = path.join(OUT, 'remux_out.mp4');
  if (fs.existsSync(f)) {
    console.log('落盘:', f, fs.statSync(f).size, 'bytes  sha256=', crypto.createHash('sha256').update(fs.readFileSync(f)).digest('hex').slice(0, 16));
  } else {
    console.log('落盘失败：', fs.readdirSync(OUT));
  }
}
if (logs.length) console.log('页面异常:', logs.slice(-5));
ws.close(); server.close(); bye();
process.exit(ret && ret.ok ? 0 : 1);
