#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频工坊 logo 生成器：矢量几何 -> 高清位图（无水印）。

对每个方向输出 3 个文件：
  icon_<key>_squircle_1024.png  macOS 应用图标（圆角 + 透明外角）
  icon_<key>_alipay_320.png     支付宝应用图标（满幅方图、无透明通道）
  icon_<key>.svg                矢量源文件（可无限缩放）

圆角凸多边形用「内缩多边形 + 各边宽 2r 圆头描边」＝与半径 r 圆盘的 Minkowski 和，
几何精确，不会出现「顶点外溢成球」。

用法: <venv>/bin/python gen_logo.py
"""
import math
import os

import numpy as np
from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo_candidates")
FINAL = 1024
SS = 4
W = FINAL * SS
ACCENT = (79, 227, 255)
HL_STRENGTH = 0.18
CORNER_RATIO = 0.225          # squircle 圆角半径 / 边长


def hx(s):
    s = s.lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def hexof(c):
    return "#%02X%02X%02X" % tuple(c)


def grad_rgb(size, c0, c1):
    t = (np.linspace(0, 1, size)[:, None] + np.linspace(0, 1, size)[None, :]) / 2.0
    t = t[..., None]
    a = np.array(c0, float)
    b = np.array(c1, float)
    return (a * (1 - t) + b * t).astype(np.uint8)


def highlight_alpha(size, strength=HL_STRENGTH):
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    cx, cy = size * 0.5, size * 0.08
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) / (size * 0.92)
    return (np.clip(1.0 - dist, 0, 1) ** 2 * strength * 255).astype(np.uint8)


# ------------------------------------------------------------ 几何工具
def _norm(v):
    n = math.hypot(v[0], v[1])
    return (v[0] / n, v[1] / n) if n else (0.0, 0.0)


def _signed_area(pts):
    s = 0.0
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        s += x0 * y1 - x1 * y0
    return s


def cw(pts):
    """统一为屏幕坐标(y 向下)顺时针，保证内法线方向一致。"""
    return pts if _signed_area(pts) > 0 else pts[::-1]


def _inner_poly(pts, r):
    """凸多边形向内等距 r 收缩后的顶点（相邻等距线交点）。"""
    n = len(pts)
    out = []
    for i in range(n):
        a, b, c = pts[(i - 1) % n], pts[i], pts[(i + 1) % n]
        n1 = _norm((-(b[1] - a[1]), (b[0] - a[0])))      # 边 a->b 内法线
        n2 = _norm((-(c[1] - b[1]), (c[0] - b[0])))      # 边 b->c 内法线
        p1 = (a[0] + n1[0] * r, a[1] + n1[1] * r)
        d1 = (b[0] - a[0], b[1] - a[1])
        p2 = (b[0] + n2[0] * r, b[1] + n2[1] * r)
        d2 = (c[0] - b[0], c[1] - b[1])
        den = d1[0] * d2[1] - d1[1] * d2[0]
        if abs(den) < 1e-9:
            out.append(p1)
            continue
        t = ((p2[0] - p1[0]) * d2[1] - (p2[1] - p1[1]) * d2[0]) / den
        out.append((p1[0] + d1[0] * t, p1[1] + d1[1] * t))
    return out


class Canvas:
    """把 0..1 单位坐标映射到工作分辨率，并封装绘制。"""

    def __init__(self):
        self.mw = Image.new("L", (W, W), 0)
        self.ma = Image.new("L", (W, W), 0)
        self.dw = ImageDraw.Draw(self.mw)
        self.da = ImageDraw.Draw(self.ma)

    def u(self, v):
        return v * W

    def rr(self, box, r, mask="w", fill=255):
        d = self.dw if mask == "w" else self.da
        x0, y0, x1, y1 = (self.u(v) for v in box)
        d.rounded_rectangle([x0, y0, x1, y1], radius=self.u(r), fill=fill)

    def rpoly(self, pts, r, mask="w", fill=255):
        """圆角凸多边形（精确）。"""
        d = self.dw if mask == "w" else self.da
        P = cw([(self.u(a), self.u(b)) for a, b in pts])
        rp = self.u(r)
        inner = _inner_poly(P, rp)
        d.polygon(inner, fill=fill)
        wd = max(1, int(round(2 * rp)))
        n = len(inner)
        for i in range(n):
            d.line([inner[i], inner[(i + 1) % n]], fill=fill, width=wd)
            x, y = inner[i]
            d.ellipse([x - rp, y - rp, x + rp, y + rp], fill=fill)

    def star(self, cx, cy, ro, ri, fill=255):
        pts = []
        for k in range(4):
            a0 = math.radians(90 + k * 90)
            a1 = math.radians(90 + k * 90 + 45)
            pts.append((self.u(cx) + self.u(ro) * math.cos(a0), self.u(cy) - self.u(ro) * math.sin(a0)))
            pts.append((self.u(cx) + self.u(ri) * math.cos(a1), self.u(cy) - self.u(ri) * math.sin(a1)))
        self.da.polygon(pts, fill=fill)


# ------------------------------------------------------------ 三个方向
def shapes_a(c):
    """A · 精修下载箭头"""
    c.rr((0.465, 0.185, 0.535, 0.455), 0.020)                        # 箭杆
    c.rpoly([(0.305, 0.445), (0.695, 0.445), (0.500, 0.720)], 0.018)  # 箭头
    c.rr((0.335, 0.780, 0.665, 0.848), 0.034)                        # 底线


def shapes_b(c):
    """B · 胶片格 + 下载箭头"""
    c.rr((0.205, 0.265, 0.795, 0.735), 0.065)                        # 胶片外框
    c.rr((0.253, 0.313, 0.747, 0.687), 0.032, fill=0)                # 挖空内芯
    hs = 0.017
    for yu in (0.345, 0.430, 0.515, 0.600):                          # 左右齿孔
        for xu in (0.229, 0.771):
            c.rr((xu - hs, yu - hs, xu + hs, yu + hs), 0.008, fill=0)
    c.rr((0.4725, 0.335, 0.5275, 0.495), 0.020)                      # 箭杆
    c.rpoly([(0.375, 0.495), (0.625, 0.495), (0.500, 0.650)], 0.016)  # 箭头


def shapes_c(c):
    """C · 播放三角 + AI 火花"""
    c.rpoly([(0.310, 0.280), (0.310, 0.720), (0.710, 0.500)], 0.030)  # 播放键
    c.star(0.748, 0.258, 0.105, 0.038)                               # AI 火花


GEO = {
    "A_download": dict(c0=(30, 123, 240), c1=(56, 214, 245), accent=None),
    "B_film": dict(c0=(18, 43, 92), c1=(43, 79, 216), accent=None),
    "C_play_ai": dict(c0=(58, 42, 168), c1=(123, 92, 240), accent=ACCENT),
}
DRAW = {"A_download": shapes_a, "B_film": shapes_b, "C_play_ai": shapes_c}


def build(key):
    cfg = GEO[key]
    img = Image.fromarray(grad_rgb(W, cfg["c0"], cfg["c1"]), "RGB")
    img.paste((255, 255, 255), mask=Image.fromarray(highlight_alpha(W), "L"))
    c = Canvas()
    DRAW[key](c)
    img.paste((255, 255, 255), mask=c.mw)
    if cfg["accent"]:
        img.paste(cfg["accent"], mask=c.ma)

    rgb1024 = img.resize((FINAL, FINAL), Image.LANCZOS)
    sq = Image.new("L", (W, W), 0)
    ImageDraw.Draw(sq).rounded_rectangle([0, 0, W - 1, W - 1], radius=int(CORNER_RATIO * W), fill=255)
    rgba = img.convert("RGBA")
    rgba.putalpha(sq)
    return rgb1024, rgba.resize((FINAL, FINAL), Image.LANCZOS), rgb1024.resize((320, 320), Image.LANCZOS)


# ------------------------------------------------------------ SVG
def _P(pts):
    return cw([(a * FINAL, b * FINAL) for a, b in pts])


def _svg_rect(box, r, fill):
    x0, y0, x1, y1 = (v * FINAL for v in box)
    return (f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{x1 - x0:.1f}" height="{y1 - y0:.1f}" '
            f'rx="{r * FINAL:.1f}" ry="{r * FINAL:.1f}" fill="{fill}"/>')


def _svg_rpath(pts, r):
    P = _P(pts)
    n = len(P)
    rp = r * FINAL

    def ent(i):
        v, a = P[i], P[(i - 1) % n]
        u = _norm((a[0] - v[0], a[1] - v[1]))
        return (v[0] + u[0] * rp, v[1] + u[1] * rp)

    def ext(i):
        v, b = P[i], P[(i + 1) % n]
        u = _norm((b[0] - v[0], b[1] - v[1]))
        return (v[0] + u[0] * rp, v[1] + u[1] * rp)

    E = [ent(i) for i in range(n)]
    X = [ext(i) for i in range(n)]
    seg = [f"M {X[0][0]:.1f} {X[0][1]:.1f}"]
    for k in range(1, n + 1):
        i = k % n
        seg.append(f"L {E[i][0]:.1f} {E[i][1]:.1f}")
        seg.append(f"A {rp:.1f} {rp:.1f} 0 0 1 {X[i][0]:.1f} {X[i][1]:.1f}")
    seg.append("Z")
    return f'<path d="{" ".join(seg)}" fill="#fff"/>'


def _svg_star(cx, cy, ro, ri, fill):
    pts = []
    for k in range(4):
        a0 = math.radians(90 + k * 90)
        a1 = math.radians(90 + k * 90 + 45)
        pts.append(f"{cx * FINAL + ro * FINAL * math.cos(a0):.1f},{cy * FINAL - ro * FINAL * math.sin(a0):.1f}")
        pts.append(f"{cx * FINAL + ri * FINAL * math.cos(a1):.1f},{cy * FINAL - ri * FINAL * math.sin(a1):.1f}")
    return f'<polygon points="{" ".join(pts)}" fill="{fill}"/>'


def svg_for(key):
    cfg = GEO[key]
    b = []
    if key == "A_download":
        b.append(_svg_rect((0.465, 0.185, 0.535, 0.455), 0.020, "#fff"))
        b.append(_svg_rpath([(0.305, 0.445), (0.695, 0.445), (0.500, 0.720)], 0.018))
        b.append(_svg_rect((0.335, 0.780, 0.665, 0.848), 0.034, "#fff"))
    elif key == "B_film":
        b.append(_svg_rect((0.205, 0.265, 0.795, 0.735), 0.065, "#fff"))
        b.append(_svg_rect((0.253, 0.313, 0.747, 0.687), 0.032, "url(#bg)"))
        hs = 0.017
        for yu in (0.345, 0.430, 0.515, 0.600):
            for xu in (0.229, 0.771):
                b.append(_svg_rect((xu - hs, yu - hs, xu + hs, yu + hs), 0.008, "url(#bg)"))
        b.append(_svg_rect((0.4725, 0.335, 0.5275, 0.495), 0.020, "#fff"))
        b.append(_svg_rpath([(0.375, 0.495), (0.625, 0.495), (0.500, 0.650)], 0.016))
    elif key == "C_play_ai":
        b.append(_svg_rpath([(0.310, 0.280), (0.310, 0.720), (0.710, 0.500)], 0.030))
        b.append(_svg_star(0.748, 0.258, 0.105, 0.038, hexof(cfg["accent"])))

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{FINAL}" height="{FINAL}" viewBox="0 0 {FINAL} {FINAL}">
  <defs>
    <linearGradient id="bg" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="{FINAL}" y2="{FINAL}">
      <stop offset="0" stop-color="{hexof(cfg["c0"])}"/>
      <stop offset="1" stop-color="{hexof(cfg["c1"])}"/>
    </linearGradient>
    <radialGradient id="hl" gradientUnits="userSpaceOnUse" cx="{FINAL // 2}" cy="{int(FINAL * 0.08)}" r="{int(FINAL * 0.92)}">
      <stop offset="0" stop-color="#FFFFFF" stop-opacity="{HL_STRENGTH}"/>
      <stop offset="1" stop-color="#FFFFFF" stop-opacity="0"/>
    </radialGradient>
    <clipPath id="sq"><rect x="0" y="0" width="{FINAL}" height="{FINAL}" rx="{int(CORNER_RATIO * FINAL)}" ry="{int(CORNER_RATIO * FINAL)}"/></clipPath>
  </defs>
  <g clip-path="url(#sq)">
    <rect width="{FINAL}" height="{FINAL}" fill="url(#bg)"/>
    <rect width="{FINAL}" height="{FINAL}" fill="url(#hl)"/>
    {"".join(b)}
  </g>
</svg>
'''


def main():
    os.makedirs(OUT, exist_ok=True)
    for key in GEO:
        rgb1024, rgba1024, rgb320 = build(key)
        p1 = os.path.join(OUT, f"icon_{key}_squircle_1024.png")
        p2 = os.path.join(OUT, f"icon_{key}_alipay_320.png")
        p3 = os.path.join(OUT, f"icon_{key}.svg")
        rgba1024.save(p1)
        rgb320.save(p2, quality=95)
        with open(p3, "w", encoding="utf-8") as f:
            f.write(svg_for(key))
        print("OK", key, os.path.getsize(p1), os.path.getsize(p2), os.path.getsize(p3))


if __name__ == "__main__":
    main()
