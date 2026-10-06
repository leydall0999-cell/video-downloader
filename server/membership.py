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
# 🔴 2026-10-04 清理：删掉 original / batch_material。二者只有配额条目、没有任何
#    use_daily 拦截点；而且「原画」本身是**清晰度档位门**（>1080P 需会员），
#    不按次计费 —— 拿次数配额表达是错的概念。留着会让会员页承诺
#    「原画 100 次/日、批量素材 1000 条/日」两份不存在的权益。
# 🔴 2026-10-06 用户定档「每个功能独立配置，可以手动调整」—— 拆键。
# 此前 6 个重活共用一个 `app_compute` 键：管理员在后台改「音乐转换」也会改到
# 「高清修复」，因为它们是同一个数。拆成独立键后每个功能各计各的。
#
# ⚠️ 存量迁移（用户同轮定档「旧记录归到 app_compute 总池」）：
#   `_LEGACY_KEYS` 里的老键**保留在表里**（作为兜底与历史归集目标），
#   `migrate_legacy_usage()` 在读取时把老键的已用量并到第一个新键，
#   这样「今天之前用过 3 次转换」不会因为换键而凭空多出 3 次额度。
#   老键不再有新的写入点（业务全部改用新键），但**保留**是为了：
#     ① 历史数据归集；② 后台仍能看到「总池」这一行做对照。
LEGACY_QUOTA_KEYS: tuple[str, ...] = ("app_compute", "cloud")

DAILY_QUOTA_LIMITS: dict[str, int] = {
    "download": 1000,         # 下载任务 / 日（会员）
    "matting": 500,           # 本地一键抠图 / 日（会员）；云端火山抠图走积分不计此配额
    "cloud": 200,             # 网页版在线处理 / 日（会员）—— 旧总池，2026-10-06 拆池后无写入点，仅存量归集
    # 🔴 2026-10-06 拆池：8 个云端功能各自独立「每日免费次数」键（会员档）
    "cloud_commentary": 200,   # 视频解说 / 日（会员）
    "cloud_convert": 200,      # 在线转码 / 日（会员）
    "cloud_dewatermark": 200,  # 在线去水印（图片）/ 日（会员）
    "cloud_subtitle": 200,     # 在线字幕提取 / 日（会员）
    # 🔴 2026-10-06 第二轮：4 个此前塞在旧键里的功能各自独立
    "cloud_concat": 200,            # 在线拼接 / 日（会员）
    "cloud_dewatermark_pdf": 200,   # 在线去水印（PDF）/ 日（会员）
    "cloud_subtitle_burn": 200,     # 字幕烧录 / 日（会员）
    "cloud_subtitle_translate": 200,  # 字幕翻译 / 日（会员）
    # ── 以下 6 个是 2026-10-06 从 app_compute 拆出的独立键 ──────────────
    "convert_video": 200,     # 视频格式转换（会员）
    "convert_audio": 200,     # 音乐（音频）转换（会员）
    "convert_image": 200,     # 图片格式转换（会员）
    "compress": 200,          # 高效压缩（会员）
    "sr": 200,                # 高清修复（图片）（会员）
    "bridge": 200,            # 音视频桥接（合成 / 替换）
    # 🔴 2026-10-06 第二轮拆池：上一轮 6 键内部还塞着多个功能（拼接混在转换、
    #    视频超分混在高清修复），本轮各拆一个独立键 —— 一个键 = 一个用户可见功能。
    "sr_video": 200,          # 视频超分（会员）
    "concat_video": 200,      # 视频拼接（会员）
    "concat_audio": 200,      # 音频拼接（会员）
    # 老键：仅用于存量归集与后台对照，不再有写入点
    "app_compute": 200,
}
# 免费档每日配额
FREE_DAILY_LIMITS: dict[str, int] = {
    "download": 10,
    "matting": 8,             # 免费本地抠图 8 次/日
    "cloud": 3,               # 网页版在线处理 3 次/日（旧总池，保留向后兼容）
    "subtitle": 2,            # 免费字幕提取 2 次/日（faster-whisper 本地推理）；会员无限
    # 🔴 2026-10-06 拆池：云端功能各自独立「每日免费次数」（默认各 3 次/日）
    "cloud_commentary": 3,    # 视频解说 3 次/日
    "cloud_convert": 3,       # 在线转码 3 次/日
    "cloud_dewatermark": 3,   # 在线去水印（图片）3 次/日
    "cloud_subtitle": 3,      # 在线字幕提取 3 次/日
    # 🔴 2026-10-06 第二轮：每个云端功能彻底独立（一个键 = 一个用户可见功能）
    "cloud_concat": 3,            # 在线拼接 3 次/日
    "cloud_dewatermark_pdf": 3,   # 在线去水印（PDF）3 次/日
    "cloud_subtitle_burn": 3,     # 字幕烧录 3 次/日
    "cloud_subtitle_translate": 3,  # 字幕翻译 3 次/日
    # ── 6 个独立键（2026-10-06）─────────────────────────────────────
    "convert_video": 5,       # 视频格式转换 5 次/日
    "convert_audio": 5,       # 音乐转换 5 次/日
    "convert_image": 5,       # 图片转换 5 次/日
    "compress": 5,            # 高效压缩 5 次/日
    "sr": 5,                  # 高清修复（图片）5 次/日
    "bridge": 5,              # 音视频桥接 5 次/日
    # ── 第二轮新增 3 个（2026-10-06）──────────────────────────────
    "sr_video": 5,            # 视频超分 5 次/日
    "concat_video": 5,        # 视频拼接 5 次/日
    "concat_audio": 5,        # 音频拼接 5 次/日
    # 老键：存量归集用
    "app_compute": 5,
}
# 字幕提取(subtitle) 自 2026-09-13 起改为免费 2 次/日（会员无限），不列入不限配额。
# 评论/数据批量（DataTool 的功能，VDL V1 未实现）已于 2026-10-04 移除 → 空元组。
UNLIMITED_QUOTA: tuple[str, ...] = ()


def effective_daily_limits() -> tuple[dict[str, int], dict[str, int]]:
    """返回 `(会员每日配额, 免费每日配额)`，已叠加后台「免费额度」分栏的覆盖。

    🔴 2026-10-06：后台新增「免费额度」分栏后，每日配额不再是写死常量。
    覆盖层在 `plans.json` 的 `free_quota`：
      · `daily_member_limits` → 覆盖会员档（key 是 `FEATURE_USAGE_DEFS[].resource`）
      · `daily_free_limits`   → 覆盖免费档
    未覆盖的键落回上方代码常量（默认值永不丢失）。
    读脏值一律收敛：非整数丢弃、负数（会员不限用 -1）保留、其余按 int 取。
    """
    member = dict(DAILY_QUOTA_LIMITS)
    free = dict(FREE_DAILY_LIMITS)
    try:
        fq = (load_plan_overrides().get("free_quota") or {})
    except Exception:
        return member, free
    if not isinstance(fq, dict):
        return member, free
    # 🔴 只认**配额表里真实存在**的 resource 键（2026-10-06 修）。
    # 背景：前端保存时误把展示标识 `FEATURE_USAGE_DEFS[].key`（video_parse /
    # local_matting / subtitle_extract）当成 resource 提交，导致 plans.json 里
    # 躺着三条**永不生效**的垃圾项 —— 后台界面还照样显示它们，改次数却对
    # download/matting/subtitle 毫无作用。原先这里 `dst[key] = int(v)` 来者不拒，
    # 垃圾项就混进了生效的配额表。**根因在前端已修（data-key 改用 resource）**，
    # 这里再加白名单兜底：即便历史脏数据还在，也不会污染配额。
    valid = set(DAILY_QUOTA_LIMITS) | set(FREE_DAILY_LIMITS)
    for src, dst in (("daily_member_limits", member), ("daily_free_limits", free)):
        ov = fq.get(src)
        if not isinstance(ov, dict):
            continue
        for k, v in ov.items():
            key = str(k or "").strip()
            if not key or key not in valid:
                continue          # 未知 resource：忽略（不报错、不落进配额表）
            try:
                dst[key] = int(v)
            except (TypeError, ValueError):
                continue          # 脏值忽略：宁可回退默认，也不让整个配额表炸掉
    return member, free

