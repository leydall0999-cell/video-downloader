// 网页版「会员」购买页（#memberView）回归守卫（2026-09-30）
//
// 背景（用户 2026-09-30 反馈「网页版是不是没有会员购买界面」）：
//   网页版此前**根本没有会员购买界面** —— 唯一的充值卡（#pfPlans）埋在「个人中心 → 个人资料」里，
//   而个人中心对未登录用户只渲染登录表单（pfRender: el.pfAuthBox.hidden = logged），
//   于是匿名访客把整站翻遍也看不到任何价格与购买入口（实测：可读文本仅 35 字，hasPayEntry=false）。
//
// 本页的三条设计红线，改坏即红：
//   ① 免登录必须能看到全部价目与权益（价格只能来自 /api/member/plans，前端不许存死数）；
//   ② 「立即开通」才要求登录 —— 未登录先弹登录框，登录成功后**自动续下单**（两条登录路径都要接）；
//   ③ 会员页与个人中心共用同一个扫码弹窗，所以 #payModal 必须在顶层（不能留在 pfUserBox 里，
//      否则未登录态下它连渲染上下文都没有）。
//
// 说明：不做 DOM 运行时（该页依赖整页初始化 + 后端），用「源码切片 + 结构断言」，锚点被挪走立即红。
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const indexHtml = readFileSync(join(repoRoot, 'web', 'index.html'), 'utf8');
const appJs = readFileSync(join(repoRoot, 'web', 'app.js'), 'utf8');
const stylesCss = readFileSync(join(repoRoot, 'web', 'styles.css'), 'utf8');

// ---- ① 页面本体 ----
const start = indexHtml.indexOf('id="memberView"');
assert.ok(start > 0, '#memberView 必须存在（网页版会员购买页）');
const endIdx = indexHtml.indexOf('id="profileView"', start);
assert.ok(endIdx > start, '#memberView 应紧邻在 #profileView 之前（切片右边界）');
const mem = indexHtml.slice(start, endIdx);

assert.ok(/<section id="memberView" class="panel" hidden aria-labelledby="memTitle">/.test(indexHtml),
  '#memberView 必须是 hidden 的 .panel（靠 switchView 显示）');
for (const id of ['memTracks', 'memTop', 'memNote', 'memStatus', 'memSeg']) {
  assert.ok(mem.includes(`id="${id}"`), `会员页缺少容器 #${id}`);
}
assert.ok(/<div class="mem-tracks" id="memTracks"><p class="pf-plan-empty">加载中…<\/p><\/div>/.test(mem),
  '#memTracks 应有「加载中…」静态首屏（避免白屏）');

