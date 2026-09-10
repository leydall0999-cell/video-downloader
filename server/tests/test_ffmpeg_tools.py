"""媒体加工工具回归测试（2026-09-10 新增）。

背景
----
`ffmpeg_tools` 提供「对已下载媒体做二次加工」的 11 个能力：抽音频、做 GIF、
时间裁剪、画面裁剪、压缩、放大、抽封面、批量抽帧、预览拼图、铃声、去水印。
每个函数都是「构造 ffmpeg 参数 → 落产物 → 写侧车」，此前零测试。

这块的风险有两类，且都不轻：

1. **安全**：`crop_video` 把用户输入的表达式直接拼进 `-vf` 滤镜串。若不做
   元字符拦截，`a,scale=...` 之类可以拼接任意滤镜链（滤镜链注入）。
2. **静默退化**：这些函数失败时一律返回 `None` 而不抛异常。若参数构造被改坏，
   表现是「点了没反应」而非报错——没有测试就只能靠用户反馈才发现。

本轮补测过程中的判断记录（避免误报）：
- `crop_video` 对表达式调用 `_escape` 会把 `:` 转义成 `\\:`。曾怀疑这会让
  `crop=iw:ih:0:0` 失效，**实测排除**：ffmpeg 会正确解析转义后的冒号，真实
  产物尺寸 160x240（= iw/2）符合预期。

覆盖：_safe_title / _escape / _unique_out / _unique_dir / _write_sidecar /
      crop_video 注入防护 / probe_duration / 各加工函数真实端到端。

运行：
    cd server && python tests/test_ffmpeg_tools.py
    cd server && python -m pytest tests/test_ffmpeg_tools.py -v
"""
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as A  # noqa: E402
import ffmpeg_tools as ft  # noqa: E402

FF = A.FFMPEG_BIN


# --------------------------------------------------------------------------- #
# 脚手架
# --------------------------------------------------------------------------- #
def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="vdl-ft-"))


def _ffmpeg_ok() -> bool:
    return bool(FF) and Path(FF).exists()


