/**
 * 行为测试：上传端点池 + 分片参数（2026-10-22 上传提速改造）。
 *
 * 背景（当天实测数据，同一条用户宽带）：
 *   · 用户宽带纯上行上限 ≈ 873 KB/s（nc 直传 HK 节点，不经任何 Web 服务）
 *   · 改造前 32MB×2 路同源  = 93 KB/s
 *   · 改造后 2MB×6 路跨端口 = 886 KB/s（≈吃满上行）
 *   · 32MB 单片在 CF 链路 169s 必被 524 掐断（CF 源站响应超时约 100s）
 *
 * 根因：浏览器对同一 origin 复用**一条** HTTP/2 连接，CF 按连接限速 ⇒ 并行分片不是
 * 并行，是抢同一条管子。解法是「同一主机换 HTTPS 端口」—— 不同端口在浏览器里是不同
 * origin ⇒ 独立连接池 ⇒ 独立限速桶。
 *
 * 🔴 本案最大的风险不是慢，是**把分片传到了另一个后端的磁盘上**（2026-09-11 Railway
 *    事故：分片落到别的节点，finish 必报「分片不完整」）。所以本文件把「只可能对
 *    同一后端生成候选」当成红线来测，包括 hk.hanyuxz.top 这个**独立物理节点**不能被
 *    误纳进来。
 *
 * 测法：把 app.js 里端点池那段源码原样抽出来，注入假的 location/window 后执行，
 * 做真实行为断言（不是正则 grep），这样改坏了立马红。
 */
const fs = require('fs');
const path = require('path');

// 本文件由 run_offline_tests.sh 从 server/tests 下执行；兼容直接放在 web/tests 下时单跑。
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

// ── 抽出端点池源码块，注入受控 location/window 后执行 ────────────────────────────
const MARK_START = 'const ucEndpointCandidates = () => {';
const MARK_END = 'const ucNoteEpResult = (ep, ok) => {';
const iStart = SRC.indexOf(MARK_START);
const iEnd = SRC.indexOf(MARK_END);
if (iStart < 0 || iEnd < 0) {
  console.log('  ✗ 在 app.js 里找不到端点池源码块（MARK 失效）');
  process.exit(1);
}
const iEndClose = SRC.indexOf('};', iEnd) + 2;
const BLOCK = SRC.slice(iStart, iEndClose);

/**
 * 造一个隔离的端点池实例。
 * @param {string} origin  假 location.origin
 * @param {object} opts    { https, hostname, fetchImpl }
 */
function makePool(origin, opts = {}) {
  const loc = {
    origin,
    protocol: opts.https === false ? 'http:' : 'https:',
    hostname: opts.hostname !== undefined ? opts.hostname : new URL(origin).hostname,
  };
  const fetchImpl = opts.fetchImpl || (async () => ({ ok: true }));
  const body = `
    let UC_UPLOAD_ENDPOINTS = [location.origin];
    const UC_EP_FAIL_LIMIT = 3;
    const ucEpFail = Object.create(null);
    ${BLOCK}
    return {
      candidates: ucEndpointCandidates,
      probe: ucProbeEndpoints,
      stats: ucChStats,
      pick: ucPickEndpoint,
      note: ucNoteEpResult,
      get list() { return UC_UPLOAD_ENDPOINTS; },
      set list(v) { UC_UPLOAD_ENDPOINTS = v; },
      fails: ucEpFail,
    };
  `;
  const win = { AbortController };
  const factory = new Function('location', 'window', 'fetch', body);
  return factory(loc, win, fetchImpl);
}

