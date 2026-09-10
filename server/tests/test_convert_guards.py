"""转换链路守卫逻辑离线回归测试（2026-09-10 新增）。

背景：「转换（图片/音乐/视频）」是核心链路中最后一块无测试保护的区域。
本次不测转码本身（依赖 ffmpeg 与真实媒体），而是**测住转换入口的三道守卫**——
它们一旦失守，后果比转码失败严重得多：

  1. 分片上传的 upload_id 正则：非法 id 可越出 `up_{id}.p*` 命名空间，
     读到/删掉其它上传会话甚至其它用户的临时分片。
  2. 分片命名与排序：合并顺序一旦错乱，产出的是「内容乱序的坏视频」，
     全程不报错，用户播放时才发现。
  3. 本地路径白名单：这是**任意文件读取**的唯一闸门（/api/convert/local
     直接接受前端传来的绝对路径）。

覆盖：
  _UPLOAD_ID_RE          上传 id 正则边界（长度/字符集/穿越字符）
  _upload_parts          分片命名零填充契约与排序正确性、会话隔离
  _resolve_safe_local_path  本地路径安全（白名单、目录穿越逃逸、非普通文件）

设计约束：本测试不向用户家目录写入任何文件。路径「通过」用例复用已存在的
测试文件自身，「逃逸」用例用目录穿越构造，二者都无需创建临时文件。

运行：
    cd server && python tests/test_convert_guards.py
    cd server && python -m pytest tests/test_convert_guards.py -v
"""
import os
import sys
from pathlib import Path

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402  先完成 app 初始化，避免 routers 循环导入
from routers import convert as cv  # noqa: E402
from fastapi import HTTPException  # noqa: E402


# ---------------------------------------------------------------- 上传 id 正则

def test_upload_id_regex_accepts_legal():
    """合法 id：8~64 位 [A-Za-z0-9_-]。"""
    for good in ["abc12345", "A" * 64, "aB3_-xyz", "0" * 8]:
        assert cv._UPLOAD_ID_RE.match(good), f"应接受合法 id: {good!r}"
    print("✅ 合法 upload_id 被接受（8~64 位字母数字下划线连字符）")


def test_upload_id_regex_rejects_length():
    """长度越界必须拒绝：过短会与其它 id 前缀冲突，过长是异常输入。"""
    assert not cv._UPLOAD_ID_RE.match("a" * 7), "7 位应拒绝"
    assert not cv._UPLOAD_ID_RE.match("a" * 65), "65 位应拒绝"
    assert not cv._UPLOAD_ID_RE.match("")
    print("✅ 长度越界的 upload_id 被拒绝，不会与其它会话前缀混淆")


def test_upload_id_regex_rejects_path_chars():
    """路径/通配字符必须拒绝——它们能让 glob 命中别的会话分片。

    `_upload_parts` 直接把 id 拼进 glob 模式 `up_{id}.p*`，若 id 含 `.`、`/`、
    `*` 等字符，就能越出本会话命名空间（轻则读到别人的分片，重则覆盖删除）。
    """
    for bad in ["abc.1234", "abc/1234", "abc*1234", "abc 1234", "../etc/x", "abc\\1234"]:
        assert not cv._UPLOAD_ID_RE.match(bad), f"应拒绝含特殊字符的 id: {bad!r}"
    assert not cv._UPLOAD_ID_RE.match("中文中文中文中文"), "非 ASCII 应拒绝"
    print("✅ 含路径/通配字符的 id 被拒绝，分片 glob 不会越出本会话")


# ---------------------------------------------------------------- 分片命名与排序

def _cleanup_parts(upload_id: str):
    for p in cv._upload_parts(upload_id):
        try:
            p.unlink()
        except OSError:
            pass


def test_upload_parts_sorted_numeric_beyond_nine():
    """关键契约：分片名零填充 `p{index:04d}`，故字符串排序 = 数字排序。

    回归保护——若有人把 f"p{index}" 改成非零填充，10 片以上将按
    p1, p10, p11, …, p2 顺序合并（内容乱序的坏文件，且全程无报错）。
    32MB/片意味着 320MB 以上的视频必然触发，属高概率路径。
    """
    uid = "testguard0001"
    _cleanup_parts(uid)
    # 故意乱序写入，覆盖跨位数（9 -> 10）的进位点
    for i in [15, 0, 10, 2, 1, 9]:
        (server_app.UPLOAD_TMP / f"up_{uid}.p{i:04d}").write_bytes(bytes([i % 256]))
    try:
        names = [p.name for p in cv._upload_parts(uid)]
        assert names == [f"up_{uid}.p{i:04d}" for i in [0, 1, 2, 9, 10, 15]], f"排序错误: {names}"
        print("✅ 分片按数字序合并（含 9→10 进位点），大文件不产生乱序坏视频")
    finally:
        _cleanup_parts(uid)


