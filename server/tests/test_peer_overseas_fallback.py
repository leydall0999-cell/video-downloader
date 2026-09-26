"""海外请求兜底转发（`_peer_overseas_fallback`）回归测试。

背景（2026-09-27 用户报障）
---------------------------
网页版解析 YouTube 报 `[Errno 104] Connection reset by peer`。定位铁证：国内 ECS
nginx 日志里该请求 400/**216 字节**，与本机复现的国内节点错误体逐字节相同 ⇒ 请求
确实打在**国内节点**。而国内节点到不了 YouTube（TLS SNI 被重置），于是给出这条用户
既看不懂、也无法处置的原生异常。

根因：`web/app.js` 的 `node` 只在页面加载时取一次 `/api/nodes`。用户的标签页从
09-26 14:41 一直开着，而服务器侧 `VDL_PEER_ENDPOINT` 是当晚 22:55 才生效 ⇒ 该页面
`node.peer` 恒为空 ⇒ 永远按「本机直连」把海外站请求发给国内节点。

前端修复（web-dev 30ef052：对端落盘 localStorage / 定期刷新 / 失败自愈）只能救
**重新加载过**的页面；已经开着的旧标签页没有任何前端手段能救。故在**节点侧**加兜底：
判定为海外站就把请求原样转给对端，旧页面无需知道对端存在也能正常工作。

本测试钉住：
  1. 海外判定 `_has_overseas_url`（单链接 / 批量 / 混合 / 国内站 / 坏 JSON 全部核对）；
  2. 闸门：**只有**「国内节点 + 配了对端」才转发（单节点部署、海外节点零影响）；
  3. 国内站请求不得被转发（否则国内站会被丢到海外节点绕一圈）；
  4. `/api/tasks/*` 先走本节点，仅「本节点没有该任务（404）」才转对端；
  5. 大文件 / SSE 只回 307（让浏览器直连对端，视频不经国内节点中转），且 307 保留
     方法与查询串。

全程离线：用假 `call_next` 与假 `_peer_forward` 直接驱动中间件，不打任何网络。
运行：cd server && python tests/test_peer_overseas_fallback.py
"""
import asyncio
import json
import os
import sys

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

# 隔离数据目录：app 导入会建 .auth_secret / stats.json，绝不能落到真实用户目录
os.environ.setdefault("VDL_DATA_DIR", "/tmp/vdl_test_peer_fwd")
os.environ.setdefault("VDL_CLOUD_LINK", "0")

from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse, Response  # noqa: E402

import app as A  # noqa: E402

_YT = "https://youtu.be/YK9a6TC9j54?si=d7stctspomFBd8Oj"
_BILI = "https://www.bilibili.com/video/BV1GJ411x7h7"


def _body(obj) -> bytes:
    return json.dumps(obj).encode()


# --------------------------------------------------------------------------- #
# 1. 海外判定
# --------------------------------------------------------------------------- #
def test_has_overseas_url_matrix():
    cases = [
        (_body({"url": _YT}), True, "YouTube 单链接 → 海外"),
        (_body({"url": _BILI}), False, "B站 → 国内，不得转发"),
        (_body({"url": "https://b23.tv/abc123"}), False, "B站短链 b23.tv → 国内"),
        (_body({"urls": ["https://v.douyin.com/x/", "https://www.youku.com/x"]}), False,
         "整批国内站 → 不转发"),
        (_body({"urls": [_BILI, "https://www.youtube.com/watch?v=x"]}), True,
         "混合批次含海外 → 转发（国内站由对端回派）"),
        (_body({"urls": ["https://x.com/a"]}), True, "推特 → 海外"),
        (_body({"url": ""}), False, "空链接 → 不转发"),
        (b"{}", False, "无 url/urls 字段 → 不转发"),
        (b"{oops", False, "坏 JSON → 不转发（不得抛异常打断请求）"),
        (b"", False, "空请求体 → 不转发"),
    ]
    for raw, want, why in cases:
        got = A._has_overseas_url(raw)
        assert got is want, f"{why}：期望 {want}，实际 {got}"
    print("✅ 海外判定正确（单链接/批量/混合/国内站/坏 JSON 全覆盖）")


