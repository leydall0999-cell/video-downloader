// 网页版「生成二维码 / 生成网页」两视图回归守卫（2026-09-30 受限版分享）
//
// 设计红线，改坏即红：
//   ① 分享凭据必须登录才拿（前端 shareEnsureToken 未登录必须走 openAuthModal，绝不静默降级）；
//   ② 上传必须带 X-Auth / X-Filename / X-Expire 三头（分享节点鉴权契约），成功后必须调
//      /api/share/consume 计数（否则服务端每日限次被绕过）；
//   ③ 受限版上限：前端必须校验文件大小（服务端 limits 下发，不得写死更大值）；
//   ④ 「生成网页」在浏览器端合成，模板红线沿用桌面端 pagetool 的实测教训：
//      绝不把 base64 写两遍（data-src-target 锚点回填）、MIME 表写死（.m4a→audio/mp4）、
//      视频带编码提示、PDF 带移动端降级提示；
//   ⑤ 二维码本地生成（vendor_qrcode.min.js），不许外链任何二维码服务。
// 说明：与 test_member_page.mjs 同思路 —— 源码切片 + 结构断言，锚点被挪走立即红。
import assert from 'node:assert/strict';
import { readFileSync, existsSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const indexHtml = readFileSync(join(repoRoot, 'web', 'index.html'), 'utf8');
const appJs = readFileSync(join(repoRoot, 'web', 'app.js'), 'utf8');
const stylesCss = readFileSync(join(repoRoot, 'web', 'styles.css'), 'utf8');

// ---- ① 页面本体与导航（合并视图：一个「分享」tab + 内部子页签）----
for (const id of ['shareView', 'shareQrView', 'pageGenView', 'tabShare', 'shareSubnav']) {
  assert.ok(indexHtml.includes(`id="${id}"`), `缺少 #${id}`);
}
assert.ok(!indexHtml.includes('id="tabShareQr"') && !indexHtml.includes('id="tabPageGen"'),
  '旧的两个独立 tab 必须删除（已合并为 #tabShare + 子页签）');
const iSqr = indexHtml.indexOf('data-sharepane="shareqr"');
const iPg = indexHtml.indexOf('data-sharepane="pagegen"');
const iImgConv = indexHtml.indexOf('id="tabImageConvert"');
const iSub = indexHtml.indexOf('id="tabSubtitle"');
assert.ok(iImgConv < indexHtml.indexOf('id="tabShare"') && indexHtml.indexOf('id="tabShare"') < iSub,
  '「分享」tab 应排在「图片转换」与「AI 字幕」之间');
assert.ok(indexHtml.includes('>生成二维码/链接</button>'), '导航 tab 文案必须是「生成二维码/链接」（用户 2026-10-01 指定）');
assert.ok(iSqr >= 0 && iPg > iSqr, '子页签必须是 生成二维码 在前、生成网页 在后');
assert.ok(indexHtml.includes('id="sqrFileInput"') && indexHtml.includes('id="pgFileInput" multiple'),
  '两视图都要有文件选择 input（生成网页必须 multiple）');
assert.ok(indexHtml.includes('id="sqrExpire"'), '生成二维码必须可选有效期');
assert.ok(indexHtml.includes('id="pgExpire"'), '生成网页（在线链接）必须可选有效期');
// 子页签必须复用 pf-subnav（并排胶囊，参考用户截图）；标题用渐变竖条 guide-title
assert.ok(/<nav class="pf-subnav" id="shareSubnav"/.test(indexHtml), '子页签必须复用 pf-subnav 并排样式');
for (const [id, cls] of [['sqrTitle', 'guide-title'], ['pgTitle', 'guide-title']]) {
  assert.ok(new RegExp(`id="${id}" class="${cls}"`).test(indexHtml), `#${id} 必须用渐变竖条 guide-title 标题样式`);
}

// ---- ② vendor 二维码库：本地自托管 + 已加载 ----
const vendorPath = join(repoRoot, 'web', 'js', 'vendor_qrcode.min.js');
assert.ok(existsSync(vendorPath) && statSync(vendorPath).size > 5000, '缺少本地二维码库 web/js/vendor_qrcode.min.js');
const vendorHead = readFileSync(vendorPath, 'utf8').slice(0, 400);
assert.ok(/MIT|qrcode-generator/i.test(vendorHead) || /qrcode/.test(vendorHead), 'vendor 库来源可疑');
assert.ok(indexHtml.includes('js/vendor_qrcode.min.js'), 'index.html 未加载二维码库');
assert.ok(!/qss\.io|api\.qrserver|google.*chart.*qr|qrserver\.com/i.test(appJs + indexHtml),
  '不许外链二维码生成服务（隐私 + 离线）');
assert.ok(/window\.qrcode/.test(appJs), '二维码必须用本地 vendor 库生成');

// ---- ③ 三处接线（合并视图版）----
assert.ok(appJs.includes("tabShare: $('tabShare')") && appJs.includes("shareSubnav: $('shareSubnav')"),
  'el 表缺少合并视图引用');
assert.ok(appJs.includes("shareQrView: $('shareQrView')") && appJs.includes("pageGenView: $('pageGenView')"),
  'el 表缺少两个子面板引用');
assert.ok(!appJs.includes("$('tabShareQr')") && !appJs.includes("$('tabPageGen')"),
  'el 表残留已删除的旧 tab 引用');
const svStart = appJs.indexOf('function switchView(view) {');
const svEnd = appJs.indexOf('else stopTorPoll();', svStart);
const sv = appJs.slice(svStart, svEnd);
assert.ok(/const isShare = view === 'share'/.test(sv), 'switchView 缺少 isShare 分支');
assert.ok(!/view === 'shareqr'/.test(sv) && !/view === 'pagegen'/.test(sv),
  'switchView 不再接受 shareqr/pagegen 独立视图');
assert.ok(/\|\| isShare/.test(sv), 'isShare 必须并入 isAnyExtra（否则与下载视图同屏叠加）');
assert.ok(/el\.shareView\.hidden = !isShare/.test(sv), 'switchView 未切换分享视图显隐');
assert.ok(/el\.shareQrView\.hidden = !\(isShare && _sharePane === 'shareqr'\)/.test(sv) &&
  /el\.pageGenView\.hidden = !\(isShare && _sharePane === 'pagegen'\)/.test(sv),
  '子面板显隐必须由 isShare + _sharePane 共同决定');
assert.ok(/el\.tabShare\.classList\.toggle\('is-active', isShare\)/.test(sv), '未切换分享 tab 高亮');
assert.ok(/if \(el\.tabShare\) el\.tabShare\.addEventListener\('click', \(\) => switchView\('share'\)\)/.test(appJs),
  '分享 tab 未绑定 switchView');
// 子页签点击：必须更新 _sharePane + 同步两面板显隐 + 高亮
const subStart = appJs.indexOf("if (el.shareSubnav) el.shareSubnav.querySelectorAll('.pf-subnav-btn')");
assert.ok(subStart > 0, '缺少子页签点击绑定');
const subBlock = appJs.slice(subStart, subStart + 700);
assert.ok(/_sharePane = b\.getAttribute\('data-sharepane'\)/.test(subBlock), '子页签点击必须更新 _sharePane');
assert.ok(/shareQrView\.hidden = _sharePane !== 'shareqr'/.test(subBlock) &&
  /pageGenView\.hidden = _sharePane !== 'pagegen'/.test(subBlock), '子页签点击必须同步两面板显隐');
assert.ok(/classList\.toggle\('is-active', x === b\)/.test(subBlock), '子页签点击必须更新高亮');
// 生成网页转在线链接必须使用用户选的有效期
const pgLinkStart = appJs.indexOf('const pgToLink = async () => {');
const pgLinkBlock = appJs.slice(pgLinkStart, appJs.indexOf('const pgCopyUrl', pgLinkStart));
assert.ok(/parseInt\(el\.pgExpire && el\.pgExpire\.value, 10\)/.test(pgLinkBlock),
  '生成网页转在线链接必须读取 #pgExpire 有效期');

// ---- ④ 登录门禁 + 凭据契约 ----
const setStart = appJs.indexOf('const shareEnsureToken = async (say) => {');
assert.ok(setStart > 0, '缺少 shareEnsureToken');
const setEnd = appJs.indexOf('const shareApplyLimitTexts', setStart);
const set = appJs.slice(setStart, setEnd);
assert.ok(set.includes("request('/api/share/token')"), '凭据必须来自 /api/share/token');
assert.ok(/code === 'NO_AUTH'/.test(set) && /openAuthModal\(/.test(set),
  '未登录取凭据必须弹登录框（不许静默失败）');
assert.ok(/code === 'SHARE_UNAVAILABLE'|r\.error/.test(set), '节点不支持时要给用户明确提示');

const upStart = appJs.indexOf('const shareUpload = (body, name, expireDays, onProgress)');
assert.ok(upStart > 0, '缺少 shareUpload');
const up = appJs.slice(upStart, appJs.indexOf('const shareConsume', upStart) > 0
  ? appJs.indexOf('const shareApplyLimitTexts', upStart) : appJs.indexOf('const shareShowResult', upStart));
assert.ok(/setRequestHeader\('X-Auth', _shareTok\.token\)/.test(up), '上传必须带 X-Auth');
assert.ok(/setRequestHeader\('X-Filename', encodeURIComponent\(name\)\)/.test(up), '上传必须带 X-Filename');
assert.ok(/setRequestHeader\('X-Expire', String\(Math\.max\(1, expireDays \| 0\) \* 86400\)\)/.test(up),
  '上传必须带 X-Expire（秒）');
assert.ok(!up.includes('/api/upload-chunk'), '受限版上传必须走分享节点 /api/upload（不是转换分片通道）');
assert.ok(/\/api\/upload'/.test(up), '上传端点应为 /api/upload（nginx 同源反代 8901）');

// 限次契约：取 token 即计数 → 前端【不许缓存复用】token（否则限次形同虚设），
// 且不许再调已废弃的 /api/share/consume。
assert.ok(!appJs.includes('/api/share/consume'), '/api/share/consume 已废弃，不得再调用');
const setBlock = appJs.slice(setStart, appJs.indexOf('const shareApplyLimitTexts', setStart));
assert.ok(!/if \(_shareTok\) return/.test(setBlock), 'shareEnsureToken 不得缓存复用 token（限次靠每次取新 token 计数）');
assert.ok(appJs.includes("request('/api/share/limits')"), '限额文案必须走只读 /api/share/limits（不计数）');
assert.ok((appJs.match(/await shareEnsureToken\(/g) || []).length >= 2,
  '两条上传路径（二维码 / 网页转链接）都必须先取凭据');

// ---- ⑤ 受限版上限（前端必须拦） ----
assert.ok(/f\.size > maxMb \* 1024 \* 1024/.test(appJs), '生成二维码前端必须校验单文件大小');
assert.ok(/total > maxMb \* 1024 \* 1024/.test(appJs), '生成网页前端必须校验合计大小');
assert.ok(!/(9\d{1,2})\s*\*\s*1024\s*\*\s*1024/.test(appJs.replace(/maxMb \* 1024 \* 1024/g, '')),
  '大小上限必须由服务端 limits 下发，前端不许写死更大值');

// ---- ⑥ 生成网页合成器红线（移植自 pagetool.py 的实测教训）----
const pgStart = appJs.indexOf('const PG_KIND = {');
assert.ok(pgStart > 0, '缺少 PG_KIND（合成器被整体移除）');
const pgEnd = appJs.indexOf('const bufToDataUri', pgStart);
const pg = appJs.slice(pgStart, pgEnd);
assert.ok(/data-src-target/.test(pg), '下载锚点必须用 data-src-target 回填（base64 绝不写两遍）');
assert.ok(/'.m4a': 'audio\/mp4'/.test(appJs), 'MIME 表必须写死 .m4a→audio/mp4（浏览器对 mp4a-latm 拒播）');
assert.ok(pg.includes('PG_NON_PLAYABLE_HINT'), '视频必须带编码提示');
assert.ok(pg.includes('部分移动端浏览器不支持内嵌 PDF'), 'PDF 必须带移动端降级提示');
assert.ok(/PLACEHOLDER_TEXT/.test(pg) && pg.includes("it.kind === 'text'"),
  '文本必须走占位符注入（不要把文本再 base64 一份）');
assert.ok(pg.includes('<\\/script>'), '内嵌页脚本闭合必须写 <\\/script>（否则提前终止宿主页面脚本）');

// ---- ⑦ 样式 ----
for (const sel of ['.share-row {', '.share-qr-img {', '.share-meta {']) {
  assert.ok(stylesCss.includes(sel), `styles.css 缺少 ${sel}`);
}

// ---- ⑧ 合并视图入口：nav data-view=share + 子页签切换调用不许被改名 ----
assert.ok(appJs.includes("switchView('share')"), '分享视图入口调用被改名');
assert.ok(!appJs.includes("switchView('shareqr')") && !appJs.includes("switchView('pagegen')"),
  '旧独立视图入口调用必须清除');

console.log('✅ 网页版分享两视图守卫通过：登录门禁/凭据契约/计数/受限上限/合成器红线/本地二维码');
