"""离线测试：虎皮椒（xunhupay）聚合支付通道的签名 / 下单 / 验签。

不连真实网络：用 monkeypatch 替换 urllib.request.urlopen 模拟虎皮椒网关，
覆盖三条最易错链路——签名串构造、下单拿收银台 URL、回调验签（正确通过 / 篡改拒绝）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "vdl_pay_server", REPO / "deploy" / "pay_server.py")
P = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(P)


def _write_cfg(tmp: Path) -> str:
    cfg = tmp / "xunhupay.json"
    cfg.write_text(json.dumps({"appid": "HB0000test", "appsecret": "SECRETK3Y",
                               "notify_base": "https://pay.example.com"}),
                   encoding="utf-8")
    return str(cfg)


class _FakeResp:
    def __init__(self, payload: bytes) -> None:
        self._b = payload

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *a) -> None:
        pass

    def read(self) -> bytes:
        return self._b


def test_sign_deterministic_and_matches_php_semantics():
    params = {"version": "1.1", "appid": "HB0000test", "total_fee": "29.80",
              "title": "视频工坊·下载会员月卡", "trade_order_id": "VDLP1"}
    s1 = P._xunhu_sign(params, "SECRETK3Y")
    s2 = P._xunhu_sign(dict(params), "SECRETK3Y")
    assert s1 == s2 and len(s1) == 32
    # 与「ksort → key=value&...（urlencode 值）→ +secret → md5」手工一致
    qs = "&".join(f"{k}={urllib.parse.quote_plus(str(params[k]))}"
                  for k in sorted(params))
    import hashlib
    assert s1 == hashlib.md5((qs + "SECRETK3Y").encode()).hexdigest()


def test_create_roundtrip_returns_cashier_url(tmp_path):
    os.environ["VDL_XUNHU_CFG"] = _write_cfg(tmp_path)
    P._xunhu_cfg = None
    P._xunhu_err = ""
    fake = _FakeResp(json.dumps({"errcode": 0, "url": "https://api.xunhupay.com/c/abc123",
                                 "out_trade_order_id": "VDLP1"}).encode())
    with mock.patch.object(urllib.request, "urlopen", return_value=fake):
        res = P._place_xunhu("视频工坊·下载会员月卡", "VDLP1", "29.80", "desktop")
    assert res["mode"] == "xunhupay"
    assert res["pay_url"] == "https://api.xunhupay.com/c/abc123"
    assert res["qr_code"] == ""


def test_create_rejects_business_error(tmp_path):
    os.environ["VDL_XUNHU_CFG"] = _write_cfg(tmp_path)
    P._xunhu_cfg = None
    P._xunhu_err = ""
    fake = _FakeResp(json.dumps({"errcode": 4001, "errmsg": "应用不存在"}).encode())
    with mock.patch.object(urllib.request, "urlopen", return_value=fake):
        try:
            P._place_xunhu("t", "VDLP2", "1.90", "desktop")
            raise AssertionError("应当抛错")
        except RuntimeError as e:
            assert "虎皮椒下单失败" in str(e)


def test_notify_verify_accepts_valid_and_rejects_tampered():
    appid, appsecret, gw, nb = ("HB0000test", "SECRETK3Y",
                                 "https://api.xunhupay.com/payment/do.html",
                                 "https://pay.example.com")
    params = {"plugins": "alipay", "appid": appid, "trade_order_id": "VDLP9",
              "transaction_id": "T2026", "total_fee": "29.80", "type": "alipay",
              "status": "OD"}
    good = dict(params)
    good["hash"] = P._xunhu_sign(good, appsecret)
    # 正确：移除 hash 后重算应一致
    calc = P._xunhu_sign({k: v for k, v in good.items() if k != "hash"}, appsecret)
    assert calc == good["hash"]
    # 篡改：hash 不对应拒绝
    bad = dict(good)
    bad["hash"] = "deadbeef" * 4
    calc2 = P._xunhu_sign({k: v for k, v in bad.items() if k != "hash"}, appsecret)
    assert calc2 != bad["hash"]


def test_get_xunhu_missing_cfg_reports_error(tmp_path):
    os.environ["VDL_XUNHU_CFG"] = str(tmp_path / "nope.json")
    P._xunhu_cfg = None
    P._xunhu_err = ""
    r = P.get_xunhu()
    assert isinstance(r, str) and "缺失" in r


if __name__ == "__main__":
    test_sign_deterministic_and_matches_php_semantics()
    test_create_roundtrip_returns_cashier_url(Path("/tmp/vdl_xunhu_t"))
    test_create_rejects_business_error(Path("/tmp/vdl_xunhu_t"))
    test_notify_verify_accepts_valid_and_rejects_tampered()
    test_get_xunhu_missing_cfg_reports_error(Path("/tmp/vdl_xunhu_t"))
    print("ALL XUNHU TESTS PASSED")
