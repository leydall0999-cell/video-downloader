/**
 * 守卫：上传分片的「全局 origin 租借」——每个 origin 同时只允许 1 条活跃分片。
 *
 * 背景（2026-10-23 真实 Chromium A/B，本机同一条宽带，各 2MB 分片）：
 *   · 6 路全打同一 origin  → 102 KB/s，3/6 片 120s 超时
 *   · 6 路分到 6 个 origin → 411 KB/s，6/6 成功（29.9s）
 *   · 12 路挤 6 origin     → 205 KB/s，4/12 片超时
 * ⇒ 同一 origin 的多条分片是抢同一条 HTTP/2 连接（CF 按连接限速），
 *   路数越多每条越慢。用户截图里「7 KB/s / 22 KB/s」= 12 路（2 文件 × 6 路）
 *   挤在退化到只剩同源的连接池上，每路被压到个位数。
 *
 * 修法：worker 出发前先向全局池租一个空闲 origin，同一 origin 同时只有 1 条在跑；
 * 并发上限 = 池子里 origin 数，跨文件/跨功能共享。
 *
 * 本文件既做行为断言（租借/排队/归还），也锁住几条「改了就会退回去」的接线：
 *   · ucUploadChunk 必须用租到的 ep 发请求（不能还用调用方传的 endpoint）；
 *   · 必须 finally 归还（漏归还 = 队列永久卡死，上传全挂）；
 *   · 5 条上传路径的端点探测都要先走 ucEnsureEndpointPool（否则一次抖动就永久单 origin）。
 */
const fs = require('fs');
const path = require('path');

const APP_JS = [
  path.join(__dirname, '..', '..', 'web', 'app.js'),
  path.join(__dirname, '..', 'app.js'),
].find((p) => fs.existsSync(p));
if (!APP_JS) {
  console.log('  ✗ 找不到 web/app.js');
  process.exit(1);
}
const SRC = fs.readFileSync(APP_JS, 'utf8');

let pass = 0, fail = 0;
function ok(name, cond, extra) {
  if (cond) { pass++; console.log(`  ✓ ${name}`); }
  else { fail++; console.log(`  ✗ ${name}${extra ? '  -> ' + extra : ''}`); }
}

// ── 行为测：抽出租借实现，注入受控端点池后真跑 ──────────────────────────────────
const MARK_START = 'const ucOriginBusy = new Set();';
const MARK_END = 'const ucFormatSize =';
const i0 = SRC.indexOf(MARK_START);
const i1 = SRC.indexOf(MARK_END);
const LEASE_SRC = (i0 >= 0 && i1 > i0) ? SRC.slice(i0, i1) : '';

console.log('\n【1】租借实现可独立执行');
ok('抽到了 ucOriginBusy/ucAcquireOrigin/ucReleaseOrigin 源码段', !!LEASE_SRC.trim());

let api = null;
try {
  if (LEASE_SRC) {
    const factory = new Function('UC_UPLOAD_ENDPOINTS', LEASE_SRC + `
      return { ucAcquireOrigin, ucReleaseOrigin, busy: ucOriginBusy };`);
    api = factory(['https://a.test', 'https://b.test']);
  }
} catch (e) {
  api = null;
  ok('源码段可执行', false, String(e).slice(0, 120));
}
ok('源码段可执行（无语法/引用错误）', !!api);

