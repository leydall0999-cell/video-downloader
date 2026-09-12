"""server/dewatermark_core.py — 需求文档模块二：PDF / 图片去水印核心逻辑。

技术路线（与视频去水印 ffmpeg delogo / E2FGVI 无关，独立实现）：
- 图片：
  * 智能档（quality="auto"，默认）：先在用户框选区内**检测水印形态**，再按形态分流：
      - stroke：半透明文字/线条水印（绝大多数站点水印）→ 只修复水印**笔画**像素，
        背景像素零改动。这是效果的关键：整块矩形 inpaint 会把框内真实内容一起抹掉重绘，
        大区域必然糊成一片（实测 PSNR 24.8 / SSIM 0.67，21/30 场景比不处理更差）。
      - solid：实心/不透明块（内部残差密度低）→ 整块 inpaint。
      - none：未检出可见水印 / 检出不可信 → **原图不变**（Δ=0，绝不越修越糟）。
    修复算法默认 NS（实测优于 TELEA：ΔPSNR +19.13 vs +17.59）。
    30 个合成基准样本实测：PSNR 24.76→33.32、SSIM 0.67→0.89、Δavg -0.42→+8.13、
    最差 Δ -15.10→-2.04、劣化样本 21/30→3/30。
  * 传统档（quality="legacy"）：整块矩形 inpaint，与旧行为完全一致（可回退）。
  * 检测/修复仅依赖 cv2 + numpy，无新依赖、无模型下载，单图 <1s。
- PDF：
  * 注释型水印：PyMuPDF 遍历页面注释，删除 Watermark 型注释（无损、保留文字可选中性）。
  * 栅格化模式：页面栅格化 → 图片 inpaint → 重排合成（适用于扫描件 / 内容流内嵌水印）。

依赖为原生 C 扩展，安装失败（如无对应 wheel / 离线环境）时整功能优雅降级：
- `_cv2` / `_np` / `_fitz` 为 None → available()/pdf_available() 返回 False，
  上层路由据此返回 503，不影响进程启动（与 libtorrent 模式一致）。
"""
import logging
from pathlib import Path

logger = logging.getLogger("vdl.dewatermark")

try:
    import cv2 as _cv2
except Exception:  # noqa: BLE001
    _cv2 = None

try:
    import numpy as _np
except Exception:  # noqa: BLE001
    _np = None

try:
    import fitz as _fitz
except Exception:  # noqa: BLE001
    _fitz = None

def available() -> bool:
    """图片去水印是否可用（需要 cv2 + numpy）。"""
    return _cv2 is not None and _np is not None

def pdf_available() -> bool:
    """PDF 去水印是否可用（需要 fitz）。"""
    return _fitz is not None

# ------------------------------------------------------------------ 纯逻辑（不依赖原生库，可独立单测）

def normalize_region(region) -> dict:
    """把前端传来的区域（可能含字符串/越界值）收敛为 0..1 的浮点字典。

    返回 {"x","y","w","h"}，坐标被裁剪到 [0,1] 且保证 x+w<=1, y+h<=1。
    region 为 None / 空 / 非法时返回 None（调用方据此报 400）。
    """
    if not region:
        return None
    try:
        x = float(region.get("x", 0))
        y = float(region.get("y", 0))
        w = float(region.get("w", 0))
        h = float(region.get("h", 0))
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    # 先夹到 [0,1]
    x = min(max(x, 0.0), 1.0)
    y = min(max(y, 0.0), 1.0)
    w = min(max(w, 0.0), 1.0)
    h = min(max(h, 0.0), 1.0)
    # 防止越界
    if x + w > 1.0:
        w = 1.0 - x
    if y + h > 1.0:
        h = 1.0 - y
    if w <= 0 or h <= 0:
        return None
    return {"x": x, "y": y, "w": w, "h": h}

def _region_to_px(region: dict, w: int, h: int):
    """把归一化区域换算为像素矩形 (x, y, rw, rh)，并夹到图像边界内。"""
    x = max(0, int(round(region["x"] * w)))
    y = max(0, int(round(region["y"] * h)))
    rw = max(0, int(round(region["w"] * w)))
    rh = max(0, int(round(region["h"] * h)))
    if x + rw > w:
        rw = w - x
    if y + rh > h:
        rh = h - y
    return x, y, rw, rh

def normalize_regions(regions) -> list:
    """校验并归一化多区域列表。

    入参 regions 应为 [{"x","y","w","h", "op": "add"|"subtract"}, ...]。
    每项经 normalize_region 收敛；op 缺省 "add"，非法值回退 "add"。
    返回 [{"x","y","w","h","op"}]；空 / 非 list / 任一区域非法 → 返回 None（调用方据此报 400）。
    """
    if not regions or not isinstance(regions, list):
        return None
    out = []
    for r in regions:
        if not isinstance(r, dict):
            return None
        nr = normalize_region(r)
        if not nr:
            return None
        op = r.get("op") or "add"
        if op not in ("add", "subtract"):
            op = "add"
        out.append({"x": nr["x"], "y": nr["y"], "w": nr["w"], "h": nr["h"], "op": op})
    if not out:
        return None
    return out