def test_upload_parts_isolated_between_sessions():
    """不同 upload_id 的分片互不干扰——避免并发上传串味。"""
    uid_a, uid_b = "testguardAAA1", "testguardBBB1"
    _cleanup_parts(uid_a)
    _cleanup_parts(uid_b)
    for i in range(3):
        (server_app.UPLOAD_TMP / f"up_{uid_a}.p{i:04d}").write_bytes(b"a")
    for i in range(5):
        (server_app.UPLOAD_TMP / f"up_{uid_b}.p{i:04d}").write_bytes(b"b")
    try:
        assert len(cv._upload_parts(uid_a)) == 3
        assert len(cv._upload_parts(uid_b)) == 5
        assert all(uid_a in p.name for p in cv._upload_parts(uid_a))
        print("✅ 并发上传会话互相隔离，分片不会串到另一个任务")
    finally:
        _cleanup_parts(uid_a)
        _cleanup_parts(uid_b)


def test_upload_parts_empty_when_nothing_uploaded():
    """无分片时返回空列表（finish 据此报「分片不完整」）。"""
    assert cv._upload_parts("testguardNone1") == []
    print("✅ 无分片返回空列表，finish 可正确判定上传不完整")


# ---------------------------------------------------------------- 本地路径安全

def test_local_path_accepts_real_file_under_home():
    """家目录下的真实文件应通过，并返回解析后的绝对路径。"""
    got = cv._resolve_safe_local_path(__file__)
    assert got.is_absolute() and got.is_file()
    print("✅ 家目录下真实文件通过校验（/api/convert/local 正常可用）")


def test_local_path_rejects_missing_and_directory():
    """不存在 / 目录（非普通文件）→ 400，避免把目录当输入喂给 ffmpeg。"""
    for bad in ["/Users/suixindelang/__vdl_no_such_file__.mp4", str(Path.home())]:
        try:
            cv._resolve_safe_local_path(bad)
            raise AssertionError(f"应拒绝: {bad}")
        except HTTPException as e:
            assert e.status_code == 400
    print("✅ 不存在的路径与目录被拒绝（400），不会把目录当文件转码")


def test_local_path_rejects_outside_home():
    """白名单外的系统路径必须拒绝——这是任意文件读取的闸门。"""
    try:
        cv._resolve_safe_local_path("/etc/hosts")
        raise AssertionError("应拒绝 /etc/hosts")
    except HTTPException as e:
        assert e.status_code == 400
        assert "不在用户目录下" in e.detail, \
            f"拒绝理由应明确指出白名单，实际: {e.detail}"
    print("✅ 家目录外路径被拒绝，且错误信息如实说明原因（可排障）")


def test_local_path_rejects_traversal_escape():
    """逃逸防护：传入字符串看着合法、但 resolve 后落到家目录外 → 必须拒绝。

    用目录穿越代替真实软链来构造该场景——两者同属「解析后才暴露非法」的语义，
    但目录穿越**不需要在家目录创建任何文件**。早期版本用
    `tempfile.mkdtemp(dir=Path.home())` 建软链，一旦清理失败（如被删除策略拦截，
    而 ignore_errors=True 会静默吞掉）就会把垃圾永久留在用户家目录，故弃用。

    判定必须基于 resolve() 后的结果；若有人改成只校验原始字符串前缀，此测试报红。
    """
    escape = "../" * 12 + "etc/hosts"
    try:
        cv._resolve_safe_local_path(escape)
        raise AssertionError("应拒绝解析后落到白名单外的路径")
    except HTTPException as e:
        assert e.status_code == 400
        assert "不在用户目录下" in e.detail, f"拒绝理由应指向白名单，实际: {e.detail}"
    print("✅ 目录穿越逃逸被拦截（判定基于 resolve 后路径，非原始字符串）")


if __name__ == "__main__":
    test_upload_id_regex_accepts_legal()
    test_upload_id_regex_rejects_length()
    test_upload_id_regex_rejects_path_chars()

    test_upload_parts_sorted_numeric_beyond_nine()
    test_upload_parts_isolated_between_sessions()
    test_upload_parts_empty_when_nothing_uploaded()

    test_local_path_accepts_real_file_under_home()
    test_local_path_rejects_missing_and_directory()
    test_local_path_rejects_outside_home()
    test_local_path_rejects_traversal_escape()

    print("\n🎉 转换链路守卫测试全部通过（10 项）")
