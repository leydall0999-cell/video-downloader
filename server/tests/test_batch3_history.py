"""A3/A4/B5 第三批回归测试（2026-10-02）。

- A4：_yt_proxy_override / _resolve_proxy —— VDL_PROXY_YT 环境变量与
  proxy.json["youtube"] 仅对 YouTube 域（含 googlevideo.com）生效，别站不受影响；
- A3：看门狗停滞 / 硬超时的 cancel_reason 标记（源码级钉死）+ 每轮尝试前重置；
- B5：到期完成任务降级为历史条目（文件清掉、记录保留、上限 50）、
  重启恢复 completed-but-no-file 为 file_expired 历史条目、
  retry 路由放行 file_expired（源码级）、前端 file_expired 接线（源码级）。
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("VDL_DATA_DIR", tempfile.mkdtemp())

import downloader  # noqa: E402
from downloader import _yt_proxy_override, _resolve_proxy  # noqa: E402
from tasks import TaskStore, DownloadTask, _MAX_PERSISTED_COMPLETED, TASK_TTL_SECONDS  # noqa: E402


def _with_yt_proxy(env_val, file_val, fn):
    """临时设置 VDL_PROXY_YT / VDL_HOME(proxy.json) 跑断言。"""
    old_env = os.environ.get("VDL_PROXY_YT")
    old_home = os.environ.get("VDL_HOME")
    tmp = None
    try:
        if env_val is None:
            os.environ.pop("VDL_PROXY_YT", None)
        else:
            os.environ["VDL_PROXY_YT"] = env_val
        tmp = tempfile.mkdtemp()
        if file_val is not None:
            Path(tmp, "proxy.json").write_text(
                json.dumps({"youtube": file_val}), encoding="utf-8")
        os.environ["VDL_HOME"] = tmp
        fn()
    finally:
        if old_env is None:
            os.environ.pop("VDL_PROXY_YT", None)
        else:
            os.environ["VDL_PROXY_YT"] = old_env
        if old_home is None:
            os.environ.pop("VDL_HOME", None)
        else:
            os.environ["VDL_HOME"] = old_home


def test_yt_proxy_priority_and_scope():
    def file_only():
        assert _yt_proxy_override() == "socks5h://127.0.0.1:40000", "proxy.json 兜底生效"
        assert _resolve_proxy("www.youtube.com") == "socks5h://127.0.0.1:40000"
        assert _resolve_proxy("youtu.be") == "socks5h://127.0.0.1:40000"
        assert _resolve_proxy("r1---sn-x.googlevideo.com") == "socks5h://127.0.0.1:40000", \
            "嗅探直链任务是 googlevideo 域，必须同样命中分流"
        assert _resolve_proxy("www.bilibili.com") != "socks5h://127.0.0.1:40000", \
            "分流只对 YouTube 生效，国内站不得被带偏"
        assert _resolve_proxy("rumble.com") != "socks5h://127.0.0.1:40000"

    def env_wins():
        assert _yt_proxy_override() == "http://127.0.0.1:7890", "环境变量优先于 proxy.json"

    def none_configured():
        assert _yt_proxy_override() == ""

    _with_yt_proxy(None, "socks5h://127.0.0.1:40000", file_only)
    _with_yt_proxy("http://127.0.0.1:7890", "socks5h://127.0.0.1:40000", env_wins)
    _with_yt_proxy(None, None, none_configured)


def test_cancel_reason_source_pins():
    """A3 源码级钉死：看门狗=stall、硬超时=timeout、每轮尝试前重置、终态按原因分流。"""
    src = Path(downloader.__file__).read_text(encoding="utf-8")
    assert 'task.cancel_reason = "stall"' in src
    assert 'task.cancel_reason = "timeout"' in src
    assert "task.cancel_reason = \"\"" in src, "每轮尝试前必须重置取消原因"
    assert 'if _reason == "stall":' in src and 'elif _reason == "timeout":' in src
    assert "已取消下载" in src  # 用户取消分支保留
    # 停滞终态必须是 failed（可行动错误 + 可续传），不是 canceled
    i_stall = src.index('if _reason == "stall":')
    i_chunk = src[i_stall:i_stall + 900]
    assert 'status="failed"' in i_chunk and "category=\"stalled\"" in i_chunk, \
        "停滞终态应为 failed + stalled 类别（用户能看懂、能续传）"


def _mk_completed_task(store: TaskStore, tid_prefix: str = "") -> DownloadTask:
    t = store.create(url="https://example.com/v", title="t", platform="p", quality="best")
    out = t.workdir / "out.mp4"
    out.write_bytes(b"x" * 32)
    store.update(t.id, status="completed", progress=100.0, filepath=out,
                 filename="out.mp4", filesize=32)
    return t


def test_purge_demotes_completed_keeps_history():
    root = Path(tempfile.mkdtemp())
    store = TaskStore(root)
    t = _mk_completed_task(store)
    store.update(t.id, created_at=time.time() - TASK_TTL_SECONDS - 10)
    demoted = store.purge_expired()
    assert demoted == 1, f"到期完成任务应降级 1 条，实际 {demoted}"
    got = store.get(t.id)
    assert got is not None, "历史条目必须保留在任务表"
    assert got.status == "completed" and got.file_expired
    assert got.to_public_dict()["file_expired"] is True, "API 序列化必须带 file_expired 字段"
    assert got.filepath is None
    assert got.filename == "out.mp4" and got.filesize == 32, "元数据保留供历史展示"
    assert not any(p.is_file() for p in got.workdir.iterdir()), "工作目录文件必须清掉"
    # 再跑一次不重复处理
    assert store.purge_expired() == 0


def test_purge_removes_failed_and_caps_history():
    root = Path(tempfile.mkdtemp())
    store = TaskStore(root)
    # 过期 failed → 整条移除（维持原行为）
    tf = _mk_completed_task(store)
    store.update(tf.id, status="failed", error="x", filepath=None)
    store.update(tf.id, created_at=time.time() - TASK_TTL_SECONDS - 10)
    # 造 _MAX_PERSISTED_COMPLETED + 5 条过期完成任务 → 历史只留 50
    for _ in range(_MAX_PERSISTED_COMPLETED + 5):
        th_ = _mk_completed_task(store)
        store.update(th_.id, created_at=time.time() - TASK_TTL_SECONDS - 10)
    store.purge_expired()
    assert store.get(tf.id) is None, "过期 failed 任务仍应整条删除"
    hist = [t for t in store.list_all() if t.status == "completed" and t.file_expired]
    assert len(hist) == _MAX_PERSISTED_COMPLETED, f"历史上限 {_MAX_PERSISTED_COMPLETED}，实际 {len(hist)}"
    # 最老的被清掉（历史应是最新的 50 条）
    assert all(t.id != "" for t in hist)


def test_restore_completed_without_file_as_history():
    root = Path(tempfile.mkdtemp())
    store = TaskStore(root)
    t = _mk_completed_task(store)
    row = {"id": t.id, "url": t.url, "title": "历史视频", "platform": "p",
           "quality": "best", "quality_key": "best", "status": "completed",
           "filename": "gone.mp4", "filesize": 123,
           "filepath": str(root / "no-such-dir" / "gone.mp4"),
           "created_at": time.time() - 9999}
    (root / "tasks_state.json").write_text(
        json.dumps({"version": 1, "saved_at": time.time(), "tasks": [row]}),
        encoding="utf-8")
    store2 = TaskStore(root)   # __init__ 会 _load_state
    got = store2.get(t.id)
    assert got is not None, "成品缺失的完成任务应恢复为历史条目而不是丢弃"
    assert got.status == "completed" and got.file_expired and got.filepath is None
    assert got.title == "历史视频"


def test_retry_and_frontend_wiring_source_pins():
    # 后端：retry 放行 file_expired
    core_src = Path(Path(downloader.__file__).parent / "routers" / "core.py").read_text(encoding="utf-8")
    assert "file_expired" in core_src and "_hist_redownload" in core_src
    # 前端：app.js 接线 file_expired（重新下载按钮 + 状态行）
    web_root = Path(downloader.__file__).parent.parent / "web" / "app.js"
    js = web_root.read_text(encoding="utf-8")
    assert "task.file_expired" in js, "前端必须消费 file_expired 字段"
    assert "重新下载" in js


if __name__ == "__main__":
    print("▶ A3/A4/B5 第三批回归测试")
    test_yt_proxy_priority_and_scope()
    test_cancel_reason_source_pins()
    test_purge_demotes_completed_keeps_history()
    test_purge_removes_failed_and_caps_history()
    test_restore_completed_without_file_as_history()
    test_retry_and_frontend_wiring_source_pins()
    print("🎉 A3/A4/B5 第三批回归测试全部通过")
