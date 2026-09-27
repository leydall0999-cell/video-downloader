"""CDP 浏览器嗅探（对标竞品 DataTool 的「悬浮球」能力，2026-09-27）。

原理：连接用户浏览器的 Chrome DevTools Protocol（--remote-debugging-port），
Network.enable 监听每个标签页的请求/响应，按 mimeType + URL 后缀判定媒体资源
（HLS/DASH 清单、mp4/webm 直链），去重后列在工坊 UI；同时在页面右下角注入
悬浮球（Shadow DOM，不污染页面样式），用户点「下载」的项目经
window.__VDL_SNIFF.outbox 由嗅探端 Runtime.evaluate 轮询取回 ——
**不走页面 fetch，完全绕开目标站点的 CSP connect-src 限制**（YouTube 等站点
会拦掉往 127.0.0.1 发的 fetch，这是走 outbox 而不是 API 回传的原因）。

线程模型：独立后台线程 + 独立 asyncio loop（FastAPI 的 def 路由直接调用
同步方法，互不干扰）。每个 page target 一条 websocket，两条协程：
reader（收事件）+ ticker（1.5s：轮询 outbox、刷新悬浮球列表）；
外层监视协程每 3s 拉 /json 发现/回收标签页。

浏览器启动：Chrome 136+ 禁止默认 profile 开 remote debugging，
因此必须用独立 user-data-dir（~/.video-downloader/cdp-profile）启动，
不动用户现有会话。
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import subprocess
import threading
import time
import urllib.request
from collections import OrderedDict
from typing import Any
from urllib.parse import urlsplit

try:
    import websockets  # uvicorn[standard] 自带
except ImportError:  # pragma: no cover
    websockets = None

MAX_ITEMS = 200          # 环形上限：URL 全量去重，超限弹最旧
TICKER_INTERVAL = 1.5    # outbox 轮询 / 悬浮球刷新
WATCH_INTERVAL = 3.0     # /json 监视（发现新标签页）
CDP_HTTP_TIMEOUT = 3.0

# ---------------------------------------------------------------------------
# 媒体判定（纯函数，便于离线测试）
# ---------------------------------------------------------------------------

_PLAYLIST_SUFFIX_RE = None  # 延迟编译
_MIME_PLAYLIST = ("application/vnd.apple.mpegurl", "application/x-mpegurl", "application/dash+xml", "mpegurl")
_MIME_MEDIA_PREFIX = ("video/", "audio/")
_MEDIA_SUFFIXES = (".mp4", ".webm", ".m4v", ".mov", ".flv", ".mkv", ".mp3", ".m4a", ".aac", ".wav", ".ogg")
_SEGMENT_SUFFIXES = (".m4s", ".ts", ".mp4")  # mp4 同时是直链与分片形态，按 mime 区分
_IGNORED_SUFFIXES = (".vtt", ".srt", ".ass", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".css", ".js", ".html", ".json", ".xml", ".txt", ".ico")


def _path_suffix(url: str) -> str:
    """取 URL 路径的小写后缀（含点）。查询串被剥掉；无后缀返回 ''。"""
    try:
        path = urlsplit(url).path
    except ValueError:
        return ""
    # yt-dlp 风格的无后缀签名 URL（如 googlevideo/videoplayback?...）返回空
    base = os.path.basename(path)
    if "." not in base:
        return ""
    return "." + base.rsplit(".", 1)[1].lower()


def classify_media(url: str, mime: str = "") -> str:
    """判定 CDP 资源是否值得展示。返回 "playlist" | "media" | "segment" | ""。

    - playlist：m3u8 / mpd 清单 —— 可直接交给下载链路的**一等公民**
    - media：mp4/webm/音频直链
    - segment：m4s/ts 分片 —— 只聚合计数（用户中途才开嗅探时清单已错过，
      提示「先开启嗅探再播放」），嗅到清单后该占位会被清单取代
    - 空：与媒体无关（字幕/图片/接口 JSON 一律过滤）
    """
    mime = (mime or "").split(";")[0].strip().lower()
    suffix = _path_suffix(url)
    if suffix in _IGNORED_SUFFIXES:
        return ""
    if mime.startswith("image/") or mime in ("text/html", "text/css", "application/javascript", "text/javascript"):
        return ""
    if mime in _MIME_PLAYLIST or suffix in (".m3u8", ".mpd") or "mpegurl" in mime:
        return "playlist"
    if mime == "application/dash+xml":
        return "playlist"
    if mime.startswith(_MIME_MEDIA_PREFIX):
        # video/mp4 可能是整片直链也可能是 fMP4 分片：.m4s 一定是分片；
        # 纯 .mp4 + video/mp4 按直链算（整片直链远多于逐段 .mp4 分片）
        if suffix == ".m4s":
            return "segment"
        return "media"
    if suffix in (".m4s", ".ts"):
        # 无 mime（requestWillBeSent 阶段）也认分片
        return "segment"
    if suffix in _MEDIA_SUFFIXES:
        return "media"
    return ""


def _short_origin(url: str) -> str:
    try:
        p = urlsplit(url)
        return f"{p.scheme}://{p.netloc}" if p.netloc else url[:60]
    except ValueError:
        return url[:60]


# ---------------------------------------------------------------------------
# 悬浮球注入脚本（在目标页面上下文执行；Shadow DOM + outbox，绕 CSP）
# ---------------------------------------------------------------------------

_BALL_JS = r"""
(function () {
  if (window.__VDL_SNIFF) { window.__VDL_SNIFF.render(); return; }
  var S = { items: [], outbox: [] };
  window.__VDL_SNIFF = S;
  var host = document.createElement('div');
  host.setAttribute('data-vdl-sniff', '1');
  host.style.cssText = 'all:initial;position:fixed;right:18px;bottom:18px;z-index:2147483647;';
  function mount() {
    if (!document.body) { return false; }
    document.body.appendChild(host); return true;
  }
  if (!mount()) { document.addEventListener('DOMContentLoaded', mount); }
  var root = host.attachShadow ? host.attachShadow({mode: 'closed'})
                               : host.createShadowRoot();
  var css = '*{box-sizing:border-box;font:13px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;}' +
    '.ball{width:46px;height:46px;border-radius:50%;background:linear-gradient(135deg,#6c5ce7,#00b894);' +
    'box-shadow:0 4px 14px rgba(0,0,0,.35);cursor:pointer;display:flex;align-items:center;justify-content:center;' +
    'color:#fff;font-size:20px;position:relative;}' +
    '.badge{position:absolute;top:-4px;right:-4px;min-width:18px;height:18px;border-radius:9px;' +
    'background:#e74c3c;color:#fff;font-size:11px;display:none;align-items:center;justify-content:center;padding:0 4px;}' +
    '.panel{display:none;margin-bottom:8px;width:330px;max-height:340px;overflow:auto;background:#fff;' +
    'border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.3);padding:10px;color:#222;}' +
    '.panel h4{margin:0 0 8px;font-size:13px;color:#333;}' +
    '.row{border-bottom:1px solid #f0f0f0;padding:6px 0;}' +
    '.row .u{color:#555;font-size:12px;word-break:break-all;max-height:32px;overflow:hidden;}' +
    '.row button{margin-top:4px;border:0;border-radius:5px;background:#6c5ce7;color:#fff;' +
    'padding:3px 10px;cursor:pointer;font-size:12px;}' +
    '.empty{color:#999;padding:8px 0;}';
  var st = document.createElement('style'); st.textContent = css; root.appendChild(st);
  var panel = document.createElement('div'); panel.className = 'panel'; root.appendChild(panel);
  var ball = document.createElement('div'); ball.className = 'ball'; root.appendChild(ball);
  ball.innerHTML = '<span>⬇</span><span class="badge"></span>';
  var badge = ball.querySelector('.badge');
  ball.addEventListener('click', function () {
    panel.style.display = (panel.style.display === 'block') ? 'none' : 'block';
  });
  S.render = function () {
    var n = S.items.length;
    badge.style.display = n ? 'flex' : 'none';
    badge.textContent = n > 99 ? '99+' : String(n);
    if (panel.style.display !== 'block') return;
    if (!n) { panel.innerHTML = '<h4>视频工坊嗅探</h4><div class="empty">播放视频后这里会出现可下载的流</div>'; return; }
    var h = '<h4>视频工坊嗅探（' + n + '）</h4>';
    S.items.slice(0, 20).forEach(function (it, i) {
      var kind = it.kind === 'playlist' ? 'HLS/DASH' : '直链';
      h += '<div class="row"><div class="u">[' + kind + '] ' + it.url.slice(0, 120) + '</div>' +
           '<button data-i="' + i + '">下载</button></div>';
    });
    panel.innerHTML = h;
    panel.querySelectorAll('button').forEach(function (b) {
      b.addEventListener('click', function () {
        var it = S.items[Number(b.getAttribute('data-i'))];
        if (it) { S.outbox.push({ url: it.url, referer: it.referer || '', mime: it.mime || '', page_url: it.page_url || '' }); b.textContent = '已加入 ✓'; }
      });
    });
  };
  S.setItems = function (items) { S.items = items || []; S.render(); };
  S.render();
})();
"""

_OUTBOX_READ_JS = (
    "(function(){try{return JSON.stringify(window.__VDL_SNIFF"
    " ? window.__VDL_SNIFF.outbox.splice(0) : []);}catch(e){return '[]';}})()"
)

_SETITEMS_JS_PREFIX = "window.__VDL_SNIFF && window.__VDL_SNIFF.setItems("


# ---------------------------------------------------------------------------
# 嗅探器主体
# ---------------------------------------------------------------------------

class CDPSniffer:
    """单例状态机。线程安全：items/outbox 有锁，ws 连接只在嗅探线程里动。"""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_evt: threading.Event = threading.Event()
        self._port = 0
        self._launched_proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._items: OrderedDict[str, dict] = OrderedDict()   # url -> item
        self._picked: list[dict] = []                          # 悬浮球回传的下载请求
        self._error = ""
        self._state = "idle"                                   # idle / running / error

    # ---- 供路由层调用的同步接口 ----

    @property
    def state(self) -> str:
        return self._state

    def status(self) -> dict:
        with self._lock:
            return {
                "state": self._state,
                "port": self._port,
                "items": len(self._items),
                "picked": len(self._picked),
                "error": self._error,
                "supported": websockets is not None,
            }

    def items(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = list(self._items.values())
        return rows[-limit:][::-1]  # 最新在前

    def take_picked(self) -> list[dict]:
        with self._lock:
            rows, self._picked = self._picked, []
        return rows

    def add_manual(self, payload: dict) -> dict:
        """悬浮球/外部直接提交的下载项（嗅探端 outbox 轮询失败时的兜底入口）。"""
        url = (payload.get("url") or "").strip()
        if not url:
            raise ValueError("url is required")
        item = {
            "url": url,
            "mime": (payload.get("mime") or "").strip(),
            "kind": classify_media(url, payload.get("mime") or "") or "media",
            "referer": (payload.get("referer") or "").strip(),
            "page_url": (payload.get("page_url") or "").strip(),
            "page_title": payload.get("page_title") or "",
            "first_seen": time.time(),
            "count": 1,
            "source": "manual",
        }
        with self._lock:
            self._picked.append(item)
        return item

    def start(self, port: int = 9222, launch: bool = False) -> dict:
        if websockets is None:
            raise RuntimeError("websockets 库不可用（uvicorn[standard] 未安装）")
        if self._thread and self._thread.is_alive():
            return self.status()
        if not self._http_alive(port):
            if launch:
                self._launch_browser(port)
                self._wait_http(port, timeout=12.0)
            if not self._http_alive(port):
                self._state, self._error = "error", (
                    f"127.0.0.1:{port} 上没有调试浏览器；请开启「以调试模式启动浏览器」"
                    "或让 Chrome 带 --remote-debugging-port 重启")
                return self.status()
        self._stop_evt.clear()
        self._port = port
        self._error = ""
        self._thread = threading.Thread(target=self._run, name="vdl-cdp-sniffer", daemon=True)
        self._thread.start()
        return self.status()

    def stop(self) -> dict:
        self._stop_evt.set()
        proc, self._launched_proc = self._launched_proc, None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=6.0)
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass
        self._state, self._port = "idle", 0
        return self.status()

    # ---- 内部 ----

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._watch())
        except Exception as exc:  # noqa: BLE001 - 后台线程兜底
            self._state, self._error = "error", f"嗅探线程异常: {exc}"
        finally:
            try:
                self._loop.close()
            except Exception:  # noqa: BLE001
                pass

    async def _watch(self) -> None:
        """监视 /json：新 page 建连、消失页断连。"""
        self._state = "running"
        conns: dict[str, asyncio.Task] = {}
        try:
            while not self._stop_evt.is_set():
                try:
                    targets = await asyncio.to_thread(self._list_pages, self._port)
                except Exception:  # noqa: BLE001 - 浏览器暂时没起来
                    targets = []
                alive = set()
                for t in targets:
                    ws_url = t.get("webSocketDebuggerUrl") or ""
                    if not ws_url:
                        continue
                    alive.add(ws_url)
                    if ws_url not in conns:
                        conns[ws_url] = asyncio.create_task(
                            self._page_worker(ws_url, t.get("title") or ""))
                for k in list(conns):
                    if k not in alive:
                        conns[k].cancel()
                        conns.pop(k, None)
                await asyncio.sleep(WATCH_INTERVAL)
        finally:
            for t in conns.values():
                t.cancel()

    async def _page_worker(self, ws_url: str, title: str) -> None:
        import websockets as _ws
        seq = 0
        pending: dict[str, dict] = {}   # requestId -> {url, headers, page_url}
        try:
            async with _ws.connect(ws_url, max_size=None, open_timeout=8) as ws:
                seq += 1
                await ws.send(json.dumps({"id": seq, "method": "Network.enable"}))
                seq += 1
                await ws.send(json.dumps({"id": seq, "method": "Page.addScriptToEvaluateOnNewDocument",
                                          "params": {"source": _BALL_JS}}))
                seq += 1
                await ws.send(json.dumps({"id": seq, "method": "Runtime.evaluate",
                                          "params": {"expression": _BALL_JS}}))
                last_ticker = 0.0
                while not self._stop_evt.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                    except asyncio.TimeoutError:
                        raw = None
                    if raw is not None:
                        self._on_message(json.loads(raw), pending, title)
                    now = time.time()
                    if now - last_ticker >= TICKER_INTERVAL:
                        last_ticker = now
                        await self._tick(ws, ws_url)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if not self._stop_evt.is_set():
                self._error = f"标签页连接断开: {exc}"[:200]

    def _on_message(self, msg: dict, pending: dict, title: str) -> None:
        method = msg.get("method") or ""
        params = msg.get("params") or {}
        if not method and "id" in msg:
            # Runtime.evaluate 的响应：outbox 读取那条会带回用户点击的下载项
            value = ((msg.get("result") or {}).get("result") or {}).get("value")
            if isinstance(value, str) and value.startswith("[") and value != "[]":
                try:
                    rows = json.loads(value)
                except ValueError:
                    return
                if isinstance(rows, list) and rows:
                    with self._lock:
                        self._picked.extend(rows)
            return
        if method == "Network.requestWillBeSent":
            req = params.get("request") or {}
            rid = params.get("requestId") or ""
            if rid:
                pending[rid] = {
                    "url": req.get("url") or "",
                    "headers": req.get("headers") or {},
                    "page_url": params.get("documentURL") or "",
                }
                if len(pending) > 800:  # 防内存：只留最近的 requestId
                    for k in list(pending)[: -400]:
                        pending.pop(k, None)
            return
        if method == "Network.responseReceived":
            rid = params.get("requestId") or ""
            resp = params.get("response") or {}
            meta = pending.get(rid) or {}
            url = resp.get("url") or meta.get("url") or ""
            if not url:
                return
            kind = classify_media(url, resp.get("mimeType") or "")
            if not kind:
                return
            headers = meta.get("headers") or {}
            referer = headers.get("Referer") or headers.get("referer") or ""
            self._register(url=url, mime=resp.get("mimeType") or "", kind=kind,
                           referer=referer, page_url=meta.get("page_url") or "",
                           page_title=title)
            if len(pending) > 800:  # 防内存：只留最近的 requestId
                for k in list(pending)[: -400]:
                    pending.pop(k, None)

    async def _tick(self, ws: Any, ws_url: str) -> None:
        """低频轮询：读 outbox + 把最新 items 推给悬浮球。"""
        try:
            await ws.send(json.dumps({"id": int(time.time() * 1000) & 0x7FFFFFFF,
                                      "method": "Runtime.evaluate",
                                      "params": {"expression": _OUTBOX_READ_JS, "returnByValue": True}}))
            rows = self.items(limit=30)
            payload = json.dumps([{k: r.get(k) for k in ("url", "mime", "kind", "referer", "page_url")}
                                  for r in rows], ensure_ascii=False)
            await ws.send(json.dumps({"id": int(time.time() * 1000) & 0x7FFFFFFF,
                                      "method": "Runtime.evaluate",
                                      "params": {"expression": _SETITEMS_JS_PREFIX + payload + ");"}}))
        except Exception:  # noqa: BLE001 - ticker 失败不打断嗅探
            pass

    def _register(self, *, url: str, mime: str, kind: str, referer: str,
                  page_url: str, page_title: str) -> None:
        with self._lock:
            if url in self._items:
                self._items[url]["count"] += 1
                return
            item = {
                "url": url, "mime": mime, "kind": kind, "referer": referer,
                "page_url": page_url, "page_title": page_title,
                "first_seen": time.time(), "count": 1, "source": "cdp",
            }
            if kind == "playlist":
                # 清单到手：淘汰同来源页的分片占位
                for k in [k for k, v in self._items.items()
                          if v.get("kind") == "segment" and v.get("page_url") == page_url]:
                    self._items.pop(k, None)
            elif kind == "segment":
                # 同来源页已有占位则只计数
                for v in self._items.values():
                    if v.get("kind") == "segment" and v.get("page_url") == page_url:
                        v["count"] += 1
                        return
                item["url"] = url          # 占位保留第一条分片地址便于排查
                item["kind"] = "segment"
            self._items[url] = item
            while len(self._items) > MAX_ITEMS:
                self._items.popitem(last=False)

    # ---- 浏览器探测 / 启动 ----

    @staticmethod
    def _list_pages(port: int) -> list[dict]:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/json", method="GET")
        with urllib.request.urlopen(req, timeout=CDP_HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return [t for t in data if t.get("type") == "page"]

    @staticmethod
    def _http_alive(port: int) -> bool:
        try:
            CDPSniffer._list_pages(port)
            return True
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _wait_http(port: int, timeout: float = 12.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if CDPSniffer._http_alive(port):
                return True
            time.sleep(0.4)
        return False

    def _launch_browser(self, port: int) -> None:
        system = platform.system()
        candidates: list[str] = []
        if system == "Darwin":
            candidates = [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            ]
        elif system == "Windows":  # pragma: no cover
            candidates = [
                os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            ]
        else:  # Linux
            for name in ("google-chrome", "chromium", "chromium-browser"):
                p = shutil.which(name)
                if p:
                    candidates.append(p)
        exe = next((c for c in candidates if os.path.exists(c)), "")
        if not exe:
            raise RuntimeError("本机没有找到 Chrome/Chromium/Edge，无法以调试模式启动浏览器")
        profile = os.path.expanduser("~/.video-downloader/cdp-profile")
        os.makedirs(profile, exist_ok=True)
        self._launched_proc = subprocess.Popen(
            [exe, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
             "--no-first-run", "--no-default-browser-check", "--start-maximized",
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=(system != "Windows"))
        time.sleep(1.0)


SNIFFER = CDPSniffer()
