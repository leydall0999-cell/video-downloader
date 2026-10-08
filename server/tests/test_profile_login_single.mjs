// 个人中心未登录态「同屏只能有一套登录 UI」回归守卫（2026-10-08 用户截图反馈）
//
// 症状：在「个人中心」未登录状态下点右上角「登录 / 注册」，全局登录弹窗 #authModal 会叠在
//       页内登录表单 #pfAuthBox 上 —— 同屏两个登录框（用户截图把被压在弹窗后面的那个圈了出来）。
// 根因：两套独立登录 UI 各自独立打开，openAuthModal() 不检查「当前是不是已经在个人中心」。
// 修复（三条合起来才闭环）：
//   ① openAuthModal()「页内表单优先」——已在个人中心且未登录时只聚焦页内表单、不叠弹窗，
//      并把拦截原因写进页内提示行 #pfAuthHint（否则用户不知道为什么要登录）；
//   ② switchView('profile') 进入时**即时**关掉残留弹窗（避免闪一下）；
//   ③ pfRender 未登录分支再兜底关一次（覆盖 pfLoad 异步期间新开的）；再加 css `.pf-auth-card` 居中。
//
// 本测试直接抽 web/app.js 的**真源码**跑（不是抄一份副本），逻辑被改回旧版立即红。
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const appJs = readFileSync(join(repoRoot, 'web', 'app.js'), 'utf8');
const indexHtml = readFileSync(join(repoRoot, 'web', 'index.html'), 'utf8');
const stylesCss = readFileSync(join(repoRoot, 'web', 'styles.css'), 'utf8');

// ---- ① 抽真源码：pfAuthInlineShown + openAuthModal ----
const s = appJs.indexOf('const pfAuthInlineShown = ');
assert.ok(s > 0, '找不到 pfAuthInlineShown（「页内表单优先」的判据函数）');
const e = appJs.indexOf('// request() 定义早于本段', s);
assert.ok(e > s, '找不到 openAuthModal 之后的锚点注释（切片右边界被挪走了）');
const src = appJs.slice(s, e);
assert.ok(/showModal\(\)/.test(src) && /pfAuthInlineShown/.test(src), '切片内容不像 openAuthModal');

const build = (el, renderAuthHeader) => new Function('el', 'renderAuthHeader',
  src + '\nreturn { openAuthModal, pfAuthInlineShown };')(el, renderAuthHeader);

const mk = (profileVisible, pfAuthVisible) => {
  const calls = { focus: 0, showModal: 0, header: 0, scroll: 0 };
  const el = {
    profileView: { hidden: !profileVisible, scrollIntoView() { calls.scroll++; } },
    pfAuthBox: { hidden: !pfAuthVisible },
    pfAuthHint: { textContent: 'DEFAULT_HINT' },
    pfIdentifier: { focus() { calls.focus++; } },
    amStatus: { textContent: 'stale' },
    authModalHint: { textContent: 'MODAL_DEFAULT' },
    authModal: { showModal() { calls.showModal++; }, close() { calls.close = (calls.close || 0) + 1; }, open: false },
  };
  return { el, calls, api: build(el, () => { calls.header++; }) };
};

// ①a 在个人中心 + 页内表单可见 → 绝不叠弹窗；原因写进页内提示行、焦点给输入框
{
  const { el, calls, api } = mk(true, true);
  api.openAuthModal('请先登录或注册账号，即可使用批量下载');
  assert.equal(calls.showModal, 0,
    '个人中心未登录时 openAuthModal 不得再叠一层全局登录弹窗 —— 这正是用户截图里的两个登录框');
  assert.equal(el.authModalHint.textContent, 'MODAL_DEFAULT', '不该改弹窗里的提示（说明走了弹窗分支）');
  assert.equal(el.pfAuthHint.textContent, '请先登录或注册账号，即可使用批量下载',
    '拦截原因必须写进页内提示行，否则用户不知道为什么要登录');
  assert.equal(calls.focus, 1, '应把焦点给页内登录表单的输入框');
  assert.equal(calls.header, 0, '没开弹窗就不该走 renderAuthHeader');
  assert.ok(calls.scroll >= 1, '应把页内登录表单滚进视野');
}
// ①b 不在个人中心（下载页 / 会员页 …）→ 照旧弹窗，原有拦截体验不许被这次改动弄坏
{
  const { el, calls, api } = mk(false, true);
  api.openAuthModal('下载需要登录账号；免费账号每日 10 次下载额度，注册即得。');
  assert.equal(calls.showModal, 1, '其他地方（如下载页）点下载被拦时，登录弹窗必须照旧弹出');
  assert.equal(el.authModalHint.textContent, '下载需要登录账号；免费账号每日 10 次下载额度，注册即得。',
    '弹窗提示文案要随拦截原因更新');
  assert.equal(el.pfAuthHint.textContent, 'DEFAULT_HINT', '不该改页内提示行');
  assert.equal(calls.focus, 0, '不该去聚焦页内表单');
  assert.equal(calls.header, 1, '开弹窗后应刷新右上角账号按钮文案');
}
// ①c 在个人中心但页内表单不可见（已登录 / 尚未渲染）→ 保守走弹窗（fail-open，不静默吞掉登录提示）
{
  const { calls, api } = mk(true, false);
  api.openAuthModal('x');
  assert.equal(calls.showModal, 1, '页内表单不可见时应保守弹窗，不能静默什么都不做');
}

// ---- ② 另外两道闸：进个人中心即时关、未登录渲染兜底关 ----
const sv = appJs.slice(appJs.indexOf('function switchView(view) {'), appJs.indexOf('if (isMem) memRender();'));
assert.ok(/if \(isProfile\) \{[\s\S]*?el\.authModal\.close\(\)[\s\S]*?pfLoad\(\);/.test(sv),
  'switchView 进入个人中心时必须先关掉残留的全局登录弹窗，再 pfLoad()');
const pr = appJs.slice(appJs.indexOf('const pfRender = (me, member, prof) => {'), appJs.indexOf('// 总览头'));
assert.ok(/if \(!logged\) \{[\s\S]*?el\.authModal\.close\(\)[\s\S]*?return;/.test(pr),
  'pfRender 未登录分支必须兜底关掉全局登录弹窗（覆盖 pfLoad 异步期间新开的）');

// ---- ③ 页内表单本体 + 提示行 + 居中样式接线 ----
assert.ok(/<div class="uc-bulk pf-auth-card">/.test(indexHtml), '页内登录卡应带 pf-auth-card');
assert.ok(/<p class="uc-tip" id="pfAuthHint">/.test(indexHtml), '页内登录卡缺少提示行 #pfAuthHint');
assert.ok(appJs.includes("pfAuthHint: $('pfAuthHint')"), 'app.js 的 el 表缺少 pfAuthHint 引用');
const card = stylesCss.match(/\.pf-auth-card\s*\{([^}]*)\}/);
assert.ok(card, 'styles.css 缺少 .pf-auth-card（登录卡居中）');
assert.ok(/margin-inline\s*:\s*auto/.test(card[1]),
  `.pf-auth-card 必须用 margin-inline: auto 居中（margin 简写会重置 .uc-bulk 的 margin-bottom），实际：${card[1].trim()}`);

console.log('✅ 个人中心登录 UI 单一性回归通过（页内表单优先 / 进页即关 / 未登录兜底关）');
