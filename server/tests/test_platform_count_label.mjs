// 平台数对外口径守卫（2026-09-30，两端同款 · 桌面 App）
//
// 背景：header 徽章（#engineBadge）静态兜底原本停在「支持 15+ 平台」（反向低估，早该改），
// 而 JS 加载平台列表后又把它覆盖成真实平台数（`支持 116 个平台`）——徽章上出现了一个会随平台
// 增减 / 某一平台临时不可解析而漂移的**具体数字**。
// 用户拍板：两端（网页版 + 桌面 App）统一改成「取整百 + 加号」→ 116 显示为 `100+`。
//
// 所以这里钉三件事：
//   ① 徽章必须走统一格式化函数（不是硬编码，否则以后两端改不齐）；
//   ② 该函数的真实行为（116 → "100+"、99 → "99"、250 → "200+"）必须成立 —— 直接 eval 出来跑，
//      而不是断言源码字符串长得像；
//   ③ 静态兜底文案必须也是「取整百+加号」形（曾停在「支持 15+ 平台」）。
//
// 说明：纯源码级 + 函数级检查，不做 DOM 运行时。
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const appJs = readFileSync(join(repoRoot, 'web', 'app.js'), 'utf8');
const indexHtml = readFileSync(join(repoRoot, 'web', 'index.html'), 'utf8');

// ---- ① 徽章必须走格式化函数，不许再吐裸数字 ----
assert.ok(!/el\.badge\.textContent = `支持 \$\{platforms\.length\}/.test(appJs),
  '「支持 N 平台」徽章不得再直接拼接 platforms.length（应走 fmtPlatformCount）');
assert.ok(/el\.badge\.textContent = `支持 \$\{fmtPlatformCount\(platforms\.length\)\} 平台`/.test(appJs),
  '「支持 N 平台」徽章必须使用 fmtPlatformCount，且文案为「支持 100+ 平台」（不带「个」，与静态兜底一致）');

// ---- ② 静态兜底：首屏未拉到平台列表时显示的那份，也必须是「取整百+加号」形 ----
const badgeStatic = (indexHtml.match(/id="engineBadge"[^>]*>([^<]*)</) || [])[1];
assert.ok(badgeStatic, 'index.html 找不到 #engineBadge 静态文案');
assert.equal(badgeStatic.trim(), '支持 100+ 平台',
  '#engineBadge 静态兜底应为「支持 100+ 平台」（曾停在「支持 15+ 平台」，会反向低估）');

// ---- ③ 函数真实行为：抽出那一行源码 eval 出来跑 ----
const line = appJs.split('\n').find((l) => l.includes('const fmtPlatformCount = '));
assert.ok(line, 'app.js 缺少 fmtPlatformCount 定义');
const src = line.trim().replace(/^const\s+fmtPlatformCount\s*=\s*/, '').replace(/;\s*$/, '');
assert.ok(src.includes('=>'), 'fmtPlatformCount 应为单行箭头函数（本守卫按单行抽取）');
// eslint-disable-next-line no-eval
const fmtPlatformCount = eval('(' + src + ')');

const cases = [[116, '100+'], [100, '100+'], [101, '100+'], [199, '100+'], [200, '200+'],
  [250, '200+'], [1000, '1000+'], [99, '99'], [16, '16'], [0, '0']];
for (const [n, want] of cases) {
  assert.equal(fmtPlatformCount(n), want, `fmtPlatformCount(${n}) 应为 ${want}，实际 ${fmtPlatformCount(n)}`);
}

console.log('✅ 平台数口径守卫通过（桌面 App）：徽章走 fmtPlatformCount，116→100+ / 250→200+ / 99→99');