def migrate_legacy_usage(usage: dict[str, Any]) -> dict[str, Any]:
    """把老键 `app_compute` 的已用量归集到新的独立键（2026-10-06 拆键）。

    🔴 背景：拆键前 6 个重活共用 `app_compute`。用户当天已用 3 次转换，
    拆键后 `convert` 是新键（值 0）⇒ 凭空多出 3 次额度。
    用户 2026-10-06 定档「旧记录归到 app_compute 总池」—— 这里的做法是：
    **读某个新键时，若老键有已用量而该新键为 0，则把老键的量算进该新键**。
    只读不写（不改用户的落盘数据），故：
      · 老键一直保留，总池数字始终可见、可对照；
      · 不会因为迁移而「清零」或「重复计数」。
    局限（诚实说明）：老键的量会**同时**被每个新键读到，所以拆键当天
    「用掉 3 次转换」会同时占掉压缩/修复/解说的 3 次 —— 这是「归集」的
    语义（宁可少放行，也不要凭空多给），次日日切后自然恢复正常。
    """
    if not isinstance(usage, dict):
        return usage
    try:
        legacy = int(usage.get("app_compute") or 0)
    except (TypeError, ValueError):
        return usage
    if legacy <= 0:
        return usage
    for k in ("convert_video", "convert_audio", "convert_image",
              "compress", "sr", "bridge"):
        try:
            if int(usage.get(k) or 0) <= 0:
                usage[k] = legacy          # 新键尚无记录 → 以老键的量起算
        except (TypeError, ValueError):
            usage[k] = legacy
    return usage


# AI 会员权益文案（供 plans().ai_member.features，前端会员中心渲染）
# 🔴 2026-10-04 定档：只写**真实存在**的能力。此前这里写过「AI 字幕识别 / 视频总结 /
#    图片翻译体验」三项，全仓搜不到对应路由 = 拿不存在的功能做卖点，已删。
# 现在写的每一项都有实现落点：
#   · 自动解说 —— server/routers/commentary.py + commentary-pipeline（LLM_API_KEY
#     硬依赖，见 process.py:157）；三种引擎 auto/mlx（本机跑）/ 云端网关 / 用户自带
#     Key，见 llm_config.inject_llm_env。
#   · 云端网关 —— server/gateway_config.py：真实 Key 只留服务端，本机只持可吊销令牌。
#   · 云端一键抠图 50 积分/次 —— routers/matting.py:321 `_credit_gate("matting_cloud")`，
#     是目前**唯一**扣 AI 积分的真实消费点（MATTING_CLOUD_CREDIT_COST）。
# ⚠️ 措辞守则：不得出现「本地」这类实现口径字眼（用户 2026-10-04 定档），
#    也不得列出未经核实的功能 —— 加条目前先确认有对应路由。
AI_FEATURES: list[str] = [
    "自动解说：写解说词 + 配音 + 出片",
    "高质量抠图：50 积分/次，效果更好",
    "赠送积分 5500 / 15000，30 天有效",
    "包含下载会员全部权益",
]

# 个人中心「今日使用」功能配额表（与前端表格四列对应：功能/体验剩余/权益余额/积分单价）
# - resource: 关联的 daily_usage 资源键
# - free_limit / member_limit: 每日体验配额（-1 表示不限）
# - ai_bonus: AI 会员周期内赠送额度（按 unit 单位）
# - credit_cost: 权益不足时按量扣积分单价（0 表示免费）
# 🔴 2026-10-04 铁律：**只有真有 use_daily/quota_state 拦截点的功能才能进这张表**。
#    守卫 test_feature_usage_gate.mjs 静态核对「每一行的 resource 都能在业务代码里
#    搜到拦截点」，防止再塞占位行（V1 时从 DataTool 抄了 8 行从未实现的功能）。
FEATURE_USAGE_DEFS: list[dict[str, Any]] = [
    # ── 🔴 2026-10-06 大改：按「用户看到的功能」列，不再按技术名词列 ──────────
    # 用户 2026-10-06 14:22 指出：后台只写「云端算力 / 本地重算力」是内部技术词，
    # 管理员看不出它管哪些功能，要求「得按功能来：视频下载、订阅追更、视频解说、
    # 本地字幕提取、音视频格式转换、音乐转换等」，这样才清楚。
    #
    # 因此每个 resource 拆成**多个功能行**（同一 resource 的几行共用一份额度，
    # 改其中任一行都改同一份 —— 这是刻意的，见 `shared_note`）：
    #   · 名称用**首页卡片上的原文**（视频工坊 app / web/index.html 的 #homeGrid），
    #     保证管理员在后台看到的名字 = 用户在 App 里点的名字；
    #   · `resource` 仍是配额表真键（决定改哪个数），这一列后台要显示成「共用额度」。
    # ⚠️ 拆行**不改变**任何计数逻辑：同一个 resource 的几行加起来还是那一份额度。
    {"key": "video_parse",      "name": "视频下载",     "resource": "download",    "unit": "次", "free_limit": 10, "member_limit": 1000, "ai_bonus": 0, "credit_cost": 0},
    {"key": "subscribe",        "name": "订阅追更",     "resource": "download",    "unit": "次", "free_limit": 10, "member_limit": 1000, "ai_bonus": 0, "credit_cost": 0, "shared_note": "与「视频下载」共用下载额度"},
    {"key": "commentary",       "name": "视频解说",     "resource": "cloud_commentary", "unit": "次", "free_limit": 3, "member_limit": 200, "ai_bonus": 0, "credit_cost": 0, "shared_note": "走「终身云端 3 次」额度（按功能独立计），并按 AI 积分计费（自动解说 40 积分 / 画面理解 50）"},
    # ⚠️ 名称不带「本地」：用户 2026-10-04 定档「会员权益文案不得出现 云端/算力/AI/本地」，
    # 当时把「本地一键抠图」改成「一键抠图」；这里同理（守卫 test_membership_benefits）。
    {"key": "subtitle_extract", "name": "字幕提取",     "resource": "subtitle",    "unit": "次", "free_limit": 2,  "member_limit": -1,   "ai_bonus": 0, "credit_cost": 0},
    {"key": "convert_video",    "name": "视频格式转换", "resource": "convert_video", "unit": "次", "free_limit": 5,  "member_limit": 200,  "ai_bonus": 0, "credit_cost": 0, "shared_note": "只含格式转换；视频拼接已独立计次"},
    {"key": "concat_video",     "name": "视频拼接",     "resource": "concat_video",  "unit": "次", "free_limit": 5,  "member_limit": 200,  "ai_bonus": 0, "credit_cost": 0, "shared_note": "多段视频合并为一条（独立额度）"},
    {"key": "convert_audio",    "name": "音乐转换",     "resource": "convert_audio", "unit": "次", "free_limit": 5,  "member_limit": 200,  "ai_bonus": 0, "credit_cost": 0, "shared_note": "音频格式互转；音频拼接已独立计次"},
    {"key": "concat_audio",     "name": "音频拼接",     "resource": "concat_audio",  "unit": "次", "free_limit": 5,  "member_limit": 200,  "ai_bonus": 0, "credit_cost": 0, "shared_note": "多段音频合并为一条（独立额度）"},
    {"key": "convert_image",    "name": "图片转换",     "resource": "convert_image", "unit": "次", "free_limit": 5,  "member_limit": 200,  "ai_bonus": 0, "credit_cost": 0, "shared_note": "png/jpg/webp/bmp/tiff/gif 等"},
    {"key": "compress",         "name": "高效压缩",     "resource": "compress",    "unit": "次", "free_limit": 5,  "member_limit": 200,  "ai_bonus": 0, "credit_cost": 0, "shared_note": "独立额度（此前与转换/修复共用）"},
    {"key": "sr",               "name": "高清修复",     "resource": "sr",          "unit": "次", "free_limit": 5,  "member_limit": 200,  "ai_bonus": 0, "credit_cost": 0, "shared_note": "图片修复；视频超分已独立计次"},
    {"key": "sr_video",         "name": "视频超分",     "resource": "sr_video",    "unit": "次", "free_limit": 5,  "member_limit": 200,  "ai_bonus": 0, "credit_cost": 0, "shared_note": "视频放大重建细节（独立额度）"},
    {"key": "bridge",           "name": "音视频桥接",   "resource": "bridge",     "unit": "次", "free_limit": 5,  "member_limit": 200,  "ai_bonus": 0, "credit_cost": 0, "shared_note": "合成 / 替换（此前无任何配额，现独立计）"},
    {"key": "local_matting",    "name": "一键抠图",     "resource": "matting",     "unit": "次", "free_limit": 8,  "member_limit": 500,  "ai_bonus": 0, "credit_cost": 0, "shared_note": "本机跑；选云端抠图时按 AI 积分计费（云端抠图 50 积分）"},
    # 🔴 下面三行是**网页版**的云端功能（跑在服务器上），与桌面端「视频解说」一起
    #    构成 4 个独立云端功能，各自独立「每日免费次数」+ 各自独立「终身免费次数」
    #    （2026-10-06 拆池，此前 4 个共用一个 cloud 总池，名字分了计数器没分）。
    #   · 名称用「在线」而非「云端」：用户 2026-10-04 定档「文案不得出现 云端/算力/
    #     AI/本地」（守卫 test_membership_benefits）。
    #   · 真实拦截点在 web-dev 的 convert/dewatermark/subtitle/subtitles 四个 router
    #     （cloud_quota_gate 按 resource 调 use_daily + 授权中心终身额度），桌面端
    #     视频解说走 quota.py 的终身额度 + 这里 use_daily(cloud_commentary)。
    {"key": "cloud_convert",    "name": "在线转码",     "resource": "cloud_convert",   "unit": "次", "free_limit": 3,  "member_limit": 200,  "ai_bonus": 0, "credit_cost": 0, "shared_note": "网页版：上传到服务器转码（免重传重转）"},
    {"key": "cloud_concat",     "name": "在线拼接",     "resource": "cloud_concat",    "unit": "次", "free_limit": 3,  "member_limit": 200,  "ai_bonus": 0, "credit_cost": 0, "shared_note": "网页版：多片段合并为一条（独立额度）"},
    {"key": "cloud_dewatermark","name": "图片去水印",   "resource": "cloud_dewatermark", "unit": "次", "free_limit": 3,  "member_limit": 200, "ai_bonus": 0, "credit_cost": 0, "shared_note": "网页版：图片框选去水印（另按 AI 积分计费）"},
    {"key": "cloud_dewatermark_pdf", "name": "PDF 去水印", "resource": "cloud_dewatermark_pdf", "unit": "次", "free_limit": 3, "member_limit": 200, "ai_bonus": 0, "credit_cost": 0, "shared_note": "网页版：PDF 框选去水印（独立额度）"},
    {"key": "cloud_subtitle",   "name": "字幕提取",     "resource": "cloud_subtitle",   "unit": "次", "free_limit": 3,  "member_limit": 200, "ai_bonus": 0, "credit_cost": 0, "shared_note": "网页版：字幕识别 / 提取"},
    {"key": "cloud_subtitle_burn", "name": "字幕烧录",   "resource": "cloud_subtitle_burn", "unit": "次", "free_limit": 3, "member_limit": 200, "ai_bonus": 0, "credit_cost": 0, "shared_note": "网页版：字幕压进画面（独立额度）"},
    {"key": "cloud_subtitle_translate", "name": "字幕翻译", "resource": "cloud_subtitle_translate", "unit": "次", "free_limit": 3, "member_limit": 200, "ai_bonus": 0, "credit_cost": 0, "shared_note": "网页版：字幕翻译（另按 AI 积分计费）"},
]

