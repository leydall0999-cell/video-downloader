"""ffmpeg 原生渲染模块: 与 edit.py 等价的成片逻辑, 但全程走 ffmpeg C 路径(不走 moviepy 逐帧 Python 合成)。

性能对比: moviepy 的 concatenate_videoclips(method="compose") + TextClip 是主要瓶颈(逐帧 Python 合成 + 全量重编码)。
本模块把每段先各自编码为中间 mp4, 最后用 concat demuxer 拼接(不重编码), 渲染速度通常快 10~40 倍。

用法(与 edit.py 完全一致的 build 签名, 供 process.py 切换):
    import edit_ffmpeg
    edit_ffmpeg.build(video_path, script_path, out_path, vertical=..., own_voice=..., voice_override=..., original_speed=...)

注意: 依赖系统 ffmpeg(ffprobe), 且需中文可渲染字体(与 config.FONT 一致)。
"""
import os
import sys
import json
import asyncio
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import OUTPUT, WORK, VOICE, FONT, SUBTITLE_SIZE, ORIGINAL_DUCK

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


def _run(cmd):
    """以列表形式执行命令(无 shell), 返回 (returncode, stderr)。"""
    p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return p.returncode, p.stderr.decode("utf-8", "ignore")


def _probe_duration(path):
    out = subprocess.check_output(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path]).strip()
    return float(out)


def _gen_voice(text, out_path, voice):
    async def _run():
        import edge_tts
        comm = edge_tts.Communicate(text, voice)
        await comm.save(out_path)
    asyncio.run(_run())


