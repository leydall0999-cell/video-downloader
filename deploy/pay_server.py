#!/usr/bin/env python3
"""VDL 支付服务（支付宝 · 独立进程，与授权中心解耦）。

定位：处理「用户真付款 -> 自动开通对应套餐」的闭环。
  - 支付宝异步回调需公网可达 => 本服务部署在内地 ECS（pay.hanyuxz.top），nginx 反代 /api/pay/。
  - 下单需云端登录 token（证明是谁买的）；回调验签确认真付款后，内部调授权中心的
    /api/license/grant 把档位写到该账号（state 单写者原则：本服务不直接写授权中心 state）。
  - 金额以本服务 PRICE_MAP 为准（服务端真源），前端只传 plan_code，防改价。

端点：
  GET  /healthz
  POST /api/pay/create          {token, plan_code} -> {order_id, qr_png(base64), amount, plan_code}
  POST /api/pay/alipay/notify                 支付宝异步通知，返回 success/failure（纯文本）
  POST /api/pay/query           {order_id}     -> {status: PENDING|PAID|GRANT_FAILED}

依赖：python-alipay-sdk, qrcode, Pillow（香港机 venv 安装）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import string
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs

# ── 配置 ──────────────────────────────────────────────────────────────────── #
PORT = int(os.environ.get("VDL_PAY_PORT") or "8903")
DATA_DIR = Path(os.environ.get("VDL_LICENSE_DATA") or os.path.expanduser("~/.vdl-license"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
ORDERS_PATH = DATA_DIR / "pay_orders.json"
# 与授权中心共享同一 secret，用于解登录 token 取邮箱（user_id）
SECRET = (os.environ.get("VDL_LICENSE_SECRET") or "").strip()
ADMIN_TOKEN = (os.environ.get("VDL_LICENSE_ADMIN_TOKEN") or "").strip()
GRANT_URL = "https://hanyuxz.top/api/license/grant"
ALIPAY_CFG = Path(os.environ.get("VDL_ALIPAY_CFG") or "/opt/vdl-license/alipay.json")
TOKEN_TTL = float(os.environ.get("VDL_LICENSE_TOKEN_TTL_DAYS") or "30") * 86400.0

# 金额真源（与 App membership.DOWNLOAD_PLANS / AI_PLANS / CREDIT_PACKS 对齐，单位元）
PRICE_MAP: dict[str, dict[str, Any]] = {
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
    global _alipay, _alipay_err
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


# ── grant 内部调用（写档位到账号，state 单写者原则）──────────────────────────── #
def _grant(email: str, plan_code: str) -> dict[str, Any]:
    import urllib.request
    body = json.dumps({"email": email, "plan": plan_code, "note": "alipay-auto",
                       "token": ADMIN_TOKEN}).encode("utf-8")
    req = urllib.request.Request(GRANT_URL, data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


# ── HTTP Handler ──────────────────────────────────────────────────────────── #
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a) -> None:  # 静默
        pass

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _text(self, status: int, text: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(text.encode("utf-8"))

    def _body(self) -> dict[str, Any]:
        try:
            ln = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(ln) or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        if self.path.split("?")[0] in ("/healthz", "/api/pay/healthz"):
            self._json(200, {"ok": True, "service": "vdl-pay"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        p = self.path.split("?")[0]
        if p == "/api/pay/create":
            self._create()
        elif p == "/api/pay/alipay/notify":
            self._notify()
        elif p == "/api/pay/query":
            self._query()
        else:
            self._json(404, {"error": "not found"})

    def _create(self):
        data = self._body()
        token = str(data.get("token") or "")
        plan_code = str(data.get("plan_code") or "")
        if plan_code not in PRICE_MAP:
            return self._json(400, {"ok": False, "error": "未知套餐", "code": "BAD_PLAN"})
        email = parse_token(token, SECRET)
        if not email:
            return self._json(401, {"ok": False, "error": "登录态失效，请重新登录",
                                    "code": "BAD_TOKEN"})
        alipay, err = get_alipay()
        if err:
            return self._json(500, {"ok": False, "error": err, "code": "ALIPAY_CFG"})
        price = PRICE_MAP[plan_code]["price"]
        subject = PRICE_MAP[plan_code]["subject"]
        order_id = _gen_order_id()
        try:
            r = alipay.api_alipay_trade_precreate(
                subject, order_id, price, notify_url=alipay.app_notify_url)
        except Exception as e:
            return self._json(500, {"ok": False, "error": f"支付宝下单失败: {e}",
                                    "code": "ALIPAY_ERR"})
        if r.get("code") != "10000" or r.get("msg") != "Success":
            return self._json(400, {"ok": False,
                                    "error": r.get("sub_msg") or r.get("msg") or "下单失败",
                                    "code": "ALIPAY_REJECT"})
        qr = r.get("qr_code", "")
        with _LOCK:
            o = _load_orders()
            o[order_id] = {"email": email, "plan_code": plan_code, "amount": price,
                           "status": "PENDING", "created_at": time.time()}
            _save_orders(o)
        try:
            qr_png = _qr_png(qr)
        except Exception:
            qr_png = ""
        self._json(200, {"ok": True, "order_id": order_id, "qr_png": qr_png,
                         "amount": price, "plan_code": plan_code, "qr": qr})

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
            _grant(email, plan_code)
            with _LOCK:
                o = _load_orders()
                o[order_id]["status"] = "PAID"
                _save_orders(o)
            return self._text(200, "success")
        except Exception as e:
            with _LOCK:
                o = _load_orders()
                o[order_id]["status"] = "GRANT_FAILED"
                o[order_id]["err"] = str(e)[:200]
                _save_orders(o)
            # 仍回 success 避免支付宝无限重试；后台可查 GRANT_FAILED 补单
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
