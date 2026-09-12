"""server/dewatermark_diffusion.py — AI 图片去水印（扩散模型 inpainting，torch + diffusers）。

与 dewatermark_ai.py (LaMa ONNX) 的定位区别：
- LaMa 是傅里叶卷积，CPU 即可跑，~200ms~2s/512 片，质量好但有天花板（大区域 / 复杂语义背景）。
- 扩散模型（SD-Inpainting / SDXL-Inpainting）是文本引导潜扩散，质量更高：大区域、语义修复、
  复杂照片背景的无痕度显著优于 LaMa。代价是需要 torch + 数 GB 权重，且内存需求高（16GB+ 统一内存）。

本模块面向「高配机用户」（16GB+ 统一内存）：
- 低配机（<16GB）探测到内存不足 → diffusion_supported() 返回 False，UI 直接置灰该档，不下载、不导入。
- **运行库（torch+diffusers 栈，约 2GB）不进基础安装包**（基础包保持 ~300MB）：仅当高配机用户
  首次真正选择「扩散」引擎时，才按需从 PyPI 下载 wheel 并解压到用户目录 ~/.vdl_models/diffusion_lib/
  （不触碰 .app 包体，不破坏签名），然后加入 sys.path 即用。低配机永远不会下载这 2GB。
- 权重（SD1.5 ~4GB fp16 / SDXL ~6.5GB fp16）同样首次用时按需下载到 ~/.vdl_models/diffusion/，不进包。

协议（分发必须合规，详见项目记忆铁律）：
- SD-Inpainting (SD 1.5): CreativeML OpenRAIL-M —— 允许商用，须随应用附 LICENSE 全文 + 传递
  Attachment A 使用限制（禁违法/有害内容生成）。
- SDXL-Inpainting: CreativeML OpenRAIL++-M —— 同上，更严格。
- MAT（CC BY-NC 4.0 非商用）已排除，不可分发。
- BrushNet（Apache-2.0）需 base SD 模型 + 专用 pipeline，权重更大，留后续。
- 应用内「AI 模型使用条款」入口必须展示 OpenRAIL-M 要点（见 web/ 去水印面板 / 设置）。

与 LaMa 相同的架构约定：
- 推理在后台线程（app.executor）跑，不卡 UI；frozen 桌面端同进程（与 LaMa 一致，因派发不可靠）。
- 内存护栏：<16GB 直接拒绝（避免 torch 加载 OOM 拖垮 app）。
- 输入：原图 + 用户框选 mask（白=去水印区）；输出：扩散修复后图。
- prompt 策略：去水印场景不需要生成新物体，用中性修复 prompt（延续背景纹理）
  + negative（watermark/text/logo/artifact）。strength=1.0 全替换掩码区。
"""
import hashlib
import logging
import os
import platform
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import requests as _requests

logger = logging.getLogger("vdl.dewatermark_diffusion")

try:
    import cv2 as _cv2
except Exception:  # noqa: BLE001
    _cv2 = None
try:
    import numpy as _np
except Exception:  # noqa: BLE001
    _np = None


# —— 扩散模型注册表（高配机按需下载，不进主打包）——
# 每项契约：
#   repo         HF repo id（diffusers 原生 inpaint checkpoint）
#   subdir       落 ~/.vdl_models/diffusion/ 的子目录名
#   native_size  原生推理分辨率（SD1.5=512, SDXL=1024）；超出则先 resize 到该边长推理再上采样回原图
#   min_ram_gb   该档可用的最小物理内存（GB）；低于则 available() 返回 False
#   dtype        加载精度（fp16 省内存；MPS 上 float16 有轻微掩码精度损失，已在设备选择处权衡）
# 协议：SD1.5 / SDXL inpainting 均为 CreativeML OpenRAIL-M / OpenRAIL++-M，允许商用（须附 LICENSE）。
MODELS_DIFFUSION = {
    "sd15": {
        "repo": "stable-diffusion-v1-5/stable-diffusion-v1-5-inpainting",
        "subdir": "sd15_inpainting",
        "native_size": 512,
        "min_ram_gb": 16.0,
        "dtype": "fp16",
        "steps": 30,
    },
    "sdxl": {
        "repo": "diffusers/stable-diffusion-xl-1.0-inpainting-0.1",
        "subdir": "sdxl_inpainting",
        "native_size": 1024,
        "min_ram_gb": 32.0,
        "dtype": "fp16",
        "steps": 30,
    },
}
DEFAULT_DIFFUSION_MODEL = "sd15"

# 扩散推理最低内存门槛（单模型 sd15 也需 16GB；低于则整个扩散档不可用）
DIFFUSION_MIN_RAM_GB = 16.0

