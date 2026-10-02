"""B2 任务落盘持久化回归测试（2026-10-02）。

TaskStore 状态写 tasks_state.json；重启后：
- 未完成任务 → failed + resumable（分片在则 True），TTL 重置；
- completed 且成品文件仍在 → 原样恢复；文件没了 → 恢复为 file_expired 历史条目（B5）；
- remove 同步删状态；failed 无分片 → 恢复原状态、resumable=False。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tasks as tasks_mod  # noqa: E402


def _new_store(root: Path) -> tasks_mod.TaskStore:
    return tasks_mod.TaskStore(root)


def test_state_written_on_create_and_update():
    root = Path(tempfile.mkdtemp())
    st = _new_store(root)
    t = st.create(url="https://example.com/v", title="测试视频", platform="B站",
                  quality="1080P", cookie="ck=1")
    assert (root / tasks_mod.STATE_FILENAME).exists(), "create 后必须已有状态文件"
    st.update(t.id, status="downloading", progress=55.0, downloaded_bytes=1024)
    st.flush()
    row = json.loads((root / tasks_mod.STATE_FILENAME).read_text(encoding="utf-8"))["tasks"][0]
    assert row["status"] == "downloading" and row["progress"] == 55.0, row
    assert row["cookie"] == "ck=1", "续传参数（cookie）必须落盘"


def test_interrupted_task_restored_as_resumable_failed():
    root = Path(tempfile.mkdtemp())
    st = _new_store(root)
    t = st.create(url="https://example.com/v", title="x", platform="p", quality="best")
    st.update(t.id, status="downloading", progress=40.0)
    st.flush()
    (root / t.id / "video.mp4.part").write_bytes(b"x")   # 模拟磁盘分片
    st2 = _new_store(root)                                # 模拟重启
    t2 = st2.get(t.id)
    assert t2 is not None, "下载中的任务重启后必须恢复"
    assert t2.status == "failed" and t2.resumable, (t2.status, t2.resumable)
    assert "中断" in t2.error


def test_completed_restored_only_if_file_exists():
    root = Path(tempfile.mkdtemp())
    st = _new_store(root)
    t = st.create(url="https://example.com/v", title="x", platform="p", quality="best")
    st.update(t.id, status="completed", filename="video.mp4",
              filepath=str(root / t.id / "video.mp4"))
    (root / t.id / "video.mp4").write_bytes(b"ok")
    st.flush()
    t3 = _new_store(root).get(t.id)
    assert t3 is not None and t3.status == "completed" and t3.filepath.exists()
    os.remove(root / t.id / "video.mp4")                  # 成品被 TTL 清掉
    st.flush()
    # B5（2026-10-02）：成品缺失的 completed 不再丢弃——恢复为历史条目
    # （file_expired=True、filepath=None、保留元数据，可整条重新下载）
    t4 = _new_store(root).get(t.id)
    assert t4 is not None and t4.status == "completed", "B5: 应恢复为历史条目"
    assert t4.file_expired and t4.filepath is None and t4.filename == "video.mp4"


def test_remove_drops_state_and_failed_without_partial():
    root = Path(tempfile.mkdtemp())
    st = _new_store(root)
    t = st.create(url="https://example.com/a", title="a", platform="p", quality="best")
    st.update(t.id, status="completed", filename="a.mp4", filepath=str(root / t.id / "a.mp4"))
    (root / t.id / "a.mp4").write_bytes(b"ok")
    st.flush()
    st.remove(t.id)
    ids = [x["id"] for x in json.loads(
        (root / tasks_mod.STATE_FILENAME).read_text(encoding="utf-8"))["tasks"]]
    assert t.id not in ids, "remove 必须同步从状态文件删除"
    t7 = st.create(url="https://example.com/w", title="w", platform="p", quality="best")
    st.update(t7.id, status="failed", error="网络错误")
    st.flush()
    t5 = _new_store(root).get(t7.id)
    assert t5 is not None and t5.status == "failed" and not t5.resumable


if __name__ == "__main__":
    print("▶ 任务落盘持久化回归测试（B2）")
    test_state_written_on_create_and_update()
    test_interrupted_task_restored_as_resumable_failed()
    test_completed_restored_only_if_file_exists()
    test_remove_drops_state_and_failed_without_partial()
    print("🎉 任务落盘持久化回归测试全部通过")
