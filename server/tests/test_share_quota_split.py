#!/usr/bin/env python
"""「生成二维码 / 生成网页」进个人中心使用统计（web-dev，2026-10-19）

背景（用户反馈）
----------------
个人中心「使用统计」列了 10 行，但网页版顶部页签里明明还有
「生成二维码/链接」这个入口（内部两个子页签：生成二维码、生成网页）——
用户：「这两个也加进去，顺便统计还有什么功能没加进去一起加进去」。

盘点了网页版 8 个页签下所有用户可见功能后，缺的确实只有这两项：
  下载视频(1) / 视频格式转换 + 视频拼接 / 图片 + PDF 去水印 / 音乐转换 /
  图片转换 / AI 字幕 提取·烧录·翻译 —— 均已各有配额键。
分享此前是**唯一漏网的**：它的限次写在 routers/share_token.py 自己的本地文件里
（VDL_WEB_SHARE_DAILY，20 次/日），既不与账号绑定（换浏览器归零），也从不出现在
统计表，后台也改不了。

现改为账号级每日配额：share_qr / share_page（免费 20 次/日、会员 200 次/日），
按子页签分派（见 routers/share_token.py::_share_resource）。

本文件守卫 6 件事
-----------------
1. `_share_resource()` 的分派矩阵（pagegen → share_page；其余 → share_qr）；
2. 统计表两行的 free/member 与 FREE_DAILY_LIMITS / DAILY_QUOTA_LIMITS 一致；
3. 免费 20 / 会员 200（沿用受限版分享原有体验，不许倒退）；
4. `feature_usage_status()` 真能看到这两行；
5. `quota_state()` 真能拦住（不在配额表里 ⇒ unknown ⇒ fail-open，等于没限）；
6. 口径正确 + 接线完整：
   · 分享**不得**走 cloud_quota_gate（授权中心 _CLOUD_RESOURCES 只认 8 个旧键，
     未知键会被回退成 cloud_commentary —— 那会让「生成二维码」去消耗「视频解说」
     的终身额度，并把 20 次/日倒退成 3 次终身）；
   · 前端两个入口取 token 时都必须带 src，否则两个功能会并到一个计数器上。
"""
import os
import sys
from pathlib import Path

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

os.environ.setdefault("VDL_DATA_DIR", "/tmp/vdl_test_share_split")
os.environ.setdefault("VDL_CLOUD_LINK", "0")
os.environ.setdefault("VDL_OFFLINE_TESTS", "1")
for _k in ("VDL_QUOTA_RELAY_URL", "VDL_WORKER_URL"):
    os.environ.pop(_k, None)

from membership import (  # noqa: E402
    FEATURE_USAGE_DEFS, FREE_DAILY_LIMITS, DAILY_QUOTA_LIMITS,
    MembershipStore, feature_usage_status, download_benefits,
)
import app as _app_mod  # noqa: E402,F401  先加载 app，否则 routers 循环导入
from routers.share_token import _share_resource  # noqa: E402

_WEB = Path(_SERVER_DIR).parent / "web" / "app.js"


def _row(key: str) -> dict:
    for d in FEATURE_USAGE_DEFS:
        if d.get("key") == key:
            return d
    raise AssertionError(f"FEATURE_USAGE_DEFS 缺少 {key} 行 —— 统计表又少一项")


# --------------------------------------------------------------------------- #
# 1. 分派矩阵
# --------------------------------------------------------------------------- #
def test_dispatch_matrix():
    assert _share_resource("pagegen") == "share_page"
    assert _share_resource("  PAGEGEN ") == "share_page"
    for s in ("shareqr", "", None, "qr", "whatever"):
        assert _share_resource(s) == "share_qr", s


# --------------------------------------------------------------------------- #
# 2. 表与额度一致
# --------------------------------------------------------------------------- #
def test_defs_match_quota_tables():
    for key, resource in (("share_qr", "share_qr"), ("share_page", "share_page")):
        row = _row(key)
        assert row["resource"] == resource
        assert row["free_limit"] == FREE_DAILY_LIMITS[resource], key
        assert row["member_limit"] == DAILY_QUOTA_LIMITS[resource], key


def test_limits_keep_existing_experience():
    """沿用受限版分享原有的 20 次/日：迁到配额表后**不许倒退**。"""
    for r in ("share_qr", "share_page"):
        assert FREE_DAILY_LIMITS[r] == 20, f"{r} 免费应为 20 次/日（原 VDL_WEB_SHARE_DAILY）"
        assert DAILY_QUOTA_LIMITS[r] == 200, r


