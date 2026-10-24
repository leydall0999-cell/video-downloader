"""网盘文件源（cloud drives）解析器集合。

与现有视频平台提取器（yt-dlp / 各 platform extractor）并列的一类新数据源：
输入一个网盘链接 + 用户登录态，产出可直接交给 VDL 下载器的直链（dlink）。

设计原则：
- 每个网盘一个模块（baidu.py / aliyun.py ...），统一实现 `resolve(url, creds) -> list[CloudFile]`。
- 模块只负责「调网盘 API 拿直链 + 描述下载所需 cookie/headers」，真正的下载交给
  VDL 现有下载器（server/downloader.py 已支持 per-task cookie 透传，且对百度 dlink 的
  CDN host 不会强加 Referer）。
- 纯函数、不 import app，便于离线单测（用 mock 替换 _api_get 即可）。
"""
