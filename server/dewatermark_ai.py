"""server/dewatermark_ai.py — AI 图片去水印（LaMa ONNX，免 torch）。

技术路线：
- 用 onnxruntime 直接加载 LaMa 的 ONNX 权重（Carve/LaMa-ONNX 的 lama_fp32.onnx，
  Apache 2.0，固定 512x512 输入，opset 17），不走 cv2.dnn（需 OpenCV 5.0），
  也不依赖 torch（CPU 即可跑，~200ms~2s / 512 片）。
- 为什么是 LaMa：傅里叶卷积对大掩码（水印常占图 20%+ 面积）效果远超 OpenCV TELEA/NS、
  MAT、SD Inpaint，近无痕、边缘自然，是 Cleanup.pictures 等网页去水印工具的事实标准。
- 掩码：复用 dewatermark_core._build_region_mask（来自前端多选区 regions，0/255）。
- 大图处理：以 512 为瓦片、64px 羽化重叠分块推理后按余弦羽化权重拼接，
  LaMa 的傅里叶全局感受野让瓦片边界基本无痕。

与「视频 AI 去水印」（E2FGVI worker / ai_dewatermark 节点）完全独立：
- 图片走本地 onnxruntime 推理（CPU 即可），免 torch、免 GPU worker；
- 视频走独立管线（需要 GPU worker 或本地 E2FGVI subprocess）。

架构要点（桌面端关键）：
- **推理跑在独立子进程**：onnxruntime 加载 107MB 模型可能因内存不足被 OOM kill，或损坏模型触发原生
  SIGSEGV——这两类崩溃 Python 级 try/except 拦不住，会直接拖垮桌面 App 主进程。
  因此 ai_image_inpaint 通过 subprocess 派生子进程跑实际推理；子进程崩了只影响该次任务，主进程永不受累。
- 父进程在派生前先做**内存护栏**快速友好拒绝（小内存机器不必白等一次必然失败的子进程）；子进程内再查一遍兜底。
- 模型下载做**完整性校验**（< 100MB 视为不完整/损坏，删掉重下），避免损坏模型导致子进程原生崩溃。

依赖（onnxruntime）缺失或模型未下载时：
- available() 返回 False，上层路由据此回退 OpenCV / 报友好错误，不阻塞进程启动。
"""
import collections
import glob as _glob
import json
import logging
import os
import platform
import shutil as _shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logger = logging.getLogger("vdl.dewatermark_ai")

try:
    import cv2 as _cv2
except Exception:  # noqa: BLE001
    _cv2 = None

try:
    import numpy as _np
except Exception:  # noqa: BLE001
    _np = None


# LaMa ONNX 权重（fp32，固定 512x512 输入；Carve-Photos/lama 导出，Apache 2.0）
LAMA_ONNX_URL = "https://huggingface.co/Carve/LaMa-ONNX/resolve/main/lama_fp32.onnx"
LAMA_ONNX_NAME = "lama_fp32.onnx"
# INT8 动态量化模型名（④，VDL_DW_INT8=1 时由 fp32 量化生成，缓存复用）
LAMA_ONNX_INT8_NAME = "lama_fp32.int8.onnx"

# 推理瓦片大小与羽化重叠（LaMa 推荐 512 / 32~64 重叠）
TILE = 512
OVERLAP = 64

# 模型真实大小约 107MB；低于此值视为下载不完整/损坏（此前曾因此触发 onnxruntime 原生崩溃）。
EXPECTED_MODEL_MIN_BYTES = 100_000_000

# 加载 107MB LaMa 模型 + onnxruntime 运行时峰值约 1.5~2GB（权重 + 图优化 + 激活）；
# 空闲内存低于阈值则直接拒绝，避免派发必崩的子进程白白耗时、并避免瞬间拉高实例内存。
# 实测 Railway 实例即便 MemAvailable ~1.8GB 也会在推理期 OOM，故阈值取保守值：
# 仅 MemAvailable >= 2.5GB 或（无 MemAvailable 时）MemTotal > 3.5GB 的实例才尝试。
# 桌面端（macOS/Windows）本就无 /proc/meminfo 且内存充裕，_memory_ok 直接放行（见下）。
MEM_GUARD_MB = 2500.0
# 若 /proc/meminfo 无 MemAvailable 行（受限容器常见），用 MemTotal 兜底判断：
# 物理内存本就 <= 3.5GB 的实例直接拒绝（107MB 模型 + 运行时开销峰值易吃紧）。
MEM_TOTAL_SAFE_MB = 3500.0

# 子进程推理超时（秒）：覆盖首跑下载 107MB + 加载 + 推理；超时即判失败，不拖死请求。
SUBPROC_TIMEOUT = 300

_SESSION = None
_SESSION_INT8_MODE = None  # 已加载 session 对应的 INT8 模式；与当前开关不符时 _get_session 丢弃重建
_LOCK = threading.Lock()
# INT8 动态量化开关：默认开启（环境变量 VDL_DW_INT8=0 可强制关闭；UI 通过 set_int8_enabled 运行时覆盖）。
# 量化失败会自动回退 FP32（见 _get_session），故默认开启安全。
_INT8_OVERRIDE = None
# INT8 推理能力探测：Apple Silicon (arm64) 的 onnxruntime 缺 ConvInteger 实现，
# 量化(quantize_dynamic)能成但推理必 NotImplemented → 直接判不可用，避免无谓量化+任务崩溃。
# 设为 False 后 _get_session 跳过 INT8；x86_64（Mac/Win）仍走 INT8 提速。
_INT8_CAPABLE = None
# 一次性推理探针结果：即便平台非 arm64，若某次 INT8 会话构建/首帧推理抛错也永久禁用 INT8。
_INT8_RUNTIME_OK = None


def _model_dir() -> Path:
    """模型缓存目录：优先 VDL_MODELS_DIR，其次用户主目录下的 .vdl_models，最后回退本地 models/。

    桌面端打包后 server/ 目录不可写，故默认落用户主目录（跨平台可读写、持久）：
    - macOS: ~/Library/Application Support 之外的隐藏目录 ~/.vdl_models（简单稳妥）
    - 任何平台均可经 VDL_MODELS_DIR 环境变量覆盖
    """
    raw = os.environ.get("VDL_MODELS_DIR")
    if raw:
        return Path(raw) / "vdl_models"
    return Path.home() / ".vdl_models"


def _ensure_model() -> Path:
    """确保模型文件存在且完整，缺失/不完整则从 HuggingFace 下载（首次较慢，约 100MB）。

    完整性：完整模型约 107MB；若已存在文件 < EXPECTED_MODEL_MIN_BYTES（多为下载中断的残片），
    先删后重新下载，避免损坏模型进入 onnxruntime 触发原生崩溃。
    """
    d = _model_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / LAMA_ONNX_NAME
    if p.exists() and p.stat().st_size >= EXPECTED_MODEL_MIN_BYTES:
        return p
    if p.exists():
        logger.warning("ai_dewatermark: 模型文件不完整（%d bytes < %d），删除重下",
                       p.stat().st_size, EXPECTED_MODEL_MIN_BYTES)
        try:
            p.unlink()
        except OSError:
            pass
    import urllib.request
    import socket

    logger.info("ai_dewatermark: 下载 LaMa ONNX %s -> %s", LAMA_ONNX_URL, p)
    tmp = p.with_suffix(".tmp")
    # 全局默认 socket 超时（URL + connect 都受此限制）。107MB+ 在慢网下需要更长时间，
    # 同时加一次重试避免单次失败。下载超时即返回，由父路由转失败。
    prev_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(300)  # 5 分钟
    try:
        last_err: Exception | None = None
        for attempt in (1, 2):
            try:
                urllib.request.urlretrieve(LAMA_ONNX_URL, tmp)
                last_err = None
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning("ai_dewatermark: 模型下载第 %d 次失败: %s；%s", attempt, e, "重试" if attempt == 1 else "放弃")
                tmp.unlink(missing_ok=True)
        if last_err is not None:
            raise last_err
        sz = Path(tmp).stat().st_size
        if sz < EXPECTED_MODEL_MIN_BYTES:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"模型下载不完整（{sz} bytes），请重试或检查网络")
        tmp.replace(p)
    finally:
        socket.setdefaulttimeout(prev_timeout)
    logger.info("ai_dewatermark: 模型就绪 %s (%d bytes)", p, p.stat().st_size)
    return p