# 中性修复 prompt（去水印场景：延续背景纹理，不生成新物体）。可用环境变量覆盖做微调。
_DIFFUSION_PROMPT = os.environ.get("VDL_DW_DIFF_PROMPT") or \
    "clean seamless background, photorealistic, high quality, detailed texture, no text"
_DIFFUSION_NEGATIVE = os.environ.get("VDL_DW_DIFF_NEG") or \
    "watermark, text, logo, signature, caption, subtitle, letters, artifact, blurry, lowres, deformed"
_DIFFUSION_STEPS = int(os.environ.get("VDL_DW_DIFF_STEPS") or "0") or 30
_DIFFUSION_SEED = int(os.environ.get("VDL_DW_DIFF_SEED") or "0")


# —— 运行时按需安装（高配机首次用时下载 torch/diffusers 栈到用户目录，不进 .app）——
# 安装目标目录：用户目录，绝不写 .app 包体，故不破坏 macOS 签名；且低配机永不触发下载。
RUNTIME_LIB_DIR = Path.home() / ".vdl_models" / "diffusion_lib"
if RUNTIME_LIB_DIR.is_dir() and str(RUNTIME_LIB_DIR) not in sys.path:
    # 已安装过：每次启动把用户目录提到 sys.path 最前，使 torch/diffusers 可被后续 import 找到
    sys.path.insert(0, str(RUNTIME_LIB_DIR))

# 运行时依赖清单（pinned）。torch+diffusers 栈约 2GB。
# 设计：基础包不含这些；仅高配机首次选「扩散」引擎时下载。
# 注意：刻意不列 numpy —— 避免用户目录的 numpy 覆盖基础包 cv2 所用的 numpy（跨 numpy ABI 风险）；
# torch/diffusers 复用基础包已装的 numpy 即可。
# 可用环境变量 VDL_DIFFUSION_INDEX_URL 指定 PyPI 镜像（如国内网络慢时）。
_RUNTIME_REQUIREMENTS = [
    ("torch", "2.5.1"),
    ("diffusers", "0.32.0"),
    ("transformers", "4.46.0"),
    ("accelerate", "1.1.0"),
    ("safetensors", "0.4.5"),
    ("tokenizers", "0.20.0"),
    ("huggingface_hub", "0.26.2"),
    ("filelock", "3.16.1"),
    ("regex", "2024.11.6"),
    ("tqdm", "4.67.1"),
    ("packaging", "24.2"),
    ("typing_extensions", "4.12.2"),
    ("sympy", "1.13.1"),
    ("networkx", "3.4.2"),
    ("jinja2", "3.1.4"),
    ("fsspec", "2024.10.0"),
]


def _py_tag() -> str:
    return getattr(getattr(sys, "implementation", None), "cache_tag", None) or "cp313"


def _plat_tag() -> str:
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return "macosx_11_0_arm64"
    if sys.platform == "darwin":
        return "macosx_10_9_x86_64"
    return "linux"


def _total_ram_gb() -> float:
    """跨平台取物理内存 GB。"""
    try:
        if hasattr(os, "sysconf") and "SC_PHYS_PAGES" in os.sysconf_names \
                and "SC_PAGE_SIZE" in os.sysconf_names:
            pages = os.sysconf("SC_PHYS_PAGES")
            psize = os.sysconf("SC_PAGE_SIZE")
            if pages and psize:
                return (int(pages) * int(psize)) / (1024 ** 3)
    except (ValueError, OSError):
        pass
    if sys.platform == "darwin":
        try:
            out = subprocess.check_output(["sysctl", "-n", "hw.memsize"],
                                          stderr=subprocess.DEVNULL).decode().strip()
            if out.isdigit():
                return int(out) / (1024 ** 3)
        except Exception:  # noqa: BLE001
            pass
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024 / (1024 ** 3)
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def diffusion_supported() -> bool:
    """硬件是否支持扩散档：物理内存 >= 16GB（且 cv2/numpy 可用）。

    低配机（<16GB）返回 False → UI 置灰该档，且永不触发运行库下载。
    """
    if _cv2 is None or _np is None:
        return False
    return _total_ram_gb() >= DIFFUSION_MIN_RAM_GB


def runtime_installed() -> bool:
    """torch+diffusers 是否已就位（基础包或用户目录 ~/.vdl_models/diffusion_lib）。"""
    try:
        import importlib.util
        if importlib.util.find_spec("torch") is None:
            return False
        if importlib.util.find_spec("diffusers") is None:
            return False
        return True
    except Exception:  # noqa: BLE001
        return False


