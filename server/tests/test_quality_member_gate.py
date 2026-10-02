"""清晰度会员门槛 E2E（2026-10-02 用户定档：免费用户 2K/4K 要开会员才能下）。

规则：免费档显式选 >1080P（1440/2160/4320…）→ 402 + MEMBER_QUOTA| 引导开会员；
      会员档放行；「最佳画质（自动）」不在此拦（免费档的封顶由前端按来源类型落档，
      因为直链/分片是单一流，强塞 1080 会挑不到流）。

覆盖：
  1. 免费 + 2160/1440 → 402，前缀 MEMBER_QUOTA|，文案给出「最高 1080P」与会员引导
  2. 免费 + 2160 被拦时**不建任务、不烧配额**（门槛在建任务之前）
  3. 免费 + 1080/720/480/360/audio/webm/m4a/best → 全部放行（门槛只切超清档）
  4. 激活下载会员后 + 2160 → 放行，任务 quality_key 记录真实档位
  5. 批量（/api/batch）同规：免费 2160 → 402；免费 1080 → 放行
  6. 会员态读不到 → fail-open（绝不因后端异常误伤付费用户）
  7. _quality_height 折算：常量键不参与判档

运行（独立进程，HOME + VDL_DATA_DIR 双隔离）：
    cd server && python tests/test_quality_member_gate.py
    .build_venv/bin/python -m pytest tests/test_quality_member_gate.py -v
"""
import os
import shutil
import sys
import tempfile
import types

_TMP = tempfile.mkdtemp(prefix="vdl_quality_gate_")
os.environ["HOME"] = _TMP  # 必须在 import app 之前
os.makedirs(os.path.join(_TMP, ".video-downloader"), exist_ok=True)
os.environ["VDL_DATA_DIR"] = os.path.join(_TMP, ".video-downloader")

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from routers import core as core_router  # noqa: E402

client = TestClient(server_app.app, raise_server_exceptions=False)

_FAKE_INFO = {
    "id": "f1", "title": "demo", "uploader": "u", "duration": 10,
    "webpage_url": "https://www.bilibili.com/video/BV1xx411c7mD", "extractor_key": "BiliBili",
    "formats": [{"height": 2160, "vcodec": "avc1", "ext": "mp4", "filesize": 4000},
                {"height": 1440, "vcodec": "avc1", "ext": "mp4", "filesize": 2000},
                {"height": 1080, "vcodec": "avc1", "ext": "mp4", "filesize": 1000},
                {"height": 720, "vcodec": "avc1", "ext": "mp4", "filesize": 800},
                {"height": None, "vcodec": "none", "ext": "m4a", "acodec": "mp4a", "filesize": 50}],
    "url": "https://example.com/v.mp4", "is_live": False, "thumbnail": "",
}
_URL = "https://www.bilibili.com/video/BV1xx411c7mD"


def _setup():
    server_app.downloader.probe = types.MethodType(
        lambda self, url, cookie="", proxy="": dict(_FAKE_INFO), server_app.downloader)
    server_app.scheduler.submit = lambda *a, **k: None
    server_app._check_rate_limit = lambda request: None


def _reset_state():
    """清会员状态文件（同时复位内存态），保证每项测试都从「免费档」起跑。"""
    p = os.path.join(_TMP, ".video-downloader", "membership.json")
    if os.path.exists(p):
        os.remove(p)
    server_app.member_store._loaded = False


def _task_count():
    return len(server_app.store.list_all())


def _download(quality):
    return client.post("/api/download", json={"url": _URL, "quality": quality,
                                              "title": "demo", "cookie": "", "proxy": ""})


def test_reject_free_above_1080():
    """免费档 2160/1440 → 402 + MEMBER_QUOTA| + 明确告知最高 1080P。"""
    _reset_state()
    _setup()
    for q in ("2160", "1440"):
        before = _task_count()
        r = _download(q)
        assert r.status_code == 402, f"{q} 免费档应 402，实际 {r.status_code}: {r.text[:200]}"
        detail = r.json().get("detail", "")
        assert detail.startswith("MEMBER_QUOTA|"), detail
        assert "1080P" in detail, f"文案要说清上限：{detail}"
        assert "开通下载会员" in detail, f"文案要给出会员引导：{detail}"
        # 文案不能复用「次/日」配额口径（那是另一堵墙）
        assert "/日" not in detail, f"清晰度门槛不该出现配额口径：{detail}"
        assert _task_count() == before, f"{q} 被拦时不应建任务"
    print("✅ 免费档 2160/1440 → 402 + MEMBER_QUOTA|（告知上限 1080P），且不建任务")


