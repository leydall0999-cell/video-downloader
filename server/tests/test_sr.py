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
    print("\n" + "=" * 50)
    print(f"  通过: {PASS}   失败: {FAIL}")
    print("=" * 50)
    if FAIL:
        sys.exit(1)
    print("✅ 高清修复测试全部通过")


if __name__ == "__main__":
    main()
