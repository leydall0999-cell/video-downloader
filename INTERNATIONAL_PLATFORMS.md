# 国际流媒体平台支持矩阵（2026-08-22 盘点）

> 基于 yt-dlp 2026.7.4 静态 extractor 探测 + VPS/线上实解析抽测。
> VDL 架构说明：白名单平台识别/显示中文名；**任意 yt-dlp 有 extractor 的站点即使不在
> 白名单也会走「通用兜底」解析**（平台名显示为裸域名）。国际站走海外代理分流
> （VDL_PROXY > macOS 系统代理 > 环境变量；Railway 机房在海外，多数国际站可直连）。

## ✅ 第一类：可直接解析下载（yt-dlp 原生 extractor + 白名单已加）

| 平台 | 域名 | 说明 |
|---|---|---|
| YouTube | youtube.com / youtu.be | VDL 已有自动降级链路（免Cookie PO Token → Cookie源），最稳 |
| TikTok | tiktok.com / vt.tiktok.com | 国际版，已支持 |
| Twitch | twitch.tv | 直播需正在开播；视频/回放/Clip 可解析 |
| Kick | kick.com | 直播需正在开播；VOD/Clip 可解析（实测 extractor 生效） |
| Vimeo / Dailymotion | vimeo.com / dailymotion.com | 已支持 |
| Facebook / Instagram | facebook.com / instagram.com | 已支持（部分内容需登录） |
| Rumble | rumble.com | ⚠️ 数据中心 IP 偶发 403（反爬），可重试/换代理 |
| Tubi（2026-08-22 新增） | tubitv.com | 免费 AVOD 无 DRM，专用解析已就绪（页面 __data→HLS 直链）；云部署数据中心 IP 被反爬（2.8KB 壳页），配美/加/澳/新/英/墨住宅代理即可打通 |
| Odysee / BitChute | odysee.com / bitchute.com | ✅ BitChute 2026-08-22 已实测打通（yt-dlp 直连，修复无 height 直链漏选） |
| VEVO | vevo.com | 音乐 MV（YouTube 托管） |
| CuriosityStream | curiositystream.com | 免费片段可下，完整需订阅 |
| Pluto TV | pluto.tv | ⚠️ 偶发 429 限流，重试即可 |
| CBC Gem（加拿大） | gem.cbc.ca | 已支持 |
| ITVX（英国） | itv.com | 需英国可访问网络 |
| Arte.tv（德法） | arte.tv | 免费公开，已支持 |
| France.tv / franceinfo（法国） | france.tv / francetvinfo.fr | ⚠️ 数据中心 IP 偶发反爬失败 |
| RAI Play（意大利） | raiplay.it | 免费公开，已支持 |
| SVT Play（瑞典） | svtplay.se | 免费公开 |
| NRK TV（挪威） | nrk.no | 免费公开 |
| Yle Areena（芬兰） | areena.yle.fi | ⚠️ 数据中心 IP 偶发 403 |
| RTVE Play（西班牙） | rtve.es | 免费公开 |
| Niconico（日本） | nicovideo.jp | 已支持 |
| TVer（日本） | tver.jp | 免费公开 |
| CHZZK（韩国） | chzzk.naver.com | 直播/视频，部分需登录 |
| SonyLIV（印度） | sonyliv.com | 免费内容可下 |
| Disney+ Hotstar（印度） | hotstar.com | 免费内容可下 |
| iflix / Viu / meWATCH（东南亚） | iflix.com / viu.com / mewatch.sg | 已支持 |
| IVI / KinoPoisk（俄罗斯） | ivi.ru / kinopoisk.ru | 已支持 |
| ABC iview / SBS（澳洲） | iview.abc.net.au / sbs.com.au | 免费公开 |
| F1 TV | f1tv.com | 赛事片段/VOD 可解析（正式直播需订阅） |
| Red Bull TV | redbull.tv | 免费公开，已支持 |
| AfreecaTV（韩国） | afreecatv.com | 直播/回放，部分需登录 |
| Nebula | nebula.tv | ⚠️ 需账号 Cookie（注册用户） |
| PeerTube | 各实例 | 去中心化，公开实例可下 |
| Snapchat（Spotlight） | snapchat.com | 仅 Spotlight 公开视频 |
| BIGO LIVE | bigo.tv | 直播/回放 |
| Twitter/X、Reddit、VK、LinkedIn、TED 等 | — | 原有白名单，已支持 |

## 🔑 第二类：需登录 Cookie 或地区 IP（粘贴 Cookie / 配置代理后可用）

