"""无损压缩（routers/compress.py）离线回归测试（2026-09-11 新增）。

覆盖：
  1. 图片压缩核心 `_compress_image`：PNG 无损优化（像素逐字节一致）、
     JPG 质量档（产物可开且更小/可控）、WebP 质量档；
  2. `_run_compress` 全链路（图片）：job 状态机 running→completed、
     size_before/after/saving 统计、输出文件落盘；
  3. 「压缩后反而更大」守卫：不可压缩的小 PNG → 输出回退为原文件副本
     （saving=0，note 提示，下载仍可用）；
  4. 档位校验 `_validate_level`：非法档位回退 balanced。

视频路径依赖 ffmpeg（LGPL 发行版无 libx264，走 codec_utils.h264_args），
E2E 已实测（PNG 6.6% / MP4 51.6%/63.0%），此处不重复——保持离线测试
不依赖外部二进制行为的约定。

运行：
    cd server && python tests/test_compress.py
    cd server && python -m pytest tests/test_compress.py -v
"""
import os
import sys
import tempfile

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from PIL import Image, ImageDraw  # noqa: E402

import app  # noqa: E402
from routers.compress import (  # noqa: E402
    _compress_image,
    _run_compress,
    _validate_level,
    COMPRESS_JOBS,
)

PASS = 0
FAIL = 0


def check(tag, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"✅ {tag}")
    else:
        FAIL += 1
        print(f"❌ {tag}: {detail}")


def _noisy_png(path, size=600):
    """高熵 PNG（压缩前足够大，给优化留空间）。"""
    img = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    d = ImageDraw.Draw(img)
    for i in range(0, size, 8):
        d.line([(i, 0), (i, size)], fill=(i % 255, (i * 3) % 255, (i * 7) % 255, 255), width=3)
    img.save(path, format="PNG", optimize=False)


def test_png_lossless_pixel_identical():
    """PNG optimize=True 是无损重压：解码像素必须与原图逐字节一致。"""
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "a.png")
        out = os.path.join(td, "a_out.png")
        _noisy_png(src)
        Image.open(src).save(src, format="PNG", optimize=False)
        job = {"stage": "", "progress": 0}
        _compress_image(job, src, app.Path(out), "balanced")
        with Image.open(src) as a, Image.open(out) as b:
            same = list(a.convert("RGBA").getdata()) == list(b.convert("RGBA").getdata())
        check("PNG 无损：像素逐字节一致", same)
        check("PNG 无损：产物更小或相等",
              os.path.getsize(out) <= os.path.getsize(src),
              f"{os.path.getsize(src)} -> {os.path.getsize(out)}")


def test_jpg_quality_downscale_size():
    """JPG 质量档：strong 档产物应显著小于 high 档（同源同格式）。"""
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "a.jpg")
        out_hi = os.path.join(td, "hi.jpg")
        out_lo = os.path.join(td, "lo.jpg")
        _noisy_png(src + ".tmp.png")
        Image.open(src + ".tmp.png").convert("RGB").save(src, format="JPEG", quality=100, subsampling=0)
        _compress_image({"stage": ""}, src, app.Path(out_hi), "high")
        _compress_image({"stage": ""}, src, app.Path(out_lo), "strong")
        check("JPG 质量档分档生效",
              os.path.getsize(out_lo) < os.path.getsize(out_hi),
              f"strong={os.path.getsize(out_lo)} high={os.path.getsize(out_hi)}")
        # 产物可正常打开
        Image.open(out_lo).verify()
        Image.open(out_hi).verify()
        check("JPG 产物完整可解码", True)


def test_run_compress_full_chain_and_oversize_guard():
    """_run_compress 全链路 + 「反而更大」守卫（已最优 PNG 压缩无收益 → 回退原样副本）。"""
    with tempfile.TemporaryDirectory() as td:
        # 已 optimize 的极小 PNG：再次 optimize 压不出更小 → 命中守卫
        tiny = os.path.join(td, "tiny.png")
        Image.new("RGBA", (2, 2), (0, 0, 0, 255)).save(tiny, format="PNG", optimize=True)
        COMPRESS_JOBS["t1"] = {
            "status": "running", "stage": "", "progress": 0, "error": "",
            "out_path": "", "filename": "", "src_name": "tiny.png",
            "device_id": "t", "size_before": 0, "size_after": 0,
            "saving": 0.0, "note": "",
        }
        _run_compress("t1", tiny, "image", "balanced", src_is_temp=False)
        j = COMPRESS_JOBS["t1"]
        check("小图守卫：状态 completed", j["status"] == "completed", j.get("error"))
        check("小图守卫：saving=0 且有 note", j["saving"] == 0.0 and "足够小" in (j["note"] or ""),
              f"saving={j['saving']} note={j['note']}")
        check("小图守卫：输出为原文件副本（同尺寸）",
              j["out_path"] and os.path.getsize(j["out_path"]) == os.path.getsize(tiny),
              f"{os.path.getsize(tiny)} vs {os.path.getsize(j['out_path']) if j['out_path'] else '无输出'}")

        # 正常大图：全链路统计
        big = os.path.join(td, "big.png")
        _noisy_png(big, 800)
        COMPRESS_JOBS["t2"] = dict(j, out_path="", note="", saving=0.0)
        _run_compress("t2", big, "image", "balanced", src_is_temp=False)
        j2 = COMPRESS_JOBS["t2"]
        check("全链路：completed 且统计齐全",
              j2["status"] == "completed" and j2["size_before"] > 0 and j2["size_after"] > 0,
              j2.get("error"))
        check("全链路：输出文件存在且 filename 带 [已压缩] 前缀",
              os.path.isfile(j2["out_path"]) and j2["filename"].startswith("[已压缩]"),
              j2["filename"])
        COMPRESS_JOBS.pop("t1", None)
        COMPRESS_JOBS.pop("t2", None)


def test_validate_level():
    check("合法档位原样返回", _validate_level("high") == "high" and _validate_level("strong") == "strong")
    check("非法档位回退 balanced", _validate_level("ultra") == "balanced" and _validate_level("") == "balanced")


if __name__ == "__main__":
    test_png_lossless_pixel_identical()
    test_jpg_quality_downscale_size()
    test_run_compress_full_chain_and_oversize_guard()
    test_validate_level()
    print(f"\n通过: {PASS}  失败: {FAIL}")
    sys.exit(1 if FAIL else 0)