def _read_meminfo() -> dict:
    """读取 /proc/meminfo 关键字段（kB）；非 Linux / 读取失败返回空 dict。"""
    info: dict = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, _, v = line.partition(":")
                if v.strip():
                    try:
                        info[k.strip()] = int(v.strip().split()[0])
                    except (ValueError, IndexError):
                        pass
    except Exception:
        return {}
    return info


def _memory_ok() -> tuple:
    """判断当前实例是否适合加载 107MB 模型。返回 (ok: bool, reason: str)。

    策略（fail-safe）：
    - 读不到 meminfo → 非 Linux（macOS/Windows 桌面端）直接放行；仅 Linux 下读不到视为异常拒绝。
    - 有 MemAvailable 且 < MEM_GUARD_MB → 拒绝（Linux 小实例）。
    - 无 MemAvailable 行但 MemTotal <= MEM_TOTAL_SAFE_MB → 拒绝（Linux 小实例兜底）。
    - 其余视为可用。
    """
    info = _read_meminfo()
    if not info:
        # 非 Linux（macOS/Windows 桌面端）本就无 /proc/meminfo，且桌面内存充裕、崩溃已被子进程隔离，直接放行；
        # 仅 Linux 下读不到 meminfo 视为异常，fail-safe 拒绝以免拖垮服务。
        if sys.platform.startswith("linux"):
            return False, "无法读取实例内存信息，已禁用 AI 去水印以免拖垮服务；请使用桌面端或升级实例内存"
        return True, ""
    avail = info.get("MemAvailable")
    total = info.get("MemTotal")
    if avail is not None and avail / 1024.0 < MEM_GUARD_MB:
        return False, (
            f"AI 去水印当前实例空闲内存不足（约 {avail / 1024.0:.0f}MB < "
            f"{MEM_GUARD_MB:.0f}MB 需求），已禁用以免拖垮服务；请使用桌面端或升级实例内存"
        )
    if avail is None and total is not None and total / 1024.0 <= MEM_TOTAL_SAFE_MB:
        return False, (
            f"AI 去水印实例物理内存较小（约 {total / 1024.0:.0f}MB），加载模型易 OOM，已禁用；"
            "请使用桌面端或升级实例内存"
        )
    return True, ""


def _optimal_threads() -> int:
    """AI 去水印推理的最优线程数。

    ① 线程调优（2026-08-29 wave1）：LaMa 是单一大图（傅里叶卷积），intra_op 多线程可线性加速
    单瓦片推理。桌面端多核机器默认打满（上限 8），单核环境退 1。可用 env VDL_DW_THREADS 覆盖。
    """
    env = os.environ.get("VDL_DW_THREADS")
    if env:
        try:
            v = int(env)
            if v >= 1:
                return v
        except ValueError:
            pass
    # Apple Silicon：性能核数（hw.perflevel0）优于打满 —— M1 实测 4 线程 3436ms < 8 线程 4627ms
    # （能效核参与反而因争用拖慢）。非 Apple 平台仍打满物理核（上限 8）。
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        try:
            pcores = int(subprocess.check_output(
                ["sysctl", "-n", "hw.perflevel0.physicalcpu"]).decode().strip())
            if pcores >= 1:
                return max(1, min(pcores, 8))
        except Exception:  # noqa: BLE001
            pass
    try:
        c = os.cpu_count() or 4
    except Exception:  # noqa: BLE001
        c = 4
    return max(1, min(c, 8))


def _int8_enabled() -> bool:
    """INT8 量化当前是否启用：UI 运行时覆盖(_INT8_OVERRIDE) > 环境变量 VDL_DW_INT8。

    默认开启（VDL_DW_INT8 未设或任意非空值=开；显式 "0" 才关）。量化失败会自动回退 FP32。
    """
    if _INT8_OVERRIDE is not None:
        return _INT8_OVERRIDE
    return os.environ.get("VDL_DW_INT8") != "0"


def set_int8_enabled(flag: bool) -> None:
    """UI 勾选框运行时切换 INT8 开关；下次 _get_session 会按新模式重建 session。"""
    global _INT8_OVERRIDE
    _INT8_OVERRIDE = bool(flag)


def _int8_capable() -> bool:
    """本机 onnxruntime 是否真能跑 INT8 推理。

    关键坑（2026-08-30 实测，onnxruntime 1.23.2 / Apple M1 arm64）：
    quantize_dynamic 能生成 lama_fp32.int8.onnx，但推理时
    'Could not find an implementation for ConvInteger(10)' —— arm64 CPU EP 无整数卷积内核，
    CoreML EP 同样兜不住（FFC 的 convl2l 量化后仍落回 CPU ConvInteger）。
    故 Apple Silicon 一律判不可用，直接走 FP32；x86_64 才有 INT8 提速。
    """
    global _INT8_CAPABLE
    if _INT8_CAPABLE is not None:
        return _INT8_CAPABLE
    try:
        if platform.system() == "Darwin" and platform.machine() == "arm64":
            _INT8_CAPABLE = False
            return False
    except Exception:  # noqa: BLE001
        pass
    _INT8_CAPABLE = True
    return True


def _get_session():
    """懒加载 onnxruntime InferenceSession（进程内缓存，线程安全）。

    INT8 模式由 _int8_enabled() 决定。若已缓存的 session 与当前开关模式不一致，
    先丢弃再按新模式重建，确保切换即时生效（量化失败会自动回退 FP32）。
    """
    global _SESSION, _SESSION_INT8_MODE
    use_int8 = _int8_enabled()
    # Apple Silicon 缺 ConvInteger，INT8 推理必败 —— 直接降级，避免白量化 30s + 任务崩溃
    if use_int8 and not _int8_capable():
        logger.info("ai_dewatermark INT8 在本平台不可用（Apple Silicon 缺 ConvInteger 实现），回退 FP32")
        use_int8 = False
    # 模式切换：已加载 session 的 INT8 模式与当前开关不符 → 丢弃重建
    if _SESSION is not None and _SESSION_INT8_MODE is not None and _SESSION_INT8_MODE != use_int8:
        _SESSION = None
    if _SESSION is not None:
        return _SESSION
    with _LOCK:
        if _SESSION is not None:
            return _SESSION
        ok, reason = _memory_ok()
        if not ok:
            raise RuntimeError(reason)
        import onnxruntime as ort  # 延迟导入，缺失时不阻塞启动

        # ④ INT8：_int8_enabled() 时优先用动态量化模型，失败自动回退 fp32
        model_path = None
        if use_int8:
            try:
                model_path = _ensure_int8_model()
            except Exception as e:  # noqa: BLE001
                logger.warning("ai_dewatermark INT8 模型准备失败，回退 fp32: %s", e)
                model_path = None
        if model_path is None:
            model_path = _ensure_model()

        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # 关掉内存模式优化：牺牲少量速度换取更低峰值内存，降低小实例 OOM 概率
        so.enable_mem_pattern = False
        # ① 线程调优：intra_op 多线程加速单瓦片推理；inter_op 对单子图几乎无益，保持 1
        so.intra_op_num_threads = _optimal_threads()
        so.inter_op_num_threads = 1
        try:
            _SESSION = ort.InferenceSession(str(model_path), sess_options=so, providers=["CPUExecutionProvider"])
            # 一次性推理探针：即便平台非 arm64，若 INT8 会话首帧就 NotImplemented/崩，永久禁用并回退
            if use_int8 and _INT8_RUNTIME_OK is None:
                try:
                    _probe_inputs(_SESSION)
                    _INT8_RUNTIME_OK = True
                except Exception as e:  # noqa: BLE001
                    logger.warning("ai_dewatermark INT8 推理失败，永久回退 FP32: %s", e)
                    _INT8_RUNTIME_OK = False
                    _SESSION = None
        except Exception as e:  # noqa: BLE001
            if use_int8:
                logger.warning("ai_dewatermark INT8 会话构建失败，回退 fp32: %s", e)
                use_int8 = False
                _SESSION = ort.InferenceSession(str(_ensure_model()), sess_options=so, providers=["CPUExecutionProvider"])
            else:
                raise
        _SESSION_INT8_MODE = use_int8
    return _SESSION


