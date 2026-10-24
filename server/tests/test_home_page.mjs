// 网页版「首页」回归守卫（2026-10-24 新增功能简介页）
//
// 背景：用户要求把 hanyuxz.top 的默认落地页从「下载视频」改成独立的**功能简介首页**
//   （优先级：① 视频自动解说 ② 视频下载 ③ 百度网盘下载），且「下载视频」页必须原样不动。
//   参考 downie.cn / gopeed.com 的信息层级。
//
// 改坏即红的四条红线：
//   ① 首页是独立 section（#homeView），不与 #downloadView 混排、不改变下载页结构；
//   ② 默认落地 = 首页（tabHome 排第一且 is-active），深链 #view=x 仍可用；
//   ③ 首页用到的每个 hp-* 类都必须在 styles.css 有规则 —— 否则是「白板页」（无样式的裸 HTML）；
//   ④ 用户点名的文案一字不改：高速下载（不是满速）、体验会员级别网速、
//      「想体验全部功能请到桌面版」必须是大字提示（hp-hint）且「桌面版」加粗。
//
// 说明：与 test_member_page.mjs 同法——源码切片 + 结构断言，不做 DOM 运行时。
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const indexHtml = readFileSync(join(repoRoot, 'web', 'index.html'), 'utf8');
const appJs = readFileSync(join(repoRoot, 'web', 'app.js'), 'utf8');
const stylesCss = readFileSync(join(repoRoot, 'web', 'styles.css'), 'utf8');

let n = 0;
const ok = (cond, msg) => { assert.ok(cond, msg); n += 1; };
const eq = (a, b, msg) => { assert.equal(a, b, msg); n += 1; };

/* ---------------- ① 导航：首页 tab 排第一且默认高亮 ---------------- */
const navStart = indexHtml.indexOf('<nav class="tabs"');
const navEnd = indexHtml.indexOf('</nav>', navStart);
ok(navStart > 0 && navEnd > navStart, '找不到 #tabs 导航（切片失败）');
const nav = indexHtml.slice(navStart, navEnd);

const tabHomeIdx = nav.indexOf('id="tabHome"');
const tabDlIdx = nav.indexOf('id="tabDownload"');
ok(tabHomeIdx > 0, '缺少「首页」tab（#tabHome）');
ok(tabDlIdx > 0, '「下载视频」tab（#tabDownload）不能丢');
ok(tabHomeIdx < tabDlIdx, '「首页」tab 必须排在「下载视频」之前（默认落地页）');
ok(/class="tab is-active" data-view="home" id="tabHome"/.test(nav),
  '「首页」tab 必须是默认高亮（class 含 is-active, data-view="home"）');
ok(!/class="tab is-active" data-view="download"/.test(nav),
  '「下载视频」tab 不能再带 is-active —— 否则会与首页同时高亮');

/* ---------------- ② 首页 section 本体 ---------------- */
const hvStart = indexHtml.indexOf('id="homeView"');
ok(hvStart > 0, '缺少 #homeView（独立首页 section）');
const hvEnd = indexHtml.indexOf('<div id="downloadView"', hvStart);
ok(hvEnd > hvStart, '#homeView 应紧邻在 #downloadView 之前（切片右边界）');
const home = indexHtml.slice(hvStart, hvEnd);

// 显隐必须由 switchView 统管（避免两页同屏）；但**静态默认是可见**（首帧即首页，见第 ⑤ 段），
// 不能要求 #homeView 自带 hidden —— 那正是「先闪下载页」的成因。
ok(/el\.homeView\.hidden = !isHome;/.test(appJs),
  '首页显隐未纳入 switchView 统管（会与下载页同屏）');
// 下载页结构不许被改动：首页切片里不得出现下载页的核心控件
['id="urlInput"', 'id="resolveBtn"', 'id="downloadBtn"'].forEach((s) => {
  ok(!home.includes(s), `首页切片里出现下载页控件 ${s} —— 两页被混在一起了`);
});

