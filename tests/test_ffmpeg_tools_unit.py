"""ffmpeg_tools 单测：mock 掉 subprocess（不需要机器装 ffmpeg）。

对应 mock-native-dep-tests skill 的「mock 外部依赖让逻辑可测」原则——
ffmpeg 是外部二进制，CI/精简环境没有它时，纯靠 subprocess 调用的逻辑无法验证。
这里把 ffmpeg_tools.subprocess.run 替成假对象，断言命令构造是否正确。

运行：PYTHONPATH=server:tests python -m pytest tests/test_ffmpeg_tools_unit.py -q
"""
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import ffmpeg_tools as ft


class _FakeRun:
    """替换 subprocess.run：记录每次调用的命令，始终返回成功。"""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *a, **k):
        self.calls.append(list(cmd))
        return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    @property
    def last(self) -> list[str]:
        return self.calls[-1]


def _vid(name: str = "movie.mp4") -> Path:
    # 用临时风格的假路径，不接触真实文件系统
    return Path(f"/tmp/vdl_test/{name}")


# --------------------------------------------------------------------------- #
# 纯逻辑（不碰 subprocess）
# --------------------------------------------------------------------------- #
def test_escape_filter_chars():
    assert ft._escape("a:b\\c'd") == r"a\:b\\c\'d"


def test_safe_title_strips_illegal_chars_and_caps():
    assert ft._safe_title('a/b:c') == "a_b_c"
    assert ft._safe_title("").startswith("media")


def test_probe_duration_parses_stderr():
    with patch.object(ft.subprocess, "run",
                      lambda cmd, *a, **k: CompletedProcess(cmd, 0, "", "  Duration: 00:01:30.50, start: 0.000")):
        assert ft.probe_duration(_vid()) == 90.5


def test_probe_duration_returns_zero_on_failure():
    with patch.object(ft.subprocess, "run",
                      lambda cmd, *a, **k: CompletedProcess(cmd, 1, "", "nothing")):
        assert ft.probe_duration(_vid()) == 0.0


def test_crop_empty_expr_returns_none_without_calling_ffmpeg():
    fake = _FakeRun()
    with patch.object(ft.subprocess, "run", fake):
        assert ft.crop_video(_vid(), crop_expr="   ") is None
    assert fake.calls == [], "空 crop_expr 不应触发任何 ffmpeg 调用"


def test_crop_rejects_filter_injection():
    # 滤镜链注入防护：含 , [ ] = ; # 等滤镜图元字符必须被拒绝
    for bad in ("iw:ih,split[a][b]", "iw:ih;reverse", "iw:ih#foo", "iw:ih=a"):
        try:
            ft.crop_video(_vid(), crop_expr=bad)
            assert False, f"应拒绝恶意裁剪表达式: {bad}"
        except ValueError:
            pass
    # 合法表达式仍可用，且确实触发 ffmpeg（mock 下不依赖真实文件落盘）
    fake = _FakeRun()
    with patch.object(ft.subprocess, "run", fake):
        ft.crop_video(_vid(), crop_expr="iw/2:ih:0:0")
    assert fake.calls, "合法 crop_expr 应触发 ffmpeg"
    assert any("crop=" in " ".join(c) for c in fake.calls), "命令应含 crop 滤镜"


# --------------------------------------------------------------------------- #
# 命令构造（mock subprocess.run）
# --------------------------------------------------------------------------- #
def test_extract_audio_mp3_cmd():
    fake = _FakeRun()
    with patch.object(ft.subprocess, "run", fake):
        ft.extract_audio(_vid(), fmt="mp3", bitrate="192k")
    cmd = fake.last
    assert "-vn" in cmd and "-c:a" in cmd and "libmp3lame" in cmd
    assert "-b:a" in cmd and "192k" in cmd
    assert cmd[-1].endswith(".音频.mp3")


def test_extract_audio_opus_cmd():
    fake = _FakeRun()
    with patch.object(ft.subprocess, "run", fake):
        ft.extract_audio(_vid(), fmt="opus")
    assert "libopus" in fake.last


def test_make_gif_two_pass_palette():
    fake = _FakeRun()
    with patch.object(ft.subprocess, "run", fake):
        ft.make_gif(_vid(), fps=12, width=480)
    assert len(fake.calls) == 2, "GIF 必须双遍（palettegen + paletteuse）"
    joined = " ".join(" ".join(c) for c in fake.calls)
    assert "palettegen" in joined and "paletteuse" in joined


def test_trim_video_copy_mode():
    fake = _FakeRun()
    with patch.object(ft.subprocess, "run", fake):
        ft.trim_video(_vid(), reencode=False)
    cmd = fake.last
    assert "-c" in cmd and "copy" in cmd
    assert "libx264" not in cmd


def test_compress_video_scale_and_crf():
    fake = _FakeRun()
    with patch.object(ft.subprocess, "run", fake):
        ft.compress_video(_vid(), scale_h=720, crf=28)
    cmd = fake.last
    assert "scale=-2:720" in cmd
    assert "-crf" in cmd and "28" in cmd


def test_upscale_video_lanczos_and_unsharp():
    fake = _FakeRun()
    with patch.object(ft.subprocess, "run", fake):
        ft.upscale_video(_vid(), factor=2.0, sharpen=True)
    vf = next((c for c in fake.last if c.startswith("scale=")), "")
    assert "lanczos" in vf and "unsharp" in vf


def test_make_ringtone_m4r_uses_ipod_container():
    fake = _FakeRun()
    with patch.object(ft.subprocess, "run", fake):
        ft.make_ringtone(_vid(), fmt="m4r", fade=1.0)
    cmd = fake.last
    assert "-f" in cmd and "ipod" in cmd  # .m4r 必须显式 ipod 容器
    assert any("afade" in c for c in cmd)  # fade 淡入淡出滤镜已拼入


def test_contact_sheet_builds_tile_filter():
    fake = _FakeRun()
    with patch.object(ft.subprocess, "run", fake):
        ft.contact_sheet(_vid(), rows=3, cols=4, width=1280)
    vf = next((c for c in fake.last if "tile=" in c), "")
    assert "tile=4x3" in vf


def test_dewatermark_delogo_cmd():
    """去水印使用 delogo 滤镜，参数正确注入命令。"""
    fake = _FakeRun()
    with patch.object(ft.subprocess, "run", fake):
        ft.remove_watermark(_vid(), x=50, y=30, w=120, h=80, band=15)
    vf = next((c for c in fake.last if "delogo=" in c), "")
    assert "x=50" in vf
    assert "y=30" in vf
    assert "w=120" in vf
    assert "h=80" in vf
    assert "band=15" in vf


def test_dewatermark_show_drawbox():
    """show 模式只画框，不去水印。"""
    fake = _FakeRun()
    with patch.object(ft.subprocess, "run", fake):
        ft.remove_watermark(_vid(), x=10, y=20, w=200, h=100, show=True)
    vf = next((c for c in fake.last if "drawbox=" in c), "")
    assert "x=10" in vf
    assert "y=20" in vf
    assert "w=200" in vf
    assert "h=100" in vf
    assert "green" in vf
    # show 模式不用 delogo
    assert not any("delogo=" in c for c in fake.last)
