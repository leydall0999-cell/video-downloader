#!/usr/bin/env python3
"""扫码分享「我的分享 / 删除 / 有效期」离线回归测试（2026-09-21 新增）。

背景
----
`server/routers/share.py` 原先只有「选文件 → 上传 → 出二维码」一次性流程：
  * 历史是前端 `shState` 的**纯内存**对象 → 切走/重开 App 全丢，已发出的链接与
    二维码再也找不回（用户实测确认）；
  * 节点侧早有 `DELETE /api/share/<sid>` 与 `X-Expire`，桌面端一个都没接上
    → 发给别人的东西**既撤不回也删不掉**，只能登服务器手删。

本次补齐三样，本文件锁住它们的行为：

  _node_roots            通道根推导（direct 只能上传，删除/探活必须用源站根）
  _norm_expire           有效期夹取（0 = 永久，上限 366 天）
  _hist_*                历史落盘（去重 / 新记录在前 / 上限裁剪 / 坏文件容错）
  share_history          「我的分享」列表 + 过期判定 + probe 探活
  share_history_delete   删除（**只允许删本机历史里的 sid**，否则成为任意删除代理）
  _run_upload            上传必须把 X-Expire 透传给节点，成功后落历史

设计约束
--------
* **绝不联网**：`_node_req` / `_http_post_stream` 一律打桩。
* **绝不写用户真实家目录**：`HIST_PATH` 指向系统临时目录。
* **绝不读用户真实 share.json**：`_load_conf` 打桩成固定字典。

运行：
    cd server && python tests/test_share_history.py
    cd server && python -m pytest tests/test_share_history.py -v
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from routers import share as sh  # noqa: E402

FAKE_CONF = {"base": "https://share.hanyuxz.top",
             "token": "test-token",
             "direct": "http://8.138.223.3:8888/su"}


@contextlib.contextmanager
def _patch(**attrs):
    """临时替换模块级属性，退出时还原。"""
    old = {k: getattr(sh, k) for k in attrs}
    try:
        for k, v in attrs.items():
            setattr(sh, k, v)
        yield
    finally:
        for k, v in old.items():
            setattr(sh, k, v)


@contextlib.contextmanager
def temp_hist():
    """把历史文件指到系统临时目录（绝不碰真实家目录）。"""
    d = tempfile.mkdtemp(prefix="vdl_share_hist_")
    try:
        with _patch(HIST_PATH=Path(d) / "share_history.json") as _:
            yield sh.HIST_PATH
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- 通道根

def test_node_roots():
    """base 在前、direct 去掉通道后缀、空值剔除、重复合并。"""
    assert sh._node_roots(FAKE_CONF) == ["https://share.hanyuxz.top",
                                         "http://8.138.223.3:8888"], sh._node_roots(FAKE_CONF)

    c = dict(FAKE_CONF, direct="http://1.2.3.4:8888/api/upload")
    assert sh._node_roots(c) == ["https://share.hanyuxz.top", "http://1.2.3.4:8888"]

    # direct 已经就是根（无后缀）时原样保留
    assert sh._node_roots(dict(FAKE_CONF, direct="http://1.2.3.4:8888"))[1] == "http://1.2.3.4:8888"
    # base 为空 → 只剩 direct 根（这正是正式域名无 DNS 时的真实形态）
    assert sh._node_roots(dict(FAKE_CONF, base="")) == ["http://8.138.223.3:8888"]
    # 两者推出同一个根 → 去重，避免对同一地址重复发两次请求
    assert sh._node_roots({"base": "http://1.2.3.4:8888",
                           "direct": "http://1.2.3.4:8888/su"}) == ["http://1.2.3.4:8888"]


# ---------------------------------------------------------------- 有效期

def test_norm_expire():
    assert sh._norm_expire(None) == 0
    assert sh._norm_expire("") == 0
    assert sh._norm_expire("abc") == 0
    assert sh._norm_expire(-5) == 0
    assert sh._norm_expire(0) == 0
    assert sh._norm_expire(86400) == 86400
    assert sh._norm_expire("2592000") == 2592000
    assert sh._norm_expire(10 ** 9) == sh.EXPIRE_MAX
    assert set(sh.EXPIRE_CHOICES) == {0, 86400, 7 * 86400, 30 * 86400}


# ---------------------------------------------------------------- 历史文件

def test_hist_add_dedupe_and_order():
    with temp_hist() as hp:
        sh._hist_add({"sid": "a", "name": "a.png"})
        sh._hist_add({"sid": "b", "name": "b.png"})
        sh._hist_add({"sid": "a", "name": "a-new.png"})   # 同 sid 再来 → 提到最前并覆盖
        items = sh._hist_load()
        assert [x["sid"] for x in items] == ["a", "b"], items
        assert items[0]["name"] == "a-new.png"
        assert hp.is_file()
        # 原子写：不得留下固定名 .tmp 残骸
        assert not hp.with_name(hp.name + ".tmp").exists()
        # 没有 sid 的脏记录不入库
        sh._hist_add({"name": "no-sid.png"})
        assert len(sh._hist_load()) == 2


def test_hist_max_cap():
    with temp_hist():
        for i in range(sh.HIST_MAX + 10):
            sh._hist_add({"sid": "s%04d" % i, "name": "n%d" % i})
        items = sh._hist_load()
        assert len(items) == sh.HIST_MAX, len(items)
        assert items[0]["sid"] == "s%04d" % (sh.HIST_MAX + 9)   # 最新的在最前


def test_hist_corrupt_file_tolerated():
    """历史文件损坏不得影响上传主流程：读回空列表，且能继续写。"""
    with temp_hist() as hp:
        hp.parent.mkdir(parents=True, exist_ok=True)
        hp.write_text("{ this is not json", encoding="utf-8")
        assert sh._hist_load() == []
        sh._hist_add({"sid": "ok1", "name": "ok.png"})
        assert [x["sid"] for x in sh._hist_load()] == ["ok1"]


# ---------------------------------------------------------------- 列表端点

def test_history_endpoint_expired_flag():
    with temp_hist():
        now = int(time.time())
        sh._hist_add({"sid": "live", "name": "live.png", "size": 10, "url": "u1",
                      "time": now, "expire_at": now + 3600})
        sh._hist_add({"sid": "dead", "name": "dead.png", "size": 20, "url": "u2",
                      "time": now, "expire_at": now - 10})
        sh._hist_add({"sid": "forever", "name": "f.png", "size": 30, "url": "u3",
                      "time": now, "expire_at": 0})
        d = sh.share_history(probe=0)
        assert d["ok"] is True and d["now"] >= now
        by = {x["sid"]: x for x in d["items"]}
        assert by["live"]["expired"] is False
        assert by["dead"]["expired"] is True
        assert by["forever"]["expired"] is False        # 0 = 永久，永不判过期
        # 不探活时不应有 alive 字段（避免前端误当成「已知在线」）
        assert all("alive" not in x for x in d["items"])


def test_history_endpoint_probe():
    with temp_hist():
        sh._hist_add({"sid": "on", "name": "on.png"})
        sh._hist_add({"sid": "off", "name": "off.png"})
        sh._hist_add({"sid": "unknown", "name": "unk.png"})
        cmap = {"on": True, "off": False, "unknown": None}
        with _patch(_node_alive=lambda sid: cmap[sid]):
            d = sh.share_history(probe=1)
        got = {x["sid"]: x.get("alive") for x in d["items"]}
        assert got == {"on": True, "off": False, "unknown": None}, got


def test_history_endpoint_limit():
    with temp_hist():
        for i in range(30):
            sh._hist_add({"sid": "s%02d" % i, "name": "n"})
        assert len(sh.share_history(limit=5)["items"]) == 5
        assert len(sh.share_history(limit=0)["items"]) == 1     # 下限夹到 1，不得返回全量
        assert len(sh.share_history(limit=9999)["items"]) == 30  # 上限夹到 HIST_MAX


# ---------------------------------------------------------------- 删除

def test_delete_rejects_sid_not_in_history():
    """🔴 白名单：历史里没有的 sid 一律拒绝，否则端点成了任意删除代理。"""
    calls = []

    def boom(sid):
        calls.append(sid)
        return True, "should-not-be-called"

    with temp_hist(), _patch(_node_delete=boom):
        sh._hist_add({"sid": "mine", "name": "mine.png"})
        r = sh.share_history_delete("someone-elses")
        assert getattr(r, "status_code", None) == 404, r
        assert json.loads(r.body)["error"] == "not_in_history"
        assert calls == [], "不该对未授权 sid 发起节点删除"
        assert [x["sid"] for x in sh._hist_load()] == ["mine"]


def test_delete_ok_removes_record():
    with temp_hist():
        sh._hist_add({"sid": "keep", "name": "keep.png"})
        sh._hist_add({"sid": "drop", "name": "drop.png"})
        with _patch(_node_delete=lambda sid: (True, '{"ok": true}')):
            r = sh.share_history_delete("drop")
        assert isinstance(r, dict) and r["ok"] is True and r["sid"] == "drop"
        assert [x["sid"] for x in sh._hist_load()] == ["keep"]


def test_delete_node_failure_keeps_record():
    """节点删失败时**不能**摘掉本地记录——否则用户以为已经删掉了，其实还挂在公网。"""
    with temp_hist():
        sh._hist_add({"sid": "stuck", "name": "stuck.png"})
        with _patch(_node_delete=lambda sid: (False, "direct: connection refused")):
            r = sh.share_history_delete("stuck")
        assert getattr(r, "status_code", None) == 502, r
        body = json.loads(r.body)
        assert body["error"] == "node_delete_failed"
        assert "connection refused" in body["detail"]
        assert [x["sid"] for x in sh._hist_load()] == ["stuck"], "失败必须保留记录"


def test_node_delete_treats_404_as_success():
    """节点上本来就没有（404）＝ 目的已达成，不该报错卡住用户。"""
    def fake_req(root, method, path, headers, timeout=20):
        assert method == "DELETE" and path == "/api/share/abc"
        assert headers.get("X-Auth") == "test-token"
        return 404, b'{"ok": false, "error": "not_found"}'

    with _patch(_load_conf=lambda: dict(FAKE_CONF), _node_req=fake_req):
        ok, _ = sh._node_delete("abc")
        assert ok is True


def test_node_delete_reports_403_and_tries_next_root():
    """403 视为失败并继续试下一条通道；两条都失败才算失败。"""
    seen = []

    def fake_req(root, method, path, headers, timeout=20):
        seen.append(root)
        return 403, b'{"ok": false, "error": "bad_token"}'

    with _patch(_load_conf=lambda: dict(FAKE_CONF), _node_req=fake_req):
        ok, detail = sh._node_delete("abc")
    assert ok is False
    assert len(seen) == 2, seen                      # base + direct 根都试过
    assert "403" in detail and "bad_token" in detail


def test_node_delete_falls_back_to_direct_root():
    """base 连不上时，必须能靠 direct 推导出的源站根把删除做成。"""
    seen = []

    def fake_req(root, method, path, headers, timeout=20):
        seen.append(root)
        if root.startswith("https://"):
            raise OSError("name resolution failed")
        return 200, b'{"ok": true, "sid": "abc"}'

    with _patch(_load_conf=lambda: dict(FAKE_CONF), _node_req=fake_req):
        ok, _ = sh._node_delete("abc")
    assert ok is True
    assert seen == ["https://share.hanyuxz.top", "http://8.138.223.3:8888"], seen


# ---------------------------------------------------------------- 上传链路

def test_upload_passes_expire_and_records_history():
    """上传必须把 X-Expire 透传给节点，并在成功后落一条历史（含真实 expire_at）。"""
    captured = {}

    def fake_post(url, headers, reader, total, tid):
        captured["url"] = url
        captured["headers"] = dict(headers)
        captured["total"] = total
        return 200, json.dumps({"ok": True, "sid": "newsid01",
                                "url": "https://share.hanyuxz.top/s/newsid01",
                                "kind": "image", "expire_at": 0}).encode()

    with temp_hist(), _patch(_http_post_stream=fake_post):
        tid = sh._new_task("photo.png", 1234)
        sh._run_upload(tid, lambda: (lambda: b""), 1234, "photo.png",
                       dict(FAKE_CONF), expire=7 * 86400)

        assert captured["headers"]["X-Expire"] == str(7 * 86400), captured["headers"]
        assert captured["headers"]["X-Filename"] == "photo.png"     # 中文/特殊名同理走 quote
        task = sh._TASKS[tid]
        assert task["status"] == "done", task
        assert task["sid"] == "newsid01"

        items = sh._hist_load()
        assert len(items) == 1, items
        rec = items[0]
        assert rec["sid"] == "newsid01"
        assert rec["name"] == "photo.png"
        assert rec["size"] == 1234
        assert rec["url"].endswith("/s/newsid01")
        assert rec["channel"] == "base"
        # 节点没回 expire_at 时用「上传时刻 + 所选有效期」兜底
        assert abs(rec["expire_at"] - (rec["time"] + 7 * 86400)) <= 2, rec


def test_upload_forever_expire_and_node_expire_wins():
    """expire=0（永久）→ expire_at 应为 0；节点回传的 expire_at 优先于本地推算。"""
    def fake_post(url, headers, reader, total, tid):
        return 200, json.dumps({"ok": True, "sid": "s2", "url": "u",
                                "expire_at": 1893456000}).encode()

    with temp_hist(), _patch(_http_post_stream=fake_post):
        tid = sh._new_task("x.png", 10)
        sh._run_upload(tid, lambda: (lambda: b""), 10, "x.png", dict(FAKE_CONF), expire=0)
        rec = sh._hist_load()[0]
        assert rec["expire_at"] == 1893456000

    with temp_hist(), _patch(_http_post_stream=lambda *a, **k: (
            200, json.dumps({"ok": True, "sid": "s3", "url": "u"}).encode())):
        tid = sh._new_task("y.png", 10)
        sh._run_upload(tid, lambda: (lambda: b""), 10, "y.png", dict(FAKE_CONF), expire=0)
        assert sh._hist_load()[0]["expire_at"] == 0


def test_upload_failure_writes_no_history():
    """上传失败绝不能留下「已分享」的假记录。"""
    with temp_hist(), _patch(_http_post_stream=lambda *a, **k: (
            500, b"boom")):
        tid = sh._new_task("z.png", 10)
        sh._run_upload(tid, lambda: (lambda: b""), 10, "z.png", dict(FAKE_CONF))
        assert sh._TASKS[tid]["status"] == "failed"
        assert sh._hist_load() == []


def test_upload_path_endpoint_returns_expire():
    """桌面端入口必须把 expire 收下并回显（前端据此提示有效期）。"""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 100)
        p = f.name
    captured = {}
    try:
        with temp_hist(), _patch(_start_upload=lambda tid, mk, total, name, expire=0:
                                 captured.update(expire=expire,
                                                 name=name, total=total)):
            d = sh.share_upload_path({"path": p, "expire": 30 * 86400})
            assert d["ok"] is True and d["expire"] == 30 * 86400
            assert captured["expire"] == 30 * 86400
            assert captured["total"] == 108
            # 非法值被夹回 0，不得原样透传
            d2 = sh.share_upload_path({"path": p, "expire": "not-a-number"})
            assert d2["expire"] == 0 and captured["expire"] == 0
    finally:
        os.unlink(p)


def test_upload_path_rejects_missing_file():
    with temp_hist():
        r = sh.share_upload_path({"path": "/definitely/not/here.png"})
        assert getattr(r, "status_code", None) == 400
        assert json.loads(r.body)["error"] == "file_not_found"


# ---------------------------------------------------------------- runner

def _main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    ok = fail = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            fail += 1
            import traceback
            print("❌ %s\n%s" % (name, traceback.format_exc()))
        else:
            ok += 1
            print("✓ %s" % name)
    print("\n通过 %d / 失败 %d（共 %d）" % (ok, fail, len(tests)))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(_main())
