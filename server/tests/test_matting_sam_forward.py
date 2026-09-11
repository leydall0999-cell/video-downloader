"""sam-matting（SAM 像素级 + 导向滤波软抠像）前向离线回归测试（2026-09-11 新增）。

sam-matting 是 algorithm 引擎，但内部依赖 MobileSAM 两个 ONNX（encoder/decoder），
走 `_matting_sam_trimap → sam_refine_mask → _sam_mask_whole/_sam_mask_cropped →
_sam_session('enc'/'dec')`。真实权重 ~40MB 需下载，本测试**不下载**：

通过 monkeypatch `matting_ai._sam_session` 注入 duck-typed 假 session：
  · enc 假 session：run 返回任意合法 embedding 形状 [1,256,64,64]
  · dec 假 session：run 返回受控的 [1,1,1024,1024]（左半/右半高激活），
    把「SAM 像素级分割结果」精确控成已知张量

锁死的前向契约：
  - encoder 输出形状必须 [1,256,64,64]，且原样作为 decoder 的 image_embeddings 流入
    （encoder→decoder 接口不被破坏）
  - decoder 输出 [1,1,1024,1024]（logits，max>1 触发 sigmoid）→ 经 trimap + 导向滤波
    解出连续 alpha：**SAM 左/右半的分割必须忠实地透传为 alpha 左/右半的不透明/透明**
    （不是硬编码方向、不是被吞成整图常量）

运行：
    cd server && python tests/test_matting_sam_forward.py
    cd server && python -m pytest tests/test_matting_sam_forward.py -v
"""
import os
import sys
import types

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import matting_ai as m  # noqa: E402


# --------------------------------------------------------------------------- #
# 假 SAM encoder / decoder session
# --------------------------------------------------------------------------- #
class FakeSamEnc:
    def get_inputs(self):
        return [types.SimpleNamespace(name="image")]

    def run(self, _, feeds):
        # 任意合法 embedding；decoder 假 session 直接忽略它
        return [np.zeros((1, 256, 64, 64), dtype=np.float32)]


class FakeSamDec:
    def __init__(self, high_side="left"):
        # high_side: 哪一半 decoder 输出高激活（"left" 或 "right"）
        self.high_side = high_side
        self.last_feeds = None

    def get_inputs(self):
        return [
            types.SimpleNamespace(name="image_embeddings"),
            types.SimpleNamespace(name="point_coords"),
            types.SimpleNamespace(name="point_labels"),
            types.SimpleNamespace(name="mask_input"),
            types.SimpleNamespace(name="has_mask_input"),
            types.SimpleNamespace(name="orig_im_size"),
        ]

    def run(self, _, feeds):
        self.last_feeds = feeds
        # 用强 logits：+10 → sigmoid≈1（前景），-10 → sigmoid≈0（背景）。
        # 注意源实现对整个 decoder 输出做 sigmoid，故不能喂 0（sigmoid(0)=0.5 是「不确定」）。
        mask = np.full((1, 1, 1024, 1024), -10.0, dtype=np.float32)
        if self.high_side == "left":
            mask[:, :, :, :512] = 10.0   # 左半前景 logits
        else:
            mask[:, :, :, 512:] = 10.0   # 右半前景 logits
        return [mask]


def _fake_sam_session(high_side):
    def _impl(kind):
        if kind == "enc":
            return FakeSamEnc()
        return FakeSamDec(high_side=high_side)
    return _impl


def _with_fake_sam(high_side, fn):
    """临时把 matting_ai._sam_session 换成受控假 session，跑完还原。

    返回 (函数结果, decoder 假 session) —— 后者用于断言 decoder 收到的 feeds。
    """
    orig = m._sam_session
    state = {"dec": None}

    def _patched(kind):
        if kind == "enc":
            return FakeSamEnc()
        d = FakeSamDec(high_side=high_side)
        state["dec"] = d
        return d

    m._sam_session = _patched
    try:
        result = fn()
        return result, state["dec"]
    finally:
        m._sam_session = orig


# =========================================================================== #
# 测试
# =========================================================================== #
def test_sam_forward_encoder_to_decoder_interface():
    """encoder 输出 [1,256,64,64] 必须原样作为 decoder 的 image_embeddings 流入。"""
    img = Image.new("RGB", (100, 100), (180, 180, 180))
    rgba, dec = _with_fake_sam("left", lambda: m._matting_sam_trimap(img, 100, 100, click=[50, 50]))
    assert dec is not None
    emb = dec.last_feeds["image_embeddings"]
    assert emb.shape == (1, 256, 64, 64), emb.shape
    print("✅ SAM encoder→decoder 接口：embedding 形状 (1,256,64,64) 正确流入 decoder")


def test_sam_forward_left_half_opaque_right_transparent():
    """decoder 左半高激活 → 最终 alpha 左半不透明、右半透明（像素级分割透传）。

    注：SAM mask 经 LANCZOS 缩放回原图后，x≈45–55 处天然有软边过渡带（导向滤波
    平滑）；故采样远离边界的内象限（左 x<25 / 右 x>75）来断言方向，避开过渡区。
    """
    img = Image.new("RGB", (100, 100), (180, 180, 180))
    rgba, _ = _with_fake_sam("left", lambda: m._matting_sam_trimap(img, 100, 100, click=[50, 50]))
    assert rgba.mode == "RGBA"
    alpha = np.array(rgba)[..., 3].astype(np.float32) / 255.0
    left = float(alpha[:, :25].mean())
    right = float(alpha[:, 75:].mean())
    assert left > 0.7, ("左半（内象限）未透传为不透明", left)
    assert right < 0.3, ("右半（内象限）未透传为透明", right)
    assert left - right > 0.4, (left, right)
    print("✅ SAM 前向：左半不透明(%.2f)、右半透明(%.2f)，分割忠実透传" % (left, right))


def test_sam_forward_direction_not_hardcoded():
    """decoder 右半高激活 → 最终 alpha 必须翻转为右半不透明（证明不是硬编码方向）。"""
    img = Image.new("RGB", (100, 100), (180, 180, 180))
    rgba, _ = _with_fake_sam("right", lambda: m._matting_sam_trimap(img, 100, 100, click=[50, 50]))
    alpha = np.array(rgba)[..., 3].astype(np.float32) / 255.0
    left = float(alpha[:, :25].mean())
    right = float(alpha[:, 75:].mean())
    assert right > 0.7, ("右半（内象限）未透传为不透明", right)
    assert left < 0.3, ("左半（内象限）未透传为透明", left)
    print("✅ SAM 前向方向正确：右半激活→右半不透明(%.2f)、左半透明(%.2f)" % (right, left))


if __name__ == "__main__":
    test_sam_forward_encoder_to_decoder_interface()
    test_sam_forward_left_half_opaque_right_transparent()
    test_sam_forward_direction_not_hardcoded()
    print("\n🎉 sam-matting 前向链路测试全部通过（3 项）")
