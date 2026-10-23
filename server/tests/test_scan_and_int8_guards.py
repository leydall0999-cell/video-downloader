"""2026-10-20 Win 版全功能实测发现的两个回归的守卫测试。

1. `library.scan_library` 必须跳过功能内部工作区
   （实测：30s 视频去水印写 450+ 张帧图到 dewatermark/dw_work_*/frames/，
   媒体库 456 条里 451 条是帧图，真片子被埋、误删还会毁掉在跑的任务）。
2. `dewatermark_ai._get_session` 里的 `_INT8_RUNTIME_OK` 必须声明 global
   （缺声明 → UnboundLocalError 被吞成「INT8 会话创建失败，回退 fp32」，
   白量化两分钟却永远拿不到提速）。

运行：cd server && python -m pytest tests/test_scan_and_int8_guards.py -q
"""
import inspect
import os
import sys
from pathlib import Path

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import dewatermark_ai as D  # noqa: E402
import library as L  # noqa: E402


def _touch(p: Path, data: bytes = b"x" * 2048):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def test_scan_skips_internal_workdirs(tmp_path):
    """去水印工作区/预览缓存/上传暂存不进媒体库；去水印成品仍然进。"""
    dw = tmp_path / "dewatermark"
    # 中间产物（必须全部被排除）
    _touch(dw / "dw_work_1bc3" / "frames" / "00427.png")
    _touch(dw / "dw_work_1bc3" / "frames" / "00428.png")
    _touch(dw / "dw_work_1bc3" / "mask.mp4")
    _touch(dw / "dw_up_573f4307.png")
    _touch(dw / "dw_vid_up_cfe703e432f6.mp4")
    _touch(dw / "dw_vid_thumb_cfe703e4.jpg")
    _touch(dw / "dw_vid_film_cfe703e4.jpg")
    _touch(dw / "dw_prev_9f2a.mp4")
    _touch(dw / "dw_prev_up_9f2a.mp4")
    _touch(dw / "mat_up_8461884da805.png")
    _touch(dw / "mat_anl_abc.png")
    _touch(dw / "mat_spl_abc.png")
    _touch(dw / "preview_cache" / "seg_0.mp4")
    _touch(tmp_path / "uploads" / "chunk_0.part")
    _touch(tmp_path / "uploads" / "uploaded.mp4")
    # 用户素材与成品（必须仍然可见）
    _touch(tmp_path / "真实视频.mp4")
    _touch(dw / "dw_vid_abc.mp4")
    _touch(dw / "dw_img_abc.png")
    _touch(dw / "mat_abc.png")

    items = L.scan_library(tmp_path, force=True)
    names = {i["name"] for i in items}

    assert "真实视频.mp4" in names
    assert "dw_vid_abc.mp4" in names, "去水印成品不该被一起藏起来"
    assert "dw_img_abc.png" in names
    assert "mat_abc.png" in names, "抠像成品不该被一起藏起来"
    for bad in ("00427.png", "00428.png", "mask.mp4", "dw_up_573f4307.png",
                "dw_vid_up_cfe703e432f6.mp4",
                "dw_vid_thumb_cfe703e4.jpg", "dw_vid_film_cfe703e4.jpg",
                "dw_prev_9f2a.mp4", "dw_prev_up_9f2a.mp4",
                "mat_up_8461884da805.png", "mat_anl_abc.png", "mat_spl_abc.png",
                "seg_0.mp4", "chunk_0.part", "uploaded.mp4"):
        assert bad not in names, f"内部中间产物 {bad} 不应出现在媒体库"
    assert len(items) == 4, f"应只剩 4 条真实素材/成品，实际 {sorted(names)}"


def test_get_session_declares_int8_global():
    """`_INT8_RUNTIME_OK` 若被当成局部变量，读它就会 UnboundLocalError。"""
    code = D._get_session.__code__
    assert "_INT8_RUNTIME_OK" not in code.co_varnames, (
        "_get_session 把 _INT8_RUNTIME_OK 当成了局部变量："
        "函数里赋值了它却没写 global，运行时会 UnboundLocalError"
    )
    assert "global _INT8_RUNTIME_OK" in inspect.getsource(D._get_session)