# --------------------------------------------------------------------------- #
# AI 积分成本表
# --------------------------------------------------------------------------- #
# 🔴 2026-10-05 用户定档（覆盖 2026-09-08 的「本地一律免费」旧口径）：
#   **云端算力 + 本机重算力都计入积分**，按「次」定价。
#   旧口径只对火山云端抠图计费，导致两个后果：① 本机跑 faster-whisper / LaMa /
#   MLX 解说 / 声音克隆 这些重算力白用；② 下表之外的调用点因 `credit_cost()`
#   对未知 op 返回 0 而**静默免费**（不报错、不告警）。
#
# 字段说明：
#   name   后台显示的功能名（用户视角）
#   where  真实调用点 file:line（后台展示用，便于核对「这个价对应哪段代码」）
#   cost   默认积分（可被 plans.json 的 credit_costs 覆盖，后台可改）
#   note   计价说明 / 内部备注
#
# 计费粒度：**按「次」定价，一次操作内发生多次模型调用只扣一份**
# （如一次「AI 抠图」内部 remove-bg 1~2 次 + enhance 1~2 次，重试成本含在单价里）。
# 调整单价即可覆盖重试成本，不做按调用次数累加 —— 那样用户看不懂，也难审计。
#
# 🔴 增删本表任一行都必须同步：
#   ① 这里；② `ALLOWED_CREDIT_OPS`（未登记的 op 会被告警而非静默放行）；
#   ③ 守卫 `test_ai_credit_costs.py`（钉住「每个计费项都要有真实调用点」、
#      「每条调用点都要有计费项」两个方向，防止再漏）。
MATTING_CLOUD_CREDIT_COST: int = 50   # 保留旧名，兼容既有引用

AI_CREDIT_COSTS: dict[str, dict[str, Any]] = {
    # ── 云端算力（真实花钱：平台承担上游费用）──────────────────────────────
    # 🔴 定价依据（2026-10-05）：按「ECS 上 144 次真实调用的 token 记录 × 厂商公开单价」
    #    测出平台真实成本，再按约 200 倍成本上限定价。`real_cost` 字段是测算依据，
    #    **调价时先看它**（后台表格也展示）。上游涨价时要重新测算。
    "matting_cloud": {
        "name": "云端一键抠图",
        "where": "server/routers/matting.py:321 → matting_ai.py → cloud_matting_mediakit.py",
        "cost": 50,
        "real_cost": "¥0.02~0.24/次（火山按次计费；一次操作内部 remove-bg 1~2 次 + enhance 0~2 次）",
        "note": "火山 MediaKit remove-image-background，含场景重试成本。⚠️ 图像能力单价未公开，"
                "此为按实测推的区间，建议在火山控制台核对。",
    },
    "matting_cloud_enhance": {
        "name": "AI 画质增强",
        "where": "server/cloud_matting_mediakit.py:607 enhance-image",
        "cost": 15,
        "real_cost": "¥0.037~0.075/次（代码内实测 ~0.037 元/张）",
        "note": "火山生成式 enhance-image（豆包），人像场景默认 professional。独立计费 API，"
                "不包含在云端抠图价内。",
    },
    "matting_vision": {
        "name": "AI 视觉定位 / 图像理解",
        "where": "server/vision_client.py:261/501/547/636/701（qwen-vl-max）",
        "cost": 5,
        "real_cost": "≈¥0.01/次（qwen-vl-max 输入 3 元/百万、输出 9 元/百万，单图约 1300 token）",
        "note": "「说扣什么」定位 + 连通域选择 + 文字块检测。失败自动回退本地。",
    },
    "commentary_llm": {
        "name": "自动解说（大模型写稿）",
        "where": "commentary-pipeline/scripts/llm_script.py:1646/2541/1111（云端网关）",
        "cost": 40,
        "real_cost": "¥0.04~0.19/次（实测：story 3518+317 token、script 3742+6474、tail 3949+4554；"
                     "DeepSeek V4-Flash 峰谷价，闲时 1.5/4.5、高峰 3/9 元每百万 token）",
        "note": "剧情分析 + 脚本生成 + 修复重试，一次任务一份（2~4 次调用）。走 ECS 网关，"
                "真实 Key 不在本机。⚠️ DeepSeek 2026-08-17 已涨价 50%~125%，涨价要重算。",
    },
    "commentary_vision": {
        "name": "解说画面理解",
        "where": "commentary-pipeline/scripts/vision_analysis.py:157（多模态模型）",
        "cost": 50,
        "real_cost": "≈¥0.24/次（抽 40 帧 ÷ 每批 8 帧 = 5 批 + 1 次总结 = 6 次调用）",
        "note": "本表里**单次最贵**的一项（把 40 张图全过一遍多模态模型）。默认关闭，"
                "用户主动开 --vision 才计。",
    },
    "subtitle_translate": {
        "name": "字幕翻译",
        "where": "server/subtitles.py:266（OpenAI 兼容，按 chunk 分片）",
        "cost": 5,
        "real_cost": "≈¥0.005/次（按小片段 1200+800 token 估）",
        "note": "长字幕会分多片，按一次操作一份计。",
    },
    # ── 本机重算力（用户机器上真实吃 CPU/GPU，平台成本≈0）──────────────────
    "subtitle_asr": {
        "name": "本地字幕提取",
        "where": "server/routers/subtitle.py:546/718（faster-whisper CPU int8）",
        "cost": 5,
        "real_cost": "¥0（只占用户机器）",
        "note": "含 SenseVoice 预识别。另有日配额（免费 2/日、会员无限）。",
    },
    "dewatermark_ai": {
        "name": "AI 去水印",
        "where": "server/dewatermark_ai.py（LaMa ONNX，onnxruntime 子进程）",
        "cost": 10,
        "real_cost": "¥0（占 1.5~2GB 内存 + 长时间 CPU）",
        "note": "🔴 原先**连日配额都没有**，等于白用本机 CPU（Explore 2026-10-05 实测）。",
    },
    "local_matting_ai": {
        "name": "本地 AI 抠图（BiRefNet / MODNet）",
        "where": "server/matting_ai.py（onnxruntime，本机）",
        "cost": 10,
        "real_cost": "¥0（只占用户机器）",
        "note": "另有日配额（免费 8/日、会员 500/日）。",
    },
    "commentary_local_mlx": {
        "name": "本机大模型解说",
        "where": "commentary-pipeline/scripts/llm_script.py:1221/1243（MLX 权重）",
        "cost": 40,
        "real_cost": "¥0（MLX 跑在用户机器；但长片会退化——llm_script.py:1167 实测本机 3B-4bit "
                     "在 45 分钟片上会变复读机，同一句解说词重复 147 次）",
        "note": "⚠️ 定 40 而非 0：定 0 用户会一律选本机，把成本转嫁给算力最差的机器、"
                "并换来更差的出片质量。定与云端同价（40）让「本机优先、云端兜底」这条路由"
                "（llm_script.py:2364）自然生效 —— 长片自动转云端不额外加价。"
                "若你更看重防套利，可上调至 120。",
    },
    "voice_clone": {
        "name": "声音克隆配音",
        "where": "server/app.py:2907 → voice_studio_client.py:98/113（Qwen3-TTS）",
        "cost": 30,
        "real_cost": "¥0（每句一次推理，但一次任务几十句 ⇒ 占用户机器较久）",
        "note": "每句一次推理，本机 7871 服务长驻。一条视频通常几十句 ⇒ 单次任务总占用可观。",
    },
}

