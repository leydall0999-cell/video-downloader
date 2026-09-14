"""成片「文件名/列表」回归测试（2026-09-15 新增）。

背景（用户实测反馈）：
  上传 45 分钟视频 → 生成成片，历史里出现的两条命名问题：
    ① `60e0adcf0f011d68-解说完成202609150032.nomusic.mp4`
       —— 前缀是缓存接收站的 sha 哈希（用户看到的是「乱码」），不是原片名；
          后缀 `.nomusic` 是渲染时落的无音乐原片 sidecar，被当成了独立成片。
    ② 历史条目数量翻倍：每部成片都多出一条 `.nomusic.mp4` 同名记录。

根因：
  · `app._commentary_title()` 最后一行直接 `Path(src_path).stem` 返回，**没有语义校验**。
    本地拖拽上传走 cache-by-hash，源文件按 sha256 前 16 位命名 → 哈希进了成片名。
  · `commentary_list()` 只按扩展名过滤，`.nomusic.mp4` 的 suffix 也是 `.mp4`，一并列出。

覆盖：
  app._meaningful_stem                 语义名判定（upload/纯数字/哈希/VID_xxx 都不算）
  app._commentary_title                片名优先级链（显式 title → stash 原名 → 任务标题
                                       → 源文件名 → ffprobe → v<6hex> 短码）
  commentary_list                      只列成品，剔除 .nomusic sidecar

设计约束：不写用户家目录、不联网、不加载真模型；stash 索引与输出目录一律 monkeypatch。
"""
import sys
import tempfile
import types
from pathlib import Path

_SERVER_DIR = str(Path(__file__).resolve().parent.parent)
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402  先完成 app 初始化
from routers import commentary as cm  # noqa: E402


def _payload(**kw):
    """构造 `_commentary_title` 需要的请求对象（它只用 getattr 读字段）。"""
    base = {"task_id": "", "file_id": "", "title": ""}
    base.update(kw)
    return types.SimpleNamespace(**base)


class _stash_patch:
    """临时替换 stash 查表，返回指定的登记信息。"""

    def __init__(self, entry):
        self._entry = entry
        self._orig = None

    def __enter__(self):
        self._orig = server_app._stash_lookup
        server_app._stash_lookup = lambda sha: self._entry
        return self

    def __exit__(self, *exc):
        server_app._stash_lookup = self._orig
        return False


# ---------------------------------------------------------------- _meaningful_stem
def test_meaningful_stem_guards():
    """哈希/占位名不得当片名：这是「成片名变乱码」的直接防线。"""
    bad = ["upload", "", "12345", "60e0adcf0f011d68", "ee6b7cf95736", "abcdef123456"]
    for s in bad:
        assert not server_app._meaningful_stem(s), f"{s!r} 不应被当作有语义片名"
    good = ["少帅", "少帅_国产剧_第8集", "第 08 集 对峙"]
    for s in good:
        assert server_app._meaningful_stem(s), f"{s!r} 应被接受为片名"
    print("✅ _meaningful_stem 拦截哈希/占位名，放行真片名")


# ---------------------------------------------------------------- _commentary_title
def test_title_explicit_wins():
    """显式传入的片名优先级最高（前端带原始文件名走这条）。"""
    got = server_app._commentary_title(_payload(title="少帅 第8集", file_id="stash:abc"), "/x/whatever.mp4")
    assert got == "少帅 第8集", f"显式 title 应胜出，got {got!r}"
    print("✅ 显式片名优先")


def test_title_from_stash_original_name():
    """本地拖拽上传：用 stash 登记的用户原始文件名，而不是 sha 哈希。"""
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "60e0adcf0f011d68.mp4"
        src.write_bytes(b"\x00")
        entry = {"path": str(src), "name": "少帅 第8集.mp4"}
        with _stash_patch(entry):
            got = server_app._commentary_title(_payload(file_id="stash:60e0adcf0f011d68"), str(src))
    assert got == "少帅 第8集", f"应取 stash 原始文件名，got {got!r}"
    print("✅ stash 场景取用户原始文件名（不再是哈希）")