def _build_region_mask(regions, w: int, h: int):
    """把多区域合并为单张二值 mask（uint8）。

    先收集所有 add 区域为 255（重叠自然并集），再统一用 subtract 区域置 0 挖洞。
    subtract 始终从加选并集中扣除，不受 regions 传入顺序影响。
    任一区域像素矩形为空则跳过。返回全 0 表示没有有效加选区域。
    """
    mask = _np.zeros((h, w), dtype=_np.uint8)
    for r in regions:
        if r.get("op") != "subtract":
            x, y, rw, rh = _region_to_px(r, w, h)
            if rw <= 0 or rh <= 0:
                continue
            mask[y:y + rh, x:x + rw] = 255
    for r in regions:
        if r.get("op") == "subtract":
            x, y, rw, rh = _region_to_px(r, w, h)
            if rw <= 0 or rh <= 0:
                continue
            mask[y:y + rh, x:x + rw] = 0
    return mask

def _inpaint_from_mask(img, mask, method: str, radius: float):
    """对一张 BGR numpy 数组按给定 mask 做 inpaint，返回同形状数组。"""
    flag = _cv2.INPAINT_TELEA if method == "telea" else _cv2.INPAINT_NS
    return _cv2.inpaint(img, mask, float(radius), flag)

# ------------------------------------------------------------------ 智能图片修复（传统档增强）
#
# 设计依据（2026-09-12 用 30 个「真实照片/渐变/纹理 × 斜排大字/平铺小字/角落徽标/实心
# logo」合成基准量化得出，均有 ground truth 可算 PSNR/SSIM）：
#   1) 瓶颈在 mask 精度而非修复算法：用真值笔画 mask 时 cv2.inpaint 已达 PSNR 44.3
#      （Δavg +19.1），而整块矩形框选只有 24.8（Δavg -0.4，21/30 反而更差）。
#   2) NS 全面优于 TELEA（Δavg +19.13 vs +17.59）。
#   3) 检测不确信时必须「不动」：整块 inpaint 在失败场景最差 Δ=-14.79，而不动恒为 0。
#   4) 实心块内部残差≈0（中值背景被块自身抬高），据此可与「文字笔画」可靠区分
#      （30 样本判别准确 29/30）。
#   5) 逐像素 alpha 反解在代数上恒等于直接用背景估计，故不引入（实测标注量反解更差）。
#   6) 100% 覆盖的 mask 会让 cv2.inpaint **静默 no-op**（原样返回）。排查/测试时若发现
#      「处理完没变化」，先查 mask 覆盖率；也正因如此，绝不要把子图连同满 mask 一起送进去。
#   7) 整图平铺水印（图库常见）会被误判 solid；即便用真值 mask，其 inpaint 也低于不处理
#      （26.67 vs 28.32）→ 大框选必须拒绝整块修复（max_solid_rect）。
#   8) 背景估计窗口受限于 scales（最大 35）：密集文字水印的**笔画内部**残差≈0
#      （真值强水印像素 p25≈3），这是召回无法达到 100% 的物理原因，故靠外扩 1~2px 补外沿。
#   9) 松框（用户手拖比水印大 2.5 倍）是旧链路最大杀手：PNSR 38.14→13.36；
#      但 30 样本整体上「检测笔画」仍显著优于「整块」——不要为了单个平滑场景改回整块。

