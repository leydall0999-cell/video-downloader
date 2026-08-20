# 爱奇艺长期稳定下载方案（Cookie + yt-dlp 直下）

## 两种解析方式对比

| 方式 | 适用链接 | 原理 | 稳定性 |
|---|---|---|---|
| **VPS 真实浏览器 worker（默认）** | `playShare.html?shareId=...` 分享页；或任意 iqiyi 链接（未贴 Cookie 时） | Railway 调国内 VPS 起 Chromium 渲染分享页、捕获 m3u8 直链 | 受爱奇艺反爬对抗影响，可能偶发失败 / 超时 |
| **Cookie + yt-dlp 直下（长期稳定）** | `v_xxx.html` 播放页长链 **+ 已粘贴登录 Cookie** | 跳过 VPS worker，直接用 yt-dlp 的 IqiyiIE 带上你的 Cookie 拉流 | 不受 worker 反爬影响；VIP Cookie 也能生效 |

> 代码已支持：当用户在「高级选项」粘贴了 Cookie、且链接是 `v_xxx.html`（非 playShare 分享页）时，自动跳过 VPS worker，回退 yt-dlp 直下。

## 如何走「长期稳定」路径（step by step）

1. **拿到爱奇艺登录 Cookie**
   - 电脑浏览器（Chrome / Edge）登录 iqiyi.com。
   - F12 → Application → Cookies → `iqiyi.com` → 复制整列 Cookie 值；或 Console 输入 `document.cookie` 复制整串。
   - VIP 用户：保持会员订阅有效，Cookie 才带 VIP 权益。

2. **粘贴到网页版「高级选项 → 会话 Cookie」**
   - 打开 hanyuxz.top，点「高级选项」，把上面复制的串粘进「会话 Cookie」框。
   - 默认勾选「贡献 Cookie 到公共池」——想只自己用就取消勾选（仅本次请求，不入池）。

3. **用 `v_xxx.html` 播放页长链，不要分享页短链**
   - ✅ 正确：`https://www.iqiyi.com/v_19rxxxx.html`（浏览器地址栏里播放页的 URL）。
   - ❌ 错误：`playShare.html?shareId=...` 分享链接——它是纯 JS SPA，yt-dlp 即使有 Cookie 也解析不出，必须走 VPS worker。
   - 从分享链接取长链：点开分享链接 → 等播放器加载 → 复制地址栏里变成 `v_xxx.html` 的 URL。

4. 粘贴链接解析 → 选清晰度 → 下载。此时走的是 yt-dlp + 你的 Cookie，与 VPS worker 无关。

## 为什么分享页（playShare）仍要走 worker

`playShare.html` 是前端 SPA，视频 tvid 由 JS 异步渲染，yt-dlp 的 IqiyiIE 抓不到。所以分享页只能靠 VPS 真实浏览器渲染后捕获 m3u8，这条路受爱奇艺升级检测影响会偶发失败——遇到就稍后重试，或改用上面的「长链 + Cookie」方式。

## 故障排查

- **仍走 worker 报错**：确认链接是 `v_xxx.html` 而非 `playShare.html`；确认「高级选项」Cookie 已填且是 iqiyi 的（不是其他站）。
- **解析报登录 / 地区限制**：Cookie 失效，或不是会员却选了会员清晰度 → 重新复制 Cookie / 选免费清晰度。
- **想完全不依赖 VPS**：只用 `v_xxx.html + Cookie` 方式即可，VPS worker 仅对分享页必要。

## 给开发端

- 代码位置：`server/downloader.py::_iqiyi_info`（新增 `cookie` 参数；`cookie and "playShare" not in url` 时跳过 worker 回退 yt-dlp）。
- 调用点：`probe()`（约 1915 行）与 `_run_once()`（约 2496 行）均透传 `cookie`。
- 边界：playShare 链接即使带 Cookie 仍走 worker（SPA 无法被 yt-dlp 直解）；无 Cookie 的非分享页保持原 worker 行为（向后兼容）。