| 平台 | 说明 |
|---|---|
| BBC iPlayer（英国） | 需**英国 IP + 账号**，双重门槛 |
| NRK / SVT / RAI / RTVE 等北欧公共台 | 大部分内容地区锁（仅本国 IP），海外数据中心 IP 返回"不可用"/403（2026-08-22 实测 NRK 报 Ikke tilgjengelig lenger、SVT 404 过期链接、Yle 403） |
| France.tv / Yle | 部分内容**全球公开**（实测中国 IP 可完整解析 1600s+），数据中心 IP 偶发反爬 |
| Nebula | 需账号 Cookie（`--cookies`） |
| CuriosityStream 完整内容 | 需订阅账号 |
| CHZZK 部分内容 | 需 Naver 登录 |
| Facebook / Instagram 部分 | 私密/未公开内容需登录 |
| Snapchat / SonyLIV / Hotstar 部分 | 视内容权限 |

> 用法：在「高级选项 → Cookie」粘贴该站浏览器 Cookie 后重试；BBC/北欧台等地区站还需代理 IP 落在对应地区。
> 2026-08-22 实测：Channel 4 返回 KnownDRMIE（DRM）→ 已归入第三类；Yle 在中国 VPS IP 可解析、在美国数据中心 IP 403，地区/反爬敏感。

## 🚫 第三类：DRM / 订阅付费墙（合规红线，不支持破解）

yt-dlp 对这些站点仅返回 `KnownDRMIE`（Unsupported 提示），正片受 Widevine/PlayReady
DRM 加密，**破解 DRM 违反合规原则，明确不支持**：

Netflix · Disney+ · Prime Video · Apple TV+ · Max (HBO) · Paramount+ · Hulu ·
Peacock · Crunchyroll · Mubi · Rakuten Viki · Crackle · STARZ · AMC+ · ESPN+ ·
RTL+ · U-NEXT · FOD · ZEE5 · TVNZ+ · CTV · Globoplay · JioCinema · TVING ·
Sling TV · FuboTV · YouTube TV · Acorn TV · **Channel 4（2026-08-22 实测 KnownDRMIE）** ·
Criterion Channel · Crave

> 这些平台已加入白名单（前端显示平台名），粘贴链接后返回明确的 DRM 提示，而非笼统错误。

## ❌ 第四类：yt-dlp 无 extractor（暂不支持，需自行开发或无法）

DAZN · Plex · Kanopy · Popcornflix · The Roku Channel · Criterion Channel ·
My5 · BritBox · Now TV · Movistar+ · iVysilani · Pro TV Plus · Antena Play ·
Hulu Japan · Watcha · ALTBalaji · YuppTV · Kwai · SnackVideo · more.tv ·
Claro TV+ · 13Go · ThreeNow · Showmax · beIN CONNECT · Turkcell TV+ · Vudu ·
Amazon Freevee（amazon.com 域被非视频站拦截）

> 2026-08-22 复核修正：AfreecaTV / Red Bull TV / F1 TV / PeerTube / Niconico /
> SonyLIV / iflix / meWATCH / KinoPoisk / CHZZK 其实都有 yt-dlp extractor（已补白名单或走通用兜底）；
> Channel 4 / Criterion / Acorn 归入 DRM 类。
>
> **DAZN 深度评估（2026-08-22，用户点名重点）**：免费方案存在（注册账号看精选
> 直播/回放），但播放走 `authentication-prod.ar.indazn.com/v1/authenticate` 认证 +
> DASH(.mpd) + **Widevine DRM** license 管线；VPS 中国 IP 与 Railway 数据中心 IP
> 均 403 地区封锁（仅住宅 IP 实测可访问）。→ **VPS worker 不可行**（物理地区封锁）
> + 合规不破解 Widevine。已加白名单识别（`3a09736`），解析/下载返回明确提示。
> 同类付费服务（Showmax / beIN / Turkcell / Movistar+ 等）大概率同构（账号+DRM+
> 地区）；无 DRM 的免费 AVOD（Tubi / Popcornflix 等）理论上可开发 worker，但
> VPS 中国 IP 同样受地区/反爬限制，需逐个实测。

## 使用提示
1. **网络**：Railway 部署机房在海外，多数国际站可直连；本地部署需配 `VDL_PROXY`
   海外代理（详见 CN_PROXY_SETUP.md）。
2. **Cookie**：付费/登录墙内容粘贴浏览器 Cookie 到「高级选项 → Cookie」。
3. **地区**：BBC/France.tv 等地区限制内容需代理 IP 落在对应国家。
4. **反爬**：Rumble/Pluto/Yle 等偶发 403/429/403，重试或稍后再试。
5. 本矩阵基于 2026-08-22 探测，平台改版后能力可能变化。