_SMART = {
    "thr_k": 0.40,        # 残差阈值 = base + thr_k*(p99.5-base)
    "sat_max": 95,        # 水印多为灰白，通道差上限（抑制彩色内容误检）
    "max_fill": 0.30,     # 过检保护：候选覆盖率超此值则只保留残差最强的部分
    "solid_inner": 0.02,  # 内缩区残差密度低于此值 → 判为实心块。实测（2026-09-12）：
                          # 内缩带取 max(6, 0.30*min)（且 ≤18）时分离干净 —— 实心块与
                          # 实心 logo 全部为 0.0000，最淡的半透明笔画也有 0.041，
                          # 阈值 0.02 居中留足余量。
    "solid_inset": (0.30, 6, 18),  # 内缩带：比例、下限、上限（上限≈最大中值核 35 的一半，
                                   # 否则窗口跨越块边缘会把「内部」统计污染成非零，
                                   # 这正是旧 0.16 比例漏判实心块的根因）
    "max_solid_rect": 0.5,  # 整块 inpaint 的前置门槛：框选面积超过全图此比例时，
                            # 即便判为 solid 也拒绝整块修复（改判 none）。
                            # 实测（2026-09-12）整图平铺水印（图库常见）会被误判 solid：
                            # 真值 mask inpaint 仅 PSNR 26.67，反而低于不处理的 28.32 ——
                            # 这种情况「不动」才是最优；真正的实心 logo 框选只占图像一小块。
    "cc_frac": 0.0004,    # 连通域最小面积（占框选面积比）
    "min_fill": 0.004,    # 最小有效覆盖率（低于此视为没检出）
    "scales": (3, 5, 9, 15, 25, 35),  # 多尺度中值背景核
    "min_redelta": 12.0,  # 残差动态范围下限：低于此视为「无可见水印/只是纹理」。
                          # 实测（2026-09-12，30 样本基准 + 召回率实测）：18 → 12 的收益
                          # 是「严格占优」——Δavg +8.38→+8.92、最差损伤与变差数均不变、
                          # 改善样本 20→22。注意干净但带纹理的底图 redelta≈17 仍会被
                          # 放入下一关，由 thr_k / min_fill / max_fill / 连通域共同拦下
                          # （回归护栏见 test_detect_none_on_clean_textured_area）。
    "stroke_grow": 2,     # 笔画 mask 外扩次数（3x3 椭圆）。水印抗锯齿外沿约 1px，
                          # 不外扩会留下淡淡的字边（实测召回 0.43→0.62，
                          # 30 样本 PSNR 29.29→33.32）。外扩过多（3 次）反而掉分。
    "min_stroke_px": 24,  # 绝对像素下限
}

# refine 档 stage2 闸门：m2d 面积占 add 区域比例上限（30 样本基准最优 0.06）。
# photo-* 会被拦（m2d_cov 0.086~1.0）、gradient-tile 仍能收益 +3.79~+7.32dB
# （m2d_cov 0.036~0.051）；texture-* / 大面积 badge 也被拦（m2d_cov ≥ 0.40）。
# 阈值含义：「stage2 重检若扩散到区域 X% 以上，认为是纹理误检而非真水印
# 残影，不跑第二遍 inpaint（会抹掉真实细节）」。
_REFINED_G4_CAP = 0.06

# refine 档 stage2 重检的激进口径（在 out1 上重检用），与 _SMART 独立。
_REFINED_STAGE2_KW = {
    "thr_k": 0.15, "min_redelta": 3.0,
    "min_fill": 0.002, "max_fill": 0.6,
    "sat_max": 130,
}

# 智能档（engine=auto）回落判断：从「原图」估水印不透明度，低于此值视为半透明/浅底
# （OpenCV 易漏或留残影）→ 回落 AI（LaMa）。30 样本基准标定：实色水印 opacity 高不触发，
# 半透明水印（α≤0.5）opacity 低触发；与 refine 的「输出重检」思路不同——后者在照片底图上
# 会把纹理误判成残留（同 G4 翻车病因），故改为背景无关的「笔画对比度」判据。
_AUTO_FALLBACK_OPACITY = 0.12


def _residual_map(gray_u8):
    """多尺度中值背景的正残差图：亮于局部背景的细结构（水印笔画）会凸显。

    核需 ≥2× 笔画宽度才能把亮笔画从背景里「抹掉」，单一核无法同时覆盖大字与小字，
    故取各尺度正残差的最大值。

    注意：这是 1.0.15 的「智能档」默认残差（auto 档走这条，作为单遍回退基线）。
    双边滤波增强版见 _residual_map_bilateral（refine 档用）。
    """
    gf = gray_u8.astype(_np.float32)
    res = _np.zeros_like(gf)
    rh, rw = gf.shape
    for k in _SMART["scales"]:
        if k >= min(rh, rw):
            break
        res = _np.maximum(res, gf - _cv2.medianBlur(gray_u8, k).astype(_np.float32))
    return res


def _residual_map_bilateral(gray_u8):
    """中值多尺度 + 双边保边背景的正残差**并集**（refine 档 / 两阶段 stage1 用）。

    中值模糊在文字区会把笔画算进背景估计 → 残差≈0 → 漏检（尤其粗笔画内部，
    真值强水印像素 p25≈3，这是单遍召回上不去的物理根因）。双边滤波保边，笔画
    不被吃进背景估计 → 全笔画（含抗锯齿外沿）残差可测。两者取并集互补，
    召回从 ~60% 提升到 ~86%（见 2026-09-12 实验 bf_test / twopass_v3）。

    代价：bilateralFilter 比 medianBlur 慢约 3~5×，但单图仍在百毫秒级，可接受。
    """
    gf = gray_u8.astype(_np.float32)
    # 中值多尺度（与 _residual_map 相同）
    res_med = _np.zeros_like(gf)
    rh, rw = gf.shape
    for k in _SMART["scales"]:
        if k >= min(rh, rw):
            break
        res_med = _np.maximum(res_med, gf - _cv2.medianBlur(gray_u8, k).astype(_np.float32))
    # 双边保边背景（d=15 / sigma=100 经 bf_sizes 扫描为最佳平衡点）
    bf = _cv2.bilateralFilter(gray_u8, 15, 100, 100).astype(_np.float32)
    res_bf = _np.maximum(gf - bf, 0)
    return _np.maximum(res_med, res_bf)


