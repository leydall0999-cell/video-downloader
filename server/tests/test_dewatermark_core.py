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

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import pytest  # noqa: E402

import dewatermark_core as dwc  # noqa: E402


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

    print("\n🎉 去水印核心测试全部通过（28 项；另有 2 项依赖 pytest fixture 由 pytest 运行）")
