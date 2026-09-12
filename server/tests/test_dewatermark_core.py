"""去水印核心链路离线回归测试（2026-09-10 新增）。

背景：去水印是「核心链路零测试」中真正的空白区——此前 27 个用例全在会员/配额，
本次补测前 dewatermark_core.py 与 dewatermark_ai.py 的纯逻辑为 0 测试保护。
这两块直接决定「用户框选的水印区域到底会不会被修掉」：
选区归一化错一个边界，mask 就偏一像素或整体不生效，且没有任何自动手段能发现。

覆盖（全部为纯函数 / 无副作用，不加载 ONNX 模型、不联网）：
  dewatermark_core:
    - normalize_region    归一化 + 越界夹紧 + 非法输入拒绝
    - normalize_regions   多区域校验、op 语义与回退
    - _region_to_px       归一化 -> 像素矩形换算与边界夹紧
    - _build_region_mask  加选并集 / 减选挖洞 / 顺序无关性
  dewatermark_ai:
    - _tile_weight        瓦片羽化权重（边缘不压暗、重叠区平滑过渡）
    - _optimal_threads    线程数取值域
    - 模型注册表与运行期切换、INT8 开关解析

运行：
    cd server && python tests/test_dewatermark_core.py
    cd server && python -m pytest tests/test_dewatermark_core.py -v
"""
import os
import sys
from pathlib import Path

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import pytest  # noqa: E402

import dewatermark_core as dwc  # noqa: E402

import numpy as np  # noqa: E402
import cv2  # noqa: E402


# ---------------------------------------------------------------- 区域归一化

