#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""嗅探链路「清晰度」透传守卫（2026-10-01 用户实测「目前没法选择分辨率」）。

现象：用户从扩展 popup 点「解析并下载」把 YouTube 视频页交给桌面端后，任务卡直接显示
「已选清晰度：最佳画质（自动）」，**全链路没有任何地方能选分辨率**。
根因：扩展 → /api/sniffer/send → 桌面端 downloadItem 的 quality 被写死成 'best'
（嗅探面板里每条「下载 / 解析并下载」同样写死）。

修法（三处同时到位，缺一处就是「选了不生效」）：
  ① 后端 cdp_sniffer.add_manual 透传白名单内的 quality（非法/缺省置 ""，由桌面端兜底）；
  ② 桌面端 downloadItem 经 qualityForDownload(it) 下发：条目自带优先，其次面板下拉，
     免费用户的「自动」档再由 2026-10-02 的会员门槛封顶到 1080P；
  ③ 扩展 popup 有自己的清晰度下拉，随发送上报并落 chrome.storage.local。

清空边界（有意为之，不是漏做）：只对「视频页 / HLS 清单」这类**可解析出多档**的条目
生效；直链/分片本身就是单一流，带清晰度反而可能挑不到流（如给 4K 直链选 1080）。

补充（2026-10-02 真机实测，只做上面三处**还不够**）：扩展 popup 提交的是页面 URL，
而 classify_media 只认媒体后缀 → 条目落成 "media" → 桌面端按直链处理、把 quality 丢掉
（实测：选 1080 建出的任务仍显示「最佳画质（自动）」）。故再补三处：
  ④ 后端 add_manual 采信白名单 ALLOWED_KIND 内的来源方 kind（页面 URL 才不会被误判）；
  ⑤ 扩展 postSend 随包上报 kind；
  ⑥ 桌面端 sniffQuality 让「显式 quality」先于 kind 判断返回（纵深防御）。

运行：
    .build_venv/bin/python tests/test_sniffer_quality_wiring.py