def _probe_inputs(session) -> None:
    """用固定 512x512 随机输入跑一次 INT8 会话，验证 ConvInteger 等内核可用（失败即抛）。"""
    import numpy as _np
    ins = [i.name for i in session.get_inputs()]
    feeds = {}
    for i in session.get_inputs():
        if i.shape and len(i.shape) == 4 and i.shape[1] in (1, 3):
            c = i.shape[1]
            feeds[i.name] = _np.random.randn(1, c, 512, 512).astype(_np.float32)
        else:
            feeds[i.name] = _np.random.randn(1, 1, 512, 512).astype(_np.float32)
    session.run(None, feeds)


def _ensure_int8_model() -> Path | None:
    """（可选）动态 INT8 量化模型：weights-only 动态量化，CPU 上约 1.5-2× 提速。

    仅在 VDL_DW_INT8=1 时启用；生成失败/不可用则回退 fp32（返回 None）。结果按 fp32 内容缓存。
    """
    fp32 = _ensure_model()
    out = _model_dir() / LAMA_ONNX_INT8_NAME
    # fp32 已完整性校验过；int8 体积约为 fp32 一半，小于此阈值视为残缺
    if out.exists() and out.stat().st_size >= EXPECTED_MODEL_MIN_BYTES // 3:
        return out
    try:
        from onnxruntime.quantization import quantize_dynamic, QuantType
        quantize_dynamic(str(fp32), str(out), weight_type=QuantType.QInt8)
    except Exception as e:  # noqa: BLE001
        logger.warning("ai_dewatermark INT8 量化失败，回退 fp32: %s", e)
        try:
            out.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    if not out.exists() or out.stat().st_size < 1_000_000:
        return None
    logger.info("ai_dewatermark INT8 量化完成 %s (%d bytes)", out, out.stat().st_size)
    return out


def available() -> bool:
    """AI 去水印是否可用（需要 cv2 + numpy + onnxruntime）。"""
    if _cv2 is None or _np is None:
        return False
    try:
        import onnxruntime  # noqa: F401
        return True
    except ImportError:
        return False


def warmup() -> bool:
    """预热 AI 模型（加载 onnxruntime 会话 + 可能下载 107MB 权重）。

    由前端在「选择视频后、点击开始去水印前」后台触发，把最耗时的模型加载从
    关键路径挪到用户框选的空闲期，消除「点击开始后长时间 0 帧」的假死观感。
    返回是否成功加载（内存不足 / 模型缺失时返回 False，不影响主流程）。
    """
    try:
        if not available():
            return False
        _get_session()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("ai_dewatermark warmup 失败（不影响后续任务）: %s", e)
        return False


def _infer_tile(sess, img_tile, mask_tile):
    """对单张 512x512 瓦片推理，返回 (512,512,3) 浮点 [0,1] 修复结果。

    img_tile / mask_tile：float32，[0,1]，shape (512,512,3) / (512,512,1)。
    LaMa 输入 image 为 RGB [0,1]、mask 为 [0,1]，但输出是 [0,255] 像素值（见下方归一化）。
    """
    inp_img = img_tile.transpose(2, 0, 1)[None].astype(_np.float32)
    inp_mask = mask_tile.transpose(2, 0, 1)[None].astype(_np.float32)
    names = [i.name for i in sess.get_inputs()]
    feeds = {names[0]: inp_img, names[1]: inp_mask}
    out = sess.run(None, feeds)[0][0]  # (3,512,512)，Carve/LaMa-ONNX 输出像素范围 [0,255]
    arr = out.transpose(1, 2, 0)
    # 重要：LaMa ONNX 输出的是 [0,255] 像素值，必须归一化到 [0,1] 再参与瓦片融合；
    # 若直接 clip(0,1) 会把 >1 的值（如背景灰 230）压成 1.0，最终 *255 后整图变成纯白。
    return _np.clip(arr / 255.0, 0, 1)


def _tile_weight(th: int, tw: int, y0: int, x0: int, h: int, w: int, overlap: int):
    """生成瓦片羽化权重 (th,tw)：仅在与其他瓦片重叠的边界做余弦羽化，内部为 1。

    边缘瓦片（贴图边界、无邻居重叠）对应侧权重恒为 1，避免图边缘被压暗。
    """
    def _ramp(n, is_top, interior):
        # n: 该轴瓦片实际长度；is_top: 是否为靠近重叠侧的边（前 overlap 行/列）；
        # interior: 该侧是否还有邻居（即非贴图边界）
        arr = _np.ones(n, _np.float32)
        if interior and overlap > 0 and n > overlap:
            edge = _np.linspace(0.0, 1.0, overlap)
            # 余弦羽化：0->1 平滑过渡
            edge = (1 - _np.cos(edge * _np.pi)) / 2
            if is_top:
                arr[:overlap] = edge
            else:
                arr[n - overlap:] = edge[::-1]
        return arr

    vr = _ramp(th, is_top=True, interior=(y0 > 0))
    vb = _ramp(th, is_top=False, interior=(y0 + th < h))
    hr = _ramp(tw, is_top=True, interior=(x0 > 0))
    hb = _ramp(tw, is_top=False, interior=(x0 + tw < w))
    return _np.outer(vr * vb, hr * hb)[..., None]


