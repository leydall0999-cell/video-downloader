"""解说视频自动剪辑管线 - 全局配置"""
import os

# 项目根目录：默认取本文件上一级(scripts/ 的父目录即管线根)，
# 可用环境变量 COMMENTARY_BASE 覆盖（部署到 Linux 强机时指向实际路径）
_HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("COMMENTARY_BASE") or os.path.dirname(_HERE)

# 工作目录根：桌面版「单二进制双角色」打包时，管线源码/模型在只读的包内资源目录，
# 但 input/output/work 必须落在可写目录（否则虽 import 不崩，落盘会崩，且重启即丢）。
# 用 COMMENTARY_WORK_ROOT 把这三个目录重定向到用户可写位置；不设时回退到
# BASE 下的默认子目录（开发态行为完全不变）。
_WORK_ROOT_ENV = (os.environ.get("COMMENTARY_WORK_ROOT") or "").strip()
_WORK_ROOT = _WORK_ROOT_ENV if _WORK_ROOT_ENV else BASE
INPUT = os.path.join(_WORK_ROOT, "input")      # 放原始视频
OUTPUT = os.path.join(_WORK_ROOT, "output")    # 成片输出
WORK = os.path.join(_WORK_ROOT, "work")        # 中间文件(音轨/srt/转写稿/配音)
SCRIPTS = _HERE


def ensure_dirs():
    """按需创建工作目录。

    重要: 绝不能在模块导入时无条件建目录——桌面版打包后管线源码位于
    只读的应用资源目录内, import config 就会 PermissionError 直接崩。
    改为由真正要落盘的入口(process.main / transcribe.main / edit*.build)显式调用。
    """
    created = []
    for d in (INPUT, OUTPUT, WORK):
        try:
            os.makedirs(d, exist_ok=True)
            created.append(d)
        except OSError as exc:
            print(f"[警告] 无法创建工作目录 {d}: {exc}")
    return created


# ---- 转写(whisper) ----
WHISPER_MODEL = "base"   # base 够用；要更准改 "small" / "medium"
LANGUAGE = "zh"          # 中文；换语种改这里


def _resolve_model_dir():
    """定位本地 ct2(faster-whisper) 模型目录，按优先级回落。

    自包含分发铁律(2026-08-10): 模型必须随包预置，运行时禁止联网下载。
    因此「空串→HuggingFace 下载」仅保留给开发态(非 frozen)，打包版若解析不到
    本地模型会直接报错，绝不静默联网。

    候选顺序:
    1. 环境变量 COMMENTARY_MODEL_DIR —— 桌面版启动器注入的随包模型绝对路径
    2. 随包约定位置: <管线根>/models/whisper-base（已随仓库预置的本地模型）
    3. 历史位置: work/whisper-base（开发期落盘）
    4. 空串 —— 开发态交给 faster-whisper 按模型名从 HuggingFace 下载(需联网)
    """
    explicit = (os.environ.get("COMMENTARY_MODEL_DIR") or "").strip()
    if explicit:
        if os.path.isdir(explicit):
            return explicit
        print(f"[警告] COMMENTARY_MODEL_DIR 指向的目录不存在: {explicit}，改用自动探测")
    for cand in (
        os.path.join(BASE, "models", "whisper-base"),
        os.path.join(os.path.dirname(BASE), "models", "whisper-base"),
        os.path.join(WORK, "whisper-base"),
    ):
        if os.path.isdir(cand):
            return cand
    return ""


WHISPER_MODEL_PATH = _resolve_model_dir()

# ---- AI 旁白(edge-tts 中文音色) ----
# 女声: zh-CN-XiaoxiaoNeural(温柔) / zh-CN-XiaoyiNeural(活泼)
# 男声: zh-CN-YunjianNeural(沉稳) / zh-CN-YunyangNeural(新闻腔)
# 注意: zh-CN-YunxiNeural 对部分口语/方言文本会 NoAudioReceived，已被回退链替换。
VOICE = "zh-CN-XiaoxiaoNeural"

# 原片人声保留比例(解说模式下原声压低, 让旁白突出)。0=完全静音
ORIGINAL_DUCK = 0.15

# ---- 字幕字体(需支持中文) ----
def _find_cjk_font():
    # 0) 随包捆绑字体（自包含铁律优先）：打包后 assets/fonts 与管线同目录，
    #    由构建脚本 --add-data 打进安装包。有则一律优先用——保证跨平台渲染一致，
    #    且不依赖系统字体（系统字体可能缺中文/被替换/路径随版本变动）。
    #    开发态该目录通常仅含 README（无字体文件），会自动回落到下方系统候选。
    _bundled_fonts = os.path.join(BASE, "assets", "fonts")
    if os.path.isdir(_bundled_fonts):
        for _f in sorted(os.listdir(_bundled_fonts)):
            if _f.lower().endswith((".ttf", ".ttc", ".otf", ".otc")):
                return os.path.join(_bundled_fonts, _f)
    windir = os.environ.get("WINDIR", r"C:\Windows")
    candidates = [
        # Windows
        os.path.join(windir, "Fonts", "msyh.ttc"),     # 微软雅黑
        os.path.join(windir, "Fonts", "msyhbd.ttc"),   # 微软雅黑粗体
        os.path.join(windir, "Fonts", "simhei.ttf"),   # 黑体
        os.path.join(windir, "Fonts", "simsun.ttc"),   # 宋体
        os.path.join(windir, "Fonts", "yahei.ttf"),
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
    # 兜底：无中文字体时不返回假路径，交给 ImageFont.load_default() 并在调用处告警
    return ""

FONT = _find_cjk_font()
SUBTITLE_SIZE = 44
