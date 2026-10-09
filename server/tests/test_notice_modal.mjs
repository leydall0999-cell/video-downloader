// 「下单失败 / 活动限购」改用弹窗 回归守卫（2026-10-09 用户截图反馈）
//
// 症状（用户原话「该套餐每个账号限购 1 次，你已购买 1 次。改用弹窗」）：
//   在会员页点「VIP会员·1天」的「立即开通」，页面**最底部**出现一行小字
//   「❌ 该套餐每个账号限购 1 次，你已购买 1 次」—— 位置在价目卡片最下方、随滚动位置
//   时隐时现，用户容易当成「点了没反应」而反复重复点击下单按钮。
// 根因：`pfBuy()` 只把失败文案 `say()` 写进状态行（#memStatus / #pfMemberStatus），
//       没有任何需要用户确认的强提示。
// 修复：新增通用提示弹窗 `#noticeModal` + `showNotice(title, body, hint)`，
//       下单失败的两条路径（下单前被拒 / 轮询到 LIMIT_REJECTED·GRANT_FAILED）全部改走弹窗。
//
// 本测试抽 web/index.html、web/styles.css、web/app.js 的**真源码**做结构断言，
// 任何一处被改回「只写状态行」立即变红。
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const indexHtml = readFileSync(join(repoRoot, 'web', 'index.html'), 'utf8');
const stylesCss = readFileSync(join(repoRoot, 'web', 'styles.css'), 'utf8');
const appJs = readFileSync(join(repoRoot, 'web', 'app.js'), 'utf8');

// ---- ① index.html：弹窗结构必须齐（标题/正文/补充说明/两个关闭途径）----
const dlgStart = indexHtml.indexOf('<dialog id="noticeModal"');
assert.ok(dlgStart > 0, '找不到 #noticeModal —— 通用提示弹窗被删了');
const dlgEnd = indexHtml.indexOf('</dialog>', dlgStart);
assert.ok(dlgEnd > dlgStart, '#noticeModal 切片右边界（</dialog>）缺失');
const dlgHtml = indexHtml.slice(dlgStart, dlgEnd);
for (const [what, re] of [
  ['标题 #noticeTitle', /<h2 id="noticeTitle">/],
  ['正文 #noticeBody', /id="noticeBody"/],
  ['补充说明 #noticeHint', /id="noticeHint"[^>]*hidden/],
  ['关闭按钮 #noticeClose', /id="noticeClose"/],
  ['确认按钮 #noticeOk', /id="noticeOk"/],
  ['无障碍标签 aria-labelledby', /aria-labelledby="noticeTitle"/],
]) {
  assert.ok(re.test(dlgHtml), `#noticeModal 缺少${what}`);
}
// 提示弹窗是顶层 dialog，不得嵌套在别的 dialog 里（嵌套会强制进 top layer 两次、关不掉）
assert.equal(indexHtml.split('<dialog id="noticeModal"').length - 1, 1,
  '#noticeModal 必须唯一');