// 三张卡片：视频自动解说（桌面端）/ 视频下载 / 百度网盘下载
eq((home.match(/class="hp-card /g) || []).length, 3, '首页必须恰好 3 张能力卡（解说/下载/网盘）');
ok(/<h3>视频自动解说<\/h3>/.test(home), '第一张卡必须是「视频自动解说」（用户指定优先级 ①）');
ok(/<h3>视频下载<\/h3>/.test(home), '缺少「视频下载」卡');
ok(/<h3>百度网盘下载<\/h3>/.test(home), '缺少「百度网盘下载」卡');
ok(/class="hp-badge">桌面端</.test(home), '解说卡必须标「桌面端」徽章（网页版暂未提供该能力）');
ok(/<a href="\/download">下载客户端 →<\/a>/.test(home), '解说卡必须引导到 /download（网页版做不到，不能给个死按钮）');

// 全功能清单：10 项网页可用 + 12 项桌面独有 = 22
const capItems = home.match(/<li><span class="hp-cap-ic">/g) || [];
eq(capItems.length, 22, `全功能清单必须列全 22 项（网页 10 + 桌面 12），实际 ${capItems.length}`);
ok(home.includes('桌面客户端独有'), '全功能清单必须区分「桌面客户端独有」（否则等于骗用户网页版能用）');
// 2026-10-24 用户明确「不要」：底部的「怎么用 / 已知限制」整段已移除，别再自动加回来
ok(!home.includes('hp-how') && !home.includes('hp-steps') && !home.includes('hp-limits'),
  '底部「怎么用 / 已知限制」段应已移除（用户 2026-10-24 明确不要）');
ok(!home.includes('已知限制'), '不得再出现「已知限制」标题（用户已否掉这一段）');

/* ---------------- ③ 用户点名文案（一字不改） ---------------- */
const cssNoComment = stylesCss.replace(/\/\*[\s\S]*?\*\//g, '');
ok(home.includes('百度网盘分享链接高速下载'), '主副标题必须写「高速下载」（用户明确改为高速，不是满速）');
ok(!home.includes('满速'), '不得再出现「满速下载」（用户 2026-10-24 要求改成「高速下载」）');
ok(home.includes('体验会员级别网速'), '网盘卡必须写「体验会员级别网速」（用户指定文案）');
ok(!home.includes('需登录百度账号'), '不得出现「需登录百度账号」（用户要求去掉）');
ok(/16\.2 MB\/s/.test(home), '网盘任务条必须展示 16.2 MB/s（用实测速度，不用 3.6 的旧数字）');

// 「想体验全部功能请到桌面版」= 一行纯文字提示。
// 2026-10-24 用户第二次明确：「只做文字提示，不做按钮」⇒ 去掉底色/边框/圆角/箭头，
// 整条就是普通说明文字，只有「桌面版」保持为文字链接（下划线）可点去 /download。
ok(/<p class="hp-hint">想体验全部功能请到<a href="\/download"><b>桌面版<\/b><\/a><\/p>/.test(home),
  '「想体验全部功能请到桌面版」必须是纯文字提示（hp-hint 段落 + 「桌面版」文字链接），不能做成按钮');
// CTA 区「有点击欲望」的三条落地：主按钮更大更厚、有扫光、副按钮有方向图标
ok(/class="hp-btn hp-primary" id="homeStartBtn">\s*<span>开始使用<\/span>/.test(home),
  '主按钮「开始使用」必须带图标结构（不再是纯文字）');
ok(/class="hp-btn hp-ghost" href="\/download">\s*<svg/.test(home), '副按钮「下载桌面版」必须带下载图标');
const btnRule = (cssNoComment.match(/\.hp-btn\s*\{[^}]*\}/) || [''])[0];
const _btnH = parseFloat((btnRule.match(/height:\s*([\d.]+)rem/) || [])[1]);
ok(_btnH >= 3.2, `主按钮高度须 ≥3.2rem（原来 2.7rem 太小，不够「想点」；实际 ${_btnH || '未定义'}rem）`);
ok(/@keyframes hpSheen\b/.test(cssNoComment) && /\.hp-primary::after\s*\{[\s\S]*?animation:\s*hpSheen/.test(cssNoComment),
  '主按钮缺少扫光动画（hpSheen）—— 突出点击意愿的关键动效');
ok(/prefers-reduced-motion:\s*reduce\)\s*\{[\s\S]*?hp-primary::after\s*\{\s*animation:\s*none/.test(cssNoComment),
  '扫光动画必须尊重 prefers-reduced-motion（无动画偏好时关闭）');

/* ---------------- ④ 首页每个 hp-* 类都必须有样式（防白板） ---------------- */
const usedCls = new Set();
(home.match(/class="([^"]+)"/g) || []).forEach((m) => {
  m.slice(7, -1).split(/\s+/).forEach((c) => { if (c.startsWith('hp-')) usedCls.add(c); });
});
ok(usedCls.size >= 20, `首页应使用足量 hp- 类（实际 ${usedCls.size}），类名可能被改成无前缀而撞车`);
const missing = [...usedCls].filter((c) => !new RegExp('\\.' + c + '(?![\\w-])').test(cssNoComment));
eq(missing.length, 0, `以下 hp- 类在 styles.css 里没有规则（会渲染成白板）：${missing.join(', ')}`);

ok(/#homeView\s*\{[^}]*margin-top/.test(cssNoComment),
  '#homeView 需要与顶栏留出间距（margin-top），否则首页卡片贴住导航');
// 大字提示条：字号必须显著大于正文（0.92rem 的 lede），否则用户要的「大字」没做到
const hintRule = (cssNoComment.match(/\.hp-hint\s*\{[^}]*\}/) || [''])[0];
ok(/font-size:\s*clamp\(1\.0[0-9]?rem/.test(hintRule) || /font-size:\s*1\.[0-9]+rem/.test(hintRule),
  '.hp-hint 字号必须放大（≥1.05rem）—— 用户要求「大字」写这行提示');
ok(/font-weight:\s*[78]00/.test(hintRule), '.hp-hint 必须加粗（用户要求「大字」提示）');
// 用户 2026-10-24 明确：「只做文字提示，不做按钮」—— 这条反过来钉死，防止以后又被做成按钮。
ok(!/(^|[^-\w])background\s*:/.test(hintRule), '.hp-hint 不得有底色（那是按钮外观，用户只要文字提示）');
ok(!/border-radius\s*:/.test(hintRule), '.hp-hint 不得有圆角（会变成胶囊按钮）');
ok(!/(^|[^-\w])border\s*:/.test(hintRule) && !/border-left\s*:/.test(hintRule),
  '.hp-hint 不得有边框/左色条（用户明确不要按钮）');
ok(!/box-shadow\s*:/.test(hintRule), '.hp-hint 不得有投影（按钮才需要托起来）');
ok(!/(^|[^-\w])padding\s*:/.test(hintRule), '.hp-hint 不得有内边距（会把文字撑成一个盒子）');
ok(!/display:\s*inline-flex/.test(hintRule), '.hp-hint 不能是 flex 盒子（会被当成控件）');
ok(!/\.hp-hint:hover\s*\{[^}]*transform\s*:/.test(cssNoComment),
  '.hp-hint 悬停不能让整条位移/放大（那是按钮的反馈，用户只要文字）');
ok(!/hp-hint\s*>\s*i/.test(cssNoComment),
  '提示条不得再带箭头图标（hp-hint > i 是按钮暗示）；结构上 p.hp-hint 里只允许文字 + 「桌面版」链接');

/* ---------------- ⑤ app.js 接线（漏一处就是「点了没反应」） ---------------- */
ok(/tabHome:\s*\$\('tabHome'\)/.test(appJs), 'el 表未登记 tabHome');
ok(/homeView:\s*\$\('homeView'\)/.test(appJs), 'el 表未登记 homeView');

const svStart = appJs.indexOf('function switchView');
ok(svStart > 0, '找不到 switchView');
const svEnd = appJs.indexOf('\n}', appJs.indexOf('el.tabHome', svStart));
const sv = appJs.slice(svStart, svEnd > svStart ? svEnd : svStart + 4000);

ok(/const isHome = view === 'home'/.test(sv), 'switchView 未识别 home 视图');
ok(/if \(el\.homeView\) el\.homeView\.hidden = !isHome/.test(sv), 'switchView 未切换 homeView 显隐');
ok(/el\.downloadView\.hidden = isHome \|\|/.test(sv),
  'isHome 未并入 downloadView 隐藏条件 —— 首页与下载页会同屏叠着');
ok(/el\.tabHome\.classList\.toggle\('is-active', isHome\)/.test(sv), 'switchView 未切换首页 tab 高亮');

ok(/el\.tabHome\.addEventListener\('click', \(\) => switchView\('home'\)\)/.test(appJs),
  '首页 tab 未绑定点击 —— 点了没反应');
ok(/querySelectorAll\('#homeView \[data-goto="download"\]'\)/.test(appJs),
  '首页卡片上的「去下载」跳转未接线（data-goto 处理器缺失）');
// 网页版 tab 由 applyWebTabs 只隐藏「桌面端专属」那几个；首页必须不在那张清单里，
// 且 HTML 里不能自带 hidden（否则入口直接消失、用户永远点不到）
const _hideList = (appJs.match(/\[([^\]]*'tabLibrary'[^\]]*)\]\.forEach/) || [])[1] || '';
ok(_hideList.includes('tabLibrary'), '找不到网页版 tab 隐藏清单（applyWebTabs 结构变了）');
ok(!_hideList.includes('tabHome'), 'tabHome 不得被列入网页版隐藏清单 —— 首页入口会消失');
ok(!/id="tabHome"[^>]*\shidden/.test(nav), 'tabHome 不能自带 hidden 属性');

// 默认落地页 = 首页（两处：同步兜底 + 异步重套用），且都必须带深链回落
// 🔴 2026-10-24：默认视图改为「有 #homeView 就 home，否则 download」——
//    桌面壳与网页版共用 web/app.js，但桌面 index.html 没有首页元素，写死 home 会把下载页藏成空白。
ok(/if \(!bootViewSet\) switchView\(el\.homeView \? 'home' : 'download'\)/.test(appJs),
  '同步兜底的默认视图必须是首页（无 #homeView 的宿主回落 download）');
ok(/switchView\(_hashView \|\| \(el\.homeView \? 'home' : 'download'\)\)/.test(appJs),
  '异步回落的默认视图必须是首页（且保留 #view=x 深链优先）');

/* ---------------- ⑤ 首帧：静态 HTML 就落在首页（不能等 JS 来切） ---------------- */
// 用户实测：打开 hanyuxz.top「先跳转下载页、再跳转首页」，很怪。
// 根因：静态 HTML 里 #homeView 带 hidden、#downloadView 不带 —— 浏览器第一次绘制画的就是下载页，
//       等 app.js 执行完 switchView('home') 才切走，于是出现闪屏。
// 红线：首页的可见性必须由**静态标记**决定，不依赖 JS 执行时机。
const _homeTag = (indexHtml.match(/<section id="homeView"[^>]*>/) || [''])[0];
ok(_homeTag && !/\shidden[\s>]/.test(_homeTag),
  '首帧闪屏：静态 HTML 里 #homeView 不能带 hidden（否则浏览器首帧画的是下载页，JS 再切回去 = 闪一下）');
const _dlTag = (indexHtml.match(/<div id="downloadView"[^>]*>/) || [''])[0];
ok(_dlTag && /\shidden[\s>]/.test(_dlTag),
  '首帧闪屏：静态 HTML 里 #downloadView 必须带 hidden（否则下载页先被绘制出来）');
// 桌面壳 / #view=download 需要首帧回到下载页：head 里用 data-boot 标记 + 内联样式兜底，
// 且必须在样式表之前就生效（内联 style，不能放 styles.css —— 外部样式到达前已经绘制过一帧）。
ok(/html\[data-boot\] #homeView \{ display: none !important; \}/.test(indexHtml),
  '深链的首帧兜底样式缺失（data-boot 规则）');
ok(/html\[data-boot="download"\] #downloadView \{ display: block !important; \}/.test(indexHtml),
  '深链 #view=download 首帧未把下载页放出来（下载页静态是 hidden，会白屏）');
ok(/data-boot', hash === 'download' \? 'download' : 'other'/.test(indexHtml),
  '首帧脚本必须只按深链分流（不能按桌面壳 —— 桌面端加载的是另一份无 #homeView 的 HTML，兜底反而会闪）');
ok(/switchView\(_hashView0 \|\| \(el\.homeView \? 'home' : 'download'\)\)/.test(appJs),
  '同步 boot 落地的默认视图必须是首页（无 #homeView 时回落 download）');
// data-boot 只管第一次绘制，进入正常切换后必须撤掉，否则会与 hidden 属性打架
ok(/document\.documentElement\.removeAttribute\('data-boot'\)/.test(appJs),
  'switchView 未清除 data-boot 首帧标记 —— 首帧样式会一直压着后续切换');

console.log(`✅ 首页回归守卫生效（web-dev）：独立 section / 默认落地 / 22 项能力 / 文案一字不改 / ${usedCls.size} 个 hp- 类全有样式 / 接线齐备（共 ${n} 项断言）`);