# 已登记的计费 op 全集。任何 `credit_cost(op)` / `spend_for(op)` 传入表外的 op
# 都会被 `credit_cost()` 打一条 warning（不再静默返回 0）—— 忘记配价是财务漏洞，
# 必须能被看见。
ALLOWED_CREDIT_OPS: frozenset[str] = frozenset(AI_CREDIT_COSTS)


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


# `free_trial` 是扁平策略字典（enabled/mode/exclude/max_cost…），列入此表 ⇒ 后台可
# 只提交改动项（如只传 enabled）而不抹掉其余字段，与其余四张表同一套合并语义。
_SAVE_TABLE_KEYS = ("download_plans", "ai_plans", "credit_packs", "credit_costs",
                    "free_trial",
                    # 🔴 2026-10-06「免费额度」分栏：纯免费功能的每日配额覆盖
                    # （{daily_free_limits: {...}, daily_member_limits: {...}}）。
                    # 必须进白名单走**字段级合并** —— 否则后台只改「免费次数」也会把
                    # 「会员次数」整条抹掉（与 2026-10-03 那次「改销量清空套餐」同类）。
                    "free_quota")


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
# 套餐上架/改价回源节流：原 300s（云端被别处改动后桌面最长 5 分钟才反映）。
# 2026-10-04 配套修复：降到 15s，与会员状态节流对齐；桌面自己 push_plans_to_cloud
# 仍会主动刷新缓存，降 TTL 只让「被动感知云端变化」更快。
_CLOUD_PLANS_TTL = 15.0


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


def push_free_quota_to_cloud() -> dict[str, Any]:
    """把本机 `free_quota.cloud_lifetime` 覆盖下发授权中心（后台保存后调用）。

    🔴 2026-10-06：终身免费次数的**放行判定在云端**，本机 plans.json 只是缓存。
    此前后台保存只写本机 ⇒ 改了不生效（自查发现，承诺了做不到）。现在与套餐价格
    同一套路：保存即下发，云端按覆盖值判定，两端口径一致。
    返回同步状态，**绝不抛出**（云端不可达时后台仍应保存成功，只是云端未同步）。
    """
    url = _license_api("/api/license/free_quota_set")
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
        fq = (load_plan_overrides().get("free_quota") or {})
        # 键**不存在** = 管理员没配过覆盖 → 不要去动云端（避免把别的机器配的覆盖抹掉）；
        # 键存在但为 null = 显式「恢复默认」→ 下发 null 让中心清除覆盖。
        has_key = "cloud_lifetime" in fq
        if not has_key:
            return {"ok": True, "reason": "no_override", "effective": {}}
        import license_client
        r = license_client.free_quota_set_remote(
            token, cloud_lifetime=(fq.get("cloud_lifetime") if has_key else None))
        if isinstance(r, dict) and r.get("ok"):
            return {"ok": True, "effective": r.get("effective_cloud_limits") or {}}
        return {"ok": False, "reason": str((r or {}).get("error") or "cloud_rejected")}
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


# ── 档位售卖状态（2026-10-03）：模式 / 秒杀 / 限量 / 活动时间 ──────────────────
# 后台「套餐与积分成本」可给每一档手填这些字段（存在 plans.json 覆盖层里）：
#   on_sale     上架开关（false = 隐藏该档，前端不展示、不可下单）
#   mode        normal 普通 / flash_sale 秒杀 / limited 限量 / event 活动
#   badge       角标文案（前端卡片上显示，如「限时 5 折」）
#   desc        补充说明（前端卡片副文案）
#   flash_price 秒杀价（mode=flash_sale 且在秒杀窗口内时取代 price_cny）
#   flash_start / flash_end  秒杀窗口（Unix 秒）
#   start_at / end_at        活动/售卖窗口（Unix 秒，0 = 不限）
#   stock       限量总份数（0 = 不限）；sold 已售份数
PLAN_MODES = ("normal", "flash_sale", "limited", "event")


