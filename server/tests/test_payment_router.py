"""server/tests/test_payment_router.py —— 支付 REST 层回归测试（离线）。

钉死三件易复发的事：
1. 订单目录必须走 auth_store._base_dir()（~/.video-downloader + VDL_DATA_DIR 隔离），
   不得回落 ~/.videodownloader（无短横线）。app 上根本没有 DATA_DIR 属性，旧代码的
   getattr 回落分支恒为死路，却会把订单写到错误的历史目录上。
2. 离线测试不得写脏真实家目录（VDL_DATA_DIR 必须生效）。
3. routers/payment.py 必须可 import 且四个 /api/cloud/pay/* 路由都在 —— 因为 app.py
   无条件 include 该 router，一旦此文件缺文件/缺路由，全新 clone 直接起不来。

不启动服务、不发网络请求。
"""
import os
import sys
import tempfile

SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

EXPECTED_ROUTES = [
    "/api/cloud/pay/create",
    "/api/cloud/pay/query",
    "/api/cloud/pay/notify",
    "/api/cloud/pay/simulate_paid",
]


def test_order_dir_follows_base_dir_and_vdl_data_dir():
    from routers import payment

    tmp = tempfile.mkdtemp(prefix="vdl_pay_router_")
    old = os.environ.get("VDL_DATA_DIR")
    os.environ["VDL_DATA_DIR"] = tmp
    try:
        got = str(payment._order_dir())
    finally:
        if old is None:
            os.environ.pop("VDL_DATA_DIR", None)
        else:
            os.environ["VDL_DATA_DIR"] = old
    assert got == os.path.join(tmp, "pay_orders"), (
        f"订单目录应受 VDL_DATA_DIR 隔离，实际: {got}")


def test_order_dir_never_falls_back_to_legacy_no_dash_dir():
    """回归：曾写过 ~/.videodownloader（无短横线）。家目录里两个目录都存在，
    写错目录不会报错，只会让订单静默落到历史目录 + 绕开测试隔离。"""
    from routers import payment

    tmp = tempfile.mkdtemp(prefix="vdl_pay_router_")
    old = os.environ.get("VDL_DATA_DIR")
    os.environ["VDL_DATA_DIR"] = tmp
    try:
        got = str(payment._order_dir())
    finally:
        if old is None:
            os.environ.pop("VDL_DATA_DIR", None)
        else:
            os.environ["VDL_DATA_DIR"] = old
    legacy = os.path.expanduser("~/.videodownloader/pay_orders")
    assert got != legacy, "订单目录回落到了历史目录 ~/.videodownloader（无短横线）"


def test_router_importable_with_expected_pay_routes():
    """app.py 无条件 include 本 router：缺文件 或 路由改名 都会让 App 起不来。"""
    from routers import payment

    paths = {getattr(r, "path", None) for r in payment.router.routes}
    for p in EXPECTED_ROUTES:
        assert p in paths, f"缺少路由 {p}；现有: {sorted(x for x in paths if x)}"


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
        print(f"✅ {fn.__name__}")
    print("ALL PAYMENT ROUTER TESTS PASSED")
