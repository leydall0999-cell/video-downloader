"""解说路由层守卫与路径推导离线回归测试（2026-09-10 新增）。

背景：解说管线的算法侧（渲染/片头片尾/LLM/音量）在独立仓库 commentary-pipeline
已有完整 pytest 覆盖；但 **app 侧路由层 `routers/commentary.py` 一直无测试**。
这一层承载两类风险，且都不属于「算错」而是「放行不该放行的输入」：

  1. /api/commentary/audio-preview 直接接受前端传来的**任意绝对路径**并回传文件流
     —— 若路径守卫失守即是任意文件读取（把 ~/.ssh/id_rsa 当音频读出来）。
  2. BGM 试听 sidecar 路径推导（`<成片>.previews/<kind>.mp3`）：推错则试听 404，
     用户以为渲染失败而重渲，浪费算力。

覆盖：
  _bgm_preview_mp3_path       sidecar 路径推导与存在性判定
  commentary_audio_preview    本地音乐试听的路径守卫（绝对路径/家目录/扩展名白名单）
  commentary_bgm_preview      BGM 风格 kind 白名单
  commentary_config_get       配置读取端点基本可用性

设计约束：**本测试绝不向用户真实家目录写入任何文件**。
需要「家目录内文件」的用例一律通过 `fake_home()` 把 `Path.home` 指向系统临时目录，
临时文件因此落在 /var/folders（系统自行回收）。这是刻意的——早期版本直接在真实
家目录建 `.vdl_cmtest_*` 目录，一旦清理失败（如被沙盒删除策略拦截，而
`ignore_errors=True` 会静默吞掉）就会把垃圾永久留在用户家目录。

运行：
    cd server && python tests/test_commentary_routes.py
    cd server && python -m pytest tests/test_commentary_routes.py -v
"""
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402  先完成 app 初始化，避免 routers 循环导入
from routers import commentary as cm  # noqa: E402
from fastapi import HTTPException  # noqa: E402


@contextmanager
def fake_home():
    """把 pathlib.Path.home 临时指向系统临时目录，供「家目录内文件」用例使用。

    返回的路径已 resolve()——macOS 上 /var 是 /private/var 的软链，若这里给的是
    未解析路径，而被测函数内部会把传入路径 resolve()，二者做 relative_to 时会
    因前缀不同而误判为「不在家目录下」。
    """
    import pathlib
    fake = Path(tempfile.mkdtemp(prefix="vdl_fakehome_")).resolve()
    orig = pathlib.Path.home
    pathlib.Path.home = staticmethod(lambda: fake)
    try:
        yield fake
    finally:
        pathlib.Path.home = orig
        shutil.rmtree(fake, ignore_errors=True)


