"""离线集成测试：用本地 Mock 模拟百度 passport 二维码接口，完整跑通扫码登录链路。

解决的问题：沙盒无法访问 passport.baidu.com/channel/unicast（长轮询被代理超时），
导致「扫码确认后提取 BDUSS」这一步只能靠用户真机验证。本测试在本地起一个
严格遵循已抓到的真实响应格式的「假百度」，把 gen -> poll -> 提取 -> login 全链路
离线验证，之后改代码可在沙盒内自测，不再需要用户当测试小白鼠。

运行：
    cd server && python tests/test_baidu_qr_offline.py
（或作为 pytest 用例：pytest server/tests/test_baidu_qr_offline.py）
"""
import http.server
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler

# 把 server/ 目录加入 sys.path，使测试无论从哪里运行都能 import baidu_qr
_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import baidu_qr  # noqa: E402
import baidu_pcs  # noqa: E402

# 一个合法的最小 1x1 PNG（base64 解码），Mock 的二维码图片直接返回它
import base64 as _b64
_PNG = _b64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


class _MockHandler(BaseHTTPRequestHandler):
    # 由 server 实例上的属性控制行为
    def log_message(self, *a):  # 静默
        pass

    def _send(self, code, body, headers=None, cookie=None):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else body.encode("utf-8"))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query)
        server = self.server

        # 1) 生成二维码
        if path == "/v2/api/getqrcode":
            body = json.dumps({
                "errno": 0,
                "sign": server.qr_sign,
                "imgurl": f"http://127.0.0.1:{server.qr_port}/qr.png",
                "prompt": "",
                "expires_in": 120,
            })
            self._send(200, body)
            return

        # 2) 二维码图片
        if path == "/qr.png":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(_PNG)))
            self.end_headers()
            self.wfile.write(_PNG)
            return

        # 3) 长轮询 unicast：根据调用次数推进状态
        if path == "/channel/unicast":
            cb = (q.get("callback") or ["bd__cbs__00000000"])[0]
            server.unicast_calls += 1
            n = server.unicast_calls
            if n <= 2:
                status = "0"          # 等待扫码
            elif n == 3:
                status = "1"          # 已扫码，待确认
            else:
                status = "2"          # 已确认
            data = {"errno": 0, "data": {"status": status}}
            # 已确认 + cookie 模式：通过 Set-Cookie 下发 BDUSS（模拟真实 passport 行为）
            cookie = None
            if status == "2" and server.qr_mode == "cookie":
                cookie = "BDUSS=MOCKBDUSSVALUE1234567890abcdef; Path=/; HttpOnly"
            body = f"{cb}({json.dumps(data)});"
            self._send(200, body, cookie=cookie)
            return

        # 4) qrcodestatus：body 模式兜底，返回含 bduss 字段的 JSON
        if path == "/v2/api/qrcodestatus":
            cb = (q.get("callback") or ["bd__cbs__00000000"])[0]
            body = f'{cb}({{"errno":0,"data":{{"bduss":"MOCKBDUSSFROMBODY"}}}});'
            self._send(200, body)
            return

        self._send(404, json.dumps({"errno": 404}))


class _MockServer:
    def __init__(self, mode="cookie"):
        self.mode = mode
        self.sign = "MOCKSIGN_" + str(int(time.time()))
        self.port = 0
        self.unicast_calls = 0
        self._httpd = None
        self._thread = None

    def __enter__(self):
        self._httpd = http.server.HTTPServer(("127.0.0.1", 0), _MockHandler)
        self._httpd.qr_sign = self.sign
        self._httpd.qr_port = self._httpd.server_address[1]
        self._httpd.qr_mode = self.mode
        self._httpd.unicast_calls = 0
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *a):
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception:
            pass