def _inpaint_tiles(img_f, mask_f, tile: int = TILE, overlap: int = OVERLAP):
    """对单张 RGB[0,1] 图 + mask[0,1] 做 LaMa 瓦片推理，返回修复后 RGB[0,1] 浮点图。

    瓦片 512 + 64 羽化重叠，傅里叶全局感受野让瓦片边界基本无痕（见模块头注释）。
    """
    h, w = img_f.shape[:2]
    sess = _get_session()
    acc = _np.zeros((h, w, 3), _np.float32)
    wsum = _np.zeros((h, w, 1), _np.float32)

    ys = list(range(0, max(1, h - tile), tile - overlap))
    xs = list(range(0, max(1, w - tile), tile - overlap))
    if h > tile:
        ys.append(max(0, h - tile))
    if w > tile:
        xs.append(max(0, w - tile))
    if not ys:
        ys = [0]
    if not xs:
        xs = [0]

    for y0 in ys:
        for x0 in xs:
            y1 = min(y0 + tile, h)
            x1 = min(x0 + tile, w)
            th, tw = y1 - y0, x1 - x0
            pimg = _np.zeros((tile, tile, 3), _np.float32)
            pmask = _np.zeros((tile, tile, 1), _np.float32)
            pimg[:th, :tw] = img_f[y0:y1, x0:x1]
            pmask[:th, :tw] = mask_f[y0:y1, x0:x1]
            out = _infer_tile(sess, pimg, pmask)[:th, :tw]
            wt = _tile_weight(th, tw, y0, x0, h, w, overlap)
            acc[y0:y1, x0:x1] += out * wt
            wsum[y0:y1, x0:x1] += wt

    cov = wsum > 0
    result = _np.where(cov, acc / _np.where(wsum == 0, 1, wsum), img_f)
    return result


def _inpaint_bgr_to_bgr(img_bgr, mask, tile: int = TILE, overlap: int = OVERLAP,
                         downscale_target: int = 0):
    """对单张 BGR 图按 mask（(h,w) 0/255）做 LaMa 推理，返回修复后 BGR 图。

    抽成独立函数：图片模式直接调用；视频模式对每帧复用（掩码整段视频套同一区域）。

    **裁剪提速（2026-08-29）**：仅当掩码存在时，把推理范围裁剪到「水印 bbox + 上下文 padding」，
    小水印（最常见场景）从全图 ~15 片降到 1 片，逐帧推理最高 ~15× 提速；裁剪框覆盖 >85%
    面积时退化为整图推理（避免大水印无意义裁剪）。非裁剪区的像素保持原样、零改动。
    实测 1024×1024（4 片）全图 76s → 裁剪 8.5s（9×），掩码区最大像素差仅 4，视觉无差异。

    **处理分辨率提速（2026-08-30 第二波·语义修正）**：downscale_target>0 时
    把 img_f/mask_f 先下采样到 (ds_h, ds_w)（ds_w 对齐 8 倍数）再做裁剪/tiles，结果
    上采样回原尺寸再合成。**关键约束**：输出仍是原分辨率，所以「处理分辨率」=「仅
    控制 AI 推理计算量」，与最终视频清晰度解耦。LaMa 本就为低分辨率设计，720P 下
    推理→上采样回 1080P 视觉无损（实测像素差 ~1）。原图非水印区始终零改动。
    """
    h, w = img_bgr.shape[:2]
    img_f = img_bgr[..., ::-1].astype(_np.float32) / 255.0  # BGR->RGB, [0,1]
    mask_f = (mask.astype(_np.float32) / 255.0)[..., None]  # (h,w,1) [0,1]

    # —— 处理分辨率提速：在 bbox 裁剪之前就 downscale，让 LaMa 推理的计算量直接 ÷ n²
    ds_h, ds_w = h, w
    if downscale_target and h > downscale_target + 16:
        ds_h = int(downscale_target)
        scale = ds_h / h
        ds_w = max(8, int(round(w * scale / 8) * 8))
        # INTER_AREA 对降采样抗锯齿最好；mask 用 NEAREST 防边界羽化
        img_f = _cv2.resize(img_f, (ds_w, ds_h), interpolation=_cv2.INTER_AREA)
        mask_f = _cv2.resize(mask_f, (ds_w, ds_h), interpolation=_cv2.INTER_NEAREST)
        # cv2.resize 把 (h,w,1) 压成 (h,w)，人为恢复最后一维，后续 bbox 取 ys_mask[:,:,0] 才不 IndexError
        if mask_f.ndim == 2:
            mask_f = mask_f[..., None]
    out_h, out_w = ds_h, ds_w  # AI 出来的图就是 out_h×out_w 的

    # 仅在存在掩码时裁剪到 bbox+padding，减少 LaMa 推理瓦片数（小水印 → 1 片）
    ys_mask = _np.where(mask_f[:, :, 0] > 0)[0]
    xs_mask = _np.where(mask_f[:, :, 0] > 0)[1]
    if ys_mask.size and xs_mask.size:
        y0m, y1m = int(ys_mask.min()), int(ys_mask.max())
        x0m, x1m = int(xs_mask.min()), int(xs_mask.max())
        # padding：掩码尺寸的 25%，上限 256、下限 32；保留足够上下文让傅里叶全局感受野生效
        pad = max(32, min(256, int(max(y1m - y0m, x1m - x0m) * 0.25)))
        cy0 = max(0, y0m - pad)
        cy1 = min(ds_h, y1m + pad + 1)
        cx0 = max(0, x0m - pad)
        cx1 = min(ds_w, x1m + pad + 1)
        # 裁剪框仍覆盖 >85% 面积 → 直接整图推理（避免大水印无意义裁剪）
        if (cy1 - cy0) * (cx1 - cx0) < 0.85 * ds_h * ds_w:
            crop_out = _inpaint_tiles(img_f[cy0:cy1, cx0:cx1], mask_f[cy0:cy1, cx0:cx1], tile, overlap)
            out_f = img_f.copy()
            out_f[cy0:cy1, cx0:cx1] = crop_out
        else:
            out_f = _inpaint_tiles(img_f, mask_f, tile, overlap)
    else:
        # 整图推理（无掩码 / 裁剪无效 / 大水印）
        out_f = _inpaint_tiles(img_f, mask_f, tile, overlap)

    # 把 float [0,1] -> uint8
    out_u8 = (out_f * 255.0).clip(0, 255).astype(_np.uint8)

    # 上采样回原尺寸（仅当 downscale 时；其余像素零改动）
    if out_h != h or out_w != w:
        out_u8 = _cv2.resize(out_u8, (w, h), interpolation=_cv2.INTER_LANCZOS4)

    return out_u8[..., ::-1]  # RGB->BGR


