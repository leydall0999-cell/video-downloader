# -*- coding: utf-8 -*-
"""端到端验证：网页版 AI 积分墙 + 首次免费，走**真实 HTTP 栈**（不是源码文本检查）。

背景：单元测试 G 组只能证明"源码里写了 gate_message"，证明不了它真的挂载在用
且 402 契约正确。这个用例用 FastAPI TestClient 把整个 app 起来，真注册账号、
真发请求，钉住三件事：
  1. AI 入口在均未登录时被挡（403/401），不会因为接线把鉴权顺序搞反；
  2. 同一个新账号的**首次** AI 请求放行（免费体验），积分仍为 0；
  3. 第二次同出一辙的请求返回 402，detail 就是给用户看的中文文案。

注意：这里只验证「闸门与契约」，不真的跑去跑转写/推理 —— 解说那条会在 503
（该实例未启用解说）处被打住，我们先把 wins水位ide 闸门装在 503 之前，
所以用它来观测"闸门是否判定通过"刚好够用（见 _TRY_COMMENTARY 的说明）。

所有读写都在 VDL_DATA_DIR 临时目录，绝不碰真实家目录。
"""
import os
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

_TMP = tempfile.mkdtemp(prefix="vdl_trial_e2e_")
os.environ["VDL_DATA_DIR"] = _TMP
os.environ["VDL_PLANS_CLOUD"] = "0"
# 🔴 必须关掉授权中心联动：注册/登录默认会打**真实 ECS**（:8902），并把云端的权益
#    与配额快照同步回本机 store。开着有两个后果：① 测试依赖外网与被占用的真实账号；
#    ② 上一次跑用掉的云端次数会同步回来，导致下一次跑到一半就被"今日配额已用尽"
#    拦住 —— 表现为**偶发失败**，极难定位。关掉后退化为纯本机账号，完全可重跑。
os.environ["VDL_CLOUD_LINK"] = "0"

from fastapi.testclient import TestClient   # noqa: E402
import app as server_app                    # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        print(f"  ✅ {name}")
    else:
        FAILS.append(name)
        print(f"  ❌ {name}{(' — ' + extra) if extra else ''}")


def register(c: TestClient, name: str) -> str:
    """注册账号并返回 Bearer token。

    🔴 网页版注册字段是 `identifier`（要求邮箱/手机号），不是 App 端的 username；
    返回体里再判 `ok` —— 云端不可达时会{"ok": True} 带 cloud_notice，仍然可用，
    但 `{ok: False}` 时 token 为空，必须报错而不是继续跑（否则全部断言失去意义）。
    """
    ident = f"{name}@example.com"
    body = {"identifier": ident, "password": "P@ssw0rd123"}
    r = c.post("/api/auth/register", json=body)
    got = (r.json() or {})
    token = (got.get("token") or "").strip()
    if not token:
        lr = c.post("/api/auth/login", json=body)
        got = (lr.json() or {})
        token = (got.get("token") or "").strip()
    if not token:
        print(f"  ⚠️  注册失败：{got.get('error') or got}")
    return token


def observe_trial_consumption(c: TestClient, token: str) -> None:
    """[3][4] 用去水印 ai 引擎观测「名额被消耗一次就没了」。

    AI 积分闸门装在「引擎可用性检查（503/400）」之前，所以只要返回码不是 402，
    就说明闸门判定通过了；反之第二次返回 402 即证明名额已耗尽。
    """
    print("\n[3] 首次使用：应放行且不扣分")
    first = c.post("/api/dw/image",
                   files={"file": ("a.png", b"\x89PNG\r\n\x1a\n", "image/png")},
                   data={"engine": "ai", "regions": '[{"x":0.1,"y":0.1,"w":0.2,"h":0.1,"op":"add"}]'},
                   headers=auth(token))
    check("首次不是 402（闸门放行或卡在更后的业务校验）",
          first.status_code != 402, f"实际 {first.status_code} {first.text[:120]}")
    st2 = c.get("/api/member/status", headers=auth(token)).json()
    check("积分仍为 0（走的是免费名额，不是扣款）",
          int(st2.get("credits_total") or 0) == 0, str(st2.get("credits_total")))
    # 🔴 别写 `int(x or 默认)` —— x 的合法值就是 0，`0 or 1` 会变成 1，
    #    断言就成了永远通过的假象。字段缺失时用 None 原值判断。
    rc = (st2.get("free_trials") or {}).get("remaining_count")
    check("名额已用掉", rc == 0, f"仍显示 {rc} ⇒ 名额没被消耗")

    print("\n[4] 第二次：必须被 402 拦下")
    second = c.post("/api/dw/image",
                    files={"file": ("a.png", b"\x89PNG\r\n\x1a\n", "image/png")},
                    data={"engine": "ai", "regions": '[{"x":0.1,"y":0.1,"w":0.2,"h":0.1,"op":"add"}]'},
                    headers=auth(token))
    check("第二次返回 402", second.status_code == 402,
          f"实际 {second.status_code} {second.text[:160]}")
    if second.status_code == 402:
        detail = (second.json() or {}).get("detail") or ""
        check("402 文案提到账号已用过一次免费体验",
              "已用过一次免费体验" in detail, f"实际文案：{detail[:160]}")
        check("402 文案不带机器码前缀", "MEMBER_QUOTA" not in detail)


