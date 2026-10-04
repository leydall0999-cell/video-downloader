// 🔴 cn / hk 两个部署面的安全修复必须同步（2026-10-04 血泪教训）
//
// 事故经过：本仓（cn，8.138.223.3，规范部署面）修好了
//   ① `/api/member/activate` 的白嫖后门（加 user_is_admin 门禁）
//   ② 个人中心占位功能行 / 权益文案清理
//   ③ 顶层卡片「连一起」的 3rem 间距
// 但 **hk（47.82.101.79，另一台也对外提供服务的机器）上跑的是上一次同步的旧副本**，
// 于是在 hk 上实测复现了同一个白嫖：任意注册用户 POST
// `{"code":"download_year"}` 即得 `ok:true` + `download_member.active=True`（一年会员）。
// 且 `hk.hanyuxz.top` 是 **DNS 直接解析到那台机器**（主域名走 CF，hk 不走），
// 公网可直接触达 ⇒ 安全修复只落在一半部署面上，等于没修。
//
// 本测试钉死：**仓库里每个「已在路由里出现的成员/权限判据」都必须在两端一致**。
// 它不需要网络（纯静态比对），能进离线套件，所以每次提交都会跑；
// 想真查线上差异，跑 server/tests/check_deploy_drift.sh（需 ssh）。
//
// 为什么不能只靠流程：本次是先在 cn 修好、几天后才在旧日志里注意到 hk 是旧副本。
// 而 hk 从 2026-10-01 起就没再同步过 —— 落后 4 个前端提交、含 1 个安全修复。
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const serverDir = join(repoRoot, 'server');

function collectPy(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    if (name === '__pycache__') continue;
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...collectPy(p));
    else if (name.endsWith('.py')) out.push(p);
  }
  return out;
}
const srcs = collectPy(serverDir)
  .map(p => ({ name: p.slice(serverDir.length + 1), src: readFileSync(p, 'utf8') }));

// ── ① 所有会员/权限相关的「判据函数」都必须存在 ─────────────────────────────
// 新增这类函数时（尤其带 user_is_admin / require_admin 的），本断言会提醒同步两端口径。
const GUARDS = [
  'def user_is_admin(',            // auth_store：账号是否超管（activate / 后台门禁共用）
  'def require_admin(',            // 后台接口鉴权
];
for (const g of GUARDS) {
  assert.ok(srcs.some(({ src }) => src.includes(g)),
    `server/ 里找不到 ${g} —— 若被误删，权限判据会静默失效。`);
}

// ── ② 套餐直激活必须带超管门禁，且门禁在发放之前 ─────────────────────────────
// 这是本轮实测在 hk 上被利用的那个口子，两端都必须有。
const routerPy = srcs.find(({ name }) => name === join('routers', 'membership.py'));
assert.ok(routerPy, 'server/routers/membership.py 必须存在');
const fnStart = routerPy.src.indexOf('def member_activate(');
assert.ok(fnStart > 0, 'server/routers/membership.py 必须仍有 member_activate 路由');
const fnEnd = routerPy.src.indexOf('\n@router.', fnStart + 10);
const body = routerPy.src.slice(fnStart, fnEnd > 0 ? fnEnd : undefined);

assert.ok(/user_is_admin\s*\(\s*uid\s*\)/.test(body),
  'member_activate 必须调用 user_is_admin(uid) —— 这是 2026-10-04 在 hk 上实测可利用的'
  + '白嫖后门（任意登录用户一条 curl 拿一年会员）。改这个路由前务必确认 cn + hk 两端都已收口。');
const guardAt = body.indexOf('user_is_admin');
const grantAt = body.indexOf('.activate(code');
assert.ok(guardAt >= 0 && grantAt > guardAt, '超管校验必须在 store.activate() 之前（顺序反了等于没校验）。');
assert.ok(!/via="ui_test"/.test(body),
  'member_activate 不得再用 via="ui_test"（应写 admin_direct，ui_test 在购买记录里会被显示成「激活码」）。');

// ── ③ 两端共用同一份 membership.py：本仓任何改动都必须下发 hk ────────────────
// 这里做不了网络比对，改为记录「同步契约」的显式声明：改动 membership.py /
// routers/membership.py 时，发布清单里必须包含 hk 节点。
const deployNote = join(repoRoot, 'server', 'tests', 'DEPLOY_TARGETS.md');
assert.ok(readFileSync(deployNote, 'utf8').includes('47.82.101.79'),
  'server/tests/DEPLOY_TARGETS.md 必须写明 hk 节点 IP —— 漏了就会重演「以为只有一台机器」。');

// ── ④ 前端入口不得在任一部署面残留（纯静态：仓库侧已清理）──────────────────
const appJs = readFileSync(join(repoRoot, 'web', 'app.js'), 'utf8');
const indexHtml = readFileSync(join(repoRoot, 'web', 'index.html'), 'utf8');
const strip = (s) => s.split('\n').map(l => l.replace(/^\s*(\/\/|\/\*|\*).*$/, '')).join('\n');
for (const [f, s] of [['web/app.js', strip(appJs)], ['web/index.html', strip(indexHtml)]]) {
  assert.ok(!/pfActivateCode/.test(s) && !/pfActivateBtn/.test(s),
    `${f} 不应再有激活码入口（cn/hk 两端都已移除；仓库侧回退了要同步清）。`);
}

console.log('✅ cn/hk 双部署面同步守卫通过（activate 超管门禁在位 + 部署目标清单含 hk IP）');
