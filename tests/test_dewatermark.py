"""tests/test_dewatermark.py — 需求文档模块二（PDF/图片去水印）测试。

沙盒无法安装 cv2/fitz（PyPI 被代理拦截），用 fake 模块注入 dewatermark_core 命名空间，
跑通真实 job 生命周期与接线；像素正确性由部署环境真实库保证（用户本机验证）。
运行：PYTHONPATH=server:tests .build_venv/bin/python -m pytest tests/test_dewatermark.py -v
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

import dewatermark_core as dwc
import fake_cv2 as _fcv2
import fake_numpy as _fnumpy
import fake_fitz as _ffitz

# 注入 fake 原生库，使 available()/pdf_available() 在测试进程内为 True
dwc._cv2 = _fcv2.FakeCv2()
dwc._np = _fnumpy
dwc._fitz = _ffitz

from fastapi.testclient import TestClient  # noqa: E402
import app  # noqa: E402

client = TestClient(app.app)


def _wait(job_id, kind, timeout=8.0):
    """轮询状态直到 completed/failed。"""
    for _ in range(int(timeout * 10)):
        r = client.get(f"/api/dw/{kind}/{job_id}")
        assert r.status_code == 200, r.text
        st = r.json()["status"]
        if st in ("completed", "failed"):
            return st, r.json()
        time.sleep(0.1)
    return "timeout", {}


# ------------------------------------------------------------------ 纯逻辑（不依赖原生库）

def test_normalize_region_valid():
    r = dwc.normalize_region({"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.15})
    assert r == {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.15}


def test_normalize_region_none_and_zero():
    assert dwc.normalize_region(None) is None
    assert dwc.normalize_region({"x": 0, "y": 0, "w": 0, "h": 0}) is None
    assert dwc.normalize_region({"w": -1}) is None


def test_normalize_region_clamps_overflow():
    r = dwc.normalize_region({"x": 0.8, "y": 0.8, "w": 0.5, "h": 0.5})
    # x+w / y+h 不能越界 1.0
    assert r["x"] + r["w"] <= 1.0
    assert r["y"] + r["h"] <= 1.0


def test_region_to_px_uses_image_dims():
    # 100x80 图像，归一化 (0.1,0.1,0.2,0.1) -> (10,8,20,8)
    x, y, w, h = dwc._region_to_px({"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.1}, 100, 80)
    assert (x, y, w, h) == (10, 8, 20, 8)


# ------------------------------------------------------------------ 降级（缺依赖时 503）

def test_image_route_503_when_unavailable():
    saved_cv, saved_np = dwc._cv2, dwc._np
    dwc._cv2, dwc._np = None, None
    try:
        r = client.post("/api/dw/image",
                        files={"file": ("a.png", b"\x89PNG", "image/png")},
                        data={"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.1})
        assert r.status_code == 503
    finally:
        dwc._cv2, dwc._np = saved_cv, saved_np


def test_pdf_route_503_when_unavailable():
    saved = dwc._fitz
    dwc._fitz = None
    try:
        r = client.post("/api/dw/pdf",
                        files={"file": ("a.pdf", b"%PDF", "application/pdf")},
                        data={"mode": "annotations"})
        assert r.status_code == 503
    finally:
        dwc._fitz = saved


# ------------------------------------------------------------------ 图片去水印（注入 fake 跑通）

def test_image_route_param_validation():
    # 非图片 -> 409（依赖已注入，走到扩展名校验）
    r = client.post("/api/dw/image",
                    files={"file": ("a.txt", b"xx", "text/plain")},
                    data={"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.1})
    assert r.status_code == 409
    # 缺区域 -> 400
    r2 = client.post("/api/dw/image",
                     files={"file": ("a.png", b"\x89PNG", "image/png")},
                     data={"x": 0, "y": 0, "w": 0, "h": 0})
    assert r2.status_code == 400
    # 非法 method -> 400
    r3 = client.post("/api/dw/image",
                     files={"file": ("a.png", b"\x89PNG", "image/png")},
                     data={"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.1, "method": "bogus"})
    assert r3.status_code == 400


def test_image_route_success_lifecycle():
    r = client.post("/api/dw/image",
                    files={"file": ("a.png", b"\x89PNG", "image/png")},
                    data={"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.1, "method": "telea", "radius": 3})
    assert r.status_code == 200
    jid = r.json()["job_id"]
    st, body = _wait(jid, "image")
    assert st == "completed", body
    # 下载结果
    rf = client.get(f"/api/dw/image/{jid}/file")
    assert rf.status_code == 200
    assert rf.content == b"fake-image-bytes"
    # 未完成时下载 -> 409（直接构造 running 态 job 验证守卫，避免 fake 瞬时完成竞态）
    with app.DW_LOCK:
        app.DW_JOBS["fake_running_img"] = {
            "status": "running", "out_path": "", "error": "", "filename": "", "kind": "image",
        }
    rf2 = client.get("/api/dw/image/fake_running_img/file")
    assert rf2.status_code == 409
    # 不存在的 job -> 404
    assert client.get("/api/dw/image/nope/file").status_code == 404


# ------------------------------------------------------------------ PDF 去水印

def test_pdf_annotations_success():
    r = client.post("/api/dw/pdf",
                    files={"file": ("a.pdf", b"%PDF", "application/pdf")},
                    data={"mode": "annotations"})
    assert r.status_code == 200
    jid = r.json()["job_id"]
    st, body = _wait(jid, "pdf")
    assert st == "completed", body
    rf = client.get(f"/api/dw/pdf/{jid}/file")
    assert rf.status_code == 200
    assert rf.content.startswith(b"%PDF")


def test_pdf_raster_success():
    r = client.post("/api/dw/pdf",
                    files={"file": ("a.pdf", b"%PDF", "application/pdf")},
                    data={"mode": "raster", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.1,
                          "method": "ns", "radius": 4, "dpi": 120})
    assert r.status_code == 200
    jid = r.json()["job_id"]
    st, body = _wait(jid, "pdf")
    assert st == "completed", body
    rf = client.get(f"/api/dw/pdf/{jid}/file")
    assert rf.status_code == 200
    assert rf.content.startswith(b"%PDF")


def test_pdf_param_validation():
    # 非 PDF -> 409
    r = client.post("/api/dw/pdf",
                    files={"file": ("a.txt", b"xx", "text/plain")},
                    data={"mode": "annotations"})
    assert r.status_code == 409
    # 非法 mode -> 400
    r2 = client.post("/api/dw/pdf",
                     files={"file": ("a.pdf", b"%PDF", "application/pdf")},
                     data={"mode": "bogus"})
    assert r2.status_code == 400
    # raster 缺区域 -> 400
    r3 = client.post("/api/dw/pdf",
                     files={"file": ("a.pdf", b"%PDF", "application/pdf")},
                     data={"mode": "raster", "x": 0, "y": 0, "w": 0, "h": 0})
    assert r3.status_code == 400
