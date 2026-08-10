"""转写模块: 视频 -> 单声道音轨 -> whisper 中文转写 -> transcript.json + .srt

引擎架构（2026-08 起以体积/可分发性为第一优先级）:
  默认唯一引擎: faster-whisper (ctranslate2) —— 纯 CPU int8
    · 不依赖 PyTorch，整条依赖链约 95MB（PyTorch 单独就 640MB+）
    · 桌面分发版随包预置本地模型(models/whisper-base)，安装即用、首次转写不联网；
      模型以运行时 int8 量化加载(见 WHISPER_COMPUTE)，兼具小体积与 int8 速度
    · 速度对中文 base 模型完全够用，且内存占用远低于 torch
  可选备用引擎: openai-whisper (PyTorch) —— 需要自行 pip 安装
    · 只有显式设 WHISPER_ENGINE=openai 或 VDL_WHISPER_TORCH=1 才会尝试
    · 有 N 卡 CUDA / Apple Silicon MPS 时确实更快，适合本机开发或高配自建
    · 打包分发版不内置 torch，请求该引擎时会打印中文提示并自动改走 ctranslate2

模型来源（见 config._resolve_model_dir 的回落）:
  COMMENTARY_MODEL_DIR 环境变量 > 随包 models/whisper-base > work/whisper-base
  ⚠️ 自包含铁律: 打包版(frozen)若解析不到本地模型会直接报错，绝不联网下载。
  仅开发态(非 frozen)才允许回退到按模型名从 HuggingFace 下载。

性能调优:
  - CPU 线程数: WHISPER_CPU_THREADS=4（老笔记本减少核心争抢）
  - 计算精度:   WHISPER_COMPUTE=int8 / int8_float16 / float16
  - 运行设备:   WHISPER_DEVICE=cpu / cuda（ctranslate2 也支持 N 卡）

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
from config import WORK, WHISPER_MODEL, LANGUAGE, WHISPER_MODEL_PATH, ensure_dirs
from ffmpeg_path import ffmpeg_bin


def extract_audio(video_path, audio_path):
    cmd = [ffmpeg_bin(), "-y", "-i", video_path, "-vn",
           "-ac", "1", "-ar", "16000", "-f", "wav", audio_path]
    subprocess.run(cmd, check=True, capture_output=True)


def _fmt_ts(t):
    h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


# ---------------------------------------------------------------------------
# 可选备用引擎: openai-whisper (PyTorch) — MPS/CUDA GPU 加速
# 默认不启用（torch 依赖链 640MB+，桌面分发版已排除）。
# 仅当 WHISPER_ENGINE=openai 或 VDL_WHISPER_TORCH=1 且本机自行装了 torch 时才走。
# ---------------------------------------------------------------------------

def _transcribe_whisper_torch(audio_path, model_size=WHISPER_MODEL):
    """使用 openai-whisper 转写，自动适配 GPU/CPU 最佳后端。

    设备选择顺序（跨平台自适应）:
      Windows + NVIDIA GPU → CUDA (fp16, 5-10x 加速)
      macOS 14+ Apple Silicon → MPS (Metal GPU, 5-10x 加速)
      其他 → PyTorch 优化 CPU (MKL/OpenBLAS, 仍比 ctranslate2 快 3-5x)

    内存/硬盘加速（用户侧）:
      - RAM > 8GB 可安全加载 large 模型；基座 small/medium 更稳妥
      - 模型 + 工作目录尽可能放 SSD（首次加载 + 临时音频 IO 快得多）
    """
    import torch
    import whisper

    # 设备自适应：CUDA > MPS > CPU
    if torch.cuda.is_available():
        device_str = "cuda"
    elif torch.backends.mps.is_available():
        device_str = "mps"
    else:
        device_str = "cpu"
        # CPU 模式下，用环境变量控制线程数（默认所有核，内存小可减）
        cpu_threads = os.environ.get("WHISPER_CPU_THREADS", "").strip()
        if cpu_threads:
            torch.set_num_threads(int(cpu_threads))

    compute_fp16 = device_str != "cpu"

    print(f"[whisper-torch] device={device_str} fp16={compute_fp16}")

    if os.path.isdir(WHISPER_MODEL_PATH):
        raise RuntimeError("openai-whisper 不支持本地目录格式的模型；请用 faster_whisper 引擎或移除 WHISPER_MODEL_PATH")

    # download_root: 放在 SSD 上（默认 ~/.cache/whisper），大模型加载更快
    model = whisper.load_model(model_size, device=device_str, in_memory=True)
    result = model.transcribe(audio_path, language=LANGUAGE, fp16=compute_fp16, verbose=False)

    out = []
    for seg in result.get("segments", []):
        txt = seg.get("text", "").strip()
        if txt:
            out.append({"start": round(seg["start"], 2),
                        "end": round(seg["end"], 2),
                        "text": txt})
    # info 只用于进度输出，openai-whisper 没有 info 对象，用 None 返回
    return out, None


# ---------------------------------------------------------------------------
# 默认引擎: faster-whisper (ctranslate2) — 纯 CPU int8，随包分发
# ---------------------------------------------------------------------------

def _transcribe_whisper_faster(audio_path, model_size=WHISPER_MODEL):
    """使用 faster-whisper/ctranslate2 转写，纯 CPU int8，管线默认引擎。"""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "缺少转写引擎 faster-whisper。请执行: "
            "pip install -r requirements.txt（或 pip install faster-whisper）"
        ) from exc

    if os.path.isdir(WHISPER_MODEL_PATH):
        model_ref = WHISPER_MODEL_PATH
        print(f"[whisper-ct2] 加载本地模型: {model_ref}")
    elif getattr(sys, "frozen", False):
        # 自包含铁律: 打包版绝不联网下载模型，缺模型即明确报错
        raise RuntimeError(
            "未找到随包预置的 Whisper 模型。请确认安装包完整"
            f"（期望目录: {WHISPER_MODEL_PATH} 或环境变量 COMMENTARY_MODEL_DIR 指向有效模型）。"
            "本程序为离线自包含分发版，不会联网下载模型。"
        )
    else:
        model_ref = model_size
        print(f"[whisper-ct2] 加载模型 '{model_size}'(开发态首次会联网下载)")

    device = os.environ.get("WHISPER_DEVICE", "cpu").strip() or "cpu"
    compute = os.environ.get("WHISPER_COMPUTE", "int8").strip() or "int8"
    print(f"[whisper-ct2] device={device} compute_type={compute}")
    model = WhisperModel(model_ref, device=device, compute_type=compute)
    # beam_size=1 足够、比 5 快 2~4x；vad_filter 跳过静音段，进一步加速并减少无效片段
    segments, info = model.transcribe(audio_path, language=LANGUAGE,
                                      beam_size=1, vad_filter=True,
                                      vad_parameters=dict(min_silence_duration_ms=500))

    out = []
    for seg in segments:
        txt = seg.text.strip()
        if txt:
            out.append({"start": round(seg.start, 2),
                        "end": round(seg.end, 2),
                        "text": txt})
    return out, info


# ---------------------------------------------------------------------------
# 主转写入口: 默认 faster-whisper；显式请求时才尝试 torch，失败仍回落
# ---------------------------------------------------------------------------

def _torch_requested():
    engine = os.environ.get("WHISPER_ENGINE", "").strip().lower()
    if engine == "faster":
        return False
    if engine == "openai":
        return True
    return os.environ.get("VDL_WHISPER_TORCH", "").strip().lower() in ("1", "true", "yes")


def transcribe(audio_path, model_size=WHISPER_MODEL):
    if _torch_requested():
        if getattr(sys, "frozen", False):
            print("[whisper] 打包分发版未内置 PyTorch（体积原因），"
                  "已忽略 openai 引擎请求，改用随包的 faster-whisper。")
        else:
            try:
                segs, info = _transcribe_whisper_torch(audio_path, model_size)
                print("[whisper-torch] 转写成功")
                return segs, info
            except Exception as exc:
                print(f"[whisper-torch] openai 引擎不可用（{exc}），"
                      "自动改用默认引擎 faster-whisper ...")

    segs, info = _transcribe_whisper_faster(audio_path, model_size)
    print("[whisper-ct2] 转写成功")
    return segs, info


def main(video_path):
    ensure_dirs()
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

    print("[2/2] 转写中(加载本地模型, 稍等) ...")
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
