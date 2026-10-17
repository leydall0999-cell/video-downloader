"""跨端「每日免费额度」统一守卫（2026-10-17 用户定档「以后台那套为准」）。

## 背景
用户实测：同一账号桌面端显示「视频下载 3/3、字幕提取 1/1、各工具 1/1、视频解说 0/0」，
网页版却显示「下载 10/10、云端功能 3/3」。根因是网页版此前**只读代码常量**，不读授权
中心的 `free_quota.daily_free_limits`；而桌面端那套数值来自后台（授权中心是唯一真源）。

2026-10-17 修复：网页版 `effective_daily_limits()` 现在叠加授权中心的 `daily_free_limits`
覆盖层（与桌面端同口径），部署到国内 / 香港两个节点。

## 本守卫钉死两条，缺一不可
① **必须读云端覆盖**：当授权中心返回 `free_quota.daily_free_limits` 时，网页版生效值必须
   是云端值（而非代码常量）。否则「后台改了免费次数、网页不变」的对不上会复发。
② **离线开关必须切断云端**：`VDL_CLOUD_LINK=0` / `VDL_PLANS_CLOUD=0` 时，**不得**去打真实
   授权中心，必须回退代码常量 —— 否则离线测试会读到生产云端的真实覆盖值（本次修复前就因此
   把两个离线用例染红），破坏测试确定性。

全程离线：mock `_license_api` 与一个假的 `urlopen`，不触碰真实网络 / 真实授权中心。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

_SERVER = Path(__file__).resolve().parent.parent
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

os.environ.setdefault("VDL_DATA_DIR", "/tmp/vdl_test_daily_quota_unified")

import membership as M  # noqa: E402


# 假云端响应：把下载压到 3、转码压到 7、字幕提取压到 0、其余保持常量；mode=on 不归零。
# 注意：只挑网页版常量里真实存在的键（download / cloud_convert / cloud_subtitle）。
# cloud_commentary 是桌面端专属资源，网页端常量没有它，覆盖会被白名单过滤掉。
_FAKE_FREE_QUOTA = {
    "mode": "on",
    "daily_free_limits": {
        "download": 3,
        "cloud_convert": 7,
        "cloud_subtitle": 0,
    },
    "daily_member_limits": {
        "download": 999,
    },
}


class _FakeResp:
    def __init__(self, payload: dict):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_cloud(fake_quota: dict | None):
    """把「拉授权中心」整个替换成内存假实现：_license_api 返回哨兵 URL，
    urlopen 返回带 free_quota 的假响应（fake_quota=None 时模拟云端无该字段）。"""
    payload = {"ok": True}
    if fake_quota is not None:
        payload["free_quota"] = fake_quota

    def _fake_urlopen(req, timeout=5):
        return _FakeResp(payload)

    return mock.patch.object(M, "_license_api", return_value="http://127.0.0.1:9999/api/license/plans"), \
           mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen)


def _clear_cache():
    M._FREEQ_CACHE["ts"] = 0.0
    M._FREEQ_CACHE["data"] = {}
    M._FREEQ_CACHE["fail_ts"] = 0.0


FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + name + (("  —— " + detail) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def test_web_reads_cloud_daily_free_limits():
    """① 网页版必须采纳云端 daily_free_limits 覆盖（不能只认常量）。"""
    _clear_cache()
    saved = {k: os.environ.get(k) for k in ("VDL_CLOUD_LINK", "VDL_PLANS_CLOUD")}
    os.environ["VDL_CLOUD_LINK"] = "1"
    os.environ["VDL_PLANS_CLOUD"] = "1"
    try:
        with _patch_cloud(_FAKE_FREE_QUOTA)[0], _patch_cloud(_FAKE_FREE_QUOTA)[1]:
            member, free = M.effective_daily_limits(force=True)
    finally:
        for k, v in saved.items():
            (os.environ.pop(k) if v is None else os.environ.__setitem__(k, v))

    # 云端覆盖的键 → 生效值必须等于云端值
    check("下载采纳云端值 3（常量本是 10）", free.get("download") == 3, f"download={free.get('download')}")
    check("转码采纳云端值 7（常量本是 3）", free.get("cloud_convert") == 7, f"cloud_convert={free.get('cloud_convert')}")
    check("字幕提取采纳云端值 0（常量本是 3）", free.get("cloud_subtitle") == 0, f"cloud_subtitle={free.get('cloud_subtitle')}")
    # 云端未覆盖的键 → 落回常量（cloud_concat 常量 3）
    check("未覆盖键回退常量（cloud_concat=3）", free.get("cloud_concat") == 3, f"cloud_concat={free.get('cloud_concat')}")
    # 会员档同样采纳云端 daily_member_limits
    check("会员下载采纳云端值 999", member.get("download") == 999, f"member.download={member.get('download')}")


def test_offline_switch_disables_cloud():
    """② VDL_CLOUD_LINK=0 必须切断云端、回退常量；否则离线测试会被真实云端值污染。"""
    _clear_cache()
    saved = {k: os.environ.get(k) for k in ("VDL_CLOUD_LINK", "VDL_PLANS_CLOUD")}
    os.environ["VDL_CLOUD_LINK"] = "0"  # 全局离线开关（run_offline_tests.sh 用的就是它）
    os.environ["VDL_PLANS_CLOUD"] = "1"
    try:
        # 即便云端真有覆盖值，离线开关也必须让它失效
        with _patch_cloud(_FAKE_FREE_QUOTA)[0], _patch_cloud(_FAKE_FREE_QUOTA)[1]:
            member, free = M.effective_daily_limits(force=True)
    finally:
        for k, v in saved.items():
            (os.environ.pop(k) if v is None else os.environ.__setitem__(k, v))

    check("离线：下载回退常量 10（不得是云端 3）", free.get("download") == 10, f"download={free.get('download')}")
    check("离线：转码回退常量 3（不得是云端 7）", free.get("cloud_convert") == 3, f"cloud_convert={free.get('cloud_convert')}")
    check("离线：字幕提取回退常量 3（不得是云端 0）", free.get("cloud_subtitle") == 3, f"cloud_subtitle={free.get('cloud_subtitle')}")


def test_cloud_missing_field_falls_back_to_constant():
    """云端 free_quota 缺 daily_free_limits 时（旧版授权中心）必须回退常量，不报错。"""
    _clear_cache()
    saved = {k: os.environ.get(k) for k in ("VDL_CLOUD_LINK", "VDL_PLANS_CLOUD")}
    os.environ["VDL_CLOUD_LINK"] = "1"
    os.environ["VDL_PLANS_CLOUD"] = "1"
    try:
        # 模拟旧版授权中心：只返回 cloud_lifetime/mode，没有 daily_free_limits
        with _patch_cloud({"mode": "on"})[0], _patch_cloud({"mode": "on"})[1]:
            member, free = M.effective_daily_limits(force=True)
    finally:
        for k, v in saved.items():
            (os.environ.pop(k) if v is None else os.environ.__setitem__(k, v))

    check("旧版云端（无 daily_free_limits）→ 下载回退常量 10", free.get("download") == 10, f"download={free.get('download')}")
    check("旧版云端 → 转码回退常量 3", free.get("cloud_convert") == 3, f"cloud_convert={free.get('cloud_convert')}")


def main() -> None:
    print("=== 跨端每日免费额度统一守卫（web-dev）===")
    test_web_reads_cloud_daily_free_limits()
    test_offline_switch_disables_cloud()
    test_cloud_missing_field_falls_back_to_constant()
    print()
    if FAILS:
        print(f"❌ 失败 {len(FAILS)} 项：")
        for f in FAILS:
            print("   -", f)
        sys.exit(1)
    print("✅ 网页版每日免费额度：采纳云端覆盖 + 离线开关切断云端，与桌面端同一份真源")


if __name__ == "__main__":
    main()
