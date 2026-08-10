"""ffmpeg / ffprobe 可执行文件定位（跨平台 + 随包分发友好）

优先级（从高到低）:
  1. 环境变量 VDL_FFMPEG_BIN / VDL_FFPROBE_BIN —— 桌面版启动器注入的随包绝对路径
  2. 环境变量 FFMPEG_LOCATION 指向的目录（yt-dlp 惯例，兼容已有配置）
  3. shutil.which 在 PATH 上查找（开发机 / Homebrew / apt 安装）
  4. 裸名兜底（交给系统解析，找不到时由 subprocess 抛 FileNotFoundError）

为什么必须走这里:
  桌面版打包后是冻结二进制，用户机器上大概率没有系统级 ffmpeg，
  只有随包携带的 <应用目录>/bin/ffmpeg(.exe)。任何硬编码 "ffmpeg" 的调用
  在打包版上都会直接崩。全管线统一从本模块取路径。
"""
import os
import shutil
import sys

_WIN = sys.platform.startswith("win")
_EXE = ".exe" if _WIN else ""


def _from_location_dir(name):
    """从 FFMPEG_LOCATION 指定的目录里找 name 可执行文件。"""
    loc = (os.environ.get("FFMPEG_LOCATION") or "").strip()
    if not loc:
        return ""
    # FFMPEG_LOCATION 既可能是目录也可能直接是 ffmpeg 可执行文件路径
    if os.path.isfile(loc) and os.path.basename(loc).lower().startswith(name):
        return loc
    cand = os.path.join(loc, name + _EXE)
    if os.path.isfile(cand):
        return cand
    return ""


def _resolve(env_key, name):
    explicit = (os.environ.get(env_key) or "").strip()
    if explicit and os.path.isfile(explicit):
        return explicit
    if explicit:
        # 显式指定但文件不存在：不静默吞掉，继续往下找但保留告警语义
        print(f"[警告] {env_key} 指向的文件不存在: {explicit}，改用自动探测")
    found = _from_location_dir(name)
    if found:
        return found
    found = shutil.which(name + _EXE) or shutil.which(name)
    if found:
        return found
    return name + _EXE if _WIN else name


def ffmpeg_bin():
    """返回可用的 ffmpeg 路径（每次实时解析，便于运行期注入生效）。"""
    return _resolve("VDL_FFMPEG_BIN", "ffmpeg")


def ffprobe_bin():
    """返回可用的 ffprobe 路径。"""
    return _resolve("VDL_FFPROBE_BIN", "ffprobe")


def available():
    """两个可执行文件是否都真实存在（用于自检/诊断）。"""
    fm, fp = ffmpeg_bin(), ffprobe_bin()
    ok_m = os.path.isfile(fm) or bool(shutil.which(fm))
    ok_p = os.path.isfile(fp) or bool(shutil.which(fp))
    return ok_m and ok_p


if __name__ == "__main__":
    print("ffmpeg :", ffmpeg_bin())
    print("ffprobe:", ffprobe_bin())
    print("可用   :", available())