// ---- ② styles.css：正文/说明样式存在（否则正文用 .modal-sub 的 .8rem 小字，太弱）----
assert.ok(/\.notice-text\s*\{/.test(stylesCss), 'styles.css 缺少 .notice-text（弹窗正文样式）');
assert.ok(/\.notice-hint\s*\{/.test(stylesCss), 'styles.css 缺少 .notice-hint（弹窗补充说明样式）');
assert.ok(/\.modal\s*\{[^}]*width:\s*min\(560px/.test(stylesCss),
  '.modal 基础宽度规则不应被顺手动掉 —— 其他弹窗还靠它');

// ---- ③ app.js：el 表登记 + showNotice 实现 ----
assert.ok(/noticeModal:\s*\$\('noticeModal'\)/.test(appJs),
  'el 表未登记 noticeModal（showNotice 会拿不到节点，静默降级成 toast）');

const fnStart = appJs.indexOf('const showNotice = (title, body, hint) => {');
assert.ok(fnStart > 0, '找不到 showNotice 实现');
const fnEnd = appJs.indexOf('\n  };', fnStart);
assert.ok(fnEnd > fnStart, 'showNotice 切片右边界缺失');
const fnBody = appJs.slice(fnStart, fnEnd);
assert.ok(/el\.noticeTitle\.textContent\s*=/.test(fnBody), 'showNotice 必须写标题');
assert.ok(/el\.noticeBody\.textContent\s*=/.test(fnBody), 'showNotice 必须写正文');
assert.ok(/el\.noticeHint\.hidden\s*=/.test(fnBody), 'showNotice 必须按有无补充说明切换 #noticeHint 显隐');
// 降级链：不支持 <dialog>.showModal 时必须回落 showToast —— 宁可弱一点，也不能静默丢消息
assert.ok(/typeof dlg\.showModal !== 'function'/.test(fnBody) && /showToast\(/.test(fnBody),
  'showNotice 缺少 <dialog> 不可用时的 showToast 降级（会静默丢消息）');
// 已打开则只换内容：重复 showModal() 会抛 InvalidStateError 把后面的代码打断
assert.ok(/if\s*\(!dlg\.open\)/.test(fnBody),
  'showNotice 必须先判 dlg.open 再 showModal（重复 showModal 会抛错）');
// 关闭途径：关闭按钮 + 确认按钮 + 点遮罩（Esc 由 dialog 原生处理）
assert.ok(/el\.noticeClose\.addEventListener\('click', closeNotice\)/.test(appJs), '关闭按钮未接 closeNotice');
assert.ok(/el\.noticeOk\.addEventListener\('click', closeNotice\)/.test(appJs), '确认按钮未接 closeNotice');
assert.ok(/e\.target === el\.noticeModal\)\s*closeNotice\(\)/.test(appJs), '点遮罩未接 closeNotice');

// ---- ④ pfBuy：两条失败路径必须走弹窗，且不得再往状态行写 ❌ ----
const pfStart = appJs.indexOf('const pfBuy = async (code, statusEl) => {');
assert.ok(pfStart > 0, '找不到 pfBuy');
const pfEnd = appJs.indexOf('if (el.payModalClose) el.payModalClose.addEventListener', pfStart);
assert.ok(pfEnd > pfStart, 'pfBuy 切片右边界缺失');
const pf = appJs.slice(pfStart, pfEnd);

// ④a 老写法（写状态行）必须彻底消失 —— 它就是「看不见的提示」的来源
assert.ok(!/say\('❌/.test(pf),
  'pfBuy 不得再把 ❌ 文案写进状态行（用户滚动不到就看不见，会当成点了没反应）');

// ④b 下单前被拒：活动限购要单独识别（code=LIMIT_REACHED，收款前拦截、未产生扣款）
assert.ok(/\(\(r && r\.code\) \|\| ''\) === 'LIMIT_REACHED'/.test(pf),
  'pfBuy 未按 code=LIMIT_REACHED 区分「活动限购」（它是收款前拦截，与通用失败要分开说）');
assert.ok(/showNotice\('该套餐已限购'/.test(pf),
  '「该套餐每个账号限购 N 次」必须以弹窗标题「该套餐已限购」呈现');
assert.ok(/showNotice\('下单失败'/.test(pf), '通用下单失败必须走弹窗');
assert.ok(/网络错误/.test(pf) && /showNotice\(/.test(pf.slice(pf.indexOf('网络错误') - 260, pf.indexOf('网络错误') + 60)),
  '网络异常分支也必须走弹窗（原先只写状态行）');

// ④c 轮询到已扣款状态：必须先关支付弹窗再开提示弹窗，否则两个 dialog 叠在 top layer
const rejPos = pf.indexOf("} else if (q && q.ok && (q.status === 'LIMIT_REJECTED'");
assert.ok(rejPos > 0, '找不到轮询的 LIMIT_REJECTED / GRANT_FAILED 分支');
const rejEnd = pf.indexOf('} catch (_e) { /* 轮询失败静默重试 */ }', rejPos);
assert.ok(rejEnd > rejPos, '轮询分支切片右边界缺失');
const rejBlock = pf.slice(rejPos, rejEnd);
const iClose = rejBlock.indexOf('pfClosePayModal()');
const iNotice = rejBlock.indexOf('showNotice(');
assert.ok(iClose >= 0, '轮询到 LIMIT_REJECTED / GRANT_FAILED 时未关闭支付弹窗');
assert.ok(iNotice >= 0, '轮询到 LIMIT_REJECTED / GRANT_FAILED 时未弹出提示');
assert.ok(iClose < iNotice, '必须先 pfClosePayModal() 再 showNotice()（顺序反了两个弹窗会叠着）');
assert.ok(/showNotice\('该套餐已限购，本单将退款'/.test(rejBlock),
  'LIMIT_REJECTED（已扣款待退款）必须有独立弹窗文案');
assert.ok(/showNotice\('已付款但开通失败'/.test(rejBlock),
  'GRANT_FAILED（已付款发货故障）必须有独立弹窗文案');

// ---- ⑤ 支付成功那条不得被顺手改成弹窗（成功用 toast/状态行即可，不打断用户）----
assert.ok(/say\('✅ 支付成功，会员已开通'\)/.test(pf),
  '支付成功仍走轻提示；本次只改失败路径，别把成功路径也弹窗化');

console.log('✅ 下单失败/活动限购弹窗回归通过（#noticeModal 结构齐 / showNotice 有降级 / pfBuy 无 ❌ 状态行 / 先关支付弹窗再提示）');