// ---- ①b 三轨「胶囊分段条」（2026-09-30 用户指定版式）：一次只显示一条轨 ----
const seg = mem.slice(mem.indexOf('id="memSeg"'), mem.indexOf('id="memTracks"'));
for (const t of ['download', 'ai', 'credits']) {
  assert.ok(seg.includes(`data-memtrack="${t}"`), `分段条缺少 data-memtrack="${t}" 按钮`);
}
assert.ok(/class="mem-seg-btn is-active"/.test(seg), '分段条默认应高亮「下载会员」');
assert.ok(appJs.includes("memSeg: $('memSeg')"), 'app.js 的 el 表缺少 memSeg 引用');
assert.ok(/const memRenderTrack = \(\) => \{/.test(appJs), '缺少按轨渲染函数 memRenderTrack');
assert.ok(/const memSyncSeg = \(\) => \{/.test(appJs), '缺少分段条状态同步 memSyncSeg');
assert.ok(/el\.memSeg\.querySelectorAll\('\.mem-seg-btn'\)\.forEach\(\(b\) => \{\s*b\.addEventListener\('click'/.test(appJs),
  '分段按钮没有绑定点击切换（点了没反应）');
assert.ok(/_memTrack = key;/.test(appJs), '分段切换必须更新 _memTrack，否则永远显示同一条轨');
assert.ok(/b\.hidden = !memTrackHasPlans\(key\);/.test(appJs),
  '后端没配的轨必须在分段条里隐藏（不许出现点了空白的假入口）');

// ---- ② 导航入口：必须在个人中心之前，且带分组分隔 ----
assert.ok(/<button type="button" class="tab tab-sep" data-view="member" id="tabMember">/.test(indexHtml),
  '导航缺少「💎 会员」tab（data-view="member"，带 tab-sep 分组线）');
const iMember = indexHtml.indexOf('id="tabMember"');
const iProfile = indexHtml.indexOf('id="tabProfile"');
assert.ok(iMember > 0 && iProfile > 0 && iMember < iProfile,
  '「会员」tab 应排在「个人中心」之前（会员属于账号簇，且在未登录时仍要能进）');

// ---- ③ 三处接线（web 视图缺一不可：el 引用 / switchView / 点击监听 + tabs 显示清单）----
assert.ok(appJs.includes("tabMember: $('tabMember')") && appJs.includes("memberView: $('memberView')"),
  'app.js 的 el 表缺少 tabMember / memberView 引用');
assert.ok(appJs.includes("memTracks: $('memTracks')"), 'app.js 的 el 表缺少 memTracks 引用');

const svStart = appJs.indexOf('function switchView(view) {');
assert.ok(svStart > 0, '找不到 switchView');
// 切片到 switchView 结尾（以「离开时停表」那行收口），避免写死长度后被后续新增行截断
const svEnd = appJs.indexOf('else stopTorPoll();', svStart);
assert.ok(svEnd > svStart, 'switchView 切片右边界（stopTorPoll）不存在');
const sv = appJs.slice(svStart, svEnd);
assert.ok(/const isMem = view === 'member'/.test(sv), 'switchView 缺少 isMem 分支');
assert.ok(/const isAnyExtra = [^;]*\|\| isMem/.test(sv),
  'isMem 必须并入 isAnyExtra，否则下载视图不会让位给会员页（两页同屏叠着）');
assert.ok(/el\.memberView\.hidden = !isMem/.test(sv), 'switchView 未切换 #memberView 显隐');
assert.ok(/el\.tabMember\.classList\.toggle\('is-active', isMem\)/.test(sv), 'switchView 未切换会员 tab 高亮');
assert.ok(/if \(isMem\) memRender\(\);/.test(sv), 'switchView 未在进入会员页时拉取价目');
// 🔴 启动竞态保护（2026-09-30 真机实测）：boot 的 .then 里若无条件 switchView('download')，
//    会把用户在加载窗口期点开的「会员」弹回下载视图。必须先判断已有非默认视图选中。
assert.ok(/if \(!document\.querySelector\('\.tab\.is-active:not\(#tabDownload\)'\)\)/.test(appJs),
  'boot 默认视图必须带「用户已选中非默认视图则不覆盖」保护，否则会员页会被启动竞态弹回下载');
// 2026-10-01 放宽：点击处理里允许附带埋点等额外语句（箭头函数可有花括号块），
// 只要仍然调用 switchView('member') 即视为已接线 —— 守卫本意是「点击能切到会员页」。
assert.ok(/if \(el\.tabMember\) el\.tabMember\.addEventListener\('click', \(\) => \{?\s*switchView\('member'\)/.test(appJs),
  '会员 tab 没有绑定 switchView（点击无反应）');
assert.ok(/if \(el\.tabMember\) el\.tabMember\.hidden = false;/.test(appJs),
  '网页版 tabs 显示清单未放行 tabMember');
// #view=member 深链依赖 switchView('member') 本身即可，无需额外接线（提醒：改动此处请一并回归深链）

// ---- ④ 价格单一真源：只许来自后端，前端不得出现任何写死价格 ----
const mrStart = appJs.indexOf('const memRender = async () => {');
assert.ok(mrStart > 0, '找不到 memRender');
const mr = appJs.slice(mrStart, appJs.indexOf('const memBuy =', mrStart));
assert.ok(mr.includes("request('/api/member/plans')"), '会员页必须从 /api/member/plans 取价目');
for (const track of ['download_member', 'ai_member', 'credit_packs']) {
  assert.ok(mr.includes(track), `会员页未渲染「${track}」分组（三轨会员要齐）`);
}
// 🔴 契约差异（2026-09-30 真浏览器实测抓到）：credit_packs 本身就是档位表（无 .plans 嵌套），
//    直接按 {plans,benefits} 统一读会把积分包判空 → 分段条里积分包 0 卡。必须归一化。
assert.ok(/return \{ plans: p\.credit_packs \|\| null \};/.test(mr),
  'credit_packs 必须归一化为 { plans: … }（它没有 .plans 嵌套，直接 Object.entries）');
assert.ok(/Number\(pl\.price_cny\)/.test(appJs), '价格必须从 plan.price_cny 读取');
assert.ok(!/(price_cny|amount)\s*:\s*\d/.test(appJs), '前端不得写死价格（首价必须由超管后台配置）');
const cardStart = appJs.indexOf('const memPlanCard = (');
const card = appJs.slice(cardStart, appJs.indexOf('const memRenderTop', cardStart));
assert.ok(card.includes('pf-plan-buy') && card.includes('data-code='),
  '套餐卡必须带「立即开通」按钮与 data-code，否则无法下单');
assert.ok(!/\d+\.\d{2}/.test(card), '套餐卡模板里出现了写死的两位小数价格');

// ---- ⑤ 登录门禁 + 登录后自动续下单 ----
const mbStart = appJs.indexOf('const memBuy = (');
assert.ok(mbStart > 0, '找不到 memBuy');
const mb = appJs.slice(mbStart, appJs.indexOf('_memResumePending', mbStart));
assert.ok(/if \(!pfToken\(\)\)/.test(mb), 'memBuy 必须先判登录态');
assert.ok(/openAuthModal\(/.test(mb), '未登录点开通必须弹登录框');
assert.ok(/_memPendingCode = code/.test(mb), '未登录时要把想买的档位记下来（否则登录后丢单）');
assert.ok(/pfBuy\(code, el\.memStatus\)/.test(mb), '已登录应复用 pfBuy 下单，并把反馈写到会员页提示区');
const resumeCalls = (appJs.match(/_memResumePending\(\);/g) || []).length;
assert.ok(resumeCalls >= 2, `登录成功后自动续下单要接在两处登录路径（会员页登录框 / 顶栏登录弹窗），实际 ${resumeCalls} 处`);

// ---- ⑤b 未接通道时的诚实降级（离线 / 未接真通道时后端不返回 qr_png）----
// 契约事实：真通道（网页版后端已转发 VPS 支付服务，2026-10-09）返回 qr_png + qr；
// 离线 / 未接通道时两者都缺。早期实现写的是 `el.payQr.src = r.qr_png || ''` ——
// 于是每个人点开通都是「裂图 + 永久等待支付」。
assert.ok(/const hasQr = !!r\.qr_png;/.test(appJs), 'pfBuy 必须显式判断 qr_png 是否存在');
assert.ok(/if \(!hasQr\) return;/.test(appJs), '没有二维码时必须就此返回（不空转轮询）');
assert.ok(/el\.payQr\.hidden = true;/.test(appJs) && /el\.payQr\.removeAttribute\('src'\)/.test(appJs),
  '无二维码时应清空并隐藏 <img>，而不是留一个空 src 的裂图');
assert.ok(appJs.includes('支付通道尚未开通'), '无二维码时要明确告知「通道未开通」，别让用户对着空白干等');
// 标题与扫码提示也要跟着切：否则「通道未开通」的弹窗上还挂着「扫码支付」「请扫码付款」，自相矛盾
assert.ok(indexHtml.includes('id="payTip"'), '扫码提示缺少 #payTip（无法按状态切换）');
assert.ok(/el\.payModalTitle\.textContent = hasQr \? '扫码支付开通会员' : '订单已创建';/.test(appJs),
  '无二维码时弹窗标题应改为「订单已创建」，不要继续写「扫码支付」');
assert.ok(/el\.payTip\.hidden = !hasQr;/.test(appJs), '无二维码时应隐藏「请扫码付款」提示（文案已按通道动态切换，不再写死支付宝）');

// ---- ⑥ 扫码弹窗必须在顶层：否则未登录态下它落在 hidden 的 pfUserBox 里 ----
const nPay = (indexHtml.match(/id="payModal"/g) || []).length;
assert.equal(nPay, 1, `#payModal 只应存在一处，实际 ${nPay} 处`);
assert.ok(indexHtml.indexOf('id="payModal"') > indexHtml.indexOf('</main>'),
  '#payModal 必须提到 </main> 之后（会员页与个人中心共用；留在 pfUserBox 内会导致未登录态取不到）');

// ---- ⑦ 样式：宽屏 3 列（880px 正文宽下 6 档 = 3×2，与桌面端会员中心同版式）----
for (const sel of ['.mem-lead {', '.mem-top {', '.mem-track-h {', '.mem-benefits li::before {',
  '.mem-tracks .pf-plans {', '.mem-tracks .pf-plan-buy {',
  '.mem-seg {', '.mem-seg-btn.is-active {']) {
  assert.ok(stylesCss.includes(sel), `styles.css 缺少 ${sel}`);
}
assert.ok(!stylesCss.includes('.mem-track-t {'), '.mem-track-t 已被分段条取代，死规则应删除');
assert.ok(/\.mem-tracks \.pf-plans \{ grid-template-columns: repeat\(auto-fit, minmax\(240px, 1fr\)\);/.test(stylesCss),
  '会员页栅格应覆盖为 minmax(240px,1fr)（880px 下 3 列；小于此值会自动收列）');
assert.ok(/@media \(max-width: 560px\) \{\s*\.mem-tracks \.pf-plans \{ grid-template-columns: 1fr; \}/.test(stylesCss),
  '窄屏（≤560px）应退化为单列，否则卡片被挤到 240px 以下');

// ---- ⑧ 个人中心里原有的充值卡不许被顺手删掉（两处入口都要在）----
assert.ok(indexHtml.includes('id="pfPlans"'), '个人中心「开通 / 续费下载会员」卡不该被移除');
assert.ok(appJs.includes('try { pfRenderPlans(); }'), 'pfRenderPlans 接线被移除（个人中心充值卡会变空白）');

console.log('✅ 会员购买页回归守卫通过：免登录可见价目 / 三轨齐备 / 登录门禁与自动续单 / 弹窗在顶层 / 3 列栅格');
