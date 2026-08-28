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
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
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
_LOCK = threading.Lock()


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

    logger.info("ai_dewatermark: 下载 LaMa ONNX %s -> %s", LAMA_ONNX_URL, p)
    tmp = p.with_suffix(".tmp")
    try:
        urllib.request.urlretrieve(LAMA_ONNX_URL, tmp)
        sz = Path(tmp).stat().st_size
        if sz < EXPECTED_MODEL_MIN_BYTES:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"模型下载不完整（{sz} bytes），请重试或检查网络")
        tmp.replace(p)
    except Exception as e:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        raise
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


def _get_session():
    """懒加载 onnxruntime InferenceSession（进程内缓存，线程安全）。"""
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    with _LOCK:
        if _SESSION is not None:
            return _SESSION
        ok, reason = _memory_ok()
        if not ok:
            raise RuntimeError(reason)
        import onnxruntime as ort  # 延迟导入，缺失时不阻塞启动

        p = _ensure_model()
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # 关掉内存模式优化：牺牲少量速度换取更低峰值内存，降低小实例 OOM 概率
        so.enable_mem_pattern = False
        # 限线程：避免 CPU 尖峰 / 内存膨胀（单图推理本就快，无需多线程）
        so.intra_op_num_threads = 1
        so.inter_op_num_threads = 1
        _SESSION = ort.InferenceSession(str(p), sess_options=so, providers=["CPUExecutionProvider"])
    return _SESSION


def available() -> bool:
    """AI 去水印是否可用（需要 cv2 + numpy + onnxruntime）。"""
    if _cv2 is None or _np is None:
        return False
    try:
        import onnxruntime  # noqa: F401
        return True
    except ImportError:
        return False


def _infer_tile(sess, img_tile, mask_tile):
    """对单张 512x512 瓦片推理，返回 (512,512,3) 浮点 [0,1] 修复结果。

    img_tile / mask_tile：float32，[0,1]，shape (512,512,3) / (512,512,1)。
    LaMa 输入 image 为 RGB [0,1]、mask 为 [0,1]，输出 [0,1]。
    """
    inp_img = img_tile.transpose(2, 0, 1)[None].astype(_np.float32)
    inp_mask = mask_tile.transpose(2, 0, 1)[None].astype(_np.float32)
    names = [i.name for i in sess.get_inputs()]
    feeds = {names[0]: inp_img, names[1]: inp_mask}
    out = sess.run(None, feeds)[0][0]  # (3,512,512)
    return _np.clip(out.transpose(1, 2, 0), 0, 1)


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

    img_f = img[..., ::-1].astype(_np.float32) / 255.0  # BGR->RGB, [0,1]
    mask_f = (mask.astype(_np.float32) / 255.0)[..., None]  # (h,w,1) [0,1]

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
            # 拼到 512 瓦片
            pimg = _np.zeros((tile, tile, 3), _np.float32)
            pmask = _np.zeros((tile, tile, 1), _np.float32)
            pimg[:th, :tw] = img_f[y0:y1, x0:x1]
            pmask[:th, :tw] = mask_f[y0:y1, x0:x1]
            out = _infer_tile(sess, pimg, pmask)[:th, :tw]
            wt = _tile_weight(th, tw, y0, x0, h, w, overlap)
            acc[y0:y1, x0:x1] += out * wt
            wsum[y0:y1, x0:x1] += wt

    # 加权平均（重叠区融合），无覆盖区保持原图（理论上 mask 区必被覆盖）
    cov = wsum > 0
    result = _np.where(cov, acc / _np.where(wsum == 0, 1, wsum), img_f)
    out_bgr = (result * 255).clip(0, 255).astype(_np.uint8)[..., ::-1]  # RGB->BGR
    ok = _cv2.imwrite(str(dst_path), out_bgr)
    if not ok:
        raise RuntimeError("AI 去水印结果写入失败")
    return Path(dst_path)


def ai_image_inpaint(src_path, dst_path, regions, tile: int = TILE, overlap: int = OVERLAP) -> Path:
    """AI 图片去水印（父进程入口）：内存护栏快速拒绝 + 子进程隔离推理。

    实际推理在独立子进程执行，避免 onnxruntime 加载 107MB 模型时的 OOM / 损坏模型原生崩溃
    拖垮桌面 App 主进程。regions 序列化到临时 JSON 传给子进程。
    """
    if not available():
        raise RuntimeError("AI 去水印不可用（缺少 onnxruntime 依赖或模型未下载）")
    ok, reason = _memory_ok()
    if not ok:
        raise RuntimeError(reason)

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


if __name__ == "__main__":
    sys.exit(_worker_main(sys.argv))