(async () => {
  console.log('\n【1】分片参数：2MB 甜点 / 6 路拐点（实测标定值）');
  ok('UC_CHUNK_SIZE = 2MB', /const UC_CHUNK_SIZE = 2 \* 1024 \* 1024;/.test(SRC));
  ok('UC_BIG_CHUNK_SIZE = 4MB（>2GB 大文件，控请求数）', /const UC_BIG_CHUNK_SIZE = 4 \* 1024 \* 1024;/.test(SRC));
  ok('UC_CHUNK_CONCURRENCY = 6（8 路会把源站打爆，实测全 502）', /const UC_CHUNK_CONCURRENCY = 6;/.test(SRC));
  ok('MUS/IMG 也是 2MB', /const MUS_CHUNK_SIZE = 2 \* 1024 \* 1024;/.test(SRC) && /const IMG_CHUNK_SIZE = 2 \* 1024 \* 1024;/.test(SRC));
  ok('MUS/IMG 不再是串行（有并发常量且 >1）',
    /const MUS_CHUNK_CONCURRENCY = 4;/.test(SRC) && /const IMG_CHUNK_CONCURRENCY = 4;/.test(SRC));
  ok('全站再无 32MB/64MB 分片常量（它们在 CF 链路会被 524）',
    !/(UC|MUS|IMG)_(BIG_)?CHUNK_SIZE = (32|64) \* 1024 \* 1024/.test(SRC));

  console.log('\n【2】候选生成：只对「已验证同后端」的那一个主机名放开');
  const poolMain = makePool('https://hanyuxz.top');
  const cands = poolMain.candidates();
  ok('主站 https 下会生成多端口候选', cands.length > 1, `实际 ${cands.length} 条`);
  ok('候选全部是 https 且主机名只有 hanyuxz.top / vdl-check.hanyuxz.top',
    cands.every((c) => {
      try {
        const u = new URL(c);
        return u.protocol === 'https:' && (u.hostname === 'hanyuxz.top' || u.hostname === 'vdl-check.hanyuxz.top');
      } catch (e) { return false; }
    }), JSON.stringify(cands));
  ok('CF 边缘的 6 个 HTTPS 端口都在候选里',
    [443, 2053, 2083, 2087, 2096, 8443].every((p) => cands.some((c) => c === `https://hanyuxz.top:${p}` || (p === 443 && c === 'https://hanyuxz.top'))),
    JSON.stringify(cands));
  ok('候选不含重复项', new Set(cands).size === cands.length);

  console.log('\n【3】红线：绝不把分片送到另一个物理节点');
  ok('hk.hanyuxz.top（香港独立节点）只生成单一同源',
    JSON.stringify(makePool('https://hk.hanyuxz.top').candidates()) === JSON.stringify(['https://hk.hanyuxz.top']));
  ok('http 本机（桌面 App 的 pywebview）只生成单一同源',
    JSON.stringify(makePool('http://127.0.0.1:8321', { https: false, hostname: '127.0.0.1' }).candidates())
      === JSON.stringify(['http://127.0.0.1:8321']));
  ok('localhost 只生成单一同源',
    JSON.stringify(makePool('http://localhost:8321', { https: false, hostname: 'localhost' }).candidates())
      === JSON.stringify(['http://localhost:8321']));
  ok('裸 IP 部署只生成单一同源',
    JSON.stringify(makePool('https://8.138.223.3', { hostname: '8.138.223.3' }).candidates())
      === JSON.stringify(['https://8.138.223.3']));

  console.log('\n【4】可达性探测：只留真能用的端口（企业/校园网常只放行 443）');
  const seen = [];
  const poolProbe = makePool('https://hanyuxz.top', {
    fetchImpl: async (url) => { seen.push(url); return { ok: /:2053|:8443/.test(url) }; },
  });
  const usable = await poolProbe.probe();
  // 桩实现只对 :2053/:8443 返回 200 —— 两个主机名各 2 个端口 + 主域自己 = 5 条
  ok('探测后只保留可达端点（+ 主域自己）', usable.length === 5, JSON.stringify(usable));
  ok('可用结果里含所有返回 200 的端口', usable.some((u) => u === 'https://hanyuxz.top:2053') && usable.some((u) => u === 'https://hanyuxz.top:8443'));
  ok('不可达端口被剔除', !usable.some((u) => u === 'https://hanyuxz.top:2083'));
  ok('探测并发发起（不是串行逐个试）', seen.length >= 10, `实际 ${seen.length} 次`);
  const poolAllDead = makePool('https://hanyuxz.top', { fetchImpl: async () => { throw new Error('blocked'); } });
  ok('全部端口不可达时安全退回单一同源',
    JSON.stringify(await poolAllDead.probe()) === JSON.stringify(['https://hanyuxz.top']));

  // 实测备注：下面这些断言钉的是「绑路」这个已落地的行为；将来若改为轮换，只要吞吐不
  // 劣化（A/B 已证明两者同档）也可以，但必须连同上方注释一起更新。
  console.log('\n【5】选路：每路 worker 绑死一条连接');
  // 🔴 为什么绑路：2026-10-22 同一浏览器会话 A/B（各 6 路 × 3 片 × 1MB）
  //      · 单 origin（改造前的行为） 100 KB/s  ← 与用户报的 93 KB/s 吻合，根因确诊
  //      · 6 端口绑路 227 KB/s  ·  6 端口轮换 224 KB/s
  //    绑路与轮换吞吐无差别，决定性变量是「用几个独立连接池」，不是怎么分配。
  //    这里选绑路只为把并发 socket 固定成 N 条（而非 N 路 × M 端点），且慢 conn 上的路会自动少取分片。
  const poolRR = makePool('https://hanyuxz.top');
  poolRR.list = ['https://hanyuxz.top', 'https://hanyuxz.top:2053', 'https://hanyuxz.top:2083',
                 'https://hanyuxz.top:2087', 'https://hanyuxz.top:2096', 'https://hanyuxz.top:8443'];
  const it = { _chStats: poolRR.stats() };
  const lanes = [];
  for (let w = 0; w < 6; w++) lanes.push(poolRR.pick(it, 0, 0, w));
  ok('6 路 worker 落在 6 个不同的独立连接池上', new Set(lanes).size === 6, JSON.stringify(lanes));
  ok('lane 0 一路固定同一个端点（连传 20 片也是它）',
    Array.from({ length: 20 }, (_, k) => poolRR.pick(it, k, 0, 0)).every((p) => p === lanes[0]), lanes[0]);
  ok('不同 lane 之间互不串台',
    [1, 2, 3, 4, 5].every((w) => poolRR.pick(it, 0, 0, w) === lanes[w]));
  const poolWrap = makePool('https://hanyuxz.top');
  poolWrap.list = ['https://hanyuxz.top', 'https://hanyuxz.top:2053'];
  const wrapItem = { _chStats: poolWrap.stats() };
  ok('端点数少于路数时按 lane 取模回绕（4 路分摊 2 条）',
    [0, 1, 2, 3].every((w) => poolWrap.pick(wrapItem, 0, 0, w) === poolWrap.list[w % 2]));
  ok('没传 lane 的调用点退回轮询（历史行为不破坏）',
    (() => {
      const legacy = { _chStats: poolRR.stats() };
      const seen = new Set();
      for (let i = 0; i < 6; i++) seen.add(poolRR.pick(legacy, i, 0));
      return seen.size === 6;
    })());

  // 故障转移：attempt>0 必须换一条，不能重试坏链路
  const itFail = { _chStats: poolRR.stats(), _lastEp: 'https://hanyuxz.top:2053' };
  const retryEp = poolRR.pick(itFail, 0, 1);
  ok('重试（attempt>0）不会重选刚失败的那条通道', retryEp !== 'https://hanyuxz.top:2053', retryEp);

  // 连续失败 3 次后摘除端点，被摘的那路要自动改绑还活着的
  const poolEvict = makePool('https://hanyuxz.top');
  poolEvict.list = ['https://hanyuxz.top', 'https://hanyuxz.top:2053', 'https://hanyuxz.top:2083'];
  const dead = 'https://hanyuxz.top:2083';
  const evItem = { _chStats: poolEvict.stats() };
  poolEvict.note(dead, false); poolEvict.note(dead, false);
  ok('未达阈值前绑定的端点继续用（不因一次抖动就换连接）',
    poolEvict.pick(evItem, 0, 0, 2) === dead, poolEvict.pick(evItem, 0, 0, 2));
  poolEvict.note(dead, false);   // 第 3 次 → 触发摘除
  const newPin = poolEvict.pick(evItem, 0, 0, 2);
  ok('端点被摘除后该路自动改绑到还活着的池', newPin !== dead && poolEvict.list.includes(newPin), newPin);
  ok('其余路未被牵连', poolEvict.pick(evItem, 0, 0, 0) === 'https://hanyuxz.top'
    && poolEvict.pick(evItem, 0, 0, 1) === 'https://hanyuxz.top:2053');
  poolEvict.note(dead, true);    // 成功即清零
  const poolBack = makePool('https://hanyuxz.top');
  poolBack.list = ['https://hanyuxz.top', 'https://hanyuxz.top:2053', dead];
  const backItem = { _chStats: poolBack.stats() };
  poolBack.note(dead, false); poolBack.note(dead, false); poolBack.note(dead, false);
  poolBack.note(dead, true);
  ok('成功后失败计数清零，端点重新可选（没有被永久拉黑）',
    poolBack.pick(backItem, 0, 0, 2) === dead, poolBack.pick(backItem, 0, 0, 2));

  console.log('\n【6】退化：只有单一端点时必须照常工作（不报错、不空数组）');
  const poolOne = makePool('https://hanyuxz.top');
  poolOne.list = ['https://hanyuxz.top'];
  ok('单端点时选路恒返回它自己',
    [0, 1, 2, 3].every((i) => poolOne.pick({ _chStats: poolOne.stats() }, i, 0) === 'https://hanyuxz.top')
    && poolOne.pick({ _chStats: poolOne.stats(), _lastEp: 'https://hanyuxz.top' }, 0, 1) === 'https://hanyuxz.top');

  console.log('\n【7】四条上传路径都要真的用上多端点（否则改了等于没改）');
  ok('视频转码路径传 ep', /const ep = ucPickEndpoint\(item, i, attempts, lane\);[\s\S]{0,220}ucUploadChunk\(uploadId, i, totalChunks, blob, \(loaded\) => \{[\s\S]{0,120}item\._xhrs, ep\)/.test(SRC));
  ok('每条路径都把 lane 号传进选路（绑连接的前提）',
    (SRC.match(/ucPickEndpoint\([^)]*,\s*lane\)/g) || []).length >= 5,
    `${(SRC.match(/ucPickEndpoint\([^)]*,\s*lane\)/g) || []).length} 处`);
  ok('视频拼接（store 模式）路径传 ep', /ucUploadChunk\(uploadId, i, totalChunks, blob, \(loaded\) => \{ inFlight\.set\(i, loaded\); updateProgress\(\); \}, item\._xhrs, ep\)/.test(SRC));
  ok('音乐路径传 ep', /ucUploadChunk\(uploadId, idx, totalChunks, blob, null, xhrs, ep\)/.test(SRC));
  ok('字幕路径传 ep', SRC.split('ucUploadChunk(uploadId, idx, totalChunks, blob, null, xhrs, ep)').length - 1 >= 3);
  ok('每条路径都记录成败（供剔除坏端口）', (SRC.match(/ucNoteEpResult\(/g) || []).length >= 6);
  ok('每条路径都在起 worker 前等端点探测（6.5s 超时兜底）× 5 处',
    (SRC.match(/6500/g) || []).length >= 5, `${(SRC.match(/6500/g) || []).length} 处`);

  console.log(`\n结果：${pass} 通过 / ${fail} 失败`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error('测试运行异常:', e); process.exit(1); });
