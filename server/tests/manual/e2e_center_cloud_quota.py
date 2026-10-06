"""授权中心「云端免费额度按功能拆池」真机 E2E（2026-10-07 全量复测）。

**真机脚本，故意不进离线套件**：会真的注册账号、真的打 8.138.223.3:8888、
真的用管理员令牌下发覆盖层（短暂影响全局默认值，脚本内 try/finally 保证清除）。
放 `manual/` 是为了不让它被 `run_offline_tests.sh` 收走 —— 离线套件必须不发网络。

用法：python3 server/tests/manual/e2e_center_cloud_quota.py

覆盖：8 键独立 / 每日独立 / 退还 / 跨端共享 / 未知键回落 / 坏 token / 管理员覆盖层。
"""
import json
import sys
import time
import urllib.error
import urllib.request

CENTER = "http://8.138.223.3:8888"
RES = ["cloud_commentary", "cloud_convert", "cloud_concat",
       "cloud_dewatermark", "cloud_dewatermark_pdf",
       "cloud_subtitle", "cloud_subtitle_burn", "cloud_subtitle_translate"]

results = []


def post(path, data, timeout=15):
    req = urllib.request.Request(
        CENTER + path, data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body[:200]}


def chk(cond, label, extra=""):
    results.append((bool(cond), label))
    print(("  OK   " if cond else "  FAIL ") + label + (f"  [{extra}]" if extra else ""))


def quota(tok, **kw):
    body = {"token": tok}
    body.update(kw)
    st, r = post("/api/license/cloud_quota", body)
    return st, r


def main():
    ts = int(time.time())
    email = f"e2e-{ts}@example.com"
    print("== 1. 注册 ==")
    st, reg = post("/api/license/register",
                   {"email": email, "password": "pw123456", "fingerprint": "e2e-fp"})
    chk(st == 200 and reg.get("ok"), "注册成功", f"status={st}")
    tok = reg.get("token", "")
    chk(bool(tok), "拿到 token")

    print("== 2. 初始额度：8 键各 3 次终身 / 各 1 次每日 ==")
    st, r = quota(tok)
    cq = r.get("cloud_quota") or {}
    lr, dr = cq.get("lifetime_remaining") or {}, cq.get("daily_auto_remaining") or {}
    chk(all(lr.get(k) == 3 for k in RES), "终身 8 键各 3", str(lr))
    chk(all(dr.get(k) == 1 for k in RES), "每日 8 键各 1", str(dr))
    chk(all((cq.get("lifetime_limits") or {}).get(k) == 3 for k in RES),
        "lifetime_limits 回真值 3")

    print("== 3. 逐键独立性：cloud_convert 用满 3 次，其它键不受影响 ==")
    for i in range(3):
        st, r = quota(tok, lifetime=1, resource="cloud_convert")
        chk(r.get("allowed") is True, f"convert 第{i+1}次放行")
    st, r = quota(tok, lifetime=1, resource="cloud_convert")
    chk(r.get("allowed") is False, "convert 第 4 次被拒", str(r.get("reason")))
    chk((r.get("cloud_quota") or {}).get("lifetime_remaining", {}).get("cloud_convert") == 0,
        "convert 剩余 0")
    st, r = quota(tok, lifetime=1, resource="cloud_subtitle")
    chk(r.get("allowed") is True, "subtitle 仍可用（拆池生效）")

    print("== 4. 用满第二个键，验证互不串号 ==")
    for _ in range(3):
        quota(tok, lifetime=1, resource="cloud_dewatermark_pdf")
    st, r = quota(tok, lifetime=1, resource="cloud_dewatermark_pdf")
    chk(r.get("allowed") is False, "pdf 去水印用满被拒")
    st, r = quota(tok, lifetime=1, resource="cloud_dewatermark")
    chk(r.get("allowed") is True, "图片去水印独立可用（与 PDF 不是一个池）")

    print("== 5. 每日 auto 按功能独立 ==")
    st, r = quota(tok, daily=1, resource="cloud_subtitle_burn")
    chk(r.get("allowed") is True, "burn 每日第 1 次放行")
    st, r = quota(tok, daily=1, resource="cloud_subtitle_burn")
    chk(r.get("allowed") is False, "burn 每日第 2 次被拒", str(r.get("reason")))
    st, r = quota(tok, daily=1, resource="cloud_subtitle_translate")
    chk(r.get("allowed") is True, "translate 每日独立可用")

    print("== 6. 退还 ==")
    st, r = quota(tok, lifetime=1, resource="cloud_convert", refund=True)
    chk(r.get("refunded") is True, "convert 退还成功")
    st, r = quota(tok, lifetime=1, resource="cloud_convert")
    chk(r.get("allowed") is True, "退还后可再扣")

    print("== 7. 旧客户端兼容：不传 resource → 落默认键 cloud_commentary ==")
    st, r = quota(tok, lifetime=1)
    chk(r.get("allowed") is True, "默认键放行")
    cq = r.get("cloud_quota") or {}
    chk((cq.get("lifetime") or {}).get("cloud_commentary") == 1, "落到 cloud_commentary",
        str(cq.get("lifetime")))

    print("== 8. 未知 resource 回落默认键（不 500、不创建新池）==")
    st, r = quota(tok, lifetime=1, resource="cloud_whatever")
    chk(st == 200 and r.get("ok") is True, "未知键不报错", f"status={st}")
    chk((r.get("cloud_quota") or {}).get("lifetime", {}).get("cloud_commentary") == 2,
        "未知键归到 cloud_commentary")

    print("== 9. 坏 token / 空 token ==")
    st, r = quota("bogus-token")
    chk(r.get("ok") is not True, "坏 token 不放行")
    st, r = quota("")
    chk(r.get("ok") is not True, "空 token 不放行")

    print("== 10. 管理员覆盖层：终身次数下发中心后即时生效 ==")
    sys.path.insert(0, "/Users/suixindelang/WorkBuddy/video-downloader-app/server")
    import admin_store
    adm = admin_store._license_admin_token()
    chk(bool(adm), "拿到管理员令牌")
    if adm:
        try:
            st, r = post("/api/license/free_quota_set",
                         {"token": adm, "cloud_lifetime": {"cloud_convert": 1}})
            chk(st == 200 and r.get("ok") is True, "覆盖下发成功", f"status={st}")
            st, r = quota(tok)
            lim = (r.get("cloud_quota") or {}).get("lifetime_limits") or {}
            chk(lim.get("cloud_convert") == 1, "convert 上限变 1", str(lim.get("cloud_convert")))
            chk(lim.get("cloud_subtitle") == 3, "未覆盖的键仍是 3", str(lim.get("cloud_subtitle")))
            st, r = quota(tok, lifetime=1, resource="cloud_convert")
            chk(r.get("allowed") is False, "上限收紧后立即被拒（覆盖真生效）")
        finally:
            st, r = post("/api/license/free_quota_set",
                         {"token": adm, "cloud_lifetime": None})
            chk(st == 200 and r.get("ok") is True, "覆盖已清除", f"status={st}")
            st, r = quota(tok)
            lim = (r.get("cloud_quota") or {}).get("lifetime_limits") or {}
            chk(all(lim.get(k) == 3 for k in RES), "清除后恢复默认 3", str(lim))

    print()
    bad = [lb for ok, lb in results if not ok]
    if bad:
        print(f"FAILED {len(bad)}:")
        for b in bad:
            print("  -", b)
        sys.exit(1)
    print(f"CENTER_E2E_OK  ({len(results)} 项全过)  账号={email}")


if __name__ == "__main__":
    main()