# ---- 测试逻辑 ----
def _run_flow(mode):
    """跑一遍完整流程，返回 (gen_result, final_poll_result, captured_cookie)。"""
    with _MockServer(mode=mode) as srv:
        base = f"http://127.0.0.1:{srv.port}"
        # 把 baidu_qr 的常量指向本地 Mock
        saved = (baidu_qr.GET_QR, baidu_qr.UNICAST, baidu_qr.QR_STATUS, baidu_qr.PCS_LOGIN)
        baidu_qr.GET_QR = base + "/v2/api/getqrcode"
        baidu_qr.UNICAST = base + "/channel/unicast"
        baidu_qr.QR_STATUS = base + "/v2/api/qrcodestatus"

        captured = {}

        class FakePCS:
            def login(self, cookie_str):
                captured["cookie"] = cookie_str
                return {"ok": True, "message": "登录成功(测试)"}

        baidu_qr.PCS_LOGIN = FakePCS()

        # 重置模块状态
        baidu_qr._STATE.update({"sign": None, "session": None, "gid": None,
                                 "confirmed": False, "login_result": None})

        gen = baidu_qr.qr_gen()
        assert gen.get("ok"), f"qr_gen 失败: {gen}"
        assert gen["sign"] == srv.sign, "sign 不匹配"
        assert gen["img"].startswith("data:image/png;base64,"), "img 不是 base64 data url"

        # 轮询到 confirmed
        result = None
        for _ in range(10):
            result = baidu_qr.qr_poll(srv.sign)
            if result["status"] in ("confirmed", "expired", "error"):
                break
            time.sleep(0.05)
        # 恢复
        baidu_qr.GET_QR, baidu_qr.UNICAST, baidu_qr.QR_STATUS, baidu_qr.PCS_LOGIN = saved
        return gen, result, captured


def test_qr_flow_cookie_mode():
    """场景 A：BDUSS 通过 unicast 的 Set-Cookie 下发（最可能的真实行为）。"""
    gen, result, captured = _run_flow("cookie")
    assert result["status"] == "confirmed", f"未确认: {result}"
    assert result["login"]["ok"], f"login 失败: {result['login']}"
    assert "BDUSS=MOCKBDUSSVALUE1234567890abcdef" in captured.get("cookie", ""), \
        f"未提取到 BDUSS(cookie模式): {captured}"
    print("✅ 场景A(Set-Cookie下发BDUSS): 二维码生成→轮询→提取BDUSS→login 全链路通过")


def test_qr_flow_body_mode():
    """场景 B：unicast 不下发 cookie，兜底走 qrcodestatus 从响应体提取 bduss。"""
    with _MockServer(mode="body") as srv:
        base = f"http://127.0.0.1:{srv.port}"
        saved = (baidu_qr.GET_QR, baidu_qr.UNICAST, baidu_qr.QR_STATUS, baidu_qr.PCS_LOGIN)
        baidu_qr.GET_QR = base + "/v2/api/getqrcode"
        baidu_qr.UNICAST = base + "/channel/unicast"
        baidu_qr.QR_STATUS = base + "/v2/api/qrcodestatus"
        captured = {}

        class FakePCS:
            def login(self, cookie_str):
                captured["cookie"] = cookie_str
                return {"ok": True, "message": "登录成功(测试)"}

        baidu_qr.PCS_LOGIN = FakePCS()
        baidu_qr._STATE.update({"sign": None, "session": None, "gid": None,
                                 "confirmed": False, "login_result": None})

        gen = baidu_qr.qr_gen()
        assert gen.get("ok")
        # 强制让 unicast 确认：直接调 _finish_login 走兜底（模拟 cookie 缺失）
        # 先制造 confirmed 状态：多轮轮询到 status=2
        result = None
        for _ in range(10):
            result = baidu_qr.qr_poll(srv.sign)
            if result["status"] in ("confirmed", "expired", "error"):
                break
            time.sleep(0.05)
        baidu_qr.GET_QR, baidu_qr.UNICAST, baidu_qr.QR_STATUS, baidu_qr.PCS_LOGIN = saved
        # 在 body 模式下 unicast 不发 cookie，_finish_login 会走 QR_STATUS 兜底
        assert result["status"] == "confirmed", f"未确认: {result}"
        assert "bduss" in (captured.get("cookie", "").lower()) or "BDUSS" in captured.get("cookie", ""), \
            f"body 兜底未提取到 bduss: {captured}"
        print("✅ 场景B(响应体兜底提取bduss): 兜底链路通过")


def test_qr_poll_expired():
    """场景 C：用一个不存在的 sign 轮询，应返回 expired 而非崩溃。"""
    r = baidu_qr.qr_poll("NON_EXISTENT_SIGN")
    assert r["status"] == "expired", f"异常 sign 应返回 expired: {r}"
    print("✅ 场景C(失效sign): 安全返回 expired")


