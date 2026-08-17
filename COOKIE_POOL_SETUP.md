# chrqj 公共 Cookie 池 · 部署与运维手册

> 场景：网页版（部署在 Railway 等服务器）解析 chrqj.com 弹 **403**。
> 根因：chrqj 视频流会校验**播放页会话 Cookie**，服务器没有你的浏览器，自动 Cookie 读不到 → 被拒。
> 解决：建一个「公共 Cookie 池」，由开发端 / App 端把登录态喂进去，访客**零操作**即可复用。

---

## 一、架构与隔离边界

chrqj 的 Cookie 有三层来源，优先级从高到低：

| 优先级 | 来源 | 谁来提供 | 说明 |
|---|---|---|---|
| 1 | 环境变量 `CHRQJ_COOKIE` | 开发端 | 开发端托管一个共享免费账号 Cookie，最稳 |
| 2 | 公共 Cookie 池 | 开发端 / App 端上报 | 本文档主角，让普通访客无感 |
| 3 | 个人本机缓存 | 本机浏览器 | 仅桌面版本机能用 |
| 4 | 读取本机浏览器 | 本机 | 仅桌面版本机能用 |

**重要隔离**：公共池（`server/cookie_pool.py`）与「个人本机缓存」（`server/cookie_cache.py`）**物理分离、互不影响**。
后者原则是「仅本机、不外传、不跨用户」，本功能**完全不碰它**——公共池走独立的 `~/.videodownloader/cookie_pool/` 目录，专门存用户**自愿上报、知情同意**的指定站点登录态。

---

## 二、环境变量一览

在 Railway 项目 `radiant-art/web` 的环境变量里按需配置（都不强制，不设也能跑，只是少了加固/告警）：

| 变量 | 作用 | 建议 |
|---|---|---|
| `CHRQJ_COOKIE` | 开发端托管的共享 Cookie，纯 `a=b; c=d` 串（**不带** `Cookie:` 前缀）。优先级最高 | 想「别人拿来就能下」就设它，最省心 |
| `VDL_COOKIE_ENC_KEY` | Fernet key，加密公共池存储。不设则降级为 `chmod 600` 明文 | 生产环境建议设（用 `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` 生成） |
| `VDL_COOKIE_ALERT_WEBHOOK` | 公共池空了/全失效时，向该 URL `POST` 告警 JSON（企业微信/飞书/钉钉机器人） | 想被动变主动就设 |
| `VDL_COOKIE_SYNC_TOKEN` | 通用上报接口 `/api/cookie/sync` 的客户端令牌。**App 走 `from-local` 不需要它** | 仅当你要对外提供通用上报时才设 |
| `VDL_COOKIE_POOL_DOMAINS` | 公共池白名单扩展（逗号/分号分隔，如 `douyin.com;example.com`）。不设则默认 = 下载层「强反爬域名清单」+ chrqj | 要支持额外站点时设 |
| `VDL_COOKIE_POOL_TEST_URLS` | 通用验真的测试 URL（`domain=url` 分号分隔，如 `mysite.io=https://mysite.io/v/1`）。没配的站验真会「放行」而非验真 | 要用 yt-dlp 通用验真时设 |

> 探测周期固定 **30 分钟**一轮，Cookie 有效期固定 **30 天**，均在代码内（无需环境变量调）。

---

## 三、部署步骤

> **快速生成环境变量**：本机跑 `gen_cookie_env.py` 一键产出 `CHRQJ_COOKIE` + `VDL_COOKIE_ENC_KEY`，
> 自动从浏览器读 chrqj 登录态并验真，复制即用：
> ```bash
> .build_venv/bin/python gen_cookie_env.py          # 自动读浏览器
> .build_venv/bin/python gen_cookie_env.py --cookie-file cookies.txt   # 或从文件读
> ```

功能已在 commit `4d44f69`（env 兜底）+ `2b4d334`（公共池）中实现，并已推送到 `main`。

1. **GitHub 已是最新**（`origin/main` 已含上述提交，Railway 自动部署即可生效）。
2. 去 Railway 控制台 → `radiant-art` → `web` 服务 → **Variables** 按需加环境变量：
   - 至少要 `CHRQJ_COOKIE`（开发端免费账号 Cookie），其余可选。
3. 改完环境变量点 **Deploy** 重新部署一次，让改动生效。
4. 部署后用 chrqj 链接在网页版试解析，应不再 403。

---

## 四、App 端「同步 Cookie 到云端」按钮（推荐喂池方式）

桌面版（本机运行）里：

