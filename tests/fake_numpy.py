"""tests/fake_numpy.py — 极简 numpy 替身，仅覆盖 dewatermark_core 实际调用的表面 API。

不追求数值正确（那是 OpenCV/numpy 的职责），只为驱动被测逻辑走通控制流：
- zeros((h,w), uint8) 返回可切片赋值的掩码对象
- frombuffer(...).reshape(...) 返回可 .tobytes() 的数组对象
"""
import os


class _Uint8:
    pass


uint8 = _Uint8()


class FakeArray:
    def __setitem__(self, key, value):
        # mask[y:y+rh, x:x+rw] = 255 —— 忽略，仅占位
        return None

    def __getitem__(self, key):
        return 0

    def reshape(self, *shape):
        return self

    def any(self, axis=None):
        # fake 不追踪真实内容；测试只关心控制流，返回 True 让 inpaint 分支走通
        return True

    def all(self, axis=None):
        return True

    def tobytes(self):
        return b"RGBRGB"


def zeros(shape, dtype=None):
    return FakeArray()


def frombuffer(buf, dtype=None):
    return FakeArray()
