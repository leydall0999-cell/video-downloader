// 顶层视图卡片与吸顶顶栏的间距回归（2026-10-04 用户截图反馈「网页多个功能两个卡片连一起了」）
//
// 症状：main 直属的视图卡片（去水印 / 音乐转换 / 图片转换 / AI 字幕 / 会员 / 更多功能）
//       上边缘距 sticky 顶栏下边框仅 1px —— 视觉上「顶栏 + 卡片」像两块粘在一起。
//       对照「视频处理」参考版式：首个内容块距顶栏 3rem（.uc-subtabs 的 margin-top）。
// 修复：`main > .panel { margin-top: 3rem; }`，只作用于 main 直属视图卡片，
//       不碰下载页 hero 之后的卡片 / 分享·个人中心的子面板 / 视频处理自己的卡片。
//
// 本测试钉三件事（任一被改坏即红）：
//   ① 规则存在，且挂在 `main >` 层级、值为 3rem；
//   ② index.html 里那几个视图确实还是 main 的直属 .panel（否则选择器落空、修复静默失效）；
//   ③ 没有 id 级规则用 `margin` 简写把 margin-top 重置掉（#dwView 原来就是
//      `margin: 0 auto 1rem`，会盖掉上间距 —— 这是最容易复发的写法）。
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const stylesCss = readFileSync(join(repoRoot, 'web', 'styles.css'), 'utf8');
const indexHtml = readFileSync(join(repoRoot, 'web', 'index.html'), 'utf8');

// ① 规则本体
const rule = stylesCss.match(/main\s*>\s*\.panel\s*\{([^}]*)\}/);
assert.ok(rule, 'styles.css 必须存在 `main > .panel` 上间距规则（否则顶层卡片会重新贴住顶栏）');
assert.ok(/margin-top\s*:\s*3rem/.test(rule[1]),
  `main > .panel 的 margin-top 应为 3rem（对齐视频处理参考版式 3rem 上间距），实际：${rule[1].trim()}`);

// ② 结构前提：这些顶层视图必须是 main 的直属 .panel（缩进两空格 = 直属子级）
//    注：#shareQrView / #pageGenView 同样是两空格缩进，但它们是 #shareView 的子面板
//        （DOM 里 parent=shareView），已有 .uc-subtabs 撑出上间距，故不列入本测试的关注集。
const twoSpacePanels = [...indexHtml.matchAll(/^ {2}<(?:section|div)\s+id="([A-Za-z]+)"[^>]*class="panel"/gm)]
  .map(m => m[1]);
const expected = ['musicConvertView', 'imageConvertView', 'subtitleView', 'memberView', 'dwView', 'appIntroView'];
for (const id of expected) {
  assert.ok(twoSpacePanels.includes(id),
    `#${id} 必须仍是 main 的直属 .panel（当前两空格缩进的卡片：${twoSpacePanels.join(', ')}）—— `
    + '若改成了别的层级，`main > .panel` 就落空了，白改。');
}

// ③ 不许有 id 级 `margin` 简写把 margin-top 重置掉
for (const id of expected) {
  const m = stylesCss.match(new RegExp(`#${id}\\s*\\{([^}]*)\\}`));
  if (!m) continue;
  const body = m[1];
  if (/margin\s*:/.test(body) && !/margin-top\s*:/.test(body)) {
    assert.fail(`#${id} 使用了 margin 简写（会把 margin-top 重置为 0，盖掉 main > .panel 的上间距）：`
      + body.trim());
  }
}
// #dwView 的正向写法（居中使用 margin-inline，不重置上下边距）
const dw = stylesCss.match(/#dwView\s*\{([^}]*)\}/);
assert.ok(dw && /margin-inline\s*:\s*auto/.test(dw[1]),
  `#dwView 必须用 margin-inline: auto 居中（而不是 margin 简写），实际：${dw ? dw[1].trim() : '缺失'}`);

// ④ 对照：参考版式的 3rem 上间距不能被顺手改小（两者是同一套视觉节奏）
assert.ok(/\.uc-subtabs\s*\{[^}]*margin\s*:\s*3rem\s+0\s+1rem/.test(stylesCss),
  '.uc-subtabs 的 3rem 上间距（参考版式基准）不应被改动');

// ⑤ 个人中心（2026-10-08 补）：#profileView 是**唯一没有 `.panel` 类的顶层视图**（轻量版，
//    不套外层卡片），所以上面的 `main > .panel` 命中不到它 ⇒ 标题 #pfTitle 会直接贴住吸顶顶栏
//    （实测 viewTop = 61px = 顶栏底边，间距 0px；memberView / musicConvertView 均为 48px）。
//    不变式：它必须自带一条 `#profileView { margin-top: 3rem }`（若哪天给它加了 class="panel"，
//    则自动落到 ① 的作用域里，这两条断言会提示你并入 expected 列表）。
const hasPfPanelClass = /^ {2}<section id="profileView"[^>]*class="panel"/m.test(indexHtml);
const pfRule = stylesCss.match(/#profileView\s*\{([^}]*)\}/);
if (hasPfPanelClass) {
  assert.ok(!pfRule || !/(^|[;\s])margin\s*:/.test(pfRule[1]),
    `#profileView 已带 .panel、由 main > .panel 兜底，就不得再用 margin 简写把上间距重置掉：${pfRule[1].trim()}`);
} else {
  assert.ok(pfRule, '#profileView 必须自带 margin-top 规则（它没有 .panel 类，命中不到 main > .panel）');
  assert.ok(/margin-top\s*:\s*3rem/.test(pfRule[1]),
    `#profileView 的 margin-top 应为 3rem（对齐其他顶层视图），实际：${pfRule[1].trim()}`);
  assert.ok(!/(^|[;\s])margin\s*:/.test(pfRule[1]),
    `#profileView 不得使用 margin 简写（会重置上下边距），实际：${pfRule[1].trim()}`);
}

console.log(`✅ 顶层视图卡片/顶栏间距回归通过（${expected.length} 个顶层视图卡片，规则 margin-top: 3rem，`
  + `个人中心 ${hasPfPanelClass ? '随 main > .panel' : '自带 #profileView 规则'}）`);