def available() -> bool:
    """扩散去水印现在是否可用：硬件支持 且 运行库（torch+diffusers）已就位。

    低配机或运行库缺失时返回 False。运行库缺失时请调用 ensure_diffusion_runtime() 按需安装。
    """
    return diffusion_supported() and runtime_installed()


def _device() -> str:
    """Apple Silicon → mps（Metal），否则 cpu。"""
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return "mps"
    return "cpu"


def list_diffusion_models() -> list:
    """返回当前内存档次下可用的扩散模型名列表（供 UI 下拉）。"""
    ram = _total_ram_gb()
    out = []
    for name, spec in MODELS_DIFFUSION.items():
        if ram >= spec["min_ram_gb"]:
            out.append(name)
    return out


def current_diffusion_model() -> str:
    """返回默认扩散模型（sd15，除非内存足够且用户曾切）。"""
    return DEFAULT_DIFFUSION_MODEL


# —— 运行库按需下载 / 安装 ——
def _pick_wheel(urls, py_tag, plat) -> dict:
    """从 PyPI JSON 的 urls 列表挑出最匹配当前解释器/平台的 bdist_wheel。

    评分：纯 python（py3-none-any）最优通用；平台 wheel 命中当前 py tag / 平台 tag 加分；
    其它 OS（manylinux/win/musllinux）重罚。返回选中的 url 字典；无 wheel 返回 None。
    """
    cands = [u for u in urls if u.get("packagetype") == "bdist_wheel"]
    if not cands:
        return None

    def _score(u):
        fn = u.get("filename", "")
        s = 0
        if "py3-none-any" in fn:
            s += 100
        if py_tag in fn:
            s += 60
        if "abi3" in fn:
            s += 40
        if plat in fn:
            s += 50
        if "universal2" in fn:
            s += 30
        if any(k in fn for k in ("manylinux", "win_", "musllinux")):
            s -= 1000
        return s

    cands.sort(key=_score, reverse=True)
    return cands[0]


def _resolve_wheel(name: str, version: str, index_url: str) -> dict:
    """用 PyPI JSON API 解析某包的 wheel 真实下载地址 + sha256。"""
    api = f"{index_url.rstrip('/')}/{name}/{version}/json"
    resp = _requests.get(api, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    urls = data.get("urls", [])
    pick = _pick_wheel(urls, _py_tag(), _plat_tag())
    if not pick:
        pick = next((u for u in urls if u.get("packagetype") == "bdist_wheel"), None)
    if not pick:
        raise RuntimeError(f"未找到 {name}=={version} 的可用 wheel（平台 {_plat_tag()}）")
    sha = (pick.get("digests") or {}).get("sha256") or pick.get("sha256")
    return {
        "name": name,
        "version": version,
        "url": pick["url"],
        "sha256": sha,
        "filename": pick["filename"],
        "size": pick.get("size"),
    }


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _download_wheel(meta: dict, lib: Path, progress_cb) -> Path:
    """下载 wheel（带 sha256 校验 + 进度回调）；已缓存且校验通过则直接复用。"""
    wheels_dir = lib / "wheels"
    wheels_dir.mkdir(parents=True, exist_ok=True)
    dest = wheels_dir / meta["filename"]
    if dest.exists() and meta.get("sha256") and _sha256_of(dest) == meta["sha256"]:
        return dest
    tmp = dest.with_suffix(dest.suffix + ".part")
    with _requests.get(meta["url"], stream=True, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or meta.get("size") or 0)
        done = 0
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                fh.write(chunk)
                done += len(chunk)
                if progress_cb and total:
                    pct = done * 100 // total
                    progress_cb("download", meta["name"], pct,
                                f"下载 {meta['name']} {done // (1024 * 1024)}/{total // (1024 * 1024)}MB")
    if meta.get("sha256") and _sha256_of(tmp) != meta["sha256"]:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"{meta['name']} 校验失败（sha256 不匹配），已取消安装")
    tmp.replace(dest)
    return dest


