"""AI 去水印（LaMa ONNX）推理前向链路离线回归测试（2026-09-11 新增）。

背景：此前的去水印测试只覆盖「选区归一化 / mask 合成 / 瓦片羽化权重 / 模型注册表」
等**纯函数**，**没有触碰真正跑模型的推理前向**：
    `_inpaint_tiles → _infer_tile(sess.run) → 除以 out_div → 余弦羽化拼接 →
     BGR 合成 → 写盘`
这条链路一旦回归（比如忘记 ÷out_div 把整图压成纯白、羽化权重把边缘压暗、
模型根本没被调用却报成功），用户框选的水印区域要么没修掉、要么整图被改坏，且
不报错。

真实 LaMa 权重 ~107MB，本测试**不下载任何权重**：monkeypatch `_get_session`
注入 duck-typed 假 session，把模型输出控成「仅由 mask 决定的确定性张量」，从而把
前向链路的数值契约锁死：

  - `_infer_tile` 必须做 CHW→HWC 转置 + ÷out_div + clip[0,1]（漏除会把图拉白）
  - `_inpaint_tiles` 单瓦片时融合结果必须 == 单瓦片推理（羽化权重全 1）
  - `ai_image_inpaint_core` 全图 mask → 输出整片被模型改写；
    部分 mask → 非水印区**逐字节不动**（前向绝不能污染未框区域）

运行：
    cd server && python tests/test_dewatermark_forward.py
    cd server && python -m pytest tests/test_dewatermark_forward.py -v
"""
import os
import sys
import types

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import numpy as np  # noqa: E402

import dewatermark_ai as dw  # noqa: E402
import dewatermark_core as dwc  # noqa: E402


# --------------------------------------------------------------------------- #
# 假 LaMa InferenceSession
#   - kind="identity_mask"：输出 = out_div * where(mask>0.5, 1, img)
#     （mask 处恒白、非 mask 处原样）——用于验证「mask 真的定位了修复区」
#   - kind="raw"：原样回吐 precomputed raw 张量（1,3,512,512），用于验证 _infer_tile 数学
# --------------------------------------------------------------------------- #
class FakeLamaSession:
    def __init__(self, kind="identity_mask", raw=None, out_div=255.0):
        self.kind = kind
        self.raw = raw
        self.out_div = out_div
        self.last_feeds = None

    def get_inputs(self):
        return [types.SimpleNamespace(name="image"), types.SimpleNamespace(name="mask")]

    def run(self, _, feeds):
        self.last_feeds = feeds
        if self.kind == "raw":
            return [np.asarray(self.raw, dtype=np.float32)]
        img = feeds["image"]        # (1,3,512,512) [0,1]
        mask = feeds["mask"]        # (1,1,512,512) [0,1]
        img_h = img[0].transpose(1, 2, 0)   # (512,512,3)
        mask_h = mask[0, 0]                 # (512,512)
        out = np.where(mask_h[..., None] > 0.5, 1.0, img_h)
        return [(out * self.out_div).transpose(2, 0, 1)[None].astype(np.float32)]


def _with_fake(session, fn):
    orig = dw._get_session
    dw._get_session = lambda name=None: session
    try:
        return fn()
    finally:
        dw._get_session = orig


# --------------------------------------------------------------------------- #
# 1) _infer_tile 数学契约（转置 + ÷out_div + clip）
# --------------------------------------------------------------------------- #
def test_infer_tile_transpose_divide_clip():
    """_infer_tile 必须把模型输出做 CHW→HWC、÷out_div、clip[0,1]。

    用 raw = 随机 (1,3,512,512)（含超界值），断言结果与
    clip(raw/out_div, 0, 1).transpose(1,2,0) 逐元素一致。
    """
    rng = np.random.RandomState(7)
    raw = (rng.randn(1, 3, 512, 512).astype(np.float32)) * 50.0  # 含 >out_div 的部分
    fake = FakeLamaSession(kind="raw", raw=raw, out_div=255.0)
    zero_img = np.zeros((512, 512, 3), np.float32)
    zero_mask = np.zeros((512, 512, 1), np.float32)
    out = dw._infer_tile(fake, zero_img, zero_mask, out_div=255.0)
    expected = np.clip(raw[0] / 255.0, 0.0, 1.0).transpose(1, 2, 0)
    assert out.shape == (512, 512, 3), out.shape
    assert out.dtype == np.float32, out.dtype
    assert np.allclose(out, expected), "转置/÷out_div/clip 任一环节不对"
    print("✅ _infer_tile 数学契约：CHW→HWC + ÷out_div + clip[0,1]")


def test_infer_tile_clip_prevents_white_wash():
    """若漏做 ÷out_div，>1 的输出会被 clip 成全 1（纯白）。本测试反向证明除子生效：
    喂 out_div*0.5 的常数 → 输出应恰为 0.5（中灰），而非被误拉成 1.0。"""
    raw = np.full((1, 3, 512, 512), 255.0 * 0.5, dtype=np.float32)
    fake = FakeLamaSession(kind="raw", raw=raw, out_div=255.0)
    zero_img = np.zeros((512, 512, 3), np.float32)
    zero_mask = np.zeros((512, 512, 1), np.float32)
    out = dw._infer_tile(fake, zero_img, zero_mask, out_div=255.0)
    assert np.allclose(out, 0.5), f"÷out_div 疑似丢失，输出被拉成 {float(out.mean())}"
    print("✅ _infer_tile 正确 ÷out_div（0.5*out_div 输入 → 中灰 0.5，未被拉白）")