def detect_watermark(img, x: int, y: int, rw: int, rh: int, residual_fn=None, **kw):
    """在像素矩形 (x,y,rw,rh) 内检测水印，返回 (mask|None, kind, info)。

    mask 为**该矩形内**的局部 uint8 mask（0/255）；kind:
      'stroke' 半透明文字/线条水印 → mask 为水印笔画
      'solid'  实心/不透明块        → mask 为 None（调用方走整块修复）
      'none'   未检出/不可信        → mask 为 None（调用方**不要改动**）

    residual_fn: 残差图生成函数，默认 _residual_map（中值，auto 档）。
                 refine 档传 _residual_map_bilateral 启用双边保边检测。
    """
    if not available():
        return None, "none", {"why": "no-opencv"}
    kw = {**_SMART, **kw}
    if residual_fn is None:
        residual_fn = _residual_map
    H, W = img.shape[:2]
    x = max(0, min(int(x), W - 1))
    y = max(0, min(int(y), H - 1))
    rw = max(0, min(int(rw), W - x))
    rh = max(0, min(int(rh), H - y))
    if rw < 8 or rh < 8:
        return None, "none", {"why": "rect-too-small"}
    roi = img[y:y + rh, x:x + rw]
    rect_area = int(rw * rh)
    g = _cv2.cvtColor(roi, _cv2.COLOR_BGR2GRAY)

    res = residual_fn(g)
    hi = float(_np.percentile(res, 99.5))
    base = float(_np.percentile(res, 55))
    if hi - base < kw["min_redelta"]:
        return None, "none", {"why": "flat", "redelta": round(hi - base, 2)}
    thr = base + max(5.0, kw["thr_k"] * (hi - base))
    mx = roi.max(axis=2).astype(_np.float32)
    mn = roi.min(axis=2).astype(_np.float32)
    cand = (res > thr) & ((mx - mn) < kw["sat_max"])
    if int(cand.sum()) < kw["min_stroke_px"]:
        return None, "none", {"why": "few-candidates"}

    cover = float(cand.sum()) / max(1, rect_area)
    info = {"cover": round(cover, 4)}

    # —— 实心块判别：内缩区（排除边缘带）残差密度低 ⇒ 是一整块不透明内容。
    # 内缩量必须 ≥ 最大中值核的一半，否则窗口跨越块边缘、把「内部」统计抬高成非零
    gl_frac, gl_lo, gl_hi = kw["solid_inset"]
    ins = int(min(gl_hi, max(gl_lo, gl_frac * min(rh, rw))))
    if rh - 2 * ins > 2 and rw - 2 * ins > 2:
        inner = (cand[ins:rh - ins, ins:rw - ins])
        inner_hit = float(inner.sum()) / float(inner.size)
        info["inner_hit"] = round(inner_hit, 4)
        if inner_hit < kw["solid_inner"]:
            # 大框选拒整块修复：整图平铺水印会被误判 solid，而它 inpaint 只会毁图
            if rect_area > kw["max_solid_rect"] * float(H * W):
                return None, "none", {**info, "why": "solid-rect-too-large"}
            return None, "solid", {**info, "why": "solid-inner-flat"}

    # —— 过检保护：覆盖率离谱（纹理误检）时只保留残差最强的部分
    if cover > kw["max_fill"]:
        vals = res[cand]
        cut = float(_np.percentile(vals, 100.0 * (1.0 - kw["max_fill"] / cover)))
        cand = cand & (res > max(cut, thr))
        info["protected"] = True

    m = cand.astype(_np.uint8) * 255
    se3 = _cv2.getStructuringElement(_cv2.MORPH_ELLIPSE, (3, 3))
    m = _cv2.morphologyEx(m, _cv2.MORPH_CLOSE, se3)
    m = _cv2.morphologyEx(m, _cv2.MORPH_OPEN, se3)
    # 连通域过滤：去掉纹理产生的细碎点（水印笔画应是成形的连通结构）
    min_area = max(6, int(kw["cc_frac"] * rect_area))
    n, lab, st, _ = _cv2.connectedComponentsWithStats(m, 8)
    keep = _np.zeros_like(m)
    for i in range(1, n):
        if st[i, _cv2.CC_STAT_AREA] >= min_area:
            keep[lab == i] = 255
    m = keep
    if int((m > 0).sum()) < kw["min_stroke_px"]:
        return None, "none", {**info, "why": "no-component"}
    m = _cv2.dilate(m, se3, iterations=int(kw["stroke_grow"]))
    fill = float((m > 0).sum()) / max(1, rect_area)
    info["fill"] = round(fill, 4)
    if fill < kw["min_fill"]:
        return None, "none", {**info, "why": "fill-too-low"}
    if fill > 0.9:
        if rect_area > kw["max_solid_rect"] * float(H * W):
            return None, "none", {**info, "why": "solid-rect-too-large"}
        return None, "solid", {**info, "why": "fill-near-full"}
    return m, "stroke", info


