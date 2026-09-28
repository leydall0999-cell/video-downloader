#!/usr/bin/env python3
"""桌面端上传端点必须同源（2026-09-29，与网页版 test_upload_body_limit.py 同源守卫对齐）。

背景
----
`web/app.js` 的 `UC_UPLOAD_ENDPOINTS` 曾有第二项
`https://web-production-b9993.up.railway.app`（早期云上同源备份）。该应用自
2026-09-11 起已不存在，而分片选路在「样本不足（<4 片）」时按奇偶分流、重试又固定
切到「另一条」⇒ 每个奇数下标分片都要先撞一次死主机；样本攒够后仍有 20% 概率被
「探索」分去撞。用户可见的表现是上传变慢 + 偶发「分片 N/…」失败重试。

为什么不能随便加一个不同源的端点
--------------------------------
分片上传的分片是**按 (upload_id, index) 落在接收方磁盘**上的，finish 在该节点上
合并。两个端点若不同后端、不同分片存储，分片会被劈成两半，finish 必然报
「分片不完整」。网页版当年敢用双端点，正是因为 CF 域与 Railway 域指向同一后端。

本测试钉住：`UC_UPLOAD_ENDPOINTS` 只能出现同源项。
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.dirname(HERE)
REPO = os.path.dirname(SERVER_DIR)
APP_JS = os.path.join(REPO, "web", "app.js")


def test_upload_endpoints_same_origin_only():
    with open(APP_JS, encoding="utf-8") as fh:
        src = fh.read()
    m = re.search(r"const UC_UPLOAD_ENDPOINTS = \[([^\]]*)\]", src)
    assert m, "web/app.js 里找不到 UC_UPLOAD_ENDPOINTS（被改名了？守卫要跟着改）"
    entries = [e.strip() for e in m.group(1).split(",") if e.strip()]
    assert entries, "UC_UPLOAD_ENDPOINTS 为空，分片没有可用的上传端点"
    for e in entries:
        assert e == "location.origin", (
            "上传端点出现了非同源项 %r。\n"
            "非同源端点只有在「与主站同一后端、同一份分片存储」时才成立；一旦它不可用，"
            "分片会被劈到两个存储上（finish 报「分片不完整」），而选路在样本不足时还会"
            "按奇偶分流、让每个奇数下标分片都去撞它。新增前先确认分片存储是同一份。"
            % e)
    print("✅ 桌面端上传端点仅同源（%d 项）" % len(entries))


def test_chunk_upload_helper_reports_gateway_413():
    """非 JSON 的 413 要说清是网关拦的，不能与应用自己的 413 混为一谈。"""
    with open(APP_JS, encoding="utf-8") as fh:
        src = fh.read()
    assert "上传被网关拒绝（HTTP 413·单次体积超限）" in src, (
        "分片上传的 413 分支缺少「网关拦截」的区分文案：非 JSON 的 413 是网关（nginx/CF）"
        "在到达应用前拒掉的，与应用自己带 JSON detail 的 413（单个分片超过大小上限）语义不同")
    print("✅ 网关 413 与应用 413 已区分文案")


if __name__ == "__main__":
    test_upload_endpoints_same_origin_only()
    test_chunk_upload_helper_reports_gateway_413()
    print("\n🎉 桌面端上传端点守卫全部通过")
