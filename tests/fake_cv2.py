"""tests/fake_cv2.py — 极简 OpenCV 替身，覆盖 dewatermark_core 实际调用的表面 API。

imread 返回占位对象；imwrite 真正落一个假文件（便于验证下载路由）；inpaint/cvtColor
原样返回输入（像素正确性由真实 OpenCV 在部署环境保证，本测试只验编排与接线）。
"""
from pathlib import Path


class FakeImage:
    def __init__(self, h=80, w=100, c=3):
        self.shape = (h, w, c)


class FakeCv2:
    IMREAD_COLOR = 1
    INPAINT_TELEA = 0
    INPAINT_NS = 1
    COLOR_RGB2BGR = 1
    COLOR_RGBA2BGR = 2
    COLOR_BGR2RGB = 3

    def imread(self, path, flags=None):
        return FakeImage()

    def imwrite(self, path, img):
        Path(path).write_bytes(b"fake-image-bytes")
        return True

    def inpaint(self, img, mask, radius, flag):
        return img

    def cvtColor(self, img, code):
        return img
