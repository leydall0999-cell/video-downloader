"""对象存储直链（方案③）回归测试（2026-10-20）。

覆盖三层：
  1. 签名算法 —— OSS V1 用**本文件内独立实现**的验签器交叉核对（不复用被测代码），
     S3 SigV4 用 AWS 官方示例的固定期望值钉死（签名错一个字节就 403，必须钉住）。
  2. 配置解析 —— env 覆盖文件、缺字段返回 None（→ 前端静默回退单流下载）。
  3. 端到端 —— 起一个本地假 OSS 服务器（校验 Authorization / 预签名 query），
     真跑一遍「上传 → 生成预签名直链 → 浏览器取回字节」，并断言只上传一次。

为什么值得钉这么死：这条链路的失败模式是**静默 403**（签名错）和**重复上传**
（每点一次保存就重传一次 418MB）。两者在真机上极难从日志看出来。
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_AK = "test-ak"
_SK = "test-sk"
_BUCKET = "b"


def _b64sha1(secret: str, msg: str) -> str:
    return base64.b64encode(
        hmac.new(secret.encode(), msg.encode(), hashlib.sha1).digest()).decode()


class _FakeOSS:
    """最小 OSS 服务：PUT 校验 Authorization，GET 校验预签名，行为足以证伪签名实现。"""

    def __init__(self):
        self.store = {}
        self.puts = 0
        self.gets = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # 静音
                pass

            def _fail(self, code, msg):
                body = msg.encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_PUT(self):
                path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
                if not path.startswith(f"/{_BUCKET}/"):
                    return self._fail(404, "no bucket")
                key = path[len(_BUCKET) + 2:]
                auth = self.headers.get("Authorization") or ""
                date = self.headers.get("Date") or ""
                ctype = self.headers.get("Content-Type") or ""
                expect = _b64sha1(_SK, f"PUT\n\n{ctype}\n{date}\n/{_BUCKET}/{key}")
                if auth != f"OSS {_AK}:{expect}":
                    return self._fail(403, f"bad signature: {auth}")
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                outer.store[key] = (body, ctype, self.headers.get("Content-Disposition") or "")
                outer.puts += 1
                self.send_response(200)
                self.send_header("ETag", '"' + hashlib.md5(body).hexdigest() + '"')
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                parts = urllib.parse.urlsplit(self.path)
                path = urllib.parse.unquote(parts.path)
                key = path[len(_BUCKET) + 2:] if path.startswith(f"/{_BUCKET}/") else ""
                q = urllib.parse.parse_qs(parts.query)
                if key not in outer.store:
                    return self._fail(404, "no such key")
                try:
                    expires = int((q.get("Expires") or ["0"])[0])
                except ValueError:
                    return self._fail(403, "bad expires")
                if expires < int(time.time()):
                    return self._fail(403, "expired")
                expect = _b64sha1(_SK, f"GET\n\n\n{expires}\n/{_BUCKET}/{key}")
                if (q.get("Signature") or [""])[0] != expect:
                    return self._fail(403, "bad presign signature")
                if (q.get("OSSAccessKeyId") or [""])[0] != _AK:
                    return self._fail(403, "bad ak")
                outer.gets += 1
                body, ctype, disp = outer.store[key]
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Disposition", disp)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def _write_cfg(home: Path, **kw) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "object_store.json").write_text(json.dumps(kw), encoding="utf-8")


def test_s3_presign_matches_aws_official_vector():
    """AWS 文档示例：examplebucket/test.txt，20130524T000000Z，签名固定值。"""
    import object_store as obj

    cfg = {
        "bucket": "examplebucket", "access_key_id": "AKIAIOSFODNN7EXAMPLE",
        "access_key_secret": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "scheme": "https", "endpoint": "s3.amazonaws.com", "region": "us-east-1",
        "path_style": False,
    }
    orig = obj._amz_date
    obj._amz_date = lambda ts=None: ("20130524T000000Z", "20130524")
    try:
        url = obj.s3_presign_get(cfg, "test.txt", 86400)
    finally:
        obj._amz_date = orig
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert q["X-Amz-Signature"][0] == (
        "aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"), url
    assert q["X-Amz-SignedHeaders"][0] == "host"
    assert q["X-Amz-Credential"][0].endswith("/20130524/us-east-1/s3/aws4_request")


def test_oss_presign_verifies_against_independent_checker():
    import object_store as obj

    cfg = {"bucket": "oss-example", "access_key_id": "ak1", "access_key_secret": "sk1",
           "scheme": "https", "endpoint": "oss-cn-shenzhen.aliyuncs.com"}
    url = obj.oss_presign_get(cfg, "vdl/tasks/t1/a.mp4", 1141889120)
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    expect = _b64sha1("sk1", "GET\n\n\n1141889120\n/oss-example/vdl/tasks/t1/a.mp4")
    assert q["Signature"][0] == expect
    assert q["OSSAccessKeyId"][0] == "ak1"
    assert "oss-example.oss-cn-shenzhen.aliyuncs.com" in url


def test_oss_put_headers_signature_is_verifiable():
    import object_store as obj

    cfg = {"bucket": "bk", "access_key_id": "ak", "access_key_secret": "sk",
           "scheme": "https", "endpoint": "e.example.com"}
    h = obj.oss_put_headers(cfg, "k/1.mp4", "video/mp4", 'attachment; filename="a.mp4"',
                            10, date="Thu, 17 Nov 2005 18:49:58 GMT")
    expect = _b64sha1("sk", "PUT\n\nvideo/mp4\nThu, 17 Nov 2005 18:49:58 GMT\n/bk/k/1.mp4")
    assert h["Authorization"] == f"OSS ak:{expect}"


def test_object_key_is_ascii_and_task_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("VDL_HOME", str(tmp_path))
    import object_store as obj

    obj.reset_cache()
    k1 = obj.object_key("abc123", "普通玩家从0打倒一个亿 #和平精英.mp4")
    k2 = obj.object_key("abc123", "完全不同的标题.mkv")
    assert k1.startswith("vdl/tasks/abc123/")
    assert k1.endswith(".mp4") and k2.endswith(".mkv")
    assert k1.isascii() and k2.isascii(), (k1, k2)
    assert k1 != k2
    # 任务 id 里的奇怪字符被剔掉，路径不会被注入
    assert "../" not in obj.object_key("../../etc", "x.mp4")


def test_config_env_overrides_file_and_incomplete_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("VDL_HOME", str(tmp_path))
    import object_store as obj

    obj.reset_cache()
    assert obj.load_config() is None and not obj.enabled()

    _write_cfg(tmp_path, provider="aliyun_oss", endpoint="oss-cn-shenzhen.aliyuncs.com",
               bucket="bkt", access_key_id="ak", access_key_secret="sk")
    obj.reset_cache()
    cfg = obj.load_config()
    assert cfg and cfg["bucket"] == "bkt" and cfg["prefix"] == "vdl/tasks"

    monkeypatch.setenv("VDL_OSS_BUCKET", "from-env")
    monkeypatch.setenv("VDL_OSS_EXPIRES", "120")
    obj.reset_cache()
    cfg = obj.load_config()
    assert cfg["bucket"] == "from-env" and cfg["expires"] == 120

    # 缺 endpoint → 视为未配置（宁可回退，也不要在运行时才炸）
    _write_cfg(tmp_path, bucket="bkt", access_key_id="ak", access_key_secret="sk")
    monkeypatch.delenv("VDL_OSS_BUCKET", raising=False)
    monkeypatch.delenv("VDL_OSS_EXPIRES", raising=False)
    obj.reset_cache()
    assert obj.load_config() is None


def test_disabled_config_returns_disabled_status(tmp_path, monkeypatch):
    monkeypatch.setenv("VDL_HOME", str(tmp_path))
    import object_store as obj

    obj.reset_cache()
    fp = tmp_path / "a.mp4"
    fp.write_bytes(b"x" * 16)
    assert obj.ensure_uploaded("t1", fp, "a.mp4")["status"] == "disabled"


def test_end_to_end_upload_then_presigned_download(tmp_path, monkeypatch):
    """真跑一遍：上传（含分块流式）→ 预签名直链 → 取回字节一致；且只传一次。"""
    srv = _FakeOSS()
    try:
        monkeypatch.setenv("VDL_HOME", str(tmp_path))
        _write_cfg(tmp_path, provider="aliyun_oss", endpoint=f"127.0.0.1:{srv.port}",
                   bucket=_BUCKET, access_key_id=_AK, access_key_secret=_SK,
                   path_style=True, scheme="http")
        import object_store as obj

        obj.reset_cache()
        obj.forget("task-e2e")

        payload = os.urandom(3 * 1024 * 1024 + 123)   # 跨多个 1MB 分块
        src = tmp_path / "成品.mp4"
        src.write_bytes(payload)

        st = obj.ensure_uploaded("task-e2e", src, "成品.mp4")
        assert st["status"] == "uploading", st

        deadline = time.time() + 30
        while time.time() < deadline:
            st = obj.ensure_uploaded("task-e2e", src, "成品.mp4")
            if st["status"] != "uploading":
                break
            time.sleep(0.2)
        assert st["status"] == "ready", st
        assert st["size"] == len(payload)

        import requests

        r = requests.get(st["url"], timeout=20)
        assert r.status_code == 200, (r.status_code, r.text[:200])
        assert r.content == payload, "直链取回的字节与源文件不一致"
        assert "filename*=UTF-8''" in (r.headers.get("Content-Disposition") or "")
        assert srv.puts == 1, f"应只上传一次，实际 {srv.puts} 次"

        # 再问一次：命中登记表，不再上传
        st2 = obj.ensure_uploaded("task-e2e", src, "成品.mp4")
        assert st2["status"] == "ready"
        assert srv.puts == 1, "已上传过的文件被重复上传"

        # 文件被替换（体积变化）→ 必须重传，不能给旧 url
        src.write_bytes(payload + b"tail")
        st3 = obj.ensure_uploaded("task-e2e", src, "成品.mp4")
        assert st3["status"] == "uploading", st3
        deadline = time.time() + 30
        while time.time() < deadline:
            st3 = obj.ensure_uploaded("task-e2e", src, "成品.mp4")
            if st3["status"] != "uploading":
                break
            time.sleep(0.2)
        assert st3["status"] == "ready", st3
        assert srv.puts == 2, f"体积变了应重传，实际上传 {srv.puts} 次"
    finally:
        srv.close()


def test_failed_upload_reports_failed_and_does_not_cache(tmp_path, monkeypatch):
    """服务器拒签（403）→ status=failed，且不写登记表（下一次点击还能重试）。"""
    srv = _FakeOSS()
    try:
        monkeypatch.setenv("VDL_HOME", str(tmp_path))
        _write_cfg(tmp_path, provider="aliyun_oss", endpoint=f"127.0.0.1:{srv.port}",
                   bucket=_BUCKET, access_key_id=_AK, access_key_secret="wrong-secret",
                   path_style=True, scheme="http")
        import object_store as obj

        obj.reset_cache()
        src = tmp_path / "b.mp4"
        src.write_bytes(b"y" * 1024)

        assert obj.ensure_uploaded("task-bad", src, "b.mp4")["status"] == "uploading"
        deadline = time.time() + 20
        st = {}
        while time.time() < deadline:
            st = obj.ensure_uploaded("task-bad", src, "b.mp4")
            if st["status"] != "uploading":
                break
            time.sleep(0.2)
        assert st["status"] == "failed", st
        assert obj.uploaded_entry("task-bad") is None
    finally:
        srv.close()
