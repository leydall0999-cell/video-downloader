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
    _validate_codec,
    _validate_output_format,
    pillow_avif,
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


def test_validate_codec_and_format():
    check("视频编码：合法原样返回",
          _validate_codec("h264") == "h264" and _validate_codec("hevc") == "hevc")
    check("视频编码：非法回退 h264",
          _validate_codec("av1") == "h264" and _validate_codec("") == "h264")
    check("图片格式：合法原样返回",
          _validate_output_format("keep") == "keep"
          and _validate_output_format("webp") == "webp"
          and _validate_output_format("avif") == "avif")
    check("图片格式：非法回退 keep",
          _validate_output_format("bmp") == "keep" and _validate_output_format("") == "keep")


def _photo_png(path, size=512):
    """照片型平滑渐变 PNG（WebP/AVIF 的强项场景：低频、平滑、类真实照片）。

    ⚠️ 高频噪声 PNG（_noisy_png）反而会让 WebP 无损/有损膨胀——那不符合真实用途，
    此处用平滑渐变模拟照片，才能体现转格式压缩的真实收益。
    """
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            px[x, y] = (int(255 * x / size), int(255 * y / size),
                        int(128 + 100 * ((x + y) / (2 * size))))
    img.save(path, format="PNG", optimize=True)


def test_webp_output_smaller():
    """图片输出格式=webp：照片型素材产物应为更小且可解码的 WebP。"""
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "a.png")
        out = os.path.join(td, "a.webp")
        _photo_png(src, 512)
        _compress_image({"stage": ""}, src, app.Path(out), "balanced", output_format="webp")
        check("WebP 输出真实更小", os.path.getsize(out) < os.path.getsize(src),
              f"{os.path.getsize(src)} -> {os.path.getsize(out)}")
        with Image.open(out) as im:
            check("WebP 可解码且格式正确", im.format == "WEBP", im.format)


def test_avif_output_smaller():
    """图片输出格式=avif：照片型素材产物应为更小且可解码的 AVIF（需 pillow_avif）。"""
    if pillow_avif is None:
        print("⏭️  AVIF 测试跳过：未安装 pillow-avif-plugin")
        return
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "a.png")
        out = os.path.join(td, "a.avif")
        _photo_png(src, 512)
        _compress_image({"stage": ""}, src, app.Path(out), "balanced", output_format="avif")
        check("AVIF 输出真实更小", os.path.getsize(out) < os.path.getsize(src),
              f"{os.path.getsize(src)} -> {os.path.getsize(out)}")
        with Image.open(out) as im:
            check("AVIF 可解码且格式正确", im.format == "AVIF", im.format)


def test_avif_missing_plugin_errors():
    """未启用 AVIF 插件时，avif 输出应明确报错而非静默损坏。"""
    # 仅当插件真的没装时才算守卫命中；装了则验证「能正常出 AVIF」已在上一个用例覆盖
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "a.png")
        out = os.path.join(td, "a.avif")
        _noisy_png(src, 64)
        try:
            _compress_image({"stage": ""}, src, app.Path(out), "balanced", output_format="avif")
            check("AVIF 有插件：未抛错（正常）", pillow_avif is not None)
        except RuntimeError as e:
            check("AVIF 无插件：抛出可读错误", pillow_avif is None and "AVIF" in str(e), str(e))


def test_run_compress_hevc_video_guarded():
    """视频 HEVC 全链路（仅当本机 ffmpeg 支持 hevc_videotoolbox 时跑，否则跳过）。"""
    ffmpeg = getattr(app, "FFMPEG_BIN", "") or ""
    if not (ffmpeg and os.path.isfile(ffmpeg)):
        print("⏭️  HEVC 视频测试跳过：离线环境无 ffmpeg")
        return
    try:
        from codec_utils import available_hevc
    except Exception:
        print("⏭️  HEVC 视频测试跳过：无法导入 codec_utils")
        return
    if available_hevc(ffmpeg) != "hevc_videotoolbox":
        print("⏭️  HEVC 视频测试跳过：ffmpeg 无 hevc_videotoolbox")
        return
    import subprocess
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "v.mp4")
        rc = subprocess.run([ffmpeg, "-y", "-f", "lavfi", "-i",
                             "testsrc=duration=2:size=320x240:rate=24", "-pix_fmt",
                             "yuv420p", "-c:v", "mpeg4", "-q:v", "8", src],
                            capture_output=True, text=True).returncode
        if rc != 0:
            print("⏭️  HEVC 视频测试跳过：测试源生成失败")
            return
        COMPRESS_JOBS["hev1"] = {
            "status": "running", "stage": "", "progress": 0, "error": "",
            "out_path": "", "filename": "", "src_name": "v.mp4",
            "device_id": "t", "size_before": 0, "size_after": 0, "saving": 0.0, "note": "",
        }
        _run_compress("hev1", src, "video", "balanced", src_is_temp=False, codec="hevc")
        j = COMPRESS_JOBS["hev1"]
        check("HEVC 全链路 completed", j["status"] == "completed", j.get("error"))
        check("HEVC 输出为 .mp4 且存在", j["out_path"].endswith(".mp4") and os.path.isfile(j["out_path"]),
              j["out_path"])
        # ffprobe 确认视频流编码为 hevc
        try:
            from routers.compress import _ffprobe_bin
            vcodec = subprocess.run([_ffprobe_bin(), "-v", "error", "-select_streams",
                                    "v:0", "-show_entries", "stream=codec_name", "-of",
                                    "default=noprint_wrappers=1:nokey=1", j["out_path"]],
                                   capture_output=True, text=True).stdout.strip().lower()
            check("HEVC 输出确为 hevc 编码", vcodec == "hevc", vcodec)
        except Exception as e:
            check("HEVC ffprobe 验证跳过（探测失败）", False, str(e))
        COMPRESS_JOBS.pop("hev1", None)


if __name__ == "__main__":
    test_png_lossless_pixel_identical()
    test_jpg_quality_downscale_size()
    test_run_compress_full_chain_and_oversize_guard()
    test_validate_level()
    test_validate_codec_and_format()
    test_webp_output_smaller()
    test_avif_output_smaller()
    test_avif_missing_plugin_errors()
    test_run_compress_hevc_video_guarded()
    print(f"\n通过: {PASS}  失败: {FAIL}")
    sys.exit(1 if FAIL else 0)
