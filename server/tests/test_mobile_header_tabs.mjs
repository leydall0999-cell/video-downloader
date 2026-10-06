// 移动端顶栏功能导航可见性回归（2026-10-07 用户截图反馈「手机网页版功能缺失」）
//
// 症状：手机（~390px）打开网页版，顶栏只剩「品牌 + 支持100+平台 / Download下载 / 登录」，
//       去水印 / AI字幕 / 个人中心 / 更多功能 等入口整排消失。
// 根因：nav.tabs 并入单行 flex 顶栏 header-inner，带 min-width:0 + 默认 flex-shrink:1；
//       而 .brand / .header-actions 都是 flex:none 不收缩。窄屏空间不足时唯独 .tabs
//       被压缩成 0 宽（线上实测 #tabs width=0、.tabs-inner width=0）→ 功能导航不可见。
// 修法：@media (max-width: 760px) 让顶栏换行、.tabs 独占一行（order:3 + flex:1 1 100%）
//       横向滚动，并给 .tab 合理触控高度。
//
// 本测试钉三件事（任一被改坏即红）：
//   ① 存在 ≤760px 的媒体查询块，且其中 .header-inner 允许换行（flex-wrap: wrap）；
//   ② 同块内 .tabs 明确 order:3 + flex:1 1 100%（独占一行、不再被挤成 0 宽）；
//   ③ index.html 里 #tabs 仍是顶栏内的 .tabs（否则选择器落空、修复静默失效）。
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const stylesCss = readFileSync(join(repoRoot, 'web', 'styles.css'), 'utf8');
const indexHtml = readFileSync(join(repoRoot, 'web', 'index.html'), 'utf8');

// 取出所有 `@media (max-width: 760px) { ... }` 块（花括号配对）
function mediaBlocks(css, query) {
  const out = [];
  let from = 0;
  for (;;) {
    const start = css.indexOf(query, from);
    if (start < 0) break;
    const open = css.indexOf('{', start);
    if (open < 0) break;
    let depth = 0, i = open;
    for (; i < css.length; i++) {
      if (css[i] === '{') depth++;
      else if (css[i] === '}') { depth--; if (depth === 0) break; }
    }
    out.push(css.slice(open + 1, i));
    from = i + 1;
  }
  return out;
}

const blocks = mediaBlocks(stylesCss, '@media (max-width: 760px)');
const mobileHeader = blocks.find((b) => /\.header-inner/.test(b));
assert.ok(mobileHeader,
  'styles.css 必须存在一个 @media (max-width: 760px) 块来修复手机顶栏（否则功能导航会被挤成 0 宽、整排消失）');

// ① 顶栏允许换行
assert.ok(/\.header-inner\s*\{[^}]*flex-wrap\s*:\s*wrap/.test(mobileHeader),
  `≤760px 时 .header-inner 必须 flex-wrap: wrap（让功能导航换到第二行），实际块：\n${mobileHeader.trim()}`);

// ② 功能导航独占一行
assert.ok(/\.tabs\s*\{[^}]*order\s*:\s*3/.test(mobileHeader),
  `≤760px 时 .tabs 必须有 order: 3（换到第二行），实际块：\n${mobileHeader.trim()}`);
assert.ok(/\.tabs\s*\{[^}]*flex\s*:\s*1\s+1\s+100%/.test(mobileHeader),
  `≤760px 时 .tabs 必须 flex: 1 1 100%（独占整行），否则仍可能被压缩，实际块：\n${mobileHeader.trim()}`);

// ③ 结构前提：#tabs 仍是顶栏内的 .tabs 导航
assert.ok(/<nav\s+class="tabs"\s+id="tabs"/.test(indexHtml),
  'index.html 必须保留 `<nav class="tabs" id="tabs">`（功能导航容器），否则上面的样式选择器全部落空');
assert.ok(/class="[^"]*header-inner"[\s\S]*?<nav\s+class="tabs"\s+id="tabs"/.test(indexHtml),
  '#tabs 必须位于 .header-inner 顶栏容器内（与品牌、胶囊按钮同一行），否则「单行被挤」的场景不成立');

console.log('✅ 移动端顶栏功能导航回归通过（≤760px：header 换行 + tabs 独占一行横向滚动）');
