"""首页缓存（_index_cache）失效判据回归测试（2026-09-30 线上事故）

事故：只改了 index.html + app.js 下发（没动 styles.css），线上首页**永远是旧内容** ——
连 CF 回源都是旧的，刷新浏览器/清浏览器缓存都没用。根因是 app.index() 的内存缓存
只用 styles.css 的 mtime 做失效判据，index.html 自己的 mtime 没参与比较：

    _index_cache = {"html": None, "mtime": 0.0}          # 只有一个 mtime
    if cache["html"] is None or cache["mtime"] != css_mtime:

于是「只换 HTML」的部署永远不会重读 → 首页停在上一版，直到进程重启或恰好也改了 CSS
（后者会掩盖问题，所以这个坑可以潜伏很久才被撞见一次）。

本测试直接调用真实路由（TestClient）并临时替换 app.WEB_DIR，覆盖：
  ① 只改 index.html  → 必须重读（回归点，改坏即红）
  ② 只改 styles.css  → 必须重读（原有能力，不许为了修①退化）
  ③ 两个 mtime 都没变 → 必须命中缓存（不能退化成每次读盘）

运行：cd server && VDL_DATA_DIR=$(mktemp -d) python tests/test_index_cache_invalidation.py
"""
import os
import sys
import tempfile

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

os.environ.setdefault("VDL_DATA_DIR", tempfile.mkdtemp(prefix="vdl_idxcache_"))

import app as server_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

HTML = "<!doctype html><html><body><p>MARKER-%s</p></body></html>"
CSS = "/* css-%s */"


def _write(web_dir, tag, html_mtime=None, css_mtime=None):
    idx = os.path.join(web_dir, "index.html")
    css = os.path.join(web_dir, "styles.css")
    with open(idx, "w", encoding="utf-8") as f:
        f.write(HTML % tag)
    with open(css, "w", encoding="utf-8") as f:
        f.write(CSS % tag)
    # mtime 显式设置：同一秒内连写两个文件时，文件系统 mtime 粒度可能不足以区分版本
    base = 1_700_000_000
    os.utime(idx, (base, base if html_mtime is None else html_mtime))
    os.utime(css, (base, base if css_mtime is None else css_mtime))


def _get(web_dir):
    # 每次都用新的 TestClient：绕开 TestClient 自身的响应缓存干扰
    return TestClient(server_app.app).get("/").text


def test_index_html_change_invalidates_cache():
    """只改 index.html（不动 styles.css）必须立刻生效 —— 本次事故的回归点。"""
    with tempfile.TemporaryDirectory() as web_dir:
        old_web = server_app.WEB_DIR
        server_app.WEB_DIR = web_dir
        try:
            server_app._index_cache.update({"html": None, "css_mtime": 0.0, "idx_mtime": 0.0})
            _write(web_dir, "v1", html_mtime=2000, css_mtime=2000)
            assert "MARKER-v1" in _get(web_dir), "首次请求应返回 v1"

            # 只换 HTML，CSS 原样（mtime 不动）
            _write(web_dir, "v2", html_mtime=3000, css_mtime=2000)
            body = _get(web_dir)
            assert "MARKER-v2" in body, (
                "只改 index.html 后首页仍是旧内容 —— 缓存失效判据漏了 index.html 的 mtime"
                "（2026-09-30 线上事故复发）")
        finally:
            server_app.WEB_DIR = old_web
    print("✅ 只改 index.html → 缓存正确失效")


def test_styles_css_change_still_invalidates_cache():
    """只改 styles.css 也必须生效（原有能力，修 ① 时不许退化）。"""
    with tempfile.TemporaryDirectory() as web_dir:
        old_web = server_app.WEB_DIR
        server_app.WEB_DIR = web_dir
        try:
            server_app._index_cache.update({"html": None, "css_mtime": 0.0, "idx_mtime": 0.0})
            _write(web_dir, "w1", html_mtime=2000, css_mtime=2000)
            assert "MARKER-w1" in _get(web_dir)

            _write(web_dir, "w2", html_mtime=2000, css_mtime=4000)
            assert "MARKER-w2" in _get(web_dir), "只改 styles.css 后首页应重读"
        finally:
            server_app.WEB_DIR = old_web
    print("✅ 只改 styles.css → 缓存正确失效")


def test_unchanged_mtime_hits_cache():
    """两个 mtime 都没变时必须命中缓存（不能退化成每次读盘）。

    手法：写入不同内容但把 mtime 强行设回原值 —— 只要还按 mtime 失效，就必须吐旧内容。
    """
    with tempfile.TemporaryDirectory() as web_dir:
        old_web = server_app.WEB_DIR
        server_app.WEB_DIR = web_dir
        try:
            server_app._index_cache.update({"html": None, "css_mtime": 0.0, "idx_mtime": 0.0})
            _write(web_dir, "c1", html_mtime=5000, css_mtime=5000)
            assert "MARKER-c1" in _get(web_dir)

            _write(web_dir, "c2", html_mtime=5000, css_mtime=5000)
            body = _get(web_dir)
            assert "MARKER-c1" in body, "mtime 未变却重读了 HTML —— 缓存形同虚设（部署期丢性能）"
            assert "MARKER-c2" not in body, "mtime 未变却返回了新内容 —— 说明判定逻辑已经不按 mtime"
        finally:
            server_app.WEB_DIR = old_web
    print("✅ mtime 未变 → 命中缓存（未退化成每次读盘）")


def test_source_guard_both_mtimes_in_condition():
    """源码级守卫：失效条件里必须同时出现两个 mtime（防有人「简化」回去）。"""
    src = open(os.path.join(_SERVER_DIR, "app.py"), encoding="utf-8").read()
    i = src.find("_index_cache = {")
    assert i > 0, "找不到 _index_cache 定义"
    seg = src[i:i + 1800]
    assert '"idx_mtime"' in seg, "缓存结构里应有 idx_mtime（index.html 的 mtime）"
    assert "cache[\"idx_mtime\"] != idx_mtime" in seg, "失效条件必须比较 index.html 的 mtime"
    assert "cache[\"css_mtime\"] != css_mtime" in seg, "失效条件必须比较 styles.css 的 mtime"
    print("✅ 源码守卫：失效条件同时覆盖 index.html 与 styles.css 两个 mtime")


if __name__ == "__main__":
    test_index_html_change_invalidates_cache()
    test_styles_css_change_still_invalidates_cache()
    test_unchanged_mtime_hits_cache()
    test_source_guard_both_mtimes_in_condition()
    print("\n✅ 首页缓存失效判据回归测试全部通过")