def test_reject_free_does_not_burn_quota():
    """门槛在建任务之前：被清晰度门拦下不消耗免费下载次数。"""
    _reset_state()
    _setup()
    assert _download("2160").status_code == 402
    used = server_app.member_store.status()["daily_usage"].get("download", 0)
    assert used == 0, f"被清晰度门拦下不应烧下载配额，实际 used={used}"
    print("✅ 清晰度门先于计费：被拦不烧免费额度")


def test_allow_free_at_or_below_1080():
    """1080 及以下 + 音频/容器档 + best 一律放行。"""
    _reset_state()
    _setup()
    for q in ("1080", "720", "480", "360", "audio", "m4a", "webm", "best"):
        r = _download(q)
        assert r.status_code == 200, f"{q} 免费档应放行，实际 {r.status_code}: {r.text[:200]}"
    print("✅ 免费档 1080/720/480/360/audio/m4a/webm/best 全部放行")


def test_member_allows_4k():
    """激活下载会员 → 2160 放行，任务记录真实档位。"""
    _reset_state()
    _setup()
    assert _download("2160").status_code == 402, "前置：免费档应被拦"
    r = server_app.member_store.activate("download_month")
    assert r["ok"]
    before = _task_count()
    resp = _download("2160")
    assert resp.status_code == 200, f"会员档应放行 2160：{resp.text[:200]}"
    task_id = resp.json().get("task_id")
    tasks = {t.id: t for t in server_app.store.list_all()}
    assert _task_count() == before + 1 and task_id in tasks
    assert tasks[task_id].quality_key == "2160", tasks[task_id].quality_key
    assert "4K" in tasks[task_id].quality or "2160" in tasks[task_id].quality, tasks[task_id].quality
    print("✅ 会员档 2160 放行，任务 quality_key=2160（标签含 4K）")


def test_batch_same_rule():
    """批量入口同规：免费 2160 → 402；免费 1080 → 放行。"""
    _reset_state()
    _setup()
    body = {"urls": [_URL], "quality": "2160", "cookie": "", "proxy": ""}
    r = client.post("/api/batch", json=body)
    assert r.status_code == 402, f"批量免费 2160 应 402，实际 {r.status_code}: {r.text[:200]}"
    assert r.json().get("detail", "").startswith("MEMBER_QUOTA|"), r.text[:200]
    body["quality"] = "1080"
    r = client.post("/api/batch", json=body)
    assert r.status_code == 200, f"批量免费 1080 应放行：{r.text[:200]}"
    assert r.json().get("count") == 1, r.text[:200]
    print("✅ /api/batch 同规：免费 2160 拦、1080 放")


def test_gate_fails_open_without_member_store():
    """会员态读不到 → fail-open（宁可漏管一次，也不误挡付费用户）。

    只做函数级断言：HTTP 路径上 `_download_gate_error`（2026-09-06 的配额墙）先跑，
    它同样必须读会员态，故 store 抛异常时整条请求本来就会 500 —— 那是既有行为，
    与本门槛无关，不能拿来当本函数的判据。
    """
    _reset_state()
    _setup()
    orig = server_app.current_member_store

    def _boom(_request):
        raise RuntimeError("会员态不可用")

    server_app.current_member_store = _boom
    try:
        assert core_router._quality_gate_error(None, "2160") is None, "会员态异常时应放行"
        assert core_router._quality_gate_error(None, "1440") is None, "会员态异常时应放行"
        # 放行不等于绕过别的墙：清晰度门自己绝不能在异常时抛出去
        assert core_router._quality_gate_error(None, "1080") is None
    finally:
        server_app.current_member_store = orig
    print("✅ 会员态不可用时清晰度门 fail-open（不拦、也不抛）")


def test_quality_height_mapping():
    """常量键不参与判档；数字键按高度判。"""
    h = core_router._quality_height
    assert h("best") == 0 and h("audio") == 0 and h("webm") == 0 and h("m4a") == 0
    assert h("") == 0 and h("1080p") == 0 and h("abc") == 0
    assert h("1080") == 1080 and h("1440") == 1440 and h("2160") == 2160
    assert core_router.FREE_MAX_QUALITY == 1080
    print("✅ _quality_height 折算正确（常量键/脏值 → 0，不误判）")


if __name__ == "__main__":
    test_quality_height_mapping()
    test_reject_free_above_1080()
    test_reject_free_does_not_burn_quota()
    test_allow_free_at_or_below_1080()
    test_member_allows_4k()
    test_batch_same_rule()
    test_gate_fails_open_without_member_store()
    print("\n🎉 清晰度会员门槛 E2E 全部通过（7 项）")
    shutil.rmtree(_TMP, ignore_errors=True)