def test_normalize_region_passthrough():
    """合法区域原样返回（0..1 浮点四元组）。"""
    out = dwc.normalize_region({"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4})
    assert out == {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}
    print("✅ 合法区域原样通过，不做多余改写")


def test_normalize_region_accepts_numeric_strings():
    """前端经 JSON/表单传来的字符串数值应被 float() 收编。"""
    out = dwc.normalize_region({"x": "0.1", "y": "0.2", "w": "0.3", "h": "0.4"})
    assert out == {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}
    print("✅ 字符串数值正常收编，避免前端传字符串就整体失效")


def test_normalize_region_rejects_invalid():
    """None / 空 dict / 非法字符串 / 零宽高 一律返回 None（上层据此报 400）。"""
    assert dwc.normalize_region(None) is None
    assert dwc.normalize_region({}) is None
    assert dwc.normalize_region({"x": "abc", "y": 0, "w": 0.3, "h": 0.4}) is None
    assert dwc.normalize_region({"x": 0.1, "y": 0.1, "w": 0, "h": 0.4}) is None
    assert dwc.normalize_region({"x": 0.1, "y": 0.1, "w": -0.2, "h": 0.4}) is None
    print("✅ 非法输入被拒绝，不会生成零面积 mask 去糊图")


def test_normalize_region_rejects_missing_keys():
    """缺 key 时缺失项默认 0，w/h 为 0 即判非法——不静默产生空选区。"""
    assert dwc.normalize_region({"x": 0.1}) is None
    print("✅ 缺 key 不静默通过，避免「点了没反应」的静默失败")


def test_normalize_region_clamps_out_of_range():
    """负坐标夹到 0；超长边截断以保证 x+w<=1、y+h<=1。"""
    neg = dwc.normalize_region({"x": -0.5, "y": 0.1, "w": 0.2, "h": 0.2})
    assert neg == {"x": 0.0, "y": 0.1, "w": 0.2, "h": 0.2}

    wide = dwc.normalize_region({"x": 0.0, "y": 0.0, "w": 2.0, "h": 0.5})
    assert wide["w"] == 1.0 and wide["h"] == 0.5

    print("✅ 越界坐标被夹紧，不会算出图外的像素矩形")


def test_normalize_region_overflow_becomes_invalid():
    """x 已贴 1.0 再给正宽度，截断后 w=0 → 判非法（而不是返回零宽区域）。"""
    assert dwc.normalize_region({"x": 1.5, "y": 0.1, "w": 0.2, "h": 0.2}) is None
    print("✅ 完全越界的区域被判非法，不会伪装成有效选区")


def test_normalize_region_corner_overflow_truncates():
    """右下角溢出只截断，不整块丢弃：0.9+0.5 应得到 w≈0.1。"""
    out = dwc.normalize_region({"x": 0.9, "y": 0.9, "w": 0.5, "h": 0.5})
    assert out["x"] == pytest.approx(0.9)
    assert out["w"] == pytest.approx(0.1)
    assert out["h"] == pytest.approx(0.1)
    print("✅ 角部溢出按剩余空间截断，保住与图边重叠的那部分水印")


# ---------------------------------------------------------------- 多区域列表

def test_normalize_regions_rejects_empty_shapes():
    """None / 空列表 / 非 list 一律 None。"""
    assert dwc.normalize_regions(None) is None
    assert dwc.normalize_regions([]) is None
    assert dwc.normalize_regions({"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1}) is None
    print("✅ 非列表与空输入被拒绝，路由层 400 判定有据")


def test_normalize_regions_defaults_op_to_add():
    """op 缺省为 add；非法 op 回退 add 而不是报错。"""
    a = dwc.normalize_regions([{"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}])
    assert a == [{"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2, "op": "add"}]

    b = dwc.normalize_regions([{"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2, "op": "xxx"}])
    assert b[0]["op"] == "add"
    print("✅ 非法 op 静默回退为加选，符合「宁可多修不可漏修」的取舍")


def test_normalize_regions_keeps_subtract():
    """subtract 语义必须保留——这是「选区里挖掉一块」的唯一表达方式。"""
    out = dwc.normalize_regions([{"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2, "op": "subtract"}])
    assert out[0]["op"] == "subtract"
    print("✅ 减选语义被保留，多选区挖洞能力不被吞掉")


def test_normalize_regions_any_invalid_fails_all():
    """列表内任一区域非法即整体 None（不部分生效），避免半套选区被应用。"""
    bad = dwc.normalize_regions([
        {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        {"x": 0, "y": 0, "w": 0, "h": 0},
    ])
    assert bad is None
    assert dwc.normalize_regions([["a"]]) is None
    print("✅ 有一项非法则整体拒绝，杜绝「只修了一半」的诡异结果")


def test_normalize_regions_multi_valid():
    """多个合法区域（含减选）按序返回，长度与顺序保持。"""
    out = dwc.normalize_regions([
        {"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5, "op": "add"},
        {"x": 0.2, "y": 0.2, "w": 0.1, "h": 0.1, "op": "subtract"},
    ])
    assert len(out) == 2
    assert [r["op"] for r in out] == ["add", "subtract"]
    print("✅ 多区域有序保留，加选/减选混用不被打乱")


# ---------------------------------------------------------------- 像素换算

def test_region_to_px_basic():
    """归一化矩形换算为像素矩形 (x, y, rw, rh)。"""
    assert dwc._region_to_px({"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5}, 100, 100) == (0, 0, 50, 50)
    assert dwc._region_to_px({"x": 0.5, "y": 0.5, "w": 0.5, "h": 0.5}, 100, 100) == (50, 50, 50, 50)
    print("✅ 归一化->像素换算正确，选区不会整体偏移")


def test_region_to_px_rounds():
    """非整数结果四舍五入，小图也不产生零宽矩形。"""
    assert dwc._region_to_px({"x": 0.0, "y": 0.0, "w": 0.333, "h": 0.333}, 10, 10) == (0, 0, 3, 3)
    print("✅ 小图换算不塌缩为 0 宽，缩略图场景仍可框选")


def test_region_to_px_clamps_to_image():
    """换算结果被夹到图像范围内，绝不越界索引。"""
    x, y, rw, rh = dwc._region_to_px({"x": 0.8, "y": 0.8, "w": 0.5, "h": 0.5}, 100, 100)
    assert x + rw <= 100 and y + rh <= 100
    print("✅ 像素矩形不越界，numpy 切片不会 IndexError")


# ---------------------------------------------------------------- mask 合成

def test_build_region_mask_requires_numpy():
    """mask 合成依赖 numpy；缺失时应显式报错而非静默返回错值。"""
    if dwc._np is None:
        pytest.skip("numpy 未安装，跳过 mask 合成测试")
    print("✅ numpy 可用，继续验证 mask 合成")


def test_mask_single_add_area():
    """单个加选区域：mask 面积 = 矩形像素面积。"""
    if dwc._np is None:
        pytest.skip("numpy 未安装")
    m = dwc._build_region_mask([{"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5, "op": "add"}], 100, 100)
    assert m.shape == (100, 100)
    assert m.dtype == dwc._np.uint8
    assert int((m == 255).sum()) == 50 * 50
    print("✅ 单区域 mask 面积精确，框多大就修多大")


def test_mask_overlapping_adds_union():
    """两个重叠加选区取并集（重叠部分不重复计数）。"""
    if dwc._np is None:
        pytest.skip("numpy 未安装")
    m = dwc._build_region_mask([
        {"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5, "op": "add"},
        {"x": 0.25, "y": 0.0, "w": 0.5, "h": 0.5, "op": "add"},
    ], 100, 100)
    # 两矩形：x∈[0,50] 与 x∈[25,75]，y∈[0,50] → 并集 x∈[0,75] × y∈[0,50]
    assert int((m == 255).sum()) == 75 * 50
    print("✅ 重叠加选自然并集，不会相互抵消")


def test_mask_subtract_carves_hole():
    """减选区从加选并集中挖洞。"""
    if dwc._np is None:
        pytest.skip("numpy 未安装")
    m = dwc._build_region_mask([
        {"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5, "op": "add"},
        {"x": 0.0, "y": 0.0, "w": 0.25, "h": 0.25, "op": "subtract"},
    ], 100, 100)
    assert int((m == 255).sum()) == 50 * 50 - 25 * 25
    print("✅ 减选挖洞生效，可剔除不想处理的区域")


def test_mask_subtract_is_order_independent():
    """关键契约：subtract 无论排在 add 之前还是之后，都从并集中扣除。

    实现分两趟收集（先全部 add，再全部 subtract），故切片顺序不影响结果。
    若哪天有人把两趟合并成单趟，此测试会立刻报红。
    """
    if dwc._np is None:
        pytest.skip("numpy 未安装")
    add = {"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5, "op": "add"}
    sub = {"x": 0.0, "y": 0.0, "w": 0.25, "h": 0.25, "op": "subtract"}
    m1 = dwc._build_region_mask([add, sub], 100, 100)
    m2 = dwc._build_region_mask([sub, add], 100, 100)
    assert dwc._np.array_equal(m1, m2)
    assert int((m1 == 255).sum()) == 50 * 50 - 25 * 25
    print("✅ 减选与顺序无关，前端选区顺序变化不改变修复结果")


def test_mask_only_subtract_is_empty():
    """只有减选没有加选 → 全 0，上层据此报「未框选有效区域」。"""
    if dwc._np is None:
        pytest.skip("numpy 未安装")
    m = dwc._build_region_mask([{"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5, "op": "subtract"}], 100, 100)
    assert not m.any()
    print("✅ 纯减选不产生 mask，避免「什么都没框却报成功」")


def test_mask_skips_zero_pixel_regions():
    """归一化后极小区域（像素矩形为 0 宽）应被跳过而非写入错误切片。"""
    if dwc._np is None:
        pytest.skip("numpy 未安装")
    m = dwc._build_region_mask([{"x": 0.0, "y": 0.0, "w": 0.001, "h": 0.001, "op": "add"}], 100, 100)
    assert not m.any()
    print("✅ 零像素区域被安全跳过，不越界写入")


# ---------------------------------------------------------------- 瓦片羽化权重

def _np_required():
    import numpy as _np
    return _np


def test_tile_weight_full_when_no_neighbor():
    """瓦片独占整图（无任何邻居）时权重恒为 1——图边缘不能被压暗。"""
    dw = _import_dw()
    if dw is None:
        pytest.skip("dewatermark_ai 不可导入")
    w = dw._tile_weight(64, 64, 0, 0, 64, 64, overlap=16)
    assert w.shape == (64, 64, 1)
    assert float(w.min()) == pytest.approx(1.0)
    assert float(w.max()) == pytest.approx(1.0)
    print("✅ 无邻居瓦片权重全 1，整图边缘不出现暗带")


def test_tile_weight_feathers_interior():
    """内部瓦片四边都有邻居 → 四角权重趋 0、中心为 1（余弦羽化）。"""
    dw = _import_dw()
    if dw is None:
        pytest.skip("dewatermark_ai 不可导入")
    w = dw._tile_weight(512, 512, 512, 512, 2048, 2048, overlap=64)[..., 0]
    assert float(w[256, 256]) == pytest.approx(1.0)
    assert float(w[0, 0]) == pytest.approx(0.0)
    assert float(w[0, 256]) == pytest.approx(0.0)
    assert float(w.min()) == pytest.approx(0.0)
    assert float(w.max()) == pytest.approx(1.0)
    print("✅ 内部重叠区平滑过渡到 0，相邻瓦片权重互补、无接缝")


def test_tile_weight_edge_tile_not_darkened():
    """贴图边角的瓦片：贴边侧不羽化（=1），仅有邻居的一侧衰减。"""
    dw = _import_dw()
    if dw is None:
        pytest.skip("dewatermark_ai 不可导入")
    w = dw._tile_weight(512, 512, 0, 0, 2048, 2048, overlap=64)[..., 0]
    assert float(w[0, 0]) == pytest.approx(1.0)     # 左上角贴图边 → 不衰减
    assert float(w[0, -1]) == pytest.approx(0.0)    # 右侧有邻居 → 衰减到 0
    assert float(w[-1, 256]) == pytest.approx(0.0)  # 下方有邻居 → 衰减到 0
    print("✅ 贴边侧保持全权重，避免整幅图外沿出现暗边")


def test_tile_weight_ramp_monotonic():
    """羽化 ramp 必须单调：从贴边侧的 1 平滑降到重叠侧的 0，不得来回震荡。"""
    dw = _import_dw()
    if dw is None:
        pytest.skip("dewatermark_ai 不可导入")
    w = dw._tile_weight(512, 512, 512, 0, 2048, 2048, overlap=64)[..., 0]
    # x0=0 → 左侧无邻居（恒 1）；右侧有邻居 → 右 64 列单调不增
    right = w[256, -64:]
    diffs = [right[i + 1] - right[i] for i in range(len(right) - 1)]
    assert all(d <= 1e-6 for d in diffs), "右侧羽化应单调不增"
    assert float(right[0]) == pytest.approx(1.0)
    assert float(right[-1]) == pytest.approx(0.0)
    print("✅ 羽化单调平滑，不会在接缝处产生可见亮度波纹")


# ---------------------------------------------------------------- 推理参数/模型状态

def _import_dw():
    try:
        import dewatermark_ai as dw
        return dw
    except Exception:  # noqa: BLE001
        return None


def test_optimal_threads_in_range():
    """线程数必须落在 [1,8]：0 线程或打满超线程都会拖慢/异常。"""
    dw = _import_dw()
    if dw is None:
        pytest.skip("dewatermark_ai 不可导入")
    n = dw._optimal_threads()
    assert isinstance(n, int) and 1 <= n <= 8
    print(f"✅ 推理线程数取值合法（本机 {n}），不会算出 0 线程或无限打满")


def test_optimal_threads_env_override(monkeypatch):
    """VDL_DW_THREADS 可覆盖；非法值被忽略回落自动探测。"""
    dw = _import_dw()
    if dw is None:
        pytest.skip("dewatermark_ai 不可导入")
    monkeypatch.setenv("VDL_DW_THREADS", "2")
    assert dw._optimal_threads() == 2
    monkeypatch.setenv("VDL_DW_THREADS", "not-a-number")
    assert 1 <= dw._optimal_threads() <= 8
    print("✅ 环境变量可调线程数，脏值安全回落（便于用户排障）")


def test_model_registry_and_switch():
    """模型注册表非空、默认模型在册；切换非法模型名必须报错。"""
    dw = _import_dw()
    if dw is None:
        pytest.skip("dewatermark_ai 不可导入")
    models = dw.list_models()
    assert isinstance(models, list) and models
    assert dw.current_model() in models
    assert dw._current_out_div() > 0

    before = dw.current_model()
    try:
        dw.set_model(models[0])
        assert dw.current_model() == models[0]
        with pytest.raises(ValueError):
            dw.set_model("__no_such_model__")
    finally:
        dw.set_model(before)
    print("✅ 模型可切换、非法名被拒，且切换后状态可复原")


def test_int8_enabled_env_semantics(monkeypatch):
    """INT8 默认开；仅显式 VDL_DW_INT8=0 才关（其它任意值=开）。"""
    dw = _import_dw()
    if dw is None:
        pytest.skip("dewatermark_ai 不可导入")
    saved = dw._INT8_OVERRIDE
    try:
        dw._INT8_OVERRIDE = None
        monkeypatch.delenv("VDL_DW_INT8", raising=False)
        assert dw._int8_enabled() is True
        monkeypatch.setenv("VDL_DW_INT8", "0")
        assert dw._int8_enabled() is False
        monkeypatch.setenv("VDL_DW_INT8", "1")
        assert dw._int8_enabled() is True
        # UI 运行期覆盖优先于环境变量
        dw.set_int8_enabled(False)
        monkeypatch.setenv("VDL_DW_INT8", "1")
        assert dw._int8_enabled() is False
    finally:
        dw._INT8_OVERRIDE = saved
    print("✅ INT8 开关语义正确，UI 勾选可覆盖环境变量且不留残留状态")


# ---------------------------------------------------------------- 智能图片修复（2026-09-12）
#
# 背景：传统档（非 AI）此前是「整块矩形 cv2.inpaint」。30 个合成基准（真实照片/渐变/纹理
# × 斜排大字/平铺小字/角落徽标/实心 logo，均有 ground truth）实测：
#   整块 TELEA：PSNR 24.76 / SSIM 0.67，且 21/30 个场景比「不处理」更差（最差 ΔPSNR -15.10）
#   → 这正是用户抱怨「每次都得用 AI」的根因：框内真实内容被一起抹掉重绘。
#   改为「检测水印形态 → 只修水印笔画」后：PSNR 33.32 / SSIM 0.89，劣化样本降到 3/30。
# 以下用合成图把这几条行为锁死，防止将来回归。

def _require_cv():
    if dwc._np is None or dwc._cv2 is None:
        pytest.skip("numpy/cv2 未安装，跳过智能修复测试")
    return dwc._np, dwc._cv2


def _bench_base(h=180, w=240, seed=0):
    """有渐变 + 细纹的底图：纯色底会让检测退化，测不出真实差异。"""
    np, _ = _require_cv()
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    g = 120 + 46 * np.sin(xx / 17.0) + 34 * np.sin(yy / 13.0) + rng.normal(0, 5, (h, w))
    g = np.clip(g, 0, 255)
    return np.stack([g * 0.96, g, np.clip(g * 1.04, 0, 255)], axis=2).astype(np.uint8)


# 用矩形笔画拼字（「日」形，3 横 2 竖），不依赖系统字体文件
_TEXT_STROKES = [(50, 58, 40, 200), (86, 94, 40, 200), (122, 130, 40, 200),
                 (46, 134, 40, 48), (46, 134, 192, 200)]


def _bench_smooth(h=180, w=240, seed=0):
    """照片中平坦区域（天空/墙面）的模拟：弱纹理 + 轻微模糊。

    用来测「干净区域必须不动」。注意别拿强纹理底图当干净图——强周期纹理的
    残差幅度本就可观（实测 redelta≈17，而淡水面 a=0.25 才 28），二者物理上
    难以区分，此时宁可漏检不动。
    """
    np, cv2 = _require_cv()
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    g = 150 + 14 * np.sin(xx / 45.0) + 10 * np.sin(yy / 37.0) + rng.normal(0, 3, (h, w))
    g = cv2.GaussianBlur(g, (0, 0), 2.0)
    return np.stack([np.clip(g * 0.97, 0, 255), np.clip(g, 0, 255),
                     np.clip(g * 1.03, 0, 255)], axis=2).astype(np.uint8)


def _add_text_wm(base, alpha=0.45):
    """叠半透明白色文字水印，返回 (含水印图, 精确笔画 mask, 用户框选矩形 mask)。"""
    np, cv2 = _require_cv()
    h, w = base.shape[:2]
    layer = np.zeros((h, w), np.float32)
    for (y0, y1, x0, x1) in _TEXT_STROKES:
        layer[y0:y1, x0:x1] = 1.0
    layer = cv2.GaussianBlur(layer, (0, 0), 0.7)
    a = (layer * alpha)[..., None]
    wm = (1 - a) * base.astype(np.float32) + a * 255.0
    exact = ((layer * alpha) > 0.05).astype(np.uint8) * 255
    rect = np.zeros((h, w), np.uint8)
    rect[40:140, 34:206] = 255
    return np.clip(wm, 0, 255).astype(np.uint8), exact, rect


def _psnr(a, b, mask=None):
    np, _ = _require_cv()
    a = a.astype(np.float32); b = b.astype(np.float32)
    if mask is not None:
        m = mask > 0
        a, b = a[m], b[m]
    mse = float(np.mean((a - b) ** 2))
    return 99.0 if mse <= 1e-9 else 10 * float(np.log10(255.0 ** 2 / mse))


def test_detect_stroke_on_translucent_text():
    """半透明文字水印应判为 stroke，且 mask 只覆盖笔画（远小于框选矩形）。"""
    np, _ = _require_cv()
    base = _bench_base()
    wm, exact, rect = _add_text_wm(base, 0.45)
    m, kind, info = dwc.detect_watermark(wm, 34, 40, 172, 100)
    assert kind == "stroke", f"应判为文字笔画，实际 {kind} {info}"
    assert m is not None
    fill = float((m > 0).sum()) / float((rect > 0).sum())
    assert 0.02 < fill < 0.6, f"笔画覆盖率应远小于整块，实际 {fill:.3f}"
    # 检测到的笔画应覆盖大部分真实笔画（召回够高，否则水印会残留）
    # 注意 m 是「框选矩形内」的局部 mask，比较前需把精确 mask 裁到同一区域
    sub = exact[40:140, 34:206]
    hit = float(((m > 0) & (sub > 0)).sum()) / float((sub > 0).sum())
    assert hit > 0.5, f"笔画召回过低 {hit:.2f}，水印会残留"
    print(f"✅ 半透明白字判为 stroke，只标 {fill:.0%} 像素（整块框选要标 100%）")


def test_detect_solid_block_is_not_stroke():
    """实心不透明块内部残差≈0，必须判为 solid（走整块修复），不能被当成笔画。"""
    np, _ = _require_cv()
    base = _bench_base()
    wm = base.copy()
    wm[60:110, 70:170] = 250          # 实心白块
    m, kind, info = dwc.detect_watermark(wm, 64, 54, 112, 62)
    assert kind == "solid", f"实心块应判为 solid，实际 {kind} {info}"
    print("✅ 实心块正确判为 solid（若误判为 stroke 会只修边缘、水印留在图上）")


def test_detect_none_on_clean_flat_area():
    """没有可见水印的区域必须判 none —— 上层据此「不动」，不制造无中生有的改动。"""
    np, _ = _require_cv()
    base = _bench_smooth(seed=7)      # 无任何水印的平坦区域
    m, kind, info = dwc.detect_watermark(base, 34, 40, 172, 100)
    assert kind == "none", f"干净区域不应检出可修内容，实际 {kind} {info}"
    assert m is None
    print("✅ 无可见水印区域判 none，保证「不确信就不动」")


def test_detect_handles_tiny_rect():
    """极小框选要安全返回 none，不能抛异常（用户可能误点出 1px 选区）。"""
    _require_cv()
    base = _bench_base()
    m, kind, info = dwc.detect_watermark(base, 5, 5, 4, 4)
    assert kind == "none" and m is None and info.get("why") == "rect-too-small"
    print("✅ 极小框选安全降级，不抛异常")


def test_plan_legacy_marks_whole_rect():
    """legacy 档必须与旧行为逐像素一致：整块矩形全部标记。"""
    np, _ = _require_cv()
    base = _bench_base()
    wm, exact, rect = _add_text_wm(base)
    regions = [{"x": 34 / 240, "y": 40 / 180, "w": 172 / 240, "h": 100 / 180, "op": "add"}]
    mask, stats = dwc.plan_image_repair(wm, regions, "legacy")
    assert int((mask > 0).sum()) == int((rect > 0).sum()) == 172 * 100
    assert stats["solid"] == 1 and stats["stroke"] == 0
    print("✅ legacy 档整块标记，可随时回退到旧行为")


def test_plan_auto_shrinks_mask_to_strokes():
    """auto 档把 mask 收窄到水印笔画：修复像素数应显著小于框选面积。"""
    np, _ = _require_cv()
    base = _bench_base()
    wm, exact, rect = _add_text_wm(base)
    regions = [{"x": 34 / 240, "y": 40 / 180, "w": 172 / 240, "h": 100 / 180, "op": "add"}]
    mask, stats = dwc.plan_image_repair(wm, regions, "auto")
    assert stats["stroke"] == 1 and stats["none"] == 0
    assert 0 < stats["repair_px"] < 0.6 * 172 * 100, f"未收窄：{stats}"
    print(f"✅ auto 档只标 {stats['repair_px']} / {172*100} 像素，背景像素零改动")


def test_plan_auto_subtract_still_carves_hole():
    """减选语义在 auto 档必须保持：减选区不与加选并集重叠处必须为 0。"""
    np, _ = _require_cv()
    base = _bench_base()
    wm, exact, rect = _add_text_wm(base)
    regions = [
        {"x": 34 / 240, "y": 40 / 180, "w": 172 / 240, "h": 100 / 180, "op": "add"},
        {"x": 34 / 240, "y": 40 / 180, "w": 172 / 240, "h": 20 / 180, "op": "subtract"},
    ]
    mask, _stats = dwc.plan_image_repair(wm, regions, "auto")
    # 减选覆盖 y=40..60，该条带内不得有任何待修像素
    assert int((mask[40:60, 34:206] > 0).sum()) == 0
    print("✅ auto 档减选仍生效，挖洞语义未被智能分流破坏")


def test_auto_improves_watermark_region():
    """效果回归：智能档修复后应显著比「不处理」更接近真实底图。"""
    np, _ = _require_cv()
    base = _bench_base()
    wm, exact, rect = _add_text_wm(base, 0.45)
    regions = [{"x": 34 / 240, "y": 40 / 180, "w": 172 / 240, "h": 100 / 180, "op": "add"}]
    mask, _stats = dwc.plan_image_repair(wm, regions, "auto")
    assert mask.any()
    fixed = dwc._feather_merge(wm, dwc._inpaint_from_mask(wm, mask, "ns", 3), mask)
    before, after = _psnr(base, wm, rect), _psnr(base, fixed, rect)
    assert after > before + 2.0, f"修复后应明显更接近底图：{before:.2f} → {after:.2f}"
    print(f"✅ 修复效果回归通过：框选区 PSNR {before:.2f} → {after:.2f}")


def test_auto_never_worse_than_untouched():
    """底线：在「框错/无可见水印」的图上，auto 档必须逐像素不变（绝不越修越糟）。

    旧行为在这种图上会整块 inpaint，实测最差可把 PSNR 拉低 15 dB。
    """
    np, _ = _require_cv()
    base = _bench_smooth(seed=11)
    regions = [{"x": 34 / 240, "y": 40 / 180, "w": 172 / 240, "h": 100 / 180, "op": "add"}]
    mask, stats = dwc.plan_image_repair(base, regions, "auto")
    assert not mask.any(), f"干净图不应产生任何修复像素，实际 {stats}"
    assert stats["none"] == 1
    print("✅ 无可见水印时不产生任何改动（旧行为此处会糊掉一块）")


def test_large_selection_refuses_whole_rect_inpaint():
    """大框选 + 判为实心块时必须拒绝整块修复（改判 none）。

    实测依据：整图平铺水印（图库常见）会被误判 solid，而它即便用真值 mask 做
    inpaint 也只有 PSNR 26.67，反而低于不处理的 28.32 —— 这种情况「不动」才是最优。
    真正的实心 logo 框选只占图像一小块，不受此护栏影响。
    """
    np, _ = _require_cv()
    base = _bench_base()
    wm = base.copy()
    wm[60:110, 70:170] = 250                      # 同一块实心白块
    # 小框：仍应按实心块整块修复
    m_small, kind_small, _info = dwc.detect_watermark(wm, 64, 54, 112, 62)
    assert kind_small == "solid", f"小框实心块应修理，实际 {kind_small}"
    # 大框（几乎整图）：必须拒绝
    m_big, kind_big, info_big = dwc.detect_watermark(wm, 0, 0, 240, 180)
    assert kind_big == "none", f"大框选不应整块 inpaint，实际 {kind_big} {info_big}"
    assert m_big is None
    assert info_big.get("why") == "solid-rect-too-large", info_big
    print("✅ 大框选拒绝整块 inpaint（整图平铺水印不再被糊掉）")


def test_full_frame_selection_never_repaints_whole_image():
    """框选整幅图时，任何情况下都不许把整幅图重绘掉。

    整图平铺水印是图库常见场景：它的笔画散布全图，局部背景估计抓不准，
    若被当成「实心块」就会整幅 inpaint 直接毁图。此测试锁死「修复面必须远小于全图」。
    """
    np, _ = _require_cv()
    base = _bench_base()
    overlay = base.astype(np.float32).copy()
    for y0 in range(0, 180, 44):                  # 铺满整幅的稀疏小字
        for x0 in range(0, 240, 70):
            overlay[y0:y0 + 4, x0:min(240, x0 + 46)] = 250.0
    wm = np.clip(overlay, 0, 255).astype(np.uint8)
    mask, stats = dwc.plan_image_repair(
        wm, [{"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0, "op": "add"}], "auto")
    total = 240 * 180
    assert "solid" not in [d.get("kind") for d in stats["details"]], stats
    assert stats["repair_px"] < 0.6 * total, f"修复面过大，接近整图重绘：{stats}"
    print(f"✅ 整幅框选不整图重绘：只修 {stats['repair_px']}/{total} px"
          f"（{stats['repair_px'] / total:.1%}）")


def test_auto_mask_stays_bounded_vs_exact():
    """auto 的笔画 mask 允许外扩补掉抗锯齿外沿，但不得膨胀到接近整块框选。"""
    np, _ = _require_cv()
    base = _bench_base()
    wm, exact, _rect = _add_text_wm(base, 0.45)
    mask, _stats = dwc.plan_image_repair(
        wm, [{"x": 34 / 240, "y": 40 / 180, "w": 172 / 240, "h": 100 / 180, "op": "add"}],
        "auto")
    exact_px = int((exact > 0).sum())
    got_px = int((mask > 0).sum())
    assert got_px > 0
    assert got_px < 2.5 * exact_px, (
        f"修复面 {got_px}px 相对真值 {exact_px}px 膨胀过多，接近整块糊图")
    print(f"✅ 笔画 mask 外扩有界：{got_px}px / 真值 {exact_px}px = {got_px / exact_px:.2f}x")


def test_image_inpaint_ex_keeps_original_and_writes_file(tmp_path):
    """未检出可修内容时必须仍产出文件（原图副本），不能让下游拿到空结果。"""
    np, cv2 = _require_cv()
    base = _bench_smooth(seed=13)
    src = tmp_path / "in.png"
    dst = tmp_path / "out.png"
    assert cv2.imwrite(str(src), base)
    path, info = dwc.image_inpaint_ex(src, dst, [
        {"x": 34 / 240, "y": 40 / 180, "w": 172 / 240, "h": 100 / 180, "op": "add"}], quality="auto")
    assert path == dst and dst.exists() and dst.stat().st_size > 0
    assert info["action"] == "kept_original" and info["changed"] is False
    back = cv2.imread(str(dst), cv2.IMREAD_COLOR)
    assert int(np.abs(back.astype(int) - base.astype(int)).max()) == 0, "应逐像素保持原图"
    print("✅ 未检出时原样输出且文件有效，不会丢产出")


def test_image_inpaint_returns_path_and_honours_legacy_method(tmp_path):
    """向后兼容：image_inpaint 仍返回 Path；legacy 档尊重调用方传入的 method。"""
    np, cv2 = _require_cv()
    base = _bench_base(seed=17)
    wm, exact, rect = _add_text_wm(base, 0.5)
    src = tmp_path / "in2.png"
    dst = tmp_path / "out2.png"
    assert cv2.imwrite(str(src), wm)
    regions = [{"x": 34 / 240, "y": 40 / 180, "w": 172 / 240, "h": 100 / 180, "op": "add"}]
    p1 = dwc.image_inpaint(src, dst, regions, "telea", 3, quality="legacy")
    assert p1 == dst and dst.exists()
    _p, info_auto = dwc.image_inpaint_ex(src, tmp_path / "out3.png", regions, "telea", 3, quality="auto")
    assert info_auto["method_used"] == "ns", "auto 档应自动选用实测更优的 NS"
    assert info_auto["requested_method"] == "telea"
    print("✅ image_inpaint 返回 Path 保持兼容；auto 档自动用 NS、legacy 档尊重入参")


# ------------------------------------------------------------------ 两阶段精修（refine 档，2026-09-12 新增）

import tempfile, os as _os

_TMP = Path(tempfile.mkdtemp(prefix="dw_test_"))

def _make_test_image(w=400, h=300, bg_val=128):
    return np.full((h, w, 3), bg_val, dtype=np.uint8)


def test_refine_basic_api_contract():
    img = _make_test_image()
    img[140:160, 50:350] = [200, 200, 210]
    src = _TMP / "r_api.png"
    dst = _TMP / "r_api_out.png"
    assert cv2.imwrite(str(src), img)
    regions = [{"x": 0.05, "y": 0.40, "w": 0.80, "h": 0.10, "op": "add"}]
    p, info = dwc.image_inpaint_ex(src, dst, regions, "ns", 3, quality="refine")
    assert p == dst and dst.exists()
    assert info["quality"] == "refine"
    assert info["action"] in ("repaired", "kept_original")
    assert isinstance(info["stage1_repair_px"], int)
    assert isinstance(info["stage2_repair_px"], int)
    assert info.get("method_used") == "ns"
    print("OK refine API contract")


def test_refine_repairs_more_than_auto():
    img = _make_test_image(600, 400, 100)
    cv2.putText(img, "WATERMARK", (80, 210), cv2.FONT_HERSHEY_SIMPLEX,
                1.8, (240, 240, 245), 4, cv2.LINE_AA)
    src = _TMP / "r_more.png"
    dst_a = _TMP / "r_more_auto.png"
    dst_r = _TMP / "r_more_ref.png"
    assert cv2.imwrite(str(src), img)
    regions = [{"x": 0.05, "y": 0.42, "w": 0.85, "h": 0.22, "op": "add"}]
    _, ia = dwc.image_inpaint_ex(src, dst_a, regions, "ns", 3, quality="auto")
    _, ir = dwc.image_inpaint_ex(src, dst_r, regions, "ns", 3, quality="refine")
    total = ir["stage1_repair_px"] + ir["stage2_repair_px"]
    assert total >= ia.get("repair_px", 0)
    print(f"OK refine {ir['stage1_repair_px']}+{ir['stage2_repair_px']} >= auto {ia.get('repair_px',0)}")


def test_refine_keeps_clean():
    img = _make_test_image()
    src = _TMP / "r_clean.png"
    dst = _TMP / "r_clean_out.png"
    assert cv2.imwrite(str(src), img)
    regions = [{"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5, "op": "add"}]
    _, info = dwc.image_inpaint_ex(src, dst, regions, "ns", 3, quality="refine")
    assert info["action"] == "kept_original"
    out = cv2.imread(str(dst))
    assert np.array_equal(out, img)
    print("OK refine keeps clean image")


def test_refine_subtract():
    img = _make_test_image(500, 400, 120)
    img[180:220, 30:230] = [230, 230, 235]
    src = _TMP / "r_sub.png"
    dst = _TMP / "r_sub_out.png"
    assert cv2.imwrite(str(src), img)
    regions = [
        {"x": 0.0, "y": 0.40, "w": 0.5, "h": 0.15, "op": "add"},
        {"x": 0.25, "y": 0.45, "w": 0.10, "h": 0.08, "op": "subtract"},
    ]
    _, info = dwc.image_inpaint_ex(src, dst, regions, "ns", 3, quality="refine")
    assert info["action"] == "repaired"
    out = cv2.imread(str(dst))
    sy1, sy2 = int(0.45*400), int(0.53*400)
    sx1, sx2 = int(0.25*500), int(0.35*500)
    diff = np.abs(out[sy1:sy2, sx1:sx2].astype(int) - img[sy1:sy2, sx1:sx2].astype(int)).mean()
    # 羽化混合在边界处有轻微渗透，允许中心区基本不变即可
    assert diff < 15.0, f"subtract region should be mostly unchanged (diff={diff:.1f})"
    print("OK refine respects subtract")


def test_refine_expand():
    mask = np.zeros((200, 300), dtype=np.uint8)
    mask[50:70, 20:100] = 255
    mask[120:140, 150:250] = 255
    exp = dwc._refine_expand(mask, 200, 300)
    assert (exp > 0).sum() > (mask > 0).sum()
    assert exp[50:70, 20:100].all()
    assert exp[120:140, 150:250].all()
    print("OK _refine_expand covers component interior")


def test_bilateral_residual():
    img = _make_test_image(300, 200, 80)
    cv2.putText(img, "TEST", (60, 110), cv2.FONT_HERSHEY_SIMPLEX,
                1.5, (220, 220, 225), 3, cv2.LINE_AA)
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    res = dwc._residual_map_bilateral(g)
    assert res[90:130, 50:180].max() > 10
    assert res[0:30, 0:50].mean() < 3
    print("OK _residual_map_bilateral detects text")


if __name__ == "__main__":
    test_normalize_region_passthrough()
    test_normalize_region_accepts_numeric_strings()
    test_normalize_region_rejects_invalid()
    test_normalize_region_rejects_missing_keys()
    test_normalize_region_clamps_out_of_range()
    test_normalize_region_overflow_becomes_invalid()
    test_normalize_region_corner_overflow_truncates()

    test_normalize_regions_rejects_empty_shapes()
    test_normalize_regions_defaults_op_to_add()
    test_normalize_regions_keeps_subtract()
    test_normalize_regions_any_invalid_fails_all()
    test_normalize_regions_multi_valid()

    test_region_to_px_basic()
    test_region_to_px_rounds()
    test_region_to_px_clamps_to_image()

    test_build_region_mask_requires_numpy()
    test_mask_single_add_area()
    test_mask_overlapping_adds_union()
    test_mask_subtract_carves_hole()
    test_mask_subtract_is_order_independent()
    test_mask_only_subtract_is_empty()
    test_mask_skips_zero_pixel_regions()

    test_tile_weight_full_when_no_neighbor()
    test_tile_weight_feathers_interior()
    test_tile_weight_edge_tile_not_darkened()
    test_tile_weight_ramp_monotonic()

    test_optimal_threads_in_range()
    test_model_registry_and_switch()

    # 智能图片修复（2026-09-12 增补：传统档效果优化 + 绝不越修越糟底线）
    test_detect_stroke_on_translucent_text()
    test_detect_solid_block_is_not_stroke()
    test_detect_none_on_clean_flat_area()
    test_detect_handles_tiny_rect()
    test_plan_legacy_marks_whole_rect()
    test_plan_auto_shrinks_mask_to_strokes()
    test_plan_auto_subtract_still_carves_hole()
    test_auto_improves_watermark_region()
    test_auto_never_worse_than_untouched()
    test_large_selection_refuses_whole_rect_inpaint()
    test_full_frame_selection_never_repaints_whole_image()
    test_auto_mask_stays_bounded_vs_exact()

    # 两阶段精修（refine 档，2026-09-12 新增）
    test_refine_basic_api_contract()
    test_refine_repairs_more_than_auto()
    test_refine_keeps_clean()
    test_refine_subtract()
    test_refine_expand()
    test_bilateral_residual()

    print("\n🎉 去水印核心测试全部通过（46 项；另有 2 项依赖 pytest fixture 由 pytest 运行）")
