"""离线测试：虎皮椒（xunhupay）聚合支付通道的签名 / 下单 / 原生码 / 验签。

不连真实网络：mock 掉 urllib.request，覆盖五条真实炸过的链路——
签名口径（原值、不 urlencode）、必填参数（time / nonce_str）、
PC 端原生微信码还原、取不到原生码时的回落、回调验签（正确通过 / 篡改拒绝）。

🔴 本文件 2026-10-09 之前是**错的**：`test_sign_...` 断言的正是「值做 urlencode」
   这一错误实现，且本文件当时**没有登记进 run_offline_tests.sh**（等于从未运行）。
   于是「签名错误 + 缺 time/nonce_str」两个必炸缺陷一路进了线上，直到真实下单才暴露。
   ⇒ 教训：断言必须对照**官方算法原文**写，不能照抄自己的实现；新测试必须登记并 grep 确认被执行。
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "vdl_pay_server", REPO / "deploy" / "pay_server.py")
P = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(P)

APPID = "HB0000test"
SECRET = "SECRETK3Y"
NOTIFY_BASE = "https://pay.example.com"


def _write_cfg(tmp: Path) -> str:
    cfg = tmp / "xunhupay.json"
    cfg.write_text(json.dumps({"appid": APPID, "appsecret": SECRET,
                               "notify_base": NOTIFY_BASE}), encoding="utf-8")
    return str(cfg)


def _reset_cfg(tmp: Path) -> None:
    os.environ["VDL_XUNHU_CFG"] = _write_cfg(tmp)
    P._xunhu_cfg = None
    P._xunhu_err = ""


class _FakeResp:
    def __init__(self, payload: bytes) -> None:
        self._b = payload

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *a) -> None:
        pass

    def read(self) -> bytes:
        return self._b


def _official_hash(params: dict, secret: str) -> str:
    """照抄官方 PHP generate_xh_hash：ksort → `key=value` **原值** → & 连接 → 拼 appsecret → md5。

    官方原文（https://www.xunhupay.com/doc/api/pay.html）：
        ksort($datas);
        foreach ($datas as $key=>$val) {
            if ($key=='hash' || is_null($val) || $val==='') { continue; }
            if ($arg) { $arg .= '&'; }
            $arg .= "$key=$val";
        }
        return md5($arg . $hashkey);
    ⇒ 值**不做 urlencode**；空值 / hash 自身不参与。
    """
    items = [(k, params[k]) for k in sorted(params)
             if k != "hash" and params[k] is not None and str(params[k]) != ""]
    s = "&".join(f"{k}={v}" for k, v in items)
    return hashlib.md5((s + secret).encode("utf-8")).hexdigest()


def test_sign_uses_raw_values_and_skips_empty():
    """签名必须取原值（不得 urlencode），且空值与 hash 不参与。"""
    params = {"version": "1.1", "appid": APPID, "total_fee": "29.80",
              "title": "视频工坊·VIP会员月卡",
              "notify_url": NOTIFY_BASE + "/api/pay/xunhupay/notify",
              "trade_order_id": "VDLP1"}
    got = P._xunhu_sign(params, SECRET)
    assert got == _official_hash(params, SECRET), "签名与官方 PHP 口径不一致"
    assert len(got) == 32 and got.islower()

    # 变异哨兵：若实现回退成「对值做 urlencode」，本断言必须变红。
    qs_enc = "&".join(f"{k}={urllib.parse.quote_plus(str(params[k]))}"
                      for k in sorted(params))
    wrong = hashlib.md5((qs_enc + SECRET).encode("utf-8")).hexdigest()
    assert got != wrong, "urlencode 版签名竟然与官方口径相同 —— 哨兵失效，请检查用例"

    # 空值 / None / hash 都不参与
    a = dict(params)
    b = dict(params, plugins="", attach=None, hash="should-be-ignored")
    assert P._xunhu_sign(a, SECRET) == P._xunhu_sign(b, SECRET)


def test_place_xunhu_sends_required_params(tmp_path):
    """下单必须带官方必填的 time / nonce_str，且不带无意义的 payment。"""
    _reset_cfg(tmp_path)
    sent = {}

    def fake_urlopen(req, *a, **k):
        sent["body"] = urllib.parse.parse_qs(req.data.decode("utf-8"))
        return _FakeResp(json.dumps(
            {"errcode": 0, "url": "https://api.xunhupay.com/payments/wechat/index?id=1",
             "url_qrcode": "https://api.xunhupay.com/payments/wechat/qrcode_v3?id=1"}
        ).encode())

    with mock.patch.object(urllib.request, "urlopen", fake_urlopen):
        res = P._place_xunhu("视频工坊·VIP会员月卡", "VDLP1", "29.80", "desktop")

    p = {k: v[0] for k, v in sent["body"].items()}
    assert p["appid"] == APPID and p["total_fee"] == "29.80"
    assert p["version"] == "1.1"
    assert p["time"].isdigit() and abs(int(p["time"]) - int(__import__("time").time())) < 120, \
        "time 必须是秒级时间戳（缺它网关报「缺少参数appid,time,hash或他们的值不合法」）"
    assert len(p["nonce_str"]) == 32, "nonce_str 是官方必填项"
    assert "payment" not in p, "本渠道由 appid 决定，payment 无意义且污染签名集合"
    assert p["notify_url"] == NOTIFY_BASE + "/api/pay/xunhupay/notify"
    assert p["return_url"] == NOTIFY_BASE + "/api/pay/return"
    # hash 必须是对「实际发出的参数（去掉 hash）」按官方口径重算的结果
    assert p["hash"] == _official_hash({k: v for k, v in p.items() if k != "hash"}, SECRET)

    assert res["mode"] == "xunhupay"
    assert res["pay_url"].startswith("https://api.xunhupay.com/")


def test_place_xunhu_extracts_native_wechat_code(tmp_path):
    """url_qrcode 里的 data(base64) 要还原成 weixin:// 原生码 —— 桌面端扫码直调微信支付。"""
    _reset_cfg(tmp_path)
    native = "weixin://wxpay/bizpayurl?pr=5QQN6zaXK3q9a3G1"
    b64 = base64.urlsafe_b64encode(native.encode()).decode().rstrip("=")
    qrcode_url = "https://api.xunhupay.com/payments/wechat/qrcode_v3?id=1&data=" + b64
    fake = _FakeResp(json.dumps(
        {"errcode": 0, "url": "https://api.xunhupay.com/payments/wechat/index?id=1",
         "url_qrcode": qrcode_url}).encode())

    with mock.patch.object(urllib.request, "urlopen", return_value=fake):
        res = P._place_xunhu("标题", "VDLP1", "1.90", "desktop")

    assert res["qr_code"] == native, "未能把 url_qrcode 还原成 weixin:// 原生码"
    assert res["pay_url"].endswith("/index?id=1")


