"""下载报错分类（category）贯穿测试。

验证：
1. DownloadTask 携带 category 字段，且 to_public_dict 序列化输出它；
2. 下载失败时 _run_once 会把 _friendly_error 产出的 category 写入任务，
   前端任务卡片 / 列表据此展示针对性行动建议，而不是笼统的「下载失败」。

不依赖真实网络：用 monkeypatch 让 _download_options 直接抛错模拟下载失败。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

import pytest

import downloader as dl  # noqa: E402
from tasks import TaskStore  # noqa: E402


def test_task_category_field_and_serialization(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create(url="https://example.com/v", title="", platform="test", quality="best", quality_key="best")
    assert task.category == ""
    task.category = "cdn_forbidden"
    d = task.to_public_dict()
    assert "category" in d
    assert d["category"] == "cdn_forbidden"


def test_run_once_stores_category_on_403(tmp_path, monkeypatch):
    """通用 403（非 hardened 站点）应归类为 cdn_forbidden。"""
    store = TaskStore(tmp_path)
    task = store.create(
        url="https://www.youtube.com/watch?v=abc123",
        title="", platform="youtube", quality="best", quality_key="best",
    )

    def boom(*a, **k):
        from yt_dlp.utils import DownloadError
        raise DownloadError("HTTP Error 403: Forbidden")

    monkeypatch.setattr(dl, "_download_options", boom)
    # 阻断可能发网络的辅助调用
    monkeypatch.setattr(dl, "_normalize_share_url", lambda u, proxy="": u)
    monkeypatch.setattr(dl, "_resolve_proxy", lambda *a, **k: "")

    dl._run_once(task, store, "best")

    updated = store.get(task.id)
    assert updated.status == "failed"
    assert updated.category == "cdn_forbidden"
    # error / hint 也应被写入，前端失败区据此展示
    assert updated.error
    assert updated.hint


def test_run_once_stores_category_network_on_oserror(tmp_path, monkeypatch):
    """底层 OSError（DNS/连接/超时）应归类为 network，前端据此给「重试」建议。"""
    store = TaskStore(tmp_path)
    task = store.create(
        url="https://www.youtube.com/watch?v=xyz",
        title="", platform="youtube", quality="best", quality_key="best",
    )

    def boom(*a, **k):
        raise OSError("Name or service not known")

    monkeypatch.setattr(dl, "_download_options", boom)
    monkeypatch.setattr(dl, "_normalize_share_url", lambda u, proxy="": u)
    monkeypatch.setattr(dl, "_resolve_proxy", lambda *a, **k: "")

    dl._run_once(task, store, "best")

    updated = store.get(task.id)
    assert updated.status == "failed"
    assert updated.category == "network"


def test_friendly_error_categories():
    """_friendly_error 的 rules 分支应带正确 category，前端据此差异化展示。"""
    from yt_dlp.utils import DownloadError, ExtractorError

    # 优酷 -3007（非 403 文案）也应归类为 cookie_required → 前端显示「去粘贴 Cookie」
    e = dl._friendly_error(
        ExtractorError("Youku said: -3007: you need to login"),
        {"host": "youku.com", "is_hardened": True},
    )
    assert e.category == "cookie_required"

    # 网络超时 → network（前端给「重试」）
    e2 = dl._friendly_error(DownloadError("Read timed out"), {"host": "x"})
    assert e2.category == "network"

    # 地区限制 → restricted（前端提示官方渠道）
    e3 = dl._friendly_error(DownloadError("not available in your country"), {"host": "x"})
    assert e3.category == "restricted"

    # 兜底未知 → unknown
    e4 = dl._friendly_error(DownloadError("some random boom"), {"host": "x"})
    assert e4.category == "unknown"
