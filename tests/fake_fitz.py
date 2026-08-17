"""tests/fake_fitz.py — 极简 PyMuPDF 替身，覆盖 dewatermark_core 实际调用的表面 API。

支持两种被测路径：
- 注释型：page.annots() 返回 Watermark 注释列表，delete_annot 移除，doc.save 落假 PDF
- 栅格化：page.get_pixmap(dpi=) 返回带 samples/n/width/height 的 Pix；
  new_page + insert_image 合成；doc.save 落假 PDF
"""
from pathlib import Path

PDF_ANNOT_WATERMARK = 24
csRGB = object()


class _Rect:
    def __init__(self, w, h):
        self.width = w
        self.height = h


class FakePix:
    def __init__(self, w=100, h=80, n=3, samples=b"rgb"):
        self.width = w
        self.height = h
        self.n = n
        self.samples = samples


class Pixmap:
    def __init__(self, colorspace, width, height, samples):
        self.colorspace = colorspace
        self.width = width
        self.height = height
        self.samples = samples


class FakeAnnot:
    def __init__(self, subtype=PDF_ANNOT_WATERMARK):
        self.type = (subtype, "Watermark")


class FakePage:
    def __init__(self, n_annots=0, rect=None):
        self._annots = [FakeAnnot() for _ in range(n_annots)]
        self.rect = rect or _Rect(595, 842)
        self.inserted = []

    def annots(self):
        return list(self._annots)

    def delete_annot(self, a):
        self._annots.remove(a)

    def get_pixmap(self, dpi=150):
        return FakePix()

    def insert_image(self, rect, pixmap=None):
        self.inserted.append((rect, pixmap))


class FakeDoc:
    def __init__(self, pages=1, n_annots=0):
        self._pages = [FakePage(n_annots) for _ in range(pages)]
        self.saved = None

    def __iter__(self):
        return iter(self._pages)

    def load_page(self, i):
        return self._pages[i]

    @property
    def page_count(self):
        return len(self._pages)

    def new_page(self, width=0, height=0):
        p = FakePage(rect=_Rect(width, height))
        self._pages.append(p)
        return p

    def save(self, path, **kw):
        Path(path).write_bytes(b"%PDF-1.4 fake")
        self.saved = str(path)

    def close(self):
        pass


def open(path=None):
    return FakeDoc()
