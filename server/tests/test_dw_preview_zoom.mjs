// 去水印第二轮反馈回归（2026-09-29 用户实测）：
// ① 触控板捏合（ctrlKey+wheel）落弹窗黑色区域时整页被缩放——白色弹窗框跟着变大；
//    修复 = 在 dwImgModal / dwPreviewWrap 拦截 ctrl+wheel 转成「只放大图片」。
// ② 结果要可预览：原图/处理后可点击打开灯箱大图；效果不行可「↻ 重新加工」回编辑器。
// 说明：dw 区块依赖过重（img/svg/canvas/getBoundingClientRect 全套），动态跑整块不现实，
// 这里用「源码切片 + 结构断言」钉住关键行为，锚点被挪走时立即红。
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const appJs = readFileSync(join(repoRoot, 'web', 'app.js'), 'utf8');
const indexHtml = readFileSync(join(repoRoot, 'web', 'index.html'), 'utf8');
const stylesCss = readFileSync(join(repoRoot, 'web', 'styles.css'), 'utf8');

// ---- 区块切片：从元素注册表到「开始处理」绑定为止的 dw 前端代码 ----
const START = appJs.indexOf("dwImgFile: $('dwImgFile')");
const END_ANCHOR = "el.dwImgRedo.addEventListener('click', () => {";
const END = appJs.indexOf(END_ANCHOR);
assert.ok(START > 0 && END > START, 'dw 前端区块应存在');
const dwBlock = appJs.slice(START, END + END_ANCHOR.length + 400);

// ① 捏合拦截：ctrl+wheel 一律 preventDefault 且转成图片缩放，且不会与 img 自身 wheel 双重缩放
assert.ok(/const dwPinchToZoom = \(target\) => \(e\) => \{/.test(dwBlock),
  '必须存在 dwPinchToZoom 捏合拦截器');
assert.ok(/if \(!e\.ctrlKey\) return;/.test(dwBlock), '只拦截 ctrl+wheel（捏合）');
assert.ok(/e\.preventDefault\(\);/.test(dwBlock), '必须 preventDefault 阻止整页缩放');
assert.ok(/el\.dwImgModal\.addEventListener\('wheel', dwPinchToZoom\('modal'\)/.test(dwBlock),
  '弹窗必须绑定捏合拦截');
assert.ok(/el\.dwPreviewWrap\.addEventListener\('wheel', dwPinchToZoom\('preview'\)/.test(dwBlock),
  '主预览区必须绑定捏合拦截');
assert.ok(/e\.target === el\.dwModalImg \|\| e\.target === el\.dwModalCanvas \|\| e\.target === el\.dwModalSvg/.test(dwBlock),
  'img/canvas 上的 wheel 已由 dwBindView 处理，必须去重防双重缩放');
assert.ok(/zObj\.value = e\.deltaY < 0 \? Math\.min\(5, zObj\.value \+ 0\.2\) : Math\.max\(1, zObj\.value - 0\.2\);/.test(dwBlock),
  '捏合必须走 dwApplyZoom 同款步进（只放大图片）');

// ② 结果预览灯箱
assert.ok(/const dwOpenResultLightbox = \(src, cap\) => \{/.test(dwBlock), '必须存在灯箱打开函数');
assert.ok(/el\.dwImgOrig\.addEventListener\('click', \(\) => dwOpenResultLightbox\(el\.dwImgOrig\.src, '原图'\)\)/.test(dwBlock),
  '点原图应打开灯箱');
assert.ok(/el\.dwImgOut\.addEventListener\('click', \(\) => dwOpenResultLightbox\(el\.dwImgOut\.src, '处理后'\)\)/.test(dwBlock),
  '点处理后应打开灯箱');
assert.ok(/el\.dwResultLightboxClose\.addEventListener\('click', dwCloseResultLightbox\)/.test(dwBlock),
  '灯箱关闭按钮必须绑定');
assert.ok(/e\.target\.classList\.contains\('dw-modal-backdrop'\)\) dwCloseResultLightbox\(\);/.test(dwBlock),
  '点背景应关闭灯箱');

// ②b 重新加工：隐藏旧结果 + 保留选区提示 + 滚回编辑器
assert.ok(/el\.dwImgRedo\.addEventListener\('click', \(\) => \{/.test(dwBlock), '必须绑定重新加工按钮');
assert.ok(/el\.dwImgResult\.hidden = true;/.test(dwBlock), '重新加工应隐藏旧结果');
assert.ok(/el\.dwImgPreview\.scrollIntoView\(\{ behavior: 'smooth', block: 'center' \}\)/.test(dwBlock),
  '重新加工应滚回编辑器');
assert.ok(/选区仍保留/.test(dwBlock), '重新加工应提示选区仍保留');

// ---- index.html 结构 ----
assert.ok(/id="dwResultLightbox"/.test(indexHtml), '结果灯箱容器必须存在');
assert.ok(/id="dwResultLightboxImg"/.test(indexHtml), '灯箱大图必须存在');
assert.ok(/id="dwResultLightboxClose"/.test(indexHtml), '灯箱关闭按钮必须存在');
assert.ok(/id="dwImgRedo"[^>]*>↻ 重新加工/.test(indexHtml), '重新加工按钮必须存在');
assert.ok(/原图（点击预览）/.test(indexHtml) && /处理后（点击预览）/.test(indexHtml),
  '结果图标题应提示可点击预览');

// ---- styles.css ----
assert.ok(/\.dw-compare img \{ cursor: zoom-in; \}/.test(stylesCss), '结果图应有放大手势光标');
assert.ok(/\.dw-lightbox-inner \{/.test(stylesCss), '灯箱样式必须存在');
assert.ok(/\.dw-lightbox-close \{/.test(stylesCss), '灯箱关闭按钮样式必须存在');

console.log('✅ 去水印捏合缩放 + 结果预览/重新加工 回归测试通过');
