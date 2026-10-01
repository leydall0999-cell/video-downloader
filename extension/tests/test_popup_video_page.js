// 扩展 popup「视频页空状态」操作按钮守卫（2026-10-01 用户反馈）。
//
// 背景：YouTube 等用加密流（UMP/SABR）的站点，扩展抓不到可下载直链，popup 里原本
// 只显示一段文字提示，**没有任何可点操作**，用户反馈「这里也要可以操作」。
// 现在空状态必须给「解析并下载 / 复制链接」两个按钮，且复制失败要如实提示（不静默）。
//
// 本测试做源码级契约钉死（不为 popup 造一整套假 document），与 test_ext_version_cmp.py
// 的「从源码抽真实实现来跑」思路一致：防止有人后续把按钮/事件删掉而无人察觉。
'use strict';

const fs = require('fs');
const path = require('path');

let PASS = 0;
let FAIL = 0;
function check(name, cond, extra) {
  if (cond) { PASS++; console.log('  ✅ ' + name); }
  else { FAIL++; console.log('  ❌ ' + name + (extra !== undefined ? '  → ' + extra : '')); }
}

const dir = path.join(__dirname, '..');
const js = fs.readFileSync(path.join(dir, 'popup.js'), 'utf8');
const css = fs.readFileSync(path.join(dir, 'popup.css'), 'utf8');
const html = fs.readFileSync(path.join(dir, 'popup.html'), 'utf8');

console.log('▶ 扩展 popup「视频页空状态」操作按钮契约');

// ① 视频页分支仍在（防整体被删）
check('保留视频页空状态分支（st.videoPage）', /if \(st\.videoPage\)/.test(js));

// ② 空状态改为 DOM 构建并挂 empty-actions 容器（不再是单一 innerHTML 字符串）
check('空状态含 empty-actions 容器', /acts\.className = 'empty-actions'/.test(js));

// ③ 两个按钮文案必须存在
check('含「解析并下载」按钮', /textContent = '解析并下载'/.test(js));
check('含「复制链接」按钮', /textContent = '复制链接'/.test(js));

// ④ 事件必须真的绑定（点了没反应 = 需求未达成）
check('「解析并下载」绑定 sendCurrentPage',
  /addEventListener\('click', function \(\) \{ sendCurrentPage\(dlBtn\)/.test(js));
check('「复制链接」绑定 copyPageUrl',
  /addEventListener\('click', function \(\) \{ copyPageUrl\(cpBtn\)/.test(js));

// ⑤ 复制实现存在且用剪贴板；失败必须可见提示（绝不静默——这正是本次用户遇到的坑）
check('定义了 copyPageUrl 并使用剪贴板',
  /function copyPageUrl\(btn\)/.test(js) && /navigator\.clipboard\.writeText\(url\)/.test(js));
check('复制失败有可见提示（不静默）', /复制失败，请手动复制地址栏链接/.test(js));

// ⑥ 样式：布局类必须有定义，否则两个按钮会挤成一行无间距
check('popup.css 定义 .empty-actions', /\.empty-actions\s*\{/.test(css));

// ⑦ 「解析并下载」文案在提示里指向该操作（用户体验连贯）
check('提示语引导用户点「解析并下载」', /点下面「解析并下载」/.test(js));

// ⑧ 清晰度选择（2026-10-01 用户反馈「目前没法选择分辨率」）：
// 扩展发来的条目原本一律按「最佳画质（自动）」建任务，用户无从选择。现在 popup 顶部
// 给下拉、随每条发送上报、并记住选择；用户选的分辨率还必须回显出来。
const selBlock = (html.match(/<select id="sendQuality"[\s\S]*?<\/select>/) || [''])[0];
const selOptions = (selBlock.match(/<option value="([^"]*)"/g) || []).length;
check('popup.html 含 #sendQuality 下拉', selBlock.length > 0);
check('清晰度下拉含 8 档（best/2160/1440/1080/720/480/360/audio）', selOptions === 8, selOptions);
check('发送时上报 quality（qualityForItem）', /quality: qualityForItem\(it\),/.test(js));
check('只对「页面 / 清单」上报（直链本身就是单一流）',
  /if \(k !== 'page' && k !== 'playlist'\) return '';/.test(js));
check('改选落 chrome.storage.local（下次打开还记得）',
  /chrome\.storage\.local\.set\(\{ sendQuality: state\.quality \}\)/.test(js));
check('改选即时回显「将以「…」下载」', /\.empty-q/.test(js) && /将以「/.test(js));
check('popup.css 定义 .empty-q', /\.empty-q\s*\{/.test(css));
check('popup.css 定义 .qrow（下拉行布局）', /\.qrow\s*\{/.test(css));

console.log('');
console.log('=========================================');
console.log('  通过: ' + PASS + '   失败: ' + FAIL);
console.log('=========================================');
process.exit(FAIL === 0 ? 0 : 1);