def test_urls_in_body_handles_both_keys():
    assert A._urls_in_body(_body({"url": _YT})) == [_YT]
    assert A._urls_in_body(_body({"urls": [_BILI, _YT]})) == [_BILI, _YT]
    # url 是列表 / urls 是字符串这种畸形输入不该炸，也不该被当成链接
    assert A._urls_in_body(_body({"url": [_YT]})) == [_YT]
    assert A._urls_in_body(_body({"urls": _YT})) == [_YT]
    assert A._urls_in_body(b"[1,2,3]") == []
    print("✅ 请求体取链接：url / urls 两种键都能识别，畸形输入不抛错")


# --------------------------------------------------------------------------- #
# 2~5. 中间件闸门（离线驱动，不建 TestClient、不打网络）
# --------------------------------------------------------------------------- #
def _mk_request(path, method="POST", body=b"", query=""):
    sent = {"done": False}

    async def receive():
        if not sent["done"]:
            sent["done"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": [(b"content-type", b"application/json")],
        "scheme": "http",
        "server": ("testserver", 80),
        "root_path": "",
    }
    return Request(scope, receive)


def _run_middleware(path, *, body=b"", method="POST", query="", status=200):
    """驱动 `_peer_overseas_fallback`，返回 (响应, 转发记录列表)。

    `call_next` 返回一个可辨识的「本地处理」标记，用来区分「被转发」与「本地消化」。
    """
    seen = []

    async def call_next(request):
        return Response(content=b'{"local":true}', status_code=status,
                        media_type="application/json")

    resp = asyncio.run(A._peer_overseas_fallback(_mk_request(path, method, body, query), call_next))
    return resp, seen


def _with_peer(fn, *, region="cn", peer="https://hk.example.test"):
    """临时伪装成「国内节点 + 已配海外对端」，并吞掉真实转发调用。"""
    orig = (A.PEER_ENDPOINT, A.NODE_REGION, A._peer_forward)
    fwd = []

    async def fake_forward(request, path, body):
        fwd.append({"path": path, "method": request.method, "body": body,
                    "query": request.url.query})
        return JSONResponse({"forwarded": path}, status_code=200)

    A.PEER_ENDPOINT, A.NODE_REGION, A._peer_forward = peer, region, fake_forward
    try:
        return fn(fwd)
    finally:
        A.PEER_ENDPOINT, A.NODE_REGION, A._peer_forward = orig


def test_overseas_post_is_forwarded():
    def _body_fn(fwd):
        resp, _ = _run_middleware("/api/resolve", body=_body({"url": _YT}))
        assert len(fwd) == 1, f"海外链接应被转发一次，实际 {len(fwd)} 次"
        assert fwd[0]["path"] == "/api/resolve"
        assert json.loads(fwd[0]["body"])["url"] == _YT, "请求体必须原样带上"
        assert json.loads(resp.body)["forwarded"] == "/api/resolve", "应回对端响应"
        print("✅ 海外链接（resolve）→ 原样转发对端，请求体不丢")

    _with_peer(_body_fn)


def test_domestic_post_stays_local():
    def _body_fn(fwd):
        resp, _ = _run_middleware("/api/resolve", body=_body({"url": _BILI}))
        assert not fwd, f"国内站不得被转发，却转了 {fwd}"
        assert json.loads(resp.body)["local"] is True, "国内站必须由本节点处理"
        print("✅ 国内站 → 本地消化，绝不绕海外")

    _with_peer(_body_fn)


def test_batch_with_overseas_is_forwarded_whole():
    def _body_fn(fwd):
        _run_middleware("/api/batch", body=_body({"urls": [_BILI, _YT]}))
        assert len(fwd) == 1 and fwd[0]["path"] == "/api/batch"
        assert json.loads(fwd[0]["body"])["urls"] == [_BILI, _YT], "整批原样转，不拆批"
        print("✅ 混合批次 → 整批转对端（国内站由对端按 VDL_WORKER_URL 回派）")

    _with_peer(_body_fn)


