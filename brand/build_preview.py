#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 logo 候选的多尺寸预览页（自包含；每张图只内嵌一次，靠 CSS 复用）。"""
import base64
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CAND = os.path.join(HERE, "logo_candidates")

ITEMS = [
    ("A_download", "A · 精修下载箭头", "蓝 → 青渐变；最“标准”的下载语义，辨识度最高、最稳，改动风险最小。"),
    ("B_film", "B · 胶片格 + 下载箭头", "深蓝 → 靛蓝；外框点题“视频”，中间仍是下载箭头，语义覆盖最完整。"),
    ("C_play_ai", "C · 播放键 + AI 火花", "靛蓝 → 紫；强调“AI 处理”，差异化最强，但离“下载”语义最远。"),
]
SIZES = [16, 24, 32, 48, 64, 128]


def b64(p):
    with open(p, "rb") as f:
        return base64.b64encode(f.read()).decode()


cards, styles = [], []
for key, title, desc in ITEMS:
    big = b64(os.path.join(CAND, f"icon_{key}_squircle_1024.png"))
    a320 = b64(os.path.join(CAND, f"icon_{key}_alipay_320.png"))
    styles.append(f'.i-{key}{{background-image:url("data:image/png;base64,{big}")}}')
    styles.append(f'.a-{key}{{background-image:url("data:image/png;base64,{a320}")}}')
    small = "".join(f'<div class="sz"><div class="ico i-{key}" style="width:{s}px;height:{s}px"></div>'
                    f'<span>{s}px</span></div>' for s in SIZES)
    cards.append(f'''
    <section class="card">
      <div class="hero">
        <div class="bg dark"><div class="ico big i-{key}"></div></div>
        <div class="bg light"><div class="ico big i-{key}"></div></div>
      </div>
      <div class="meta">
        <h2>{title}</h2>
        <p>{desc}</p>
        <div class="tag">小尺寸辨识度</div>
        <div class="sizes">{small}</div>
        <div class="tag">支付宝上传图（320×320，满幅无透明）</div>
        <div class="ico a-{key} a320"></div>
      </div>
    </section>''')

html = f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>视频工坊 · Logo 候选预览</title>
<style>
  :root{{--bg:#0f1216;--fg:#e9eef5;--mut:#93a0b0;--line:#242c36;--card:#151a21}}
  *{{box-sizing:border-box}}
  body{{margin:0;padding:28px;background:var(--bg);color:var(--fg);
       font:15px/1.65 -apple-system,"PingFang SC","Helvetica Neue",Arial,sans-serif}}
  h1{{font-size:22px;margin:0 0 6px}}
  .sub{{color:var(--mut);margin:0 0 22px;font-size:13px}}
  .card{{display:grid;grid-template-columns:minmax(240px,340px) 1fr;gap:24px;
        background:var(--card);border:1px solid var(--line);border-radius:16px;
        padding:20px;margin-bottom:18px}}
  .hero{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}
  .bg{{border-radius:12px;display:flex;align-items:center;justify-content:center;
      padding:14px;min-height:150px}}
  .bg.dark{{background:#0b0d10}}
  .bg.light{{background:#f4f6f9}}
  .ico{{background-size:contain;background-repeat:no-repeat;background-position:center;border-radius:22.5%}}
  .ico.big{{width:100%;max-width:132px;aspect-ratio:1/1}}
  .a320{{width:96px;height:96px;border-radius:18px;margin-top:8px}}
  h2{{font-size:17px;margin:2px 0 6px}}
  .meta p{{margin:0 0 14px;color:var(--mut);font-size:13px}}
  .tag{{font-size:11px;color:var(--mut);border:1px solid var(--line);border-radius:999px;
       padding:2px 9px;display:inline-block;margin:10px 0 10px}}
  .sizes{{display:flex;align-items:flex-end;gap:16px;flex-wrap:wrap;margin-bottom:4px}}
  .sz{{display:flex;flex-direction:column;align-items:center;gap:5px}}
  .sz span{{font-size:10px;color:var(--mut)}}
  @media(max-width:640px){{.card{{grid-template-columns:1fr}}}}
</style></head>
<body>
  <h1>视频工坊 · Logo 候选（3 个方向）</h1>
  <p class="sub">均为自绘矢量（无水印），已导出 macOS 应用图标（1024 圆角）+ 支付宝上传图（320 满幅）+ SVG 源文件。左列：深/浅底效果；右列：多尺寸辨识度。</p>
  {''.join(cards)}
</body></html>
<style>{''.join(styles)}</style>
'''

out = os.path.join(CAND, "preview.html")
with open(out, "w", encoding="utf-8") as f:
    f.write(html)
print("OK", out, os.path.getsize(out))
