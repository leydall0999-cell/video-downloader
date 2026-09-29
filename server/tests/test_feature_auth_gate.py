#!/usr/bin/env python3
"""功能级登录门禁（服务端）回归测试。

背景（2026-09-29 用户要求「所有功能必须登录才能使用」并确认服务端也要强制）
--------------------------------------------------------------------------
前端门禁（document 捕获拦截 + 登录弹窗，见 web/app.js _LOGIN_GATED_ACTIONS）
只是第一层；本测试钉住服务端第二层：执行类端点（转换/拼接/去水印/字幕/解说/
订阅/种子/队列/清理）未登录一律 403，detail 含「登录」（前端 request() 据
403+登录 文案置 needLogin 并拉起登录框）；带有效 Bearer 则门禁放行。

全程离线：auth_store.create_user + issue_token 本地造账号，不打云端、
不打 ffmpeg、不打上传端点真实转码（payload 在门禁之后的业务校验处自然失败）。
运行：cd server && python tests/test_feature_auth_gate.py
"""
import os
import sys

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

os.environ.setdefault("VDL_DATA_DIR", "/tmp/vdl_test_authgate")
os.environ.setdefault("VDL_CLOUD_LINK", "0")

import app as server_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# 打桩：限流/配额放行（登录门禁在其之前，不受影响）
server_app._check_rate_limit = lambda request: None
server_app._check_convert_quota = lambda request: (True, 0, 5)

from auth_store import create_user, issue_token  # noqa: E402

c = TestClient(server_app.app)

# (path, kwargs)：kwargs 必须能过 FastAPI 参数校验（门禁在函数体内，校验在前）。
# 注：web 服务端只挂载 crypto/fs/convert/dewatermark/cloud/core/cn_tunnel/
# membership/payment/extension/auth/admin/support/subtitle —— 解说/订阅/种子/
# 队列/清理/媒体库字幕 router 未挂载（网页端这些按钮对应的后端不存在），故不在清单。
GATED = [
    ("/api/convert", {"json": {"task_id": "gate-t1", "target": "mp3"}}),
    ("/api/upload-chunk/finish", {"data": {"upload_id": "gatefinish01", "total": "1"}}),
    ("/api/convert/reconvert", {"data": {"job_id": "nope", "target": "mp3"}}),
    ("/api/concat", {"json": {"segments": ["seg_a.mp4", "seg_b.mp4"]}}),
    ("/api/dw/image", {"files": {"file": ("a.png", b"\x89PNG fake")}}),
    ("/api/dw/pdf", {"files": {"file": ("a.pdf", b"%PDF-1.4 fake")}}),
    ("/api/subtitle/extract", {"json": {"local_path": "/tmp/gate_x.mp4"}}),
    ("/api/subtitle/finish", {"data": {"upload_id": "gatesubfin01", "total": "1"}}),
]


def test_anonymous_blocked():
    """未登录：执行类端点一律 403，detail 必须引导登录。"""
    for path, kw in GATED:
        r = c.post(path, **kw)
        assert r.status_code == 403, f"{path} 未登录应 403，得到 {r.status_code}: {r.text[:120]}"
        detail = ""
        try:
            detail = r.json().get("detail") or ""
        except Exception:
            pass
        assert "登录" in detail, f"{path} 403 文案必须含「登录」（前端据此拉登录框），得到：{detail!r}"
    print(f"✅ 未登录 403 + 引导登录文案：{len(GATED)} 个执行类端点")


def test_with_token_passes_gate():
    """带有效 Bearer：门禁放行（后续业务校验 400/404/503 均可，但不得是「登录」403）。"""
    # 每次运行用唯一账号：create_user 对已存在 identifier 返回 None（测试需可重复跑）
    ident = f"gate_{os.getpid()}@self.test"
    uid = create_user(ident, "pw123456")
    assert uid, "本地造账号失败"
    tok = issue_token(uid)
    assert tok
    H = {"Authorization": f"Bearer {tok}"}
    for path, kw in GATED:
        r = c.post(path, headers=H, **kw)
        detail = ""
        try:
            detail = r.json().get("detail") or ""
        except Exception:
            pass
        assert not (r.status_code == 403 and "登录" in detail), \
            f"{path} 带有效 token 不应被登录门禁拦截（得到 {r.status_code}: {detail!r}）"
    print(f"✅ 有效 token 门禁放行：{len(GATED)} 个端点均未命中登录拦截")


def test_anonymous_upload_chunk_still_open():
    """分片上传端点本身不设门禁（finish 才拦）：匿名分片仍可上传，孤儿分片有 TTL 清理兜底。"""
    r = c.post("/api/upload-chunk",
               data={"upload_id": "gatechunk01", "index": "0", "total": "1"},
               files={"file": ("part1", b"A" * 1024)})
    assert r.status_code == 200, f"匿名分片上传应放行（门禁只挂 finish），得到 {r.status_code}: {r.text[:120]}"
    print("✅ 匿名分片上传不受影响（门禁只挂 finish/执行端点）")


if __name__ == "__main__":
    test_anonymous_blocked()
    test_with_token_passes_gate()
    test_anonymous_upload_chunk_still_open()
    print("ALL PASS")