def _ts(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def plan_sales_state(plan: dict[str, Any], now: Optional[float] = None) -> dict[str, Any]:
    """算出一档当前的售卖状态与现价（前端置灰、下单校验共用这一份口径）。

    返回：buyable 是否可买、reason 不可买原因（中文）、price 现价、
    original_price 原价、is_flash 是否秒杀中、flash_phase 秒杀窗口三态
    （upcoming 未开始 / active 进行中 / ended 已结束 / none 非秒杀模式）、
    remaining 剩余份数（None=不限）、
    start_at/end_at 售卖窗口、mode 模式、badge/desc 展示字段。
    """
    now = float(now if now is not None else time.time())
    p = dict(plan or {})
    mode = str(p.get("mode") or "normal")
    if mode not in PLAN_MODES:
        mode = "normal"
    on_sale = p.get("on_sale", True)
    on_sale = True if on_sale is None else bool(on_sale)
    base_price = float(p.get("price_cny") or 0)
    start_at = _ts(p.get("start_at"))
    end_at = _ts(p.get("end_at"))
    fs = _ts(p.get("flash_start"))
    fe = _ts(p.get("flash_end"))
    flash_price = float(p.get("flash_price") or 0)
    in_flash = bool(flash_price > 0 and fs and fe and fs <= now <= fe)
    # 秒杀窗口三态：前端据此决定「秒杀价/划线原价/秒杀角标/秒杀至…
    # 要不要出现」。窗口已过必须一切收起 —— 否则价格早已回原价，卡片却还挂着
    # 「限时秒杀」角标和「秒杀至 …」，用户会误以为自己正在享受秒杀价。
    flash_phase = "none"
    if mode == "flash_sale" and flash_price > 0 and fs and fe:
        if now < fs:
            flash_phase = "upcoming"
        elif now <= fe:
            flash_phase = "active"
        else:
            flash_phase = "ended"
    stock = int(p.get("stock") or 0)
    sold = int(p.get("sold") or 0)
    remaining = (stock - sold) if stock > 0 else None

    reason = ""
    if not on_sale:
        reason = "已下架"
    elif start_at and now < start_at:
        reason = "活动未开始"
    elif end_at and now > end_at:
        reason = "活动已结束"
    elif stock > 0 and sold >= stock:
        reason = "已售罄"

    return {
        "mode": mode,
        "on_sale": on_sale,
        "buyable": (reason == ""),
        "reason": reason,
        "price": (flash_price if in_flash else base_price),
        "original_price": base_price,
        "is_flash": in_flash,
        "flash_phase": flash_phase,
        "flash_price": flash_price,
        "flash_start": fs,
        "flash_end": fe,
        "start_at": start_at,
        "end_at": end_at,
        "stock": stock,
        "sold": sold,
        "remaining": remaining,
        "badge": str(p.get("badge") or ""),
        "desc": str(p.get("desc") or ""),
    }


def ensure_plan_buyable(code: str, now: Optional[float] = None) -> dict[str, Any]:
    """下单/激活前的可买校验。不通过时返回 {"ok": False, "error": 中文原因}。"""
    eff = effective_plans()
    for cat in ("download_plans", "ai_plans", "credit_packs"):
        plan = (eff.get(cat) or {}).get(code)
        if not plan:
            continue
        st = plan_sales_state(plan, now=now)
        if not st["buyable"]:
            return {"ok": False, "error": st["reason"] or "该套餐当前不可购买",
                    "state": st, "category": cat}
        return {"ok": True, "state": st, "category": cat}
    return {"ok": False, "error": "套餐不存在或已下架"}


def effective_pay_plans() -> dict[str, dict[str, Any]]:
    """下单用套餐表：{code: {price, name, grant}}，与 payment_core.PAY_PLANS 同构。

    🔴 价格单一真源（2026-09-30）：支付内核若继续读 payment_core 的硬编码
    PAY_PLANS，超管在后台改的价格**不会影响真实扣款金额**。故改由本函数按
    「覆盖层 → 代码常量」实时产出，注入 PaymentService。
    """
    eff = effective_plans()
    now = time.time()
    out: dict[str, dict[str, Any]] = {}
    for key in ("download_plans", "ai_plans", "credit_packs"):
        for code, p in eff[key].items():
            # 现价走售卖状态：秒杀窗口内用秒杀价，避免「页面显示秒杀、下单按原价」
            st = plan_sales_state(p, now=now)
            out[code] = {"price": float(st["price"] or 0),
                         "name": p.get("label") or code, "grant": code,
                         "buyable": bool(st["buyable"]), "reason": st["reason"]}
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
                        # 🔴 档位内部也要**字段级**合并（2026-10-03）：原来整条替换，
                        # 只提交一个字段（如限量计数 sold +1）会把该档的 price/days/
                        # stock/mode 全抹掉 —— 表现为「改一次销量，套餐配置被清空」。
                        base = merged.get(ik)
                        if isinstance(base, dict) and isinstance(iv, dict):
                            merged[ik] = {**base, **iv}
                        elif isinstance(iv, dict) and not isinstance(base, dict):
                            # 🔴 类型变了（如 cloud_lifetime 由 int 拆池成 dict）→ 直接覆盖，
                            # 不能 `{**base, **iv}`（base 是 int 会 TypeError）。
                            merged[ik] = iv
                        else:
                            merged[ik] = iv
                existing[k] = merged
            else:
                existing[k] = v
        p = plan_override_path()
        atomic_io.atomic_write_json(p, existing)
        _PLAN_OVERRIDE_CACHE = (p.stat().st_mtime_ns, existing)
        return existing


_UNLOGGED_OPS: set[str] = set()


def credit_cost(op: str, sub: str | None = None) -> int:
    """查询某次 AI 操作的积分成本。

    优先级：**plans.json 的 `credit_costs` 覆盖层 → 代码默认表 `AI_CREDIT_COSTS`**。
    成本 0 = 该功能免费（表里显式写 0，或功能本身不消耗算力）。

    🔴 2026-10-05 起查表而非 `if op == "matting_cloud"`，并对**表外 op 打 warning**。
    旧实现对任何未知 op 直接 `return 0` ⇒ 新加的功能忘记配价就**静默免费**，
    不报错、不告警 —— 这是财务漏洞的温床（本次盘点的 P0 发现）。
    同一次进程里每个未知 op 只告警一次（`_UNLOGGED_OPS` 去重），避免刷屏。
    """
    costs = (load_plan_overrides().get("credit_costs") or {})
    if op in costs:
        try:
            return int(costs[op])
        except (TypeError, ValueError):
            logging.getLogger("membership").warning(
                "credit_costs[%r] 不是整数，忽略覆盖值", op)

    row = AI_CREDIT_COSTS.get(op)
    if row is None:
        if op not in _UNLOGGED_OPS:
            _UNLOGGED_OPS.add(op)
            logging.getLogger("membership").warning(
                "🔴 credit_cost(%r) 未在 AI_CREDIT_COSTS 中登记 → 按免费处理。"
                "若该功能真实消耗算力，请补一行（backend/membership.py::AI_CREDIT_COSTS）。",
                op)
        return 0
    return int(row.get("cost", 0))


def credit_cost_table() -> list[dict[str, Any]]:
    """后台展示用：把成本表与运行时生效价合成一张（**不返回任何凭据**）。

    `effective` 是实际会扣的积分（覆盖层优先），`default` 是代码默认。
    两者不等说明管理员改过价，前端据此高亮。
    """
    costs = (load_plan_overrides().get("credit_costs") or {})
    out: list[dict[str, Any]] = []
    for op, row in AI_CREDIT_COSTS.items():
        default = int(row.get("cost", 0))
        try:
            eff = int(costs[op])
        except (KeyError, TypeError, ValueError):
            eff = default
        out.append({
            "op": op,
            "name": str(row.get("name") or op),
            "where": str(row.get("where") or ""),
            "note": str(row.get("note") or ""),
            # 🔴 2026-10-05：平台真实成本（按实测 token × 厂商公开单价测算）。
            #    后台改价前先看它 —— 上游涨价时凭这个重算，别只按感觉调。
            "real_cost": str(row.get("real_cost") or ""),
            "default": default,
            "effective": eff,
            "overridden": eff != default,
        })
    # 表外但覆盖层里有的（历史遗留 / 已下线功能）也列出来，便于清理
    known = set(AI_CREDIT_COSTS)
    for op, v in costs.items():
        if op in known:
            continue
        try:
            eff = int(v)
        except (TypeError, ValueError):
            eff = 0
        out.append({
            "op": op, "name": op, "where": "", "note": "⚠️ 覆盖层里有此项，但代码表里没有对应定义（可能已下线，建议清理）",
            "default": 0, "effective": eff, "overridden": True, "orphan": True,
        })
    return out


# --------------------------------------------------------------------------- #
# 免费用户「首次体验」策略（2026-10-05 用户定档）
# --------------------------------------------------------------------------- #
# 背景：2026-10-05 把 AI 积分墙补齐后，积分池为 0 的免费用户**任何 AI 功能第一次点就
# 撞 402**，连"这东西到底好不好用"都无从判断 —— 等于把功能在漏斗最前面就掐死了。
# 用户定档（2026-10-05 晚二次修正）：**只要是首次使用的账号，其首次使用免费一次**。
# 早先一度是「每个功能各免一次」，改回来是因为那等于给每个账号发 11 张门票，
# 单个免费账号能薅走的平台成本约 ¥0.8，砍难度低；改成 once 后人均 ≤¥0.25，
# 且更符合「先让你尝一口」的原始意图。
#
# 记录落在会员状态文件自身的 `free_trials` 键（随账号走，不是全局），键名语义：
#   mode=per_op → 每个 op 一个名额，键是 op 名；
#   mode=once   → 全站共用一个名额，键是 "*"。
#
# 🔴 **它的边界**：这只是账号级控制，换个注册账号仍能重薅。真要收紧得靠用户改不了的
#    维度（强设备指纹 / 授权中心去重），而不是靠本地文件。once 口径下单个免费账号
#    占用的平台成本上限约 ¥0.25（取各条目 real_cost 的最大者量级）。
DEFAULT_FREE_TRIAL_POLICY: dict[str, Any] = {
    "enabled": True,
    # once = 账号首次使用免费一次（默认，2026-10-05 晚用户定档）｜
    # per_op = 每个功能各免一次｜off = 关闭
    "mode": "once",
    # 不参与试用的 op（把最贵的解说/画面理解排除就用这个，人均成本立刻降到 ¥0.3 量级）
    "exclude": [],
    # 会员（下载或 AI 任一活跃）是否也享受 —— 默认不给：会员已按套餐拿到积分，
    # 用完请复购，不拿"试用"给会员兜底，否则付费纪律会被自己松开。
    "members_too": False,
    # 0 = 不限；>0 时单价超过该积分的功能不参与试用（比维护 exclude 列表省事）
    "max_cost": 0,
}
_TRIAL_MODES = ("per_op", "once", "off")
_TRIAL_ONCE_KEY = "*"


def free_trial_policy() -> dict[str, Any]:
    """运行时生效的试用策略：plans.json 的 `free_trial` 覆盖层 → 代码默认。

    后台写到 `save_plan_overrides({"free_trial": {...}})`；逐个字段合并，
    管理员只改一项（比如只切开关）不会把其余字段抹掉。
    读到的脏值一律收敛到合法范围 —— 后台写坏配置不该让整个扣费链路炸掉
    （fail-open 的方向是「退回默认口径」而不是「全站免单」）。
    """
    ov = load_plan_overrides().get("free_trial")
    out = dict(DEFAULT_FREE_TRIAL_POLICY)
    if isinstance(ov, dict) and ov:
        for k, v in ov.items():
            if v is None:
                continue
            out[k] = v
    mode = str(out.get("mode") or "once").strip().lower()
    out["mode"] = mode if mode in _TRIAL_MODES else "once"
    if out["mode"] == "off":
        out["enabled"] = False
    out["enabled"] = bool(out.get("enabled"))
    ex = out.get("exclude")
    out["exclude"] = [str(x) for x in ex] if isinstance(ex, (list, tuple, set)) else []
    try:
        out["max_cost"] = max(0, int(out.get("max_cost") or 0))
    except (TypeError, ValueError):
        out["max_cost"] = 0
    out["members_too"] = bool(out.get("members_too"))
    return out


def _trial_key(op: str, mode: str) -> str:
    return _TRIAL_ONCE_KEY if mode == "once" else str(op)


def spend_for(store: "MembershipStore", op: str, sub: str | None = None,
              reason: str | None = None) -> dict[str, Any]:
    """按成本扣 AI 积分。返回 spend_credits 的结果 dict（ok/error）。

    成本 <=0 视为免费操作，直接返回 ok=True（不污染积分池）。
    积分不足且命中「首次体验」名额时按放行处理（`trial=True`），不扣积分。
    """
    cost = credit_cost(op, sub)
    if cost <= 0:
        return {"ok": True, "spent": 0, "free": True, "credits_left": store.status()["credits_total"]}
    res = store.spend_credits(cost, reason=reason or op)
    if res.get("ok"):
        return res
    # 与 gate_message 同口径：这里的试用分支让「不走 gate_message 的调用点」也不会漏。
    if store.trial_available(op, cost):
        if store.trial_consume(op, reason=reason or op):
            return {"ok": True, "spent": 0, "free": True, "trial": True,
                    "credits_left": store.status()["credits_total"]}
        # else: 云端已被另一台领走 → 走正常扣分失败返回
    return res


def can_afford(store: "MembershipStore", op: str, sub: str | None = None) -> str:
    """**只查不扣**：返回 "" = 付得起；返回人话原因 = 付不起。零副作用。

    🔴 2026-10-06 新增。**绝不能用 `gate_message` 当探针** —— 它会先
    `spend_credits` 真扣积分，余额不够时还会 `trial_consume` 消耗「首次体验」
    名额。拿它做「要不要升级云端」的预判，等于**问一次就烧掉一次免费机会**
    （实测：0 积分用户探测后日志出现 `free trial consumed op=matting_cloud`）。

    判定顺序与 gate_message 一致（先积分、后试用名额），只是不动账：
      1. 单价 <= 0 → 免费功能，放行；
      2. 订阅积分 + 永久积分 >= 单价 → 放行；
      3. 该 op 还有首次体验名额（`trial_available` 是只读）→ 放行；
      4. 否则返回与 gate_message 同措辞的原因（前端直接展示）。
    """
    try:
        cost = int(credit_cost(op, sub))
    except Exception:  # noqa: BLE001
        return ""                      # 单价查不到 → 保守放行，交给事后真扣费兜底
    if cost <= 0:
        return ""
    try:
        st = store._state                      # noqa: SLF001 — 同模块内读状态，不改
        ai = st.get("ai_member") or {}
        ai_left = int(ai.get("credits_left", 0)) if ai.get("active") else 0
        perm_total = int((st.get("permanent_credits") or {}).get("total", 0))
        if ai_left + perm_total >= cost:
            return ""
        pol = free_trial_policy()
        if pol.get("enabled") and store.trial_available(op, cost):
            return ""
        if str(pol.get("mode")) == "once":
            return ("你的账号已用过一次免费体验（每个账号限一次），请开通 AI 会员或购买积分包")
        return f"{op} 积分不足（需要 {cost}，当前 {ai_left + perm_total}）"
    except Exception as e:  # noqa: BLE001 — 探针异常按「付得起」处理，不误伤用户
        logging.getLogger("membership").warning("can_afford probe error op=%s: %s", op, e)
        return ""


def gate_message(store: "MembershipStore", op: str, sub: str | None = None,
                 reason: str | None = None) -> str | None:
    """扣积分并产出拦截原因：None=放行；非 None=「积分不足」原因字符串（供 402 detail）。

    成本 <=0 视为免费放行；spend 系统异常时降级放行（记日志），不阻断主流程。

    🔴 2026-10-05「免费用户首次体验」：这里的判定顺序是
        **先扣积分 → 扣不动才动用试用名额 → 都不行才拦**。
       1. 先扣：账户里还有积分就不占用试用名额 —— 名额一次性资源，有余额时烧掉它
          等于白送，用户本可以用这次名额去试更贵的功能。
       2. 后试用：余额不够时若该 op 还剩名额，标记用掉并返回 None（放行、不扣分）。
       3. 都不行：文案里如实说明「该功能的免费体验是否已用过」，别只丢一句
          「积分不足」—— 用户上次明明跑通了同一个操作，会以为是系统出错了。
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

    left = res.get("credits_left", 0)
    pol = free_trial_policy()
    hint = ""
    if pol.get("enabled") and store.trial_available(op, cost):
        try:
            if store.trial_consume(op, reason=reason or op):
                logging.getLogger("membership").info(
                    "free trial consumed op=%s cost=%s user=%s", op, cost,
                    getattr(store.path, "name", ""))
                return None
            # 被另一台领走：trial_consume 已把本机标记为已用 → 走下面的 402 提示
        except Exception as e:  # noqa: BLE001
            logging.getLogger("membership").warning("free trial consume failed op=%s: %s", op, e)
            hint = "请开通 AI 会员或购买积分包"
    # 兜底提示：目录已没名额（本机用过 / 云端被另一台领走）
    if not hint:
        # once 口径下名额是账号级的：**不是**"这个功能试过了"，而是"这个账号试过了"。
        # 措辞必须对得上，否则用户第二次点的是另一个功能，却被告知"本功能"已用过，
        # 会当成系统串号报错。
        if str(pol.get("mode")) == "once":
            hint = ("你的账号已用过一次免费体验（每个账号限一次），请开通 AI 会员或购买积分包"
                    if store.trial_used(op, pol) else "请开通 AI 会员或购买积分包")
        else:
            hint = ("本功能的免费体验已用过一次，请开通 AI 会员或购买积分包"
                    if store.trial_used(op, pol) else "请开通 AI 会员或购买积分包")
    return f"AI 积分不足：本次操作需 {cost} 积分，当前 {left}（{hint}）"

# --------------------------------------------------------------------------- #
# 状态文件
# --------------------------------------------------------------------------- #


def default_state_path() -> Path:
    """状态文件默认路径 ~/.video-downloader/membership.json（frozen 兼容）。

    同样受 VDL_DATA_DIR 覆盖（见 _base_dir），保证测试不写真实家目录。
    """
    return _base_dir() / "membership.json"


# 已下线资源的历史计数键（2026-10-04 随占位功能一并移除）
# 加载旧状态文件与跨日重置时都要清掉，否则死键会继续出现在 status() / usage_summary()。
_DEAD_USAGE_KEYS: tuple[str, ...] = (
    "original", "batch_material", "ai_subtitle", "subtitle_batch", "image_translate",
)


def _empty_state() -> dict[str, Any]:
    return {
        "download_member": {"active": False, "plan": None, "expire_at": 0.0},
        "ai_member": {"active": False, "plan": None, "expire_at": 0.0,
                      "grant_credits": 0, "credits_left": 0, "feature_credits": {}},
        "permanent_credits": {"total": 0, "packs": []},
        "daily_usage": {"date": "", "download": 0, "subtitle": 0, "cloud": 0,
                        "cloud_commentary": 0, "cloud_convert": 0,
                        "cloud_dewatermark": 0, "cloud_subtitle": 0,
                        "matting": 0, "app_compute": 0},
        "usage_history": {},
        # 免费用户「首次体验」已用记录：{op 或 "*": 使用时刻}（见 DEFAULT_FREE_TRIAL_POLICY）
        "free_trials": {},
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
    for k in ("download_member", "ai_member", "permanent_credits", "daily_usage",
              "usage_history", "meta", "free_trials"):
        if isinstance(data.get(k), dict):
            st[k].update(data[k])
    # 2026-10-04：丢弃已下线资源的历史计数键（_DEAD_USAGE_KEYS）。
    # 放在这里而不是只在跨日重置里清，是为了**读到旧文件那一刻就干净**——
    # 否则用户当天不做任何操作时，死键仍会出现在 status() / usage_summary() 里。
    for dead in _DEAD_USAGE_KEYS:
        st["daily_usage"].pop(dead, None)
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
#
# 🔴 2026-10-04 用户定档：**条目全部保留，只把「云端 / 算力 / AI / 本地」这几个字
#    从文案里去掉**（它们是架构与成本口径，不是用户视角的权益）。
#    例：原来「本地一键抠图 500 次/日」→ 现在「一键抠图 500 次/日」；
#        「云端算力（转码/拼接/…）」→「在线处理（转码/拼接/…）」。
#    ⚠️ 不是隐藏条目 —— 每条权益、配额、限流都照旧（见 test_benefits_hide_impl_wording 守卫）。
# 🔴 2026-10-06 拆键后新增 5 条独立权益（此前 6 个重活共用 app_compute 一条）。
# 文案守则（用户 2026-10-04 定档，守卫 test_membership_benefits）：不得出现
# 「云端 / 算力 / AI / 本地」——用「在线」「本机」以外的中性说法。
_BENEFIT_FROM_LIMITS: tuple[tuple[str, str], ...] = (
    ("download", "下载任务 {v} 次/日"),
    ("matting", "一键抠图 {v} 次/日"),
    # 🔴 2026-10-06 拆池：4 个云端功能各自独立权益（此前共用「在线处理」一条）
    ("cloud_commentary", "视频解说 {v} 次/日"),
    ("cloud_convert", "在线转码 {v} 次/日"),
    ("cloud_concat", "在线拼接 {v} 次/日"),
    ("cloud_dewatermark", "图片去水印 {v} 次/日"),
    ("cloud_dewatermark_pdf", "PDF 去水印 {v} 次/日"),
    ("cloud_subtitle", "字幕提取 {v} 次/日"),
    ("cloud_subtitle_burn", "字幕烧录 {v} 次/日"),
    ("cloud_subtitle_translate", "字幕翻译 {v} 次/日"),
    ("convert_video", "视频格式转换 {v} 次/日"),
    ("concat_video", "视频拼接 {v} 次/日"),
    ("convert_audio", "音乐转换 {v} 次/日"),
    ("concat_audio", "音频拼接 {v} 次/日"),
    ("convert_image", "图片转换 {v} 次/日"),
    ("compress", "高效压缩 {v} 次/日"),
    ("sr", "高清修复 {v} 次/日"),
    ("sr_video", "视频超分 {v} 次/日"),
    ("bridge", "音视频桥接（合成 / 替换）{v} 次/日"),
    # 老键：仅存量归集用，不作为对外权益展示（否则用户看到「视频处理」会以为
    # 转换/压缩/超分还共用一份额度 —— 拆键后已不共用）
)
# 不走每日配额、但属于会员权益的说明项
_BENEFIT_EXTRA: tuple[dict[str, str], ...] = (
    {"key": "quality", "text": "清晰度：1080P 及以上全部开放（>1080P 需会员）"},
    {"key": "devices", "text": "同一账号 2 台设备同时在线"},
    {"key": "speed", "text": "高速通道 · 全速不限速"},
    {"key": "no_credits", "text": "不含积分额度：抠图等功能需另购积分包"},
    {"key": "support", "text": "优先客服支持"},
)


def download_benefits() -> list[dict[str, str]]:
    """下载会员权益清单：配额项自动跟随 DAILY_QUOTA_LIMITS + 不限项 + 静态说明。

    2026-10-04 起不再有隐藏名单：条目全部保留，只把「云端/算力/AI/本地」几个字
    从文案里去掉了（见 _BENEFIT_FROM_LIMITS 上方注释）。
    """
    # 🔴 2026-10-07：权益文案同样要跟随后台「免费额度」分栏的**会员档**覆盖（与
    #   quota_state 同口径）。此前直读 DAILY_QUOTA_LIMITS 常量 ⇒ 后台把「会员每日」
    #   改完后再看会员中心，文案还是旧数字（同型漂移）。
    mem_limits, _free_limits = effective_daily_limits()
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for key, tpl in _BENEFIT_FROM_LIMITS:
        v = int(mem_limits.get(key) or 0)
        if v > 0:
            out.append({"key": key, "text": tpl.format(v=v)})
            seen.add(key)
    # 🔴 2026-10-06 拆键：老键（app_compute）只在存量归集时读，**不对外展示**
    # 权益 —— 展示它会让用户以为「转换/压缩/超分还共用一份额度」，而拆键后已不共用。
    # 它仍留在 DAILY_QUOTA_LIMITS 里（历史用量归集 + 后台对照），故这里显式跳过。
    _skip = set(LEGACY_QUOTA_KEYS)
    if _skip:
        out = [x for x in out if x["key"] not in _skip]
    # member_limit = -1 的功能 = 会员不限次（字幕提取等）；同样取生效值
    for d in FEATURE_USAGE_DEFS:
        k = str(d.get("key"))
        _ml = int(mem_limits.get(d["resource"], d.get("member_limit", 0)) or 0)
        if _ml == -1 and k not in seen:
            out.append({"key": k, "text": f"{d.get('name') or k}：不限"})
            seen.add(k)
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
        # 免费用户的「首次体验」余量（2026-10-05）：逐个 op 算好，前端直接用，
        # 不必自己照着策略推导一遍 —— 推导逻辑分家几乎必然出现「表单显示能试用、
        # 真点下去被拦」的错位。
        out["free_trials"] = self.free_trial_view()
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

        优先级：代码常量 ← plans.json 覆盖层 ← 授权中心覆盖（云端最高，与网页端一致）。
        展示必须走 effective_plans()：云端不可达时自动退化为本机覆盖层 + 代码常量。
        🔴 此前此函数只读本机 plans.json（load_plan_overrides），导致「后台下架/改价只推
        云端」时桌面端不跟随 —— 本地 on_sale=true、云端 on_sale=false，桌面仍显示上架
        （2026-10-04 实测）。现改为走 effective_plans()，与网页端对齐，云端成为单一真源。
        """
        eff = effective_plans()
        now = time.time()
        dl = {c: dict(p, state=plan_sales_state(p, now=now)) for c, p in eff["download_plans"].items()}
        ai = {c: dict(p, state=plan_sales_state(p, now=now)) for c, p in eff["ai_plans"].items()}
        cp = {c: dict(p, state=plan_sales_state(p, now=now)) for c, p in eff["credit_packs"].items()}
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

        # 售卖状态校验（2026-10-03）：下架 / 活动未开始 / 已结束 / 售罄 → 直接拒。
        # 限量档在**发放成功后**自动 +1（sold），后台可看到实时已售数。
        _all_plans: dict[str, dict[str, Any]] = {}
        _all_plans.update(_dl_plans)
        _all_plans.update(_ai_plans)
        _all_plans.update(_credit_packs)
        if code in _all_plans:
            _st = plan_sales_state(_all_plans[code], now=now)
            if not _st["buyable"]:
                return {"ok": False, "error": _st["reason"] or "该套餐当前不可购买",
                        "state": _st}

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
        # 限量档售出 +1（写回覆盖层，让后台「已售」实时反映）
        if code in _all_plans and int(_all_plans[code].get("stock") or 0) > 0:
            try:
                cat = next(k for k in ("download_plans", "ai_plans", "credit_packs")
                           if code in _all_plans)
                save_plan_overrides({cat: {code: {
                    "sold": int(_all_plans[code].get("sold") or 0) + 1}}})
            except Exception:  # noqa: BLE001 — 计数失败不影响发放
                pass
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

    # ---- 免费用户「首次体验」名额（2026-10-05）---- #
    def _trials_raw(self) -> dict[str, Any]:
        """已用名额表。惰性建键，旧状态文件没有 `free_trials` 也能正常跑。"""
        self._ensure_loaded()
        tr = self._state.get("free_trials")
        if not isinstance(tr, dict):
            tr = {}
            self._state["free_trials"] = tr
        return tr

    def trial_available(self, op: str, cost: int = 0) -> bool:
        """该 op 现在还能不能走「免费体验」。五道判据缺一不可。

        注意这里**只看本 store 的会员标志，不调 status()**：status() 会连带调用
        `free_trial_view()` → 回到 `trial_available()`，就成了无限递归。
        """
        pol = free_trial_policy()
        if not pol.get("enabled"):
            return False
        if op in (pol.get("exclude") or []):
            return False
        mc = int(pol.get("max_cost") or 0)
        if mc > 0 and int(cost or 0) > mc:
            return False
        if not pol.get("members_too") and self._is_download_active():
            return False          # 会员按付费纪律自己承担，不拿试用兜底
        return _trial_key(op, str(pol.get("mode"))) not in self._trials_raw()

    def trial_used(self, op: str, pol: dict[str, Any] | None = None) -> bool:
        """该 op 的试用名额是否已经用掉（只判状态，不改状态）。"""
        pol = pol or free_trial_policy()
        return _trial_key(op, str(pol.get("mode"))) in self._trials_raw()

    def _cloud_token(self) -> str:
        """取本 store 的云账号 token；本 store 没有时回落全局 store。

        🔴 2026-10-06 修复（跨端「仅一次」静默失效根因）：
        云端账号由 cloud_account 写入**全局** `app.member_store`，而 `current_member_store(request)`
        对「已登录本地账号」返回的是 **per-user** store —— 二者不一致，导致免费体验名额跨端
        领取读不到 token → `_trial_claim_remote` 直接 `offline` 静默 fail-open（本机放行、
        不上报授权中心），跨端「仅一次」形同虚设。这里回落全局 store 取到真实 token。
        """
        try:
            t = str(((self._state.get("meta") or {}).get("account") or {}).get("token") or "").strip()
            if t:
                return t
        except Exception:
            pass
        try:
            import app  # 懒导入，避免与 app 的循环依赖
            gstore = getattr(app, "member_store", None)
            if gstore is not None:
                gstore._ensure_loaded()
                gt = str(((gstore._state.get("meta") or {}).get("account") or {}).get("token") or "").strip()
                if gt:
                    return gt
        except Exception:
            pass
        return ""

    def _trial_claim_remote(self, op: str, mode: str) -> str:
        """跨端原子领取免费名额。返回 'fresh' | 'already' | 'offline'。

        - 'fresh'   : 本端首次成功领取全局唯一名额（放行）
        - 'already' : 另一台设备/网页已领走（本端应拒绝）
        - 'offline' : 授权中心不可达，fail-open 视为本端领取
        不改动本地状态，由调用方按结果落本地盘。
        """
        try:
            self._ensure_loaded()
            token = self._cloud_token()
        except Exception:
            return "offline"
        if not token:
            return "offline"
        try:
            import license_client
            r = license_client.trial_claim_remote(token, op, mode)
            if not r or not r.get("ok"):
                return "offline"
            return "already" if r.get("already") else "fresh"
        except Exception:
            return "offline"

    def trial_consume(self, op: str, reason: str = "") -> bool:
        """标记名额已用；跨端唯一：先去授权中心做原子领取，按结果定夺。

        返回 True=放行（本端首次领取成功 / 离线兜底），False=被另一台领走（应拒绝）。
        与 `spend_credits` 同时机 —— **在任务真正开跑之前**就占掉（防「故意制造失败」白嫖）。
        """
        pol = free_trial_policy()
        mode = str(pol.get("mode"))
        key = _trial_key(op, mode)
        res = self._trial_claim_remote(op, mode)
        if res == "already":
            # 另一台已领走：本机同步标记为已用（UI 一致），但本次拒绝
            self._trials_raw()[key] = self._now()
            self._persist()
            return False
        # fresh 或 offline：本端领取成功（离线兜底放行）
        self._trials_raw()[key] = self._now()
        meta = self._state.setdefault("meta", {})
        meta.setdefault("history", []).append({
            "type": "free_trial", "op": op, "reason": reason, "at": self._now(),
        })
        meta["history"] = meta["history"][-200:]
        self._persist()
        return True

    def free_trial_view(self) -> dict[str, Any]:
        """给 status() / 前端用的试用视图：{-enabled, mode, remaining:{op:0|1}, used:[op]}。

        `remaining` 逐个 op 算真实余量（而不是只回一个 enabled 布尔），这样会员中心
        能直接渲染「还剩几次免费体验」，不用前端自己再照着策略推导一遍。
        """
        pol = free_trial_policy()
        mode = str(pol.get("mode"))
        remaining: dict[str, int] = {}
        for op in AI_CREDIT_COSTS:
            remaining[op] = 1 if self.trial_available(op, int(credit_cost(op))) else 0
        # 🔴 once 口径下所有 op 共用同一个名额，`sum(remaining)` 会算出 11 张票，
        #    前端拿去渲染「还剩 11 次」就全错。此时真实余量只有 0/1 两种取值。
        used_any = any(remaining[op] == 0 and self.trial_used(op, pol)
                       for op in AI_CREDIT_COSTS)
        if mode == "once":
            left = 0 if self.trial_used(_TRIAL_ONCE_KEY, pol) else 1
            remaining_count = left
        else:
            remaining_count = sum(remaining.values())
        return {
            "enabled": bool(pol.get("enabled")),
            "mode": mode,
            "max_cost": int(pol.get("max_cost") or 0),
            "exclude": list(pol.get("exclude") or []),
            "members_too": bool(pol.get("members_too")),
            "remaining": remaining,
            "remaining_count": remaining_count,
            "used_any": used_any,
        }

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
            # 🔴 2026-10-06：改为**按配额表自动清零**，不再写死键名。
            # 原写法只清 6 个硬编码键 —— 拆键后新增的 convert / compress / sr /
            # bridge **永远不会跨日重置**（用户第二天额度仍显示用尽）。
            # 写死键名是「加新键时忘记加日切」这类 bug 的根源。
            for _k in list(du.keys()):
                if _k != "date":
                    du[_k] = 0
            for _dead in _DEAD_USAGE_KEYS:      # 清掉历史状态文件里的死键
                du.pop(_dead, None)

    def quota_state(self, resource: str) -> dict[str, Any]:
        """查询某资源的当日用量/上限（按当前档位：免费 or 会员）。unlimited 恒放行。"""
        self._ensure_loaded()
        self._roll_daily(self._now())
        if resource in UNLIMITED_QUOTA:
            return {"resource": resource, "allowed": True, "unlimited": True}
        is_member = self._is_download_active()
        # 🔴 2026-10-06：走生效值（叠加后台「免费额度」分栏的覆盖），不再直读常量
        mem_limits, free_limits = effective_daily_limits()
        limit_map = mem_limits if is_member else free_limits
        limit = limit_map.get(resource)
        if limit is None and mem_limits.get(resource) is None:
            # 未知资源：V1 不设卡（保守默认放行，避免误伤功能）
            return {"resource": resource, "allowed": True, "unknown": True}
        if limit is None:
            # 免费表未覆盖但会员表有（原画/批量）→ 免费额度为 0
            limit = 0
        # 🔴 拆键兼容：把老键 app_compute 的已用量归集到新独立键（只读迁移）
        _mig = migrate_legacy_usage(self._state["daily_usage"])
        used = int(_mig.get(resource, 0))
        return {"resource": resource, "limit": limit, "used": used,
                "remaining": max(0, limit - used),
                "allowed": used < limit,
                "tier": "member" if is_member else "free",
                "member_limit": mem_limits.get(resource),
                "free_limit": free_limits.get(resource, 0)}

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
    # 🔴 历史用量只能从 _state 读：status() 是「对外公开视图」，刻意不暴露
    # usage_history（体积大、含逐日明细）。原来这里读 st["usage_history"]，
    # 恒为 {} → 个人中心「使用统计」表的近三日/近七日/本月**永远显示 0**，
    # 而后台「用户使用详情」读 _state 所以有数据，两边对不上（2026-10-03 实测）。
    du = st.get("daily_usage", {})
    hist = dict(getattr(store, "_state", None) or {}).get("usage_history") or {}
    totals: dict[str, int] = {}
    for d in dates:
        src = du if d == du.get("date") else (hist.get(d) or {})
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
    # 🔴 2026-10-07：日限额必须取**生效值**（叠加后台「免费额度」分栏的覆盖），与真正
    #   的放行判定 `quota_state()` 同源。此前本函数直读 `FEATURE_USAGE_DEFS` 里烘焙的
    #   `free_limit` / `member_limit` ⇒ 后台把「视频下载 10→3」改完后，功能已按 3 次
    #   拦（quota_state 走的是覆盖值），但本表仍显示 10/10 —— 用户 2026-10-07 截图
    #   反馈「更改了为什么没有生效」，实为**显示层与拦截层口径漂移**。
    mem_limits, free_limits = effective_daily_limits()
    limit_map = mem_limits if is_member else free_limits
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
        _lim = limit_map.get(d["resource"])
        if _lim is None:
            # 当前档位的配额表里没有这个 resource：免费档缺但会员档有（原画 / 批量）
            # ⇒ 免费额度为 0（与 quota_state 同口径）；其余情况回退表内烘焙值，
            # 保证「表里少登记一行」时也不会显示成 0/未定义。
            _lim = 0 if (not is_member and mem_limits.get(d["resource"]) is not None) \
                else int(d.get("member_limit", 0) if is_member else d.get("free_limit", 0))
        limit = int(_lim)
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
