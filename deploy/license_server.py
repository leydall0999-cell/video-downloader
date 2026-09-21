#!/usr/bin/env python3
"""VDL 卡密授权中心（P2 一机一码 · 零依赖，仅标准库）。

职责（唯一真源，App 端不做验签）：
  1) 生成签名卡密（管理员，token 鉴权）
  2) 卡密验签 + 激活绑定设备指纹（一机一码强制点）
  3) 卡密状态查询（App 启动校验卡密是否被作废）
  4) 作废卡密（管理员）

卡密格式：VDL-<PLAN>-<RAND10HEX>-<SIG8HEX>
  SIG = HMAC-SHA256(secret, "<PLAN>|<RAND10>") 前 8 位 hex
  secret 只存在本机环境变量 VDL_LICENSE_SECRET，**永不下发** ⇒ 卡密不可离线伪造。

绑定语义（一机一码）：
  - unused 卡密 + 指纹 → 绑定该指纹、置 used（原子写）
  - 同卡密 + 同指纹重复提交 → 幂等成功（网络重试不二次扣）
  - 同卡密 + 不同指纹 → 拒绝 ALREADY_BOUND（换机需管理员先解绑）
  - revoked → 拒绝 REVOKED

端点（全部 JSON）：
  GET  /healthz
  POST /api/license/gen     {plan, count, note?, token}
  POST /api/license/redeem  {code, fingerprint}
  POST /api/license/check   {code, fingerprint}
  POST /api/license/revoke  {code, token}

部署：香港机 /opt/vdl-license/，systemd vdl-license 监听 127.0.0.1:8902，
nginx `location /api/license/` 反代。数据 /opt/vdl-license/data/cards.json。
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

# ── 配置 ──────────────────────────────────────────────────────────────────── #
SECRET = (os.environ.get("VDL_LICENSE_SECRET") or "").strip()
ADMIN_TOKEN = (os.environ.get("VDL_LICENSE_ADMIN_TOKEN") or "").strip()
PORT = int(os.environ.get("VDL_LICENSE_PORT") or "8902")
DATA_PATH = Path(os.environ.get("VDL_LICENSE_DATA")
                 or os.path.expanduser("~/.vdl-license/cards.json"))

# 套餐短码 → App 侧 membership 套餐 code（唯一映射，两端共同语义）
PLAN_MAP: dict[str, str] = {
    "DLM":  "download_month",
    "DLH":  "download_half_year",
    "DLY":  "download_year",
    "AIM5": "ai_5500",
    "AM15": "ai_15000",
    "CP5K": "credits_5000",
    "CK15": "credits_15000",
}
# 反查：plan_code → 短码（gen 时用）
PLAN_CODE_TO_SHORT = {v: k for k, v in PLAN_MAP.items()}

CODE_RE = re.compile(r"^VDL-([A-Z0-9]{3,4})-([0-9A-Fa-f]{10})-([0-9A-Fa-f]{8})$")

# 简易限流：每 IP 每分钟最多 60 次 redeem/check（防暴力猜码）
_THROTTLE: dict[str, deque] = {}
_THROTTLE_GUARD = threading.Lock()
_THROTTLE_LIMIT = 60
_THROTTLE_WINDOW = 60.0


class ApiError(Exception):
    def __init__(self, status: int, code: str, msg: str):
        super().__init__(msg)
        self.status, self.code, self.msg = status, code, msg


# ── 存储原子写 ─────────────────────────────────────────────────────────────── #
_LOCK = threading.Lock()


def _load_state() -> dict[str, Any]:
    try:
        return json.loads(DATA_PATH.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_PATH.with_name(DATA_PATH.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, DATA_PATH)


# ── 卡密核心逻辑（与 HTTP 解耦，便于单测）──────────────────────────────────── #
def _sign(plan_short: str, rand: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), f"{plan_short}|{rand}".encode("utf-8"),
                    "sha256").hexdigest()[:8]


def gen_code(plan_code: str, secret: str, rng: Any = None) -> str:
    """按 membership 套餐 code 生成一张卡密。未知套餐抛 ValueError。"""
    short = PLAN_CODE_TO_SHORT.get(plan_code)
    if not short:
        raise ValueError(f"未知套餐: {plan_code}")
    rng = rng or secrets
    rand = rng.token_hex(5)                     # 10 hex
    return f"VDL-{short}-{rand}-{_sign(short, rand, secret)}"


def verify_code(code: str, secret: str) -> str:
    """验签名，返回套餐短码。格式/签名不符抛 ApiError(BAD_CODE)。

    容忍用户手输大小写混杂：短码统一大写、随机段/签名统一小写后再比对。
    """
    m = CODE_RE.match((code or "").strip())
    if not m:
        raise ApiError(400, "BAD_CODE", "卡密格式不正确")
    short, rand, sig = m.groups()
    short, rand, sig = short.upper(), rand.lower(), sig.lower()
    if not hmac.compare_digest(_sign(short, rand, secret), sig):
        raise ApiError(400, "BAD_CODE", "卡密签名校验失败")
    if short not in PLAN_MAP:
        raise ApiError(400, "BAD_CODE", f"未知套餐短码: {short}")
    return short


def redeem_impl(state: dict[str, Any], code: str, fp: str, now: float,
                secret: str) -> dict[str, Any]:
    """卡密激活绑定（纯函数，直接改 state）。返回公开 dict。"""
    if not secret:
        raise ApiError(500, "NO_SECRET", "服务端未配置 VDL_LICENSE_SECRET")
    short = verify_code(code, secret)
    plan_code = PLAN_MAP[short]
    rec = state.setdefault("cards", {}).setdefault(code, {
        "plan": short, "plan_code": plan_code, "status": "unused",
        "bound_fp": "", "bound_at": 0.0, "created_at": now,
    })
    status = rec.get("status")
    if status == "revoked":
        raise ApiError(403, "REVOKED", "卡密已被作废")
    bound_fp = rec.get("bound_fp") or ""
    if status == "used" and bound_fp != fp:
        raise ApiError(403, "ALREADY_BOUND",
                       "卡密已绑定其他设备（换机请联系客服解绑）")
    if status == "used" and bound_fp == fp:
        return {"ok": True, "idempotent": True, "plan_code": plan_code,
                "bound_at": rec.get("bound_at", now)}
    rec.update({"status": "used", "bound_fp": fp, "bound_at": now})
    return {"ok": True, "idempotent": False, "plan_code": plan_code,
            "bound_at": now}


def check_impl(state: dict[str, Any], code: str, fp: str) -> dict[str, Any]:
    """卡密状态查询（App 启动校验是否被作废）。"""
    rec = (state.get("cards") or {}).get((code or "").strip())
    if not rec:
        return {"ok": True, "known": False, "status": "unknown"}
    out = {"ok": True, "known": True, "status": rec.get("status"),
           "plan_code": rec.get("plan_code"),
           "matches": (rec.get("bound_fp") or "") == fp}
    return out


def revoke_impl(state: dict[str, Any], code: str, now: float) -> dict[str, Any]:
    rec = (state.get("cards") or {}).get((code or "").strip())
    if not rec:
        raise ApiError(404, "NOT_FOUND", "卡密不存在")
    rec["status"] = "revoked"
    rec["revoked_at"] = now
    return {"ok": True, "code": code, "status": "revoked"}


# ── 限流 ───────────────────────────────────────────────────────────────────── #
def _throttled(ip: str, now: float) -> bool:
    with _THROTTLE_GUARD:
        dq = _THROTTLE.setdefault(ip, deque())
        while dq and now - dq[0] > _THROTTLE_WINDOW:
            dq.popleft()
        if len(dq) >= _THROTTLE_LIMIT:
            return True
        dq.append(now)
        return False


# ── HTTP 层 ────────────────────────────────────────────────────────────────── #
class Handler(BaseHTTPRequestHandler):
    server_version = "VDLLicense/1.0"

    def log_message(self, fmt, *args):  # noqa: N802
        sys.stderr.write("[license] %s - %s\n" % (self.address_string(), fmt % args))

    # ---- helpers ----
    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 64 * 1024:
            raise ApiError(400, "BAD_BODY", "请求体为空或过大")
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(400, "BAD_BODY", "请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise ApiError(400, "BAD_BODY", "请求体必须是 JSON 对象")
        return data

    def _route(self, method: str) -> None:
        path = self.path.split("?")[0].rstrip("/") or "/"
        try:
            if method == "GET":
                if path in ("/healthz", "/api/license/healthz"):
                    return self._json(200, {"status": "ok", "service": "vdl-license"})
                return self._json(404, {"ok": False, "error": "not found"})
            if not path.startswith("/api/license/"):
                return self._json(404, {"ok": False, "error": "not found"})
            ip = self.client_address[0]
            now = time.time()
            data = self._body()
            action = path.rsplit("/", 1)[-1]

            if action == "gen":
                self._require_admin(data)
                self._require_secret()
                return self._handle_gen(data, now)
            if action == "revoke":
                self._require_admin(data)
                with _LOCK:
                    st = _load_state()
                    out = revoke_impl(st, str(data.get("code") or ""), now)
                    _save_state(st)
                return self._json(200, out)
            if action in ("redeem", "check"):
                if _throttled(ip, now):
                    return self._json(429, {"ok": False, "error": "请求过于频繁，稍后再试"})
                code = str(data.get("code") or "").strip()
                fp = str(data.get("fingerprint") or "").strip().lower()
                if not code or not re.match(r"^[0-9a-f]{32}$", fp):
                    return self._json(400, {"ok": False, "error": "缺少 code 或 fingerprint"})
                with _LOCK:
                    st = _load_state()
                    if action == "redeem":
                        out = redeem_impl(st, code, fp, now, SECRET)
                        _save_state(st)
                    else:
                        out = check_impl(st, code, fp)
                return self._json(200, out)
            return self._json(404, {"ok": False, "error": "unknown action"})
        except ApiError as e:
            return self._json(e.status, {"ok": False, "error": e.msg, "code": e.code})

    def _require_admin(self, data: dict[str, Any]) -> None:
        if not ADMIN_TOKEN:
            raise ApiError(500, "NO_ADMIN_TOKEN", "服务端未配置 VDL_LICENSE_ADMIN_TOKEN")
        tok = str(data.get("token") or "")
        if not hmac.compare_digest(tok, ADMIN_TOKEN):
            raise ApiError(403, "FORBIDDEN", "管理员 token 不正确")

    def _require_secret(self) -> None:
        if not SECRET:
            raise ApiError(500, "NO_SECRET", "服务端未配置 VDL_LICENSE_SECRET")

    def _handle_gen(self, data: dict[str, Any], now: float) -> None:
        plan = str(data.get("plan") or "").strip()
        try:
            count = int(data.get("count") or 1)
        except (TypeError, ValueError):
            count = 0
        if not 1 <= count <= 500:
            raise ApiError(400, "BAD_COUNT", "count 取值 1~500")
        note = str(data.get("note") or "")[:120]
        with _LOCK:
            st = _load_state()
            cards = st.setdefault("cards", {})
            codes: list[str] = []
            for _ in range(count):
                c = gen_code(plan, SECRET)
                cards[c] = {"plan": c.split("-")[1], "plan_code": PLAN_MAP[c.split("-")[1]],
                            "status": "unused", "bound_fp": "", "bound_at": 0.0,
                            "created_at": now, "note": note}
                codes.append(c)
            _save_state(st)
        self._json(200, {"ok": True, "codes": codes, "plan": plan, "count": len(codes)})

    def do_GET(self):  # noqa: N802
        self._route("GET")

    def do_POST(self):  # noqa: N802
        self._route("POST")


def main() -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[license] listening on 127.0.0.1:{PORT}  data={DATA_PATH}"
          f"  secret={'set' if SECRET else 'MISSING'}"
          f"  admin={'set' if ADMIN_TOKEN else 'MISSING'}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
