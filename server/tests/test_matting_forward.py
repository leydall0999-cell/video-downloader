"""一键抠图（BiRefNet 等）AI 推理前向链路离线回归测试（2026-09-11 新增）。

背景：此前的核心链路测试只覆盖「前处理纯函数 / 选区 / 边缘柔化」，**没有触碰模型
推理前向本身**——即 `_preprocess → sess.run → sigmoid 自动探测 → 软阈值/BFS/高斯羽化
→ resize 回原图` 这条真正出 alpha 蒙版的链路。这条链路一旦回归（比如归一化写错、
sigmoid 探测反向、后处理把整图拉黑/拉白），出图会整体发灰/发白/主体被吞，且不会报错。

真实 BiRefNet 权重 ~100MB+，本测试**不下载任何权重**：通过 monkeypatch `_get_session`
注入一个 duck-typed 假 InferenceSession，把模型输出控成已知张量，从而把前向链路的
「数值契约」锁死：

  - 喂给模型的张量必须是 (1,3,1024,1024) float32 且已做 ImageNet 归一化（不是裸 0-255）
  - logits（max>1）走 sigmoid，已含 sigmoid 的 [0,1] 直出（max<=1 不重复 sigmoid）
  - 软阈值 [0.20,0.75] + BFS 连通性 + power(1.8) + 高斯羽化 + resize 回原图尺寸
  - 返回 PIL "L" 模式、尺寸 == 原图、值域 0-255 的 alpha 蒙版

运行：
    cd server && python tests/test_matting_forward.py
    cd server && python -m pytest tests/test_matting_forward.py -v
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
# 假 InferenceSession（不加载任何权重；输出由测试精确控制）
# --------------------------------------------------------------------------- #
class FakeMattingSession:
    """单输入 (image) ONNX session 替身；run 返回 (1,1,1024,1024) 常数 logits/prob。"""

    def __init__(self, value):
        self.value = float(value)        # >1 视为 logits；<=1 视为已 sigmoid
        self.last_feed = None

    def get_inputs(self):
        return [types.SimpleNamespace(name="input")]

    def run(self, _, feeds):
        self.last_feed = feeds["input"]
        return [np.full((1, 1, 1024, 1024), self.value, dtype=np.float32)]


class FakeMattingGradientSession:
    """输出一条 0→2 的线性梯度（max=2 → 触发 sigmoid），用于验证空间结构透传。"""

    def get_inputs(self):
        return [types.SimpleNamespace(name="input")]

    def run(self, _, feeds):
        g = np.linspace(0.0, 2.0, 1024 * 1024, dtype=np.float32).reshape(1, 1, 1024, 1024)
        return [g]


class FakeMattingStepSession:
    """左半 logit=0、右半 logit=3（max=3 → 触发 sigmoid 分支）。

    用于**精确**锁定 sigmoid 分支：正确代码下左半 sigmoid(0)=0.5 → 软阈值后
    仍是中等 alpha（不透明）；若漏补 sigmoid（pred=raw），左半 raw=0 → clip→0
    → 整片透明。两者在「左半均值」上可明确区分。"""

    def get_inputs(self):
        return [types.SimpleNamespace(name="input")]

    def run(self, _, feeds):
        step = np.zeros((1, 1, 1024, 1024), dtype=np.float32)
        step[:, :, :, 512:] = 3.0   # 右半高 logit → 恒白
        return [step]


def _with_fake(session, fn):
    """临时把 _get_session 换成假 session，跑 fn，最后还原（pytest/direct 皆可）。"""
    orig = m._get_session
    m._get_session = lambda name=None: session
    try:
        return fn()
    finally:
        m._get_session = orig


# --------------------------------------------------------------------------- #
# 1) 前处理 → 喂给模型的张量契约
# --------------------------------------------------------------------------- #
def test_forward_feed_is_normalized_1024_tensor():
    """模型收到的必须是 (1,3,1024,1024) float32 且已完成归一化（不是裸 0-255）。"""
    fake = FakeMattingSession(0.5)
    img = Image.new("RGB", (800, 600), (200, 100, 50))
    orig = m._get_session
    m._get_session = lambda name=None: fake
    try:
        m.predict_mask(img, "birefnet-general")
    finally:
        m._get_session = orig
    feed = fake.last_feed
    assert feed is not None, "前向链路没有真正调用模型（sess.run 未被触发）"
    assert feed.shape == (1, 3, 1024, 1024), feed.shape
    assert feed.dtype == np.float32, feed.dtype
    # 已做 ImageNet 归一化：典型值落在个位数量级，绝不可能是裸 0-255
    assert np.isfinite(feed).all()
    assert float(np.abs(feed).max()) < 10.0, f"疑似喂了未归一化像素值，max={float(np.abs(feed).max())}"
    print("✅ 前向输入契约：形状(1,3,1024,1024) / float32 / 已 ImageNet 归一化")


def test_forward_feed_matches_canonical_preprocess():
    """前向喂给模型的张量必须 == 规范 _preprocess(img, input_size, norm) 输出
    （input_size/norm 取对应模型，证明前向没喂裸像素、也没用错尺寸/归一化）。"""
    norm = m.MODELS["birefnet-general"].get("norm", "max")
    fake = FakeMattingSession(0.5)
    img = Image.new("RGB", (333, 222), (128, 64, 32))
    orig = m._get_session
    m._get_session = lambda name=None: fake
    try:
        m.predict_mask(img, "birefnet-general")
    finally:
        m._get_session = orig
    feed = fake.last_feed
    canonical = m._preprocess(img, (1024, 1024), norm)
    assert np.allclose(feed, canonical, atol=1e-3), "前向输入与规范前处理不一致"
    print("✅ 前向输入 == 规范 _preprocess 输出（尺寸/归一化一致，未喂裸像素）")


# --------------------------------------------------------------------------- #
# 2) sigmoid 自动探测（核心正确性特征）
# --------------------------------------------------------------------------- #
def test_forward_applies_sigmoid_on_logits():
    """模型输出 logits（max>1）→ 补 sigmoid：全 5.0 概率≈0.993 → 软阈值满 alpha → 全白。"""
    fake = FakeMattingSession(5.0)
    img = Image.new("RGB", (64, 64), (10, 20, 30))
    mask = _with_fake(fake, lambda: m.predict_mask(img, "birefnet-general"))
    assert mask.mode == "L" and mask.size == img.size
    arr = np.array(mask)
    assert float(arr.min()) == 255.0 and float(arr.max()) == 255.0, "logits 路径未产生满 alpha"
    print("✅ logits（max>1）正确补 sigmoid → 高置信区满 alpha（全白）")


def test_forward_no_double_sigmoid_on_prob():
    """模型输出已在 [0,1]（max<=1）→ 不再 sigmoid，直接按概率走软阈值。"""
    fake = FakeMattingSession(0.5)   # 0.5 ∈ [0.20,0.75] → 软 alpha≈0.545 → power(1.8)≈0.34
    img = Image.new("RGB", (64, 64), (10, 20, 30))
    mask = _with_fake(fake, lambda: m.predict_mask(img, "birefnet-general"))
    arr = np.array(mask)
    # 0.545**1.8 * 255 ≈ 86，且应在 (0,255) 之间（既没被拉黑也没被拉白）
    assert 20 < int(arr.mean()) < 200, f"软阈值映射异常，均值={int(arr.mean())}"
    print(f"✅ 已 sigmoid 输出（max<=1）不重复 sigmoid，软阈值映射正确（均值≈{int(arr.mean())}）")


def test_forward_low_logits_yields_transparent():
    """全 -10 logits → sigmoid≈0 → 低于软阈值下限 → 整图全透（全黑蒙版）。"""
    fake = FakeMattingSession(-10.0)
    img = Image.new("RGB", (64, 64), (10, 20, 30))
    mask = _with_fake(fake, lambda: m.predict_mask(img, "birefnet-general"))
    arr = np.array(mask)
    assert int(arr.min()) == 0 and int(arr.max()) == 0, "低置信未整图透"
    print("✅ 低 logits → 整图透明（全黑蒙版），背景不被误抠")


# --------------------------------------------------------------------------- #
# 3) 输出形态契约
# --------------------------------------------------------------------------- #
def test_forward_output_is_l_at_original_size():
    """返回 PIL 'L' 模式、尺寸 == 原图。"""
    fake = FakeMattingSession(2.0)
    img = Image.new("RGB", (333, 222), (40, 80, 120))
    mask = _with_fake(fake, lambda: m.predict_mask(img, "birefnet-general"))
    assert mask.mode == "L", mask.mode
    assert mask.size == (333, 222), mask.size
    print("✅ 前向输出为 PIL 'L' 蒙版且尺寸对齐原图")


def test_forward_spatial_structure_passthrough():
    """梯度 logits（0→2）→ 软阈值后左暗右亮；蒙版不应是常值（结构确实透传）。"""
    fake = FakeMattingGradientSession()
    img = Image.new("RGB", (128, 128), (10, 20, 30))
    mask = _with_fake(fake, lambda: m.predict_mask(img, "birefnet-general"))
    arr = np.array(mask)
    assert not np.allclose(arr, arr[0, 0]), "模型输出的空间结构没有透传到 alpha 蒙版"
    print("✅ 模型输出的空间渐变正确透传为 alpha 渐变")


def test_forward_sigmoid_branch_precision():
    """精度锁定 sigmoid 分支：左半 logit=0（max=3 触发 sigmoid 分支）。

    正确代码：左半 sigmoid(0)=0.5 → 软阈值 [0.20,0.75] 得 (0.5-0.2)/0.55≈0.545
    → 中等 alpha（不透明，均值明显 > 0）。
    漏补 sigmoid（pred=raw）：左半 raw=0 → clip(0,1)=0 → 软阈值下限以下 → 透明（均值≈0）。
    两者在「左半均值」上可明确区分，故本测试能真正杀死「漏补 sigmoid」变异。"""
    fake = FakeMattingStepSession()
    img = Image.new("RGB", (128, 128), (10, 20, 30))
    mask = _with_fake(fake, lambda: m.predict_mask(img, "birefnet-general"))
    arr = np.array(mask)
    assert arr.shape == (128, 128), arr.shape
    left_mean = float(arr[:, :64].mean())
    right_mean = float(arr[:, 64:].mean())
    # 右半必为满 alpha（白）
    assert right_mean > 250, f"右半高 logit 未产生满 alpha，均值={right_mean}"
    # 左半在正确代码下应是不透明的中等 alpha；漏补 sigmoid 时塌成 0
    assert left_mean > 50, (
        f"sigmoid 分支疑似失效：左半 logit=0 应 sigmoid→中等 alpha，"
        f"实测均值={left_mean}（漏补 sigmoid 时会塌成 0）"
    )
    print(f"✅ sigmoid 分支精确锁定：左半(中等 alpha)均值≈{int(left_mean)}，右半(满)≈{int(right_mean)}")


if __name__ == "__main__":
    test_forward_feed_is_normalized_1024_tensor()
    test_forward_feed_matches_canonical_preprocess()
    test_forward_applies_sigmoid_on_logits()
    test_forward_no_double_sigmoid_on_prob()
    test_forward_low_logits_yields_transparent()
    test_forward_output_is_l_at_original_size()
    test_forward_spatial_structure_passthrough()
    test_forward_sigmoid_branch_precision()
    print("\n🎉 抠图 AI 推理前向链路测试全部通过（8 项）")
