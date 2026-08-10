"""配音试听小工具：用 edge-tts 把一段文本转成指定音色的 mp3。

用法：
    python scripts/voice_preview.py <text> <voice> <output.mp3>

复用在主站 POST /api/commentary/voice-preview 路由（user 点 "试听" 时调用）。
"""
import sys
import asyncio
import edge_tts

# 音色回退链：指定音色失败时，按此顺序尝试其他稳定音色。
FALLBACK_VOICES = [
    "zh-CN-YunjianNeural",
    "zh-CN-YunyangNeural",
    "zh-CN-YunxiaNeural",
    "zh-CN-XiaoxiaoNeural",
]


async def _try_voice(text: str, voice: str, output: str, timeout: float = 20.0) -> bool:
    try:
        await asyncio.wait_for(edge_tts.Communicate(text, voice).save(output), timeout=timeout)
        import os
        return os.path.exists(output) and os.path.getsize(output) > 100
    except Exception:
        return False


async def main():
    if len(sys.argv) < 4:
        print("用法: voice_preview.py <text> <voice> <output.mp3>", file=sys.stderr)
        sys.exit(2)
    text, voice, output = sys.argv[1], sys.argv[2], sys.argv[3]
    if not text.strip():
        print("错误: text 为空", file=sys.stderr)
        sys.exit(2)

    # 优先尝试用户指定的音色（最多 2 次，防瞬抖）
    for attempt in range(2):
        if await _try_voice(text, voice, output):
            print(f"OK voice={voice} attempt={attempt + 1}")
            return

    # 失败后按回退链尝试
    tried = [voice]
    for fallback in FALLBACK_VOICES:
        if fallback == voice:
            continue
        tried.append(fallback)
        if await _try_voice(text, fallback, output):
            print(f"OK voice={fallback} fallback_from={voice}")
            return

    print(f"edge-tts 生成失败: 已尝试 {', '.join(tried)}，均无法合成该文本", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())