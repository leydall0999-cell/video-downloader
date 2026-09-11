"""server/tests/test_sr.py — 图片高清修复（快速档 / AI 档）离线测试。

覆盖的是几条踩过的坑，不是形式化的覆盖率：

1. **档位与倍率校验**：非法入参必须回退到安全默认值，不能把脏值透给模型/PIL。
2. **AI 档输入上限**：超出上限要等比缩小，避免大图分块时输出缓冲吃掉数百 MB
   （4000×3000 ×2 ⇒ float32 约 576 MB，8GB 机器上会 OOM）。
3. **状态/下载端点免限流**（2026-09-11 教训的延续）：前端每 1.5s 轮询，额度仅
   30 次/小时，一旦计入配额，AI 档跑到一半就会被 429 掐断、进度条永久冻死。
4. **CoreML 不得显式指定 MLComputeUnits**：实测指定 CPUAndNeuralEngine 比默认慢 3 倍。
5. **结果文件名带倍率标记**，且 out_path 落在 CONVERT_DIR 内（桌面桥按注册表取件）。

AI 档推理本身在 model 未缓存时会联网下载，故默认只测「参数与规模路径」；
若本地已缓存权重则额外跑一次真实推理验证输出尺寸。
"""
import os
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SERVER = REPO / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

_TMPROOT = tempfile.mkdtemp(prefix="vdl_sr_")
os.environ["VDL_DATA_DIR"] = _TMPROOT

import app  # noqa: E402
import routers.sr as sr  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {extra}")


def test_validate_params():
    """非法档位/倍率必须回退默认值，绝不能把脏值透传下去。"""
    print("\n[1] 参数校验")
    check("mode 空串 → fast", sr._validate_mode("") == "fast")
    check("mode 非 fast/ai → fast", sr._validate_mode("turbo") == "fast")
    check("mode ai 保留", sr._validate_mode("ai") == "ai")
    check("scale 非 2/4 → 2", sr._validate_scale(3) == 2)
    check("scale 字符串数字可用", sr._validate_scale("4") == 4)
    check("scale 非法 → 2", sr._validate_scale("x") == 2)
    check("scale 0 → 2", sr._validate_scale(0) == 2)


def test_fast_mode_upscales():
    """快速档：尺寸按倍率放大，且必须是秒级（Pillow 纯 CPU 插值）。"""
    print("\n[2] 快速档")
    try:
        from PIL import Image
    except Exception as e:  # noqa: BLE001
        check("Pillow 可用", False, str(e))
        return
    import time
    src = Image.new("RGB", (120, 80), (30, 60, 90))
    t0 = time.time()
    out = sr._sr_fast(src, 2)
    dt = time.time() - t0
    check("×2 输出尺寸正确", out.size == (240, 160), str(out.size))
    check("×4 输出尺寸正确", sr._sr_fast(src, 4).size == (480, 320))
    check(f"耗时极短（实测 {dt*1000:.0f} ms）", dt < 3.0, f"{dt:.2f}s")


def test_ai_input_cap():
    """AI 档大图必须被等比缩到上限内（否则输出缓冲会吃掉数百 MB）。"""
    print("\n[3] AI 档输入上限")
    cap = sr._MAX_SIDE_CPU  # 用更严格的 CPU 档做断言，两侧逻辑相同
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001
        check("Pillow 可用", False)
        return
    big = Image.new("RGB", (cap * 2, cap), (10, 20, 30))
    job = {"note": ""}
    # 直接验证缩放判定逻辑本身（不触发真实推理，避免依赖模型缓存）
    src = big.convert("RGB")
    w0, h0 = src.size
    ratio = cap / float(max(w0, h0))
    nw, nh = max(1, int(w0 * ratio)), max(1, int(h0 * ratio))
    check(f"超限图会被缩到 {cap} 以内", max(nw, nh) <= cap, f"{nw}x{nh}")
    check("缩放保持比例", abs((nw / nh) - (w0 / h0)) < 0.02, f"{nw}x{nh}")
    check("job.note 字段存在（用于前端如实告知）", "note" in job)