(async () => {
  if (api) {
    console.log('\n【2】行为：同 origin 互斥 + 排队唤醒 + 归还');
    const { ucAcquireOrigin, ucReleaseOrigin, busy } = api;
    const e1 = await ucAcquireOrigin('https://a.test');
    const e2 = await ucAcquireOrigin('https://a.test');   // a 已占 ⇒ 拿另一条空闲
    ok('第一次 acquire 拿到想要的 origin', e1 === 'https://a.test', e1);
    ok('同一条被占后自动换空闲 origin（不排队死等）', e2 === 'https://b.test', e2);
    ok('两条都占住', busy.size === 2, [...busy].join(','));

    let e3 = null, pending = true;
    const p = ucAcquireOrigin('https://a.test').then((x) => { e3 = x; pending = false; });
    await new Promise((r) => setTimeout(r, 30));
    ok('池子占满时第三条请求排队（不越权并发）', pending && e3 === null, `e3=${e3} pending=${pending}`);

    ucReleaseOrigin(e1);
    await p;
    ok('归还后排队者立刻拿到（无丢唤醒）', e3 === 'https://a.test', e3);
    ok('归还后 busy 数量一致（不泄漏）', busy.size === 2, [...busy].join(','));

    ucReleaseOrigin(e2); ucReleaseOrigin(e3);
    ok('全部归还后池子清空', busy.size === 0, [...busy].join(','));
    ok('重复归还同一条不报错（幂等）', (() => { try { ucReleaseOrigin(e1); return true; } catch (e) { return false; } })());
  }

  console.log('\n【3】接线：ucUploadChunk 必须用租到的 ep，且 finally 归还');
  const iu = SRC.indexOf('const ucUploadChunk =');
  const iuEnd = SRC.indexOf('const ucUploadOne =', iu);
  const UC_SRC = (iu >= 0 && iuEnd > iu) ? SRC.slice(iu, iuEnd) : '';
  ok('ucUploadChunk 改成 async（先 await 租借再建 XHR）', /const ucUploadChunk = async \(/.test(UC_SRC));
  ok('入口处 await ucAcquireOrigin', /const ep = await ucAcquireOrigin\(endpoint\);/.test(UC_SRC));
  ok('XHR 打的是租到的 ep（不是调用方传进来的 endpoint）',
     /xhr\.open\('POST', ep \+ '\/api\/upload-chunk'\)/.test(UC_SRC),
     '还在用 endpoint = 租借白做了');
  ok('finally 里归还（异常/取消也要还，漏还=队列卡死）',
     /\} finally \{\s*\n\s*ucReleaseOrigin\(ep\);/.test(UC_SRC));
  ok('旧写法「直接用 endpoint 建连接」已清除',
     !/xhr\.open\('POST', \(endpoint \|\| location\.origin\)/.test(SRC));

  console.log('\n【4】退化重探：5 条上传路径都要先走 ucEnsureEndpointPool');
  const probes = (SRC.match(/ucEnsureEndpointPool\(\)\.then\(\(\) => ucProbeEndpoints\(\)\)/g) || []).length;
  ok('转换/拼接/音乐/图片/字幕 5 处都接了重探', probes === 5, `实际 ${probes} 处`);
  ok('ucEnsureEndpointPool 池子已多路时直接返回（不做无谓重探）',
     /if \(UC_UPLOAD_ENDPOINTS\.length > 1\) return;/.test(SRC));
  ok('本来就只有同源时不再重探（非主站域名/本地调试别空转）',
     /if \(ucEndpointCandidates\(\)\.length <= 1\) return;/.test(SRC));
  ok('重探前丢掉缓存 promise', /_ucEpProbe = null;/.test(SRC));

  console.log('\n【5】设计意图留痕（防后人改回「每文件各自 6 路」）');
  ok('注释写明「每个 origin 同时只允许 1 条活跃分片」', /每个 origin 同时只允许 1 条活跃分片/.test(SRC));
  ok('注释保留了实测三组数据（102/411/205 KB/s）',
     /102 KB\/s/.test(SRC) && /411 KB\/s/.test(SRC) && /205 KB\/s/.test(SRC),
     '数据被删 ⇒ 后人只会看到「为什么不用 6 路」然后改回去');

  console.log('\n' + '-'.repeat(60));
  console.log(`结果：${pass} 通过 / ${fail} 失败`);
  process.exit(fail ? 1 : 0);
})();
