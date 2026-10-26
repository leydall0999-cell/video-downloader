// 解说页「切走再回来，原视频没了」回归守卫（2026-10-24 用户报障）
//
// 用户原话：「刚才没做完，打开了其他页面，又回来继续做了，看不到原视频文件了，
//   重新加载一次也没有出来，刷新没有效果」。
//
// 根因：loadCommentary() 每次进解说页都无条件 `setupComPreview(null)`
//   （那是 2026-09-18 为修「黑屏上残留覆层」加的），但从没做过「恢复」——
//   侧边栏切到别的页再点回来、或点「刷新」，选中/拖入的源视频就被清成空占位。
//
// 修法（web/app.js，纯前端，可热更）：
//   ① `comLastPick` 记住当前源（library 档还写 localStorage，真刷新也能恢复）；
//   ② 两个选源入口（媒体库下拉 change / 从本地文件生成）都调 comSaveLastPick；
//   ③ loadCommentary() 清之前先把 pick 与 File 对象捞出来，refreshComSource()
//      **之后**再 comRestoreLastPick 放回去（顺序不能反：下拉选项是那时才重建的）；
//   ④ 本地文件在真刷新后拿不回 File 对象 ⇒ 明确提示重选，不留空屏让人猜。
//   羽化带不用特殊处理：loadedmetadata 会自己再跑一次探测，绿条随之回来。
//
// 行为验证（Playwright + 桩后端 + 真 mp4，同一脚本跑新旧两版）：
//   旧版：选片 → 切走 → 切回 ⇒ src 空、下拉空、裁剪卡隐藏（复现用户报障）；
//   新版：同样流程 9/9 全过（src/下拉/裁剪卡/时长全部恢复，点刷新也一样）。
//
// 本测试读 web/app.js 真源码（不测副本）。
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const appJs = readFileSync(join(repoRoot, 'web', 'app.js'), 'utf8');
// 剥掉注释再比对：注释里也写着 comLastPick / setupComPreview(null) 等字样，
// 不剥就会「注释里提一句」也算过，断言自证成真。
const js = appJs.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');

let n = 0;
const ok = (cond, msg) => { assert.ok(cond, msg); n += 1; };

// ---- ① 状态与三个函数都在 ----
ok(/let comLastPick = null;/.test(js), '缺少 comLastPick 状态变量（恢复的凭据）');
ok(/const comSaveLastPick = /.test(js), '缺少 comSaveLastPick（选源时记账）');
ok(/const comReadLastPick = /.test(js), '缺少 comReadLastPick（真刷新后从 localStorage 回读）');
ok(/const comRestoreLastPick = /.test(js), '缺少 comRestoreLastPick（把源放回预览）');

// ---- ② loadCommentary 必须是「先捞后清再恢复」，且恢复排在 refreshComSource 之后 ----
ok(/const _pick = comLastPick \|\| comReadLastPick\(\);/.test(js),
  'loadCommentary 必须在清空前捞走 comLastPick（否则恢复无从谈起）');
ok(/const _localFile = selectedLocalFile;/.test(js),
  'loadCommentary 必须在清空前捞走 selectedLocalFile —— 它是全页唯一持有 File 对象的地方，\n' +
  '跟着一起清就再也恢复不了（切视图不重载页面，对象还在；真刷新才没）');
ok(/comRestoreLastPick\(_pick, _localFile\);/.test(js),
  'loadCommentary 末尾必须调 comRestoreLastPick（否则切回来仍是空屏）');
{
  const iRefresh = js.indexOf('refreshComSource();');
  const iRestore = js.indexOf('comRestoreLastPick(_pick, _localFile);');
  ok(iRefresh > 0 && iRestore > iRefresh,
    '恢复必须排在 refreshComSource() 之后 —— 媒体库下拉选项是那一刻才重建的，\n' +
    '提前恢复会因选项不存在而静默失败（表现为「恢复了但没反应」）');
}

// ---- ③ 两个选源入口都要记账（漏一个就是「选了这个能恢复、选那个不能」）----
ok(/comSaveLastPick\(\{ kind: 'library', id: el\.comSource\.value/.test(js),
  '媒体库下拉 change 里必须 comSaveLastPick({kind:"library"})（最常见的入口）');
ok(/comSaveLastPick\(\{ kind: 'local', id: '', name: file\.name \}\)/.test(js),
  'setLocalFile（本地文件/拖拽）里必须 comSaveLastPick({kind:"local"})');
ok(/comSaveLastPick\(null\);/.test(js),
  '把下拉切回默认项（等于取消选择）必须清账，否则会恢复出一个用户已取消的源');

// ---- ④ 本地文件恢复不到时要明说，不能留空屏 ----
ok(/刷新后无法自动读回本地文件/.test(js),
  '真刷新后本地文件拿不回来，必须给明确提示（comFileStatus）——\n' +
  '静默留空屏就是用户报障的「看不到原视频文件了」');
ok(/selectedLocalFile = localFile;[\s\S]{0,200}setupComPreview\(URL\.createObjectURL\(localFile\)/.test(js),
  '本地文件恢复路径必须重新建 objectURL 并上屏（不能只记状态不显示）');

// ---- ⑤ library 档恢复前要先确认选项里还有这条 ----
ok(/\[\.\.\.el\.comSource\.options\]\.some\(\(o\) => o\.value === pick\.id\)/.test(js),
  '恢复媒体库档前必须校验选项仍存在（片源被删/归档时安静留空，不报错刷屏）');

// ---- ⑥ 羽化绿条靠 loadedmetadata 自动重跑，别把这条链拆了 ----
ok(/addEventListener\('loadedmetadata', \(\) => \{\s*comFeatherPaint\(\);\s*comFeatherDetect\(false\);/.test(js),
  'loadedmetadata → comFeatherDetect 的自动探测链必须还在\n' +
  '（恢复视频源后，「已探测到原字幕（命中 N/24 帧）」靠它自己回来）');

console.log(`✅ 解说页源恢复守卫生效（app-dev）：记账/捞存/顺序/入口齐备/本地提示/探测链完好（共 ${n} 项）`);
