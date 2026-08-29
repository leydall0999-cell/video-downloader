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
import shutil as _shutil
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


def _inpaint_bgr_to_bgr(img_bgr, mask, tile: int = TILE, overlap: int = OVERLAP):
    """对单张 BGR 图按 mask（(h,w) 0/255）做 LaMa 瓦片推理，返回修复后 BGR 图。

    抽成独立函数：图片模式直接调用；视频模式对每帧复用（掩码整段视频套同一区域）。
    """
    h, w = img_bgr.shape[:2]
    img_f = img_bgr[..., ::-1].astype(_np.float32) / 255.0  # BGR->RGB, [0,1]
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
    out_bgr = (result * 255).clip(0, 255).astype(_np.uint8)[..., ::-1]  # RGB->BGR
    return out_bgr


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


def ai_video_inpaint(src_path, dst_path, regions, ffmpeg_bin, progress_cb=None,
                     resolution: str = "original", smooth: bool = True, window: int = 2,
                     start_sec: float = 0.0, end_sec: float = 0.0, segments=None) -> Path:
    """AI 视频去水印（B 档：逐帧 LaMa + 邻帧中值平滑）：抽帧→逐帧 inpaint→平滑→重编码混音。

    src_path/dst_path：输入/输出视频路径。regions：归一化区域列表（整段视频套同一掩码）。
    ffmpeg_bin：ffmpeg 可执行路径（来自 app.FFMPEG_BIN）。
    progress_cb(done, total)：每帧处理后回调（用于前端进度）。
    resolution：original/720/480（降分辨率提速）。smooth：是否做时序中值平滑。

    **时间分段（Segment，2026-08-29 加）**：start_sec/end_sec 指定水印出现的秒数区间
    （闭区间，end_sec<=0 表示到片尾）。区间内的帧才跑 LaMa 推理，**区间外的帧直接复制原帧**——
    10 分钟视频只有 47 秒有水印时，推理量从 100% 降到约 5%。参考 lama-cleaner-video-gui 的
    segment 设计。progress_cb 的 done/total 仍按全部帧计数（进度条平滑推进）。

    **多段（segments，2026-08-29 加）**：segments 是
    `[{"start": s, "end": e, "regions": [...]}, ...]`，每段可带**自己的框选区域**
    （处理"片头 logo 在左上、片尾字幕在底部"这类位置会变的水印）。
    同一帧被多个段覆盖时，各段的 regions 会**合并**成一张 mask。
    segments 为空时自动回退到单一 (start_sec, end_sec, regions)，向后兼容。
    """
    if not available():
        raise RuntimeError("AI 去水印不可用（缺少 onnxruntime 依赖或模型未下载）")
    import dewatermark_core as dwc

    ffmpeg_bin = ffmpeg_bin or "ffmpeg"
    src = str(src_path)
    work = tempfile.mkdtemp(prefix="vdl_dwvid_")
    try:
        frames_dir = os.path.join(work, "frames")
        proc_dir = os.path.join(work, "proc")
        final_dir = os.path.join(work, "final")
        os.makedirs(frames_dir, exist_ok=True)
        os.makedirs(proc_dir, exist_ok=True)
        os.makedirs(final_dir, exist_ok=True)

        # 1) 抽帧（原生 fps；可选降分辨率）
        vf = None
        if resolution == "720":
            vf = "scale=-2:720"
        elif resolution == "480":
            vf = "scale=-2:480"
        extract = [ffmpeg_bin, "-y", "-i", src]
        if vf:
            extract += ["-vf", vf]
        extract += ["-q:v", "2", os.path.join(frames_dir, "%05d.png")]
        _run_ffmpeg(extract)

        frame_files = sorted(_glob.glob(os.path.join(frames_dir, "*.png")))
        total = len(frame_files)
        if total == 0:
            raise RuntimeError("视频抽帧失败（无法读取帧，可能格式不支持或 ffmpeg 缺失）")
        fps = _probe_fps(ffmpeg_bin, src)

        # 2) 时间分段（Segment）：支持多段，每段带自己的 regions
        #    segments = [{"start": s, "end": e, "regions": [...]}, ...]
        #    为空时回退到单一 (start_sec, end_sec, regions)，兼容旧调用
        if not segments:
            segments = [{"start": float(start_sec or 0), "end": float(end_sec or 0), "regions": regions}]

        seg_ranges = []
        for seg in segments:
            s = float(seg.get("start") or 0)
            e = float(seg.get("end") or 0)
            sf = 0 if s <= 0 else min(total - 1, int(round(s * fps)))
            ef = total - 1 if e <= 0 else min(total - 1, int(round(e * fps)))
            if ef < sf:
                sf, ef = ef, sf  # 容错：区间写反了就交换
            seg_ranges.append((sf, ef, seg.get("regions") or []))

        # 每帧 → 覆盖它的 segment 下标（预计算，避免逐帧遍历所有段）
        frame_segs = [[] for _ in range(total)]
        for idx, (sf, ef, _regs) in enumerate(seg_ranges):
            for i in range(sf, ef + 1):
                frame_segs[i].append(idx)

        inpaint_count = sum(1 for segs in frame_segs if segs)
        logger.info("dw video segments: %s 段 (fps=%.3f, total=%s 帧, 推理=%s, copy=%s)",
                    len(seg_ranges), fps, total, inpaint_count, total - inpaint_count)

        # 掩码缓存：key = (w, h, 覆盖该帧的段下标组合)
        # （原实现每帧重建 _build_region_mask，18000 帧会重复 18000 次）
        _mask_cache = {}

        def _get_mask(w, h, seg_indices):
            key = (w, h, tuple(seg_indices))
            if key not in _mask_cache:
                merged = []
                for idx in seg_indices:
                    merged.extend(seg_ranges[idx][2])
                m = dwc._build_region_mask(merged, w, h)
                if not m.any():
                    raise RuntimeError("未框选有效水印区域")
                _mask_cache[key] = m
            return _mask_cache[key]

        # 3) 逐帧处理：被任一段覆盖则跑 LaMa 推理，否则零成本复制
        for i, fp in enumerate(frame_files):
            segs = frame_segs[i]
            if segs:
                img = _cv2.imread(fp, _cv2.IMREAD_COLOR)
                if img is None:
                    raise RuntimeError(f"无法读取第 {i + 1} 帧")
                h, w = img.shape[:2]
                out_bgr = _inpaint_bgr_to_bgr(img, _get_mask(w, h, segs))
                _cv2.imwrite(os.path.join(proc_dir, os.path.basename(fp)), out_bgr)
            else:
                # 无段覆盖：直接复制原帧（不动像素，零推理成本）
                _shutil.copyfile(fp, os.path.join(proc_dir, os.path.basename(fp)))
            if progress_cb:
                try:
                    progress_cb(i + 1, total)
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

        # 6) 重编码混音
        cmd = [ffmpeg_bin, "-y", "-framerate", f"{fps:g}", "-i", os.path.join(src_frames, "%05d.png")]
        if has_audio:
            cmd += ["-i", audio_path, "-c:a", "aac", "-b:a", "192k"]
        cmd += ["-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
        if has_audio:
            cmd += ["-shortest"]
        cmd += [str(dst_path)]
        _run_ffmpeg(cmd)

        if not os.path.exists(str(dst_path)) or os.path.getsize(str(dst_path)) == 0:
            raise RuntimeError("视频去水印重编码未产出有效文件")
        return Path(dst_path)
    finally:
        try:
            _shutil.rmtree(work, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(_worker_main(sys.argv))
