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


def probe_capability(lama_available: bool = False, diffusion_available: bool = False,
                     diffusion_models: list = None) -> dict:
    """探测运行环境能力，返回推荐引擎与说明文案。

    lama_available: 调用方传入 LaMa 模型是否已就绪（避免本模块反向依赖 dewatermark_ai）。
    diffusion_available: 调用方传入扩散模型（torch+diffusers）是否就绪。
    diffusion_models: 调用方传入当前内存档次下可选扩散模型名列表（sd15/sdxl）。
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

    # 扩散档（SD-Inpainting / SDXL-Inpainting）需 torch+diffusers 且物理内存 >=16GB；
    # 内存够 + torch 就绪才真正可用，否则仅「理论支持」但在 UI 上显示为不可用并提示升级。
    diff_models = list(diffusion_models or [])
    diffusion_ok = (ram_gb >= 16) and bool(diffusion_available)

    # 「智能」档对所有机型都安全：默认 OpenCV，仅难例（半透明/浅底）回落 AI（LaMa）。
    # 注意：推荐文案必须与「能不能真的跑」一致——高内存但扩散运行库（torch/diffusers）
    # 未就绪时，若文案仍写「推荐扩散」而 recommended_engine 却是 auto，用户会看到
    # 「推荐某档但该档是灰的」的矛盾（2026-09-12 修正）。
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
               "升级内存后可解锁该档（首次使用需下载约 4GB 权重）。") % ram_gb
    elif not diffusion_ok:
        # 内存够但运行库没就绪（torch/diffusers 未安装）→ 不推荐，且文案说明真实原因，
        # 避免「文案推荐扩散、下拉里却是灰的」自相矛盾。
        rec = ("检测到内存充裕（约 %.1f GB%s）：已默认「智能」模式——日常水印走 OpenCV，"
               "难例自动切 AI（LaMa）。「AI 增强修复（扩散模型）」在内存上已满足，"
               "但需额外安装增强引擎运行库（torch/diffusers，约 2GB）后才会启用"
               "（首次使用另需下载 4~6.5GB 权重），当前未就绪，故不默认开启。") % (
            ram_gb, "，Apple 芯片含神经网络引擎" if asil else "")
    elif tier == "high":
        rec = ("检测到内存充裕（约 %.1f GB%s）：可用「AI 增强修复（扩散模型）」获得比 LaMa"
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
        "diffusion_available": diffusion_ok,
        "diffusion_models": diff_models,
        "diffusion_min_ram_gb": 16.0,
        "recommended_engine": recommended,
        "recommendation": rec,
        "engines": {
            "opencv": True,
            "ai": bool(lama_available),
            "auto": True,
            "diffusion": diffusion_ok,
        },
    }


if __name__ == "__main__":
    import json
    print(json.dumps(probe_capability(), ensure_ascii=False, indent=2))
