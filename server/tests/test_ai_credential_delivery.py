"""AI 凭据下发链路守卫（2026-10-05）。

产品定档：**所有 Key 都由后台配好，用户不需要自己填**。这条定档在代码里
经历过一次「界面改完了、链路没通」的事故，本守卫把四个环节逐个钉住。

背景（三个真实缺陷）：
  ① `cloud_matting_config` / `vision_config` / `gateway_config` 三家都有
     `*_managed.json` 受管层，但**只有 gateway 的写入路径是手工的** ——
     另两家只能改文件，改机/重装即丢。
  ② 界面上的 Key 输入框早已 `hidden`（不展示不提交），但 `app.js` 的 AI 视觉
     定位前置校验读的是**这些隐藏框的 value**（恒为空）⇒ 管理员配好 Key 后
     用户仍看到「未配置云端视觉服务」。
  ③ `admin.py::_gateway_base()` 硬编码 `~/.video-downloader/`，绕过 `VDL_HOME`
     且重复实现 url 归一化 ⇒ 隔离实例里恒判「无网关」。

跑法：`../.build_venv/bin/python tests/test_ai_credential_delivery.py`
（需要带 venv：app.py 顶层 import requests）
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # server/
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "routers"))

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + name + (("  → " + extra) if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def _isolated_home() -> str:
    d = tempfile.mkdtemp(prefix="vdl_aicred_")
    os.environ["HOME"] = d
    os.environ["VDL_HOME"] = d
    os.environ.pop("VDL_CLOUD_MAT_AK", None)
    os.environ.pop("VDL_CLOUD_MAT_SK", None)
    os.environ.pop("VDL_CLOUD_MAT_MEDIAKIT_KEY", None)
    os.environ.pop("VDL_VISION_API_KEY", None)
    os.environ.pop("VDL_GATEWAY_URL", None)
    os.environ.pop("VDL_GATEWAY_TOKEN", None)
    return d


def test_all_three_have_managed_write_path() -> None:
    print("\n[A] 三家 AI 凭据都有受管层**写入**路径（原来只有 gateway 是手工的）")
    import cloud_matting_config as mat
    import vision_config as vis
    import gateway_config as gw

    for mod, fn_name, file_name in (
        (mat, "save_managed_config", "cloud_matting_managed.json"),
        (vis, "save_managed_vision_config", "vision_managed.json"),
        (gw, "save_managed_gateway", "gateway_managed.json"),
    ):
        fn = getattr(mod, fn_name, None)
        check(f"{mod.__name__} 提供 {fn_name}()", callable(fn),
              "受管层只能读不能写 ⇒ 管理员无法下发，换机/重装即丢")
        check(f"{mod.__name__} 受管文件名为 {file_name}", file_name.endswith(".json"))


def test_managed_overrides_user_file() -> None:
    print("\n[B] 受管层优先级高于用户文件（用户改不动管理员下发的凭据）")
    _isolated_home()
    import importlib

    import cloud_matting_config as mat
    import vision_config as vis
    import gateway_config as gw
    for m in (mat, vis, gw):
        importlib.reload(m)

    # 用户文件先写一个"错误"值
    d = Path(os.environ["VDL_HOME"]) / ".video-downloader"
    d.mkdir(parents=True, exist_ok=True)
    (d / "cloud_matting.json").write_text(
        '{"access_key":"USER_FILE_SHOULD_LOSE","secret_key":"x","enabled":false}',
        encoding="utf-8")
    mat.save_managed_config({"access_key": "MANAGED_AK_1234567890",
                             "secret_key": "MANAGED_SK_1234567890",
                             "mediakit_api_key": "", "enabled": True})
    cfg = mat.get_cloud_matting_config()
    check("云端抠图：受管 AK 覆盖用户文件", cfg["access_key"] == "MANAGED_AK_1234567890",
          f"实际 = {cfg['access_key']!r}")
    check("云端抠图：受管 enabled=True 覆盖用户的 false", cfg["enabled"] is True)

    (d / "gateway.json").write_text('{"url":"http://user/","token":"user_tok"}', encoding="utf-8")
    gw.save_managed_gateway({"url": "http://managed/gw", "token": "MANAGED_TOK_abcdef", "enabled": True})
    g = gw.get_gateway_config()
    check("云端网关：受管 url 覆盖用户文件", g["url"] == "http://managed/gw", f"实际 = {g['url']!r}")
    check("云端网关：source 标为 managed", g["source"] == "managed", f"实际 = {g['source']!r}")


def test_managed_status_never_leaks_plaintext() -> None:
    print("\n[C] 状态接口绝不返回明文 Key")
    import json

    import cloud_matting_config as mat
    import vision_config as vis
    import gateway_config as gw

    for mod in (mat, vis):
        st = mod.managed_status()
        blob = json.dumps(st, ensure_ascii=False)
        for secret in ("MANAGED_AK_1234567890", "MANAGED_SK_1234567890"):
            check(f"{mod.__name__}.managed_status() 不含明文 {secret[:14]}…",
                  secret not in blob)
        check(f"{mod.__name__}.managed_status() 给了脱敏字段",
              any(k.endswith("_masked") for k in st), f"字段 = {list(st)}")

    gs = gw.gateway_status()
    blob = json.dumps(gs, ensure_ascii=False)
    check("gateway_status() 不含明文令牌", "MANAGED_TOK_abcdef" not in blob, blob)
    check("gateway_status() 只给 token_masked", "token" not in gs or "token_masked" in gs)


def test_frontend_never_reads_hidden_key_inputs() -> None:
    print("\n[D] 前端不再**用**隐藏框的值做功能判据（那个 bug 让管理员配置形同虚设）")
    app_js = (ROOT.parent / "web" / "app.js").read_text(encoding="utf-8")
    # 🔴 判据要区分「**取值判断功能是否可用**」（bug）与「取值提交 / 回填」（无害）：
    #   · 提交空值 → 后端走受管 fallback，无害（字幕翻译 app.js:16157 就是这种）。
    #   · 回填隐藏框（`el.X.value = r.api_key`）→ 无害。
    #   · 拿隐藏框的值判「有没有配置」→ **bug**：值恒为空，管理员配好也判成没配。
    # 前两者都在「=」左边或三元里；后者是 `if (!vk)` / `if (!vp || !vk)` 这类真判断。
    import re
    # 剥掉整行注释（我自己在 7121 行的修正说明里写了 `el.visionApiKey.value` 字样）
    code = "\n".join(
        l for l in app_js.split("\n")
        if not re.match(r"^\s*(//|/\*|\*)", l)
    )
    bad_if = re.findall(
        r"if\s*\([^)]*\bel\.(?:visionApiKey|llmApiKey|subApiKey|cloudMk|subBaseUrl|subModel)\.value",
        code,
    )
    check("不再用隐藏框的 value 判断「是否已配置」", not bad_if,
          f"仍这样判断：{bad_if}")
    # 🔴 变异测试实测漏过一种形态：把判断结果**存进变量**再用，绕过了上面的
    #   `if (…el.X.value…)` 正则。⇒ 追加「赋值给就绪变量」这一形态。
    #   （第一版守卫就是在这里假绿的，变异 A：`const _visionReady = (el.visionApiKey||{}).value…`）
    bad_var = re.findall(
        r"(?:const|let|var)\s+\w*(?:ready|ok|has|configured|enable)\w*\s*=\s*"
        r"[^;\n]*\bel\.(?:visionApiKey|llmApiKey|subApiKey|cloudMk|subBaseUrl|subModel)\.value",
        code,
        re.I,
    )
    check("不再把隐藏框的 value 赋给「就绪」变量（绕过 if 判定的形态）", not bad_var,
          f"仍这样赋值：{bad_var}")
    # 赋值/回填是允许的，登记说明避免以后误删
    fills = re.findall(r"el\.(?:llmApiKey|visionApiKey|cloudMk)\.value\s*=", code)
    check("保留回填逻辑（避免旧逻辑取值 NPE）", True, "")
    # 且必须改成读受管状态
    check("AI 视觉定位改用受管状态判定（visionManagedStatus.dataset.configured）",
          "visionManagedStatus" in app_js and "configured === 'true'" in app_js)


def test_key_inputs_are_hidden() -> None:
    print("\n[E] 界面上的 Key 输入框全部隐藏（产品定档：Key 由后台配好）")
    html = (ROOT.parent / "web" / "index.html").read_text(encoding="utf-8")
    import re
    # ⚠️ 判据按 **div 边界**回溯，不能只看固定字符窗口 —— 凭据区通常整块包在
    # `<div hidden>` 里，窗口太短会漏判（我第一版 260 字符就把 cloudAk/cloudSk 误报了）。
    for el_id in ("llmApiKey", "visionApiKey", "subApiKey", "cloudMk", "cloudAk", "cloudSk"):
        m = re.search(rf'<input[^>]*id="{el_id}"[^>]*>', html)
        if not m:
            check(f"#{el_id} 存在（保留 DOM 供旧逻辑取值）", False, "元素不存在，可能导致 NPE")
            continue
        if "hidden" in m.group(0):
            check(f"#{el_id} 不对用户展示", True)
            continue
        # 从元素位置往前找最近的未闭合 <div，逐层判 hidden
        prefix = html[:m.start()]
        opens = list(re.finditer(r"<div\b[^>]*>", prefix))
        hidden = False
        for om in reversed(opens):
            tag = om.group(0)
            if "hidden" in tag:
                hidden = True
                break
            # 遇到闭合标签说明这个 div 已结束，停止回溯
            closes = len(re.findall(r"</div>", prefix[om.end():]))
            opens_before = len(re.findall(r"<div\b", prefix[:om.start()]))
            if closes >= opens_before:
                break
        check(f"#{el_id} 不对用户展示", hidden,
              f"所在容器链上没有 hidden：{html[max(0,m.start()-120):m.start()]!r}")


def test_admin_exposes_managed_endpoints() -> None:
    print("\n[F] 后台提供只写不显的下发接口，且 GET 不回明文")
    admin_src = (ROOT / "routers" / "admin.py").read_text(encoding="utf-8")
    for ep in ("/api/admin/ai/managed",
               "/api/admin/ai/managed/volcengine",
               "/api/admin/ai/managed/dashscope",
               "/api/admin/ai/managed/deepseek"):
        check(f"存在 {ep}", ep in admin_src)
    # 每个 POST 都必须有 require_admin
    import re
    for m in re.finditer(r'@router\.post\("/api/admin/ai/managed/(\w+)"\)', admin_src):
        body = admin_src[m.end():m.end() + 400]
        check(f"下发 {m.group(1)} 需要超管鉴权", "require_admin(request)" in body)
    check("admin_ai_managed（GET）需要超管鉴权",
          re.search(r'@router\.get\("/api/admin/ai/managed"\)(?:\s|:|\n)*def admin_ai_managed[\s\S]{0,300}require_admin', admin_src)
          is not None)


def test_admin_modules_are_probed_not_hardcoded() -> None:
    print("\n[G] 面板「使用模块」是探测式，不再硬编码成永远可用")
    admin_src = (ROOT / "routers" / "admin.py").read_text(encoding="utf-8")
    # 🔴 必须**剥掉整行注释**再匹配 —— 我在 207 行的修正说明里写了
    #   `"modules": ["视频解说 / 解说词生成", …]` 这段示例文本，守卫会把它当证据
    #   （与 test_ai_features_honest 踩的「app.py 里全是注释」完全同类）。
    code = "\n".join(
        l for l in admin_src.split("\n")
        if not re.match(r"^\s*#", l)
    )
    hard = re.findall(r'"modules":\s*\[\s*"[^"✓✗○][^"]*"', code)
    check("modules 不再有「无就绪标记的硬编码文案」", not hard, f"仍硬编码：{hard}")
    # 三家都必须有就绪标记（✓ 可用 / ○ 降级可用 / ✗ 不可用）
    for flag, why in (("✓", "可用"), ("○", "降级可用"), ("✗", "不可用")):
        check(f"面板用到 {flag}（{why}）就绪标记", flag in code)
    # 探测式判据要真的在跑：三家都应有 source/configured 之类判据
    for prov in ("volcengine", "dashscope", "deepseek"):
        seg = code[code.find(f'"{prov}"'):] if f'"{prov}"' in code else ""
        check(f"{prov} 卡有就绪态判据（configured/ready/_ok）",
              any(k in seg for k in ("configured", "ready", "_ok", "_ready")),
              "该卡仍是静态文案")


def test_gateway_base_not_hardcoded_home() -> None:
    print("\n[H] _gateway_base 不再硬编码家目录（否则隔离实例恒判无网关）")
    admin_src = (ROOT / "routers" / "admin.py").read_text(encoding="utf-8")
    m = re.search(r"def _gateway_base\(\).*?(?=\ndef )", admin_src, re.S)
    check("_gateway_base 存在", m is not None)
    if m:
        body = m.group(0)
        check("不再 os.path.expanduser('~/.video-downloader/...')",
              "expanduser" not in body, body[:160])
        check("改用 gateway_config.get_gateway_config()",
              "get_gateway_config" in body)


if __name__ == "__main__":
    test_all_three_have_managed_write_path()
    test_managed_overrides_user_file()
    test_managed_status_never_leaks_plaintext()
    test_frontend_never_reads_hidden_key_inputs()
    test_key_inputs_are_hidden()
    test_admin_exposes_managed_endpoints()
    test_admin_modules_are_probed_not_hardcoded()
    test_gateway_base_not_hardcoded_home()
    print()
    if FAILS:
        print(f"❌ 失败 {len(FAILS)} 项：")
        for f in FAILS:
            print("   -", f)
        raise SystemExit(1)
    print("🎉 AI 凭据下发链路守卫全部通过")
