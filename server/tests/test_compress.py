"""高效压缩（routers/compress.py）离线回归测试（2026-09-11 新增）。

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
import subprocess
import sys
import tempfile
import threading
import time

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from PIL import Image, ImageDraw  # noqa: E402

import app  # noqa: E402
from codec_utils import available_h264, rate_controlled_args, target_bitrate_kbps  # noqa: E402
from routers.compress import (  # noqa: E402
    _compress_image,
    _probe_video_meta,
    _run_compress,
    _submit_compress,
    _validate_level,
    _validate_codec,
    _validate_output_format,
    _TRANSCODE_SEM,
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


def _make_lowbitrate_mp4(path, seconds=3, w=640, h=360, kbps=300):
    """生成一个「已经被压过」的低码率 H.264 源（模拟网站在线播放片源）。

    这类源是码率钳制要解决的场景：恒定质量模式会把它们重编成数倍体积。
    """
    ffmpeg = getattr(app, "FFMPEG_BIN", "") or ""
    if not (ffmpeg and os.path.isfile(ffmpeg)):
        return False
    r = subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i",
         f"testsrc2=duration={seconds}:size={w}x{h}:rate=25",
         "-c:v", "h264_videotoolbox", "-b:v", f"{kbps}k",
         "-pix_fmt", "yuv420p", path],
        capture_output=True, text=True)
    return r.returncode == 0 and os.path.isfile(path) and os.path.getsize(path) > 0


def test_target_bitrate_clamps_to_source():
    """目标码率 = min(分辨率建议码率, 源码率 x 钳制系数)，且档位/编码单调。"""
    # 实测场景：864x486 / 629kbps 网站在线片源，balanced 档 0.72 → 约 453k
    got = target_bitrate_kbps(629, 486, quality="balanced", codec="h264")
    check("低码率源被源码率钳制（629k x 0.72 ≈ 453k）", 440 <= got <= 465, got)
    # 高码率源应受分辨率预算约束，而不是跟着源跑到 5.7M
    got2 = target_bitrate_kbps(8000, 1080, quality="balanced", codec="h264")
    check("高码率源受分辨率预算约束（<= 4500k）", got2 <= 4500, got2)
    hevc = target_bitrate_kbps(8000, 1080, quality="balanced", codec="hevc")
    check("HEVC 目标码率约为 H.264 的 75%", abs(hevc - got2 * 0.75) <= 2, (got2, hevc))
    strong = target_bitrate_kbps(8000, 1080, quality="fast", codec="h264")
    high = target_bitrate_kbps(8000, 1080, quality="high", codec="h264")
    check("档位单调（极致 < 推荐 < 轻度）", strong < got2 < high, (strong, got2, high))
    check("源码率未知时退回分辨率预算", target_bitrate_kbps(0, 720) == 2500)
    check("极低源码率有下限保护（>=120k）",
          target_bitrate_kbps(80, 1080, quality="fast") >= 120)


def test_rate_controlled_args_shape():
    """码率受控参数：必须有 -b:v / -maxrate / -bufsize，且不用 -q:v / -crf。"""
    ffmpeg = getattr(app, "FFMPEG_BIN", "") or ""
    if available_h264(ffmpeg) not in ("h264_videotoolbox", "libopenh264"):
        print("⏭️  跳过：本机 ffmpeg 无可用 H.264 编码器")
        return
    args = rate_controlled_args(ffmpeg, codec="h264", target_kbps=453, pix_fmt="yuv420p")
    check("含 -b:v 目标码率", "-b:v" in args and "453k" in args, args)
    check("含 -maxrate 峰值上限且大于目标",
          "-maxrate" in args and int(args[args.index("-maxrate") + 1].rstrip("k")) > 453, args)
    check("含 -bufsize 缓冲", "-bufsize" in args, args)
    check("不使用恒定质量 -q:v（正是它把产物放大 2~3 倍）", "-q:v" not in args, args)
    check("不使用 -crf（VideoToolbox 不支持）", "-crf" not in args, args)
    check("强制 yuv420p（兼容性）", "yuv420p" in args, args)
    fallback = rate_controlled_args(ffmpeg, codec="h264", target_kbps=0, pix_fmt="yuv420p")
    check("target<=0 时回退恒定质量模式（无码率依据不静默降质）", "-q:v" in fallback, fallback)


def test_probe_video_meta_reads_bitrate_and_resolution():
    """一次 ffprobe 取时长/分辨率/码率，供码率钳制使用；失败必须降级不抛错。"""
    if not _make_lowbitrate_mp4("/tmp/_vdl_unused.mp4"):
        print("⏭️  跳过：离线环境无可用 ffmpeg")
        return
    try:
        os.unlink("/tmp/_vdl_unused.mp4")
    except OSError:
        pass
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src.mp4")
        if not _make_lowbitrate_mp4(src, seconds=2):
            print("⏭️  跳过：测试源生成失败")
            return
        meta = _probe_video_meta(src)
        check("探测到时长", 1.5 <= meta["duration"] <= 3.0, meta["duration"])
        check("探测到分辨率 640x360", (meta["width"], meta["height"]) == (640, 360), meta)
        check("探测到合理码率（300k 目标 ±120% 容差）",
              100 <= meta["bit_rate"] / 1000 <= 700, meta["bit_rate"])
        bad = _probe_video_meta(os.path.join(td, "nope.mp4"))
        check("文件不存在时降级为零值而非抛错",
              bad["duration"] == 0 and bad["bit_rate"] == 0 and bad["width"] == 0, bad)


def test_low_bitrate_source_actually_shrinks():
    """防回归核心：对「已压过」的低码率源，结果必须真的小于原文件。

    旧实现走恒定质量（-q:v 60）：实测 629kbps 源产物达 284%，随后被「压缩后更大
    就保留原文件」守卫回退成副本 —— 用户白等一场。本用例锁死新行为。
    """
    ffmpeg = getattr(app, "FFMPEG_BIN", "") or ""
    if not (ffmpeg and os.path.isfile(ffmpeg)) or available_h264(ffmpeg) != "h264_videotoolbox":
        print("⏭️  跳过：需要带 h264_videotoolbox 的 ffmpeg")
        return
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "low.mp4")
        if not _make_lowbitrate_mp4(src, seconds=3, w=640, h=360, kbps=300):
            print("⏭️  跳过：测试源生成失败")
            return
        before = os.path.getsize(src)
        COMPRESS_JOBS["low1"] = {
            "status": "running", "stage": "", "progress": 0, "error": "",
            "out_path": "", "filename": "", "src_name": "low.mp4",
            "device_id": "t", "size_before": 0, "size_after": 0, "saving": 0.0, "note": "",
        }
        _run_compress("low1", src, "video", "balanced", src_is_temp=False, codec="h264")
        j = COMPRESS_JOBS["low1"]
        check("低码率源压缩完成", j["status"] == "completed", j.get("error"))
        check("产物确实小于源文件（不再回退原文件副本）",
              0 < j["size_after"] < before, f"{before} -> {j['size_after']}")
        check("saving 为正", j["saving"] > 0, j["saving"])
        check("记录了源码率/目标码率（可诊断）",
              j["src_kbps"] > 0 and j["target_kbps"] > 0, j)
        check("目标码率不超过源码率（钳制生效）",
              j["target_kbps"] < j["src_kbps"], (j["src_kbps"], j["target_kbps"]))
        check("记录了已用时间（前端显示等待预期）", j["elapsed"] > 0, j["elapsed"])
        COMPRESS_JOBS.pop("low1", None)


def test_transcode_gate_queues_when_full():
    """转码闸门：名额占满时新任务停在「排队中」，释放后能跑完。

    背景：VideoToolbox 硬编引擎共享，实测 3 路并发每路慢 2.9 倍；转码与下载共用
    8 worker 池，不设闸门会更慢。
    """
    if not _make_lowbitrate_mp4("/tmp/_vdl_unused2.mp4"):
        print("⏭️  跳过：离线环境无可用 ffmpeg")
        return
    try:
        os.unlink("/tmp/_vdl_unused2.mp4")
    except OSError:
        pass
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "q.mp4")
        if not _make_lowbitrate_mp4(src, seconds=2, w=320, h=240, kbps=250):
            print("⏭️  跳过：测试源生成失败")
            return
        _TRANSCODE_SEM.acquire()
        _TRANSCODE_SEM.acquire()          # 占满名额
        job_id = ""
        try:
            job_id = _submit_compress(src, "balanced", "t", src_name="q.mp4")
            time.sleep(1.0)
            j = COMPRESS_JOBS.get(job_id) or {}
            check("名额占满时任务停在「排队中」（未开跑）",
                  j.get("stage") == "排队中" and (j.get("progress") or 0) == 0, j.get("stage"))
        finally:
            _TRANSCODE_SEM.release()
            _TRANSCODE_SEM.release()
        for _ in range(100):
            if (COMPRESS_JOBS.get(job_id) or {}).get("status") != "running":
                break
            time.sleep(0.1)
        check("释放名额后任务能跑完",
              (COMPRESS_JOBS.get(job_id) or {}).get("status") == "completed",
              (COMPRESS_JOBS.get(job_id) or {}).get("error"))
        COMPRESS_JOBS.pop(job_id, None)


def test_queued_transcode_does_not_starve_shared_pool():
    """防回归：排队等名额的转码任务**不得占用**与下载共用的线程池。

    背景（2026-09-11 实修）：转码闸门最初写成在工作线程里 `_TRANSCODE_SEM.acquire()`
    **阻塞**等名额。闸门只有 2 个名额，于是同时提交 8 个视频就把整池（worker 数 =
    `VDL_BATCH_HARD_MAX` = 8）占满 —— 6 个线程白白卡在等名额上，下载 / 格式转换 /
    抠图 / 去水印全部饿死（用户表现：「点下载没反应」）。
    修法：非阻塞抢名额 + 定时器重投自己，等待中的任务不持有任何 worker。

    断言方式：占满名额后提交「与池容量等量」的视频任务，再用一个哨兵任务证明
    共享池仍有空闲名额 —— 旧写法下哨兵必然被饿死，测试变红。
    """
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "starve.mp4")
        if not _make_lowbitrate_mp4(src, seconds=1, w=320, h=240, kbps=250):
            print("⏭️  跳过：离线环境无可用 ffmpeg")
            return
        _TRANSCODE_SEM.acquire()
        _TRANSCODE_SEM.acquire()                       # 占满转码名额
        job_ids = []
        try:
            for _ in range(app.VDL_BATCH_HARD_MAX):    # 提交量 = 共享池容量
                job_ids.append(_submit_compress(src, "balanced", "t",
                                                src_name="starve.mp4"))
            deadline = time.time() + 5.0               # 确定性等待：全部转入排队态
            while time.time() < deadline:
                if all((COMPRESS_JOBS.get(j) or {}).get("stage") == "排队中"
                       for j in job_ids):
                    break
                time.sleep(0.05)
            queued = [j for j in job_ids
                      if (COMPRESS_JOBS.get(j) or {}).get("stage") == "排队中"]
            check("转码名额占满时全部视频任务停在「排队中」",
                  len(queued) == len(job_ids), f"{len(queued)}/{len(job_ids)}")
            done = threading.Event()
            app.executor.submit(lambda: done.set())
            check("排队中的转码没有占满共享线程池（下载/转换不会被饿死）",
                  done.wait(2.0), "哨兵任务 2 秒内未获执行 → 共享池被排队任务占满（旧写法必然如此）")
        finally:
            for j in job_ids:                          # 先摘登记表 → 在途重投变空操作
                COMPRESS_JOBS.pop(j, None)
            _TRANSCODE_SEM.release()
            _TRANSCODE_SEM.release()
            time.sleep(1.0)                            # 等在途重投自清
            for j in job_ids:
                for f in app.CONVERT_DIR.glob(f"compress_{j}.*"):
                    try:
                        f.unlink()
                    except OSError:
                        pass


def test_status_endpoints_are_not_rate_limited():
    """防回归：压缩进度轮询端点必须**免**限流，提交类端点必须**保留**限流。

    背景（2026-09-11 实机故障）：compress_status / compress_file 曾被误加限流，
    而额度只有 30 次/小时、前端每 1.5s 轮询一次 —— 一个 90 秒的转码任务需要约
    60 次轮询，跑到一半就被 429 掐断，进度条永久停在某个百分比（用户看到卡在
    32%），表现为「视频压缩好慢」，实际编码早已跑完。convert / matting /
    dewatermark 的状态端点本就不限流，压缩应与它们对齐。
    """
    import re as _re
    src = open(os.path.join(_SERVER_DIR, "routers", "compress.py"), encoding="utf-8").read()

    def _func_body(name):
        m = _re.search(rf"\ndef {name}\(.*?(?=\n@router|\ndef |\Z)", src, _re.S)
        return m.group(0) if m else ""

    for fn in ("compress_status", "compress_file"):
        body = _func_body(fn)
        check(f"{fn} 存在于 compress.py", bool(body))
        check(f"{fn} 不调用 _check_rate_limit（轮询不计配额）",
              bool(body) and "_check_rate_limit" not in body, body[:120])

    for fn in ("compress_local", "compress_finish"):
        body = _func_body(fn)
        check(f"{fn} 仍保留限流（防滥用不能一起被删）",
              bool(body) and "_check_rate_limit" in body)


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
    test_target_bitrate_clamps_to_source()
    test_rate_controlled_args_shape()
    test_probe_video_meta_reads_bitrate_and_resolution()
    test_low_bitrate_source_actually_shrinks()
    test_transcode_gate_queues_when_full()
    test_queued_transcode_does_not_starve_shared_pool()
    test_status_endpoints_are_not_rate_limited()
    print(f"\n通过: {PASS}  失败: {FAIL}")
    sys.exit(1 if FAIL else 0)
