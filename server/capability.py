"""server/capability.py — 运行环境能力探测（跨平台，纯标准库）。

用途：图片去水印「智能」引擎在运行时根据每台电脑的硬件给出「合理推荐」——
低内存机器默认走轻量 OpenCV、难例才回落 AI；大内存机器可直接默认 AI 无痕。

所有取值仅用于「推荐」与「可用引擎列表」，不影响任何强制行为；用户随时可手动覆盖。
"""
import logging
import os
import platform
import sys

logger = logging.getLogger("vdl.capability")


def _total_ram_bytes() -> int:
    """跨平台取物理内存字节数；取不到返回 0。"""
    # POSIX（macOS / Linux 均支持 sysconf 物理页）
    try:
        if hasattr(os, "sysconf") and "SC_PHYS_PAGES" in os.sysconf_names \
                and "SC_PAGE_SIZE" in os.sysconf_names:
            pages = os.sysconf("SC_PHYS_PAGES")
            psize = os.sysconf("SC_PAGE_SIZE")
            if pages and psize:
                return int(pages) * int(psize)
    except (ValueError, OSError):
        pass
    # macOS 兜底
    if sys.platform == "darwin":
        try:
            import subprocess
            out = subprocess.check_output(["sysctl", "-n", "hw.memsize"],
                                          stderr=subprocess.DEVNULL).decode().strip()
            if out.isdigit():
                return int(out)
        except Exception:  # noqa: BLE001
            pass
    # Linux 兜底
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    # 单位 kB
                    return int(line.split()[1]) * 1024
    except Exception:  # noqa: BLE001
        pass
    return 0


def _is_apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def probe_capability(lama_available: bool = False, diffusion_supported: bool = False,
                     diffusion_installed: bool = False, diffusion_models: list = None) -> dict:
    """探测运行环境能力，返回推荐引擎与说明文案。

    lama_available: 调用方传入 LaMa 模型是否已就绪（避免本模块反向依赖 dewatermark_ai）。
    diffusion_supported: 调用方传入扩散档硬件是否支持（物理内存 >= 16GB）。
    diffusion_installed: 调用方传入扩散运行库（torch+diffusers）是否已就位。
    diffusion_models: 调用方传入当前内存档次下可选扩散模型名列表（sd15/sdxl）。

    设计（按需下载）：扩散档的运行库（torch/diffusers，约 2GB）不进基础包；仅在硬件支持
    的机器上把该档在 UI 显示为可选，用户首次真正选择时才下载安装。因此：
    - engines.diffusion = 硬件是否支持（决定选项是否可选/置灰）；
    - recommended_engine 仅在「已安装」时才推荐 diffusion，未安装则推荐 auto（避免一打开就
      静默触发 2GB 下载）。这与「选项可选、默认不推荐下载」一致，文案也如实说明首次下载。
    """
    ram = _total_ram_bytes()
    ram_gb = ram / (1024 ** 3)
    asil = _is_apple_silicon()
    if ram_gb >= 32:
        tier = "extreme"
    elif ram_gb >= 16:
        tier = "high"
    elif ram_gb >= 8:
        tier = "mid"
    else:
        tier = "low"

    # 扩散档（SD-Inpainting / SDXL-Inpainting）需物理内存 >=16GB。
    # supported = 硬件支持（UI 可选）；installed = 运行库已就位（真正能跑）。
    diff_models = list(diffusion_models or [])
    supported = bool(diffusion_supported) and (ram_gb >= 16)
    installed = bool(diffusion_installed)

    # 「智能」档对所有机型都安全：默认 OpenCV，仅难例（半透明/浅底）回落 AI（LaMa）。
    # diffusion 推荐仅在「已安装」时给出，避免一打开就触发 2GB 下载（2026-09-12 修正）。
    recommended = "auto"
    if tier == "low":
        rec = ("检测到内存较小（约 %.1f GB）：已默认「智能」模式——普通水印走 OpenCV"
               "（快、省内存），遇到半透明/浅底等难例会自动切 AI（LaMa）修复。"
               "若处理大图时卡顿，可手动切回「OpenCV 经典」。"
               "「AI 增强修复（扩散模型）」需 16GB+ 内存，当前设备暂不支持。") % ram_gb
    elif tier == "mid":
        rec = ("检测到内存约 %.1f GB：已默认「智能」模式——日常水印用 OpenCV，"
               "难例自动切 AI（LaMa）。如需最高无痕效果可手动选「AI 无痕修复（LaMa）」。"
               "「AI 增强修复（扩散模型，质量更高）」需 16GB+ 内存，当前 8GB 暂不支持；"
               "升级内存后可解锁该档（首次使用需下载约 2GB 运行库 + 4~6.5GB 权重）。") % ram_gb
    elif not installed:
        # 硬件支持但运行库未安装：选项可选（用户可主动选），但默认不推荐（避免静默下载 2GB）。
        # 文案如实说明首次使用会下载。
        rec = ("检测到内存充裕（约 %.1f GB%s）：可选择「AI 增强修复（扩散模型）」获得比 LaMa"
               "更高的无痕质量——大区域、复杂背景水印修复更干净。该引擎运行库（torch/diffusers，"
               "约 2GB）未安装，首次选择该档时会自动下载安装（另需 4~6.5GB 模型权重），请保持网络畅通。"
               "默认仍用「智能」/「AI 无痕修复（LaMa）」即可。") % (
            ram_gb, "，Apple 芯片含神经网络引擎" if asil else "")
    elif tier == "high":
        rec = ("检测到内存充裕（约 %.1f GB%s）：推荐用「AI 增强修复（扩散模型）」获得比 LaMa"
               "更高的无痕质量——大区域、复杂背景水印修复更干净。首次使用会自动下载约 4GB"
               "权重（SD 1.5）。也可保持「智能」让程序自动选。") % (
            ram_gb, "，Apple 芯片含神经网络引擎" if asil else "")
        recommended = "diffusion"
    else:  # extreme
        rec = ("检测到内存非常充裕（约 %.1f GB%s）：推荐默认「AI 增强修复（扩散模型）」——"
               "可选 SD 1.5 或更高清的 SDXL（1024²，需下载约 6.5GB 权重），无痕质量最佳。") % (
            ram_gb, "，Apple 芯片含神经网络引擎" if asil else "")
        recommended = "diffusion"
    if not lama_available:
        rec += "（当前 LaMa 权重未下载，首次用 AI 会自动下载约 107MB；只用 OpenCV 也可。）"
    return {
        "ram_bytes": ram,
        "ram_gb": round(ram_gb, 1),
        "tier": tier,
        "apple_silicon": asil,
        "lama_available": bool(lama_available),
        "diffusion_available": supported,
        "diffusion_supported": supported,
        "diffusion_installed": installed,
        "diffusion_models": diff_models,
        "diffusion_min_ram_gb": 16.0,
        "recommended_engine": recommended,
        "recommendation": rec,
        "engines": {
            "opencv": True,
            "ai": bool(lama_available),
            "auto": True,
            "diffusion": supported,
        },
    }


if __name__ == "__main__":
    import json
    print(json.dumps(probe_capability(), ensure_ascii=False, indent=2))
