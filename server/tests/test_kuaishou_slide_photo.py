#!/usr/bin/env python3
"""守卫：快手「图集/幻灯片」作品必须快速失败并给可读原因（2026-10-28 巡检实测）。

背景（线上实测）：`v.kuaishou.com/<code>` 短链展开后，若作品是**图集/图文**，
final URL 会带 `subBiz=BROWSE_SLIDE_PHOTO`，页面只有幻灯片 HLS 流
（`vod-rt-remux/hls-ts/*.m3u8`），**没有** progressive mp4 直链。
此前该分支不存在 ⇒ 一律进 25s 轮询 → 必然超时 → 抛
「未解析到快手视频流（APOLLO_STATE 无 mp4 直链）」：
用户白等 30s（实测 29~37s），报错还看不懂。

本守卫钉四件事（缺一即红）：
  ① 源码里有 `BROWSE_SLIDE_PHOTO` 判据；
  ② 判据在 `page.goto(...)` **之后**（URL 未展开时判不出来）且在
     25s 轮询 deadline **之前**（放到轮询后面等于没加）；
  ③ 失败文案可读（含「图集」），不再是「APOLLO_STATE 无 mp4 直链」；
  ④ 正常视频路径没被改坏：25s 轮询、video src 选流、mp4 返回结构仍在。

跑法：`python3 tests/test_kuaishou_slide_photo.py`
"""
from __future__ import annotations

import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
# 可传入被测文件路径：变异测试在**副本**上注入缺陷后再跑本守卫（绝不动真源码）
SRC = (pathlib.Path(sys.argv[1]) if len(sys.argv) > 1
       else HERE.parent.parent / "vps_worker" / "kuaishou_resolve.py")

FAILS = 0
CHECKS = 0


def check(desc: str, cond: bool, extra: str = "") -> None:
    global FAILS, CHECKS
    CHECKS += 1
    if cond:
        print(f"  ✅ {desc}")
    else:
        FAILS += 1
        print(f"  ❌ {desc} | {extra}")


def main() -> int:
    print(f"被测文件：{SRC}")
    if not SRC.is_file():
        print(f"❌ 找不到被测文件：{SRC}")
        return 1
    src = SRC.read_text(encoding="utf-8")

    # —— ① 判据存在 ——
    m_flag = re.search(r'["\']BROWSE_SLIDE_PHOTO["\']\s+in', src)
    check("存在 BROWSE_SLIDE_PHOTO 判据", bool(m_flag),
          "没找到 `\"BROWSE_SLIDE_PHOTO\" in ...` 判断")

    # —— ② 位置：goto 之后、25s 轮询之前 ——
    m_goto = re.search(r"page\.goto\(", src)
    m_poll = re.search(r"deadline\s*=\s*time\.time\(\)\s*\+\s*25", src)
    check("仍保留 25s 轮询 deadline", bool(m_poll), "轮询截止行被删/改")
    if m_flag and m_goto and m_poll:
        check("判据在 page.goto 之后（URL 已展开）", m_goto.start() < m_flag.start(),
              "判据早于 goto ⇒ 短链还没展开，永远判不出图集")
        check("判据在 25s 轮询之前（才算提前失败）", m_flag.start() < m_poll.start(),
              "判据晚于轮询 ⇒ 仍然白等 25s，等于没修")

    # —— ③ 文案可读 ——
    if m_flag:
        # 判据后 400 字符内应出现 raise + 可读原因
        window = src[m_flag.start(): m_flag.start() + 400]
        check("判据命中后立即 raise", "raise RuntimeError" in window,
              "没有 raise ⇒ 判据不生效")
        check("失败文案含「图集」二字（用户能看懂）", "图集" in window,
              "文案没提图集，用户仍看不懂为什么失败")
        check("文案不再复用「APOLLO_STATE 无 mp4 直链」", "APOLLO_STATE 无 mp4" not in window,
              "图集分支仍报通用错误")

    # —— ④ 正常视频路径未被改坏 ——
    check("仍按 video 标签 src 优先选流", "document.querySelector('video')" in src,
          "选流逻辑被删")
    check("仍返回 ext=mp4 结构", re.search(r'"ext":\s*"mp4"', src) is not None,
          "返回结构被改")
    check("仍提取标题/作者/时长", all(k in src for k in ('"title":', '"uploader":', '"duration":')),
          "元数据字段缺失")

    print()
    if FAILS:
        print(f"❌ 快手图集守卫未通过（{FAILS}/{CHECKS} 项失败）")
        return 1
    print(f"🎉 快手图集已快速失败且文案可读（{CHECKS} 项全过）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
