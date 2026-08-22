"""网易CC直播（cc.163.com）解析：纯 HTTP，无需 Playwright/登录。

原理：
- 房间 URL: https://cc.163.com/{cuteid}（cuteid 为新版频道号）
- 流地址 API（无鉴权，GET）:
    https://vapi.cc.163.com/video_play_url/{cuteid}?webrtc=0&src=webcc_4000_h5&vbrmode=1&secure=1&mix_audience_mode=0&pts_map=1&t={ts}
  → {"videourl": "https://*.cc.netease.com/pushstation/xxx.flv?auth_key=...",
     "bakvideourl": ..., "vbrname_list": [...], "anchor_uid": ...}
- 离线频道返回 HTTP 410 + {"code":"Gone","data":"no live"}
- 标题取自房间页 <title>（{nickname}的{nickname}直播间_...）
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
_REFERER = "https://cc.163.com/"
_VAPI = "https://vapi.cc.163.com/video_play_url/{cid}?webrtc=0&src=webcc_4000_h5&vbrmode=1&secure=1&mix_audience_mode=0&pts_map=1&t={ts}"


def _http_get(url: str, timeout: int = 20, referer: str = _REFERER) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Referer": referer,
    })
    try:
        return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        # cc.163.com 无尾斜杠会 308 永久重定向，urllib 不自动跟
        if e.code == 308 and e.headers.get("Location"):
            loc = urllib.parse.urljoin(url, e.headers["Location"])
            return _http_get(loc, timeout=timeout, referer=referer)
        raise


def _pick_cid(url: str) -> str:
    m = re.search(r"cc\.163\.com/(?:live/)?(\d+)", url)
    return m.group(1) if m else ""


def _fetch_title(cid: str) -> str:
    try:
        html = _http_get("https://cc.163.com/%s" % cid)
    except Exception:
        return ""
    # 页面无 <title> 标签，标题在 SSR JSON 的 "title" 字段（形如 "{nickname}的{nickname}直播间_..."）
    m = re.search(r'"title"\s*:\s*"([^"]{0,120})"', html)
    if not m:
        m = re.search(r"<title>([^<]{0,120})</title>", html)
    if not m:
        return ""
    t = m.group(1).strip()
    # 形如 "{nickname}的{nickname}直播间_{nickname}视频直播 - 网易CC直播"
    m2 = re.search(r"的([^的]{0,60}?)直播间", t)
    if m2:
        return m2.group(1).strip()
    return t.split("_")[0][:80]


def resolve(url: str, timeout: int = 60) -> dict:
    cid = _pick_cid(url)
    if not cid:
        return {"ok": False, "error": "无法从 URL 解析出网易CC频道号（需形如 cc.163.com/{id}）"}
    try:
        body = _http_get(_VAPI.format(cid=cid, ts=int(time.time() * 1000)))
    except urllib.error.HTTPError as e:
        if e.code == 410:
            return {"ok": False, "error": "频道 %s 当前不在直播（HTTP 410 no live）" % cid}
        return {"ok": False, "error": "vapi 请求失败 HTTP %s" % e.code}
    except Exception as e:
        return {"ok": False, "error": "vapi 请求异常: %s" % e}
    try:
        d = json.loads(body)
    except Exception:
        return {"ok": False, "error": "vapi 响应非 JSON"}
    video_url = d.get("videourl") or ""
    if not video_url:
        return {"ok": False, "error": "vapi 未返回 videourl（频道可能不在直播）"}
    title = _fetch_title(cid) or ("网易CC直播 %s" % cid)
    return {
        "ok": True,
        "video_id": cid,
        "title": title,
        "uploader": str(d.get("anchor_uid") or ""),
        "duration": None,
        "thumbnail": "",
        "webpage_url": "https://cc.163.com/%s" % cid,
        "video_url": video_url,
        "bak_video_url": d.get("bakvideourl") or "",
        "vbrs": d.get("vbrname_list") or [],
        "ext": "flv",
        "is_live": True,
    }


if __name__ == "__main__":
    import sys
    u = sys.argv[1] if len(sys.argv) > 1 else "https://cc.163.com/163163"
    print(json.dumps(resolve(u), ensure_ascii=False, indent=1))