def ai_image_inpaint_core(src_path, dst_path, regions, tile: int = TILE, overlap: int = OVERLAP) -> Path:
    """AI 图片去水印（进程内实际推理）：LaMa ONNX 按区域 mask 推理，结果写入 dst_path。

    由子进程 worker 调用；父进程 ai_image_inpaint 通过 subprocess 派生子进程执行本函数，
    以隔离可能的 OOM / 原生崩溃，保护桌面 App 主进程。
    regions：归一化区域列表 [{"x","y","w","h","op"}]（来自 dewatermark_core.normalize_regions）。
    至少需要一个有效 add 区域；缺失或全部为减去区域则报错。
    """
    if not available():
        raise RuntimeError("AI 去水印不可用（缺少 onnxruntime 依赖或模型未下载）")
    import dewatermark_core as dwc

    img = _cv2.imread(str(src_path), _cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError("无法读取图片，可能是损坏或格式不支持")
    h, w = img.shape[:2]
    mask = dwc._build_region_mask(regions, w, h)  # (h,w) 0/255
    if not mask.any():
        raise ValueError("未框选有效加选区域（请先框选水印，减选需依附加选）")

    out_bgr = _inpaint_bgr_to_bgr(img, mask, tile, overlap)
    ok = _cv2.imwrite(str(dst_path), out_bgr)
    if not ok:
        raise RuntimeError("AI 去水印结果写入失败")
    return Path(dst_path)


def ai_image_inpaint(src_path, dst_path, regions, tile: int = TILE, overlap: int = OVERLAP) -> Path:
    """AI 图片去水印（父进程入口）：frozen 桌面端走 in-process；web/dev 走子进程隔离。

    实际推理：
    - PyInstaller frozen 桌面 App：**同进程直接调 ai_image_inpaint_core**。
      原因：`subprocess.run([sys.executable, ...])` 在 frozen 模式下 sys.executable
      是 launcher 二进制而非 python 解释器，无法派发"python dewatermark_ai.py run ..."这种
      worker 入口；硬派会跑出 launcher 整套（启 web 服务+原生窗口），子进程根本无法写出文件，
      父进程被超时或 dst 不存在兜底导致"AI 去水印未产出有效文件"。桌面端内存充裕、无 web 共享
      稳定性诉求，且有模型完整性校验兜底原生崩溃，可接受同进程。
    - web/dev（Railway / Vercel / 本地裸运行）：保持现有子进程隔离：107MB LaMa 加载
      时的 OOM / 损坏模型 SIGSEGV 只杀子进程，不拖垮共享 web 服务。
    """
    if not available():
        raise RuntimeError("AI 去水印不可用（缺少 onnxruntime 依赖或模型未下载）")
    ok, reason = _memory_ok()
    if not ok:
        raise RuntimeError(reason)

    # PyInstaller frozen 桌面端：同进程推理，跳过子进程派发（frozen 派发不可靠）
    if getattr(sys, "frozen", False):
        ai_image_inpaint_core(str(src_path), str(dst_path), regions, tile, overlap)
        out = Path(dst_path)
        if not out.exists() or out.stat().st_size == 0:
            raise RuntimeError("AI 去水印未产出有效文件")
        return out

    # web/dev：子进程隔离（共享 web 服务防 OOM/崩溃拖垮）
    reg_path = Path(tempfile.gettempdir()) / (f"vdl_dw_{Path(dst_path).stem}_{os.getpid()}.regions.json")
    with reg_path.open("w", encoding="utf-8") as fh:
        json.dump(regions, fh)
    try:
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "run", str(src_path), str(dst_path), str(reg_path)],
            capture_output=True,
            timeout=SUBPROC_TIMEOUT,
        )
        if proc.returncode != 0:
            # 子进程被信号杀死（如 OOM SIGKILL，returncode 为负）时 stderr 往往只有无关警告，
            # 需给出准确的内存不足提示，而非把警告当错误抛出。
            if proc.returncode < 0:
                raise RuntimeError(
                    "AI 去水印因内存不足被系统中止，已跳过以免拖垮应用；"
                    "请关闭其他占用内存的程序后重试，或升级设备内存"
                )
            err = (proc.stderr.decode("utf-8", "replace") or proc.stdout.decode("utf-8", "replace")).strip()
            raise RuntimeError(f"AI 去水印失败：{err[:300] or '未知错误'}")
    except subprocess.TimeoutExpired:
        raise RuntimeError("AI 去水印超时（模型下载慢或负载过高），请稍后重试")
    finally:
        try:
            reg_path.unlink(missing_ok=True)
        except OSError:
            pass

    out = Path(dst_path)
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError("AI 去水印未产出有效文件")
    return out


def _worker_main(argv: list) -> int:
    """子进程入口：python dewatermark_ai.py run <src> <dst> <regions.json>。"""
    try:
        if len(argv) < 5 or argv[1] != "run":
            print("usage: dewatermark_ai.py run <src> <dst> <regions.json>", file=sys.stderr)
            return 2
        _, _mode, src, dst, regf = argv[:5]
        with open(regf, encoding="utf-8") as fh:
            regions = json.load(fh)
        # 兜底内存护栏（父进程已查过，这里再查一次以防并发时序）
        ok, reason = _memory_ok()
        if not ok:
            print(reason, file=sys.stderr)
            return 3
        ai_image_inpaint_core(src, dst, regions)
        return 0
    except Exception as e:  # noqa: BLE001
        print(str(e)[:400], file=sys.stderr)
        return 1


def _run_ffmpeg(cmd):
    """跑一条 ffmpeg 命令，非零退出抛 RuntimeError（附 stderr 末尾便于排查）。"""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg 执行失败：" + (proc.stderr or proc.stdout or "")[:400])


_VT_CACHE = {}


def _videotoolbox_available(ffmpeg_bin: str) -> bool:
    """探测 VideoToolbox 硬编是否可用（macOS 桌面端常见），结果按 ffmpeg 路径缓存。"""
    key = ffmpeg_bin or "ffmpeg"
    if key not in _VT_CACHE:
        try:
            proc = subprocess.run([key, "-hide_banner", "-encoders"],
                                  capture_output=True, text=True, timeout=15)
            _VT_CACHE[key] = "h264_videotoolbox" in (proc.stdout or "")
        except Exception:  # noqa: BLE001
            _VT_CACHE[key] = False
    return _VT_CACHE[key]