1. 打开桌面 App，登录过 chrqj.com 的浏览器保持登录状态。
2. 点 **「同步 Cookie 到云端」** 按钮。
3. 弹窗 **知情同意**（说明将把登录态上传到公共服务），点确认才上传。
4. 后端读本机浏览器 chrqj 登录态 → 验真（无效拒）→ 入公共池。

> 该接口是 `/api/cookie/sync/from-local`，**仅本机来源（127.0.0.1）可调用**，公网调用一律 403，安全。
> App 自己的下载逻辑完全不变，只是多了一个「顺手喂池」的附加动作。

---

## 五、网页端高级选项（兜底手动方式）

如果公共池为空、且没设 `CHRQJ_COOKIE`，网页访客仍可在 **「高级选项」里粘贴自己的 chrqj Cookie**，
系统会持久化到容器 `~/.videodownloader/cookies/www.chrqj.com.json` 供提取器读取。
⚠️ Railway 重新部署会清空容器，需重新粘贴（不如公共池/环境变量持久）。

---

## 六、后台探测与告警

- 进程启动后开一个 daemon 线程，**每 30 分钟**对公共池里每个白名单域调用 `verify_and_prune`：
  - 用该 Cookie 真调一次 chrqj 签名接口；**明确无效**才剔除（`None`=网络不可达则保留，避免误删）。
- 若某域**有效数为 0**（空了或全失效），触发 `_cookie_pool_alert`：
  - 写日志 `WARNING`；
  - 写状态文件 `~/.videodownloader/cookie_pool_alert.json`；
  - 若设了 `VDL_COOKIE_ALERT_WEBHOOK`，同时 `POST` 告警到该机器人。

这样 Cookie 过期不再是「悄悄失效、全员再 403 你还不知道」。

---

## 七、运维清单

| 现象 | 处理 |
|---|---|
| 网页版 chrqj 又 403 | 先看 `cookie_pool_alert.json` 是否有告警；有则更新 `CHRQJ_COOKIE` 或重点 App「同步」 |
| 想换共享账号 | 改 `CHRQJ_COOKIE` 环境变量 → Redeploy |
| 想增强存储加密 | 设 `VDL_COOKIE_ENC_KEY`（Fernet key） → Redeploy |
| 想接告警到群 | 设 `VDL_COOKIE_ALERT_WEBHOOK` 为机器人 webhook → Redeploy |
| 某用户上报垃圾/失效 Cookie | `verify_cookie` 验真失败直接拒，进不了池；进了也会在 30 分钟内被探测剔除 |

---

## 八、合规与风险边界

- 仅收**白名单域**（默认 = 下载层「强反爬域名清单」`_COOKIE_HARDENED_DOMAINS` + chrqj，可用 `VDL_COOKIE_POOL_DOMAINS` 扩展），**绝不收清单之外的网站 Cookie**。
- 公共池默认只下**免费清晰度**（提取器优先免登录清晰度），不主动暴露会员资源，降低账号被封/盗用风险。
- App 端上报**必须用户知情同意**，绝不静默上传。
- 守住 VDL 红线：不破解付费墙 / DRM / 付费 VIP 内容；免费资源共享一般无碍，请勿用它下明确付费内容。
- 残余风险：共享登录态在极端情况下可能被源站风控（聚合站通常弱）；Cookie 必过期，靠上面运维手段兜底。

---

## 九、如何复用到其他平台

公共池已通用化，加站不再需要改「存储/加密/限频/知情同意/探测」这些框架，只需两步：

1. **放行域名**（三选一，任选即可）：
   - 该站已在下载层 `_COOKIE_HARDENED_DOMAINS` 清单里（douyin/快手/小红书/bilibili/v.qq 等）→ **自动放行**；
   - 或设环境变量 `VDL_COOKIE_POOL_DOMAINS=新域名`；
   - 或在 `server/cookie_pool.py` 的 `_BASE_DOMAINS` 加一条。
2. **验真**（可选，但建议）：
   - 有 yt-dlp 内置提取器的站 → 设 `VDL_COOKIE_POOL_TEST_URLS=域名=该站任意视频页URL`，用通用验真；
   - 接口签名特殊的聚合站（像 chrqj）→ 在 `cookie_pool.py` 加一个 `verify_xxx` 并挂进 `verify_cookie` 分发。

> 注意：内置主流站（douyin/快手等）在 Railway 上的 403 通常还有**海外 IP 地域墙**因素，公共池喂 Cookie 之外还需 `VDL_PROXY_CN` 国内代理配合。
