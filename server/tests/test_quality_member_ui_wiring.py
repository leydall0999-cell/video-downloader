"""清晰度会员门槛「接线」守卫（2026-10-02 用户定档）。

行为测试（test_quality_member_gate.py）证明后端会拦；本文件钉住**三端接线**，
防止「后端拦住了、前端却没弹会员中心」或「前端又偷偷绕过门槛」这类回归：

后端：
  1. core.py 必须真的定义门槛函数，且**两个**下载入口（/api/download、/api/batch）都调它
  2. 拦截走 402 + MEMBER_QUOTA| 前缀（前端所有会员墙分支都认这个前缀）

前端（桌面壳）：
  3. web/app.js 必须暴露 window.__vdlOpenMemberCenter（desktop-app.js 在闭包外，只能走挂载点）
  4. web/app.js 的 402 文案必须按「清晰度」分岔（否则清晰度不够被说成额度明天刷新）
  5. web/js/desktop-app.js 建任务必须用 qualityForDownload 下发（不得退回 sniffQuality）
  6. qualityForDownload 必须：显式档原样下发、best+免费 → 1080、且只对 page/playlist 降级
  7. desktop-app.js 遇 MEMBER_QUOTA| 必须调 openMember()（含扩展回流的 silent 路径）
  8. 会员态读不到时按会员放行（fail-open），不得把付费用户的高清档封掉
  9. index.html 里 memberBadge / memberModal 必须真实存在（fallback 与弹窗都靠它）

运行：
    cd server && python tests/test_quality_member_ui_wiring.py
"""
import os
import re
import sys

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(_SERVER_DIR)
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

FAILS = []


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        print(f"  ✅ {name}")
    else:
        FAILS.append(name)
        print(f"  ❌ {name}" + (f"  → {extra}" if extra else ""))


def _read(rel: str) -> str:
    with open(os.path.join(_REPO, rel), "r", encoding="utf-8") as f:
        return f.read()


def test_backend_wiring():
    src = _read(os.path.join("server", "routers", "core.py"))
    check("core.py 定义 _quality_gate_error", "def _quality_gate_error(" in src)
    check("FREE_MAX_QUALITY 就是 1080",
          re.search(r"^FREE_MAX_QUALITY\s*=\s*1080", src, re.M) is not None)
    n_call = src.count("_quality_gate_error(request, payload.quality)")
    check("两个下载入口都挂上门槛（/api/download + /api/batch）", n_call == 2, f"实际调用 {n_call} 次")
    check("拦截用 402 + MEMBER_QUOTA| 前缀（前端认这个码）",
          "status_code=402, detail='MEMBER_QUOTA|' + _qerr" in src)
    # 门槛必须早于建任务：'quality' 校验之后、store.create 之前各出现一次
    for name, anchor in (("create_download", "def create_download("), ("create_batch", "def create_batch(")):
        seg = src.split(anchor, 1)[1]
        seg = seg.split("\n@router.", 1)[0]
        i_gate = seg.find("_quality_gate_error(request, payload.quality)")
        i_create = seg.find("store.create(")
        check(f"{name} 的门槛在建任务之前", 0 <= i_gate < i_create, f"gate@{i_gate} create@{i_create}")


def test_frontend_wiring():
    app = _read(os.path.join("web", "app.js"))
    check("app.js 暴露 __vdlOpenMemberCenter 挂载点",
          "window.__vdlOpenMemberCenter = openMemberCenter;" in app)
    check("app.js 的 402 文案按「清晰度」分岔",
          "/清晰度/.test(tip)" in app and "开通下载会员即可解锁 2K/4K 原画" in app)
    check("app.js 仍保留每日次数口径的文案",
          "免费额度每日 24:00 刷新" in app)

    dp = _read(os.path.join("web", "js", "desktop-app.js"))
    check("desktop-app.js 定义 openMember 并优先走挂载点",
          "const openMember = () => {" in dp and "window.__vdlOpenMemberCenter" in dp)
    check("desktop-app.js 建任务改用 qualityForDownload 下发",
          "const dlQuality = await qualityForDownload(it);" in dp and "quality: dlQuality," in dp)
    check("desktop-app.js 不再直接 sniffQuality 下发（会被免费门槛绕过）",
          "quality: sniffQuality(it)," not in dp)
    check("qualityForDownload 只在 best 时考虑降级",
          "if (q !== 'best') return q;" in dp)
    # 只取 qualityForDownload 的函数体来判断（sniffQuality 里本来就有 kind !== 'playlist'，
    # 那是另一处的语义，不能拿来当判据 —— 整文件 grep 会误判）
    _body = dp.split("const qualityForDownload = async (it) => {", 1)
    qbody = _body[1].split("\n    };", 1)[0] if len(_body) == 2 else ""
    check("qualityForDownload 函数体存在", bool(qbody.strip()))
    check("qualityForDownload 只对 kind=page 降级（直链/分片/HLS 清单降级会挑不到流）",
          "if (((it && it.kind) || '') !== 'page') return q;" in qbody)
    check("qualityForDownload 不再对 playlist 降级（HLS 清单末档选择器会挑不到流）",
          "playlist" not in qbody, qbody[:200])
    check("免费档落地 1080P", re.search(r"if \(await isDownloadMember\(\)\) return q;\s*return '1080';", dp) is not None)
    check("会员态读不到按会员放行（fail-open）",
          "member = true;" in dp and "catch (e) { /* 读不到 → 按会员放行 */ }" in dp)
    check("遇 MEMBER_QUOTA| 会弹会员中心",
          "if (msg.indexOf('MEMBER_QUOTA|') === 0) {" in dp and "openMember();" in dp)
    check("回执给扩展前剥掉内部前缀", "msg = msg.split('|').slice(1).join('|')" in dp)

    html = _read(os.path.join("web", "index.html"))
    check("index.html 有 memberBadge（openMember 的兜底入口）", 'id="memberBadge"' in html)
    check("index.html 有 memberModal（真弹窗容器）", 'id="memberModal"' in html)


def test_extension_wiring():
    js = _read(os.path.join("extension", "popup.js"))
    check("popup 保留用户选的清晰度（render 不再 state = st）",
          "state = st || {};" in js and "keptQuality" in js)
    check("popup 有 syncQualityUI（下拉与回显必须一致）", "function syncQualityUI()" in js)
    check("popup 回显用 syncQualityUI 而非仅改一处",
          js.count("syncQualityUI()") >= 3, f"出现 {js.count('syncQualityUI()')} 次（定义+调用）")
    man = _read(os.path.join("extension", "manifest.json"))
    check("扩展版本已 bump（改了 popup 必须 bump）",
          # 1.0.45 起的两位数及以上 patch 号；写死区间（如 4[5-9]）到 1.0.50 就会误报
          re.search(r'"version":\s*"1\.0\.\d{2,}"', man) is not None,
          (re.search(r'"version":\s*"([^"]+)"', man) or [None, "?"])[1])


if __name__ == "__main__":
    print("▶ 清晰度会员门槛接线守卫")
    test_backend_wiring()
    test_frontend_wiring()
    test_extension_wiring()
    print("")
    if FAILS:
        print(f"❌ {len(FAILS)} 项接线守卫失败，构建不应发布")
        sys.exit(1)
    print("🎉 清晰度会员门槛接线守卫全部通过")
