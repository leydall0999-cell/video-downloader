#!/usr/bin/env python3
"""自更新链路的离线回归测试（2026-09-11 新增）。

背景：为什么必须补上这块
------------------------
「检查更新 → 下载 → 准备 staging → 派生助手覆盖 /Applications」这条链路
此前**零测试覆盖**，而它恰恰是全项目最容易埋雷的地方。两个真实事故：

  * `server/routers/system.py` 的 `_prepare_update()` **全量分支**用了
    `shutil.rmtree` 却没有 `import shutil` → 一调用就 NameError，被
    `except Exception: return None` 吞掉，对外只剩一句「更新准备失败」。
    更隐蔽的是**增量分支完全不碰 shutil**，所以增量一直是好的，
    把全量分支的致命问题掩盖了数月 —— 这就是静态守卫之外还需要行为测试的原因。
  * 增量套用后主二进制丢失 +x（`bspatch` 产物是 0644，`replace()` 后执行位没了），
    更新完成的应用双击直接报 `Launch failed` —— 用户更新后应用打不开。

覆盖
----
  _parse_ver               版本号数值比较（1.0.10 应比 1.0.9 新）
  _verify_app              签名 + 版本双重校验的三个分支
  _prepare_update          全量分支：sha 不符拒绝、**校验失败必须返回 None**、
                           校验通过才返回 staging；增量只在版本精确匹配时启用
  _apply_delta             增量套用：bsdiff 后**权限保持不变**（+x 不能丢）、
                           新增文件按 manifest 的 mode 复原、del / link 分支
  发布脚本守卫              基线裁剪必须用逐段数值排序，不能退回裸 sort
  基线裁剪行为              当前版本 / 线上版本豁免裁剪、keep=0 关闭裁剪、
                           非版本号形态的目录文件不碰（2026-09-26 补）

关于基线裁剪用例为何"截取脚本原文执行"：
 此前这里只有一条静态文本守卫（检查 sort 参数字符串），证明不了保护逻辑真的生效。现改为从 publish_update.sh 原文截取第 3.7 节的真实片段喂给 bash，
 并把 HOME 重定向到临时目录（脚本用 mv 到 $HOME/.Trash 回收基线）—— 既保证
 "脚本一改测试就跟着变"，又严格遵守不向用户家目录写东西的约束。
 已做变异验证：把 VERSION/REMOTE_VER 的保护换成 `if false`，该用例立即失败。

设计约束
--------
* **不向用户家目录写任何文件**：所有临时文件都落在 `tempfile.TemporaryDirectory()`。
* **不访问网络**：把 `_http_download` 替换成本地文件复制，其余环节
  （sha256 计算、`ditto` 解压、nested 检测、权限处理）全部走真实代码。
* 依赖 macOS 自带工具 `ditto` / `codesign`；增量用例还需 `bsdiff`
  （homebrew）。缺失时该用例打印「⚠️ 跳过」而非失败，避免在非 mac 环境误报。

运行：
    cd server && python tests/test_selfupdate.py
    cd server && python -m pytest tests/test_selfupdate.py -v
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

_IS_MAC = sys.platform == "darwin"

import app as server_app  # noqa: E402  先完成 app 初始化，避免 routers 循环导入
from routers import system as su  # noqa: E402


# --------------------------------------------------------------------------- #
# 素材构造
# --------------------------------------------------------------------------- #
def _mk_min_app(root: Path, version: str) -> Path:
    """造一个最小 .app 骨架：够 ditto 解压、够读 version.txt 与主可执行位。"""
    app = root / "VideoDownloader.app"
    res = app / "Contents" / "Resources"
    res.mkdir(parents=True, exist_ok=True)
    (res / "version.txt").write_text(version, encoding="utf-8")
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    exe = macos / "VideoDownloader"
    exe.write_text("#!/bin/sh\necho vdl\n", encoding="utf-8")
    exe.chmod(0o755)
    return app


def _zip_app(src_app: Path, dest_zip: Path) -> None:
    """按发布侧 `ditto -c -k --keepParent` 的形态打包：zip 顶层层级是 VideoDownloader.app/。"""
    with zipfile.ZipFile(str(dest_zip), "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(src_app.rglob("*")):
            if p.is_dir():
                continue
            z.write(str(p), str(p.relative_to(src_app.parent)))


def _local_copy(url, dest, timeout=None):  # noqa: ARG001  替身，签名对齐 _http_download
    """替代真实下载：把 url 当本地路径直接复制，保留后续真实的 sha256/解压逻辑。"""
    shutil.copy(str(url), str(dest))


def _fake_patch_result():
    class _R:
        returncode = 0
    return _R()


# --------------------------------------------------------------------------- #
# 1. 版本号比较
# --------------------------------------------------------------------------- #
def test_parse_ver_is_numeric():
    """版本号必须按数值比较：1.0.10 比 1.0.9 新（字符串比较会判反）。"""
    assert su._parse_ver("1.0.10") > su._parse_ver("1.0.9")
    assert su._parse_ver("1.0.9") > su._parse_ver("1.0.2")
    assert su._parse_ver("2.0.0") > su._parse_ver("1.99.99")
    assert su._parse_ver("1.0") == su._parse_ver("1.0.0")
    assert su._parse_ver("v1.2.3") == su._parse_ver("1.2.3")
    print("✅ 版本号按数值比较：1.0.10 > 1.0.9 > 1.0.2")


def test_publish_script_uses_numeric_version_sort():
    """发布脚本裁剪基线时必须逐段数值排序。

    裸 `sort` 会把 `1.0.10` / `1.0.11` 排到 `1.0.2` **之前** —— 直接误删最新基线，
    下轮发布就没有可用的差分基准。这里既守卫脚本文本，也实跑该排序验证语义。
    """
    script = Path(__file__).resolve().parents[2] / "desktop" / "publish_update.sh"
    if not script.exists():
        print("⚠️ 跳过：未找到 desktop/publish_update.sh")
        return
    txt = script.read_text(encoding="utf-8")
    assert "-k1,1n -k2,2n -k3,3n" in txt, (
        "publish_update.sh 的基线裁剪必须用 sort -t. -k1,1n -k2,2n -k3,3n，"
        "否则 1.0.10/1.0.11 会被排到 1.0.2 前面并误删最新基线"
    )
    out = subprocess.run(
        ["bash", "-c", "printf '1.0.9\\n1.0.10\\n1.0.2\\n1.0.11\\n' | sort -t. -k1,1n -k2,2n -k3,3n"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert out == ["1.0.2", "1.0.9", "1.0.10", "1.0.11"], out
    print("✅ 发布脚本基线排序为数值序（1.0.10 / 1.0.11 不会被误删）")


# --------------------------------------------------------------------------- #
# 2. _verify_app：签名 + 版本双重校验
# --------------------------------------------------------------------------- #
def test_verify_app_rejects_unsigned():
    """未签名的 app 必须判不通过 —— 真实调用 codesign，不做替身。"""
    with tempfile.TemporaryDirectory() as td:
        app = _mk_min_app(Path(td), "9.9.9")
        assert su._verify_app(app, "9.9.9") is False
    print("✅ 未签名 app 被判不通过（真实调用 codesign）")


def test_verify_app_compares_version_when_signed_ok():
    """签名没问题时，版本号必须等于目标版本；不等则拒绝。

    这条闸门防的是「下载到版本号不符的包」（历史上真的发生过：线上 latest.json
    标 1.0.8，zip 里的 version.txt 却是 1.0.7）。
    """
    with tempfile.TemporaryDirectory() as td:
        app = _mk_min_app(Path(td), "1.0.9")
        with mock.patch.object(su.subprocess, "run", return_value=_fake_patch_result()):
            assert su._verify_app(app, "1.0.9") is True
            assert su._verify_app(app, "1.1.0") is False
            assert su._verify_app(app, "") is False
    print("✅ 签名通过后仍严格比对 version.txt（不符即拒绝）")


# --------------------------------------------------------------------------- #
# 3. _prepare_update 全量分支
# --------------------------------------------------------------------------- #
def test_prepare_update_rejects_sha_mismatch():
    """全量包 sha256 不符必须拒绝。"""
    if not _IS_MAC:
        print("⚠️ 跳过：全量分支依赖 macOS 的 ditto")
        return
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        work = root / "work"
        work.mkdir()
        pkg = root / "pkg.zip"
        _zip_app(_mk_min_app(root / "src", "9.9.9"), pkg)
        data = {
            "url": str(pkg),
            "sha256": "0" * 64,          # 故意错
            "from_version": "",
            "patch_url": "",
        }
        with mock.patch.object(su, "_http_download", _local_copy):
            out = su._prepare_update(data, work, root / "no.app", "9.9.9")
        assert out is None, "sha256 不符时不得返回 staging"
    print("✅ 全量包 sha256 不符 → 拒绝")


def test_prepare_update_full_requires_verify():
    """⚠️ 本次修复的核心：解压成功但校验不过时，必须返回 None。

    历史缺陷：全量分支解压完**直接返回 staging**，从不校验签名与版本号，
    等于「只要下载能解压，就敢让它去覆盖 /Applications」。
    """
    if not _IS_MAC:
        print("⚠️ 跳过：全量分支依赖 macOS 的 ditto")
        return
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        work = root / "work"
        work.mkdir()
        pkg = root / "pkg.zip"
        _zip_app(_mk_min_app(root / "src", "9.9.9"), pkg)
        data = {
            "url": str(pkg),
            "sha256": su._sha256_of(pkg),
            "from_version": "",
            "patch_url": "",
        }
        # ① 校验不通过 → 必须 None
        with mock.patch.object(su, "_http_download", _local_copy), \
                mock.patch.object(su, "_verify_app", return_value=False):
            out = su._prepare_update(data, work, root / "no.app", "9.9.9")
        assert out is None, "全量解压后校验失败必须返回 None（不许把坏包交给助手）"

        # ② 校验通过 → 返回 staging，且内容确实是目标版本
        with mock.patch.object(su, "_http_download", _local_copy), \
                mock.patch.object(su, "_verify_app", return_value=True):
            out = su._prepare_update(data, work, root / "no.app", "9.9.9")
        assert out is not None, "校验通过时应返回 staging"
        assert out == work / "VideoDownloader.app"
        assert (out / "Contents" / "Resources" / "version.txt").read_text().strip() == "9.9.9"
        assert (out / "Contents" / "MacOS" / "VideoDownloader").exists()
    print("✅ 全量分支：校验不过 → None；校验通过 → staging（含正确 version.txt）")


def test_prepare_update_delta_requires_exact_from_version():
    """增量只在「已装版本 == 清单 from_version」时启用，否则必须直接走全量。"""
    if not _IS_MAC:
        print("⚠️ 跳过：全量回退依赖 macOS 的 ditto")
        return
    if su.VERSION == "1.0.8":
        print("⚠️ 跳过：测试环境的 VERSION 恰为 1.0.8，无法构造不匹配场景")
        return
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        work = root / "work"
        work.mkdir()
        pkg = root / "pkg.zip"
        _zip_app(_mk_min_app(root / "src", "9.9.9"), pkg)
        calls = {"delta": 0}

        def _spy_delta(app, delta_zip):
            calls["delta"] += 1
            return True

        data = {
            "from_version": "1.0.8",                 # 与当前 VERSION 不符
            "patch_url": "https://example.invalid/p.delta",
            "patch_sha256": "",
            "url": str(pkg),
            "sha256": su._sha256_of(pkg),
        }
        with mock.patch.object(su, "_http_download", _local_copy), \
                mock.patch.object(su, "_apply_delta", _spy_delta), \
                mock.patch.object(su, "_verify_app", return_value=True):
            out = su._prepare_update(data, work, root / "no.app", "9.9.9")
        assert calls["delta"] == 0, "版本不匹配时不得套用增量（跨版本增量必失败）"
        assert out is not None, "不匹配时应正常回退到全量"
    print("✅ 已装版本 ≠ from_version → 不套增量，直接走全量")


# --------------------------------------------------------------------------- #
# 4. _apply_delta：权限是这条链路的命门
# --------------------------------------------------------------------------- #
def _bsdiff_bin() -> str:
    return shutil.which("bsdiff") or "/opt/homebrew/bin/bsdiff"


def test_apply_delta_bsdiff_preserves_exec_bit():
    """⚠️ 增量套用后主可执行文件必须仍带 +x。

    历史事故：`bspatch` 产物权限是 0644，直接 `replace()` 到目标会让 0755 的主二进制
    丢掉执行位 → 更新完成的应用双击报 `Launch failed`，用户直接打不开。
    """
    if not _IS_MAC:
        print("⚠️ 跳过：bspatch 为 macOS 自带工具")
        return
    bsdiff = _bsdiff_bin()
    if not os.path.exists(bsdiff):
        print("⚠️ 跳过：未找到 bsdiff（增量用例需要它生成补丁）")
        return
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        staging = root / "VideoDownloader.app"
        macos = staging / "Contents" / "MacOS"
        macos.mkdir(parents=True)
        exe = macos / "VideoDownloader"
        exe.write_bytes(b"OLD-BINARY-" + b"a" * 6000)
        exe.chmod(0o755)

        new_blob = root / "new.bin"
        new_blob.write_bytes(b"NEW-BINARY-" + b"b" * 6000)
        patch = root / "p.bin"
        subprocess.run([bsdiff, str(exe), str(new_blob), str(patch)], check=True)

        delta = root / "d.delta"
        with zipfile.ZipFile(str(delta), "w") as z:
            z.writestr("0", patch.read_bytes())
            z.writestr("manifest.json", json.dumps([
                {"op": "bsdiff", "idx": 0,
                 "path": "Contents/MacOS/VideoDownloader", "mode": 0o755},
            ]))

        assert su._apply_delta(staging, delta) is True
        assert exe.read_bytes() == new_blob.read_bytes(), "内容应更新为新版"
        assert stat.S_IMODE(os.stat(exe).st_mode) == 0o755, (
            "增量套用后主二进制丢了执行位 → 更新后 macOS 会拒绝启动"
        )
    print("✅ 增量套用：内容更新且主二进制保留 0755（不再出现更新后打不开）")


def test_apply_delta_new_file_honours_manifest_mode():
    """新增文件必须按 manifest 的 mode 复原权限。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        staging = root / "VideoDownloader.app"
        (staging / "Contents" / "Resources").mkdir(parents=True)
        payload = root / "payload.bin"
        payload.write_bytes(b"tool-bytes" * 10)

        delta = root / "d.delta"
        with zipfile.ZipFile(str(delta), "w") as z:
            z.writestr("0", payload.read_bytes())
            z.writestr("manifest.json", json.dumps([
                {"op": "new", "idx": 0,
                 "path": "Contents/Resources/newtool", "mode": 0o755},
            ]))

        assert su._apply_delta(staging, delta) is True
        p = staging / "Contents" / "Resources" / "newtool"
        assert p.read_bytes() == payload.read_bytes()
        # 若宿主文件系统不支持该位则容忍（Linux/CI 上可能被 umask 调整）
        if _IS_MAC:
            assert stat.S_IMODE(os.stat(p).st_mode) & 0o111, "new 文件应带执行位"
    print("✅ 增量套用：新增文件按 manifest mode 复原权限")


