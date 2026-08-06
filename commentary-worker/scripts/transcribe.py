"""转写模块: 视频 -> 单声道音轨 -> faster-whisper 中文转写 -> transcript.json + .srt

用法:
    python scripts/transcribe.py <视频路径>
    python scripts/transcribe.py input/我的素材.mp4

输出(在 work/ 目录):
    <name>.transcript.json   分段文本+时间戳, 给 WorkBuddy 提炼解说词用
    <name>.srt               字幕文件(可直接预览)
"""
import os
import sys
import json
import subprocess

# 国内首次下载 whisper 模型: 走 hf-mirror 并关闭 xet(否则代理会 401/超时)
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import WORK, WHISPER_MODEL, LANGUAGE, WHISPER_MODEL_PATH


def extract_audio(video_path, audio_path):
    cmd = ["ffmpeg", "-y", "-i", video_path, "-vn",
           "-ac", "1", "-ar", "16000", "-f", "wav", audio_path]
    subprocess.run(cmd, check=True, capture_output=True)


def _fmt_ts(t):
    h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def transcribe(audio_path, model_size=WHISPER_MODEL):
    from faster_whisper import WhisperModel
    # 优先用本地预置模型目录, 否则按名字(触发下载)
    if os.path.isdir(WHISPER_MODEL_PATH):
        model_ref = WHISPER_MODEL_PATH
        print(f"[whisper] 加载本地模型: {model_ref}")
    else:
        model_ref = model_size
        print(f"[whisper] 加载模型 '{model_size}'(首次会下载)")
    model = WhisperModel(model_ref, device="cpu", compute_type="int8")
    segments, info = model.transcribe(audio_path, language=LANGUAGE, beam_size=5)
    out = []
    for seg in segments:
        txt = seg.text.strip()
        if txt:
            out.append({"start": round(seg.start, 2),
                        "end": round(seg.end, 2),
                        "text": txt})
    return out, info


def main(video_path):
    if not os.path.exists(video_path):
        print(f"[错误] 找不到视频: {video_path}")
        sys.exit(1)
    name = os.path.splitext(os.path.basename(video_path))[0]
    audio_path = os.path.join(WORK, name + ".wav")
    transcript_path = os.path.join(WORK, name + ".transcript.json")
    srt_path = os.path.join(WORK, name + ".srt")

    print("[1/2] 抽取音轨 ...")
    extract_audio(video_path, audio_path)
    print(f"      音轨 -> {audio_path}")

    print("[2/2] 转写中(首次会下载模型, 稍等) ...")
    segs, info = transcribe(audio_path)

    with open(transcript_path, "w", encoding="utf-8") as f:
        json.dump(segs, f, ensure_ascii=False, indent=2)
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, s in enumerate(segs, 1):
            f.write(f"{i}\n{_fmt_ts(s['start'])} --> {_fmt_ts(s['end'])}\n{s['text']}\n\n")

    print(f"\n✅ 转写完成: {len(segs)} 段")
    print(f"   transcript -> {transcript_path}")
    print(f"   srt        -> {srt_path}")
    print("\n下一步: 把 transcript.json 交给 WorkBuddy 提炼成 分镜式解说词(script.json), 再跑 edit.py")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python scripts/transcribe.py <视频路径>")
        sys.exit(1)
    main(sys.argv[1])
