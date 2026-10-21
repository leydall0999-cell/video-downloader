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
          "if _is_nmw_host(host):" in dl
          and "return _nmw_info(url, proxy=effective_proxy)" in dl)
    check("任务执行段分发（_task_host → _nmw_info）",
          "elif _is_nmw_host(_task_host):" in dl
          and "info = _nmw_info(task.url, proxy=effective_proxy)" in dl)
    check("域名族判定在位（换域仍进解析器，不写死单域）",
          "def _is_nmw_host(" in dl and "_NMW_KNOWN_HOSTS" in dl)
    check("链接解析器在位（_nmw_parse_ids）",
          "def _nmw_parse_ids(" in dl)
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


def test_link_forms() -> None:
    """域名族 + 链接形态（纯函数，不起网络）。"""
    print("② 域名族与链接形态")
    src = (SERVER / "downloader.py").read_text(encoding="utf-8")
    ns: dict = {"re": __import__("re")}
    exec(src[src.index("_NMW_KNOWN_HOSTS = "):src.index("def _nmw_host(")], ns)
    for fn in _extract_fns(src, ("_nmw_host", "_is_nmw_host", "_nmw_parse_ids")).values():
        exec(compile(ast.Module([fn], []), "nmw_parse", "exec"), ns)

    is_nmw, parse, host_of = ns["_is_nmw_host"], ns["_nmw_parse_ids"], ns["_nmw_host"]
    for h in ("nmwchxx.com", "www.nmwchxx.com", "nmwcchx.com", "nmwchx.com"):
        check(f"域名族识别：{h}", is_nmw(h) is True)
    for h in ("kuaishou.com", "www.douyin.com", "", "nmw"):
        check(f"非本站不误判：{h or '(空)'}", is_nmw(h) is False)

    cases = {
        "https://nmwchxx.com/vod/play/116537/sid/877419": ("116537", "877419", ""),
        "https://nmwchxx.com/vod/play/116537/877419": ("116537", "877419", ""),
        "https://nmwchxx.com/vod/play/116537-1-1.html": ("116537", "", "1"),
        "https://nmwchxx.com/vod/detail/116537": ("116537", "", ""),
        "https://nmwchxx.com/play?id=116537&nid=877419": ("116537", "877419", ""),
        "https://nmwchxx.com/search?wd=abc": ("", "", ""),
    }
    for u, want in cases.items():
        check(f"解析 ID：{u.split('/', 3)[-1]}", parse(u) == want)
    check("host 提取（去端口/大小写）",
          host_of("https://WWW.NMWCHXX.com:8443/vod/play/1/2") == "www.nmwchxx.com")


def test_assembly_offline() -> None:
    print("③ 装配逻辑（离线 stub）")
    src = (SERVER / "downloader.py").read_text(encoding="utf-8")
    names = ("_nmw_api", "_nmw_info", "_nmw_parse_ids", "_nmw_host", "_is_nmw_host")
    fns = _extract_fns(src, names)
    missing = [n for n in names if n not in fns]
    check("AST 抽取出解析器全家桶", not missing)
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
        # 常量从源码里真取（别抄一份，抄的会跟着源码一起漂）
        const_ns: dict = {"re": __import__("re")}
        exec(src[src.index("_NMW_KNOWN_HOSTS = "):src.index("def _nmw_host(")], const_ns)

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
            "_NMW_KNOWN_HOSTS": const_ns["_NMW_KNOWN_HOSTS"],
            "_NMW_HOST_RE": const_ns["_NMW_HOST_RE"],
        }
        for fn in fns.values():
            exec(compile(ast.Module([fn], []), "nmw_fragment", "exec"), ns)

        # ① 链接形态：四种写法都要能解析。用户 2026-10-21 第三次报「又不行了」，
        #    起因就是站点这次给的是 /vod/play/{id}/{nid}（没有 /sid/）。
        forms = [
            ("/vod/play/116537/sid/877399", "877399"),  # 站内标准
            ("/vod/play/116537/877399", "877399"),      # 分享/外链（站内 404）
            ("/vod/play/116537-1-1.html", "877399"),    # maccms 老模板（集序号=1）
            ("/vod/detail/116537", "877399"),           # 详情页 → 自动取第一集
        ]
        for path, want in forms:
            captured.clear()
            info = ns["_nmw_info"]("https://nmwchxx.com" + path)
            check(f"{path} → 集 ID {want}", f"nid={want}" in captured.get("url", ""))
            check(f"{path} → 标题可用", info["title"].startswith("少帅"))

        # ② 真取不到任何 ID 才报错（引导文案在 hint 里）
        try:
            ns["_nmw_info"]("https://nmwchxx.com/vodplay/abc")
            check("无 ID 链接应抛 ResolveError", False)
        except _ResolveError as e:
            check("无 ID 链接抛 ResolveError（含引导文案）",
                  "无法从链接中识别视频 ID" in (str(e) + getattr(e, "hint", "")))

        # ③ 正常链路：标题 + 三档清晰度 + best=1080
        captured.clear()
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

        # ④ 签名公式：固定 t → sign 必须与独立计算的 SHA1(MD5(x)) 逐位一致
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

        # ⑤ 接口非 200 → 明确报错（透传服务端 msg）
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
    test_link_forms()
    test_assembly_offline()
    print("❌ 存在失败项" if _FAILED else "🎉 金牌影院解析器守卫全部通过")
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
