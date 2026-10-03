"""server/tests/test_admin_user_usage.py — 后台「用户使用详情」聚合（2026-10-03）

动机：用户反馈「用不了」时管理员要能看到他真实的权益 / 配额消耗 / 激活历史 /
客服原文，而不是靠猜。这里钉死三件事：
  1) 纯函数 _usage_bundle 的聚合结构正确（按天倒序、只留 >0 的项、激活历史倒序）；
  2) store 读失败时降级为空而不是抛异常（面板不能因为一个用户 500）；
  3) 路由已挂载且带 require_admin 门禁（纯静态断言，防漏接）。
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="vdl_usage_guard_")
os.environ["VDL_DATA_DIR"] = _TMP
os.environ["VDL_CLOUD_LINK"] = "0"
os.environ["VDL_PLANS_CLOUD"] = "0"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

FAILED: list[str] = []


def check(name: str, cond: bool) -> None:
    print(("  ✓ " if cond else "  ❌ ") + name)
    if not cond:
        FAILED.append(name)


class _FakeStore:
    def __init__(self, state: dict | None = None, boom: bool = False) -> None:
        self._state = state or {}
        self._boom = boom

    def status(self) -> dict:
        if self._boom:
            raise RuntimeError("store 坏了")
        return {"download_member": {"active": True, "expire_at": 2000000000},
                "credits": {"ai_left": 120, "permanent": 210}}


def test_bundle_shape() -> None:
    from routers.admin import _usage_bundle

    state = {
        "daily_usage": {"date": "2026-10-03", "download": 3, "matting": 1, "zero": 0},
        "usage_history": {
            "2026-10-01": {"download": 2},
            "2026-10-02": {"download": 5, "compress": 1},
            "2026-10-03": {"download": 3, "matting": 1},
        },
        "meta": {
            "history": [{"code": "download_1day", "type": "activate", "via": "pay", "at": 100},
                        {"code": "ai_15000", "type": "activate", "via": "grant", "at": 200}],
            "device_fp": "fp-abc",
        },
    }
    b = _usage_bundle("u_1", "a@b.com", False, False, 123, _FakeStore(state))

    check("返回 user_id/identifier", b["user_id"] == "u_1" and b["identifier"] == "a@b.com")
    check("会员状态带出", b["membership"].get("download_member", {}).get("active") is True)

    days = b["usage_days"]
    check("使用天数按倒序（新→旧）", [d["date"] for d in days] == sorted(
        [d["date"] for d in days], reverse=True), )
    check("每日 total 汇总正确", days[0]["total"] == 4, )
    check("只保留 >0 的计数项（zero 被剔除）", "zero" not in days[0]["items"])
    check("使用汇总 active_days 正确", b["usage_summary"]["active_days"] == 3)
    check("使用汇总 total 正确", b["usage_summary"]["total"] == 12)

    acts = b["activations"]
    check("激活历史倒序（最新在前）", acts[0]["code"] == "ai_15000")
    check("设备指纹带出", b["device_fp"] == "fp-abc")


def test_degrade_not_crash() -> None:
    from routers.admin import _usage_bundle

    b = _usage_bundle("u_2", "x@y.z", True, True, 1, _FakeStore(boom=True))
    check("status() 抛异常时降级为空（不冒泡）", b["membership"] == {})
    check("disabled/is_admin 仍带出", b["disabled"] is True and b["is_admin"] is True)
    check("空 store 时 usage_days 为空列表", b["usage_days"] == [])


def test_route_wired() -> None:
    src = pathlib.Path(__file__).resolve().parents[1] / "routers" / "admin.py"
    txt = src.read_text(encoding="utf-8")
    check("路由已挂载", '"/api/admin/users/{user_id}/usage"' in txt)
    i = txt.find('"/api/admin/users/{user_id}/usage"')
    seg = txt[i:txt.find("\n@router", i + 10)]
    check("路由带 require_admin 门禁", "require_admin(request)" in seg)
    check("聚合客服会话", "_usage_support(user_id)" in seg)
    check("聚合云端授权（失败降级）", "_usage_cloud(" in seg)


def main() -> int:
    print("[1] 聚合结构")
    test_bundle_shape()
    print("[2] 降级不崩")
    test_degrade_not_crash()
    print("[3] 路由接线")
    test_route_wired()
    print(f"\n失败 {len(FAILED)} 项")
    for f in FAILED:
        print("  -", f)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