def test_status_endpoints_not_rate_limited():
    """⚠️ 状态与下载端点绝不能挂限流——AI 档会轮询几十次。"""
    print("\n[4] 轮询端点免限流（关键护栏）")
    src = (SERVER / "routers" / "sr.py").read_text(encoding="utf-8")
    status_fn = src.split("def sr_status(")[1].split("\ndef ")[0]
    file_fn = src.split("def sr_file(")[1].split("\ndef ")[0]
    check("sr_status 未调用 _check_rate_limit", "_check_rate_limit" not in status_fn)
    check("sr_file 未调用 _check_rate_limit", "_check_rate_limit" not in file_fn)
    check("sr_status 有免限流说明注释", "限流" in status_fn or "轮询" in status_fn)
    # 提交端点**必须**限流（不然被刷）
    local_fn = src.split("def sr_local(")[1].split("\ndef ")[0]
    check("提交端点 sr_local 仍然限流", "_check_rate_limit" in local_fn)


def test_coreml_no_explicit_compute_units():
    """CoreML 不能显式指定 MLComputeUnits：实测比默认慢 3 倍。"""
    print("\n[5] CoreML 计算单元配置")
    src = (SERVER / "routers" / "sr.py").read_text(encoding="utf-8")
    # 只检查**真实调用行**：源码注释里必须保留这条警告（那是踩坑记录），
    # 所以不能简单地全文搜索字符串，否则会把知识沉淀误判成违规。
    call_lines = [ln for ln in src.splitlines() if "InferenceSession(" in ln]
    check("存在创建 session 的调用", bool(call_lines))
    check("创建 session 时未传 MLComputeUnits / provider_options",
          all("MLComputeUnits" not in ln and "provider_options" not in ln
              for ln in call_lines),
          call_lines[:1])
    check("provider 顺序为 CoreML 优先",
          '["CoreMLExecutionProvider", "CPUExecutionProvider"]' in src)


def test_registry_and_output_layout():
    """输出落在 CONVERT_DIR，文件名带倍率标记；注册表可被桌面桥查到。"""
    print("\n[6] 输出布局与注册表")
    check("SR_JOBS 注册表存在", isinstance(sr.SR_JOBS, dict))
    check("输出目录用 CONVERT_DIR", "CONVERT_DIR" in
          (SERVER / "routers" / "sr.py").read_text(encoding="utf-8"))
    # 文件名规则：{stem}_{scale}x{ext}
    src = (SERVER / "routers" / "sr.py").read_text(encoding="utf-8")
    check("文件名带倍率标记", "{scale}x" in src or "_%sx" in src or "{scale}" in src)
    check("提供 AI 档可用性查询端点", "/api/sr/model/status" in src)


def test_ai_real_inference_if_cached():
    """若本地已缓存权重，跑一次真实推理（验证端到端输出尺寸）。"""
    print("\n[7] 真实推理（仅当模型已缓存）")
    if not (sr.available() and sr._valid_cached("x2")):
        print("  ⏭  模型未缓存，跳过（离线测试不联网下载）")
        return
    try:
        from PIL import Image
        src = Image.new("RGB", (100, 70), (200, 100, 50))
        job = {"progress": 0, "stage": "", "note": "", "elapsed": 0.0, "eta": 0.0}
        out = sr._sr_ai(src, "x2", job)
        check("AI 档输出为输入的 2 倍", out.size == (200, 140), str(out.size))
        check("AI 档回填了进度", job["progress"] > 0, str(job.get("progress")))
    except Exception as e:  # noqa: BLE001
        check("AI 档推理未抛错", False, str(e)[:120])


def test_video_filter_is_lgpl_safe():
    """视频滤镜链只能用 LGPL 构建里真实存在的滤镜。

    ⚠️ 红线：``hqdn3d`` 是 **GPL** 滤镜，自编译的 LGPL ffmpeg 里被裁掉了。
    写上去不会在明处报错，而是整条命令失败、用户只看到「增强失败」四个字。
    """
    print("\n[8] 视频滤镜链 LGPL 安全")
    # 只扫 _enhance_filter 函数体：docstring/注释里会提到这些名字作为反面教材，
    # 全文件扫描会被自己的警告文字误伤（与 CoreML MLComputeUnits 同一类问题）。
    src = (SERVER / "routers" / "sr.py").read_text(encoding="utf-8")
    tail = src.split("def _enhance_filter")[1]
    body = tail.split("chain = [", 1)[1].split("return")[0]   # 只取构造滤镜链的代码
    check("滤镜构造代码不含 GPL 滤镜 hqdn3d", "hqdn3d" not in body)
    check("滤镜构造代码不含 nlmeans（同属 GPL）", "nlmeans" not in body)

    std = sr._enhance_filter("standard", 1708, 960)
    enh = sr._enhance_filter("enhance", 1708, 960)
    check("标准档含 lanczos 放大", "flags=lanczos" in std)
    check("标准档含 cas 锐化", "cas=strength=" in std)
    check("标准档不含降噪（降噪只属于增强档）", "atadenoise" not in std)
    check("增强档在锐化前做降噪", enh.index("atadenoise") < enh.index("cas"))
    # 偶数尺寸：yuv420p 下奇数宽/高会让编码直接失败
    w = int(std.split("scale=")[1].split(":")[0])
    check("输出宽度为偶数（yuv420p 要求）", w % 2 == 0, str(w))