def _expect_400(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except HTTPException as e:
        assert e.status_code == 400, f"预期 400，实际 {e.status_code}: {e.detail}"
        return e
    raise AssertionError(f"应被拒绝但放行了: {fn.__name__}{args}")


# ---------------------------------------------------------------- sidecar 路径推导

def test_bgm_preview_path_none_without_sidecar():
    """成片存在但没有 .previews 目录 → 返回 None（路由据此提示重新渲染）。"""
    tmp = Path(tempfile.mkdtemp(prefix="vdl_cm_"))
    try:
        mp4 = tmp / "final.mp4"
        mp4.write_bytes(b"stub")
        assert cm._bgm_preview_mp3_path(mp4, "soft") is None
        print("✅ 缺 sidecar 返回 None，前端可提示「请重新渲染」而非白屏")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_bgm_preview_path_resolves_sidecar():
    """有 sidecar 时返回 `<stem>.previews/<kind>.mp3`（成片与试听一一对应）。"""
    tmp = Path(tempfile.mkdtemp(prefix="vdl_cm_"))
    try:
        mp4 = tmp / "final.mp4"
        mp4.write_bytes(b"stub")
        pv = tmp / "final.previews"
        pv.mkdir()
        (pv / "soft.mp3").write_bytes(b"ID3")
        got = cm._bgm_preview_mp3_path(mp4, "soft")
        assert got is not None and got.name == "soft.mp3"
        assert got.parent.name == "final.previews", f"目录名不符: {got.parent.name}"
        print("✅ sidecar 路径推导正确（final.mp4 → final.previews/soft.mp3）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_bgm_preview_path_missing_kind():
    """只落了 soft，查 epic 应返回 None——三种风格各自独立，不可互相顶替。"""
    tmp = Path(tempfile.mkdtemp(prefix="vdl_cm_"))
    try:
        mp4 = tmp / "final.mp4"
        mp4.write_bytes(b"stub")
        pv = tmp / "final.previews"
        pv.mkdir()
        (pv / "soft.mp3").write_bytes(b"ID3")
        assert cm._bgm_preview_mp3_path(mp4, "epic") is None
        assert cm._bgm_preview_mp3_path(mp4, "light") is None
        print("✅ 缺失的风格返回 None，不会串用其它风格的试听")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_bgm_preview_path_handles_no_suffix():
    """无扩展名的成片路径不应抛异常（推导退回本身 + .previews）。"""
    tmp = Path(tempfile.mkdtemp(prefix="vdl_cm_"))
    try:
        nosuf = tmp / "nosuffix"
        nosuf.write_bytes(b"stub")
        assert cm._bgm_preview_mp3_path(nosuf, "soft") is None
        print("✅ 无扩展名路径安全返回 None（不抛异常、不误判）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 本地音乐试听守卫

def test_audio_preview_rejects_relative_or_empty():
    """相对路径 / 空路径 → 400（必须绝对路径，避免以服务进程 cwd 解析）。"""
    _expect_400(cm.commentary_audio_preview, "")
    _expect_400(cm.commentary_audio_preview, "a.mp3")
    _expect_400(cm.commentary_audio_preview, "../etc/hosts")
    print("✅ 非绝对路径被拒绝，不会以进程工作目录为基准解析文件")


def test_audio_preview_rejects_outside_home():
    """家目录外的绝对路径 → 400。这是任意文件读取的闸门。"""
    e = _expect_400(cm.commentary_audio_preview, "/etc/hosts")
    assert "家目录" in e.detail, f"拒绝理由应指明家目录限制，实际: {e.detail}"
    _expect_400(cm.commentary_audio_preview, "/etc/evil.mp3")
    print("✅ 家目录外路径被拒绝（含伪装成 .mp3 的情况），闸门有效")


def test_audio_preview_accepts_audio_in_home():
    """家目录下真实音频通过；扩展名大小写不敏感。"""
    with fake_home() as home:
        ok = home / "bgm.mp3"
        ok.write_bytes(b"ID3")
        resp = cm.commentary_audio_preview(str(ok))
        assert Path(resp.path).name == "bgm.mp3"

        up = home / "UP.MP3"
        up.write_bytes(b"ID3")
        assert Path(cm.commentary_audio_preview(str(up)).path).name == "UP.MP3"
    print("✅ 家目录内音频通过，扩展名大小写不敏感")


def test_audio_preview_rejects_non_audio_extension():
    """扩展名白名单：非音频文件即使在家目录下也拒绝。

    注意校验顺序为「路径 → 存在 → 扩展名」，故此处必须先真实建出文件，
    否则会先命中 404（文件不存在）而非 400（扩展名不允许）。
    """
    with fake_home() as home:
        txt = home / "notes.txt"
        txt.write_bytes(b"x")
        e = _expect_400(cm.commentary_audio_preview, str(txt))
        assert ".txt" in e.detail, f"应指出被拒的扩展名，实际: {e.detail}"

        noext = home / "noext"
        noext.write_bytes(b"x")
        _expect_400(cm.commentary_audio_preview, str(noext))
    print("✅ 非音频扩展名被拒绝，不会把任意文件当音频流回传")


def test_audio_preview_missing_file_is_404():
    """家目录下的合法路径但文件不存在 → 404（与「不允许」区分）。"""
    with fake_home() as home:
        try:
            cm.commentary_audio_preview(str(home / "missing.mp3"))
            raise AssertionError("应返回 404")
        except HTTPException as e:
            assert e.status_code == 404, f"预期 404，实际 {e.status_code}"
    print("✅ 文件不存在返回 404（与 400「不允许」语义区分，便于前端提示）")


# ---------------------------------------------------------------- BGM kind 白名单

def test_bgm_preview_rejects_unknown_kind():
    """kind 只允许 soft/light/epic；其它值（含大小写变体）→ 400。

    若不校验，kind 会被拼进 sidecar 文件名，成为路径穿越的入口。
    """
    for bad in ["bad", "SOFT", "soft/../x", "", "epic.mp3"]:
        e = _expect_400(cm.commentary_bgm_preview, "deadbeef0000", bad)
        assert "soft/light/epic" in e.detail, f"应说明合法取值，实际: {e.detail}"
    print("✅ 非法 kind 被拒绝（白名单校验先于 cid），杜绝拼路径穿越")


def test_bgm_preview_legal_kind_proceeds_to_cid_check():
    """合法 kind 应继续走 cid 校验——证明拒绝并非「一律拒绝」。"""
    try:
        cm.commentary_bgm_preview("deadbeef0000", "soft")
        raise AssertionError("假 cid 应被拒绝")
    except HTTPException as e:
        assert e.status_code == 400
        assert "kind" not in e.detail, f"合法 kind 不应报 kind 错误: {e.detail}"
    print("✅ 合法 kind 通过白名单并进入 cid 校验（拒绝行为有区分度）")


# ---------------------------------------------------------------- 配置端点

def test_commentary_config_get_returns_dict():
    """配置读取端点返回 dict（前端初始化依赖它，不能 500）。"""
    cfg = cm.commentary_config_get()
    assert isinstance(cfg, dict)
    print("✅ 解说配置读取端点正常返回 dict")


if __name__ == "__main__":
    test_bgm_preview_path_none_without_sidecar()
    test_bgm_preview_path_resolves_sidecar()
    test_bgm_preview_path_missing_kind()
    test_bgm_preview_path_handles_no_suffix()

    test_audio_preview_rejects_relative_or_empty()
    test_audio_preview_rejects_outside_home()
    test_audio_preview_accepts_audio_in_home()
    test_audio_preview_rejects_non_audio_extension()
    test_audio_preview_missing_file_is_404()

    test_bgm_preview_rejects_unknown_kind()
    test_bgm_preview_legal_kind_proceeds_to_cid_check()

    test_commentary_config_get_returns_dict()

    print("\n🎉 解说路由层测试全部通过（12 项）")
