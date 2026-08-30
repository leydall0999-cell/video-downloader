#!/usr/bin/env python3
"""AI 去水印多模型对比 benchmark —— 在**有网本机**运行（沙盒无外网，跑不了）。

复用 dewatermark_ai 的注册表 + 下载逻辑：缺失的模型会自动从 HuggingFace 拉到 ~/.vdl_models。
对每个候选模型：加载 onnxruntime session（CPU EP + 最优线程）→ 跑 N 次 512×512 推理
→ 输出每瓦片平均耗时(ms)、模型体积(MB)、输出像素范围自检（推断 out_div 是否正确）。

用法（在 video-downloader-app 目录下）:
  .build_venv/bin/python bench_models.py
  .build_venv/bin/python bench_models.py --models lama,lama_dilated --runs 30
  .build_venv/bin/python bench_models.py --image ~/watermarked.jpg --mask ~/mask.png   # 真实图粗检

判读:
- avg_ms 越小越快；多模型时自动打印相对首个模型的加速比。
- out_max: 若 > 1.5 说明模型输出 [0,255]（out_div=255.0 正确）；若 <= 1.5 说明输出 [0,1]，
  需把 dewatermark_ai.MODELS[该模型]["out_div"] 改成 1.0，否则瓦片融合会变黑。
- 真实图粗检：跑一次并报告输出范围/非零比例，作为画质 sanity（非 PSNR）。
"""
import argparse
import os
import sys
import time
import statistics

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "server"))
import dewatermark_ai as dwc  # noqa: E402
import numpy as np  # noqa: E402


def bench_one(name: str, runs: int) -> dict:
    spec = dwc.MODELS[name]
    path = dwc._ensure_model(name)  # 缺失自动下载
    import onnxruntime as ort  # 延迟导入
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    so.enable_mem_pattern = False
    so.intra_op_num_threads = dwc._optimal_threads()
    so.inter_op_num_threads = 1
    sess = ort.InferenceSession(str(path), sess_options=so, providers=["CPUExecutionProvider"])
    inputs = sess.get_inputs()
    rng = np.random.default_rng(0)
    img = rng.random((1, 3, 512, 512), dtype=np.float32)
    mask = (rng.random((1, 1, 512, 512), dtype=np.float32) > 0.5).astype(np.float32)
    feeds = {inputs[0].name: img, inputs[1].name: mask}

    sess.run(None, feeds)  # warmup 1 次（含图优化）
    ts, out_max = [], 0.0
    for _ in range(runs):
        t0 = time.perf_counter()
        out = sess.run(None, feeds)[0]
        ts.append(time.perf_counter() - t0)
        out_max = max(out_max, float(out.max()))
    avg = statistics.mean(ts) * 1000
    p50 = statistics.median(ts) * 1000
    size_mb = path.stat().st_size / 1e6
    out_div = 255.0 if out_max > 1.5 else 1.0
    flag = "" if abs(out_div - float(spec.get("out_div", 255.0))) < 1e-6 else "  ⚠️ out_div 与注册表不符，需改 MODELS"
    print(f"[{name}] size={size_mb:.1f}MB  avg={avg:.1f}ms  p50={p50:.1f}ms  "
          f"runs={runs}  out_max={out_max:.2f} -> out_div≈{out_div}{flag}")
    return {"name": name, "size_mb": size_mb, "avg_ms": avg, "out_div": out_div}


def real_image_check(image: str, mask: str) -> None:
    if dwc._cv2 is None:
        print("[真实图粗检] 跳过：cv2 未安装")
        return
    import cv2  # noqa: E402
    img = cv2.imread(image, cv2.IMREAD_COLOR)
    m = cv2.imread(mask, cv2.IMREAD_GRAYSCALE)
    if img is None or m is None:
        print("[真实图粗检] 跳过：无法读取图片/掩码")
        return
    h, w = img.shape[:2]
    m = cv2.resize(m, (w, h), interpolation=cv2.INTER_NEAREST)
    for name in dwc.list_models():
        try:
            spec = dwc.MODELS[name]
            path = dwc._ensure_model(name)
            import onnxruntime as ort
            so = ort.SessionOptions()
            so.intra_op_num_threads = dwc._optimal_threads()
            sess = ort.InferenceSession(str(path), sess_options=so, providers=["CPUExecutionProvider"])
            rgb = img[..., ::-1].astype(np.float32) / 255.0
            mk = (m.astype(np.float32) / 255.0)[..., None]
            # 缩放进 512 以适应固定输入
            s = 512 / max(h, w)
            ih, iw = max(1, int(round(h * s))), max(1, int(round(w * s)))
            ri = cv2.resize(rgb, (iw, ih))
            rm = cv2.resize(mk, (iw, ih), interpolation=cv2.INTER_NEAREST)
            feeds = {sess.get_inputs()[0].name: ri.transpose(2, 0, 1)[None].astype(np.float32),
                     sess.get_inputs()[1].name: rm.transpose(2, 0, 1)[None].astype(np.float32)}
            out = sess.run(None, feeds)[0][0].transpose(1, 2, 0)
            out_div = 255.0 if float(out.max()) > 1.5 else 1.0
            out = np.clip(out / out_div, 0, 1)
            nz = float((out > 0.01).mean())
            print(f"[真实图粗检][{name}] out range=[{out.min():.3f},{out.max():.3f}] "
                  f"非零像素比例={nz:.3f}  (out_div≈{out_div})")
        except Exception as e:  # noqa: BLE001
            print(f"[真实图粗检][{name}] 失败: {e}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="lama,lama_dilated")
    ap.add_argument("--runs", type=int, default=20)
    ap.add_argument("--image", default="")
    ap.add_argument("--mask", default="")
    args = ap.parse_args()
    names = [m.strip() for m in args.models.split(",") if m.strip()]

    print(f"platform: {dwc.platform.system()} {dwc.platform.machine()}  "
          f"optimal_threads={dwc._optimal_threads()}")
    print("=" * 64)
    results = []
    for n in names:
        try:
            results.append(bench_one(n, args.runs))
        except Exception as e:  # noqa: BLE001
            print(f"[{n}] FAILED: {e}")
    print("=" * 64)
    if len(results) >= 2:
        base = results[0]["avg_ms"]
        for r in results[1:]:
            speedup = base / r["avg_ms"] if r["avg_ms"] else 0
            verdict = "更快 ✅" if speedup > 1.05 else ("更慢 ❌" if speedup < 0.95 else "持平")
            print(f"{r['name']} vs {results[0]['name']}: {speedup:.2f}x  "
                  f"({r['avg_ms']:.1f}ms vs {base:.1f}ms)  {verdict}")
    if args.image and args.mask:
        print("-" * 64)
        real_image_check(args.image, args.mask)


if __name__ == "__main__":
    main()
