"""解说视频一键工作台

把视频丢进 input/ 后, 这里一条命令跑完整流程:
    转写 -> (自动解说词草稿 / 交给 WorkBuddy 精修) -> 带字幕旁白的成片

用法(都在 commentary-pipeline/ 目录下运行, 用 managed python):
----------------------------------------------------------------
# 1) 全自动草稿(解说词由转写稿自动生成, 免人工, 适合先快速出片看效果)
python process.py input/素材.mp4 --auto

# 2) 只转写, 把 transcript 交给 WorkBuddy 写爆款解说词, 然后精修出片
python process.py input/素材.mp4                 # 只转写, 停下来等你给解说词
python process.py input/素材.mp4 --edit-only work/素材.script.json   # 用精修稿出片

# 可选参数
--voice zh-CN-YunxiNeural    指定 AI 旁白音色(男声沉稳/女声温柔)
--vertical                   输出竖屏 9:16(抖音/视频号)
--original-speed             画面原速(不快进): 窗口跟随旁白时长, 不拉伸画面
--own-voice                  用 work/<i>.mp3(你自己录的)替换 AI 旁白
--moviepy                    回退到 moviepy 渲染(更慢, 仅兼容旧路径); 默认已用 ffmpeg 原生渲染(快约16倍)
----------------------------------------------------------------

依赖(managed python venv):
/Users/suixindelang/.workbuddy/binaries/python/envs/default/bin/python
"""
import os
import sys
import json
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "scripts"))
from config import INPUT, OUTPUT, WORK
import transcribe
import edit


def auto_script(transcript_path, script_path, voice=None, title=""):
    """把转写稿自动整理成口播化的解说词草稿(草稿! 质量不如 WorkBuddy 精修)。

    策略: 合并过短/过近的碎段, 直接以转写文本当旁白, 保留原时间轴。
    """
    segs = json.load(open(transcript_path, encoding="utf-8"))
    merged = []
    for s in segs:
        t = s.get("text", "").strip()
        if not t:
            continue
        if merged and (s["start"] - merged[-1]["end"]) < 0.6 and (s["start"] - merged[-1]["start"]) < 8:
            merged[-1]["end"] = s["end"]
            merged[-1]["text"] = merged[-1]["text"].rstrip("，。") + "，" + t
        else:
            merged.append({"start": s["start"], "end": s["end"], "text": t})

    out = {
        "title": title or os.path.splitext(os.path.basename(transcript_path))[0],
        "segments": [
            {
                "start": m["start"],
                "end": m["end"],
                "narration": m["text"],
                "note": "自动草稿(转写稿口播化)，建议交给 WorkBuddy 精修钩子/节奏",
            }
            for m in merged
        ],
    }
    if voice:
        out["voice"] = voice
    json.dump(out, open(script_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"  自动解说词草稿 -> {script_path} ({len(out['segments'])} 段)")
    return script_path


def main():
    ap = argparse.ArgumentParser(description="解说视频一键工作台")
    ap.add_argument("video", help="视频路径(相对/绝对)")
    ap.add_argument("--auto", action="store_true",
                    help="全自动: 转写 + 自动解说词草稿 + 出片")
    ap.add_argument("--edit-only", metavar="SCRIPT",
                    help="跳过转写, 直接用指定 script.json 剪辑出片")
    ap.add_argument("--voice", default=None, help="AI 旁白音色, 如 zh-CN-YunxiNeural")
    ap.add_argument("--vertical", action="store_true", help="输出竖屏 9:16")
    ap.add_argument("--original-speed", action="store_true",
                    help="画面原速(不快进): 窗口跟随旁白时长, 不拉伸画面")
    ap.add_argument("--own-voice", action="store_true",
                    help="用 work/<i>.mp3(自己录的)替换 AI 旁白")
    ap.add_argument("--moviepy", action="store_true",
                    help="回退到 moviepy 渲染(更慢, 仅兼容旧路径); 默认已用 ffmpeg 原生渲染(快约16倍)")
    ap.add_argument("--output", default=None,
                    help="自定义成片输出路径(默认按命名规则生成)")
    args = ap.parse_args()

    video_path = args.video
    if not os.path.isabs(video_path):
        video_path = os.path.join(HERE, video_path)
    if not os.path.exists(video_path):
        print(f"[错误] 找不到视频: {video_path}")
        sys.exit(1)

    name = os.path.splitext(os.path.basename(video_path))[0]
    transcript_path = os.path.join(WORK, name + ".transcript.json")
    audio_path = os.path.join(WORK, name + ".wav")

    # ---- 步骤1: 转写 ----
    if not args.edit_only:
        print("=== [1/2] 转写 ===")
        transcribe.extract_audio(video_path, audio_path)
        segs, info = transcribe.transcribe(audio_path)
        json.dump(segs, open(transcript_path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"  转写完成: {len(segs)} 段 -> {transcript_path}")

    # ---- 步骤2: 解说词 ----
    if args.edit_only:
        script_path = args.edit_only
        if not os.path.exists(script_path):
            print(f"[错误] 找不到解说词: {script_path}")
            sys.exit(1)
        print(f"=== 使用指定解说词: {script_path} ===")
    elif args.auto:
        print("=== [2/2] 自动解说词草稿 ===")
        script_path = os.path.join(WORK, name + ".script.json")
        auto_script(transcript_path, script_path, voice=args.voice)
    else:
        print("\n✅ 转写完成。下一步二选一:")
        print(f"   A) 全自动草稿:  python process.py {args.video} --auto"
              + (" --vertical" if args.vertical else "")
              + (f" --voice {args.voice}" if args.voice else ""))
        print(f"   B) 精修解说词:  把 {transcript_path} 交给 WorkBuddy 写解说词,")
        print(f"                   保存为 work/{name}.script.json 后运行:")
        print(f"                   python process.py {args.video} --edit-only work/{name}.script.json")
        print("   (推荐 B: 机器草稿只是转写稿, 没有钩子/节奏; WorkBuddy 写的才有爆款感)")
        return

    # ---- 步骤3: 剪辑出片 ----
    print("=== 剪辑成片 ===")
    use_ffmpeg = not args.moviepy  # 默认 ffmpeg 原生渲染(快约16倍); --moviepy 回退
    if args.vertical:
        suffix = "_竖屏成片.mp4"
    elif args.original_speed:
        suffix = "_原速成片.mp4"
    else:
        suffix = "_成片.mp4"
    out_path = args.output or os.path.join(OUTPUT, name + suffix)
    if use_ffmpeg:
        import edit_ffmpeg
        edit_ffmpeg.build(video_path, script_path, out_path,
                          vertical=args.vertical, own_voice=args.own_voice,
                          voice_override=args.voice, original_speed=args.original_speed)
    else:
        edit.build(video_path, script_path, out_path,
                   vertical=args.vertical, own_voice=args.own_voice,
                   voice_override=args.voice, original_speed=args.original_speed)
    print(f"\n🎬 全部完成! 成片在: {out_path}")


if __name__ == "__main__":
    main()