def _install_wheel(wheel_path: Path, lib: Path, progress_cb) -> None:
    """把 wheel 解压进 lib 目录（最小 wheel 安装器：不跑 pip，适配 frozen 桌面端）。

    仅解包纯 python + 扩展（.so/.dylib），跳过入口脚本（库不需要）。
    扩展文件加可执行位，保证 dyld 能加载。
    """
    name = wheel_path.name
    with zipfile.ZipFile(wheel_path) as z:
        infos = z.infolist()
        total = len(infos)
        for i, info in enumerate(infos):
            if info.filename.endswith("/"):
                (lib / info.filename).mkdir(parents=True, exist_ok=True)
                continue
            if ".data/scripts/" in info.filename:
                continue  # 跳过 console_scripts 入口（库无需命令）
            dest = lib / info.filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(dest, "wb") as out:
                out.write(src.read())
            if info.filename.endswith((".so", ".dylib")):
                os.chmod(dest, 0o755)
            if progress_cb:
                progress_cb("extract", name, (i + 1) * 100 // total, f"解压 {name}")
    marker = lib / ".installed" / f"{name}-done"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("ok")


def ensure_diffusion_runtime(progress_cb=None) -> bool:
    """确保扩散运行库（torch+diffusers 栈）已就位；缺失则按需下载 + 安装到用户目录。

    progress_cb(phase, name, pct, msg)：phase ∈ {resolve, download, extract}。
    返回 True 表示已就绪；非高配机或安装失败抛 RuntimeError（由调用方转成任务失败信息）。
    """
    if runtime_installed():
        return True
    if not diffusion_supported():
        raise RuntimeError("本机内存不足 16GB，无法使用扩散增强引擎（需 16GB+ 统一内存）")
    index_url = os.environ.get("VDL_DIFFUSION_INDEX_URL") or "https://pypi.org/pypi"
    lib = RUNTIME_LIB_DIR
    lib.mkdir(parents=True, exist_ok=True)
    if str(lib) not in sys.path:
        sys.path.insert(0, str(lib))
    for (name, version) in _RUNTIME_REQUIREMENTS:
        marker = lib / ".installed" / f"{name}-{version}-done"
        if marker.exists():
            continue
        if progress_cb:
            progress_cb("resolve", name, 0, f"准备 {name}=={version}")
        meta = _resolve_wheel(name, version, index_url)
        wheel = _download_wheel(meta, lib, progress_cb)
        _install_wheel(wheel, lib, progress_cb)
    if not runtime_installed():
        raise RuntimeError("增强引擎安装后仍无法导入 torch/diffusers，请查看日志")
    return True


_lock = threading.Lock()
_SESSIONS = {}  # model_size -> pipeline 对象（按模型缓存，线程安全）


def _model_dir() -> Path:
    """扩散模型缓存目录：优先 VDL_MODELS_DIR，否则 ~/.vdl_models/diffusion。"""
    raw = os.environ.get("VDL_MODELS_DIR")
    if raw:
        return Path(raw) / "vdl_models" / "diffusion"
    return Path.home() / ".vdl_models" / "diffusion"


def _ensure_model(model_size: str = None):
    """确保扩散模型权重已下载到本地缓存；缺失则从 HF 下载（首次较慢，数 GB）。"""
    name = model_size or DEFAULT_DIFFUSION_MODEL
    spec = MODELS_DIFFUSION.get(name) or MODELS_DIFFUSION[DEFAULT_DIFFUSION_MODEL]
    d = _model_dir() / spec["subdir"]
    # diffusers from_pretrained 需要一个已存在的目录（含 model_index.json 等）；
    # 用「目录存在且含关键文件」判定完整性。
    key_files = ("model_index.json", "unet", "vae")
    if d.exists() and all((d / k).exists() for k in key_files):
        return d
    if d.exists():
        # 目录存在但不完整（下载中断）→ 删除重下，避免 diffusers 加载半残权重崩溃
        import shutil as _sh
        logger.warning("dw diffusion[%s]: 缓存不完整，删除重下 %s", name, d)
        try:
            _sh.rmtree(d)
        except OSError:
            pass
    import torch
    from diffusers import AutoPipelineForInpainting

    logger.info("dw diffusion[%s]: 下载 %s -> %s", name, spec["repo"], d)
    dtype = torch.float16 if spec.get("dtype") == "fp16" else torch.float32
    pipe = AutoPipelineForInpainting.from_pretrained(
        spec["repo"], torch_dtype=dtype, local_files_only=False,
    )
    d.parent.mkdir(parents=True, exist_ok=True)
    pipe.save_pretrained(str(d))
    logger.info("dw diffusion[%s]: 权重就绪 %s", name, d)
    return d


def _get_pipeline(model_size: str = None):
    """懒加载 diffusers pipeline（按模型缓存，线程安全）。"""
    name = model_size or DEFAULT_DIFFUSION_MODEL
    spec = MODELS_DIFFUSION.get(name) or MODELS_DIFFUSION[DEFAULT_DIFFUSION_MODEL]
    cached = _SESSIONS.get(name)
    if cached is not None:
        return cached
    with _lock:
        cached = _SESSIONS.get(name)
        if cached is not None:
            return cached
        if not available():
            raise RuntimeError("扩散去水印不可用（内存不足 <16GB 或缺少 torch/diffusers）")
        import torch
        from diffusers import AutoPipelineForInpainting

        d = _ensure_model(name)
        dtype = torch.float16 if spec.get("dtype") == "fp16" else torch.float32
        pipe = AutoPipelineForInpainting.from_pretrained(str(d), torch_dtype=dtype)
        dev = _device()
        try:
            pipe = pipe.to(dev)
        except Exception as e:  # noqa: BLE001
            logger.warning("dw diffusion[%s]: 设备 %s 加载失败，回退 cpu: %s", name, dev, e)
            pipe = pipe.to("cpu")
            dev = "cpu"
        # 省内存：注意力切片（所有设备安全）；CPU offload 仅 CUDA 有意义（MPS/CPU 已在对应设备）
        try:
            pipe.enable_attention_slicing()
        except Exception:  # noqa: BLE001
            pass
        if dev == "cuda":
            try:
                pipe.enable_model_cpu_offload()
            except Exception:  # noqa: BLE001
                pass
        _SESSIONS[name] = pipe
    return _SESSIONS[name]


def ai_image_inpaint(src_path, dst_path, regions, model_size: str = None) -> Path:
    """扩散模型图片去水印：按区域 mask 跑 SD inpainting，结果写入 dst_path。

    regions：归一化区域列表 [{"x","y","w","h","op"}]（来自 dewatermark_core.normalize_regions）。
    至少需要一个有效 add 区域；缺失或全部为减去区域则报错。
    大图处理：原图边长 > native_size 时，先 resize 到 native_size（保持比例）跑推理，
    结果上采样回原尺寸合成（与 LaMa 的「处理分辨率」思路一致，输出仍是原分辨率）。
    """
    if not available():
        raise RuntimeError("扩散去水印不可用（内存不足 <16GB 或缺少 torch/diffusers）")
    import dewatermark_core as dwc
    import torch
    from PIL import Image

    name = model_size or DEFAULT_DIFFUSION_MODEL
    spec = MODELS_DIFFUSION.get(name) or MODELS_DIFFUSION[DEFAULT_DIFFUSION_MODEL]
    native = spec["native_size"]

    img = _cv2.imread(str(src_path), _cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError("无法读取图片，可能是损坏或格式不支持")
    h, w = img.shape[:2]
    mask = dwc._build_region_mask(regions, w, h)  # (h,w) 0/255
    if not mask.any():
        raise RuntimeError("未框选有效加选区域（请先框选水印，减选需依附加选）")

    # —— 处理分辨率：超 native 则先 downscale 到 native 边长，推理后上采样回原图 ——
    do_ds = max(h, w) > native
    if do_ds:
        scale = native / max(h, w)
        ds_h, ds_w = max(8, int(round(h * scale))), max(8, int(round(w * scale)))
    else:
        ds_h, ds_w = h, w

    # 原图转 PIL RGB；mask 转 L（白=修复区，符合 SD 约定）
    init_image = Image.fromarray(_cv2.cvtColor(img, _cv2.COLOR_BGR2RGB)).convert("RGB")
    if do_ds:
        init_image = init_image.resize((ds_w, ds_h), Image.LANCZOS)
    mask_pil = Image.fromarray(mask.astype("uint8")).convert("L")
    if do_ds:
        mask_pil = mask_pil.resize((ds_w, ds_h), Image.NEAREST)

    pipe = _get_pipeline(name)
    gen = None
    if _DIFFUSION_SEED:
        try:
            gen = torch.Generator(device=_device()).manual_seed(_DIFFUSION_SEED)
        except Exception:  # noqa: BLE001
            gen = torch.Generator().manual_seed(_DIFFUSION_SEED)
    steps = _DIFFUSION_STEPS or spec.get("steps", 30)
    try:
        result = pipe(
            prompt=_DIFFUSION_PROMPT,
            negative_prompt=_DIFFUSION_NEGATIVE,
            image=init_image,
            mask_image=mask_pil,
            num_inference_steps=steps,
            guidance_scale=7.5,
            strength=1.0,  # 全替换掩码区
            generator=gen,
        ).images[0]
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"扩散去水印推理失败：{e}")

    if do_ds:
        result = result.resize((w, h), Image.LANCZOS)
    out_bgr = _cv2.cvtColor(_np.array(result.convert("RGB")), _cv2.COLOR_RGB2BGR)
    ok = _cv2.imwrite(str(dst_path), out_bgr)
    if not ok:
        raise RuntimeError("扩散去水印结果写入失败")
    return Path(dst_path)
