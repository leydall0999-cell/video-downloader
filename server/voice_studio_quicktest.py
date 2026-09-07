"""独立快速测试：直接调本机 VoiceStudio（无需启动 VDL server）。

用法：
  cd server && python voice_studio_quicktest.py
前置：
  - 本机已运行 VoiceStudio（默认 http://localhost:3900）
  - 已在 VoiceStudio 中切到可商用引擎，并记下其模型名
  - 设置可商用模型名（三选一）：
      环境变量  VDL_VOICESTUDIO_TTS_MODEL=你的模型名
      配置接口  POST /api/voice-studio/config {"tts_model":"..."}
      配置文件  ~/.video-downloader/voice_studio_config.json
    未设置则本脚本会跳过 TTS 合成。

本脚本只做连通性 + 列举 +（可选）合成测试，方便你先验证 VoiceStudio 接得通。
不检查 commercial_ack（那是 VDL 接口的 opt-in 闸门），属开发自测工具。
"""
from __future__ import annotations

from voice_studio_client import VoiceStudioClient, VoiceStudioError
from voice_studio_config import get_voice_studio_config, DEFAULT_BASE_URL


def main() -> None:
    cfg = get_voice_studio_config()
    print(f"[配置] base_url={cfg['base_url']}  enabled={cfg['enabled']}  "
          f"tts_model={cfg['tts_model']!r}  commercial_ack={cfg['commercial_ack']}")

    client = VoiceStudioClient()
    h = client.health()
    print(f"[健康] ok={h['ok']}  {h.get('error', '')}")
    if not h["ok"]:
        print("VoiceStudio 未连通，请确认已运行且地址正确（默认 " + DEFAULT_BASE_URL + "）。退出。")
        return

    try:
        voices = client.list_voices()
        print(f"[声音] 共 {len(voices)} 个，示例前 5：")
        for v in voices[:5]:
            print("   -", v.get("id") or v.get("name") or v)
    except VoiceStudioError as e:
        print(f"[声音] 列举失败：{e.message}")

    if not cfg["tts_model"]:
        print("[TTS] 未配置 tts_model，跳过合成。请先设置可商用模型名后重跑。")
        return
    try:
        audio = client.tts("你好，这是来自 VideoDownloader 的语音合成测试。", speed=1.0)
        out = "/tmp/vdl_vs_test.mp3"
        with open(out, "wb") as f:
            f.write(audio)
        print(f"[TTS] 合成成功，写入 {out}（{len(audio)} 字节）")
    except VoiceStudioError as e:
        print(f"[TTS] 合成失败：{e.message}")


if __name__ == "__main__":
    main()
