"""解说视频自动剪辑管线 - 全局配置"""
import os

# 项目根目录：默认取本文件上一级(scripts/ 的父目录即管线根)，
# 可用环境变量 COMMENTARY_BASE 覆盖（部署到 Linux 强机时指向实际路径）
_HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("COMMENTARY_BASE") or os.path.dirname(_HERE)
INPUT = os.path.join(BASE, "input")      # 放原始视频
OUTPUT = os.path.join(BASE, "output")    # 成片输出
WORK = os.path.join(BASE, "work")        # 中间文件(音轨/srt/转写稿/配音)
SCRIPTS = _HERE

os.makedirs(INPUT, exist_ok=True)
os.makedirs(OUTPUT, exist_ok=True)
os.makedirs(WORK, exist_ok=True)

# ---- 转写(whisper) ----
WHISPER_MODEL = "base"   # base 够用；要更准改 "small" / "medium"
LANGUAGE = "zh"          # 中文；换语种改这里
# 本地模型目录(已通过 modelscope 预置在 work/whisper-base)。
# 留空或目录不存在时, 自动从 HuggingFace 下载(建议国内设镜像, 见 transcribe.py 顶部)。
WHISPER_MODEL_PATH = os.path.join(WORK, "whisper-base")

# ---- AI 旁白(edge-tts 中文音色) ----
# 女声: zh-CN-XiaoxiaoNeural(温柔) / zh-CN-XiaoyiNeural(活泼)
# 男声: zh-CN-YunxiNeural(沉稳) / zh-CN-YunyangNeural(新闻腔)
VOICE = "zh-CN-XiaoxiaoNeural"

# 原片人声保留比例(解说模式下原声压低, 让旁白突出)。0=完全静音
ORIGINAL_DUCK = 0.15

# ---- 字幕字体(需支持中文) ----
def _find_cjk_font():
    candidates = [
        # macOS
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        # Linux (Debian/Ubuntu: fonts-noto-cjk；CentOS/RHEL: google-noto-sans-cjk-ttc-fonts)
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
        "/usr/share/fonts/truetype/arphic/ukai.ttc",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return "Liberation-Sans.ttf"  # 兜底(可能无中文，需装中文字体)

FONT = _find_cjk_font()
SUBTITLE_SIZE = 44