def auth(token: str) -> dict:
    return {"Authorization": "Bearer " + token}


def main() -> None:
    print("网页版 AI 积分墙 · 端到端 HTTP 验证")
    print("=" * 62)
    c = TestClient(server_app.app)

    # ── 1. 匿名访问必须被挡（证明接线没把登录门禁冲掉）──────────────────
    print("\n[1] 匿名访问 AI 入口")
    anon = c.post("/api/dw/image",
                  files={"file": ("a.png", b"\x89PNG\r\n\x1a\n", "image/png")},
                  data={"engine": "ai", "regions": "[]"})
    check("去水印入口对匿名不放行", anon.status_code in (401, 403),
          f"实际 {anon.status_code}")
    anon2 = c.post("/api/subtitle/extract", json={"local_path": "/tmp/nope.mp4"})
    check("字幕提取入口对匿名不放行", anon2.status_code in (401, 403),
          f"实际 {anon2.status_code}")

    # ── 2. 注册一个新账号（零积分）─────────────────────────────────────
    print("\n[2] 新注册账号")
    token = register(c, "triale2e01")
    if not token:
        print("  ⚠️  注册不可用，后续用例无法进行（环境不支持 login/register）")
        return
    check("注册拿到 token", bool(token))

    st = c.get("/api/member/status", headers=auth(token)).json()
    check("新账号积分为 0", int(st.get("credits_total") or 0) == 0, str(st.get("credits_total")))
    ft = st.get("free_trials") or {}
    check("status 返回 free_trials", bool(ft), "前端拿不到余量 ⇒ 会员面板不会提示")
    check("默认口径是 once", ft.get("mode") == "once", f"实际 {ft.get('mode')}")
    check("初始余量是 1", int(ft.get("remaining_count") or 0) == 1,
          f"实际 {ft.get('remaining_count')}")

    # ── 3. 首次请求放行（吃免费名额）→ 4. 第二次必须 402 ───────────────
    #    用去水印的 ai 引擎观测：AI 积分闸门装在「引擎可用性检查（503/400）」之前，
    #    所以只要返回码不是 402，就说明闸门判定通过了。
    try:
        import dewatermark_ai as _ai
        ai_ok = bool(_ai.available())
    except Exception:
        ai_ok = False
    if not ai_ok:
        # 这台机器没装 onnxruntime/模型 ⇒ 路由会先 503，名额压根不会被消耗。
        # 此时 [3][4] 的观测失效，如实说明而不是报假红。
        print("  ⚠️  本机没有可用的 LaMa 引擎，跳过 [3][4] 的名额观测"
              "（换到装了 dewatermark_ai 依赖的机器上再看这两组）")
    else:
        observe_trial_consumption(c, token)

    # ── 5. opencv 引擎不计费（传统算法，不是模型推理）────────────────
    print("\n[5] opencv 引擎不该吃同一个名额")
    token2 = register(c, "triale2e02")
    if token2:
        r = c.post("/api/dw/image",
                   files={"file": ("b.png", b"\x89PNG\r\n\x1a\n", "image/png")},
                   data={"engine": "opencv", "regions": '[{"x":0.1,"y":0.1,"w":0.2,"h":0.1,"op":"add"}]'},
                   headers=auth(token2))
        st3 = c.get("/api/member/status", headers=auth(token2)).json()
        check("opencv 请求不是 402", r.status_code != 402, f"实际 {r.status_code}")
        check("opencv 不动用免费名额",
              int((st3.get("free_trials") or {}).get("remaining_count") or 0) == 1,
              "opencv 也扣了 ⇒ 免费档的用户被误伤")

    print("\n" + "=" * 62)
    if FAILS:
        print(f"❌ {len(FAILS)} 项未通过：")
        for f in FAILS:
            print("   ·", f)
        sys.exit(1)
    print("✅ 端到端验证全部通过")


if __name__ == "__main__":
    main()
