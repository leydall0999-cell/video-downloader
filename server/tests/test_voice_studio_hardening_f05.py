"""安全审计 2026-10-13（F-05）：VoiceStudio TTS / 转写 计费归属 + 跨源校验守卫。

F-05 修复前：
- `routers/voice_studio._credit_gate` 把计费打到**全局** `app.member_store`，
  登录用户调 TTS 时本人积分不减、也无法按用户追责；
- `/api/voice-studio/tts`、`/api/voice-studio/transcribe` 无任何跨源校验，
  外部网页可跨源触发本机算力 / 把用户内容外传。

本用例锁住：
1. 计费走 `app.current_member_store(request)`（按发起请求的用户归属）；
2. 两个敏感端点都调用 `app._cross_site_rejected(request)` 拒绝外部站点；
3. `_cross_site_rejected` 对外部 Origin 拒绝、对空 / 本机 Origin 放行（单元级验证）。
"""
import os
import sys
import tempfile
from pathlib import Path

_SERVER = Path(__file__).resolve().parent.parent
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

import app as server_app  # noqa: E402  先完成 app 初始化，避免 routers 循环导入

_SRC = (_SERVER / "routers" / "voice_studio.py").read_text(encoding="utf-8")

_failures = []


def check(cond, label, detail=""):
    if cond:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}  {detail}")
        _failures.append(label)


def test_credit_gate_uses_per_user_store():
    """计费必须落到发起请求的用户账本，而非全局匿名账本。"""
    check("app.current_member_store(request)" in _SRC,
          "voice_studio 计费走 current_member_store(request)（按用户归属）",
          "仍可能写死全局 member_store")
    check('_credit_gate(request, "voice_clone"' in _SRC,
          "_credit_gate 透传 request 给计费")


def _func_block(src, name):
    """截取 `def name(` 到下一个 `def ` / `@router` 之前的源码块。"""
    import re
    m = re.search(r"\n(?:async )?def " + re.escape(name) + r"\(", src)
    if not m:
        return ""
    start = m.start()
    nxt = re.search(r"\n(?:def |@router)", src[start + 1:])
    end = (start + 1 + nxt.start()) if nxt else len(src)
    return src[start:end]


def test_both_endpoints_have_cross_site_guard():
    """tts 与 transcribe 两个敏感端点都必须做跨源校验（config_save 彼时已带，不计入）。"""
    tts = _func_block(_SRC, "voice_studio_tts")
    tr = _func_block(_SRC, "voice_studio_transcribe")
    check("app._cross_site_rejected(request)" in tts,
          "/api/voice-studio/tts 做了跨源校验")
    check("app._cross_site_rejected(request)" in tr,
          "/api/voice-studio/transcribe 做了跨源校验")


def test_cross_site_rejected_behavior():
    """单元级验证 `_cross_site_rejected`：外部拒、同源/本机/无头放行。"""
    def fake_req(origin):
        class _H:
            def get(self, k, d=None):
                return origin if k == "origin" else d
        class _R:
            headers = _H()
        return _R()

    check(server_app._cross_site_rejected(fake_req("https://evil.example.com")) is True,
          "外部 Origin 被拒绝")
    check(server_app._cross_site_rejected(fake_req("")) is False,
          "无 Origin（同源 / 直连）放行")
    check(server_app._cross_site_rejected(fake_req("http://127.0.0.1:51888")) is False,
          "本机 Origin 放行")


def main():
    print("=== F-05 VoiceStudio 计费归属 + 跨源校验守卫 ===")
    test_credit_gate_uses_per_user_store()
    test_both_endpoints_have_cross_site_guard()
    test_cross_site_rejected_behavior()
    if _failures:
        print(f"\n❌ {len(_failures)} 项未通过")
        sys.exit(1)
    print("\n✅ F-05 守卫全过")


if __name__ == "__main__":
    main()
