"""链接校验与平台识别。

对外暴露 `parse_source` 作为唯一入口：把用户粘贴的任意文本转成
(规范化 URL, 平台) 二元组，并在不合法时抛出语义明确的异常。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

MAX_INPUT_LENGTH = 2048
ALLOWED_SCHEMES = ("http", "https")
URL_PATTERN = re.compile(r"https?://[^\s\u4e00-\u9fff\"'<>）)】\]`]+", re.IGNORECASE)


class LinkError(ValueError):
    """链接相关错误的基类，携带面向用户的中文提示与诊断分类。"""

    def __init__(self, message: str, hint: str = "", category: str = "", context: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.category = category or "unknown"
        self.context = context or {}


class InvalidURLError(LinkError):
    """输入为空、过长或不是合法的 http(s) 链接。"""


class UnsupportedPlatformError(LinkError):
    """链接合法但域名不在支持列表内。"""


@dataclass(frozen=True)
class Platform:
    key: str
    name: str
    domains: tuple[str, ...]
    icon: str = ""


# 精选白名单：覆盖国内外主流视频 / 音频平台，用于展示中文名与图标。
# 不在列表内的合法域名会走通用兜底（交给 yt-dlp 解析，它原生支持上千站点）。
SUPPORTED_PLATFORMS: tuple[Platform, ...] = (
    # —— 国内 ——
    Platform("bilibili", "哔哩哔哩", ("bilibili.com", "b23.tv", "bilibili.tv"), "📺"),
    Platform("douyin", "抖音", ("douyin.com", "iesdouyin.com"), "🎵"),
    Platform("kuaishou", "快手", ("kuaishou.com", "chenzhongtech.com", "gifshow.com"), "⚡"),
    Platform("xiaohongshu", "小红书", ("xiaohongshu.com", "xhslink.com"), "📕"),
    Platform("weibo", "微博", ("weibo.com", "weibo.cn", "video.weibo.com"), "🐦"),
    Platform("xigua", "西瓜视频", ("ixigua.com",), "🍉"),
    Platform("acfun", "AcFun", ("acfun.cn",), "🟢"),
    Platform("tencent", "腾讯视频", ("v.qq.com",), "🐧"),
    Platform("youku", "优酷", ("youku.com",), "🟠"),
    Platform("iqiyi", "爱奇艺", ("iqiyi.com", "iq.com", "iqy.net"), "🔴"),
    Platform("mgtv", "芒果TV", ("mgtv.com", "imgo.tv"), "🥭"),
    Platform("sohu", "搜狐视频", ("tv.sohu.com", "sohu.com"), "🐺"),
    Platform("zhihu", "知乎", ("zhihu.com",), "💡"),
    Platform("ximalaya", "喜马拉雅", ("ximalaya.com",), "🎧"),
    Platform("netease", "网易云音乐", ("music.163.com",), "🎼"),
    Platform("pearvideo", "梨视频", ("pearvideo.com",), "🍐"),
    Platform("haokan", "好看视频", ("haokan.baidu.com",), "🅱️"),
    Platform("pipix", "皮皮虾", ("pipix.com",), "🦐"),
    Platform("weishi", "微视", ("weishi.qq.com",), "🎬"),
    Platform("huya", "虎牙直播", ("huya.com",), "🎮"),
    Platform("douyu", "斗鱼直播", ("douyu.com",), "🐟"),
    Platform("yangshipin", "央视频", ("yangshipin.cn", "cntv.cn", "cctv.com"), "📡"),
    Platform("tudou", "土豆", ("tudou.com",), "🥔"),
    Platform("meipai", "美拍", ("meipai.com",), "📷"),
    Platform("nivod", "泥视频", ("nivod.vip",), "🎬"),
    Platform("pomo", "Pomo", ("pomo.mom",), "🎬"),
    # —— 国际 ——
    Platform("youtube", "YouTube", ("youtube.com", "youtu.be", "youtube-nocookie.com"), "▶️"),
    Platform("tiktok", "TikTok", ("tiktok.com", "vt.tiktok.com"), "🎵"),
    Platform("twitter", "X / Twitter", ("twitter.com", "x.com"), "🐦"),
    Platform("vimeo", "Vimeo", ("vimeo.com",), "🎥"),
    Platform("facebook", "Facebook", ("facebook.com", "fb.watch"), "📘"),
    Platform("instagram", "Instagram", ("instagram.com",), "📷"),
    Platform("dailymotion", "Dailymotion", ("dailymotion.com", "dai.ly"), "🎞️"),
    Platform("soundcloud", "SoundCloud", ("soundcloud.com",), "🎧"),
    Platform("twitch", "Twitch", ("twitch.tv",), "🎮"),
    Platform("rumble", "Rumble", ("rumble.com",), "🟡"),
    Platform("reddit", "Reddit", ("reddit.com", "v.redd.it"), "👽"),
    Platform("linkedin", "LinkedIn", ("linkedin.com",), "💼"),
    Platform("pinterest", "Pinterest", ("pinterest.com", "pin.it"), "📌"),
    Platform("vk", "VK", ("vk.com", "vkontakte.ru"), "🔷"),
    Platform("ok", "Odnoklassniki", ("ok.ru",), "🔵"),
    Platform("naver", "Naver TV", ("tv.naver.com", "naver.com"), "🟢"),
    Platform("kakao", "KakaoTV", ("tv.kakao.com",), "🟡"),
    Platform("nicovideo", "ニコニコ", ("nicovideo.jp",), "🎌"),
    Platform("ted", "TED", ("ted.com",), "🎓"),
    Platform("bbc", "BBC", ("bbc.co.uk", "bbc.com"), "🇬🇧"),
    Platform("rutube", "Rutube", ("rutube.ru",), "🔴"),
    Platform("mailru", "Mail.ru", ("my.mail.ru",), "📧"),
    Platform("bandcamp", "Bandcamp", ("bandcamp.com",), "🎵"),
    Platform("mixcloud", "Mixcloud", ("mixcloud.com",), "🎚️"),
    Platform("streamable", "Streamable", ("streamable.com",), "🎞️"),
    Platform("bitchute", "BitChute", ("bitchute.com",), "🟢"),
    Platform("odysee", "Odysee", ("odysee.com",), "🔶"),
    Platform("weverse", "Weverse", ("weverse.io",), "💜"),
    Platform("line", "LINE TV", ("tv.line.me",), "🟢"),
)

# 明显不是视频站点的域名，直接拦截并给出提示，避免无谓地请求 yt-dlp。
NON_VIDEO_HOSTS: tuple[str, ...] = (
    "google.com", "googleapis.com", "gstatic.com", "bing.com", "baidu.com",
    "wikipedia.org", "github.com", "gitlab.com", "stackoverflow.com",
    "amazon.com", "taobao.com", "jd.com", "tmall.com", "aliyun.com",
    "apple.com", "microsoft.com", "office.com", "live.com", "outlook.com",
    "qq.com", "mail.qq.com",  # qq.com 主体不是视频站，仅 v.qq.com 走腾讯视频
)


def _generic_platform(host: str) -> Platform:
    """兜底：未知合法域名也尝试下载，用主机名 + 地球图标标识。"""
    return Platform(key=host, name=host, domains=(host,), icon="🌐")


# 国内主流站点：直连即可访问，走海外代理反而会因跨境/节点问题超时或受限。
# 用于决定下载时是否绕开代理。
CHINA_DOMAINS: tuple[str, ...] = (
    "bilibili.com", "b23.tv", "bilibili.tv",
    "douyin.com", "iesdouyin.com",
    "kuaishou.com", "chenzhongtech.com", "gifshow.com",
    "xiaohongshu.com", "xhslink.com",
    "weibo.com", "weibo.cn", "video.weibo.com",
    "ixigua.com",
    "acfun.cn",
    "v.qq.com", "youku.com", "iqiyi.com", "iqy.net",
    "mgtv.com", "imgo.tv",
    "tv.sohu.com", "sohu.com",
    "zhihu.com",
    "ximalaya.com",
    "music.163.com",
    "pearvideo.com",
    "haokan.baidu.com",
    "pipix.com",
    "weishi.qq.com",
    "huya.com", "douyu.com",
    "yangshipin.cn", "cntv.cn", "cctv.com",
    "tudou.com", "meipai.com",
    "le.com", "wasu.cn", "1905.com",
    "chrqj.com",
)


def is_china_host(host: str) -> bool:
    """判断是否为国内站点（应直连、不走海外代理）。"""
    host = (host or "").lower()
    if host.endswith(".cn") or ".com.cn" in host or host.endswith(".com.cn"):
        return True
    return any(host == d or host.endswith(f".{d}") for d in CHINA_DOMAINS)



def extract_first_url(text: str) -> str:
    """从分享文案中挑出第一条 http(s) 链接。"""
    if not text or not text.strip():
        raise InvalidURLError("请输入视频链接", "把视频页面的地址粘贴到输入框即可")
    if len(text) > MAX_INPUT_LENGTH:
        raise InvalidURLError("输入内容过长", f"请控制在 {MAX_INPUT_LENGTH} 个字符以内")

    match = URL_PATTERN.search(text.strip())
    if not match:
        raise InvalidURLError(
            "没有识别到有效链接",
            "链接需要以 http:// 或 https:// 开头，例如 https://www.bilibili.com/video/BV1xx411c7mD",
        )
    return match.group(0).rstrip(".,;，。、")


def _hostname_of(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES or not parsed.netloc:
        raise InvalidURLError("链接格式不正确", "只支持 http:// 或 https:// 开头的网页地址")
    host = (parsed.hostname or "").lower()
    if not host:
        raise InvalidURLError("链接缺少域名", "请检查链接是否被截断")
    return host.removeprefix("www.").removeprefix("m.")


def _match_platform(host: str) -> Platform:
    """返回命中的平台；未命中时返回通用兜底（交给 yt-dlp 解析）。"""
    for platform in SUPPORTED_PLATFORMS:
        if any(host == domain or host.endswith(f".{domain}") for domain in platform.domains):
            return platform
    return _generic_platform(host)


def _normalize_url(url: str, host: str) -> str:
    """把平台特有的「移动端/短链」格式转成 yt-dlp 能识别的标准格式。"""
    # 腾讯视频：移动端 m.v.qq.com/x/m/play?vid=xxx → 桌面端 v.qq.com/x/page/xxx.html
    if host == "v.qq.com" and "/x/m/play" in url:
        m = re.search(r"[?&]vid=([a-zA-Z0-9]+)", url)
        if m:
            return f"https://v.qq.com/x/page/{m.group(1)}.html"
    return url


def parse_source(raw_input: str) -> tuple[str, Platform]:
    """校验输入并返回 (规范化链接, 命中的平台)。"""
    url = extract_first_url(raw_input)
    host = _hostname_of(url)
    if host in NON_VIDEO_HOSTS:
        raise UnsupportedPlatformError(
            f"该站点暂不支持视频下载：{host}",
            "请粘贴视频播放页链接（如 B 站、抖音、YouTube 等）",
        )
    platform = _match_platform(host)
    url = _normalize_url(url, host)
    return url, platform


def platform_catalog() -> list[dict[str, str]]:
    """给前端展示的平台清单。"""
    return [{"key": p.key, "name": p.name, "icon": p.icon} for p in SUPPORTED_PLATFORMS]