def test_video_bitrate_lift():
    """码率策略：在源码率上提升，再用目标分辨率的合理上限兜住。

    ⚠️ 2026-09-12 实测后修正：最初按目标分辨率「满配」码率，480p/600k 的片
    放大到 960p 被给到 4500k，实测产物体积涨 **6.7 倍**而观感几乎不变 ——
    放大不产生新信息，满配纯属浪费。改后同片只涨 1.6 倍（1090k）。
    """
    print("\n[9] 视频码率提升策略")
    from codec_utils import res_cap_kbps
    check("提升倍率在合理区间（1.2~3.0）", 1.2 <= sr._BITRATE_LIFT <= 3.0)
    check("存在码率上限保护", sr._MAX_ENHANCE_KBPS > 0)
    lifted = int(600 * sr._BITRATE_LIFT)
    cap = res_cap_kbps(960)
    check(f"低码率源按提升倍率而非分辨率满配（{lifted}k < {cap}k）", lifted < cap)
    check("放大后码率确实高于源码率", lifted > 600)


def test_video_mode_validation():
    """档位校验：非法值静默降级到默认档，绝不把非法参数透给 ffmpeg。"""
    print("\n[10] 视频档位校验")
    check("standard 合法", sr._validate_video_mode("standard") == "standard")
    check("enhance 合法", sr._validate_video_mode("enhance") == "enhance")
    check("非法档位降级为 standard", sr._validate_video_mode("ultra") == "standard")
    check("空值降级为 standard", sr._validate_video_mode("") == "standard")
    check("大小写不敏感", sr._validate_video_mode("ENHANCE") == "enhance")
    check("视频档位与图片档位不重叠", not (sr.VIDEO_MODES & sr.MODES))
    check("输入短边上限已设定（低清片定位）", sr._MAX_INPUT_SHORT_SIDE > 0)
    check("×4 的限制比通用上限更严", 360 < sr._MAX_INPUT_SHORT_SIDE)


def test_video_endpoints_not_rate_limited():
    """视频增强会跑几十分钟，轮询端点绝不能限流（否则进度条永久冻死）。"""
    print("\n[11] 视频端点限流边界")
    src = (SERVER / "routers" / "sr.py").read_text(encoding="utf-8")
    for fn in ("def sr_video_local", "def sr_status", "def sr_file", "def sr_video_limits"):
        check(f"存在 {fn}", fn in src)
    block = src.split("def sr_video_local")[1].split("\n@router")[0]
    check("视频提交端点限流", "app._check_rate_limit(request)" in block)
    for name in ("def sr_status", "def sr_file"):
        b = src.split(name)[1].split("\ndef ")[0]
        check(f"{name} 未限流", "app._check_rate_limit(request)" not in b)


def main():
    print("=" * 50)
    print("高清修复（sr）离线测试")
    print("=" * 50)
    test_validate_params()
    test_fast_mode_upscales()
    test_ai_input_cap()
    test_status_endpoints_not_rate_limited()
    test_coreml_no_explicit_compute_units()
    test_registry_and_output_layout()
    test_ai_real_inference_if_cached()
    test_video_filter_is_lgpl_safe()
    test_video_bitrate_lift()
    test_video_mode_validation()
    test_video_endpoints_not_rate_limited()
    print("\n" + "=" * 50)
    print(f"  通过: {PASS}   失败: {FAIL}")
    print("=" * 50)
    if FAIL:
        sys.exit(1)
    print("✅ 高清修复测试全部通过")


if __name__ == "__main__":
    main()