def test_disabled_on_global_node_or_single_node():
    # 海外节点自己就是「对端」，再转发会自我循环
    def _body_fn(fwd):
        _run_middleware("/api/resolve", body=_body({"url": _YT}))
        assert not fwd, "海外节点不得自我转发"

    _with_peer(_body_fn, region="global")

    # 单节点部署（没配 VDL_PEER_ENDPOINT）：行为必须与改动前完全一致
    _with_peer(_body_fn, peer="")

    # OPTIONS 预检不能转发，否则 CORS 预检会失效
    def _opt_fn(fwd):
        resp, _ = _run_middleware("/api/resolve", body=b"", method="OPTIONS")
        assert not fwd, "OPTIONS 预检不得被转发"

    _with_peer(_opt_fn)
    print("✅ 闸门正确：海外节点 / 单节点 / OPTIONS 预检 一律不转发")


def test_unknown_task_falls_back_to_peer():
    def _body_fn(fwd):
        _run_middleware("/api/tasks/deadbeefdeadbeef", body=b"", method="GET", status=404)
        assert len(fwd) == 1 and fwd[0]["path"] == "/api/tasks/deadbeefdeadbeef", \
            "本节点没有的任务应转对端"
        print("✅ 本节点不存在的任务 → 转对端（旧页面把对端 task_id 打到本节点也能用）")

    _with_peer(_body_fn)

    def _local_fn(fwd):
        _run_middleware("/api/tasks/deadbeefdeadbeef", body=b"", method="GET", status=200)
        assert not fwd, "本节点已有的任务必须本地处理"
        print("✅ 本节点已有的任务 → 本地处理，不转发")

    _with_peer(_local_fn)


def test_big_file_and_sse_redirect_to_peer():
    """大文件 / SSE 不能缓冲：只回 307，让浏览器直连对端取件。"""
    orig = (A.PEER_ENDPOINT, A.NODE_REGION)
    A.PEER_ENDPOINT, A.NODE_REGION = "https://hk.example.test", "cn"
    try:
        for path in ("/api/tasks/abc123/file", "/api/tasks/abc123/events"):
            resp, _ = _run_middleware(path, body=b"", method="GET",
                                      query="download=1&device=dev-1", status=404)
            assert resp.status_code == 307, f"{path} 应 307，实际 {resp.status_code}"
            loc = resp.headers.get("location", "")
            assert loc == f"https://hk.example.test{path}?download=1&device=dev-1", loc
    finally:
        A.PEER_ENDPOINT, A.NODE_REGION = orig
    print("✅ 取件 / 事件流 → 307 直连对端（保留查询串，视频不经国内节点中转）")


def test_ratchet_middleware_guards_and_suffixes():
    """源码棘轮：闸门条件与「不缓冲」后缀清单被改回去就红。"""
    src = open(os.path.join(_SERVER_DIR, "app.py"), encoding="utf-8").read()
    assert "_PEER_FWD_REDIRECT_SUFFIX = (\"/file\", \"/events\")" in src, \
        "取件/事件流必须走 307（改回缓冲会把几十 MB 视频塞进国内节点内存）"
    assert "if not PEER_ENDPOINT or NODE_REGION != \"cn\" or request.method == \"OPTIONS\":" in src, \
        "闸门必须同时要求「配了对端」「本节点是国内节点」「非预检」"
    assert "resp.status_code == 404" in src, \
        "任务路径必须按「本节点 404 才转发」判定，别无条件转发"
    print("✅ 棘轮：闸门条件与 307 后缀清单未被改回")


if __name__ == "__main__":
    test_has_overseas_url_matrix()
    test_urls_in_body_handles_both_keys()
    test_overseas_post_is_forwarded()
    test_domestic_post_stays_local()
    test_batch_with_overseas_is_forwarded_whole()
    test_disabled_on_global_node_or_single_node()
    test_unknown_task_falls_back_to_peer()
    test_big_file_and_sse_redirect_to_peer()
    test_ratchet_middleware_guards_and_suffixes()
    print("\n🎉 海外请求兜底转发测试全部通过（9 项）")
