"""VDL 会员引擎（V1，照搬 DataTool.vip 三轨结构）。

三轨：
  1) download_member  下载会员（1/3/7/30/180/365 天订阅；含下载类权益配额表）
  2) ai_member        AI 会员（月订阅 + 一次性积分池；自动捆绑下载会员权益）
  3) permanent_credits 永久积分包（纯按次计费，不与订阅绑定）

设计要点：
  - 纯标准库、零外部依赖（engine 不 import app / fastapi），可独立单测。
  - 时间与存储路径全部可注入（now_fn / path），测试无需 mock 系统时钟。
  - 激活续费顺延：expire_at = max(now, 当前到期) + 时长，不吞已有天数。
  - AI 会员激活时自动把下载会员权益覆盖到同一到期日（捆绑，无「纯 AI」档）。
  - 积分消耗顺序：先扣 AI 订阅积分（随会员到期清零），再扣永久积分。
  - 日配额惰性重置：daily_usage.date 非当日时自动清零重计。

V1 明确不做：真实支付、验签、功能门禁接入。仅提供引擎 + 状态机，供
/api/member 路由与后续功能模块调用。

参考文档：video-downloader-app/VDL_会员商业化_V1方案_2026-09-05.md
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import atomic_io

# --------------------------------------------------------------------------- #
# 套餐与权益常量表（唯一真源：VDL_会员商业化_V1方案_2026-09-05.md）
# --------------------------------------------------------------------------- #

# download member: 时长（天）、折算锚点
DOWNLOAD_PLANS: dict[str, dict[str, Any]] = {
    "download_1day":       {"price_cny": 1.90,   "days": 1,    "label": "下载会员·1天",   "saving": 0.0},
    "download_3day":       {"price_cny": 4.90,   "days": 3,    "label": "下载会员·3天",   "saving": 0.0},
    "download_7day":       {"price_cny": 9.90,   "days": 7,    "label": "下载会员·7天",   "saving": 0.0},
    "download_month":      {"price_cny": 29.80,  "days": 30,   "label": "下载会员·月",    "saving": 0.0},
    "download_half_year":  {"price_cny": 99.90,  "days": 180,  "label": "下载会员·180天", "saving": 0.44},
    "download_year":       {"price_cny": 179.00, "days": 365,  "label": "下载会员·年",    "saving": 0.50, "best": True},
}

# AI member: 积分池大小
AI_PLANS: dict[str, dict[str, Any]] = {
    "ai_5500":   {"price_cny": 49.90, "days": 30, "credits": 5500,  "label": "AI 积分月会员"},
    "ai_15000":  {"price_cny": 99.90, "days": 30, "credits": 15000, "label": "AI 月会员", "best": True},
}

# 永久积分包
CREDIT_PACKS: dict[str, dict[str, Any]] = {
    "credits_5000":  {"price_cny": 50.00, "credits": 5000,  "label": "5000 积分包"},
    "credits_15000": {"price_cny": 99.00, "credits": 15000, "label": "15000 积分包", "best": True},
}

# 下载类权益的每日配额上限（会员档；免费档见 FREE_DAILY_LIMITS）
DAILY_QUOTA_LIMITS: dict[str, int] = {
    "download": 1000,         # 下载任务 / 日（会员）—— 2026-09-06 起配额墙在「点清晰度下载」处
    "original": 100,          # 原画/4K 直链下载 / 日（会员，未接入）
    "batch_material": 1000,   # 批量下载 / 日（会员，未接入）
    "matting": 500,           # 本地一键抠图 / 日（会员）—— 2026-09-13 起配额墙；云端火山抠图走积分不计此配额
    "cloud": 200,             # 云端算力（网页版转码/拼接/去水印/字幕）/ 日（会员）—— 2026-09-29 对齐（App 本地算力不消耗）
    "app_compute": 200,       # App 本地重算力（转码/拼接/压缩/超分）/ 日（会员）—— 2026-09-29 用户定档：免费 5 次/日
    # 评论 / 数据 / 字幕批量：不限（不进 daily_usage 计配额）
}
# 免费档每日配额（2026-09-06 定稿：免费下载 10 次/日；原画/批量不开放）
FREE_DAILY_LIMITS: dict[str, int] = {
    "download": 10,
    "original": 0,            # 免费不开放原画
    "batch_material": 0,      # 免费不开放批量
    "matting": 8,             # 免费本地抠图 8 次/日；云端火山抠图走积分，不占此配额
    "cloud": 3,               # 云端算力免费 3 次/日（网页版专用键，账号级两端共享）—— 2026-09-29 对齐
    "app_compute": 5,         # App 本地算力免费 5 次/日（转码/拼接/压缩/超分，账号级上云共享）—— 2026-09-29 用户定档
    "subtitle": 2,            # 免费本地字幕提取 2 次/日（faster-whisper 本地推理）；会员无限
}
# 字幕提取(subtitle) 自 2026-09-13 起改为免费 2 次/日（会员无限），不再列入不限配额
UNLIMITED_QUOTA = ("comment", "data")

# 会员权益内免费的 AI 资源描述（供 plans/status 展示）
AI_FEATURES: list[str] = [
    "AI 字幕识别", "字幕提取", "视频总结", "图片翻译体验", "更多 AI 权益持续新增",
]

# 个人中心「今日使用」功能配额表（与前端表格四列对应：功能/体验剩余/权益余额/积分单价）
# - resource: 关联的 daily_usage 资源键（unlimited 资源不计日配额）
# - free_limit / member_limit: 每日体验配额（-1 表示不限）
# - ai_bonus: AI 会员周期内赠送额度（按 unit 单位）
# - credit_cost: 权益不足时按量扣积分单价（0 表示免费）
FEATURE_USAGE_DEFS: list[dict[str, Any]] = [
    {"key": "video_parse",       "name": "视频解析",         "resource": "download", "unit": "次",   "free_limit": 10,  "member_limit": 1000, "ai_bonus": 0,  "credit_cost": 0},
    {"key": "plugin_original",   "name": "插件原画解析",     "resource": "original", "unit": "次",   "free_limit": 0,   "member_limit": 100,  "ai_bonus": 0,  "credit_cost": 0},
    {"key": "ai_subtitle",       "name": "AI字幕识别",       "resource": "ai_subtitle", "unit": "分钟", "free_limit": 0, "member_limit": 0, "ai_bonus": 30, "credit_cost": 5},
    {"key": "subtitle_extract",  "name": "字幕提取",         "resource": "subtitle", "unit": "次",   "free_limit": 2, "member_limit": -1, "ai_bonus": 0, "credit_cost": 0},
    {"key": "batch_material",    "name": "插件批量下载素材", "resource": "batch_material", "unit": "条", "free_limit": 0, "member_limit": 1000, "ai_bonus": 0, "credit_cost": 0},
    {"key": "batch_comment",     "name": "插件批量下载评论", "resource": "comment",  "unit": "条",   "free_limit": -1,  "member_limit": -1,   "ai_bonus": 0,  "credit_cost": 0, "unlimited": True},
    {"key": "batch_data",        "name": "插件批量下载数据", "resource": "data",     "unit": "条",   "free_limit": -1,  "member_limit": -1,   "ai_bonus": 0,  "credit_cost": 0, "unlimited": True},
    {"key": "batch_subtitle",    "name": "插件批量下载字幕", "resource": "subtitle_batch", "unit": "条", "free_limit": -1, "member_limit": -1, "ai_bonus": 0, "credit_cost": 0, "unlimited": True},
    {"key": "image_translate",   "name": "图片翻译",         "resource": "image_translate", "unit": "张", "free_limit": 0, "member_limit": 0, "ai_bonus": 0, "credit_cost": 10},
]

# --------------------------------------------------------------------------- #
# AI 积分成本表（计费原则：仅「云端/服务端算力」计费；本地算力一律免费）
# --------------------------------------------------------------------------- #
# 2026-09-08 定稿：本地推理（字幕 faster-whisper、LaMa 去水印、BiRefNet 抠图、
# opencv 去水印）均跑在用户本机，不计费；只有真实服务端/云端算力（火山 MediaKit
# 云端抠图）才按次扣 AI 积分。故以下仅保留云端抠图一项成本。
# 云端抠图（火山 MediaKit，真实服务端算力）单次
MATTING_CLOUD_CREDIT_COST: int = 50


# --------------------------------------------------------------------------- #
# 套餐 / 成本运行时覆盖层（~/.video-downloader/plans.json）
# 仅作「覆盖层」：未覆盖的键回退到上方代码常量，默认值永不丢失。
# 后台管理面板可经 /api/admin/config/plans 写回。
# --------------------------------------------------------------------------- #
import threading as _threading

_PLAN_OVERRIDE_LOCK = _threading.Lock()
_PLAN_OVERRIDE_CACHE: Optional[tuple[int, dict[str, Any]]] = None


def _base_dir() -> Path:
    """数据目录：VDL_DATA_DIR 优先（离线测试隔离，绝不写真实家目录）。

    与 auth_store._base_dir() 同一语义（同目录、同覆盖位）——两处若分叉，
    测试会把真实 plans.json / membership.json 写脏。
    """
    override = os.environ.get("VDL_DATA_DIR", "").strip()
    if override:
        return Path(override)
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        return Path(os.environ.get("APPDATA", Path.home())) / "VideoDownloader"
    return Path.home() / ".video-downloader"


def plan_override_path() -> Path:
    return _base_dir() / "plans.json"


def load_plan_overrides() -> dict[str, Any]:
    """读取 plans.json 覆盖（mtime 缓存）。无文件 / 损坏 → 空 dict。"""
    global _PLAN_OVERRIDE_CACHE
    p = plan_override_path()
    try:
        if not p.exists():
            _PLAN_OVERRIDE_CACHE = None
            return {}
        mtime = p.stat().st_mtime_ns
        cached = _PLAN_OVERRIDE_CACHE
        if cached is not None and cached[0] == mtime:
            return cached[1]
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
        if not isinstance(data, dict):
            data = {}
        _PLAN_OVERRIDE_CACHE = (mtime, data)
        return data
    except (OSError, json.JSONDecodeError):
        return {}


_SAVE_TABLE_KEYS = ("download_plans", "ai_plans", "credit_packs", "credit_costs")


def _overlay_plans(defaults: dict[str, Any], override: Any) -> dict[str, Any]:
    """把覆盖表逐字段叠加到代码默认套餐上：默认值打底，覆盖字段获胜。

    覆盖表缺整个条目 → 用默认条目；条目里缺某字段（如 days/label）→
    落回默认值。只存在于覆盖层的条目原样保留。
    """
    if not isinstance(override, dict) or not override:
        return dict(defaults)
    out: dict[str, Any] = {k: dict(v) if isinstance(v, dict) else v for k, v in defaults.items()}
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            merged = dict(out[k])
            merged.update(v)
            out[k] = merged
        else:
            out[k] = v
    return out


# ── 套餐价格云端真源（2026-10-03）────────────────────────────────────────────
# 背景：桌面后台改价原来只写本机 plans.json，云端授权中心与网页各用自带常量，
# 于是出现「App 159/298、网页 99.90/179、实际扣款按云端」三方分叉。
# 现在授权中心是唯一真源：本机保存时下发（push_plans_to_cloud），读取时云端优先
# （cloud_plan_overrides，TTL 5 分钟），云端不可达才回退本机覆盖层 + 代码常量。
_CLOUD_PLANS_CACHE: dict[str, Any] = {"ts": 0.0, "plans": {}}
_CLOUD_PLANS_TTL = 300.0


def _license_api(path: str) -> str:
    """授权中心接口基址；未配置返回空串（调用方回退本机）。"""
    try:
        import license_client
        base = str(license_client.license_base() or "").rstrip("/")
    except Exception:  # noqa: BLE001 — 云端不可用不该影响本机功能
        base = str(os.environ.get("VDL_LICENSE_BASE") or "").rstrip("/")
    return f"{base}{path}" if base else ""


def cloud_plan_overrides(force: bool = False) -> dict[str, Any] | None:
    """从授权中心拉价格覆盖表。失败/未配置返回 None → 调用方回退本机常量。

    设 `VDL_PLANS_CLOUD=0` 可完全关闭云端取价（离线测试用：本机覆盖层要能独立
    验证，否则会被真实云端的价格盖掉，用例退化成依赖网络的非隔离测试）。
    """
    if str(os.environ.get("VDL_PLANS_CLOUD") or "1").strip().lower() in ("0", "false", "off"):
        return None
    url = _license_api("/api/license/plans")
    if not url:
        return None
    now = time.time()
    if not force and (now - float(_CLOUD_PLANS_CACHE.get("ts") or 0.0)) < _CLOUD_PLANS_TTL:
        return _CLOUD_PLANS_CACHE.get("plans") or {}
    try:
        import urllib.request as _rq
        req = _rq.Request(url, data=b"{}", method="POST",
                          headers={"Content-Type": "application/json"})
        with _rq.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
        plans = data.get("plans") if isinstance(data, dict) else None
        if not isinstance(plans, dict):
            return None
        _CLOUD_PLANS_CACHE["ts"] = now
        _CLOUD_PLANS_CACHE["plans"] = plans
        return plans
    except Exception:  # noqa: BLE001 — 网络/协议异常一律回退本机
        return None


def local_plans() -> dict[str, dict[str, Any]]:
    """本机意图表：代码常量 ← 本机 plans.json 覆盖层（**不含云端**）。

    🔴 下发云端必须用这张表，不能用 effective_plans()：后者已把云端旧值合并进来，
    再推回云端等于「用旧值覆盖新值」，而云端优先级最高 → 桌面改价被自己盖回去，
    表现为保存成功但价格没变（2026-10-03 实测：改 109.90 生效价仍是 99.90）。
    """
    ov = load_plan_overrides()
    return {
        "download_plans": _overlay_plans(DOWNLOAD_PLANS, ov.get("download_plans")),
        "ai_plans": _overlay_plans(AI_PLANS, ov.get("ai_plans")),
        "credit_packs": _overlay_plans(CREDIT_PACKS, ov.get("credit_packs")),
    }


def push_plans_to_cloud() -> dict[str, Any]:
    """把本机意图套餐表下发授权中心（后台保存后调用）。返回同步状态，绝不抛出。"""
    url = _license_api("/api/license/plans_set")
    if not url:
        return {"ok": False, "reason": "no_license_base"}
    try:
        import admin_store
        token = admin_store._license_admin_token()  # noqa: SLF001 — 复用既有令牌读取
    except Exception:  # noqa: BLE001
        token = ""
    if not token:
        return {"ok": False, "reason": "no_admin_token"}
    try:
        import urllib.request as _rq
        eff = local_plans()
        body = json.dumps({"token": token, "plans": {
            "download_plans": eff.get("download_plans") or {},
            "ai_plans": eff.get("ai_plans") or {},
            "credit_packs": eff.get("credit_packs") or {},
        }}, ensure_ascii=False).encode("utf-8")
        req = _rq.Request(url, data=body, method="POST",
                          headers={"Content-Type": "application/json"})
        with _rq.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
        if isinstance(data, dict) and data.get("ok"):
            _CLOUD_PLANS_CACHE["ts"] = time.time()
            _CLOUD_PLANS_CACHE["plans"] = data.get("plans") or {}
            return {"ok": True, "synced_at": data.get("updated_at")}
        return {"ok": False, "reason": str((data or {}).get("error") or "cloud_rejected")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"error: {e}"}


def effective_plans() -> dict[str, dict[str, Any]]:
    """生效套餐表：代码常量 ← 本机 plans.json 覆盖层 ← 授权中心覆盖（云端最高）。

    展示 / 下单 / 发放共用此真源；云端不可达时自动退化为本机覆盖层 + 代码常量。
    """
    ov = load_plan_overrides()
    cloud = cloud_plan_overrides() or {}
    return {
        "download_plans": _overlay_plans(
            _overlay_plans(DOWNLOAD_PLANS, ov.get("download_plans")), cloud.get("download_plans")),
        "ai_plans": _overlay_plans(
            _overlay_plans(AI_PLANS, ov.get("ai_plans")), cloud.get("ai_plans")),
        "credit_packs": _overlay_plans(
            _overlay_plans(CREDIT_PACKS, ov.get("credit_packs")), cloud.get("credit_packs")),
    }


def effective_pay_plans() -> dict[str, dict[str, Any]]:
    """下单用套餐表：{code: {price, name, grant}}，与 payment_core.PAY_PLANS 同构。

    🔴 价格单一真源（2026-09-30）：支付内核若继续读 payment_core 的硬编码
    PAY_PLANS，超管在后台改的价格**不会影响真实扣款金额**。故改由本函数按
    「覆盖层 → 代码常量」实时产出，注入 PaymentService。
    """
    eff = effective_plans()
    out: dict[str, dict[str, Any]] = {}
    for key in ("download_plans", "ai_plans", "credit_packs"):
        for code, p in eff[key].items():
            out[code] = {"price": float(p.get("price_cny") or 0),
                         "name": p.get("label") or code, "grant": code}
    return out


def save_plan_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """写入 plans.json（0600）。与现有覆盖做 overlay 合并；传 null 的键视为删除。

    接受结构：
      { "download_plans": {...}, "ai_plans": {...}, "credit_packs": {...},
        "credit_costs": {...} }
    任意键可缺省；返回写盘后的完整覆盖 dict。失败抛 OSError。

    合并语义（2026-09-24 修复）：四张表内部按「套餐 code / 成本 key」逐条合并——
    载荷只更新它携带的条目，同表其余条目原样保留（此前是整表替换，
    部分载荷会把未携带的套餐从覆盖层抹掉，造成「改一个价、别的全丢」）。
    条目值为 null 视为删除该条目。
    """
    global _PLAN_OVERRIDE_CACHE
    with _PLAN_OVERRIDE_LOCK:
        existing = load_plan_overrides()
        for k, v in (data or {}).items():
            if v is None:
                existing.pop(k, None)
                continue
            if k in _SAVE_TABLE_KEYS and isinstance(v, dict) and isinstance(existing.get(k), dict):
                merged = dict(existing[k])
                for ik, iv in v.items():
                    if iv is None:
                        merged.pop(ik, None)
                    else:
                        merged[ik] = iv
                existing[k] = merged
            else:
                existing[k] = v
        p = plan_override_path()
        atomic_io.atomic_write_json(p, existing)
        _PLAN_OVERRIDE_CACHE = (p.stat().st_mtime_ns, existing)
        return existing


def credit_cost(op: str, sub: str | None = None) -> int:
    """查询某次 AI 操作的积分成本。未知 op 或本地算力返回 0（不扣费）。

    仅 matting_cloud（火山云端）计费；字幕提取 / AI 去水印 LaMa 等本地算力均免费。
    优先级：plans.json 覆盖层 → 代码常量。
    """
    ov = load_plan_overrides()
    costs = ov.get("credit_costs") or {}
    if op in costs:
        try:
            return int(costs[op])
        except (TypeError, ValueError):
            pass
    if op == "matting_cloud":
        return int(MATTING_CLOUD_CREDIT_COST)
    return 0


def spend_for(store: "MembershipStore", op: str, sub: str | None = None,
              reason: str | None = None) -> dict[str, Any]:
    """按成本扣 AI 积分。返回 spend_credits 的结果 dict（ok/error）。

    成本 <=0 视为免费操作，直接返回 ok=True（不污染积分池）。
    """
    cost = credit_cost(op, sub)
    if cost <= 0:
        return {"ok": True, "spent": 0, "free": True, "credits_left": store.status()["credits_total"]}
    return store.spend_credits(cost, reason=reason or op)


def gate_message(store: "MembershipStore", op: str, sub: str | None = None,
                 reason: str | None = None) -> str | None:
    """扣积分并产出拦截原因：None=放行；非 None=「积分不足」原因字符串（供 402 detail）。

    成本 <=0 视为免费放行；spend 系统异常时降级放行（记日志），不阻断主流程。
    """
    cost = credit_cost(op, sub)
    if cost <= 0:
        return None
    try:
        res = store.spend_credits(cost, reason=reason or op)
    except Exception as e:  # noqa: BLE001
        logging.getLogger("membership").warning("credit gate error op=%s: %s", op, e)
        return None
    if res.get("ok"):
        return None
    return f"AI 积分不足：本次操作需 {cost} 积分，当前 {res.get('credits_left', 0)}（请开通 AI 会员或购买积分包）"

# --------------------------------------------------------------------------- #
# 状态文件
# --------------------------------------------------------------------------- #


def default_state_path() -> Path:
    """状态文件默认路径 ~/.video-downloader/membership.json（frozen 兼容）。

    同样受 VDL_DATA_DIR 覆盖（见 _base_dir），保证测试不写真实家目录。
    """
    return _base_dir() / "membership.json"


def _empty_state() -> dict[str, Any]:
    return {
        "download_member": {"active": False, "plan": None, "expire_at": 0.0},
        "ai_member": {"active": False, "plan": None, "expire_at": 0.0,
                      "grant_credits": 0, "credits_left": 0, "feature_credits": {}},
        "permanent_credits": {"total": 0, "packs": []},
        "daily_usage": {"date": "", "download": 0, "original": 0, "batch_material": 0,
                        "ai_subtitle": 0, "subtitle": 0, "subtitle_batch": 0,
                        "image_translate": 0, "matting": 0},
        "usage_history": {},
        "meta": {"activated_at": 0.0, "history": [],
                 "device_fp": "", "device_bound_at": 0.0,
                 "license_code": "", "license_revoked": False},
    }


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError):
        return _empty_state()
    st = _empty_state()
    # 逐键合并，容忍旧/缺字段
    for k in ("download_member", "ai_member", "permanent_credits", "daily_usage", "usage_history", "meta"):
        if isinstance(data.get(k), dict):
            st[k].update(data[k])
    return st


def _save_state(path: Path, state: dict[str, Any]) -> None:
    try:
        # 会员状态是「用户付钱买来的东西」，坏了等于会员凭空消失：唯一临时名原子写
        # + 按路径的进程内临界区（并发写者共用固定名 `.json.tmp` 会互截断，见 atomic_io）。
        with atomic_io.mutation(path):
            atomic_io.atomic_write_json(path, state)
    except OSError:
        # 状态文件写失败不应让业务崩溃（降级为内存态）
        pass


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #

# 会员权益文案（2026-10-03 改为**从配额表自动生成**）
# 此前是手写死的 6 条，加了 matting/cloud/app_compute/subtitle 等配额后忘记补文案，
# 出现「代码里有、页面上没有」。现在按 DAILY_QUOTA_LIMITS + FEATURE_USAGE_DEFS 生成，
# 守卫 test_membership_benefits 钉住「有配额必有文案」，以后不会再漏。
_BENEFIT_FROM_LIMITS: tuple[tuple[str, str], ...] = (
    ("download", "下载任务 {v} 次/日"),
    ("original", "原画 / 4K 直链解析 {v} 次/日"),
    ("batch_material", "批量下载素材 {v} 条/日"),
    ("matting", "本地一键抠图 {v} 次/日"),
    ("cloud", "云端算力（转码 / 拼接 / 去水印 / 字幕）{v} 次/日"),
    ("app_compute", "App 本地重算力（转码 / 拼接 / 压缩 / 超分）{v} 次/日"),
)
# 不走每日配额、但属于会员权益的说明项
_BENEFIT_EXTRA: tuple[dict[str, str], ...] = (
    {"key": "quality", "text": "清晰度：1080P 及以上全部开放"},
    {"key": "devices", "text": "同一账号 2 台设备同时在线"},
    {"key": "speed", "text": "高速通道 · 全速不限速"},
    {"key": "no_credits", "text": "不含 AI 积分：AI 字幕识别 / 云端抠图等需另购 AI 会员或积分包"},
    {"key": "support", "text": "优先客服支持"},
)


def download_benefits() -> list[dict[str, str]]:
    """下载会员权益清单：配额项自动跟随 DAILY_QUOTA_LIMITS + 不限项 + 静态说明。"""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for key, tpl in _BENEFIT_FROM_LIMITS:
        v = int(DAILY_QUOTA_LIMITS.get(key) or 0)
        if v > 0:
            out.append({"key": key, "text": tpl.format(v=v)})
            seen.add(key)
    # member_limit = -1 的功能 = 会员不限次（评论/数据/字幕批量、字幕提取…）
    for d in FEATURE_USAGE_DEFS:
        if int(d.get("member_limit", 0)) == -1 and d.get("key") not in seen:
            out.append({"key": d["key"], "text": f"{d.get('name') or d['key']}：不限"})
            seen.add(str(d.get("key")))
    out.extend(dict(x) for x in _BENEFIT_EXTRA)
    return out


@dataclass
class MembershipStore:
    """VDL 会员状态机。线程外调用方需自行加锁（见 app 单例）。"""
    path: Optional[Path] = None
    now_fn: Callable[[], float] = field(default=time.time)
    _state: dict[str, Any] = field(default_factory=_empty_state, init=False)
    _loaded: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.path is None:
            self.path = default_state_path()

    # ---- 基础读写 ----
    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._state = _load_state(self.path)
            self._loaded = True
            self._inject_token()

    def _inject_token(self) -> None:
        """把 token 从 Keychain 取回内存态（磁盘 JSON 里没有明文）。

        各读取点读的都是内存态 `acc["token"]`，所以只要在加载时补齐，
        就不需要逐个改那 6 处 `acc.get("token")`。
        """
        try:
            acc = (self._state.get("meta") or {}).get("account") or {}
            if not acc or acc.get("token") or not acc.get("email"):
                return
            from credential_store import resolve_token
            t = resolve_token(acc)
            if t:
                acc["token"] = t
                acc.setdefault("token_store", "keychain")
        except Exception:
            pass

    def _persist(self) -> None:
        """写盘：Keychain 态的 token 不落明文（内存里的值原样保留）。"""
        acc = (self._state.get("meta") or {}).get("account") or {}
        mem_token = acc.get("token") or ""
        stripped = False
        if mem_token and acc.get("token_store") == "keychain":
            acc["token"] = ""
            stripped = True
        try:
            _save_state(self.path, self._state)
        finally:
            if stripped:
                acc["token"] = mem_token

    def _now(self) -> float:
        return self.now_fn()

    # ---- 公开查询 ----
    def status(self) -> dict[str, Any]:
        """当前会员状态（惰性判定过期、惰性日切配额）。返回公开 dict。"""
        self._ensure_loaded()
        now = self._now()
        st = self._state
        dl = st["download_member"]
        ai = st["ai_member"]

        # 惰性过期
        if dl.get("active") and now >= float(dl.get("expire_at", 0)):
            dl["active"] = False
            dl["plan"] = None
            dl["expire_at"] = 0.0
        if ai.get("active") and now >= float(ai.get("expire_at", 0)):
            ai["active"] = False
            ai["plan"] = None
            ai["expire_at"] = 0.0
            ai["credits_left"] = 0  # AI 订阅积分到期清零

        # AI 会员捆绑下载权益：激活期内 download 视同可用到同一到期日
        dl_active = bool(dl.get("active")) or bool(ai.get("active"))
        dl_until = max(float(dl.get("expire_at", 0)), float(ai.get("expire_at", 0))) if (dl.get("active") or ai.get("active")) else 0.0

        # 惰性日切
        self._roll_daily(now)

        out = {
            "download_member": {
                "active": dl_active,
                "plan": dl.get("plan") if dl.get("active") else (ai.get("plan") if ai.get("active") else None),
                "expire_at": dl_until,
                "source": "download" if dl.get("active") else ("ai_bundle" if ai.get("active") else None),
            },
            "ai_member": {
                "active": bool(ai.get("active")),
                "plan": ai.get("plan"),
                "expire_at": float(ai.get("expire_at", 0)),
                "credits_left": int(ai.get("credits_left", 0)),
                "grant_credits": int(ai.get("grant_credits", 0)),
            },
            "permanent_credits": int(st["permanent_credits"].get("total", 0)),
            "credits_total": int(ai.get("credits_left", 0)) + int(st["permanent_credits"].get("total", 0)),
            "daily_usage": dict(st["daily_usage"]),
        }

        # P2 一机一码：指纹不符 / 卡密被作废 → 会员权益整体锁定（数据保留，不删状态）。
        # 锁定是**展示与判定层**的：paid 闸门读 status() 的 active 即自动降为免费档。
        lock = self._device_lock()
        if lock:
            out["download_member"]["active"] = False
            out["download_member"]["plan"] = None
            out["download_member"]["source"] = None
            out["ai_member"]["active"] = False
            out["ai_member"]["credits_left"] = 0
            out["device_locked"] = lock
        out["account"] = self.account_view()
        return out

    def _device_lock(self) -> Optional[str]:
        """取当前设备指纹并判定锁定（引擎内延迟导入，保持可独立单测）。"""
        try:
            from device_id import fingerprint  # noqa: PLC0415
            fp, strong = fingerprint()
        except Exception:
            return None  # 指纹模块不可用 → fail-open
        return self.device_lock_reason(fp, strong)

    def plans(self) -> dict[str, Any]:
        """套餐表（价格/时长/权益），供前端购买中心展示。

        优先级：plans.json 覆盖层 → 代码常量默认值。
        合并语义（2026-09-24 修复）：覆盖层**逐字段叠加**在默认条目上
        （此前是整段替换，覆盖条目里缺 days/label 会把默认值顶丢，
        出现「后台天数空白、前台显示裸 code」）。
        """
        ov = load_plan_overrides()
        dl = _overlay_plans(DOWNLOAD_PLANS, ov.get("download_plans"))
        ai = _overlay_plans(AI_PLANS, ov.get("ai_plans"))
        cp = _overlay_plans(CREDIT_PACKS, ov.get("credit_packs"))
        return {
            "download_member": {
                "plans": dl,
                "benefits": download_benefits(),
            },
            "ai_member": {
                "plans": ai,
                "bundle_note": "包含下载会员全部权益",
                "features": AI_FEATURES,
            },
            "credit_packs": cp,
            "currency": "CNY",
        }

    # ---- 激活 ----
    def activate(self, code: str, via: str = "test",
                 device_fp: Optional[str] = None,
                 license_code: str = "") -> dict[str, Any]:
        """按套餐/积分包 code 激活。续费顺延，AI 会员自动捆绑下载权益。

        P2 一机一码：激活时把设备指纹写进 meta.device_fp（仅收**强指纹**；
        弱指纹不绑，宁漏勿误伤）。之后 status() 发现指纹不符 → 会员锁定
        （防拷贝 membership.json 到别的机器白嫖）。license_code 一并留痕，
        供启动时向授权中心 check 卡密是否被作废。
        """
        self._ensure_loaded()
        now = self._now()
        st = self._state
        result: dict[str, Any] = {"ok": True, "code": code, "via": via}

        # 🔴 一律走生效套餐表（覆盖层 → 代码常量）：此前直接用模块常量，
        #    超管在后台把某档天数从 7 改成 5，发放时仍按 7 天算。
        _eff = effective_plans()
        _dl_plans, _ai_plans, _credit_packs = (
            _eff["download_plans"], _eff["ai_plans"], _eff["credit_packs"])

        if code in _dl_plans:
            info = _dl_plans[code]
            cur = float(st["download_member"].get("expire_at", 0) or 0)
            new_exp = max(now, cur) + int(info.get("days") or 0) * 86400
            st["download_member"].update({"active": True, "plan": code, "expire_at": new_exp})
            result.update({"kind": "download_member", "expire_at": new_exp})
        elif code in _ai_plans:
            info = _ai_plans[code]
            cur = float(st["ai_member"].get("expire_at", 0) or 0)
            new_exp = max(now, cur) + info["days"] * 86400
            # AI 会员按功能赠送额度：续费时不足上限则补齐，不浪费已用剩余
            fc = dict(st["ai_member"].get("feature_credits") or {})
            for d in FEATURE_USAGE_DEFS:
                if d.get("ai_bonus", 0) > 0:
                    fc[d["key"]] = max(int(fc.get(d["key"], 0)), int(d["ai_bonus"]))
            st["ai_member"].update({
                "active": True, "plan": code, "expire_at": new_exp,
                "grant_credits": int(info["credits"]), "credits_left": int(info["credits"]),
                "feature_credits": fc,
            })
            # 捆绑：下载权益覆盖到 AI 到期（不置 active 标志，status 负责推导 source）
            dl_exp = float(st["download_member"].get("expire_at", 0) or 0)
            if dl_exp < new_exp:
                st["download_member"]["expire_at"] = new_exp
            result.update({"kind": "ai_member", "expire_at": new_exp,
                           "credits_granted": int(info["credits"])})
        elif code in _credit_packs:
            info = _credit_packs[code]
            amt = int(info.get("credits") or 0)
            st["permanent_credits"]["total"] = int(st["permanent_credits"].get("total", 0)) + amt
            st["permanent_credits"].setdefault("packs", []).append({
                "pack": code, "amount": amt, "bought_at": now,
            })
            result.update({"kind": "credit_pack", "credits_added": amt})
        else:
            return {"ok": False, "error": f"未知套餐 code: {code}"}

        if not st["meta"].get("activated_at"):
            st["meta"]["activated_at"] = now
        if device_fp:  # 仅收强指纹（is_fingerprint 校验 32hex）；换机重绑也走这里
            st["meta"]["device_fp"] = device_fp
            st["meta"]["device_bound_at"] = now
        if license_code:
            st["meta"]["license_code"] = license_code
            st["meta"]["license_revoked"] = False  # 新卡激活视为恢复
        st["meta"].setdefault("history", []).append({
            "code": code, "via": via, "at": now, "type": "activate",
        })
        st["meta"]["history"] = st["meta"]["history"][-200:]  # 只留最近 200 条
        self._persist()
        return result

    # ---- 设备/账号状态（账号制定版） ----
    def device_lock_reason(self, current_fp: str = "", strong: bool = True) -> Optional[str]:
        """会员是否在本次查询中被「降级停用」。返回原因或 None。

        账号制语义（2026-09-22 起）：**不再因为机器换了就锁死会员**。
        member.json 被拷贝到别的机器用另一回事 —— 真正的限制在云端「同账号最多
        N 台设备」（见 deploy/license_server.py 的 device 配额），挤掉由 heartbeat
        发现后写到这里。调用方应放宽心态：宁可漏管一台，也不让付费用户被锁死。

        仅三种情况停用：
          - LICENSE_REVOKED：卡密/账号被管理员停用（主动行为）
          - DEVICE_EVICTED：该账号已在其他两台设备登录，本机被挤掉（重新登录即恢复）
          - ACCOUNT_BANNED：账号被超管封禁（2026-09-25 防破解，云端心跳下发）
        """
        self._ensure_loaded()
        meta = self._state.get("meta") or {}
        if meta.get("license_revoked"):
            return "LICENSE_REVOKED"
        acct = meta.get("account") or {}
        if acct.get("banned"):
            return "ACCOUNT_BANNED"
        if acct.get("evicted"):
            return "DEVICE_EVICTED"
        return None

    # ---- 云端账号状态 ----
    def save_account(self, email: str, token: str, account: Optional[dict] = None,
                     fp: str = "", name: str = "") -> dict[str, Any]:
        """记录登录态（token/邮箱/设备列表），清掉 evicted 标记。"""
        self._ensure_loaded()
        meta = self._state.setdefault("meta", {})
        acc = meta.setdefault("account", {})
        email_n = (email or "").strip().lower()
        # token 优先进 macOS Keychain（系统级 ACL，拷走 JSON 也用不了）。
        # ⚠️ fail-safe：Keychain 写失败就回落到 JSON 明文 —— 掉登录比泄露严重。
        where = "file"
        if token:
            try:
                from credential_store import set_token
                where = set_token(email_n, token)
            except Exception:
                where = "file"
            if where == "none":
                where = "file"
        acc.update({
            "email": email_n,
            # 内存态**始终**保留 token（所有 acc.get("token") 读的都是内存态）；
            # 明文不会落盘 —— _persist() 会在写盘时剥离 Keychain 态的 token。
            "token": token or "",
            "token_store": where,
            "fp": fp or acc.get("fp", ""),
            "name": name or acc.get("name", ""),
            "evicted": False,
            "logged_in": bool(token),
            "last_sync": self._now(),
        })
        if isinstance(account, dict):
            acc["devices"] = account.get("devices") or []
            acc["max_devices"] = int(account.get("max_devices") or 2)
        if "purchases_applied" not in acc:
            acc["purchases_applied"] = []
        self._persist()
        return {"ok": True}

    def clear_account(self) -> dict[str, Any]:
        """本地登出：清 token 与设备信息，但**保留已购买的权益**（重要）。

        换机重装时权益必须跟着账号走，所以这里只清登录态，不动 membership 本体。
        """
        self._ensure_loaded()
        meta = self._state.setdefault("meta", {})
        acc = meta.get("account") or {}
        try:
            from credential_store import delete_token
            delete_token(str(acc.get("email") or ""))
        except Exception:
            pass
        acc.update({"token": "", "email": acc.get("email", ""), "logged_in": False,
                    "evicted": False, "devices": [], "last_sync": 0.0,
                    "token_store": ""})
        meta["account"] = acc
        self._persist()
        return {"ok": True}

    def set_evicted(self, evicted: bool) -> None:
        """被其他设备挤出时标记 → status() 立即降级为免费档。"""
        self._ensure_loaded()
        acc = self._state.setdefault("meta", {}).setdefault("account", {})
        acc["evicted"] = bool(evicted)
        self._persist()

    def apply_cloud_purchases(self, purchases: list[dict[str, Any]],
                              via: str = "license") -> dict[str, Any]:
        """把云端账号下的套餐/积分包**按 purchase id 幂等**落户到本地权益。

        幂等是硬要求：登录会重复调用，若不按 id 去重，每次登录都会给会员顺延一年。
        """
        self._ensure_loaded()
        acc = self._state.setdefault("meta", {}).setdefault("account", {})
        applied = acc.setdefault("purchases_applied", [])
        if not isinstance(applied, list):
            applied = acc["purchases_applied"] = []
        out_applied: list[str] = []
        errors: list[dict[str, Any]] = []
        for p in (purchases or []):
            if not isinstance(p, dict):
                continue
            pid = str(p.get("id") or "").strip()
            plan_code = str(p.get("plan_code") or "").strip()
            if not pid or not plan_code or pid in applied:
                continue
            res = self.activate(plan_code, via=via)
            if res.get("ok"):
                applied.append(pid)
                out_applied.append(plan_code)
            else:
                errors.append({"id": pid, "plan_code": plan_code,
                               "error": res.get("error") or "未知错误"})
        acc["purchases_applied"] = applied[-500:]
        self._persist()
        return {"ok": not errors, "applied": out_applied, "errors": errors}

    def apply_cloud_authoritative(self, acct: dict[str, Any]) -> dict[str, Any]:
        """云端权威对账（2026-09-25 防破解核心）：用授权中心的权益快照**覆盖**本地。

        与 apply_cloud_purchases（幂等追加、只加不减）的本质区别：本地文件里的
        expire_at / credits_left / permanent_credits 被篡改（手改 JSON / 回环后门 /
        第三方工具）后，只要一联网同步，就被云端真值覆盖回滚 —— 改了也白改。

        仅当快照带 authority.v>=1（新版授权中心）时才覆盖；老服务端无快照 →
        调用方应退回 apply_cloud_purchases。封禁标记一并落 meta.account.banned，
        由 device_lock_reason → status() 全局锁定权益。
        """
        self._ensure_loaded()
        auth = (acct or {}).get("authority") or {}
        if not isinstance(auth, dict) or int(auth.get("v") or 0) < 1:
            return {"ok": False, "reason": "no_authority"}
        now = self._now()
        st = self._state
        dl = st["download_member"]
        ai = st["ai_member"]

        dl_until = float(auth.get("member_until_dl") or 0)
        dl["expire_at"] = dl_until
        dl["active"] = dl_until > now
        if not dl["active"]:
            dl["plan"] = None

        ai_until = float(auth.get("member_until_ai") or 0)
        ai_active = ai_until > now
        ai["expire_at"] = ai_until
        ai["active"] = ai_active
        if ai_active:
            ai["credits_left"] = max(0, int(auth.get("ai_credits_left") or 0))
            if not ai.get("plan"):
                ai["plan"] = "cloud_ai"
        else:
            ai["plan"] = None
            ai["credits_left"] = 0  # AI 订阅积分随订阅失效清零

        st["permanent_credits"]["total"] = max(0, int(auth.get("perm_credits") or 0))

        # 账号级每日用量同步（同一账号 App/网页共用每日配额）：
        # 取 max(本地, 云端) —— 云端只大不小，既不冲掉本地在途未报的增量，
        # 也让手改本地数字蹭额度的篡改一同步即回滚。仅快照日期=本地今天才合并。
        udate = str(auth.get("usage_date") or "")
        ucounts = auth.get("usage")
        if udate == time.strftime("%Y-%m-%d", time.localtime(now)) and isinstance(ucounts, dict):
            self._roll_daily(now)
            du = st["daily_usage"]
            keys = {str(k) for k in list(du.keys())} | {str(k) for k in ucounts}
            keys.discard("date")
            for k in keys:
                try:
                    cv = int(ucounts.get(k) or 0)
                except (TypeError, ValueError):
                    cv = 0
                try:
                    lv = int(du.get(k, 0) or 0)
                except (TypeError, ValueError):
                    lv = 0
                du[k] = max(lv, cv)

        acc = st["meta"].setdefault("account", {})
        acc["banned"] = bool(auth.get("banned"))
        st["meta"]["last_authority_sync"] = now
        self._persist()
        return {"ok": True, "banned": bool(auth.get("banned")),
                "download_until": dl_until, "ai_until": ai_until,
                "perm_credits": int(st["permanent_credits"]["total"]),
                "ai_credits_left": int(ai.get("credits_left", 0))}

    def account_view(self) -> dict[str, Any]:
        """给前端的账号快照（不吐 token）。"""
        self._ensure_loaded()
        acc = (self._state.get("meta") or {}).get("account") or {}
        try:
            from credential_store import resolve_token
            logged_in = bool(resolve_token(acc))
        except Exception:
            logged_in = bool(acc.get("token"))
        return {
            "logged_in": logged_in,
            "email": acc.get("email", ""),
            "device_fp": acc.get("fp", ""),
            "device_name": acc.get("name", ""),
            "devices": acc.get("devices") or [],
            "max_devices": int(acc.get("max_devices") or 2),
            "evicted": bool(acc.get("evicted")),
            "last_sync": float(acc.get("last_sync") or 0),
        }

    def set_license_revoked(self, revoked: bool) -> None:
        """启动校验发现卡密被作废时由路由层调用（引擎不做网络）。"""
        self._ensure_loaded()
        self._state.setdefault("meta", {})["license_revoked"] = bool(revoked)
        self._persist()

    # ---- 积分 ----
    def spend_credits(self, amount: int, reason: str = "ai_usage") -> dict[str, Any]:
        """消耗积分：先 AI 订阅积分（快过期），后永久积分。不足则拒绝。

        2026-09-25 防破解：扣减成功后异步上报授权中心记账（report_spend_async），
        云端按幂等 id 累计消耗 —— 本地 JSON 被篡改的余额会在下次同步时被云端
        权威快照覆盖（见 apply_cloud_authoritative）。
        """
        if amount <= 0:
            return {"ok": False, "error": "amount 必须为正"}
        self._ensure_loaded()
        st = self._state
        ai = st["ai_member"]
        ai_left = int(ai.get("credits_left", 0)) if ai.get("active") else 0
        perm_total = int(st["permanent_credits"].get("total", 0))
        if ai_left + perm_total < amount:
            return {"ok": False, "error": f"积分不足：需要 {amount}，当前 {ai_left + perm_total}"}

        remaining = amount
        ai_taken = 0
        # 1) AI 订阅积分
        if remaining > 0 and ai_left > 0:
            take = min(ai_left, remaining)
            ai["credits_left"] = ai_left - take
            remaining -= take
            ai_taken = take
        # 2) 永久积分
        perm_taken = 0
        if remaining > 0:
            perm_total -= remaining
            st["permanent_credits"]["total"] = perm_total
            remaining = 0
            perm_taken = amount - ai_taken
        st["meta"].setdefault("history", []).append({
            "type": "spend", "amount": amount, "reason": reason, "at": self._now(),
        })
        st["meta"]["history"] = st["meta"]["history"][-200:]
        self._persist()
        # 异步上云记账（fire-and-forget，失败/离线进 pending 队列下次同步补报）
        try:
            from routers.cloud_account import report_spend_async
            report_spend_async(self, amount=amount, ai_taken=ai_taken,
                               perm_taken=perm_taken, reason=reason)
        except Exception:
            pass
        return {"ok": True, "spent": amount, "reason": reason,
                "ai_taken": ai_taken, "perm_taken": perm_taken,
                "credits_left": self.status()["credits_total"]}

    def add_credits(self, delta: int, reason: str = "admin_adjust",
                    pool: str = "auto") -> dict[str, Any]:
        """管理员调整积分，可指定积分池。负 delta 允许透支（管理员强扣）。

        pool="ai"        → 只动 AI 订阅积分（ai_member.credits_left）
        pool="permanent" → 只动永久积分（permanent_credits.total）
        pool="auto"      → 兼容旧语义：正=充永久池，负=先扣 AI 订阅再扣永久
        返回两个池的最新余额。
        """
        self._ensure_loaded()
        st = self._state
        if pool not in ("auto", "ai", "permanent"):
            return {"ok": False, "error": f"未知积分池：{pool}"}
        delta = int(delta)
        ai = st["ai_member"]
        if pool == "ai":
            ai["credits_left"] = int(ai.get("credits_left", 0)) + delta
        elif pool == "permanent":
            st["permanent_credits"]["total"] = int(st["permanent_credits"].get("total", 0)) + delta
        else:  # auto（旧语义）
            if delta >= 0:
                st["permanent_credits"]["total"] = int(st["permanent_credits"].get("total", 0)) + delta
            else:
                amount = -delta
                ai_left = int(ai.get("credits_left", 0)) if ai.get("active") else 0
                perm_total = int(st["permanent_credits"].get("total", 0))
                # 先扣 AI 订阅积分，再扣永久积分（可能扣成负数，管理员强扣允许透支由前端提示）
                remaining = amount
                if remaining > 0 and ai_left > 0:
                    take = min(ai_left, remaining)
                    ai["credits_left"] = ai_left - take
                    remaining -= take
                if remaining > 0:
                    perm_total -= remaining
                    st["permanent_credits"]["total"] = perm_total
        st["meta"].setdefault("history", []).append({
            "code": f"admin_adjust:{delta}:{pool}", "via": reason, "at": self._now(), "type": "admin_adjust",
        })
        st["meta"]["history"] = st["meta"]["history"][-200:]
        self._persist()
        s = self.status()
        return {"ok": True, "delta": delta, "pool": pool,
                "ai_credits_left": int(s["ai_member"]["credits_left"]),
                "permanent_credits": int(s["permanent_credits"]),
                "credits_total": s["credits_total"]}

    def credits_balance(self) -> dict[str, int]:
        s = self.status()
        return {"ai_subscription": s["ai_member"]["credits_left"],
                "permanent": s["permanent_credits"],
                "total": s["credits_total"]}

    # ---- 每日配额 ----
    def _is_download_active(self) -> bool:
        """下载权益是否活跃（独立下载会员或 AI 会员捆绑）。"""
        st = self._state
        return bool(st["download_member"].get("active")) or bool(st["ai_member"].get("active"))

    def _roll_daily(self, now: float) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        du = self._state["daily_usage"]
        old_day = du.get("date")
        if old_day != day:
            # 归档旧一天的数据（如果存在且尚未归档）
            if old_day and old_day != "":
                hist = self._state.setdefault("usage_history", {})
                hist[old_day] = {k: int(v) for k, v in du.items() if k != "date"}
                # 只保留最近 90 天，避免无限增长
                cutoff = time.strftime("%Y-%m-%d", time.localtime(now - 90 * 86400))
                for k in list(hist.keys()):
                    if k < cutoff:
                        del hist[k]
                self._persist()
            du["date"] = day
            du["download"] = 0
            du["original"] = 0
            du["batch_material"] = 0
            du["ai_subtitle"] = 0
            du["subtitle"] = 0
            du["subtitle_batch"] = 0
            du["image_translate"] = 0
            du["matting"] = 0
            du["app_compute"] = 0

    def quota_state(self, resource: str) -> dict[str, Any]:
        """查询某资源的当日用量/上限（按当前档位：免费 or 会员）。unlimited 恒放行。"""
        self._ensure_loaded()
        self._roll_daily(self._now())
        if resource in UNLIMITED_QUOTA:
            return {"resource": resource, "allowed": True, "unlimited": True}
        is_member = self._is_download_active()
        limit_map = DAILY_QUOTA_LIMITS if is_member else FREE_DAILY_LIMITS
        limit = limit_map.get(resource)
        if limit is None and DAILY_QUOTA_LIMITS.get(resource) is None:
            # 未知资源：V1 不设卡（保守默认放行，避免误伤功能）
            return {"resource": resource, "allowed": True, "unknown": True}
        if limit is None:
            # 免费表未覆盖但会员表有（原画/批量）→ 免费额度为 0
            limit = 0
        used = int(self._state["daily_usage"].get(resource, 0))
        return {"resource": resource, "limit": limit, "used": used,
                "remaining": max(0, limit - used),
                "allowed": used < limit,
                "tier": "member" if is_member else "free",
                "member_limit": DAILY_QUOTA_LIMITS.get(resource),
                "free_limit": FREE_DAILY_LIMITS.get(resource, 0)}

    def use_daily(self, resource: str, n: int = 1) -> dict[str, Any]:
        """消耗下载类配额（免费档 resolve 10/日；会员档按表）。超限返回 ok=False。"""
        if n <= 0:
            return {"ok": False, "error": "n 必须为正"}
        q = self.quota_state(resource)
        if q.get("unlimited") or q.get("unknown"):
            return {"ok": True, "resource": resource, "unlimited": q.get("unlimited", False)}
        if not q["allowed"]:
            if q.get("tier") == "free":
                if resource == "download":   # 历史文案保持不变（test_membership 钉住）
                    return {"ok": False, "error": f"今日免费下载额度已用尽（{q['limit']}/日）— 开通下载会员可解锁 {q.get('member_limit', 0)} 次/日",
                            "resource": resource, "code": "MEMBER_QUOTA"}
                return {"ok": False, "error": f"今日免费处理额度已用尽（{q['limit']}/日）— 开通会员可解锁 {q.get('member_limit', 0)} 次/日",
                        "resource": resource, "code": "MEMBER_QUOTA"}
            return {"ok": False, "error": f"{resource} 今日配额已用尽（{q['limit']}/日）", "resource": resource,
                    "code": "MEMBER_QUOTA"}
        used = int(self._state["daily_usage"].get(resource, 0))
        new_used = used + n
        if new_used > q["limit"]:
            return {"ok": False, "error": f"超出 {resource} 日配额上限 {q['limit']}",
                    "resource": resource, "code": "MEMBER_QUOTA"}
        self._state["daily_usage"][resource] = new_used
        self._persist()
        # 异步上云记账（同一账号 App/网页共用每日配额；失败/离线进队列下次心跳补报）
        try:
            from routers.cloud_account import report_usage_async
            report_usage_async(self, resource, n)
        except Exception:
            pass
        return {"ok": True, "resource": resource, "used": new_used,
                "remaining": q["limit"] - new_used}


def _date_range_days(period: str, now: float) -> list[str]:
    """根据周期返回应包含的 YYYY-MM-DD 日期列表（含今天）。"""
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    if period in ("3d", "7d"):
        days = 3 if period == "3d" else 7
        return [time.strftime("%Y-%m-%d", time.localtime(now - i * 86400)) for i in range(days - 1, -1, -1)]
    if period == "month":
        # 本月 1 日到今天
        tm = time.localtime(now)
        year, month = tm.tm_year, tm.tm_mon
        start = time.mktime((year, month, 1, 0, 0, 0, 0, 0, -1))
        dates: list[str] = []
        cur = start
        while time.strftime("%Y-%m-%d", time.localtime(cur)) <= today:
            dates.append(time.strftime("%Y-%m-%d", time.localtime(cur)))
            cur += 86400
        return dates
    return [today]


def usage_summary(store: MembershipStore, period: str = "today") -> dict[str, int]:
    """汇总指定周期内各 resource 的累计用量。

    period: today | 3d | 7d | month
    """
    st = store.status()
    now = store._now()
    dates = _date_range_days(period, now)
    hist = st.get("usage_history", {})
    du = st.get("daily_usage", {})
    totals: dict[str, int] = {}
    for d in dates:
        if d == du.get("date"):
            src = du
        else:
            src = hist.get(d, {})
        for k, v in src.items():
            if k == "date":
                continue
            try:
                totals[k] = totals.get(k, 0) + int(v)
            except (TypeError, ValueError):
                continue
    return totals


def feature_usage_status(store: MembershipStore, period: str = "today") -> list[dict[str, Any]]:
    """生成个人中心「使用统计」表格所需的每行数据。

    period: today | 3d | 7d | month
    返回字段：key, name, unit, daily_used, daily_limit, daily_remaining,
              period_used, unlimited, balance, credit_cost。
    """
    st = store.status()
    ai = st["ai_member"]
    fc = ai.get("feature_credits") or {} if ai.get("active") else {}
    daily = st.get("daily_usage", {})
    period_totals = usage_summary(store, period)
    is_member = st["download_member"].get("active", False)
    rows: list[dict[str, Any]] = []
    for d in FEATURE_USAGE_DEFS:
        key = d["key"]
        unlimited = bool(d.get("unlimited", False))
        if unlimited:
            rows.append({
                "key": key,
                "name": d["name"],
                "unit": d["unit"],
                "daily_used": 0,
                "daily_limit": -1,
                "daily_remaining": -1,
                "period_used": 0,
                "unlimited": True,
                "balance": None,
                "credit_cost": int(d.get("credit_cost", 0)),
            })
            continue
        limit = int(d.get("member_limit", 0)) if is_member else int(d.get("free_limit", 0))
        used = int(daily.get(d["resource"], 0)) if limit >= 0 else 0
        remaining = -1 if limit < 0 else max(0, limit - used)
        bonus = int(d.get("ai_bonus", 0))
        balance = int(fc.get(key, 0)) if bonus > 0 and ai.get("active") else None
        rows.append({
            "key": key,
            "name": d["name"],
            "unit": d["unit"],
            "daily_used": used,
            "daily_limit": limit,
            "daily_remaining": remaining,
            "period_used": period_totals.get(d["resource"], 0),
            "unlimited": False,
            "balance": balance,
            "credit_cost": int(d.get("credit_cost", 0)),
        })
    return rows
