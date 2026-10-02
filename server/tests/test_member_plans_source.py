# -*- coding: utf-8 -*-
"""守卫：1/3/7 天下载会员档 + 套餐价格单一真源（展示 / 下单 / 发放同源）。

背景（2026-09-30 用户需求）：充值新增 1/3/7 天会员档，价格由超管在后台写。

本守卫钉住三件事：
  1) 三档存在、days 正确，且默认顺序由短到长（决定前端卡片顺序）；
  2) 超管写 plans.json 覆盖层后，**展示 / 下单金额 / 发放天数**三处同步跟随。
     此前下单读硬编码 payment_core.PAY_PLANS、发货读硬编码 membership.DOWNLOAD_PLANS
     —— 后台改了价格和天数，实际扣款与到账时长都不变（改了等于没改）。
  3) 覆盖层按键合并不吞其他档、缺字段回落默认；只存在于覆盖层的档位
     （超管自定义新套餐）同样可展示 / 可下单 / 可激活。

所有用例都在 VDL_DATA_DIR 临时目录内进行，绝不写真实家目录。
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

_tmp = tempfile.mkdtemp(prefix="vdl_plans_guard_")
os.environ["VDL_DATA_DIR"] = _tmp
os.environ["VDL_CLOUD_LINK"] = "0"
# 价格云端真源（授权中心）在本用例必须关闭：本用例独立验证「本机覆盖层」，
# 若开着会拉到真实云端价格盖掉本机改的价，退化成依赖网络的非隔离测试。
os.environ["VDL_PLANS_CLOUD"] = "0"

import membership as M                                       # noqa: E402
from membership import MembershipStore, effective_plans, effective_pay_plans  # noqa: E402
import payment_core as PC                                    # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(("  \u2705 " if cond else "  \u274c ") + name)
    if not cond:
        FAILS.append(name)


SHORT_DAYS = {"download_1day": 1, "download_3day": 3, "download_7day": 7}
ORDER = ["download_1day", "download_3day", "download_7day",
         "download_month", "download_half_year", "download_year"]


def _store(name: str, now: float) -> MembershipStore:
    return MembershipStore(path=pathlib.Path(_tmp) / name, now_fn=lambda: now)


def test_short_term_plans_exist() -> None:
    print("\n[A] 1/3/7 天档存在且顺序由短到长")
    dl = effective_plans()["download_plans"]
    check("download_plans 按 1/3/7/30/180/365 天排列", list(dl) == ORDER)
    for code, days in SHORT_DAYS.items():
        check(f"{code} 在表内且 days={days}", code in dl and int(dl[code].get("days") or 0) == days)
    check("三档均有价格与中文标签",
          all(float(dl[c].get("price_cny") or 0) > 0 and dl[c].get("label") for c in SHORT_DAYS))
    check("下单兜底表 PAY_PLANS 同步含三档", all(c in PC.PAY_PLANS for c in SHORT_DAYS))


def test_override_price_and_days_take_effect() -> None:
    print("\n[B] 超管改价/改天数 → 展示 / 下单 / 发放三处同步")
    M.save_plan_overrides({"download_plans": {"download_7day": {"days": 5, "price_cny": 12.34}}})

    dl = effective_plans()["download_plans"]
    check("[展示] 7 天档 days 变 5、价变 12.34",
          int(dl["download_7day"]["days"]) == 5
          and abs(float(dl["download_7day"]["price_cny"]) - 12.34) < 1e-9)
    check("[展示] 缺省字段回落默认 label", dl["download_7day"].get("label") == "下载会员·7天")
    check("[展示] 其他档未被吞掉",
          all(c in dl for c in ORDER if c != "download_7day"))

    plans = effective_pay_plans()
    check("[下单] 生效下单表价格 = 12.34",
          abs(float(plans["download_7day"]["price"]) - 12.34) < 1e-9)

    svc = PC.PaymentService(PC.OrderStore(pathlib.Path(_tmp) / "orders"),
                            PC.get_provider("mock", {}), plans_fn=effective_pay_plans)
    order = svc.create("u1", "download_7day")
    check("[下单] 真实订单金额 = 12.34（不是硬编码 9.90）",
          abs(float(order["amount"]) - 12.34) < 1e-9)

    st = _store("m_b.json", 1000.0)
    res = st.activate("download_7day")
    check("[发放] 到期时间 = 5 天后（不是常量里的 7 天）",
          abs(float(res["expire_at"]) - (1000.0 + 5 * 86400)) < 1)

    # /api/member/plans 的真身就是 store.plans()：前端卡片数据源，必须同源。
    # （2026-09-30 变异测试发现：只测模块级 effective_plans() 会漏掉这里——
    #  把 store.plans() 退回常量，前端展示就再也跟不上后台改价。）
    api = _store("m_api.json", 3000.0).plans()["download_member"]["plans"]
    check("[/api/member/plans 真身] store.plans() 的 7 天档 = 5 天 / 12.34",
          int(api["download_7day"]["days"]) == 5
          and abs(float(api["download_7day"]["price_cny"]) - 12.34) < 1e-9)
    check("[/api/member/plans 真身] store.plans() 仍含 1/3/7 三档",
          all(c in api for c in SHORT_DAYS))


def test_no_injection_falls_back() -> None:
    print("\n[C] 未注入 plans_fn 时回落静态表（既有单测兼容）")
    svc = PC.PaymentService(PC.OrderStore(pathlib.Path(_tmp) / "orders2"),
                            PC.get_provider("mock", {}))
    check("PAY_PLANS 静态价仍可读", abs(float(svc.plans["download_7day"]["price"]) - 9.90) < 1e-9)
    bad = PC.PaymentService(PC.OrderStore(pathlib.Path(_tmp) / "orders3"),
                            PC.get_provider("mock", {}),
                            plans_fn=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    check("plans_fn 抛异常时回落而非拦死下单",
          abs(float(bad.plans["download_7day"]["price"]) - 9.90) < 1e-9)


def test_override_only_extra_plan_works() -> None:
    print("\n[D] 超管自定义新档（只存在于覆盖层）可展示 / 下单 / 激活")
    M.save_plan_overrides({"download_plans": {"download_15day": {
        "price_cny": 15.0, "days": 15, "label": "下载会员·15天"}}})
    check("覆盖层新增档出现在生效表", "download_15day" in effective_plans()["download_plans"])
    check("覆盖层新增档出现在下单表", "download_15day" in effective_pay_plans())
    st = _store("m_d.json", 2000.0)
    res = st.activate("download_15day")
    check("新增档可激活且按 15 天发放",
          res.get("ok") and abs(float(res["expire_at"]) - (2000.0 + 15 * 86400)) < 1)


def test_override_iterative_merge() -> None:
    print("\n[E] 覆盖层逐条合并：改一个价不会把别的档从覆盖层抹掉")
    M.save_plan_overrides({"download_plans": {"download_1day": {"price_cny": 2.00}}})
    M.save_plan_overrides({"download_plans": {"download_3day": {"price_cny": 5.00}}})
    ov = M.load_plan_overrides().get("download_plans") or {}
    check("第一次改的 1 天档仍在覆盖层里", "download_1day" in ov)
    check("第二次改的 3 天档也进了覆盖层", "download_3day" in ov)


def test_test_isolation() -> None:
    print("\n[F] 数据目录隔离：不写真实家目录")
    p = str(M.plan_override_path())
    check("plan_override_path 落在 VDL_DATA_DIR 内", p.startswith(_tmp))
    check("membership 状态文件同样落在临时目录内",
          str(M.default_state_path()).startswith(_tmp))


def test_web_wiring_present() -> None:
    print("\n[G] 网页版充值入口接线（前端真源码静态核对）")
    root = HERE.parent.parent
    idx = (root / "web" / "index.html").read_text(encoding="utf-8")
    appjs = (root / "web" / "app.js").read_text(encoding="utf-8")
    css = (root / "web" / "styles.css").read_text(encoding="utf-8")
    check("个人中心有套餐容器 #pfPlans", 'id="pfPlans"' in idx)
    check("有支付二维码弹窗 #payModal", 'id="payModal"' in idx and 'id="payQr"' in idx)
    check("app.js 拉取 /api/member/plans 渲染套餐卡",
          "'/api/member/plans'" in appjs and "pfRenderPlans" in appjs)
    check("下单走 /api/cloud/pay/create + 轮询 /api/cloud/pay/query",
          "'/api/cloud/pay/create'" in appjs and "'/api/cloud/pay/query'" in appjs)
    check("pfRender 会触发套餐渲染（否则登录后不显示）",
          "pfRenderPlans();" in appjs)
    check("样式含套餐卡与支付弹窗类", ".pf-plans {" in css and ".pay-qr {" in css)
    check("套餐卡按钮带 data-code（下单靠它取档位）",
          'class="pf-ov-btn pf-plan-buy" data-code=' in appjs)


def test_cloud_price_wins_over_local() -> None:
    """[C] 云端（授权中心）价格优先级最高：网页版展示与下单都以云端为准。

    钉住 2026-10-03 的「价格单一真源」：本机覆盖层只是离线兜底，云端在时压过它。
    用桩函数模拟云端返回，不发真实请求。
    """
    print("\n[C] 云端价格优先级高于本机覆盖层")
    M.save_plan_overrides({"download_plans": {"download_7day": {"price_cny": 12.34, "days": 5}}})
    eff = effective_plans()
    check("[云端] 本机改价在云端缺席时生效（离线兜底）",
          abs(float(eff["download_plans"]["download_7day"]["price_cny"]) - 12.34) < 1e-6)

    origin = M.cloud_plan_overrides
    before_1day = float(eff["download_plans"]["download_1day"]["price_cny"])
    M.cloud_plan_overrides = lambda force=False: {  # type: ignore[assignment]
        "download_plans": {"download_7day": {"price_cny": 9.90, "days": 7}},
    }
    try:
        eff2 = effective_plans()
        check("[云端] 云端价格压过本机覆盖层",
              abs(float(eff2["download_plans"]["download_7day"]["price_cny"]) - 9.90) < 1e-6)
        check("[云端] 云端未携带的档位价格保持不变（逐档合并，不整表替换）",
              abs(float(eff2["download_plans"]["download_1day"]["price_cny"]) - before_1day) < 1e-6)
    finally:
        M.cloud_plan_overrides = origin  # type: ignore[assignment]
    check("[云端] 开关 VDL_PLANS_CLOUD=0 时返回 None（离线隔离）",
          M.cloud_plan_overrides(force=True) is None)


def main() -> int:
    test_short_term_plans_exist()
    test_override_price_and_days_take_effect()
    test_no_injection_falls_back()
    test_override_only_extra_plan_works()
    test_override_iterative_merge()
    test_cloud_price_wins_over_local()
    test_test_isolation()
    test_web_wiring_present()
    print("\n" + "=" * 46)
    if FAILS:
        print("\u274c 失败 %d 项：" % len(FAILS))
        for f in FAILS:
            print("   - " + f)
        return 1
    print("\u2705 会员套餐与价格真源守卫全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