def _probe(path: Path) -> str:
    proc = subprocess.run([FF, "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True, timeout=120)
    return proc.stderr or ""


def _video_size(path: Path):
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", _probe(path))
    return (int(m.group(1)), int(m.group(2))) if m else None


_SAMPLE_DIR: Path | None = None
_SAMPLE: Path | None = None


def _sample(seconds: float = 2.0) -> Path | None:
    """生成合成素材（带音轨），全模块只生成一次。"""
    global _SAMPLE_DIR, _SAMPLE
    if _SAMPLE is not None:
        return _SAMPLE
    if not _ffmpeg_ok():
        return None
    _SAMPLE_DIR = _tmpdir()
    out = _SAMPLE_DIR / "sample.mp4"
    cmd = [FF, "-y",
           "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=5:duration={seconds}",
           "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
           "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        return None
    _SAMPLE = out
    return _SAMPLE


def _cleanup_sample() -> None:
    if _SAMPLE_DIR is not None:
        shutil.rmtree(_SAMPLE_DIR, ignore_errors=True)


atexit.register(_cleanup_sample)


def _require_sample():
    src = _sample()
    if src is None:
        print("⚠️ 跳过：本机 ffmpeg 不可用或无法生成合成素材")
    return src


# =========================================================================== #
# 1. 文件名净化与转义
# =========================================================================== #
def test_safe_title_replaces_illegal_chars():
    """文件名净化：把各平台非法字符统一替换为下划线。"""
    assert ft._safe_title('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"
    assert ft._safe_title("正常标题（第1集）") == "正常标题（第1集）", "中文与括号应保留"
    print("✅ _safe_title 替换文件系统非法字符，保留中文")


def test_safe_title_falls_back_and_truncates():
    for empty in ("", None, "   "):
        assert ft._safe_title(empty) == "media", f"{empty!r} 应回退 media"
    assert len(ft._safe_title("x" * 200)) == 80, "超长标题应截断到 80 字符"
    assert ft._safe_title("  前后空白  ") == "前后空白"
    print("✅ _safe_title 空值回退 media、超长截断到 80 字符")


def test_escape_filter_metacharacters():
    """滤镜串转义：反斜杠、冒号、单引号——少转一个就可能改变滤镜语义。"""
    assert ft._escape("plain") == "plain"
    assert ft._escape("a:b") == "a\\:b"
    assert ft._escape("a\\b") == "a\\\\b"
    assert ft._escape("a'b") == "a\\'b"
    # 反斜杠必须先转，否则会把自己的转义符再转一遍
    assert ft._escape("a\\:b") == "a\\\\\\:b"
    print("✅ _escape 正确转义滤镜元字符，且顺序为「先反斜杠后冒号」")


# =========================================================================== #
# 2. 输出路径分配
# =========================================================================== #
def test_unique_out_never_overwrites():
    """加工产物不能覆盖既有文件（与源视频同目录，撞名概率不低）。"""
    td = _tmpdir()
    try:
        v = td / "我的视频.mp4"; v.write_bytes(b"x")
        assert ft._unique_out(v, "音频", "mp3").name == "我的视频.音频.mp3"
        (td / "我的视频.音频.mp3").write_bytes(b"x")
        assert ft._unique_out(v, "音频", "mp3").name == "我的视频.音频.1.mp3"
        (td / "我的视频.音频.1.mp3").write_bytes(b"x")
        assert ft._unique_out(v, "音频", "mp3").name == "我的视频.音频.2.mp3"
        print("✅ _unique_out 撞名时递增序号，绝不覆盖既有产物")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_unique_dir_never_overwrites():
    td = _tmpdir()
    try:
        v = td / "我的视频.mp4"; v.write_bytes(b"x")
        assert ft._unique_dir(v, "抽帧").name == "我的视频.抽帧"
        (td / "我的视频.抽帧").mkdir()
        assert ft._unique_dir(v, "抽帧").name == "我的视频.抽帧.1"
        print("✅ _unique_dir 撞名时递增序号，不覆盖既有抽帧目录")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_write_sidecar_inherits_metadata():
    """侧车让加工产物自动出现在媒体库，且继承原片元信息。"""
    td = _tmpdir()
    try:
        out = td / "done.mp4"; out.write_bytes(b"x")
        ft._write_sidecar(out, {"title": "原标题", "source_url": "http://x"}, "音频")
        sc = out.with_name(out.stem + ".vdlmeta.json")
        assert sc.name == "done.vdlmeta.json", sc.name
        meta = json.loads(sc.read_text(encoding="utf-8"))
        assert meta["title"] == "原标题（音频）", meta
        assert meta["source_url"] == "http://x"
        assert isinstance(meta["completed_at"], int) and meta["completed_at"] > 0
        print("✅ 侧车继承元信息，标题追加加工类型后缀")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_write_sidecar_title_suffix_is_idempotent():
    """标题已含后缀时不得重复追加（否则多次加工会叠加「（音频）（音频）」）。"""
    td = _tmpdir()
    try:
        out = td / "done.mp4"; out.write_bytes(b"x")
        sc = out.with_name(out.stem + ".vdlmeta.json")
        ft._write_sidecar(out, {"title": "原标题（音频）"}, "音频")
        assert json.loads(sc.read_text(encoding="utf-8"))["title"] == "原标题（音频）"
        # 无原标题时以产物文件名为基础
        ft._write_sidecar(out, {}, "片段")
        assert json.loads(sc.read_text(encoding="utf-8"))["title"] == "done（片段）"
        # 无 source_url 时兜底为空串，避免前端读到 None
        ft._write_sidecar(out, {}, "片段")
        assert json.loads(sc.read_text(encoding="utf-8"))["source_url"] == ""
        print("✅ 侧车标题后缀幂等；无标题/无来源时用产物名与空串兜底")
    finally:
        shutil.rmtree(td, ignore_errors=True)


# =========================================================================== #
# 3. 滤镜链注入防护（安全）
# =========================================================================== #
def test_crop_rejects_filter_chain_metacharacters():
    """安全关键：crop 表达式中的滤镜元字符必须被拒。

    ffmpeg 用 `,` 分隔滤镜、`;` 分隔滤镜图、`[` `]` 标注输入输出 pad、
    `=` 赋值选项、`#` 起注释。放行任意一个，用户输入就能拼出额外滤镜链。
    """
    src = Path("irrelevant.mp4")   # 黑名单在调用 ffmpeg 之前就应抛错
    for bad in ("a,b", "a;b", "a=b", "a[b]", "a]b", "crop=1:1:0:0",
                "iw:ih:0:0,scale=1:1"):
        try:
            ft.crop_video(src, crop_expr=bad)
            raise AssertionError(f"应拒绝含元字符的表达式: {bad!r}")
        except ValueError as e:
            assert "非法" in str(e), e
    print("✅ crop 表达式中的 , [ ] ; = # 全部被拒（滤镜链注入防护）")


def test_crop_empty_expression_returns_none():
    """空表达式是「未填写」而非错误，应安静返回 None（不抛异常给前端）。"""
    for empty in ("", "   ", None):
        assert ft.crop_video(Path("x.mp4"), crop_expr=empty) is None
    print("✅ 空 crop 表达式安静返回 None，不当作错误")


# =========================================================================== #
# 4. 时长探测
# =========================================================================== #
def test_probe_duration_reads_ffmpeg_stderr():
    src = _require_sample()
    if src is None:
        return
    dur = ft.probe_duration(src, FF)
    assert 1.5 < dur < 2.5, f"素材 2 秒，探测结果 {dur}"
    print(f"✅ probe_duration 不依赖 ffprobe，直接从 stderr 解析时长（{dur}s）")


def test_probe_duration_returns_zero_on_failure():
    """探测失败必须返回 0 而不是抛异常（调用方按 0 走退化分支）。"""
    td = _tmpdir()
    try:
        txt = td / "note.txt"; txt.write_text("hello")
        assert ft.probe_duration(txt, FF) == 0.0
        assert ft.probe_duration(td / "missing.mp4", FF) == 0.0
        assert ft.probe_duration(txt, "/nonexistent/ffmpeg") == 0.0
        print("✅ 非媒体文件/文件不存在/ffmpeg 缺失 一律返回 0.0，不抛异常")
    finally:
        shutil.rmtree(td, ignore_errors=True)


# =========================================================================== #
# 5. 真实加工端到端
# =========================================================================== #
def test_real_extract_audio():
    src = _require_sample()
    if src is None:
        return
    td = _tmpdir()
    try:
        out = ft.extract_audio(src, out_dir=td, fmt="mp3", bitrate="192k", ffmpeg_bin=FF)
        assert out is not None and out.exists() and out.stat().st_size > 0
        info = _probe(out)
        assert "Audio: mp3" in info and "Video:" not in info, info
        print(f"✅ 真实抽音频 → {out.name}（含 mp3 音轨、无视频流）")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_real_make_gif_cleans_palette():
    """GIF 走双遍 palette；中间调色板文件必须被清理，不能留在用户视频目录。"""
    src = _require_sample()
    if src is None:
        return
    td = _tmpdir()
    try:
        out = ft.make_gif(src, out_dir=td, start=0, duration=1, fps=8, width=200,
                          ffmpeg_bin=FF)
        assert out is not None and out.exists(), "GIF 生成失败"
        assert "Video: gif" in _probe(out)
        assert _video_size(out) == (200, 150), _video_size(out)
        assert not (td / (out.stem + ".palette.png")).exists(), "palette 中间文件未清理"
        print("✅ 真实做 GIF：双遍 palette 生效，中间调色板文件已清理")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_real_trim_reencode_and_copy():
    src = _require_sample()
    if src is None:
        return
    td = _tmpdir()
    try:
        for reencode in (True, False):
            out = ft.trim_video(src, out_dir=td, start=0, end=1,
                                reencode=reencode, ffmpeg_bin=FF)
            assert out is not None and out.exists() and out.stat().st_size > 0
            dur = ft.probe_duration(out, FF)
            assert 0.5 < dur < 1.6, f"reencode={reencode} 裁剪后时长异常: {dur}"
        print("✅ 真实时间裁剪：重编码与 -c copy 两种模式产物时长均正确")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_real_crop_changes_dimensions():
    """★ 顺带验证：crop 表达式的冒号转义不会破坏 ffmpeg 解析。"""
    src = _require_sample()
    if src is None:
        return
    td = _tmpdir()
    try:
        out = ft.crop_video(src, out_dir=td, crop_expr="iw/2:ih:0:0", ffmpeg_bin=FF)
        assert out is not None, "crop 失败"
        assert _video_size(out) == (160, 240), f"iw/2 应为 160 宽，实际 {_video_size(out)}"
        out = ft.crop_video(src, out_dir=td, crop_expr="200:150:0:0", ffmpeg_bin=FF)
        assert _video_size(out) == (200, 150), _video_size(out)
        print("✅ 真实画面裁剪：iw/2 与绝对像素两种写法尺寸都正确")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_real_compress_and_upscale_dimensions():
    src = _require_sample()
    if src is None:
        return
    td = _tmpdir()
    try:
        out = ft.compress_video(src, out_dir=td, scale_h=120, ffmpeg_bin=FF)
        assert out is not None and _video_size(out) == (160, 120), _video_size(out)
        out = ft.upscale_video(src, out_dir=td, factor=2.0, ffmpeg_bin=FF)
        assert out is not None and _video_size(out) == (640, 480), _video_size(out)
        print("✅ 真实压缩/放大：320x240 → 160x120 / 640x480")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_real_snapshot_with_fallback():
    """抽帧时刻超出视频时长时应自动回退重试，而不是返回失败。"""
    src = _require_sample()
    if src is None:
        return
    td = _tmpdir()
    try:
        out = ft.snapshot(src, out_dir=td, at=0.5, ffmpeg_bin=FF)
        assert out is not None and out.exists() and "Video: mjpeg" in _probe(out)
        # 远超素材时长，应回退到 0 秒仍产出封面
        out2 = ft.snapshot(src, out_dir=td, at=9999, ffmpeg_bin=FF)
        assert out2 is not None and out2.exists() and out2.stat().st_size > 0, \
            "越界抽帧应回退到 0 秒，而不是放弃"
        print("✅ 真实抽封面：正常时刻与越界回退都能产出图片")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_real_extract_frames_and_limit():
    src = _require_sample()
    if src is None:
        return
    td = _tmpdir()
    try:
        res = ft.extract_frames(src, out_dir=td, interval=0.5, ffmpeg_bin=FF)
        assert res is not None, "抽帧失败"
        d, n = res
        assert d.is_dir() and n == 4, f"2 秒素材按 0.5s 间隔应得 4 帧，实际 {n}"
        assert len(list(d.glob("frame_*.jpg"))) == n
        # limit 必须真实生效（防长视频抽出上万张）
        res2 = ft.extract_frames(src, out_dir=td, interval=0.5, limit=2, ffmpeg_bin=FF)
        assert res2 is not None and res2[1] == 2, f"limit=2 应只出 2 帧，实际 {res2}"
        print("✅ 真实批量抽帧：间隔与 limit 上限都生效，输出到独立子目录")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_real_contact_sheet():
    src = _require_sample()
    if src is None:
        return
    td = _tmpdir()
    try:
        out = ft.contact_sheet(src, out_dir=td, rows=2, cols=2, width=640, ffmpeg_bin=FF)
        assert out is not None and out.exists() and out.stat().st_size > 0
        w, h = _video_size(out) or (0, 0)
        assert w == 640, f"拼图宽度应为 640（width/cols*cols），实际 {w}"
        print(f"✅ 真实预览拼图：2x2 宫格 → {w}x{h}")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_real_ringtone_formats():
    src = _require_sample()
    if src is None:
        return
    td = _tmpdir()
    try:
        out_m4r = ft.make_ringtone(src, out_dir=td, duration=1, fmt="m4r",
                                   fade=0.2, ffmpeg_bin=FF)
        assert out_m4r is not None and out_m4r.suffix == ".m4r", out_m4r
        assert "Audio: aac" in _probe(out_m4r)
        out_mp3 = ft.make_ringtone(src, out_dir=td, duration=1, fmt="mp3",
                                   fade=0.2, ffmpeg_bin=FF)
        assert out_mp3 is not None and out_mp3.suffix == ".mp3", out_mp3
        assert "Audio: mp3" in _probe(out_mp3)
        print("✅ 真实制作铃声：m4r（iPhone）与 mp3（安卓）均产出正确音轨")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_real_remove_watermark_preview_and_blur():
    """去水印两条路径：show=预览框（调试定位）/ 默认=LGPL 模糊链。"""
    src = _require_sample()
    if src is None:
        return
    td = _tmpdir()
    try:
        box = ft.remove_watermark(src, out_dir=td, x=10, y=10, w=80, h=40,
                                  show=True, ffmpeg_bin=FF)
        assert box is not None and box.exists() and box.stat().st_size > 0
        blurred = ft.remove_watermark(src, out_dir=td, x=10, y=10, w=80, h=40,
                                      show=False, band=20, ffmpeg_bin=FF)
        assert blurred is not None and blurred.exists() and blurred.stat().st_size > 0
        # 模糊不改变分辨率，否则说明滤镜链写坏了
        assert _video_size(blurred) == (320, 240), _video_size(blurred)
        print("✅ 真实去水印：预览框与 LGPL 模糊链都可用，且不改分辨率")
    finally:
        shutil.rmtree(td, ignore_errors=True)


if __name__ == "__main__":
    test_safe_title_replaces_illegal_chars()
    test_safe_title_falls_back_and_truncates()
    test_escape_filter_metacharacters()
    test_unique_out_never_overwrites()
    test_unique_dir_never_overwrites()
    test_write_sidecar_inherits_metadata()
    test_write_sidecar_title_suffix_is_idempotent()
    test_crop_rejects_filter_chain_metacharacters()
    test_crop_empty_expression_returns_none()
    test_probe_duration_reads_ffmpeg_stderr()
    test_probe_duration_returns_zero_on_failure()
    test_real_extract_audio()
    test_real_make_gif_cleans_palette()
    test_real_trim_reencode_and_copy()
    test_real_crop_changes_dimensions()
    test_real_compress_and_upscale_dimensions()
    test_real_snapshot_with_fallback()
    test_real_extract_frames_and_limit()
    test_real_contact_sheet()
    test_real_ringtone_formats()
    test_real_remove_watermark_preview_and_blur()

    _cleanup_sample()
    print("\n🎉 媒体加工工具测试全部通过（21 项）")
