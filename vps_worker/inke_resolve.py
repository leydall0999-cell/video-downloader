"""映客直播/回放解析 —— VPS worker（纯 HTTP，无需浏览器）。

映客直播间 URL 形如：
  https://www.inke.cn/liveroom/index.html?uid={uid}&id={liveid}
  （uid / id 参数顺序可能互换）

公开接口（无需登录、无需签名）：
  https://webapi.busi.inke.cn/web/live_share_pc?uid={uid}&id={id}
返回：
  data.media_info.nick      主播昵称
  data.status               1 = 正在直播
  data.live_name            直播标题（如「正在直播中」）
  data.liveid               当前直播 ID（= URL 中的 id）
  data.records[]            历史回放（每场 liveid + record_url m3u8）

真实流地址规律（已实测）：
  https://record2.inke.cn/record_{liveid}/{liveid}.m3u8?uid=0
该 m3u8 带 EXT-X-ENDLIST，本质是「开播至今的 DVR 窗口」，可一次性下载为 mp4；
若当前 liveid 暂无录制（极短直播/刚开播），则回退到 records[0].record_url 取最近回放。
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_REFERER = "https://www.inke.cn/"
_SHARE_API = "https://webapi.busi.inke.cn/web/live_share_pc"


def _pick_ids(url: str):
    uid_m = re.search(r"[?&]uid=([^&]+)", url)
    id_m = re.search(r"[?&]id=([^&]+)", url)
    uid = urllib.parse.unquote(uid_m.group(1)) if uid_m else ""
    lid = urllib.parse.unquote(id_m.group(1)) if id_m else ""
    return uid, lid


def _get_json(url: str, timeout: int = 15):
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": _UA, "Referer": _REFERER}
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _stream_alive(url: str) -> bool:
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": _UA, "Referer": _REFERER},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=12) as r:
            body = r.read(200).decode("utf-8", "replace")
        return r.status == 200 and "#EXTM3U" in body
    except Exception:
        return False


def resolve(url: str, timeout: int = 30) -> dict:
    uid, lid = _pick_ids(url)
    if not lid:
        return {"ok": False,
                "error": "无法从链接提取映客直播间 ID（需含 uid 与 id 参数，如 liveroom/index.html?uid=..&id=..）"}

    d = _get_json(
        f"{_SHARE_API}?uid={urllib.parse.quote(uid)}&id={urllib.parse.quote(lid)}"
        f"&_t={int(time.time() * 1000)}"
    ) or {}
    data = d.get("data") or {}

    mi = data.get("media_info") or {}
    nick = (mi.get("nick") if isinstance(mi, dict) else "") or ""
    live_name = data.get("live_name") or ""
    status = data.get("status")
    portrait = data.get("portrait") or ""

    if nick and live_name:
        title = f"{nick} {live_name}"
    elif nick:
        title = nick
    elif live_name:
        title = live_name
    else:
        title = "映客直播"

    # 主地址：当前直播 ID 的 DVR 窗口
    stream = f"https://record2.inke.cn/record_{lid}/{lid}.m3u8?uid=0"
    is_live = bool(status == 1)

    # 校验主地址存活；不存活则回退到最近一场回放
    if not _stream_alive(stream):
        recs = data.get("records") or []
        for rec in recs:
            ru = (rec.get("record_url") or "").replace("http://", "https://")
            if ru and _stream_alive(ru):
                stream = ru
                is_live = False
                if not live_name and rec.get("title"):
                    title = f"{nick} {rec.get('title')}".strip() if nick else rec.get("title")
                break
        else:
            if not _stream_alive(stream):
                return {"ok": False,
                        "error": "映客该直播间暂无可用视频流（未开播且无回放）"}

    return {
        "ok": True,
        "video_id": lid,
        "title": title[:150],
        "uploader": nick,
        "duration": None,
        "thumbnail": portrait,
        "webpage_url": url,
        "video_url": stream,
        "ext": "m3u8",
        "is_live": is_live,
    }


if __name__ == "__main__":
    u = sys.argv[1] if len(sys.argv) > 1 else ""
    print(json.dumps(resolve(u), ensure_ascii=False, indent=2))
