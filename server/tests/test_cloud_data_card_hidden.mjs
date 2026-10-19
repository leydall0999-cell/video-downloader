// 「云端资料」卡片在桌面端隐藏 回归守卫（2026-10-20 用户要求，与网页版同步）
//
// 用户诉求（截图 + 「桌面 App 那个『云端资料』也隐藏」）：会员中心里
//   「云端资料 · 已备份 N 份设置 / M 个平台登录态  [同步]」
// 这一行不要露出来。
//
// 三条约束（都不形式主义，各对应一种真实事故）：
//   ① **只隐藏、不删元素**。app.js 的 renderCloudData() 用 `$('cloudDataRow')`
//      取节点，节点被删 ⇒ 提前 return ⇒ 自动同步链虽在，但换机/首次登录的
//      状态提示也一起没了；而且 `cloudDataSyncBtn` 的事件绑定会拿到 null。
//   ② **后台链路必须原样**。隐藏的只是「展示 + 手动按钮」；登录时自动同步
//      (/api/cloud/sync)、userdata 自动 push、/api/userdata/status 查询
//      一个都不能少 —— 否则用户会「看不到卡也拿不回资料」，比露着更糟。
//   ③ **文案照常渲染**。隐藏期仍然把状态文案填进 DOM（只是 hidden），
//      将来恢复显示不用重测一遍逻辑。
//
// 本测试读 web/app.js 与 web/index.html 的**真源码**（不测副本）。
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const appJs = readFileSync(join(repoRoot, 'web', 'app.js'), 'utf8');
const indexHtml = readFileSync(join(repoRoot, 'web', 'index.html'), 'utf8');

let n = 0;
const ok = (cond, msg) => { assert.ok(cond, msg); n += 1; };

// ---- ① 元素仍在（只隐藏，不删）----
ok(indexHtml.includes('id="cloudDataRow"'),
  '#cloudDataRow 被删了：renderCloudData 会 $() 取到 null 直接 return，状态与同步入口全失效');
ok(/id="cloudDataRow"[^>]*\bhidden\b/.test(indexHtml),
  '#cloudDataRow 初始必须是 hidden（默认不闪一下再藏）');
ok(indexHtml.includes('id="cloudDataSyncBtn"'), '同步按钮节点被删了（事件绑定会拿到 null）');

// ---- ② 开关存在且为 false ----
ok(/const CLOUD_DATA_CARD_VISIBLE\s*=\s*false/.test(appJs),
  '缺少 CLOUD_DATA_CARD_VISIBLE = false 开关（要恢复显示就改成 true，别改动渲染逻辑）');

// ---- ③ 隐藏要落在「行」和「提示」两处 ----
const renderStart = appJs.indexOf('async function renderCloudData()');
ok(renderStart > 0, '找不到 renderCloudData()');
const renderEnd = appJs.indexOf('async function cloudSyncData()', renderStart);
ok(renderEnd > renderStart, 'renderCloudData 切片失败（找不到后一个函数）');
const body = appJs.slice(renderStart, renderEnd);

ok(/row\.hidden\s*=\s*!CLOUD_DATA_CARD_VISIBLE/.test(body),
  'row.hidden 必须由开关决定（写成 row.hidden = false 就等于没隐藏）');
ok(/hint\.hidden\s*=\s*!CLOUD_DATA_CARD_VISIBLE/.test(body),
  'cloudDataHint 也必须跟开关走，否则卡片藏了、提示句还露在外面（悬空文案）');

// 文案仍在渲染（恢复显示时不用重测）
ok(/txt\.textContent\s*=/.test(body), '状态文案停止渲染了（隐藏期也应照填，便于将来恢复）');
ok(/cloud\.cookies|ckCloud/.test(body), '平台登录态条数不再渲染了');

// ---- ④ 后台链路一个都不能少 ----
ok(/await renderCloudData\(\)/.test(appJs),
  'renderCloudAccount 不再调用 renderCloudData —— 登录后状态永不刷新');
ok(appJs.includes("request('/api/userdata/status')"),
  '/api/userdata/status 查询被删了（云端备份状态断了）');
ok(appJs.includes("request('/api/cloud/sync'"),
  '/api/cloud/sync 自动同步被删了（换机拉回权益/资料的主通道）');
ok(appJs.includes("request('/api/userdata/sync'"),
  '/api/userdata/sync 手动补同步被删了（隐藏了按钮，但接口与函数要留着）');
ok(/async function cloudSyncData\(/.test(appJs), 'cloudSyncData 实现被删了');
ok(/cloudDataSyncBtn/.test(appJs), '同步按钮的事件绑定被删了（恢复显示时会点了没反应）');

console.log(`✅ 桌面端「云端资料」隐藏守卫：${n} 项断言通过`);