def test_benefit_copy_exists():
    texts = " ".join(x["text"] for x in download_benefits())
    assert "生成二维码 200 次/日" in texts, texts
    assert "生成网页 200 次/日" in texts, texts


# --------------------------------------------------------------------------- #
# 3. 统计表真的看得到
# --------------------------------------------------------------------------- #
def test_usage_status_lists_share_rows(tmp_path):
    store = MembershipStore(path=Path(tmp_path) / "m.json")
    rows = feature_usage_status(store, "today")
    names = [r["name"] for r in rows]
    assert "生成二维码" in names, f"使用统计缺少「生成二维码」：{names}"
    assert "生成网页" in names, f"使用统计缺少「生成网页」：{names}"


# --------------------------------------------------------------------------- #
# 4. 真能限次
# --------------------------------------------------------------------------- #
def test_quota_state_actually_limits(tmp_path):
    store = MembershipStore(path=Path(tmp_path) / "q.json")
    for r in ("share_qr", "share_page"):
        q = store.quota_state(r)
        assert not q.get("unknown"), f"{r} 不在配额表里，会被 fail-open 放行"
        assert q["limit"] == 20, f"{r} 免费档应为 20 次/日，实际 {q}"
    for _ in range(20):
        assert store.use_daily("share_qr")["ok"] is True
    assert store.use_daily("share_qr")["ok"] is False, "第 21 次应被拦"
    # 拆池核心语义：二维码用尽不影响生成网页
    assert store.use_daily("share_page")["ok"] is True


# --------------------------------------------------------------------------- #
# 5. 口径与接线
# --------------------------------------------------------------------------- #
def _share_code() -> str:
    """share_token.py 的**代码行**（剔除注释与 docstring —— 注释里提到某个符号
    不代表真的调用了它，否则守卫会被自己的说明文字带偏）。"""
    out = []
    for line in (Path(_SERVER_DIR) / "routers" / "share_token.py").read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("#") or s.startswith('"""') or s.startswith('"'):
            continue
        out.append(line)
    return "\n".join(out)


def _share_token_body() -> str:
    """`share_token()` 的函数体（剥掉 docstring）—— 判「先判可用性再计数」这种
    顺序，必须只看真实语句；说明文字里出现同样的词会把 index 判据带偏（实测）。"""
    import ast
    p = Path(_SERVER_DIR) / "routers" / "share_token.py"
    src = p.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "share_token")
    stmts = list(fn.body)
    if stmts and isinstance(stmts[0], ast.Expr) and isinstance(getattr(stmts[0], "value", None), ast.Constant):
        stmts = stmts[1:]
    return "\n".join(ast.get_source_segment(src, s) or "" for s in stmts)


def test_share_must_not_use_cloud_quota_gate():
    """分享不得走「终身 N 次」的云端额度通道（会把 20 次/日倒退成 3 次终身，
    且未知键在中心会被回退成 cloud_commentary，等于跨功能污染）。"""
    code = _share_code()
    assert "cloud_quota_gate(" not in code, \
        "分享改走 cloud_quota_gate 会撞上中心 _CLOUD_RESOURCES 白名单（未知键回退 cloud_commentary）"
    assert "use_daily(" in code, "取 token 必须真的计数（账号级每日配额）"
    assert "_share_resource(src)" in code, "配额键必须按子页签分派"


def test_share_unavailable_before_counting():
    """节点不支持时（HK 无分享节点）不得计数 —— 顺序不能反。"""
    body = _share_token_body()
    i_unavail = body.index("SHARE_UNAVAILABLE")
    i_count = body.index("use_daily(")
    assert i_unavail < i_count, "SHARE_UNAVAILABLE 的判断必须在计数之前"


def test_frontend_sends_pane_on_both_entries():
    """两个入口取 token 都必须带 src（不带 ⇒ 两个功能并到 share_qr 一个计数器）。"""
    src = _WEB.read_text(encoding="utf-8")
    assert "shareEnsureToken(shareSay, 'shareqr')" in src, "生成二维码入口未带 src"
    assert "shareEnsureToken(pgSay, 'pagegen')" in src, "生成网页入口未带 src"
    assert "/api/share/token?src=" in src


if __name__ == "__main__":
    import tempfile
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            if fn.__code__.co_argcount == 1:
                fn(tempfile.mkdtemp(prefix="vdl_share_"))
            else:
                fn()
            print(f"  ok  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"  FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} 通过")
    sys.exit(1 if failed else 0)