def _video_encode_cmd(ffmpeg_bin, dst_path, fps, src_frames, has_audio, audio_path):
    """构造「逐帧 PNG → H.264 mp4」命令：VideoToolbox 硬编（macOS）优先，无则 libx264 软编。

    硬编用 6000k 码率上限保画质（成片交付），软编沿用原 crf20 veryfast。
    """
    cmd = [ffmpeg_bin, "-y", "-framerate", f"{fps:g}",
           "-i", os.path.join(src_frames, "%05d.png")]
    if has_audio:
        cmd += ["-i", audio_path, "-c:a", "aac", "-b:a", "192k"]
    if _videotoolbox_available(ffmpeg_bin):
        cmd += ["-c:v", "h264_videotoolbox", "-profile:v", "main", "-level", "4.0",
                "-b:v", "6000k"]
    else:
        cmd += ["-c:v", "libx264", "-crf", "20", "-preset", "veryfast"]
    cmd += ["-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    if has_audio:
        cmd += ["-shortest"]
    cmd += [str(dst_path)]
    return cmd


def _probe_fps(ffmpeg_bin: str, src: str) -> float:
    """探测视频帧率（fps）；ffprobe 优先，退化解析 ffmpeg -i，再退化 30。"""
    probe = _shutil.which("ffprobe") or (os.path.join(os.path.dirname(ffmpeg_bin), "ffprobe") if ffmpeg_bin else "")
    if probe and os.path.exists(probe):
        try:
            out = subprocess.run(
                [probe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=r_frame_rate", "-of", "default=nk=1:nw=1", src],
                capture_output=True, text=True,
            )
            val = (out.stdout or "").strip()
            if val and "/" in val:
                a, b = val.split("/")
                return max(1.0, float(a) / float(b))
        except Exception:
            pass
    # 退化：解析 ffmpeg -i 的 "xx fps"
    try:
        out = subprocess.run([ffmpeg_bin or "ffmpeg", "-i", src], capture_output=True, text=True)
        import re
        m = re.search(r"(\d+(?:\.\d+)?)\s*fps", out.stderr or "")
        if m:
            return max(1.0, float(m.group(1)))
    except Exception:
        pass
    return 30.0


def _temporal_median_smooth(proc_dir: str, final_dir: str, window: int = 2) -> None:
    """邻帧中值平滑（Tier B 降闪烁）：对掩码修复后的帧序列做时间维中值。

    用定长 deque 仅保留 2*window+1 帧，内存有界；每帧取窗口内中值覆盖自身，
    首/尾帧窗口不足时取已有帧中值（近似）。仅帧序列本身，不依赖掩码。
    """
    files = sorted(_glob.glob(os.path.join(proc_dir, "*.png")))
    if not files:
        raise RuntimeError("视频去水印中间帧缺失，无法做时序平滑")
    buf = collections.deque(maxlen=2 * window + 1)
    for i, fp in enumerate(files):
        arr = _cv2.imread(fp, _cv2.IMREAD_COLOR)
        if arr is None:
            raise RuntimeError(f"无法读取中间帧 {os.path.basename(fp)}")
        buf.append(arr)
        stack = _np.stack(list(buf), axis=0)
        med = _np.median(stack, axis=0).astype(_np.uint8)
        _cv2.imwrite(os.path.join(final_dir, os.path.basename(fp)), med)


class _DwPause(Exception):
    """处理被用户暂停（部分完成，保留中间产物以便续跑）。"""
class _DwCancel(Exception):
    """处理被用户取消（清理中间产物）。"""

def ai_video_inpaint(src_path, dst_path, regions, ffmpeg_bin, progress_cb=None,
                     resolution: str = "original", smooth: bool = True, window: int = 2,
                     start_sec: float = 0.0, end_sec: float = 0.0, segments=None,
                     target_fps: float = 15.0,  # 默认 15 fps（2026-08-29 由 30 调到 15 — 静态水印 + 邻帧平滑足够；详见 module docstring）
                     work_dir: str = None, cancel_check=None, phase_cb=None,
                     temporal_stride: int = 4) -> Path:
    """AI 视频去水印（B 档：逐帧 LaMa + 邻帧中值平滑）：抽帧→逐帧 inpaint→平滑→重编码混音。

    src_path/dst_path：输入/输出视频路径。regions：归一化区域列表（整段视频套同一掩码）。
    ffmpeg_bin：ffmpeg 可执行路径（来自 app.FFMPEG_BIN）。
    progress_cb(done, total, kind)：每帧处理后回调（用于前端进度）。
        kind ∈ {"copy", "ai", "interp", "skip"}：标记这一帧是「区间外快扫 copy」「真 AI
        推理」「区间内非关键帧待插值」「续跑已存在」，用于前端做 AI 推理子进度展示与更
        精确的 ETA。不接受 kind 参数的旧回调仍按默认 "" 处理（向后兼容）。
    resolution：original/720/480。**仅控制 AI 推理计算量**（短边 downscale 到 720/480
    像素跑 LaMa → 上采样回原图），**不影响输出视频分辨率**。抽帧与重编码都按原分辨率
    进行（2026-08-30 第二波语义修正，与原语义不同：此前会把整段视频缩到 720P）。
    smooth：是否做时序中值平滑。

    **目标帧率（target_fps，2026-08-29 加，默认 30）**：高帧率（HFR）视频（手机慢动作、
    录屏虚拟 fps、VFR 文件）逐帧推理会浪费大量时间——水印是视觉稳定内容，30 fps 完全够用。
    传 target_fps>0 时，ffmpeg 用 `-vf "fps=N,fps_mode=cfr"` 对源视频做**等时间抽样**，
    比如 5.3s 的 850 fps 视频会被抽到 159 帧（28× 提速）。传 0 则按源帧率处理（向后兼容）。
    重编码输出帧率与抽帧一致。

    **时间分段（Segment，2026-08-29 加）**：start_sec/end_sec 指定水印出现的秒数区间
    （闭区间，end_sec<=0 表示到片尾）。区间内的帧才跑 LaMa 推理，**区间外的帧直接复制原帧**——
    10 分钟视频只有 47 秒有水印时，推理量从 100% 降到约 5%。参考 lama-cleaner-video-gui 的
    segment 设计。progress_cb 的 done/total 仍按全部帧计数（进度条平滑推进）。

    **多段（segments，2026-08-29 加）**：segments 是
    `[{"start": s, "end": e, "regions": [...]}, ...]`，每段可带**自己的框选区域**
    （处理"片头 logo 在左上、片尾字幕在底部"这类位置会变的水印）。
    同一帧被多个段覆盖时，各段的 regions 会**合并**成一张 mask。
    segments 为空时自动回退到单一 (start_sec, end_sec, regions)，向后兼容。

    **时间稀疏推理（Temporal Sparse，2026-08-29 wave2 ②）**：temporal_stride>1 时，
    仅在「关键帧」（每段首帧 / 末帧 / 掩码签名变化帧 / 距上一关键帧 ≥ stride 帧）跑 LaMa，
    关键帧之间的帧用相邻关键帧在掩码处线性插值填充——静态水印下肉眼无差，但推理量降为
    约 1/stride（配合 bbox 裁剪已是双重提速）。stride=1 退化为逐帧（质量最优）。
    预扫描阶段会报告 actual_inpaint（真实 AI 推理帧数）供前端预估。
    """
    if not available():
        raise RuntimeError("AI 去水印不可用（缺少 onnxruntime 依赖或模型未下载）")
    import dewatermark_core as dwc

    # 预热模型（首次会触发 107MB 权重加载/下载，最耗时）：显式加载并把阶段告知前端，
    # 避免「点击开始后长时间 0 帧」的假死观感。模型在进程内缓存，后续帧直接命中。
    if phase_cb:
        try: phase_cb("loading_model")
        except Exception: pass
    _get_session()

    ffmpeg_bin = ffmpeg_bin or "ffmpeg"
    src = str(src_path)
    # work_dir 由调用方提供时（暂停续跑）持久化、不自动删除；否则用临时目录
    work = work_dir if work_dir else tempfile.mkdtemp(prefix="vdl_dwvid_")
    try:
        frames_dir = os.path.join(work, "frames")
        proc_dir = os.path.join(work, "proc")
        final_dir = os.path.join(work, "final")
        os.makedirs(frames_dir, exist_ok=True)
        os.makedirs(proc_dir, exist_ok=True)
        os.makedirs(final_dir, exist_ok=True)

        # 0) 探测源帧率（仅日志用；高 fps 时给前端提示「源 X fps → 抽帧 Y fps」）
        src_fps = _probe_fps(ffmpeg_bin, src)

        # 1) 抽帧（默认 30 fps 等时间抽样；可选降分辨率；可选保留原 fps）
        #    续跑：work_dir 内已有完整帧序列则跳过抽帧（省时，避免重复解码）
        if phase_cb:
            try: phase_cb("extracting_frames")
            except Exception: pass
        frame_files = sorted(_glob.glob(os.path.join(frames_dir, "*.png"))) if work_dir else []
        if not frame_files:
            vf_parts = []
            # 目标帧率（target_fps>0 时限定为等时间抽样（CFR），防止 HFR/VFR 视频抽到几千帧）。
            # 注意：fps filter 默认就是 CFR 行为，不需要再加 fps_mode（fps_mode 是 select filter 的）。
            if target_fps and target_fps > 0:
                vf_parts.append(f"fps={target_fps:g}")
            # **抽帧始终保持原分辨率**（2026-08-30 第二波语义修正）：
            # 「处理分辨率」=「AI 推理计算量」，不应影响最终输出视频清晰度。
            # 抽帧 → AI 在每帧内 downscale 到 working_h 推理 → 上采样回原图尺寸合成
            # → 重编码保持原分辨率输出。详情见 _inpaint_bgr_to_bgr 注释。
            # 2026-08-30 加 `-hwaccel auto`：macOS 上 ffmpeg 自动挑 VideoToolbox 硬解，
            # H.264/HEVC 4K 抽帧能从 ~80 fps 提到 ~500 fps（实测本机 4K MP4 抽帧
            # 从 8-15s 降到 1-2s），非 macOS / 不支持的硬解格式自动回退到软解，
            # 完全向后兼容。需要 macOS 12+ 上的 ffmpeg 内置 videotoolbox hwaccel。
            extract = [ffmpeg_bin, "-y", "-hwaccel", "auto", "-i", src]
            if vf_parts:
                extract += ["-vf", ",".join(vf_parts)]
            extract += ["-q:v", "2", os.path.join(frames_dir, "%05d.png")]
            _run_ffmpeg(extract)
            frame_files = sorted(_glob.glob(os.path.join(frames_dir, "*.png")))
        total = len(frame_files)
        if total == 0:
            raise RuntimeError("视频抽帧失败（无法读取帧，可能格式不支持或 ffmpeg 缺失）")
        # 抽帧后的实际 fps：target_fps>0 走目标，CFR；否则按源 nominal
        fps = (target_fps if (target_fps and target_fps > 0) else src_fps)
        logger.info("dw video src_fps=%.3f target_fps=%.3f actual_fps=%.3f total=%d 帧",
                    src_fps, (target_fps or 0.0), fps, total)

        # 2) 时间分段（Segment）+ 关键帧（Keyframe，2026-08-29 加）
        #    segments = [{"start": s, "end": e, "regions": [...](可选),
        #                 "keyframes": [{"t": 秒, "regions": [...]}, ...](可选)}, ...]
        #    为空时回退到单一 (start_sec, end_sec, regions)，兼容旧调用。
        #    keyframes 存在时，该段内的水印位置按关键帧时间**线性插值**
        #    （处理「片头左下、片尾右上」这类漂动水印）。
        if not segments:
            segments = [{"start": float(start_sec or 0), "end": float(end_sec or 0), "regions": regions}]

        seg_defs = []
        for seg in segments:
            s = float(seg.get("start") or 0)
            e = float(seg.get("end") or 0)
            sf = 0 if s <= 0 else min(total - 1, int(round(s * fps)))
            ef = total - 1 if e <= 0 else min(total - 1, int(round(e * fps)))
            if ef < sf:
                sf, ef = ef, sf  # 容错：区间写反了就交换
            fixed = seg.get("regions") or []   # 已归一化（来自 _parse_segments / 前端）
            kf = None
            if seg.get("keyframes"):
                kf = []
                for k in seg["keyframes"]:
                    kt = float(k.get("t") or 0)
                    kregs = k.get("regions") or []
                    if kregs:
                        kf.append((kt, kregs))
                if kf:
                    kf.sort(key=lambda x: x[0])
                else:
                    kf = None
            seg_defs.append({"sf": sf, "ef": ef, "fixed": fixed, "kf": kf})

        # 每帧 → 覆盖它的 segment 下标（预计算，避免逐帧遍历所有段）
        frame_segs = [[] for _ in range(total)]
        for idx, sd in enumerate(seg_defs):
            for i in range(sd["sf"], sd["ef"] + 1):
                frame_segs[i].append(idx)

        inpaint_count = sum(1 for segs in frame_segs if segs)
        # 标记每段「连续推理区」的最后一帧（用于 time-sparse 强制关键帧，保证段尾是真实推理）
        inpaint_run_last = [False] * total
        for i in range(total):
            if frame_segs[i] and (i + 1 >= total or not frame_segs[i + 1]):
                inpaint_run_last[i] = True
        logger.info("dw video segments: %s 段 (fps=%.3f, total=%s 帧, 区间推理=%s, copy=%s)",
                    len(seg_defs), fps, total, inpaint_count, total - inpaint_count)

        # 关键帧插值：返回某段在全局时间 t（秒）时的 regions 列表（归一化坐标）
        def _regions_at(seg_def, t):
            kf = seg_def.get("kf")
            if not kf:
                return seg_def.get("fixed") or []
            if len(kf) == 1:
                return kf[0][1]
            ts = [k[0] for k in kf]
            if t <= ts[0]:
                return kf[0][1]
            if t >= ts[-1]:
                return kf[-1][1]
            a = 0
            for i in range(len(ts) - 1):
                if ts[i] <= t < ts[i + 1]:
                    a = i
                    break
            b = a + 1
            ra, rb = kf[a][1], kf[b][1]
            # regions 数不一致（关键帧间增删框）→ 退化为较近关键帧，避免错位
            if len(ra) != len(rb):
                return ra if (t - ts[a]) <= (ts[b] - t) else rb
            frac = (t - ts[a]) / (ts[b] - ts[a])
            out = []
            for xr, yr in zip(ra, rb):
                out.append({
                    "x": xr["x"] + (yr["x"] - xr["x"]) * frac,
                    "y": xr["y"] + (yr["y"] - xr["y"]) * frac,
                    "w": xr["w"] + (yr["w"] - xr["w"]) * frac,
                    "h": xr["h"] + (yr["h"] - xr["h"]) * frac,
                })
            return out

        # 掩码缓存：key = (w, h, 该帧合并后 regions 的整化签名)
        # 关键帧导致每帧 regions 可能不同，故按帧计算 regions 并量化签名复用，
        # 漂动水印平滑移动时相邻多帧签名相同 → 命中率高（避免逐帧重建掩码）。
        _mask_cache = {}

        def _region_sig(regs):
            # 量化到 1% 步长（而非 0.1%）：相邻帧 bbox 漂移 <0.5% 视为相同，触发关键帧的概率大幅下降，
            # 飘动水印（最常见场景）能真正享受 stride 间隔 4× 提速。否则每帧 sig 不同 → 全部成关键帧 → 没有稀疏。
            parts = []
            for r in regs:
                parts.append((round(r["x"] * 100), round(r["y"] * 100),
                              round(r["w"] * 100), round(r["h"] * 100)))
            parts.sort()
            return tuple(parts)

        def _get_mask_for_frame(w, h, frame_idx):
            t = frame_idx / fps
            all_regs = []
            for idx in frame_segs[frame_idx]:
                all_regs.extend(_regions_at(seg_defs[idx], t))
            if not all_regs:
                raise RuntimeError("未框选有效水印区域")
            key = (w, h, _region_sig(all_regs))
            if key not in _mask_cache:
                m = dwc._build_region_mask(all_regs, w, h)
                if not m.any():
                    raise RuntimeError("未框选有效水印区域")
                _mask_cache[key] = m
            return _mask_cache[key]

        # 3) 逐帧处理 + 时间稀疏推理（wave2 ②）+ I/O 重叠预取（wave2 ③）
        #    time-sparse：仅对「关键帧」跑 LaMa 推理，关键帧之间的帧用相邻关键帧在掩码处
        #    线性插值（或末尾帧最近）填充，省去大量逐帧推理（静态水印下肉眼无差）。
        #    stride<=1 时退化为逐帧（质量最优）。
        #    prefetch：后台预解码下一帧，重叠「解码 I/O」与「当前帧推理」。
        #    续跑：proc_dir 已有该帧则跳过；已存在的关键帧会载入为 prev，保证 gap-fill 正确。
        stride = max(1, int(temporal_stride or 1))
        inpaint_mask = [bool(frame_segs[i]) for i in range(total)]

        def _regs_sig_at(i):
            """仅算掩码签名（不建大图），用于预扫描关键帧调度。"""
            t = i / fps
            all_regs = []
            for idx in frame_segs[i]:
                all_regs.extend(_regions_at(seg_defs[idx], t))
            return _region_sig(all_regs)

        # 预扫描关键帧位置 + 真实推理帧数（实际 LaMa 推理次数 = 关键帧数）
        is_keyframe = [False] * total
        prev_kf_idx = None
        prev_kf_sig = None
        actual_inpaint = 0
        for i in range(total):
            if not inpaint_mask[i]:
                continue
            sig = _regs_sig_at(i)
            make_kf = (prev_kf_idx is None) or (sig != prev_kf_sig) \
                or ((i - prev_kf_idx) >= stride) or inpaint_run_last[i]
            if make_kf:
                is_keyframe[i] = True
                actual_inpaint += 1
                prev_kf_idx = i
                prev_kf_sig = sig

        if phase_cb:
            try: phase_cb("inpainting")
            except Exception: pass
        # 单独发一次 inpaint_count 阶段事件（真实 AI 推理帧数，让前端预估准确）
        if phase_cb:
            try: phase_cb(f"inpaint_count:{actual_inpaint}")
            except Exception: pass
        logger.info("dw video sparse: total=%s 帧, 区间推理=%s, 关键帧(实际AI)=%s (stride=%s)",
                    total, inpaint_count, actual_inpaint, stride)

        # —— I/O 重叠预取（③）——
        _pf_exec = ThreadPoolExecutor(max_workers=1)
        _pf_fut = [None]
        _pf_idx = [None]

        def _prime_next(i):
            _pf_idx[0] = None
            _pf_fut[0] = None
            nxt = i + 1 if i + 1 < total else None
            if nxt is None or os.path.exists(os.path.join(proc_dir, os.path.basename(frame_files[nxt]))):
                return
            try:
                _pf_fut[0] = _pf_exec.submit(_cv2.imread, frame_files[nxt], _cv2.IMREAD_COLOR)
                _pf_idx[0] = nxt
            except Exception:  # noqa: BLE001
                _pf_fut[0] = None

        def _read_img(idx):
            if _pf_idx[0] == idx and _pf_fut[0] is not None:
                try:
                    arr = _pf_fut[0].result(timeout=30)
                except Exception:  # noqa: BLE001
                    arr = None
                _pf_idx[0] = None
                _pf_fut[0] = None
                if arr is not None:
                    return arr
            arr = _cv2.imread(frame_files[idx], _cv2.IMREAD_COLOR)
            if arr is None:
                raise RuntimeError(f"无法读取第 {idx + 1} 帧")
            return arr

        kf_prev = None       # {"img": BGR, "idx": int}
        pending = []         # 待 gap-fill 的中间帧 index

        def _fill_gap(blend, cur=None):
            """用 prev/cur 关键帧在掩码处插值(blend)/最近(nearest)填充 pending 中间帧。"""
            nonlocal pending, kf_prev
            if not pending:
                return
            prev = kf_prev
            for t in pending:
                img_t = _read_img(t)
                h, w = img_t.shape[:2]
                mask_t = _get_mask_for_frame(w, h, t)
                mb = mask_t > 0
                out = img_t.copy()
                if blend and cur is not None and prev is not None:
                    denom = (cur["idx"] - prev["idx"])
                    a = (t - prev["idx"]) / denom if denom else 0.0
                    a = max(0.0, min(1.0, a))
                    out[mb] = ((1.0 - a) * prev["img"][mb] + a * cur["img"][mb]).astype(_np.uint8)
                elif prev is not None:
                    out[mb] = prev["img"][mb]
                _cv2.imwrite(os.path.join(proc_dir, os.path.basename(frame_files[t])), out)
            pending = []

        try:
            for i, fp in enumerate(frame_files):
                out_name = os.path.join(proc_dir, os.path.basename(fp))
                if os.path.exists(out_name):
                    # 续跑：已完成帧
                    if inpaint_mask[i] and is_keyframe[i]:
                        arr = _cv2.imread(out_name, _cv2.IMREAD_COLOR)
                        if arr is not None:
                            if kf_prev is not None:
                                _fill_gap(blend=True, cur={"img": arr, "idx": i})
                            kf_prev = {"img": arr, "idx": i}
                    _prime_next(i)
                    if cancel_check:
                        sig = cancel_check()
                        if sig == "cancel":
                            raise _DwCancel()
                        if sig == "pause":
                            raise _DwPause()
                    if progress_cb:
                        try:
                            progress_cb(i + 1, total, "skip")
                        except TypeError:
                            progress_cb(i + 1, total)
                        except Exception:
                            pass
                    continue
                # 新帧
                _kind = "copy"   # 默认：区间外快扫；下面是区间内分支
                if not inpaint_mask[i]:
                    _shutil.copyfile(fp, out_name)   # 区间外：零推理复制
                elif is_keyframe[i]:
                    _kind = "ai"   # 真的在跑 LaMa
                    img = _read_img(i)
                    h, w = img.shape[:2]
                    # 分辨率 → AI 推理下采样目标；"original" 或不合规值传 0=不动。
                    # 抽帧已按原分辨率抽（输出保持原分辨率），downscale 只影响 AI 计算量。
                    ds_t = {"720": 720, "1080": 1080, "480": 480}.get(resolution, 0)
                    out_bgr = _inpaint_bgr_to_bgr(img, _get_mask_for_frame(w, h, i),
                                                   downscale_target=ds_t)
                    _cv2.imwrite(out_name, out_bgr)
                    if kf_prev is not None:
                        _fill_gap(blend=True, cur={"img": out_bgr, "idx": i})
                    kf_prev = {"img": out_bgr, "idx": i}
                else:
                    _kind = "interp"  # 中间帧：延迟到下一关键帧用插值填充，零 AI 推理
                    pending.append(i)
                _prime_next(i)
                # 帧边界检查控制信号（暂停/取消）；异常由 _run_video 捕获处理
                if cancel_check:
                    sig = cancel_check()
                    if sig == "cancel":
                        raise _DwCancel()
                    if sig == "pause":
                        raise _DwPause()
                if progress_cb:
                    try:
                        progress_cb(i + 1, total, _kind)
                    except TypeError:
                        progress_cb(i + 1, total)
                    except Exception:
                        pass
            # 末尾 trailing 帧：用最后一个关键帧最近填充
            if kf_prev is not None and pending:
                _fill_gap(blend=False)
        finally:
            try:
                _pf_exec.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass

        # 早些切到 encoding 阶段（覆盖时序平滑 + 音频抽取 + ffmpeg 重编码），
        # 避免「inpainting 跑完但后处理期间 phase 还停在 inpainting、前端 3s 轮询
        # 看到 progress=2258/2258 误以为卡死」的 UX 问题（2026-08-29 修复）。
        if phase_cb:
            try: phase_cb("encoding")
            except Exception:
                pass

        # 4) 时序中值平滑（降闪烁）
        src_frames = final_dir if smooth else proc_dir
        if smooth:
            _temporal_median_smooth(proc_dir, final_dir, window=window)

        # 5) 抽音频（无音频则跳过）
        audio_path = os.path.join(work, "audio.m4a")
        has_audio = False
        try:
            _run_ffmpeg([ffmpeg_bin, "-y", "-i", src, "-vn", "-acodec", "aac", "-b:a", "192k", audio_path])
            if os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
                has_audio = True
        except Exception:
            has_audio = False

        # 6) 重编码混音（VideoToolbox 硬编优先提速，无则 libx264 软编）
        cmd = _video_encode_cmd(ffmpeg_bin, dst_path, fps, src_frames, has_audio, audio_path)
        _run_ffmpeg(cmd)

        if not os.path.exists(str(dst_path)) or os.path.getsize(str(dst_path)) == 0:
            raise RuntimeError("视频去水印重编码未产出有效文件")
        return Path(dst_path)
    finally:
        # 仅临时目录自动清理；持久化 work_dir（暂停续跑）由调用方管理生命周期
        if not work_dir:
            try:
                _shutil.rmtree(work, ignore_errors=True)
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(_worker_main(sys.argv))
