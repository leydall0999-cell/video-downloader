"""其余本地 ONNX 抠图引擎的 AI 推理前向链路离线回归测试（2026-09-11 新增）。

此前 test_matting_forward.py 只覆盖了默认引擎 birefnet-general 的前向。本文件把
「核心链路零测试」补齐到**所有走 predict_mask 的本地 ONNX 引擎**：

  · modnet-photographic   (768×768,  norm="255", 且是 trimap-free 分支)
  · isnet-general-use     (1024×1024, norm="255", 走完整 BiRefNet 后处理)
  · birefnet-matting      (1024×1024, norm="max")
  · birefnet-portrait     (1024×1024, norm="max")

每个引擎的差异点都在 _preprocess 的 input_size / norm，以及 predict_mask 的
后处理分支（modnet 跳过软阈值/BFS/高斯、直接 *255）。本测试用 model-agnostic 的
假 session（按捕获到的 feed.shape 反推 raw 形状），把每条前向的「数值契约」锁死：

  - 喂模型的张量形状必须 == (1, 3, input_size, input_size)（per-engine 分辨率）
  - norm="255" 与 norm="max" 归一化后的 R 通道值必须精确落在各自区间
    （纯色 (200,100,50)：255→≈1.307，max→≈2.249）
  - modnet 分支必须 trimap-free：输出 == raw*255（不做软阈值/幂次/高斯）
  - isnet/birefnet-* 必须走完整后处理（同输入下输出明显比 modnet 暗）

真实权重 ~100MB+，本测试不下载任何权重；通过 monkeypatch _get_session 注入
duck-typed 假 InferenceSession 实现。

运行：
    cd server && python tests/test_matting_engines_forward.py
    cd server && python -m pytest tests/test_matting_engines_forward.py -v
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
# 假 InferenceSession（model-agnostic：按捕获 feed 的形状反推 raw 形状）
# --------------------------------------------------------------------------- #
class FakeEngSession:
    """run 返回 (1,1,H,W) 常数张量（H,W 取自真正喂进来的 feed）。

    raw_fill 即「模型输出」（>1 视为 logits 触发 sigmoid；<=1 视为已 sigmoid）。
    """

    def __init__(self, raw_fill=0.5):
        self.raw_fill = float(raw_fill)
        self.last_feed = None

    def get_inputs(self):
        return [types.SimpleNamespace(name="input")]

    def run(self, _, feeds):
        self.last_feed = feeds["input"]
        H, W = self.last_feed.shape[2], self.last_feed.shape[3]
        raw = np.full((1, 1, H, W), self.raw_fill, dtype=np.float32)
        return [raw]


def _with_fake(fake, fn):
    """临时把 matting_ai._get_session 换成 fake，跑完还原。"""
    orig = m._get_session
    m._get_session = lambda name=None: fake
    try:
        return fn()
    finally:
        m._get_session = orig


# ImageNet 归一化常量（与 matting_ai._MEAN/_STD 一致，这里显式复算，不 import 源函数）
_MEAN = (0.485, 0.456, 0.406)
_STD = (0.229, 0.224, 0.225)


def _expected_preproc_r(rgb_tuple, norm):
    """复算 (200,100,50) 这类纯色图经 _preprocess 后 R 通道应得的值。

    独立公式：ary = rgb/255 (norm=255) 或 rgb/max(rgb) (norm=max)；
    再 (ary - mean[0]) / std[0]。与源实现等价但独立书写，作为契约断言。
    """
    r, g, b = [c / 255.0 for c in rgb_tuple]
    if norm == "255":
        ar = r
    else:
        mx = max(rgb_tuple) / 255.0
        ar = r / mx if mx > 0 else 0.0
    return (ar - _MEAN[0]) / _STD[0]


# =========================================================================== #
# modnet-photographic（768×768, norm=255, trimap-free 分支）
# =========================================================================== #
def test_modnet_input_size_is_768():
    """modnet 的前处理分辨率必须是 768×768（不是 1024）。"""
    fake = FakeEngSession(0.5)
    img = Image.new("RGB", (768, 768), (200, 100, 50))
    _with_fake(fake, lambda: m.predict_mask(img, "modnet-photographic"))
    assert fake.last_feed.shape == (1, 3, 768, 768), fake.last_feed.shape
    print("✅ modnet 前向输入张量 = (1,3,768,768)")


def test_modnet_norm_is_255():
    """modnet 用 norm='255'：纯色 (200,100,50) 的 R 通道归一化后 ≈1.307（不是 max 的 ≈2.249）。"""
    fake = FakeEngSession(0.5)
    img = Image.new("RGB", (768, 768), (200, 100, 50))
    _with_fake(fake, lambda: m.predict_mask(img, "modnet-photographic"))
    feed_r = fake.last_feed[0, 0]  # R 通道，纯色图全图一致
    expected = _expected_preproc_r((200, 100, 50), "255")
    assert np.allclose(feed_r, expected, atol=1e-3), (float(feed_r.mean()), expected)
    # 同时验证它明显不等于 norm='max' 的取值（≈2.249），排除归一化模式错配
    assert float(feed_r.mean()) < 2.0
    print("✅ modnet 前处理用 norm='255'（R≈%.4f，非 max 的 ≈2.249）" % expected)


def test_modnet_trimap_free_returns_raw_times_255():
    """modnet 是 trimap-free：模型输出 raw（<1 不补 sigmoid）直接 *255 当 alpha，
    不做软阈值/power/BFS/高斯。raw=0.5 → 输出恒 127。"""
    fake = FakeEngSession(0.5)
    img = Image.new("RGB", (768, 768), (30, 40, 50))
    mask = _with_fake(fake, lambda: m.predict_mask(img, "modnet-photographic"))
    arr = np.array(mask)
    assert arr.shape == (768, 768)
    assert arr.min() == arr.max() == 127, (int(arr.min()), int(arr.max()))
    print("✅ modnet trimap-free：输出 == raw*255（全 127，跳过软阈值/BFS/高斯）")


# =========================================================================== #
# isnet-general-use（1024×1024, norm=255, 走完整后处理）
# =========================================================================== #
def test_isnet_input_size_is_1024():
    fake = FakeEngSession(0.5)
    img = Image.new("RGB", (400, 300), (200, 100, 50))
    _with_fake(fake, lambda: m.predict_mask(img, "isnet-general-use"))
    assert fake.last_feed.shape == (1, 3, 1024, 1024), fake.last_feed.shape
    print("✅ isnet 前向输入张量 = (1,3,1024,1024)")


def test_isnet_norm_is_255():
    fake = FakeEngSession(0.5)
    img = Image.new("RGB", (400, 300), (200, 100, 50))
    _with_fake(fake, lambda: m.predict_mask(img, "isnet-general-use"))
    feed_r = fake.last_feed[0, 0]
    expected = _expected_preproc_r((200, 100, 50), "255")
    assert np.allclose(feed_r, expected, atol=1e-3), (float(feed_r.mean()), expected)
    assert float(feed_r.mean()) < 2.0
    print("✅ isnet 前处理用 norm='255'（R≈%.4f）" % expected)


def test_isnet_runs_full_postprocess_not_trimap_free():
    """同 raw=0.5 输入下：isnet 走完整软阈值+power(1.8) 后处理会把中值 0.5 压暗到 ≈87；
    modnet 直接 *255 = 127。两者必须明显不同（isnet 更暗），证明 isnet 没误走 modnet 分支。"""
    fake_is = FakeEngSession(0.5)
    fake_mod = FakeEngSession(0.5)
    img = Image.new("RGB", (400, 300), (30, 40, 50))
    is_mask = _with_fake(fake_is, lambda: m.predict_mask(img, "isnet-general-use"))
    mod_mask = _with_fake(fake_mod, lambda: m.predict_mask(img, "modnet-photographic"))
    is_mean = float(np.array(is_mask).mean())
    mod_mean = float(np.array(mod_mask).mean())
    # 软阈值 [0.20,0.75]：0.5 → (0.5-0.2)/0.55≈0.545 → power1.8→≈0.34 → *255≈87
    assert 70 < is_mean < 110, is_mean
    assert mod_mean == 127  # modnet 精确 127
    assert is_mean < mod_mean - 20, (is_mean, mod_mean)
    print("✅ isnet 走完整后处理（均值≈%.1f）比 modnet（127）明显更暗" % is_mean)


# =========================================================================== #
# birefnet-matting / birefnet-portrait（1024×1024, norm=max）
# =========================================================================== #
def test_birefnet_matting_input_size_1024_norm_max():
    fake = FakeEngSession(0.5)
    img = Image.new("RGB", (500, 500), (200, 100, 50))
    _with_fake(fake, lambda: m.predict_mask(img, "birefnet-matting"))
    assert fake.last_feed.shape == (1, 3, 1024, 1024), fake.last_feed.shape
    feed_r = fake.last_feed[0, 0]
    expected = _expected_preproc_r((200, 100, 50), "max")
    assert np.allclose(feed_r, expected, atol=1e-3), (float(feed_r.mean()), expected)
    assert float(feed_r.mean()) > 2.0  # norm=max → R≈2.249，区别于 255 的 ≈1.307
    print("✅ birefnet-matting：(1,3,1024,1024) + norm='max'（R≈%.4f）" % expected)


def test_birefnet_portrait_input_size_1024_norm_max():
    fake = FakeEngSession(0.5)
    img = Image.new("RGB", (500, 500), (200, 100, 50))
    _with_fake(fake, lambda: m.predict_mask(img, "birefnet-portrait"))
    assert fake.last_feed.shape == (1, 3, 1024, 1024), fake.last_feed.shape
    feed_r = fake.last_feed[0, 0]
    expected = _expected_preproc_r((200, 100, 50), "max")
    assert np.allclose(feed_r, expected, atol=1e-3), (float(feed_r.mean()), expected)
    assert float(feed_r.mean()) > 2.0
    print("✅ birefnet-portrait：(1,3,1024,1024) + norm='max'（R≈%.4f）" % expected)


if __name__ == "__main__":
    test_modnet_input_size_is_768()
    test_modnet_norm_is_255()
    test_modnet_trimap_free_returns_raw_times_255()
    test_isnet_input_size_is_1024()
    test_isnet_norm_is_255()
    test_isnet_runs_full_postprocess_not_trimap_free()
    test_birefnet_matting_input_size_1024_norm_max()
    test_birefnet_portrait_input_size_1024_norm_max()
    print("\n🎉 其余 ONNX 抠图引擎前向链路测试全部通过（8 项）")
