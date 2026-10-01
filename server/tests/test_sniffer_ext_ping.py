#!/usr/bin/env python3
"""浏览器嗅探「扩展心跳」回归测试（2026-10-01，纯离线）。

背景：嗅探面板简化为「开始嗅探」单按钮 + 扩展推荐主路径后，用户装完扩展
需要立刻看到「扩展已连接 ✓」的确定性反馈，而不是靠猜。
链路：扩展 SW 心跳 POST /api/sniffer/ext-ping → SNIFFER.mark_ext_seen()
→ status() 返回 ext_online（≤300s 内算在线；MV3 alarms 最小周期 1 分钟，留足余量）。

运行：
    cd server && python tests/test_sniffer_ext_ping.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 测试隔离：绝不写用户家目录
os.environ["VDL_DATA_DIR"] = tempfile.mkdtemp(prefix="vdl_test_extping_")

import cdp_sniffer  # noqa: E402
from routers import sniffer as rs  # noqa: E402


def test_ext_ping_flow():
    # 1) 初始（从未收到心跳）：ext_online 必须为 False，面板显示「未连接」
    st0 = cdp_sniffer.SNIFFER.status()
    assert st0.get("ext_online") is False, f"未心跳时 ext_online 应为 False: {st0}"
    # 2) 路由层 ping → ok，且 status 翻转为在线
    r = rs.sniffer_ext_ping()
    assert r.get("ok") is True
    st1 = cdp_sniffer.SNIFFER.status()
    assert st1.get("ext_online") is True, f"心跳后 ext_online 应为 True: {st1}"
    # 3) 心跳过期（>300s）→ 回落离线，不会永远在线
    cdp_sniffer.SNIFFER._ext_seen = time.time() - 400
    st2 = cdp_sniffer.SNIFFER.status()
    assert st2.get("ext_online") is False, f"心跳过期后应回落 False: {st2}"
    print("✅ 扩展心跳：未连→在线→过期离线 三态判定正确，路由层直调通过")


def test_ext_version_report():
    # 4) 心跳自报版本（1.0.37+）：status().ext_version 跟随最后一次心跳
    cdp_sniffer.SNIFFER._ext_seen = time.time()  # 复活在线态
    r = rs.sniffer_ext_ping(payload={"version": "1.0.37"})
    assert r.get("ok") is True
    st = cdp_sniffer.SNIFFER.status()
    assert st.get("ext_version") == "1.0.37", f"心跳版本应入库: {st}"
    # 5) 旧版扩展（body 不带 version / 非 dict）→ 之前报过的版本保留、不报错
    r2 = rs.sniffer_ext_ping()
    assert r2.get("ok") is True
    assert cdp_sniffer.SNIFFER.status().get("ext_version") == "1.0.37"
    # 6) 脏数据防御：超长/非字符串版本号被截断或忽略
    rs.sniffer_ext_ping(payload={"version": "x" * 40})
    v = cdp_sniffer.SNIFFER.status().get("ext_version")
    assert isinstance(v, str) and len(v) <= 20, f"版本号应截断到 20 字符内: {v!r}"
    print("✅ 心跳版本自报：入库/兼容旧版/脏数据防御 全过")


def test_ext_telemetry():
    # 7) 遥测（1.0.38+）：心跳带 captured/pushed → status 暴露，排障一眼分清断层
    rs.sniffer_ext_ping(payload={"version": "1.0.38", "captured": 12, "pushed": 5})
    st = cdp_sniffer.SNIFFER.status()
    assert st.get("ext_captured") == 12 and st.get("ext_pushed") == 5, f"遥测未入库: {st}"
    # 旧版心跳（无字段）→ 归零不报错；脏数据（字符串/负数）→ 防御
    rs.sniffer_ext_ping(payload={"version": "1.0.38"})
    st2 = cdp_sniffer.SNIFFER.status()
    assert st2.get("ext_captured") == 0 and st2.get("ext_pushed") == 0
    rs.sniffer_ext_ping(payload={"captured": "x", "pushed": -3})
    st3 = cdp_sniffer.SNIFFER.status()
    assert st3.get("ext_captured") == 0 and st3.get("ext_pushed") == 0
    print("✅ 心跳遥测：入库/缺省归零/脏数据防御 全过")


if __name__ == "__main__":
    test_ext_ping_flow()
    test_ext_version_report()
    test_ext_telemetry()
    print("🎉 扩展心跳回归测试全部通过")