def test_place_xunhu_parses_native_code_from_302(tmp_path):
    """data 不在 query 里时要跟一次 302，从 Location 的 data 还原（实测就是这个形态）。"""
    _reset_cfg(tmp_path)
    native = "weixin://wxpay/bizpayurl?pr=ABCDEFGH"
    b64 = base64.urlsafe_b64encode(native.encode()).decode().rstrip("=")
    loc = "https://api.xunhupay.com/qrcode/%s.html?data=%s&nonce_str=1&hash=2" % (APPID, b64)

    class _Opener:
        def open(self, *a, **k):
            raise urllib.error.HTTPError(loc, 302, "Found", {"Location": loc}, None)

    with mock.patch.object(urllib.request, "build_opener", lambda *a, **k: _Opener()):
        assert P._xunhu_native_code(
            "https://api.xunhupay.com/payments/wechat/qrcode_v3?id=1") == native


def test_place_xunhu_falls_back_without_native_code(tmp_path):
    """拿不到原生码时不得报错，也不得返回空二维码（回落到收银台 URL）。"""
    _reset_cfg(tmp_path)
    fake = _FakeResp(json.dumps(
        {"errcode": 0, "url": "https://api.xunhupay.com/payments/wechat/index?id=9"}
    ).encode())
    with mock.patch.object(urllib.request, "urlopen", return_value=fake):
        res = P._place_xunhu("标题", "VDLP2", "1.90", "mobile")
    assert res["qr_code"] == ""
    assert res["pay_url"].endswith("index?id=9")


def test_place_xunhu_rejects_business_error(tmp_path):
    _reset_cfg(tmp_path)
    fake = _FakeResp(json.dumps({"errcode": 4001, "errmsg": "应用不存在"}).encode())
    with mock.patch.object(urllib.request, "urlopen", return_value=fake):
        try:
            P._place_xunhu("t", "VDLP3", "1.90", "desktop")
            raise AssertionError("应当抛错")
        except RuntimeError as e:
            assert "虎皮椒下单失败" in str(e)


def test_place_xunhu_rejects_empty_result(tmp_path):
    """网关 errcode=0 但两个地址都没给 ⇒ 抛错，绝不静默返回空二维码。"""
    _reset_cfg(tmp_path)
    fake = _FakeResp(json.dumps({"errcode": 0}).encode())
    with mock.patch.object(urllib.request, "urlopen", return_value=fake):
        try:
            P._place_xunhu("t", "VDLP4", "1.90", "desktop")
            raise AssertionError("应当抛错")
        except RuntimeError as e:
            assert "未返回收银台/二维码地址" in str(e)


def test_notify_signature_accepts_valid_and_rejects_tampered():
    params = {"appid": APPID, "trade_order_id": "VDLP9", "transaction_id": "T2026",
              "total_fee": "29.80", "status": "WP",
              "time": "1791479667", "nonce_str": "a" * 16}
    good = dict(params, hash=_official_hash(params, SECRET))
    recv = {k: v for k, v in good.items() if k != "hash"}
    assert P._xunhu_sign(recv, SECRET) == good["hash"]

    tampered = dict(good, total_fee="0.01")          # 改金额
    assert P._xunhu_sign({k: v for k, v in tampered.items() if k != "hash"},
                         SECRET) != tampered["hash"]
    assert P._xunhu_sign(recv, SECRET) != "deadbeef" * 4   # 伪造 hash


def test_get_xunhu_missing_cfg_reports_error(tmp_path):
    os.environ["VDL_XUNHU_CFG"] = str(tmp_path / "nope.json")
    P._xunhu_cfg = None
    P._xunhu_err = ""
    r = P.get_xunhu()
    assert isinstance(r, str) and "缺失" in r


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="vdl_xunhu_"))


if __name__ == "__main__":
    test_sign_uses_raw_values_and_skips_empty()
    test_place_xunhu_sends_required_params(_tmp())
    test_place_xunhu_extracts_native_wechat_code(_tmp())
    test_place_xunhu_parses_native_code_from_302(_tmp())
    test_place_xunhu_falls_back_without_native_code(_tmp())
    test_place_xunhu_rejects_business_error(_tmp())
    test_place_xunhu_rejects_empty_result(_tmp())
    test_notify_signature_accepts_valid_and_rejects_tampered()
    test_get_xunhu_missing_cfg_reports_error(_tmp())
    print("ALL XUNHU TESTS PASSED")