def test_qr_poll_long_poll_timeout_is_waiting():
    """场景 F：unicast 长轮询 ReadTimeout（服务端挂起连接直到超时）应判定为 waiting，
    而非上报 error。这是修复「扫码后卡在等待扫码 / 轮询出错」的核心回归测试。"""
    import requests

    with _MockServer(mode="cookie") as srv:
        base = f"http://127.0.0.1:{srv.port}"
        saved = (baidu_qr.GET_QR, baidu_qr.UNICAST, baidu_qr.QR_STATUS, baidu_qr.PCS_LOGIN)
        baidu_qr.GET_QR = base + "/v2/api/getqrcode"
        baidu_qr.UNICAST = base + "/channel/unicast"
        baidu_qr.QR_STATUS = base + "/v2/api/qrcodestatus"
        captured = {}

        class FakePCS:
            def login(self, cookie_str):
                captured["cookie"] = cookie_str
                return {"ok": True, "message": "登录成功(测试)"}

        baidu_qr.PCS_LOGIN = FakePCS()
        baidu_qr._STATE.update({"sign": None, "session": None, "gid": None,
                                 "confirmed": False, "login_result": None})

        gen = baidu_qr.qr_gen()
        assert gen.get("ok")

        # 制造长轮询超时：让 unicast 请求抛 requests ReadTimeout
        real_session = baidu_qr._STATE.get("session")
        orig_get = real_session.get

        def _raise_timeout(*a, **k):
            raise requests.exceptions.ReadTimeout("Read timed out. (read timeout=60)")

        real_session.get = _raise_timeout
        try:
            r = baidu_qr.qr_poll(srv.sign)
        finally:
            real_session.get = orig_get

        assert r["status"] == "waiting", f"长轮询超时应为 waiting: {r}"
        assert "等待" in r.get("message", ""), f"提示语应表达等待: {r}"
        print("✅ 场景F(长轮询超时→waiting): 不再误报 error，前端可继续轮询")


def test_extract_uid():
    """场景 D：_extract_uid 从 who 输出正确提取 uid。"""
    # 正常输出
    assert baidu_pcs._extract_uid("当前账号: uid: 12345, 用户名: test") == 12345
    # uid:0（无效登录）
    assert baidu_pcs._extract_uid("当前账号: uid: 0, 用户名: , 性别: , 年龄: 0.0") == 0
    # 无 uid 信息
    assert baidu_pcs._extract_uid("未登录") == 0
    # 不同格式
    assert baidu_pcs._extract_uid("uid:99999 something") == 99999
    print("✅ 场景D(_extract_uid): uid 提取逻辑正确")


def test_login_rejects_uid_zero():
    """场景 E：login 成功但 who 返回 uid=0 → 应返回 ok=False（假登录拦截）。"""
    from unittest.mock import patch

    # 模拟：_run 返回退出码 0 + 无失败关键词（看起来成功）
    fake_run_ok = {"ok": True, "code": 0, "stdout": "登录成功", "stderr": "", "combined": "登录成功"}
    # 但 who 返回 uid:0
    fake_who_uid0 = {"ok": True, "logged_in": True, "message": "uid: 0, 用户名: ", "raw": "uid: 0"}

    with patch.object(baidu_pcs, "_run", return_value=fake_run_ok), \
         patch.object(baidu_pcs, "who", return_value=fake_who_uid0):
        result = baidu_pcs.login("BDUSS=fake_invalid_bduss")
        assert result["ok"] is False, f"uid=0 应被拦截为登录失败: {result}"
        assert "uid=0" in result["message"] or "无效" in result["message"], \
            f"错误信息应提示凭证无效: {result['message']}"

    # 正常情况：who 返回有效 uid → ok=True
    fake_who_ok = {"ok": True, "logged_in": True, "message": "uid: 12345, 用户名: testuser", "raw": "uid: 12345"}
    with patch.object(baidu_pcs, "_run", return_value=fake_run_ok), \
         patch.object(baidu_pcs, "who", return_value=fake_who_ok):
        result = baidu_pcs.login("BDUSS=valid_bduss")
        assert result["ok"] is True, f"有效 uid 应通过: {result}"
    print("✅ 场景E(uid=0假登录拦截): login→who 验证逻辑正确，uid:0 被拦截，正常 uid 通过")


if __name__ == "__main__":
    test_qr_flow_cookie_mode()
    test_qr_flow_body_mode()
    test_qr_poll_expired()
    test_qr_poll_long_poll_timeout_is_waiting()
    test_extract_uid()
    test_login_rejects_uid_zero()
    print("\n🎉 全部离线集成测试通过 — 扫码登录链路 + uid 验证 + 长轮询超时已在沙盒内自测，不再依赖用户真机。")