def test_title_falls_back_to_shortcode_not_hash():
    """核心回归：stash 索引丢失（进程重启）且源文件名就是哈希 → 必须短码兜底。"""
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "60e0adcf0f011d68.mp4"
        src.write_bytes(b"\x00")
        # 进程重启后 _stash_lookup 的磁盘兜底分支返回 name=""，模拟该场景
        with _stash_patch({"path": str(src), "name": ""}):
            got = server_app._commentary_title(_payload(file_id="stash:60e0adcf0f011d68"), str(src))
    assert got != "60e0adcf0f011d68", "哈希绝不能当片名（本次事故的原始症状）"
    assert got.startswith("v") and len(got) == 7, f"应短码兜底 v<6hex>，got {got!r}"
    print(f"✅ 哈希源名走短码兜底（{got}），不再出现 16 位乱码")


def test_title_from_meaningful_source_file():
    """媒体库/本地文件：文件名有语义就直接用。"""
    got = server_app._commentary_title(_payload(file_id="AbCdEf"), "/movies/少帅_第8集.mp4")
    assert got == "少帅_第8集", f"应取源文件名，got {got!r}"
    print("✅ 源文件名有语义时直接采用")


def test_title_from_download_task_title():
    """下载任务路径：任务标题优先于源文件名（站内标题比本地文件名更准）。"""
    class _T:
        id = "t1"
        title = "少帅 第8集"
        steps = []
        url = ""

    orig_req, orig_store = server_app._require_task, server_app.store
    server_app._require_task = lambda tid: _T()
    try:
        got = server_app._commentary_title(_payload(task_id="t1"), "/tmp/60e0adcf0f011d68.mp4")
    finally:
        server_app._require_task, server_app.store = orig_req, orig_store
    assert got == "少帅 第8集", f"应取任务标题，got {got!r}"
    print("✅ 下载任务标题优先")


# ---------------------------------------------------------------- commentary_list
def test_list_excludes_nomusic_sidecar():
    """历史列表只列成品：`.nomusic.mp4` 是同名 sidecar，不能当独立成片。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        base = "少帅 第8集-解说完成202609150032"
        (root / f"{base}.mp4").write_bytes(b"final")
        (root / f"{base}.nomusic.mp4").write_bytes(b"nomusic")
        (root / f"{base}.mp4.bgm.json").write_text("{}", encoding="utf-8")
        orig = server_app._commentary_roots
        server_app._commentary_roots = lambda: [root]
        try:
            data = cm.commentary_list()
        finally:
            server_app._commentary_roots = orig
    names = [it["name"] for it in data["items"]]
    assert names == [f"{base}.mp4"], f"只应列出成品一条，实得 {names}"
    assert not any("nomusic" in n for n in names), "sidecar 不得出现在解说历史里"
    print("✅ 解说历史剔除 .nomusic sidecar（数量不再翻倍）")


def test_list_keeps_other_video_exts():
    """回归确认过滤没有误伤：正常 mp4/mkv 都要照旧列出。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "a-解说完成2026091501.mp4").write_bytes(b"a")
        (root / "b-解说完成2026091502.mkv").write_bytes(b"b")
        orig = server_app._commentary_roots
        server_app._commentary_roots = lambda: [root]
        try:
            data = cm.commentary_list()
        finally:
            server_app._commentary_roots = orig
    names = sorted(it["name"] for it in data["items"])
    assert names == ["a-解说完成2026091501.mp4", "b-解说完成2026091502.mkv"], f"实得 {names}"
    print("✅ 正常成片（mp4/mkv）仍正常列出")


if __name__ == "__main__":
    fns = [
        test_meaningful_stem_guards,
        test_title_explicit_wins,
        test_title_from_stash_original_name,
        test_title_falls_back_to_shortcode_not_hash,
        test_title_from_meaningful_source_file,
        test_title_from_download_task_title,
        test_list_excludes_nomusic_sidecar,
        test_list_keeps_other_video_exts,
    ]
    for fn in fns:
        fn()
    print(f"\n✅ 全部 {len(fns)} 项通过")
