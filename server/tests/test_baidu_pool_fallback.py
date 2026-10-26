"""百度网盘共享池兜底 —— 运行时回归测试（2026-10-25）。

为什么需要运行时测试（而不是只靠 test_baidu_cookie_share.mjs 的源码断言）
--------------------------------------------------------------------
第一版实现把共享池兜底写在了 `get_cached_cookie_header(...)` 的 `return` **之后**，
那是死代码：池子里明明有条目，`_baidu_login_cookie()` 永远返回空，页面表现为
「共享池没生效」。源码断言只能看「有没有那段代码」，看不住「跑不跑得到」——
只有真调用一次才能抓住这类错误。故本测试直接调函数，不 mock。

同时钉死取用顺序：手动 BDUSS > 本机缓存 > 共享池（自己的账号优先于别人的）。

全程隔离：HOME 与 VDL_DATA_DIR 都指到临时目录，绝不碰真实凭据/池子。
运行：cd server && python3 tests/test_baidu_pool_fallback.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

# 🔴 必须在 import 业务模块之前换 HOME：credential_store / cookie_pool 都用
#    Path.home() 定位，只设 VDL_DATA_DIR 不够（实测会读到真实凭据）。
_TMP = tempfile.mkdtemp(prefix="vdl_baidu_pool_")
os.environ["HOME"] = _TMP
os.environ["VDL_DATA_DIR"] = os.path.join(_TMP, "data")
os.environ.setdefault("VDL_CLOUD_LINK", "0")

from credential_store import clear_baidu_bduss, set_baidu_bduss  # noqa: E402
from cookie_pool import _pool_file, get_cookie, is_allowed, is_contrib_only  # noqa: E402


def test_pool_fallback_and_priority():
    # 域判定：百度可贡献可取用，但属「仅显式贡献」
    assert is_allowed("pan.baidu.com"), "百度应被池放行（贡献/取用）"
    assert is_contrib_only("pan.baidu.com"), "百度必须标记为仅显式贡献（禁自动同步）"

    # 造一条「已验真」的池条目（直接写文件：add_cookie 会验真，假 BDUSS 进不去）
    pool_file = _pool_file("baidu.com")
    pool_file.parent.mkdir(parents=True, exist_ok=True)
    pool_file.write_text(json.dumps({
        "cookies": [{"header": "BDUSS=POOLSHARED123; STOKEN=x",
                     "ts": time.time(), "source": "contrib"}],
    }), encoding="utf-8")
    assert get_cookie("pan.baidu.com"), "池条目写进去了却取不到（_pool_file 键不一致？）"

    # ① 本机没有登录态 → 必须落到共享池（第一版死代码就死在这一步）
    clear_baidu_bduss()
    from routers.cloud_drive import _baidu_login_cookie
    got = _baidu_login_cookie()
    assert got and "POOLSHARED123" in got, f"共享池兜底没接上，实际取到: {got!r}"
    print("✅ 本机无登录态 → 落到共享池（别人贡献的那条）")

    # ② 本机存了自己的 → 必须盖过共享池
    set_baidu_bduss("BDUSS=MYOWN456")
    got2 = _baidu_login_cookie()
    assert got2 and "MYOWN456" in got2, f"手动 BDUSS 必须优先于共享池，实际: {got2!r}"
    print("✅ 本机有登录态 → 盖过共享池（自己的账号优先）")


if __name__ == "__main__":
    test_pool_fallback_and_priority()
    print("\n全部通过 ✅（百度共享池：兜底接得上 + 手动优先 共 2 例）")