"""
import json
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SERVER_DIR = str(Path(__file__).resolve().parents[1])
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import cdp_sniffer  # noqa: E402
from downloader import AUDIO_KEY, BEST_KEY, M4A_KEY, QUALITY_PRESETS, WEBM_KEY  # noqa: E402

DESKTOP_JS = _REPO / "web" / "js" / "desktop-app.js"
POPUP_JS = _REPO / "extension" / "popup.js"
POPUP_HTML = _REPO / "extension" / "popup.html"
MANIFEST = _REPO / "extension" / "manifest.json"

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f"  → {extra}" if extra else ""))


def _add(payload):
    """add_manual 只入队（不碰网络/浏览器），可直接调。"""
    return cdp_sniffer.SNIFFER.add_manual(payload)


def _options_in(html_or_js, select_id):
    m = re.search(r'<select id="%s".*?</select>' % re.escape(select_id), html_or_js, re.S)
    if not m:
        return None
    return re.findall(r'<option value="([^"]*)"', m.group(0))


def main():
    print("▶ 嗅探链路「清晰度」透传（① 后端透传 ② 桌面端使用 ③ 两端都有下拉）")

    src = DESKTOP_JS.read_text(encoding="utf-8")
    pjs = POPUP_JS.read_text(encoding="utf-8")
    phtml = POPUP_HTML.read_text(encoding="utf-8")

    # ---------- ① 后端：白名单与下载器档位单一真源 ----------
    want = {str(h) for h, _ in QUALITY_PRESETS} | {BEST_KEY, AUDIO_KEY, WEBM_KEY, M4A_KEY}
    check("ALLOWED_QUALITY 与 downloader 清晰度档一致（防两端漂移）",
          set(cdp_sniffer.ALLOWED_QUALITY) == want,
          f"diff={set(cdp_sniffer.ALLOWED_QUALITY) ^ want}")

    it = _add({"url": "https://www.youtube.com/watch?v=x", "quality": "1080"})
    check("白名单内的 quality 原样透传（1080）", it.get("quality") == "1080", repr(it.get("quality")))
    check("quality 归一化（'  BEST ' → best）",
          _add({"url": "https://e.com/a", "quality": "  BEST "}).get("quality") == "best")
    check("非法 quality 置空（'9999'）",
          _add({"url": "https://e.com/b", "quality": "9999"}).get("quality") == "")
    check("非法 quality 置空（'ultra'）",
          _add({"url": "https://e.com/c", "quality": "ultra"}).get("quality") == "")
    check("缺省 quality 置空（由桌面端兜底）",
          _add({"url": "https://e.com/d"}).get("quality") == "")
    check("改动没破坏回执凭据 send_id", bool(it.get("send_id")))

    # ---------- ①b 后端：条目类型 kind（2026-10-02 真机实测补） ----------
    # 现象：扩展 popup 选 1080 后建出的任务仍是「最佳画质（自动）」。
    # 根因：popup 提交的是**页面 URL**（无 .mp4/.m3u8 后缀）→ classify_media 判不出 →
    # 落回 "media" → 桌面端 sniffQuality 视为直链 → 丢弃 quality。修法＝采信来源方 kind。
    check("ALLOWED_KIND 覆盖旧有语义 + 新增 page",
          set(cdp_sniffer.ALLOWED_KIND) == {"media", "playlist", "segment", "page"})
    pg = _add({"url": "https://www.youtube.com/watch?v=x", "quality": "1080", "kind": "page"})
    check("页面 URL 采信 kind=page（否则桌面端当直链丢掉清晰度）",
          pg.get("kind") == "page", repr(pg.get("kind")))
    check("kind 归一化（' PAGE ' → page）",
          _add({"url": "https://e.com/h", "kind": " PAGE "}).get("kind") == "page")
    check("缺省 kind 仍回落 media",
          _add({"url": "https://e.com/e"}).get("kind") == "media")
    check("非法 kind 不采信（回落 media）",
          _add({"url": "https://e.com/f", "kind": "hack"}).get("kind") == "media")
    # 服务端判定优先：.m3u8 就是 playlist，不许被来源方 hint 改写成 media
    check("服务端判定优先于 hint（.m3u8 + hint=media 仍是 playlist）",
          _add({"url": "https://e.com/g.m3u8", "kind": "media"}).get("kind") == "playlist")
    check("hint 不会把已有判定降级（.mp4 + hint=page 仍是 media）",
          _add({"url": "https://e.com/i.mp4", "kind": "page"}).get("kind") == "media")

    # ---------- ② 桌面端：真的用了它，且不再写死 ----------
    # 2026-10-02：downloadItem 改为经 qualityForDownload(it) 下发（= sniffQuality 逐条目
    # 定档 + 免费用户的「自动」封顶 1080P）。这里只要「档位判据唯一」这件事不被破坏：
    # 谁都不许在 downloadItem 里另抄一份判据，也不许绕过封装直接塞 sniffQuality。
    check("downloadItem 用 qualityForDownload(it) 建任务",
          "const dlQuality = await qualityForDownload(it);" in src
          and re.search(r"quality: dlQuality,", src) is not None)
    check("档位判据仍只有 snifferQuality 一处（qualityForDownload 内部调用它）",
          "const q = sniffQuality(it);" in src
          and "quality: sniffQuality(it)," not in src)
    check("旧的写死 quality: 'best' 已消失",
          "quality: 'best'" not in src)
    check("sniffQuality 定义存在",
          "const sniffQuality = (it) =>" in src)
    # 直链/分片不套清晰度（否则给 4K 直链选 1080 会挑不到流）
    check("只对「视频页 / HLS 清单」生效，直链走 best",
          "if (kind !== 'page' && kind !== 'playlist') return 'best';" in src)
    # 条目自带（扩展里选的）优先：显式值必须先于 kind 判断返回，否则页面 URL 被判成
    # media 时会把用户选的清晰度无声丢掉（2026-10-02 真机实测发现，见下面 ①b）。
    i_item = src.find("const sniffQuality = (it) =>")
    i_explicit = src.find("const explicit = (it && it.quality) || '';", i_item)
    i_use = src.find("if (explicit) return explicit;", i_item)
    i_kind = src.find("if (kind !== 'page' && kind !== 'playlist') return 'best';", i_item)
    check("条目自带 quality 优先（显式值先于 kind 判断返回）",
          i_item >= 0 and i_explicit > i_item and i_use > i_explicit and i_kind > i_use,
          f"item={i_item} explicit={i_explicit} use={i_use} kind={i_kind}")

    # ---------- ③ 两端都要有下拉（否则用户无处可选） ----------
    d_opts = _options_in(src, "sniffQuality")
    e_opts = _options_in(phtml, "sendQuality")
    check("桌面面板含 #sniffQuality 下拉", d_opts is not None)
    check("扩展 popup 含 #sendQuality 下拉", e_opts is not None)
    check("两端档位一致且含 best/1080/720/audio",
          d_opts is not None and d_opts == e_opts
          and {"best", "1080", "720", "audio"} <= set(d_opts),
          f"desktop={d_opts} ext={e_opts}")
    check("档位覆盖后端所有 QUALITY_PRESETS 高度",
          d_opts is not None and {str(h) for h, _ in QUALITY_PRESETS} <= set(d_opts))

    # 面板选择要记住（否则每次开 App 都要重选）
    check("桌面面板选择持久化（localStorage）",
          "vdl.sniff.quality" in src and "localStorage.setItem(SNIFF_Q_KEY" in src)
    check("建任务后 toast 回显清晰度（用户能确认选中的生效了）",
          "const Q_LABEL" in src and "已加入下载队列（" in src)

    # 扩展：随发送上报 + 记住选择 + 即时回显
    check("postSend 上报 quality（qualityForItem）",
          "quality: qualityForItem(it)," in pjs)
    check("扩展只对「页面 / 清单」上报（直链不带）",
          "if (k !== 'page' && k !== 'playlist') return '';" in pjs)
    check("扩展改选落 chrome.storage.local",
          "chrome.storage.local.set({ sendQuality: state.quality })" in pjs)
    check("扩展改选即时回显（.empty-q）",
          ".empty-q" in pjs and "将以「" in pjs)
    check("扩展初始化读回记忆值",
          "chrome.storage.local.get([QUALITY_STORE_KEY]" in pjs)
    # 只上报 quality 不够：kind 不随包走的话，后端判不出「页面」→ 上一条白上报
    check("popup postSend 随包上报 kind（页面 URL 判不出类型）",
          "kind: it.kind || ''" in pjs)

    # 扩展改了 popup → 版本必须 bump，用户才看得出「我 reload 成功了」
    ver = json.loads(MANIFEST.read_text(encoding="utf-8")).get("version", "")


    def _ver_tuple(v):
        return tuple(int(x) for x in re.findall(r"\d+", v)[:3])

    check("扩展版本已 bump（≥1.0.43，popup 改动必须出新版本）",
          _ver_tuple(ver) >= (1, 0, 43), ver)

    print("")
    print("=========================================")
    print(f"  通过: {PASS}   失败: {FAIL}")
    print("=========================================")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