def _feather_merge(orig, fixed, mask, blur: float = 1.0):
    """把修复结果按 mask 混回原图：mask 内 alpha=1（保证修复强度），边界羽化避免硬接缝。

    注意用 max(binary, blurred)：单纯高斯模糊会让细笔画中心 alpha<1 导致水印残留。
    """
    a = mask.astype(_np.float32) / 255.0
    a = _np.maximum(a, _cv2.GaussianBlur(a, (0, 0), blur))
    a = a[..., None]
    return _np.clip(orig.astype(_np.float32) * (1 - a) + fixed.astype(_np.float32) * a,
                    0, 255).astype(_np.uint8)


def plan_image_repair(img, regions, quality: str = "auto", **kw):
    """把用户框选规划成「实际需要修复的 mask」，返回 (mask, stats)。

    quality='auto'  ：逐区域检测水印形态。stroke → 只标笔画；solid → 整块；
                      none → 该区域不标（原图不动）。最后统一扣除 subtract 区域。
    quality='legacy'：所有 add 区域整块标记（与旧版行为一致，可回退）。
    quality='refine'：保留 auto 行为（本函数不直接实现两阶段，由 image_inpaint_ex
                      的 _refine_pipeline 调用；此处若误传 refine 会按 auto 规划，
                      但 _refine_pipeline 内部会重新规划，故无副作用）。

    **kw：透传给 detect_watermark（如 residual_fn=_residual_map_bilateral、
          thr_k/min_redelta 等覆盖，供 refine 的 stage2 用更激进口径）。
    """
    h, w = img.shape[:2]
    mask = _np.zeros((h, w), dtype=_np.uint8)
    stats = {"regions": 0, "stroke": 0, "solid": 0, "none": 0,
             "rect_px": 0, "repair_px": 0, "details": []}
    for r in regions or []:
        if r.get("op") == "subtract":
            continue
        x, y, rw, rh = _region_to_px(r, w, h)
        if rw <= 0 or rh <= 0:
            continue
        stats["regions"] += 1
        if quality == "legacy":
            mask[y:y + rh, x:x + rw] = 255
            stats["solid"] += 1
            stats["rect_px"] += rw * rh
            continue
        sub, kind, info = detect_watermark(img, x, y, rw, rh, **kw)
        if kind == "solid":
            mask[y:y + rh, x:x + rw] = 255
            stats["solid"] += 1
        elif kind == "stroke" and sub is not None:
            view = mask[y:y + rh, x:x + rw]
            view[sub > 0] = 255
            stats["stroke"] += 1
        else:
            stats["none"] += 1
        stats["rect_px"] += rw * rh
        stats["details"].append({"rect": [x, y, rw, rh], "kind": kind, **info})
    # 减选最后统一扣除（语义与 _build_region_mask 一致：先加选并集，再挖洞）
    for r in regions or []:
        if r.get("op") == "subtract":
            x, y, rw, rh = _region_to_px(r, w, h)
            if rw > 0 and rh > 0:
                mask[y:y + rh, x:x + rw] = 0
    stats["repair_px"] = int((mask > 0).sum())
    return mask, stats


