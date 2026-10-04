// 网页端「激活码 / 卡密」通道收口回归（2026-10-04）
//
// 背景（三层事实，任何一层被改回去都会重新产生白嫖/坏体验）：
//   ① 授权中心的真卡密通道已下线（`/opt/vdl-license` drop-in redeem-off.conf 设
//      VDL_REDEEM_DISABLED=1，POST /api/license/redeem 返 410）⇒ 用户输入真卡密毫无意义；
//   ② 网页版 `/api/member/activate` 从来不做签名校验，只是把用户输入的字符串当
//      **套餐 code** 直接本地发放（`download_year` / `credits_5000` …）
//      ⇒ 任意登录用户一条 curl 就能本地加会员或永久积分；
//   ③ 但云端 `authority` 快照是覆盖式（membership.apply_cloud_authoritative），
//      下一次 /api/member/status（节流 15s）就把本地发放回滚
//      ⇒ 用户看到「激活成功 ✅」，15 秒后又变回免费。
// 桌面端已于 2026-09-25 收口为「仅超级管理员」（见 video-downloader-app 同一路由），
// 本次把网页端补齐，并移除前端入口。
//
// 本测试钉住：
//   ① 后端必须先判 user_is_admin 再发放，且 via 用 admin_direct（不再是 ui_test）；
//   ② 前端不得再出现 pfActivateCode / pfActivateBtn / member/activate 调用；
//   ③ 状态位 #pfMemberStatus 必须还在（头像上传等仍在用，删了会 NPE）。
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const routerPy = readFileSync(join(repoRoot, 'server', 'routers', 'membership.py'), 'utf8');
const indexHtml = readFileSync(join(repoRoot, 'web', 'index.html'), 'utf8');
const appJs = readFileSync(join(repoRoot, 'web', 'app.js'), 'utf8');

// ── ① 后端门禁 ────────────────────────────────────────────────────────────
const fnStart = routerPy.indexOf('def member_activate(');
assert.ok(fnStart > 0, 'server/routers/membership.py 必须仍有 member_activate 路由（管理员补单通道）');
// 用下一个顶层装饰器/函数做右边界，避免跨函数误判
const fnEnd = routerPy.indexOf('\n@router.', fnStart + 10);
const body = routerPy.slice(fnStart, fnEnd > 0 ? fnEnd : undefined);

assert.ok(/user_is_admin\s*\(\s*uid\s*\)/.test(body),
  'member_activate 必须调用 auth_store.user_is_admin(uid) 做超管校验 —— '
  + '漏掉它等于恢复「一条 curl 白嫖任意套餐」的后门。');
assert.ok(/from auth_store import user_is_admin/.test(body),
  'user_is_admin 必须在函数体内 import（避免模块级循环依赖）。');

const adminIdx = body.indexOf('user_is_admin');
const activateIdx = body.indexOf('.activate(code');
assert.ok(adminIdx >= 0 && activateIdx > adminIdx,
  '超管校验必须发生在 store.activate() **之前**（顺序反了等于没校验）。');

assert.ok(/via="admin_direct"/.test(body),
  '发放 via 应为 admin_direct（不再是 ui_test）——ui_test 在购买记录里会显示成「激活码」，'
  + '让管理员补单看起来像用户自己兑的。');
assert.ok(!/via="ui_test"/.test(body),
  'member_activate 不得再使用 via="ui_test"（该 via 已随前端入口一起下线）。');

// 非超管必须返回可识别的错误码，供前端/后台判断
assert.ok(/USE_REDEEM/.test(body),
  '非超管分支应返回 code=USE_REDEEM（与桌面端契约一致，后台/日志据此区分补单与用户尝试）。');

// ── ② 前端入口已移除 ──────────────────────────────────────────────────────
// 注：注释里保留这些名字作为「为何移除」的说明，所以比对前先剥掉整行注释，
//     否则守卫会被自己写的说明文字绊倒（同样口径适用于 index.html）。
const stripLineComments = (s) => s.split('\n')
  .map(l => l.replace(/^\s*(\/\/|\/\*|\*).*$/, ''))       // 整行注释
  .join('\n');
const indexCode = stripLineComments(indexHtml);
const appCode = stripLineComments(appJs);

for (const [file, src] of [['web/index.html', indexCode], ['web/app.js', appCode]]) {
  assert.ok(!/pfActivateCode/.test(src), `${file} 不应再出现 pfActivateCode（激活码输入框已移除）。`);
  assert.ok(!/pfActivateBtn/.test(src), `${file} 不应再出现 pfActivateBtn（激活按钮已移除）。`);
}
assert.ok(!/\/api\/member\/activate/.test(appCode),
  'web/app.js 不应再调用 /api/member/activate（普通用户走在线支付；超管补单走后台）。');
assert.ok(!/id="pfActivateCode"/.test(indexCode),
  'web/index.html 不应再有 id="pfActivateCode" 的输入框。');

// ── ③ 状态位必须保留 ──────────────────────────────────────────────────────
assert.ok(/id="pfMemberStatus"/.test(indexHtml),
  '#pfMemberStatus 必须保留：头像上传、账号安全等仍在写它（连同本卡一起删会 NPE）。');
assert.ok(/pfMemberStatus:\s*\$\('pfMemberStatus'\)/.test(appJs),
  'app.js 的 el 表里必须仍映射 pfMemberStatus。');

// ── ④ 桌面端口径一致性：两端必须同为「仅超管」 ─────────────────────────────
//   网页端曾在 2026-09-25 漏改，这里静态比对避免再次漂移（app 仓不在 web 仓内则跳过）。
import { existsSync } from 'node:fs';
const appRouter = join(repoRoot, '..', 'video-downloader-app', 'server', 'routers', 'membership.py');
if (existsSync(appRouter)) {
  const appPy = readFileSync(appRouter, 'utf8');
  assert.ok(/user_is_admin\s*\(\s*uid\s*\)/.test(appPy),
    '桌面端 router 应同样带 user_is_admin 校验；若桌面端改了这里也要同步复核。');
}

console.log('✅ 激活码通道收口回归通过（后端仅超管 + 前端入口已移除 + 状态位保留 + 两端口径一致）');