# --------------------------------------------------------------------------- #
# 2) _inpaint_tiles 单瓦片融合 == 单瓦片推理
# --------------------------------------------------------------------------- #
def test_inpaint_tiles_single_tile_equals_infer():
    """整图恰一瓦片时，羽化权重全 1，融合结果必须 == 该瓦片 _infer_tile 输出。"""
    fake = FakeLamaSession(kind="identity_mask", out_div=255.0)
    img_f = np.zeros((512, 512, 3), np.float32)
    mask_f = np.ones((512, 512, 1), np.float32)   # 全 mask → 模型输出恒白
    fused = _with_fake(fake, lambda: dw._inpaint_tiles(img_f, mask_f))
    single = dw._infer_tile(fake, img_f, mask_f, out_div=255.0)
    assert fused.shape == (512, 512, 3)
    assert np.allclose(fused, single), "单瓦片融合未还原为单瓦片推理结果"
    print("✅ _inpaint_tiles 单瓦片融合 == 单瓦片推理（羽化权重全 1）")


# --------------------------------------------------------------------------- #
# 3) 完整前向：ai_image_inpaint_core
# --------------------------------------------------------------------------- #
def _make_bgr(path, h, w, fill, box=None):
    arr = np.full((h, w, 3), fill, dtype=np.uint8)
    if box is not None:
        x0, y0, x1, y1 = box
        arr[y0:y1, x0:x1] = (0, 0, 255)  # 画一块红（水印）做对照
    import cv2 as _cv2
    _cv2.imwrite(str(path), arr)
    return arr


def test_forward_core_full_mask_rewrites_whole_image():
    """全图 mask → 输出整片被模型改写（不再等于原图），模型确实被调用。"""
    import cv2 as _cv2
    import tempfile
    d = tempfile.mkdtemp(prefix="vdl_dw_fwd_")
    src = os.path.join(d, "src.png")
    dst = os.path.join(d, "dst.png")
    green = (0, 255, 0)
    _make_bgr(src, 256, 256, green)
    fake = FakeLamaSession(kind="identity_mask", out_div=255.0)
    regions = [{"x": 0, "y": 0, "w": 1, "h": 1, "op": "add"}]
    out = _with_fake(fake, lambda: dw.ai_image_inpaint_core(src, dst, regions))
    assert fake.last_feeds is not None, "前向没有真正调用模型"
    res = _cv2.imread(out)
    assert res is not None and res.shape == (256, 256, 3)
    assert np.all(res == 255), "全图 mask 未整片改写为白"
    print("✅ 全图 mask → 前向整片改写（模型被真实调用，输出全白）")


def test_forward_core_partial_mask_leaves_unmasked_untouched():
    """部分 mask → 水印区被改写为白，**非水印区逐字节不动**（前向绝不能污染未框区域）。"""
    import cv2 as _cv2
    import tempfile
    d = tempfile.mkdtemp(prefix="vdl_dw_fwd_")
    src = os.path.join(d, "src.png")
    dst = os.path.join(d, "dst.png")
    green = (0, 255, 0)
    src_arr = _make_bgr(src, 400, 600, green, box=(0, 0, 300, 200))  # 左上 300x200 红=水印
    fake = FakeLamaSession(kind="identity_mask", out_div=255.0)
    regions = [{"x": 0, "y": 0, "w": 0.5, "h": 0.5, "op": "add"}]  # 归一化 0.5 = 300x200
    out = _with_fake(fake, lambda: dw.ai_image_inpaint_core(src, dst, regions))
    assert fake.last_feeds is not None, "前向没有真正调用模型"
    res = _cv2.imread(out)
    assert res is not None and res.shape == (400, 600, 3), res.shape

    # 水印区（左上 300x200）→ 被改写为白
    assert np.all(res[0:200, 0:300] == 255), "水印区未被改写"
    # 非水印区（图内任意未框点）→ 与原图逐字节一致
    assert np.array_equal(res[100, 450], src_arr[100, 450]), "未框区域被前向污染"
    assert np.array_equal(res[350, 400], src_arr[350, 400]), "未框区域被前向污染（crop 外）"
    # 多瓦片（640x480>512）也走一遍，确认羽化拼接不污染未框区
    src2 = os.path.join(d, "src2.png")
    dst2 = os.path.join(d, "dst2.png")
    src_arr2 = _make_bgr(src2, 480, 640, green, box=(0, 0, 320, 240))
    regions2 = [{"x": 0, "y": 0, "w": 0.5, "h": 0.5, "op": "add"}]
    out2 = _with_fake(fake, lambda: dw.ai_image_inpaint_core(src2, dst2, regions2))
    res2 = _cv2.imread(out2)
    assert res2 is not None and res2.shape == (480, 640, 3)
    assert np.all(res2[0:240, 0:320] == 255), "多瓦片：水印区未被改写"
    assert np.array_equal(res2[100, 500], src_arr2[100, 500]), "多瓦片：未框区域被污染"
    assert np.array_equal(res2[400, 600], src_arr2[400, 600]), "多瓦片：crop 外未框区被污染"
    print("✅ 部分 mask → 水印区改写、未框区逐字节不动（含多瓦片羽化路径）")


if __name__ == "__main__":
    test_infer_tile_transpose_divide_clip()
    test_infer_tile_clip_prevents_white_wash()
    test_inpaint_tiles_single_tile_equals_infer()
    test_forward_core_full_mask_rewrites_whole_image()
    test_forward_core_partial_mask_leaves_unmasked_untouched()
    print("\n🎉 去水印 AI 推理前向链路测试全部通过（5 项）")