def _refine_expand(mask_full, h: int, w: int):
    """把检测到的笔画 mask 做**连通组件 bbox 智能外扩**（覆盖粗笔画内部漏检区）。

    detect_watermark 能抓到笔画边缘（高对比区），但粗笔画（尤其汉字）**内部**对比度≈0
    → 检测不到 → mask 有洞 → inpaint 不到 → 残影。本函数对每个连通组件取外接矩形，
    四边各外扩 40%（且 ≥2px），把整字填满，自然覆盖笔画内部。

    这是「二次加工」精修阶段的核心：不重新检测（首遍输出上重检对比度更低、更难），
    而是基于首遍已确认的笔画位置做几何扩张，物理上保证整字覆盖。

    返回与 mask_full 同形状的 uint8 mask（0/255）。
    """
    if not mask_full.any():
        return mask_full
    se3 = _cv2.getStructuringElement(_cv2.MORPH_ELLIPSE, (3, 3))
    n, labels, stats, _ = _cv2.connectedComponentsWithStats(mask_full, 8)
    out = _np.zeros_like(mask_full)
    for i in range(1, n):
        cx, cy, cw, ch, _area = stats[i]
        pad_x = max(2, int(cw * 0.4))
        pad_y = max(2, int(ch * 0.4))
        x1 = max(0, int(cx - pad_x))
        y1 = max(0, int(cy - pad_y))
        x2 = min(w, int(cx + cw + pad_x))
        y2 = min(h, int(cy + ch + pad_y))
        out[y1:y2, x1:x2] = 255
    # 轻量椭圆闭运算，让扩张后的块边缘更圆润（避免硬方角被 inpaint 看出边界）
    out = _cv2.morphologyEx(out, _cv2.MORPH_CLOSE, se3)
    return out


def _refine_pipeline(img, regions, dst_path, method: str = "ns", radius: int = 3):
    """两阶段去水印管线（refine 档）：粗除 → 精修（带闸门）。

    Stage 1（粗除）：双边滤波残差检测 + NS inpaint → out1（清除对比度高的笔画/边缘）。

    Stage 2（精修，仅在 G4 闸门通过时执行）：
      - 在 out1 上用更激进口径重检残影 → mask2_det
      - 闸门 G4：若 mask2_det 面积占 add 区域超过 ``_REFINED_G4_CAP``（6%，
        30 样本基准最优），跳过 stage2，避免在照片纹理上把残影当水印再
        inpaint 一次抹掉真实细节（photo-tile 全被拦、gradient-tile 仍能
        收益 +3.79~+7.32dB）。
      - 否则：仅以 mask2_det 为 stage2 mask（**不再** bbox 扩张，避免破
        坏真实边缘），裁到 add ∖ subtract，在 out1 上再 NS inpaint → out2。

    返回 (Path, info)，info 含 stage1_repair_px / stage2_repair_px /
    stage2_cov_ratio / stage2_skipped 供前端提示与回归排查。
    """
    h, w = img.shape[:2]
    used = "ns"

    # —— Stage 1：双边滤波粗除 ——
    mask1, stats1 = plan_image_repair(img, regions, "auto",
                                      residual_fn=_residual_map_bilateral)
    if not mask1.any():
        # 首遍就没检出可见水印 → 原图不变（与 auto 的 kept_original 一致）
        ok = _cv2.imwrite(str(dst_path), img)
        if not ok:
            raise RuntimeError("去水印结果写入失败")
        return Path(dst_path), {"quality": "refine", "action": "kept_original",
                                "changed": False, **stats1}

    out1 = _inpaint_from_mask(img, mask1, used, radius)
    out1 = _feather_merge(img, out1, mask1)
    stage1_px = int((mask1 > 0).sum())

    # 提前算 add_union / sub_mask：既给闸门打分，也给 stage2 mask 裁剪用
    add_union = _np.zeros((h, w), dtype=_np.uint8)
    sub_mask = _np.zeros((h, w), dtype=_np.uint8)
    for r in regions or []:
        x, y, rw, rh = _region_to_px(r, w, h)
        if rw <= 0 or rh <= 0:
            continue
        if r.get("op") == "subtract":
            sub_mask[y:y + rh, x:x + rw] = 255
        else:
            add_union[y:y + rh, x:x + rw] = 255
    add_px = max(1, int(add_union.sum()))

    # —— Stage 2：精修（需过 G4 闸门）——
    # 2a. 在 out1 上用更激进口径重检残影（首遍输出对比度更低，故放宽阈值）
    mask2_det, _ = plan_image_repair(
        out1, regions, "auto", residual_fn=_residual_map_bilateral,
        **_REFINED_STAGE2_KW)

    if not mask2_det.any():
        ok = _cv2.imwrite(str(dst_path), out1)
        if not ok:
            raise RuntimeError("去水印结果写入失败")
        return Path(dst_path), {"quality": "refine", "action": "repaired",
                                "changed": True, "stage1_repair_px": stage1_px,
                                "stage2_repair_px": 0, "stage2_cov_ratio": 0.0,
                                "stage2_skipped": "stage2-empty",
                                "method_used": used}

    # 2b. 闸门 G4：m2d 面积占 add 区域比例
    m2d_cov = float(mask2_det.sum()) / add_px
    if m2d_cov > _REFINED_G4_CAP:
        ok = _cv2.imwrite(str(dst_path), out1)
        if not ok:
            raise RuntimeError("去水印结果写入失败")
        return Path(dst_path), {"quality": "refine", "action": "repaired",
                                "changed": True, "stage1_repair_px": stage1_px,
                                "stage2_repair_px": 0, "stage2_cov_ratio": m2d_cov,
                                "stage2_skipped": f"G4-cap{_REFINED_G4_CAP}",
                                "method_used": used}

    # 2c. 通过闸门：仅以 mask2_det 为 stage2 mask，裁到 add ∖ subtract
    mask2 = mask2_det & add_union & ~sub_mask
    if not mask2.any():
        ok = _cv2.imwrite(str(dst_path), out1)
        if not ok:
            raise RuntimeError("去水印结果写入失败")
        return Path(dst_path), {"quality": "refine", "action": "repaired",
                                "changed": True, "stage1_repair_px": stage1_px,
                                "stage2_repair_px": 0, "stage2_cov_ratio": m2d_cov,
                                "stage2_skipped": "post-clip-empty",
                                "method_used": used}

    out2 = _inpaint_from_mask(out1, mask2, used, radius)
    out2 = _feather_merge(out1, out2, mask2)
    ok = _cv2.imwrite(str(dst_path), out2)
    if not ok:
        raise RuntimeError("去水印结果写入失败")
    return Path(dst_path), {"quality": "refine", "action": "repaired",
                            "changed": True, "stage1_repair_px": stage1_px,
                            "stage2_repair_px": int((mask2 > 0).sum()),
                            "stage2_cov_ratio": m2d_cov,
                            "stage2_skipped": "no",
                            "method_used": used}


