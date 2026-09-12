"""图片去水印路由层（扩散档 engine=diffusion）集成回归测试（2026-09-12 新增）。

用 FastAPI TestClient 在沙盒内验证「扩散去水印」这条引擎档在路由层被正确接线、
降级与校验——不下载任何权重、不跑真实推理：

  - GET /api/dw/capability 必须 200 且暴露 engines / recommended_engine（前端据此置灰/推荐）
  - POST /api/dw/image 带 engine=diffusion 但本机扩散不可用 → 503（与 cv2/lama 降级模式一致）
  - POST /api/dw/image 带 engine=未知值 → 400（engine 白名单）
  - POST /api/dw/image 带 engine=diffusion + model=未知扩散模型 → 400（model 白名单）

深究：本机（沙盒 8GB + 无 torch）dewatermark_diffusion.available() 恒 False，
故 diffusion 503 路径是这里的默认现实；model 白名单测试通过 monkeypatch
available()=True + list_diffusion_models()=[sd15] 构造「可用」分支后再验证 400。
"""
import os
import sys
from unittest import mock

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402
import dewatermark_diffusion as dwc_diff  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _client():
    return TestClient(server_app.app)


def _png_bytes():
    """最小合法 PNG（1x1 红点），仅供表单上传通过文件扩展名/类型校验。"""
    import base64
    # 1x1 真·PNG（IHDR+IDAT+IEND），不依赖 Pillow/cv2 即可落盘后被 FastAPI 接收
    b64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    return base64.b64decode(b64)


def _post_dw_image(client, engine, model="", regions='[{"x":0.1,"y":0.1,"w":0.2,"h":0.2,"op":"add"}]'):
    return client.post(
        "/api/dw/image",
        files={"file": ("w.png", _png_bytes(), "image/png")},
        data={"regions": regions, "engine": engine, "model": model},
    )


def test_capability_endpoint_exposes_engines():
    """/api/dw/capability 必须 200 并暴露 engines/recommended_engine 字段。"""
    c = _client()
    r = c.get("/api/dw/capability")
    assert r.status_code == 200, f"GET /api/dw/capability 非 200: {r.status_code} {r.text[:200]}"
    body = r.json()
    for k in ("engines", "recommended_engine", "tier", "diffusion_available"):
        assert k in body, f"capability 响应缺字段 {k}"
    assert "diffusion" in body["engines"], "engines 应包含 diffusion 键"
    print("✅ GET /api/dw/capability 暴露 engines/recommended_engine（前端据此置灰/推荐）")


def test_image_diffusion_unavailable_returns_503():
    """沙盒内扩散不可用 → engine=diffusion 必须返回 503（优雅降级，不拖垮其余路由）。"""
    c = _client()
    r = _post_dw_image(c, engine="diffusion")
    assert r.status_code == 503, f"diffusion 不可用却非 503: {r.status_code} {r.text[:200]}"
    assert "扩散" in (r.json() or {}).get("detail", ""), "503 文案应点明扩散不可用"
    print("✅ POST /api/dw/image engine=diffusion 本机不可用时 → 503（降级一致）")


def test_image_engine_whitelist_rejects_unknown():
    """engine 不在 (auto/opencv/ai/diffusion) → 400。"""
    c = _client()
    r = _post_dw_image(c, engine="bogus_diffusion")
    assert r.status_code == 400, f"未知 engine 未拦下: {r.status_code} {r.text[:200]}"
    assert "engine" in (r.json() or {}).get("detail", ""), "400 文案应点明 engine 非法"
    print("✅ POST /api/dw/image engine=未知值 → 400（白名单）")


def test_image_diffusion_model_whitelist_rejects_unknown():
    """engine=diffusion 且 model 不在可选列表 → 400（在「可用」分支下验证 model 校验）。"""
    c = _client()
    with mock.patch.object(dwc_diff, "available", return_value=True), \
         mock.patch.object(dwc_diff, "list_diffusion_models", return_value=["sd15"]):
        r = _post_dw_image(c, engine="diffusion", model="sdxl_bogus")
        assert r.status_code == 400, f"未知扩散模型未拦下: {r.status_code} {r.text[:200]}"
        detail = (r.json() or {}).get("detail", "")
        assert "扩散模型" in detail, "400 文案应点明扩散模型非法"
    print("✅ POST /api/dw/image engine=diffusion + model=未知 → 400（model 白名单）")


if __name__ == "__main__":
    test_capability_endpoint_exposes_engines()
    test_image_diffusion_unavailable_returns_503()
    test_image_engine_whitelist_rejects_unknown()
    test_image_diffusion_model_whitelist_rejects_unknown()
    print("\n🎉 图片去水印路由层（扩散档）集成测试全部通过（4 项）")
