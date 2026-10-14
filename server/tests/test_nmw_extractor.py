"""金牌影院（nmwchxx.com）解析器离线守卫。

覆盖：
1. platforms.py：平台条目 + CHINA_DOMAINS（国内直连）；
2. downloader.py：_nmw_info / _nmw_api 存在；probe() 与下载任务执行段两处分发；
3. 签名公式锁定：sign = SHA1(MD5_hex("排序参数串&key=<key>&t=<毫秒>"))
   （2026-10-14 用两组真实抓包交叉验证过；写错一层都算回归）；
4. 装配逻辑（AST 抽出纯函数 + sys.modules 注入假 requests，离线跑）：
   URL 识别失败 / detail 标题拼接 / 多清晰度排序 / best 选取 / 接口非 200 报错。

运行：python tests/test_nmw_extractor.py   （无需网络）
"""

import ast
import hashlib
import pathlib
import sys
import types

SERVER = pathlib.Path(__file__).resolve().parent.parent


def check(name: str, cond: bool) -> None:
    print(("  ✅ " if cond else "  ❌ ") + name)
    if not cond:
        globals()["_FAILED"] = True


_FAILED = False


# ---------------------------------------------------------------- 源码棘轮
def test_source_ratchets() -> None:
    print("① 源码棘轮")
    dl = (SERVER / "downloader.py").read_text(encoding="utf-8")
    pl = (SERVER / "platforms.py").read_text(encoding="utf-8")
    check("platforms.py 有金牌影院条目",
          'Platform("nmwchxx", "金牌影院"' in pl)
    check("CHINA_DOMAINS 含 nmwchxx.com（国内直连）",
          '"nmwchxx.com",' in pl)
    check("downloader.py 定义 _nmw_info / _nmw_api",
          "def _nmw_info(" in dl and "def _nmw_api(" in dl)
    check("签名公式棘轮（SHA1(MD5) 双层，key 常量在位）",
          'hashlib.sha1(hashlib.md5(x.encode()).hexdigest().encode()).hexdigest()' in dl
          and '_NMW_SIGN_KEY = "cb808529bae6b6be45ecfab29a4889bc"' in dl)
    # 两处分发：probe() 与下载任务执行段（只接一处 = 能解析不能下载）
    check("probe() 分发（host → _nmw_info）",
          'if "nmwchxx.com" in (host or ""):' in dl
          and "return _nmw_info(url, proxy=effective_proxy)" in dl)
    check("任务执行段分发（_task_host → _nmw_info）",
          'elif "nmwchxx.com" in (_task_host or ""):' in dl
          and "info = _nmw_info(task.url, proxy=effective_proxy)" in dl)
    check("episode/url 端点路径正确",
          '"/mw-movie/anonymous/v2/video/episode/url"' in dl)


# ---------------------------------------------------------------- 纯函数装配
class _ResolveError(Exception):
    """与真 ResolveError 同签名（message, hint, category, context）。"""

    def __init__(self, message, hint="", category="", context=None):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.category = category


def _extract_fns(src: str, names: tuple[str, ...]) -> dict:
    tree = ast.parse(src)
    return {n.name: n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in names}


