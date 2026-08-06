"""自动剪辑模块: 解说词(script.json) + 源视频 -> 带字幕/旁白的成片

解说词 schema (script.json):
{
  "title": "视频标题",
  "voice": "zh-CN-XiaoxiaoNeural",   // 可选, 覆盖默认音色
  "segments": [
    {
      "start": 12.3,                  // 源视频起始(秒)
      "end": 25.8,                    // 源视频结束(秒)
      "narration": "口播文案(同时作为字幕与AI旁白)",
      "note": "画面提示, 仅备注不显示(可选)"
    }
  ]
}

用法:
    python scripts/edit.py <视频路径> <script.json> [输出mp4]
    python scripts/edit.py input/素材.mp4 work/素材.script.json output/成片.mp4

参数(也可由 process.py 传入):
    vertical   = True 时输出竖屏 9:16(封面式裁切, 默认 480x854 抖音兼容; 源低清时最稳), 默认横屏沿用源画幅
    own_voice  = True 时优先用 work/<i>.mp3(你自己录的), 找不到才用 AI 生成

说明:
    - 每段按 start/end 切源视频; 若旁白比画面长, 画面定格最后一帧(解说常用)
    - 原片人声按 ORIGINAL_DUCK 压低, 突出 AI 旁白
    - 想换自己的声音: 把每段 narration 对应的录音(同名 mp3)放进 work/ 并设 own_voice=True
"""
import os
import sys
import json
import asyncio
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import OUTPUT, WORK, VOICE, FONT, SUBTITLE_SIZE, ORIGINAL_DUCK


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


def _gen_voice(text, out_path, voice):
    async def _run():
        import edge_tts
        comm = edge_tts.Communicate(text, voice)
        await comm.save(out_path)
    asyncio.run(_run())


def _to_vertical(clip, target_w=480, target_h=854):
    """封面式裁切: 先等比放大到目标高度, 再居中裁掉两侧 -> 9:16 (480x854 抖音兼容, 源低清时最稳)"""
    clip = clip.resized(height=target_h)
    if clip.w > target_w:
        clip = clip.cropped(x_center=clip.w // 2, width=target_w)
    return clip


def build(video_path, script_path, out_path,
          vertical=False, own_voice=False, voice_override=None,
          original_speed=False):
    from moviepy import (VideoFileClip, AudioFileClip, TextClip,
                         CompositeVideoClip, CompositeAudioClip,
                         concatenate_videoclips)

    with open(script_path, encoding="utf-8") as f:
        script = json.load(f)
    voice = voice_override or script.get("voice", VOICE)
    segs = script.get("segments", [])
    if not segs:
        print("[错误] script.json 没有 segments")
        sys.exit(1)

    # 用 ffprobe 取总时长, 避免为拿时长一直开着视频
    dur_total = float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", video_path]).strip())

    clips = []
    opened = []  # 渲染前统一关闭
    vclip = VideoFileClip(video_path)
    opened.append(vclip)
    for i, seg in enumerate(segs):
        s = seg.get("start", 0)
        narration = seg.get("narration", "").strip()
        if not narration:
            continue

        # 旁白音频(先生成, 以便按真实时长决定画面窗口)
        own = os.path.join(WORK, f"{i}.mp3")
        aip = os.path.join(WORK, f"voice_{i}.mp3")
        if own_voice and os.path.exists(own):
            voice_clip = AudioFileClip(own)
        else:
            _gen_voice(narration, aip, voice)
            voice_clip = AudioFileClip(aip)
        opened.append(voice_clip)
        vdur = voice_clip.duration

        # 画面窗口: 原速模式=窗口跟随旁白真实时长(s -> s+vdur), 画面原速不拉伸;
        #          否则用解说词指定的 [start,end], 再把画面时长对齐旁白(可能快/慢放)
        if original_speed:
            e = min(s + vdur, dur_total - 0.1)
        else:
            e = min(seg.get("end", s + vdur), dur_total - 0.1)
        if e <= s:
            print(f"[跳过] 第{i+1}段时长异常 start={s} end={e}")
            continue

        src = vclip.subclipped(s, e)
        opened.append(src)

        # 字幕(自动换行 + 黑描边)
        sub = TextClip(
            text=_wrap(narration),
            font=FONT,
            font_size=SUBTITLE_SIZE,
            color="white",
            stroke_color="black",
            stroke_width=3,
            text_align="center",
        )
        sub = sub.with_position(("center", "bottom")).with_duration(vdur)
        opened.append(sub)

        # 原片人声压低, 时长限制在自身范围内避免越界空帧
        audios = []
        if src.audio is not None:
            duck_dur = min(src.audio.duration, vdur)
            ducked = src.audio.with_volume_scaled(ORIGINAL_DUCK).with_duration(duck_dur)
            audios.append(ducked)
        audios.append(voice_clip)
        comp_audio = CompositeAudioClip(audios)

        # 画面: 原速模式不拉伸(s->e 原速播放); 否则对齐旁白时长(解说常用)
        if original_speed:
            vid = src
        else:
            vid = src.with_duration(vdur)
        vid = CompositeVideoClip([vid, sub]).with_audio(comp_audio)

        if vertical:
            vid = _to_vertical(vid)

        clips.append(vid)
        print(f"  ✓ 第{i+1}段  {s:.1f}-{e:.1f}s  「{narration[:18]}...」")

    if not clips:
        print("[错误] 没有可用片段")
        for c in opened:
            try: c.close()
            except Exception: pass
        sys.exit(1)

    print("拼接成片 ...")
    final = concatenate_videoclips(clips, method="compose")
    final.write_videofile(out_path, codec="libx264", audio_codec="aac",
                          fps=30, preset=("ultrafast" if vertical else "medium"))
    final.close()
    for c in opened:
        try:
            c.close()
        except Exception:
            pass
    print(f"\n✅ 成片已生成: {out_path}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: python scripts/edit.py <视频> <script.json> [输出mp4]")
        sys.exit(1)
    vp = sys.argv[1]
    sp = sys.argv[2]
    op = sys.argv[3] if len(sys.argv) > 3 else os.path.join(
        OUTPUT, os.path.splitext(os.path.basename(vp))[0] + "_成片.mp4")
    build(vp, sp, op)
