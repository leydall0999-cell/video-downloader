// 登录弹窗「定宽带宽 + 登录方式文案」回归守卫（2026-10-08 用户截图反馈）
//
// 症状：登录弹窗右侧大片留白（用户原话「对称问题改一下，右边空太多了，缩窄」）；
//       且登录方式文案显示为「邮箱 / 手机号」，用户指定改为「电话号码 / QQ邮箱」。
// 根因：`#authModal` 的 dialog 本体宽来自 `.modal`（560px），而内部 `.modal-card`
//       带了**内联** `style="max-width:400px"` ⇒ 卡片只占 400px、dialog 仍 560px，
//       右侧固定留白 160px，且这个留白宽度写死在 HTML 里、CSS 侧无从收紧。
// 修复：① 删掉 `.modal-card` 的内联 max-width（HTML 不再写样式）；
//       ② 改由 `#authModal` 本体定宽 400px ⇒ 卡片撑满、无留白，整体比 560 明显收窄。
//
// 本测试直接抽 web/index.html、web/styles.css 的**真源码**跑，逻辑被改回旧版立即红。
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const indexHtml = readFileSync(join(repoRoot, 'web', 'index.html'), 'utf8');
const stylesCss = readFileSync(join(repoRoot, 'web', 'styles.css'), 'utf8');

// ---- ① 切片取出 #authModal 整块 HTML ----
const dlgStart = indexHtml.indexOf('<dialog id="authModal"');
assert.ok(dlgStart > 0, '找不到 #authModal');
const dlgEnd = indexHtml.indexOf('</dialog>', dlgStart);
assert.ok(dlgEnd > dlgStart, '#authModal 切片右边界（</dialog>）缺失');
const dlgHtml = indexHtml.slice(dlgStart, dlgEnd);

// ①a 卡片不得再带内联宽度 —— 它就是「右侧留白」的来源，且让 CSS 收不住
assert.ok(!/class="modal-card"[^>]*style=/.test(dlgHtml),
  '登录弹窗的 .modal-card 不得再带内联 style（内联 max-width:400px 会让卡片比 dialog 窄、右侧留白）');
// ①b 弹窗内整体不应出现任何内联 width/max-width
assert.ok(!/style="[^"]*(max-width|width)\s*:/.test(dlgHtml),
  '登录弹窗内不得写内联 width/max-width，宽度一律由 styles.css 的 #authModal 决定');

// ---- ② CSS：弹窗本体定宽 400，且基础 .modal 宽度规则仍在 ----
assert.ok(/\.modal\s*\{[^}]*width:\s*min\(560px/.test(stylesCss),
  '.modal 的基础宽度规则（min(560px, …)）不应被顺手动掉 —— 其他弹窗还靠它');
const am = stylesCss.match(/#authModal\s*\{([^}]*)\}/);
assert.ok(am, 'styles.css 缺少 #authModal 定宽规则（登录弹窗宽度收敛的唯一真源）');
assert.ok(/max-width\s*:\s*400px/.test(am[1]),
  `#authModal 必须把宽度压到 400px（否则回落到 .modal 的 560px，右侧又会空出来），实际：${am[1].trim()}`);
assert.ok(/width\s*:\s*min\(\s*400px/.test(am[1]),
  `#authModal 的 width 应写成 min(400px, 100% - 2rem) 以兼容窄屏，实际：${am[1].trim()}`);

// ---- ③ 登录方式文案（用户指定口径：电话号码 / QQ邮箱 / Gmail邮箱）----
// 三处都挂这个口径：个人中心登录表单、重置密码表单、全局登录弹窗。
const IDENT_LABEL = '电话号码 / QQ邮箱 / Gmail邮箱';
const labelRe = new RegExp(IDENT_LABEL.replace(/\//g, '\\/'), 'g');
const labelCount = (indexHtml.match(labelRe) || []).length;
assert.ok(labelCount >= 2,
  `账号字段应显示「${IDENT_LABEL}」，实际命中 ${labelCount} 处`);
assert.ok(!/邮箱 \/ 手机号/.test(indexHtml),
  '旧的「邮箱 / 手机号」口径已被用户指定替换，不应残留（含重置密码表单的「账号（…）」标签）');
// 反回归：不得残留「未标注 Gmail」的旧文案（后面紧跟 " /" 的才是新文案）
assert.ok(!/电话号码 \/ QQ邮箱(?!\s*\/)/.test(indexHtml),
  '残留了未标注 Gmail 的旧文案「电话号码 / QQ邮箱」—— 用户这次就是要求补上 Gmail');

// 文案承诺的每一种账号类型，后端必须真的接受，否则就是虚假宣传。
// 做法：直接从 auth.py 抽出真正则（语法与 JS 兼容）在本地跑，属确定性纯函数验证。
const authPy = readFileSync(join(repoRoot, 'server', 'routers', 'auth.py'), 'utf8');
const emailReSrc = (authPy.match(/_EMAIL_RE = re\.compile\(r"([^"]+)"\)/) || [])[1];
assert.ok(emailReSrc, 'auth.py 里找不到 _EMAIL_RE 定义（文案里「邮箱」一栏的依据）');
const emailRe = new RegExp(emailReSrc);
for (const addr of ['user@qq.com', 'user@foxmail.com', 'user@gmail.com', 'user@googlemail.com']) {
  assert.ok(emailRe.test(addr),
    `后端 ${'_EMAIL_RE'} 不接受 ${addr}，但文案承诺了这种邮箱 —— 文案与能力不符`);
}
for (const bad of ['user@', '@gmail.com', 'user', 'user@gmail', 'user @gmail.com']) {
  assert.ok(!emailRe.test(bad), `后端不应把非法账号 ${JSON.stringify(bad)} 判为有效邮箱`);
}
assert.ok(/gmail\.com/i.test(authPy),
  'auth.py 的注释应说明支持 Gmail 邮箱（文案「Gmail邮箱」的依据）');
assert.ok(/_PHONE_RE = re\.compile\(r"\^1\[3-9\]\\d\{9\}\$"\)/.test(authPy),
  'auth.py 应支持 11 位手机号（文案「电话号码」的依据）');

console.log('✅ 登录弹窗定宽与登录方式文案回归通过（无内联宽度 / #authModal 400px / 电话号码·QQ邮箱·Gmail邮箱）');