def _valid_audio(path):
    """音频文件非空且能被 ffprobe 解析, 否则视为 edge_tts 抖动产生的空/损坏文件。"""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    try:
        subprocess.check_output(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def _ensure_voice(text, path, voice, max_retry=3):
    """生成旁白音频并校验; 空文件/损坏则删除重试; 仍失败则删除标记无效。"""
    if _valid_audio(path):
        return True
    for _ in range(max_retry):
        if os.path.exists(path):
            os.remove(path)
        try:
            _gen_voice(text, path, voice)
        except Exception:
            pass
        if _valid_audio(path):
            return True
    if os.path.exists(path):
        os.remove(path)
    return False


def _wrap(text, max_chars=20):
    lines, cur = [], ""
    for ch in text:
        cur += ch
        if ch in "，。！？、；：,.!? " or len(cur) >= max_chars:
            lines.append(cur.strip())
            cur = ""
    if cur.strip():
        lines.append(cur.strip())
    return "\n".join(lines)


def _font_name():
    """中文字体族名(交给 fontconfig / libass 解析)。"""
    return "PingFang SC"


def _render_subtitle_png(text, out_path, w=480, h=854, size=SUBTITLE_SIZE):
    """用 PIL 把口播文案渲染成透明 PNG(白字黑描边, 底部居中), 交 ffmpeg overlay 烧入。

    规避: 当前 ffmpeg 编译版未带 libass/subtitles 与 libfreetype/drawtext, 故字幕改用
    离屏渲染成图 + overlay(核心滤镜, 各平台通用)。仅生成静态图, 不逐帧处理, 开销极小。
    """
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(FONT, size)
    except Exception:
        font = ImageFont.load_default()
    lines = _wrap(text, 20).split("\n")
    line_h = size + 10
    total_h = line_h * len(lines)
    y = h - 70 - total_h
    cx = w // 2
    for ln in lines:
        bbox = draw.textbbox((0, 0), ln, font=font)
        tw = bbox[2] - bbox[0]
        draw.text((cx - tw // 2, y), ln, font=font,
                  fill=(255, 255, 255, 255), stroke_width=3,
                  stroke_fill=(0, 0, 0, 255))
        y += line_h
    img.save(out_path)


def _atempo_chain(ratio):
    """atempo 只接受 0.5~2.0, 超出需链式。ratio>0 且为正。"""
    if ratio <= 0:
        ratio = 1.0
    parts = []
    r = ratio
    while r > 2.0:
        parts.append("atempo=2.0")
        r /= 2.0
    while r < 0.5:
        parts.append("atempo=0.5")
        r /= 0.5
    parts.append(f"atempo={r:.4f}")
    return ",".join(parts)


def build(video_path, script_path, out_path,
          vertical=False, own_voice=False, voice_override=None,
          original_speed=False):
    with open(script_path, encoding="utf-8") as f:
        script = json.load(f)
    voice = voice_override or script.get("voice", VOICE)
    segs = script.get("segments", [])
    if not segs:
        print("[错误] script.json 没有 segments")
        sys.exit(1)

    dur_total = _probe_duration(video_path)

    inter_dir = os.path.join(WORK, "_seg_ff")
    os.makedirs(inter_dir, exist_ok=True)
    intermediates = []

    for i, seg in enumerate(segs):
        narration = seg.get("narration", "").strip()
        if not narration:
            continue
        s = seg.get("start", 0)

        # 旁白音频(生成后校验, 避免 edge_tts 偶发空文件导致后续 ffprobe 崩溃)
        own = os.path.join(WORK, f"{i}.mp3")
        aip = os.path.join(WORK, f"voice_{i}.mp3")
        if own_voice and _valid_audio(own):
            voice_path = own
        else:
            if not _ensure_voice(narration, aip, voice):
                print(f"[跳过] 第{i+1}段 旁白生成失败(空文件/损坏), 跳过该段")
                continue
            voice_path = aip
        vdur = _probe_duration(voice_path)

        # 画面窗口
        if original_speed:
            e = min(s + vdur, dur_total - 0.1)
        else:
            e = min(seg.get("end", s + vdur), dur_total - 0.1)
        if e <= s:
            print(f"[跳过] 第{i+1}段时长异常 start={s} end={e}")
            continue
        win = e - s
        vratio = win / vdur if vdur > 0 else 1.0  # 视频伸缩到旁白时长
        aratio = win / vdur if vdur > 0 else 1.0  # 原声同步伸缩

        # 字幕: PIL 渲染成透明 PNG, 交给 overlay 烧入(规避缺 libass/drawtext 的 ffmpeg 构建)
        png_path = f"/tmp/vdl_sub_{i}.png"
        _render_subtitle_png(narration, png_path)

        inter_path = os.path.join(inter_dir, f"seg_{i}.mp4")
        intermediates.append(inter_path)

        # 视频链: trim -> 竖屏裁切 -> 伸缩到 vdur
        v_filters = (
            f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS"
        )
        if vertical:
            v_filters += ",scale=480:854:force_original_aspect_ratio=increase,crop=480:854"
        v_filters += f",setpts={vratio:.4f}*PTS[vpre]"
        # 字幕叠图(overlay 为核心滤镜, 各平台通用)
        sub_filter = f"[vpre][2:v]overlay=0:0[v]"
        v_filters += f";{sub_filter}"

        # 音频链: 原声压低 + 伸缩 -> 与旁白混流
        a_filters = (
            f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS,"
            f"volume={ORIGINAL_DUCK:.2f},{_atempo_chain(aratio)},aresample=44100[oa];"
            f"[1:a]aresample=44100[a1];"
            f"[oa][a1]amix=inputs=2:duration=longest:dropout_transition=0[a]"
        )

        cmd = [
            FFMPEG, "-y",
            "-i", video_path,
            "-i", voice_path,
            "-i", png_path,
            "-filter_complex", v_filters + ";" + a_filters,
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-r", "30", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "44100", "-ac", "2",
            "-t", f"{vdur:.3f}",
            "-movflags", "+faststart",
            inter_path,
        ]
        rc, err = _run(cmd)
        if rc != 0:
            print(f"[错误] 第{i+1}段渲染失败:\n{err[-800:]}")
            sys.exit(1)
        print(f"  ✓ 第{i+1}段  {s:.1f}-{e:.1f}s  「{narration[:18]}...」 -> {vdur:.1f}s")

    if not intermediates:
        print("[错误] 没有可用片段")
        sys.exit(1)

    # 拼接(不重编码)
    print("拼接成片 ...")
    list_path = os.path.join(inter_dir, "list.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for p in intermediates:
            f.write(f"file '{p}'\n")
    cmd = [
        FFMPEG, "-y",
        "-f", "concat", "-safe", "0", "-i", list_path,
        "-c", "copy",
        out_path,
    ]
    rc, err = _run(cmd)
    if rc != 0:
        print(f"[错误] 拼接失败:\n{err[-800:]}")
        sys.exit(1)
    print(f"\n✅ 成片已生成(ffmpeg): {out_path}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: python scripts/edit_ffmpeg.py <视频> <script.json> [输出mp4]")
        sys.exit(1)
    vp = sys.argv[1]
    sp = sys.argv[2]
    op = sys.argv[3] if len(sys.argv) > 3 else os.path.join(
        OUTPUT, os.path.splitext(os.path.basename(vp))[0] + "_ffmpeg成片.mp4")
    build(vp, sp, op)