def image_inpaint_ex(src_path, dst_path, regions, method: str = "ns", radius: int = 3,
                     quality: str = "auto"):
    """图片去水印（增强版）：返回 (Path, info)。

    quality='auto'（默认）：智能三路分流 + NS 修复（单遍，作为回退基线）。
    quality='legacy'       ：整块矩形 inpaint，使用调用方指定的 method（旧行为）。
    quality='refine'       ：两阶段管线（粗除→精修），对最难水印（浅底+半透明白字/
                             粗笔画汉字）显著优于单遍，详见 _refine_pipeline。

    `method` 仅在 legacy 档生效；auto / refine 档固定用实测更优的 NS，并在 info 里
    回报 method_used / repairs（各区域判定结果），供前端提示用户。
    """
    if not available():
        raise RuntimeError("OpenCV/numpy 未安装，图片去水印不可用")
    if isinstance(regions, dict):
        regions = [regions]
    if not regions:
        raise ValueError("图片去水印需要框选水印区域")
    img = _cv2.imread(str(src_path), _cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError("无法读取图片，可能是损坏或格式不支持")
    if quality not in ("auto", "legacy", "refine"):
        quality = "auto"

    if quality == "refine":
        return _refine_pipeline(img, regions, dst_path, method, radius)

    mask, stats = plan_image_repair(img, regions, quality)
    info = {"quality": quality, **stats}
    if not mask.any():
        # 智能档未检出可见水印/检出不可信 → 保持原图，绝不越修越糟（Δ=0）
        info["action"] = "kept_original"
        info["changed"] = False
        ok = _cv2.imwrite(str(dst_path), img)
        if not ok:
            raise RuntimeError("去水印结果写入失败")
        return Path(dst_path), info

    used = method if quality == "legacy" else "ns"
    if used not in ("telea", "ns"):
        used = "ns"
    out = _inpaint_from_mask(img, mask, used, radius)
    out = _feather_merge(img, out, mask)
    ok = _cv2.imwrite(str(dst_path), out)
    if not ok:
        raise RuntimeError("去水印结果写入失败")
    info.update({"action": "repaired", "changed": True,
                 "requested_method": method, "method_used": used})
    return Path(dst_path), info


def _estimate_watermark_opacity(img, stroke_mask):
    """估计水印不透明度（背景无关）：笔画像素与「笔画外一圈局部背景」的亮度差 / 背景亮度。

    半透明水印该值低（笔画与背景对比弱，如浅底半透明白字）；实色水印该值高。
    只用笔画周边一圈做局部背景，故不受整图底图（照片纹理/渐变）影响。
    返回 None 表示无法估计（笔画太小/无外圈）。
    """
    if not stroke_mask.any() or img is None:
        return None
    k = _cv2.getStructuringElement(_cv2.MORPH_ELLIPSE, (5, 5))
    inner = _cv2.erode(stroke_mask, k, iterations=2)
    outer = _cv2.dilate(stroke_mask, k, iterations=4)
    ring = outer & ~inner
    if not ring.any():
        return None
    bg = img[ring > 0].astype(_np.float64).mean(axis=0)  # BGR 均值
    stroke = img[stroke_mask > 0].astype(_np.float64)
    diff = _np.abs(stroke - bg).mean(axis=1)  # 每像素 BGR 差
    bg_luma = bg.mean() + 1e-3
    return float(diff.mean()) / bg_luma


def _auto_should_fallback_to_ai(orig_img, out_img, regions, opencv_detail) -> bool:
    """智能档（engine=auto）回落判断：OpenCV 没去干净（半透明/浅底）时改走 AI（LaMa）。

    判据（背景无关，避免照片纹理误触发）：
    1) OpenCV 完全没检出水印（action='kept_original'）→ 多半是浅/半透明 → 回落 LaMa；
    2) OpenCV 修过，但原图上该水印笔画与周边背景对比弱（opacity < _AUTO_FALLBACK_OPACITY，
       即半透明）→ 回落 LaMa；实色水印对比强 → 不回落。
    返回 True 表示应改用 AI（LaMa）重跑；False 表示 OpenCV 结果已够好。
    """
    if orig_img is None or not regions:
        return False
    if opencv_detail.get("action") == "kept_original":
        return True
    mask, _ = plan_image_repair(orig_img, regions, "auto")
    if not mask.any():
        return False
    op = _estimate_watermark_opacity(orig_img, mask)
    if op is None:
        return False
    return op < _AUTO_FALLBACK_OPACITY


# ------------------------------------------------------------------ 图片去水印

def image_inpaint(src_path, dst_path, regions, method: str = "ns", radius: int = 3,
                  quality: str = "auto") -> Path:
    """对上传图片做区域去水印，结果写入 dst_path（保留原扩展名）。

    区域 regions 为归一化区域列表 [{"x","y","w","h","op"}]（0..1，op=add/subtract），
    兼容传入单个区域 dict。至少需要一个有效 add 区域。
    method: telea | ns（仅 quality='legacy' 时生效）；radius: inpaint 半径（建议 1..10）；
    quality: auto（智能三路分流，默认）| legacy（整块 inpaint，旧行为）。
    """
    return image_inpaint_ex(src_path, dst_path, regions, method, radius, quality)[0]

# ------------------------------------------------------------------ PDF 去水印

def pdf_remove_annotations(src_path, dst_path) -> int:
    """删除 PDF 中的 Watermark 型注释（无损，保留文字与矢量内容）。返回删除数量。"""
    if not pdf_available():
        raise RuntimeError("PyMuPDF 未安装，PDF 去水印不可用")
    doc = _fitz.open(str(src_path))
    try:
        removed = 0
        for page in doc:
            for annot in list(page.annots() or []):
                # annot.type 为 (subtype_int, subtype_name)
                if annot.type[0] == _fitz.PDF_ANNOT_WATERMARK:
                    page.delete_annot(annot)
                    removed += 1
        doc.save(str(dst_path), incremental=False, deflate=True)
    finally:
        doc.close()
    return removed

def pdf_raster_remove(src_path, dst_path, regions, method: str = "telea",
                      radius: int = 3, dpi: int = 150) -> Path:
    """栅格化去水印：每页渲染为图片 → 区域 inpaint → 重排合成新 PDF。

    适用于扫描件或水印内嵌在内容流中的 PDF。会丢失文字可选中性（按图片重排）。
    regions 为归一化区域列表（含 op），兼容单个区域 dict；dpi 控制栅格化清晰度。
    若无有效加选区域（mask 全 0），该页不做 inpaint（等效保留原页）。
    """
    if not (pdf_available() and available()):
        raise RuntimeError("PyMuPDF / OpenCV 未安装，PDF 栅格化去水印不可用")
    if isinstance(regions, dict):
        regions = [regions]
    if not regions:
        raise ValueError("栅格化去水印需要框选水印区域")
    doc = _fitz.open(str(src_path))
    try:
        new_doc = _fitz.open()
        for page in doc:
            pix = page.get_pixmap(dpi=dpi)
            img = _np.frombuffer(pix.samples, dtype=_np.uint8).reshape(pix.height, pix.width, pix.n)
            # fitz pixmap 为 RGB(A)，转 BGR 供 OpenCV 处理
            if pix.n == 4:
                img = _cv2.cvtColor(img, _cv2.COLOR_RGBA2BGR)
            else:
                img = _cv2.cvtColor(img, _cv2.COLOR_RGB2BGR)
            mask = _build_region_mask(regions, pix.width, pix.height)
            if mask.any():
                out = _inpaint_from_mask(img, mask, method, radius)
            else:
                out = img
            img_rgb = _cv2.cvtColor(out, _cv2.COLOR_BGR2RGB)
            out_pix = _fitz.Pixmap(_fitz.csRGB, pix.width, pix.height, img_rgb.tobytes())
            new_page = new_doc.new_page(width=page.rect.width, height=page.rect.height)
            new_page.insert_image(page.rect, pixmap=out_pix)
        new_doc.save(str(dst_path))
    finally:
        doc.close()
        new_doc.close()
    return Path(dst_path)