def test_apply_delta_delete_and_link():
    """del 删除旧文件、link 复原符号链接（包内 Frameworks/server 靠它重建）。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        staging = root / "VideoDownloader.app"
        res = staging / "Contents" / "Resources"
        res.mkdir(parents=True)
        (res / "gone.txt").write_text("old", encoding="utf-8")

        delta = root / "d.delta"
        with zipfile.ZipFile(str(delta), "w") as z:
            z.writestr("manifest.json", json.dumps([
                {"op": "del", "path": "Contents/Resources/gone.txt"},
                {"op": "link", "path": "Contents/Frameworks/server",
                 "target": "../Resources/server"},
            ]))

        assert su._apply_delta(staging, delta) is True
        assert not (res / "gone.txt").exists(), "del 应删除旧文件"
        link = staging / "Contents" / "Frameworks" / "server"
        assert link.is_symlink(), "link 应建出符号链接"
        assert os.readlink(str(link)) == "../Resources/server"
    print("✅ 增量套用：del 删除、link 复原软链")


_TRIM_BEGIN = 'KEEP_BASES="${VDL_KEEP_BASELINES:-2}"'
_TRIM_END = "# 4) 生成 latest.json"


def _trim_snippet() -> str:
    """从真实 publish_update.sh 里截取第 3.7 节的裁剪代码片段。

    为什么要"扣 flexible 代码"而不是在测试里复刻一遍：复刻会漂移（脚本改了测试还绿），
    而这里直接取脚本原文执行 —— 谁改动裁剪逻辑，测试立刻跟着变。
    """
    script = Path(__file__).resolve().parents[2] / "desktop" / "publish_update.sh"
    txt = script.read_text(encoding="utf-8")
    for marker in (_TRIM_BEGIN, _TRIM_END):
        if marker not in txt:
            raise AssertionError("publish_update.sh 结构已变，找不到锚点：%r" % marker[:40])
    return txt[txt.index(_TRIM_BEGIN):txt.index(_TRIM_END)]


def _run_trim(root: Path, versions, keep, version="", remote_ver="", junk=()):
    """跑一次真实裁剪片段，返回裁剪后 BaseDir 里剩下的条目名。

    HOME 被重定向到临时目录：脚本处理废弃基线的方式是 `mv ... "$HOME/.Trash/..."`，
    这样回收站也落在临时目录内，严格遵守本文件「不向用户家目录写东西」的约束。
    """
    base = root / "baselines"
    base.mkdir(parents=True, exist_ok=True)
    for v in versions:
        (base / v).mkdir(exist_ok=True)
    for name, is_dir in junk:
        p = base / name
        p.mkdir(exist_ok=True) if is_dir else p.write_text("x", encoding="utf-8")
    home = root / "home"
    (home / ".Trash").mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update({
        "BASE_DIR": str(base),
        "VDL_KEEP_BASELINES": str(keep),
        "VERSION": version,
        "REMOTE_VER": remote_ver,
        "HOME": str(home),
    })
    proc = subprocess.run(["bash", "-c", _trim_snippet()], env=env,
                          capture_output=True, text=True)
    assert proc.returncode == 0, "裁剪片段执行失败：%s" % proc.stderr
    return sorted(p.name for p in base.iterdir())


def test_release_trim_never_drops_current_and_remote():
    """当前发布版本与线上版本**必须**躲过裁剪。

    误删它们不是小事： VERSION 的基线是本次发布的产物， REMOTE_VER 的基线是下轮
    生成差分的唯一基准（缺了只能退化成"每次 400MB 全量"）。
    """
    versions = ["1.0.1", "1.0.2", "1.0.3", "1.0.4", "1.0.5"]
    with tempfile.TemporaryDirectory() as td:
        left = _run_trim(Path(td), versions, keep=2,
                         version="1.0.2", remote_ver="1.0.3")
    # 排序后 1.0.1/1.0.2/1.0.3 落在被裁区间，但后两者被保护位捞了回来
    assert "1.0.1" not in left, "最老的一版应被裁剪，实际却是：%s" % left
    for guarded in ("1.0.2", "1.0.3", "1.0.4", "1.0.5"):
        assert guarded in left, "%s 不该被剪掉（当前/线上版本受保护）：%s" % (guarded, left)
    print("✅ 基线裁剪：当前版本与线上版本永不被删")


def test_release_trim_respects_numeric_order():
    """1.0.10 / 1.0.11 不能排在 1.0.2 之前 —— 否则删掉的是最新的基线。"""
    versions = ["1.0.2", "1.0.9", "1.0.10", "1.0.11"]
    with tempfile.TemporaryDirectory() as td:
        left = _run_trim(Path(td), versions, keep=2)
    assert left == ["1.0.10", "1.0.11"], left
    print("✅ 基线裁剪按数值序取舍，保留 1.0.10 / 1.0.11 而非 1.0.9")


def test_release_trim_disabled_when_keep_is_zero():
    """VDL_KEEP_BASELINES=0 是显式关闭裁剪的开关，必须一个都不删。"""
    versions = ["1.0.1", "1.0.2", "1.0.3", "1.0.4"]
    with tempfile.TemporaryDirectory() as td:
        left = _run_trim(Path(td), versions, keep=0)
    assert left == versions, "keep=0 不应裁剪任何基线，实际：%s" % left
    print("✅ 基线裁剪：VDL_KEEP_BASELINES=0 时不删任何东西")


def test_release_trim_ignores_non_version_entries():
    """只有「纯版本号形态」的目录才算候选，混进来的其它文件/目录一律不碰。

    别小看这条：BASE_DIR 在真实环境里也曾被丢进过日志文件和临时目录，
    若把它们当候选一起 mv 走，用户手上的东西就悄悄没了。
    """
    versions = ["1.0.1", "1.0.2", "1.0.3", "1.0.4"]
    junk = [("latest", True), (".DS_Store", False), ("build_tmp", True)]
    with tempfile.TemporaryDirectory() as td:
        left = _run_trim(Path(td), versions, keep=1, junk=junk)
    for name, _ in junk:
        assert name in left, "%s 不是版本号形态，不该被裁剪逻辑碰：%s" % (name, left)
    assert "1.0.1" not in left, "最老基线应被裁剪：%s" % left
    print("✅ 基线裁剪：非版本号形态的目录/文件一律不动")


if __name__ == "__main__":
    test_parse_ver_is_numeric()
    test_publish_script_uses_numeric_version_sort()

    test_verify_app_rejects_unsigned()
    test_verify_app_compares_version_when_signed_ok()

    test_prepare_update_rejects_sha_mismatch()
    test_prepare_update_full_requires_verify()
    test_prepare_update_delta_requires_exact_from_version()

    test_apply_delta_bsdiff_preserves_exec_bit()
    test_apply_delta_new_file_honours_manifest_mode()
    test_apply_delta_delete_and_link()

    test_release_trim_never_drops_current_and_remote()
    test_release_trim_respects_numeric_order()
    test_release_trim_disabled_when_keep_is_zero()
    test_release_trim_ignores_non_version_entries()

    print("\n🎉 自更新链路测试全部通过（14 项）")
