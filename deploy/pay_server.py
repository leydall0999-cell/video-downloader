#!/usr/bin/env python3
"""VDL 支付服务（支付宝 · 独立进程，与授权中心解耦）。

定位：处理「用户真付款 -> 自动开通对应套餐」的闭环。
  - 支付宝异步回调需公网可达 => 本服务部署在内地 ECS（pay.hanyuxz.top），nginx 反代 /api/pay/。
  - 下单需云端登录 token（证明是谁买的）；回调验签确认真付款后，内部调授权中心的
    /api/license/grant 把档位写到该账号（state 单写者原则：本服务不直接写授权中心 state）。
  - 金额以本服务 PRICE_MAP 为准（服务端真源），前端只传 plan_code，防改价。

端点：
  GET  /healthz
  GET  /api/pay/return                        网页支付同步回跳落地页（提示返回 App）
  POST /api/pay/create          {token, plan_code, client?} ->
                                {order_id, mode, qr_png(base64), qr, pay_url, amount, plan_code}
                                mode: face2face | page | wap
  POST /api/pay/alipay/notify                 支付宝异步通知，返回 success/failure（纯文本）
  POST /api/pay/query           {order_id}     -> {status: PENDING|PAID|GRANT_FAILED}

依赖：python-alipay-sdk, qrcode, Pillow（ECS venv 安装）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import secrets
import string
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs
import urllib.parse
# 🔴 必须显式导入子模块：`import urllib.parse` 只绑定包名，不会把 urllib.request
#    挂上去。虎皮椒下单 `_place_xunhu()` 用 urllib.request.Request/urlopen，
#    若依赖 `_grant()` 里那处函数级 import 的副作用，则「本进程尚未发过货」时
#    必报 `module 'urllib' has no attribute 'request'`（2026-10-09 实测踩中）。
import urllib.error
import urllib.request

# ── 配置 ──────────────────────────────────────────────────────────────────── #
PORT = int(os.environ.get("VDL_PAY_PORT") or "8903")
DATA_DIR = Path(os.environ.get("VDL_LICENSE_DATA") or os.path.expanduser("~/.vdl-license"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
ORDERS_PATH = DATA_DIR / "pay_orders.json"
# 与授权中心共享同一 secret，用于解登录 token 取邮箱（user_id）
SECRET = (os.environ.get("VDL_LICENSE_SECRET") or "").strip()
ADMIN_TOKEN = (os.environ.get("VDL_LICENSE_ADMIN_TOKEN") or "").strip()
# 发货内部调用：授权中心与本服务同机（license 监听 127.0.0.1:8902），直连本机。
# 🔴 不要再走 https://hanyuxz.top 公网回环 —— 多一跳 CF，CF 抖动/证书问题会让
#    「已收款」的订单卡在 GRANT_FAILED，需人工对账补发。同机直连排除该故障面。
GRANT_URL = (os.environ.get("VDL_GRANT_URL")
             or "http://127.0.0.1:8902/api/license/grant")
ALIPAY_CFG = Path(os.environ.get("VDL_ALIPAY_CFG") or "/opt/vdl-license/alipay.json")
TOKEN_TTL = float(os.environ.get("VDL_LICENSE_TOKEN_TTL_DAYS") or "30") * 86400.0

# 支付渠道选择（2026-10-08 新增「虎皮椒」第三方聚合，个人免执照可用）
# ------------
#   alipay   = 官方支付宝（当面付/电脑网站/手机网站，按 PAY_MODE 自动选；需商户资质+密钥）
#   xunhupay = 虎皮椒聚合（个人实名即用，无需营业执照/ICP 备案；支付宝+微信都支持，
#              异步回调验签后自动发货，与支付宝同款 UX：返回收银台 URL 或扫码）
#   默认 alipay（保持既有行为不变）；拿到虎皮椒密钥后设 VDL_PAY_CHANNEL=xunhupay 即切换。
PAY_CHANNEL = (os.environ.get("VDL_PAY_CHANNEL") or "alipay").strip().lower()

# 支付产品选择
# ------------
# 2026-10-08 实测：本应用（appid 2021007104663693）**没有当面付资质** ——
# precreate / trade.query 均返回 isv.insufficient-isv-permissions；而应用归属
# 「网页/移动应用」，可用的是**电脑网站支付(page.pay) / 手机网站支付(wap.pay)**。
#   face2face = 当面付 precreate，直接出 qr_code（UX 最好，需资质）
#   page      = 电脑网站支付，返回收银台 URL（桌面端默认）
#   wap       = 手机网站支付，返回收银台 URL（移动端默认）
#   auto      = 先试当面付，遇权限不足**自动回退**网页支付（默认值：
#               将来一旦拿到当面付资质，无需改任何配置即自动切回二维码 UX）
PAY_MODE = (os.environ.get("VDL_PAY_MODE") or "auto").strip().lower()
ALIPAY_GATEWAY_DEFAULT = "https://openapi.alipay.com/gateway.do"
# return_url：支付宝收银台付完后的同步跳转落地页。由 get_alipay() 依据 notify_base
# 推导为 <notify_base>/api/pay/return（nginx 已把 /api/pay/ 前缀转到本服务）。
_RETURN_URL = ""

# 金额真源（与 App membership.DOWNLOAD_PLANS / AI_PLANS / CREDIT_PACKS 对齐，单位元）
PRICE_MAP: dict[str, dict[str, Any]] = {
    "download_1day":      {"price": "1.90",   "subject": "视频工坊·下载会员1天卡"},
    "download_3day":      {"price": "4.90",   "subject": "视频工坊·下载会员3天卡"},
    "download_7day":      {"price": "9.90",   "subject": "视频工坊·下载会员7天卡"},
    "download_month":     {"price": "29.80",  "subject": "视频工坊·下载会员月卡"},
    "download_half_year": {"price": "99.90",  "subject": "视频工坊·下载会员半年卡"},
    "download_year":      {"price": "179.00", "subject": "视频工坊·下载会员年卡"},
    "ai_5500":            {"price": "49.90",  "subject": "视频工坊·AI积分月会员"},
    "ai_15000":           {"price": "99.90",  "subject": "视频工坊·AI月会员"},
    "credits_5000":       {"price": "50.00",  "subject": "视频工坊·5000积分包"},
    "credits_15000":      {"price": "99.00",  "subject": "视频工坊·15000积分包"},
}

# ── 登录 token 解析（与授权中心同算法，共享 secret）─────────────────────────── #
def _b64u_decode(s: str) -> str:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("ascii")).decode("utf-8")


def parse_token(token: str, secret: str, now: Optional[float] = None) -> str:
    now = now or time.time()
    try:
        raw = _b64u_decode(token)
        user_id, iat_s, exp_s, mac = raw.split("|")
        iat, exp = float(iat_s), float(exp_s)
    except Exception:
        return ""
    if now > exp:
        return ""
    msg = f"{user_id}|{iat_s}|{exp_s}".encode("utf-8")
    expect = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(expect, mac):
        return ""
    return user_id


# ── 支付宝客户端（懒加载，凭据缺失时下单报错）───────────────────────────────── #
_alipay = None
_alipay_err = ""


def get_alipay():
    global _alipay, _alipay_err, _RETURN_URL
    if _alipay is not None or _alipay_err:
        return _alipay, _alipay_err
    try:
        from alipay import AliPay
    except Exception as e:  # SDK 未装
        _alipay_err = f"alipay SDK 未安装: {e}"
        return None, _alipay_err
    if not ALIPAY_CFG.exists():
        _alipay_err = f"支付宝配置缺失: {ALIPAY_CFG}"
        return None, _alipay_err
    try:
        cfg = json.loads(ALIPAY_CFG.read_text(encoding="utf-8"))
    except Exception as e:
        _alipay_err = f"支付宝配置解析失败: {e}"
        return None, _alipay_err
    appid = (cfg.get("appid") or "").strip()
    priv = _ensure_pem(cfg.get("app_private_key", ""), "PRIVATE")
    pub = _ensure_pem(cfg.get("alipay_public_key", ""), "PUBLIC")
    if not appid or not priv or not pub:
        _alipay_err = "支付宝配置不完整(需 appid + app_private_key + alipay_public_key)"
        return None, _alipay_err
    notify_base = (cfg.get("notify_base") or "https://pay.hanyuxz.top").rstrip("/")
    # 同步 return_url 与异步 notify 同源，指向本服务自己的落地页（nginx 已转 /api/pay/）。
    _RETURN_URL = notify_base + "/api/pay/return"
    debug = bool(cfg.get("debug", False))
    try:
        _alipay = AliPay(
            appid=appid,
            app_notify_url=f"{notify_base}/api/pay/alipay/notify",
            app_private_key_string=priv,
            alipay_public_key_string=pub,
            sign_type="RSA2",
            debug=debug,
        )
    except Exception as e:
        _alipay_err = f"支付宝初始化失败: {e}"
        return None, _alipay_err
    return _alipay, ""


def _ensure_pem(key: str, kind: str) -> str:
    key = (key or "").strip()
    if not key:
        return ""
    if "BEGIN" in key:
        return key
    lines = [key[i:i + 64] for i in range(0, len(key), 64)]
    if kind == "PRIVATE":
        return ("-----BEGIN RSA PRIVATE KEY-----\n" + "\n".join(lines)
                + "\n-----END RSA PRIVATE KEY-----\n")
    return "-----BEGIN PUBLIC KEY-----\n" + "\n".join(lines) + "\n-----END PUBLIC KEY-----\n"


# ── 虎皮椒（xunhupay）聚合支付：个人免执照通道 ─────────────────────────────── #
# 文档：https://www.xunhupay.com/  接入：POST https://api.xunhupay.com/payment/do.html
# 签名：所有参与参数按 key 升序拼接为 key=value&...（值做 urlencode，与 PHP
#       http_build_query 一致），末尾拼接 appsecret 后取 md5。验签同理（移除 hash）。
_xunhu_cfg = None
_xunhu_err = ""


def _xunhu_sign(params: dict[str, Any], appsecret: str) -> str:
    """虎皮椒签名（等价官方 PHP generate_xh_hash）：
    ksort(键 ASCII 升序) → 逐项拼 `key=value` 用 `&` 连接 → 末尾**直接**拼 appsecret → md5(32 位小写)。

    🔴 两处必须照抄官方，2026-10-09 实测踩中：
      1. **值不做 urlencode**：官方是 `$arg .= "$key=$val"` 取原值。早期实现用
         quote_plus 编码值 —— 本服务标题含中文（"视频工坊·下载会员1天卡"）、
         notify_url 含 `:` `/`，编码后 hash 与服务端算出的不一致。
      2. **空值不参与签名**（官方 `is_null($val) || $val === ''` 跳过），`hash` 自身不参与。
    """
    items = [(k, params[k]) for k in sorted(params.keys())
             if k != "hash" and params[k] is not None and str(params[k]) != ""]
    qs = "&".join(f"{k}={v}" for k, v in items)
    return hashlib.md5((qs + appsecret).encode("utf-8")).hexdigest()


def _xunhu_native_code(url_qrcode: str) -> str:
    """把 `url_qrcode` 还原成微信原生支付链接 `weixin://wxpay/bizpayurl?pr=…`。

    官方口径：返回值里 `url` 是手机端专用跳转，`url_qrcode` 是 PC 端二维码。
    实测（2026-10-09）`url_qrcode` 会 **302** 到
    `…/qrcode/<appid>.html?data=<base64(weixin://wxpay/bizpayurl?pr=…)>&…`，
    把 `data` 做 base64 解码即得原生支付链接 —— 扫这个码可直接调起微信支付，
    比让用户扫码后跳网页收银台更稳。取不到时返回空串，调用方回落到收银台 URL。
    """
    def _from_query(u: str) -> str:
        try:
            data = urllib.parse.parse_qs(urllib.parse.urlparse(u).query).get("data", [""])[0]
        except Exception:
            return ""
        if not data:
            return ""
        try:
            s = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "ignore")
        except Exception:
            return ""
        return s if s.startswith("weixin://") else ""

    direct = _from_query(url_qrcode)
    if direct:
        return direct

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):  # noqa: D102
            return None

    try:
        urllib.request.build_opener(_NoRedirect).open(
            urllib.request.Request(url_qrcode, method="GET"), timeout=10)
    except urllib.error.HTTPError as he:
        return _from_query(he.headers.get("Location") or "")
    except Exception:
        return ""
    return ""


def get_xunhu() -> Any:
    """返回 (appid, appsecret, gateway, notify_base) 或错误字符串。

    配置默认读 /opt/vdl-license/xunhupay.json：{appid, appsecret,
    gateway?(默认 https://api.xunhupay.com/payment/do.html),
    notify_base?(默认 https://pay.hanyuxz.top)}。
    """
    global _xunhu_cfg, _xunhu_err
    if _xunhu_cfg is not None or _xunhu_err:
        return _xunhu_cfg or _xunhu_err
    p = Path(os.environ.get("VDL_XUNHU_CFG") or "/opt/vdl-license/xunhupay.json")
    if not p.exists():
        _xunhu_err = f"虎皮椒配置缺失: {p}"
        return _xunhu_err
    try:
        c = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        _xunhu_err = f"虎皮椒配置解析失败: {e}"
        return _xunhu_err
    appid = (c.get("appid") or "").strip()
    appsecret = (c.get("appsecret") or "").strip()
    if not appid or not appsecret:
        _xunhu_err = "虎皮椒配置不完整(需 appid + appsecret)"
        return _xunhu_err
    gateway = (c.get("gateway") or "https://api.xunhupay.com/payment/do.html").strip()
    notify_base = (c.get("notify_base") or "https://pay.hanyuxz.top").rstrip("/")
    _xunhu_cfg = (appid, appsecret, gateway, notify_base)
    return _xunhu_cfg


def _place_xunhu(subject: str, order_id: str, price: str, client: str) -> dict[str, Any]:
    """虎皮椒下单：返回 {mode, qr_code, pay_url}。

    - **不传 `payment`**：本账号渠道为「微信支付四」，网关按渠道自动返回微信收银台。
      实测（2026-10-09）传 `payment=alipay` / `wechat` / 不传，返回 `url` 一律为
      `payments/wechat/…`，该参数对结果无影响且会污染签名集合。
    - `time`（秒级时间戳）与 `nonce_str`（随机串）是**官方必填项**，缺任一项网关直接报
      「缺少参数appid,time,hash或他们的值不合法」（2026-10-09 实测踩中）。
    - `url_qrcode` 还原为 `weixin://wxpay/bizpayurl?pr=…` 原生码，交上层 `_qr_png()`
      生成二维码 —— 桌面端扫码即可直接调起微信支付（主路径）；
      `url`（收银台）作为 `pay_url` 供「在浏览器中打开收银台」兜底。
    """
    cfg = get_xunhu()
    if isinstance(cfg, str):
        raise RuntimeError(cfg)
    appid, appsecret, gateway, notify_base = cfg
    params: dict[str, Any] = {
        "version": "1.1",
        "appid": appid,
        "trade_order_id": order_id,
        "total_fee": price,
        "title": subject,
        "time": str(int(time.time())),
        "nonce_str": secrets.token_hex(16),
        "notify_url": notify_base + "/api/pay/xunhupay/notify",
        "return_url": notify_base + "/api/pay/return",
    }
    params["hash"] = _xunhu_sign(params, appsecret)
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        gateway, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        resp = json.loads(r.read().decode("utf-8"))
    if resp.get("errcode") != 0:
        raise RuntimeError("虎皮椒下单失败: %s" % (resp.get("errmsg") or "未知错误"))
    pay_url = resp.get("url") or ""
    qr_code = _xunhu_native_code(resp.get("url_qrcode") or "")
    if not qr_code and not pay_url:
        raise RuntimeError("虎皮椒未返回收银台/二维码地址")
    return {"mode": "xunhupay", "qr_code": qr_code, "pay_url": pay_url}


# ── 订单存储 ──────────────────────────────────────────────────────────────── #
_LOCK = threading.Lock()


def _load_orders() -> dict[str, Any]:
    try:
        return json.loads(ORDERS_PATH.read_text(encoding="utf-8") or "{}")
    except Exception:
        return {}


def _save_orders(o: dict[str, Any]) -> None:
    tmp = ORDERS_PATH.with_name(ORDERS_PATH.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(o, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, ORDERS_PATH)


def _gen_order_id() -> str:
    return "VDLP" + time.strftime("%Y%m%d%H%M%S") + "".join(
        secrets.choice(string.hexdigits[:16]) for _ in range(4))


def _qr_png(text: str) -> str:
    import qrcode
    img = qrcode.make(text, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _pay_return_html() -> str:
    """网页支付（page.pay/wap.pay）付完后的同步跳转落地页。

    同步 return 只负责「告诉用户成了、可以回 App」；真正的开通由**异步 notify**
    完成（更可靠）。所以这里绝不做开通动作、也不依赖 return 参数。
    """
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>支付完成 · 视频工坊</title></head>"
        "<body style=\"margin:0;font-family:-apple-system,system-ui,'PingFang SC',sans-serif;"
        "background:#f6f7f9;color:#1a1a1a\">"
        "<div style=\"max-width:420px;margin:18vh auto;padding:32px 24px;background:#fff;"
        "border-radius:16px;text-align:center;box-shadow:0 2px 16px rgba(0,0,0,.06)\">"
        "<div style=\"font-size:38px;line-height:1;color:#12B76A\">&#10003;</div>"
        "<h1 style=\"font-size:18px;font-weight:600;margin:12px 0 8px\">支付已完成</h1>"
        "<p style=\"font-size:14px;color:#666;margin:0 0 6px\">请返回「视频工坊」，会员将自动开通。</p>"
        "<p style=\"font-size:13px;color:#999;margin:0\">若未自动开通，请稍候片刻，或在 App 内重新查看会员状态。</p>"
        "</div></body></html>"
    )


# ── grant 内部调用（写档位到账号，state 单写者原则）──────────────────────────── #
def _grant(email: str, plan_code: str, note: str = "alipay-auto") -> dict[str, Any]:
    # note 带 order_id（alipay-auto:<order_id>）时，授权中心每日对账可把这笔发货
    # 精确对应到已收款订单；不带则只能按 (账号,套餐) 就近兜底匹配。
    import urllib.request
    body = json.dumps({"email": email, "plan": plan_code, "note": note[:120],
                       "token": ADMIN_TOKEN}).encode("utf-8")
    req = urllib.request.Request(GRANT_URL, data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        out = json.loads(r.read().decode("utf-8"))
    # 🔴 必须校验业务层 ok：授权中心若返回 HTTP 200 但 ok:false（软失败/未来改版），
    #    不校验会让 _notify 把订单误标成 PAID —— 「已收款、订单显示已发货、用户没到账」。
    if not (isinstance(out, dict) and out.get("ok")):
        raise RuntimeError(f"grant rejected: {str(out)[:200]}")
    return out


# ── 下单：按可用支付产品自动选择（当面付 / 电脑网站支付 / 手机网站支付）────────── #
def _place_order(alipay, subject: str, order_id: str, price: str, client: str) -> dict[str, Any]:
    """按可用支付产品下单，返回 {mode, qr_code, pay_url}。

    - face2face：当面付，直接返回 qr_code（二维码）
    - page/wap ：电脑/手机网站支付，返回收银台 pay_url

    AUTO 模式下先试当面付，遇权限不足（isv.insufficient-isv-permissions）自动回退网页支付。
    ⚠️ page/wap 是**纯本地签名**，本地永远能拿到 URL（拿到 ≠ 一定可支付），
       真实是否可付只有在用户打开收银台时才暴露 —— 所以不能拿「签名成功」当可用判据。
    全部候选失败时抛 RuntimeError（附各候选错误摘要），由调用方转 400。
    """
    gateway = getattr(alipay, "_gateway", ALIPAY_GATEWAY_DEFAULT)
    web_mode = "wap" if client == "mobile" else "page"
    if PAY_MODE in ("face2face", "page", "wap"):
        order = [PAY_MODE]
    else:  # auto（含未知值）：先当面付，再回退网页支付
        order = ["face2face", web_mode]

    errors: list[str] = []
    for m in order:
        try:
            if m == "face2face":
                r = alipay.api_alipay_trade_precreate(subject, order_id, price)
                if r.get("code") == "10000" and r.get("qr_code"):
                    return {"mode": "face2face", "qr_code": r.get("qr_code", ""), "pay_url": ""}
                errors.append("当面付:%s" % (
                    r.get("sub_code") or r.get("sub_msg") or r.get("msg") or "失败"))
                continue
            fn = (alipay.api_alipay_trade_wap_pay if m == "wap"
                  else alipay.api_alipay_trade_page_pay)
            qs = fn(subject, order_id, price, return_url=(_RETURN_URL or None))
            if not qs:
                errors.append("%s:空支付串" % m)
                continue
            return {"mode": m, "qr_code": "", "pay_url": gateway + "?" + qs}
        except Exception as e:  # 该产品不可用 → 继续试下一个候选
            errors.append("%s:%s" % (m, str(e)[:80]))
    raise RuntimeError("; ".join(errors) or "无可用支付产品")


# ── HTTP Handler ──────────────────────────────────────────────────────────── #
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a) -> None:  # 静默
        pass

    def _cors(self) -> None:
        # 前端支付从 web(hanyuxz.top) / 桌面(localhost:8321) 跨域打到本服务，
        # 且带 Authorization / X-Api-Key / X-Device-Id 等头，必须放行 CORS + 预检。
        origin = self.headers.get("Origin", "")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization, X-Api-Key, "
                         "X-Subscription-Key, X-Device-Id, X-Requested-With")
        self.send_header("Access-Control-Max-Age", "600")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.end_headers()
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _text(self, status: int, text: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self._cors()
        self.end_headers()
        self.wfile.write(text.encode("utf-8"))

    def _html(self, status: int, html: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _body(self) -> dict[str, Any]:
        try:
            ln = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(ln) or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/healthz", "/api/pay/healthz"):
            self._json(200, {"ok": True, "service": "vdl-pay",
                             "channel": PAY_CHANNEL, "mode": PAY_MODE})
        elif p == "/api/pay/return":
            # 网页支付同步回跳落地页（付完回 App 的提示）
            self._html(200, _pay_return_html())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        p = self.path.split("?")[0]
        if p == "/api/pay/create":
            self._create()
        elif p == "/api/pay/alipay/notify":
            self._notify()
        elif p == "/api/pay/xunhupay/notify":
            self._xunhu_notify()
        elif p == "/api/pay/query":
            self._query()
        else:
            self._json(404, {"error": "not found"})

    def _create(self):
        data = self._body()
        token = str(data.get("token") or "")
        plan_code = str(data.get("plan_code") or "")
        client = str(data.get("client") or "").strip().lower()
        if plan_code not in PRICE_MAP:
            return self._json(400, {"ok": False, "error": "未知套餐", "code": "BAD_PLAN"})
        email = parse_token(token, SECRET)
        if not email:
            return self._json(401, {"ok": False, "error": "登录态失效，请重新登录",
                                    "code": "BAD_TOKEN"})
        # 桌面/移动判定：前端传 client 优先，否则按 UA 兜底（决定用 page 还是 wap）
        if client not in ("mobile", "desktop"):
            ua = (self.headers.get("User-Agent") or "").lower()
            client = ("mobile" if any(k in ua for k in ("iphone", "ipad", "android", "mobile"))
                      else "desktop")
        price = PRICE_MAP[plan_code]["price"]
        subject = PRICE_MAP[plan_code]["subject"]
        order_id = _gen_order_id()
        if PAY_CHANNEL == "xunhupay":
            try:
                res = _place_xunhu(subject, order_id, price, client)
            except Exception as e:
                return self._json(400, {"ok": False, "error": f"虎皮椒下单失败: {e}",
                                        "code": "XUNHU_REJECT"})
        else:
            alipay, err = get_alipay()
            if err:
                return self._json(500, {"ok": False, "error": err, "code": "ALIPAY_CFG"})
            try:
                # notify_url 由 SDK 从 app_notify_url 自动注入（AliPay._app_notify_url），
                # 不要显式传 alipay.app_notify_url —— SDK 未暴露该公有属性，会 AttributeError。
                res = _place_order(alipay, subject, order_id, price, client)
            except Exception as e:
                return self._json(400, {"ok": False, "error": f"支付宝下单失败: {e}",
                                        "code": "ALIPAY_REJECT"})
        mode = res["mode"]
        qr_code = res.get("qr_code") or ""
        pay_url = res.get("pay_url") or ""
        with _LOCK:
            o = _load_orders()
            o[order_id] = {"email": email, "plan_code": plan_code, "amount": price,
                           "mode": mode, "status": "PENDING", "created_at": time.time()}
            _save_orders(o)
        # 二维码来源：当面付用 qr_code；网页支付用收银台 URL（两者都能被支付宝 App
        # 扫码打开）—— 这样桌面端仍保留「扫码支付」主 UX；网页支付另有「在浏览器
        # 打开收银台」按钮兜底（见前端 openPayModal）。
        src = qr_code or pay_url
        try:
            qr_png = _qr_png(src) if src else ""
        except Exception:
            qr_png = ""
        self._json(200, {"ok": True, "order_id": order_id, "mode": mode,
                         "qr_png": qr_png, "qr": qr_code, "pay_url": pay_url,
                         "amount": price, "plan_code": plan_code})

    def _notify(self):
        # 支付宝异步通知：form-urlencoded POST
        try:
            ln = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(ln).decode("utf-8")
        except Exception:
            return self._text(500, "failure")
        params = {k: v[0] for k, v in parse_qs(raw).items()}
        alipay, err = get_alipay()
        if err:
            return self._text(500, "failure")
        sign = params.pop("sign", "")
        params.pop("sign_type", None)
        try:
            ok = alipay.verify(params, sign)
        except Exception:
            ok = False
        if not ok:
            return self._text(400, "failure")
        trade_status = params.get("trade_status", "")
        order_id = params.get("out_trade_no", "")
        trade_no = params.get("trade_no", "")
        # 非终态（如 WAIT_BUYER_PAY）也回 success，避免支付宝无谓重试
        if trade_status not in ("TRADE_SUCCESS", "TRADE_FINISHED"):
            return self._text(200, "success")
        with _LOCK:
            o = _load_orders()
            ordr = o.get(order_id)
            if not ordr:
                return self._text(200, "success")
            if ordr.get("status") == "PAID":
                return self._text(200, "success")  # 幂等
            email = ordr["email"]
            plan_code = ordr["plan_code"]
            ordr["status"] = "GRANTING"
            _save_orders(o)
        try:
            _grant(email, plan_code, note=f"alipay-auto:{order_id}")
            with _LOCK:
                o = _load_orders()
                o[order_id]["status"] = "PAID"
                o[order_id]["paid_at"] = time.time()   # 对账用：入账时间（授权中心 recon 读取）
                if trade_no:
                    o[order_id]["trade_no"] = trade_no
                _save_orders(o)
            return self._text(200, "success")
        except Exception as e:
            with _LOCK:
                o = _load_orders()
                o[order_id]["status"] = "GRANT_FAILED"
                o[order_id]["err"] = str(e)[:200]
                # 已收款只是发货失败 —— 对账口径里 GRANT_FAILED = 已收款未发货，会告警补发
                o[order_id]["paid_at"] = time.time()
                if trade_no:
                    o[order_id]["trade_no"] = trade_no
                _save_orders(o)
            # 仍回 success 避免支付宝无限重试；后台可查 GRANT_FAILED 补单
            return self._text(200, "success")

    def _xunhu_notify(self):
        # 虎皮椒异步通知：form-urlencoded POST。验签（md5）确认真付款后调授权中心发货。
        # 字段：trade_order_id / transaction_id / total_fee / type / status(OD=已付) / hash
        try:
            ln = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(ln).decode("utf-8")
        except Exception:
            return self._text(500, "failure")
        params = {k: v[0] for k, v in parse_qs(raw).items()}
        cfg = get_xunhu()
        if isinstance(cfg, str):
            return self._text(500, "failure")
        appid, appsecret, gateway, notify_base = cfg
        recv_hash = params.pop("hash", "")
        params.pop("sign", None)  # 兼容字段
        calc = _xunhu_sign(params, appsecret)
        if not hmac.compare_digest(calc, recv_hash):
            return self._text(400, "failure")
        order_id = (params.get("trade_order_id")
                    or params.get("out_trade_order_id") or "")
        status = params.get("status", "")
        # 非终态（WP 等）也回 success，避免虎皮椒无谓重试；不发货
        if status not in ("OD", "TRADE_SUCCESS", "PAID"):
            return self._text(200, "success")
        with _LOCK:
            o = _load_orders()
            ordr = o.get(order_id)
            if not ordr:
                return self._text(200, "success")
            if ordr.get("status") == "PAID":
                return self._text(200, "success")  # 幂等
            email = ordr["email"]
            plan_code = ordr["plan_code"]
            ordr["status"] = "GRANTING"
            _save_orders(o)
        try:
            _grant(email, plan_code, note=f"xunhupay-auto:{order_id}")
            with _LOCK:
                o = _load_orders()
                o[order_id]["status"] = "PAID"
                o[order_id]["paid_at"] = time.time()
                if params.get("transaction_id"):
                    o[order_id]["trade_no"] = params["transaction_id"]
                _save_orders(o)
            return self._text(200, "success")
        except Exception as e:
            with _LOCK:
                o = _load_orders()
                o[order_id]["status"] = "GRANT_FAILED"
                o[order_id]["err"] = str(e)[:200]
                o[order_id]["paid_at"] = time.time()
                if params.get("transaction_id"):
                    o[order_id]["trade_no"] = params["transaction_id"]
                _save_orders(o)
            # 已收款仅发货失败 —— 对账口径 GRANT_FAILED = 已收款未发货，会告警补发
            return self._text(200, "success")

    def _query(self):
        data = self._body()
        order_id = str(data.get("order_id") or "")
        with _LOCK:
            o = _load_orders()
            ordr = o.get(order_id)
        if not ordr:
            return self._json(404, {"ok": False, "error": "订单不存在", "code": "NO_ORDER"})
        self._json(200, {"ok": True, "order_id": order_id, "status": ordr.get("status"),
                         "plan_code": ordr.get("plan_code"), "amount": ordr.get("amount")})


def main() -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[pay] listening 127.0.0.1:{PORT} data={ORDERS_PATH} "
          f"alipay_cfg={ALIPAY_CFG} secret={'set' if SECRET else 'MISSING'}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