def test_assembly_offline() -> None:
    print("② 装配逻辑（离线 stub）")
    src = (SERVER / "downloader.py").read_text(encoding="utf-8")
    fns = _extract_fns(src, ("_nmw_api", "_nmw_info"))
    missing = [n for n in ("_nmw_api", "_nmw_info") if n not in fns]
    check("AST 抽取出 _nmw_api / _nmw_info", not missing)
    if missing:
        return

    detail_resp = {
        "code": 200,
        "data": {
            "vodName": "少帅",
            "episodeList": [{"nid": "877399", "name": "1"}, {"nid": "877404", "name": "2"}],
        },
    }
    play_resp = {
        "code": 200,
        "data": {"list": [
            {"resolution": 480, "resolutionName": "标清",
             "url": "https://cdn.example/480/index.m3u8?auth_key=low"},
            {"resolution": 1080, "resolutionName": "蓝光",
             "url": "https://cdn.example/1080/index.m3u8?auth_key=high"},
            {"resolution": 720, "resolutionName": "高清",
             "url": "https://cdn.example/720/index.m3u8?auth_key=mid"},
        ]},
    }
    captured = {}

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def fake_get(url, headers=None, proxies=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers or {}
        if "episode/url" in url:
            return _Resp(play_resp)
        return _Resp(detail_resp)

    # _nmw_api 内部是 `import requests as _req` / `import uuid`（局部导入）⇒ 必须走
    # sys.modules 注入假模块，命名空间全局糊弄不过去。
    fake_requests = types.ModuleType("requests")
    fake_requests.get = fake_get  # type: ignore[attr-defined]
    fake_uuid = types.ModuleType("uuid")
    fake_uuid.uuid4 = lambda: "fixed-uuid"  # type: ignore[attr-defined]
    saved_req = sys.modules.get("requests")
    saved_uuid = sys.modules.get("uuid")
    sys.modules["requests"] = fake_requests
    sys.modules["uuid"] = fake_uuid
    try:
        ns = {
            "re": __import__("re"),
            "time": types.SimpleNamespace(time=lambda: 1791950306.450),
            "hashlib": hashlib,
            "uuid": types.SimpleNamespace(uuid4=lambda: "fixed-uuid"),
            "ResolveError": _ResolveError,
            "Any": object,
            "dict": dict,
            "_NMW_SIGN_KEY": "cb808529bae6b6be45ecfab29a4889bc",
            "_NMW_UA": "UA-TEST",
        }
        for fn in fns.values():
            exec(compile(ast.Module([fn], []), "nmw_fragment", "exec"), ns)

        # ① URL 形态不对 → 明确报错（引导文案在 hint 里）
        try:
            ns["_nmw_info"]("https://nmwchxx.com/vod/detail/116537")
            check("非播放页链接应抛 ResolveError", False)
        except _ResolveError as e:
            check("非播放页链接抛 ResolveError（含引导文案）",
                  "无法从链接中识别视频 ID" in (str(e) + getattr(e, "hint", "")))

        # ② 正常链路：标题 + 三档清晰度 + best=1080
        info = ns["_nmw_info"]("https://nmwchxx.com/vod/play/116537/sid/877399")
        check("标题 = 片名 + 集名", info["title"] == "少帅 第1集")
        check("三档清晰度按 resolution 降序",
              [f["height"] for f in info["formats"]] == [1080, 720, 480])
        check("best（顶层 url）= 蓝光 1080",
              info["url"].endswith("1080/index.m3u8?auth_key=high"))
        check("协议 m3u8_native + direct + Referer 头",
              info["protocol"] == "m3u8_native" and info["direct"] is True
              and info["http_headers"]["Referer"] == "https://nmwchxx.com/")
        check("extractor_key/ext 正确",
              info["extractor_key"] == "NMWChxx" and info["ext"] == "mp4")

        # ③ 签名公式：固定 t → sign 必须与独立计算的 SHA1(MD5(x)) 逐位一致
        t_ms = "1791950306450"
        qs = "clientType=1&id=116537&nid=877399"
        x = f"{qs}&key=cb808529bae6b6be45ecfab29a4889bc&t={t_ms}"
        expect = hashlib.sha1(hashlib.md5(x.encode()).hexdigest().encode()).hexdigest()
        check("请求头带 client-type/deviceId/sign(40 位)",
              captured["headers"].get("client-type") == "1"
              and captured["headers"].get("deviceId") == "fixed-uuid"
              and len(captured["headers"].get("sign") or "") == 40)
        check("sign 与独立计算的 SHA1(MD5(x)) 逐位一致（公式锁定）",
              captured["headers"]["sign"] == expect)

        # ④ 接口非 200 → 明确报错（透传服务端 msg）
        bad_resp = {"code": 500, "msg": "视频不存在"}

        def fake_get_bad(url, headers=None, proxies=None, timeout=None):
            if "detail" in url:
                return _Resp({"code": 200, "data": {"vodName": "少帅", "episodeList": []}})
            return _Resp(bad_resp)

        fake_requests.get = fake_get_bad  # type: ignore[attr-defined]
        try:
            ns["_nmw_info"]("https://nmwchxx.com/vod/play/116537/sid/877399")
            check("接口非 200 应抛 ResolveError", False)
        except _ResolveError as e:
            check("接口非 200 抛 ResolveError（透传服务端 msg）",
                  "视频不存在" in (str(e) + getattr(e, "hint", "")))
    finally:
        if saved_req is not None:
            sys.modules["requests"] = saved_req
        else:
            sys.modules.pop("requests", None)
        if saved_uuid is not None:
            sys.modules["uuid"] = saved_uuid
        else:
            sys.modules.pop("uuid", None)


def main() -> int:
    test_source_ratchets()
    test_assembly_offline()
    print("❌ 存在失败项" if _FAILED else "🎉 金牌影院解析器守卫全部通过")
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
