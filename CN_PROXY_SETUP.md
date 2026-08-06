# 国内站回源代理 · 实操手册

> 场景：video-downloader 部署在**海外**（Railway / Fly.io 等），但想下 **B站 / 抖音 / 腾讯** 等国内站。
> 这些站在海外 IP 会被地理围栏拦截（解析/下载报「该地区不可播放」）。
> 解决方案：在**物理位于国内**的机器上跑一个正向代理，Railway 用 `VDL_PROXY_CN` 指向它，
> 让国内站的请求从国内 IP 出去。

代码侧早已支持，无需改代码——只差一个「公网可达的国内出口」。本仓库附带的
[`cn_proxy.py`](./cn_proxy.py) 是一个**零依赖**（仅 Python 标准库）的 HTTP/HTTPS 正向代理，
可直接跑在国内机器上充当这个出口。

---

## 一、代理本身（国内机器上跑）

```bash
# 国内机器（有公网 IP 的 VPS，或你这台在国内、经隧道暴露的 Mac）
python3 cn_proxy.py 18888        # 监听 0.0.0.0:18888
```

- 支持 HTTP(GET/POST) 与 HTTPS(`CONNECT` 隧道) 两种转发。
- 仅用标准库，无需 `pip install`。
- 生产环境**务必加认证 / 限制来源 IP**（见第四节），裸奔代理会被扫爆。

---

## 二、三种「让 Railway 够得着」的方式（按推荐度）

### ✅ A. 国内 VPS（最稳，推荐）
买阿里云/腾讯云轻量（国内地域，约 ¥60–100/年），公网 IP 直连，无需隧道。

1. 上传 `cn_proxy.py` 到 VPS，`python3 cn_proxy.py 18888` 后台常驻。
2. 安全组放行 `18888`（建议同时限制来源 IP 为 Railway 出口，或加代理认证）。
3. Railway 环境变量加：
   ```
   VDL_PROXY_CN=http://<VPS公网IP>:18888
   ```
（README 里也给了 `gogost/gost` Docker 版，二选一。）

### ⚠️ B. 本机隧道（零月费，但需绑卡 + 本机常开）
免费隧道服务**几乎都不透传 `CONNECT`**（HTTP 代理访问 HTTPS 目标必须用它），实测结论：

| 工具 | 结论 |
| --- | --- |
| `ngrok http 8899` | ❌ HTTP 隧道不透传 CONNECT，当代理用会被立即拒绝 |
| `ngrok tcp 8899` | ✅ 原始 TCP 转发，可用；但**免费版需绑信用卡**（不扣费）|
| `cloudflared tunnel --url http://localhost:8899` | ❌ 同上，不透传 CONNECT |
| `localhost.run` SSH 反向隧道 | ❌ 免费无密钥档 TCP 转发地址不明确 / 不可用 |

**ngrok tcp 可用路径**（本机需常开、ngrok 进程不能断）：
```bash
ngrok tcp 8899          # 得到 tcp://x.tcp.ngrok.io:PORT
```
Railway 设：
```
VDL_PROXY_CN=http://x.tcp.ngrok.io:PORT
```

> 本机 Mac 在国内、直连 B站 正常时，`VDL_PROXY_CN` 指向本机 `127.0.0.1:8899` 即可本地验证
> （无需隧道）。要上 Railway，必须换成公网可达地址。

### ✅ C. 你已有国内 socks5/http 出口
直接 Railway 设：
```
VDL_PROXY_CN=http://user:pass@host:port
# 或 socks5://user:pass@host:port
```

---

## 三、已验证（本机实测）

- 本机服务设 `VDL_PROXY_CN=http://127.0.0.1:8899` 后，解析 B站 → 代理日志确认真实透传：
  ```
  [cn_proxy] #1 CONNECT www.bilibili.com:443
  [cn_proxy] #2 CONNECT api.bilibili.com:443
  ```
- 真实下载 B站 mp4（20.5MB）成功，「路由分流 + 代理」整条链 100% 可用。
- 海外站（YouTube）仍走 `VDL_PROXY`/系统代理，与国内站互不串。

→ **代码 + 代理链路已证明可用，只差「公网可达的国内出口」这一步。**

---

## 四、安全提醒（重要）

- 公网暴露的代理是**开放中继**，务必三选一加固：
  1. 限制来源 IP（VPS 安全组只允许 Railway 出口）；
  2. 加代理认证（`cn_proxy.py` 当前未内置，可自加 token 校验，或用 gost 的 `user:pass@`）；
  3. 仅用 ngrok/cloudflared 的随机隧道 URL（URL 即密码，但不稳定）。
- 该代理**仅供本服务回源国内站点**，不要对外提供跨境代理服务。
- `cn_proxy.py` 默认无认证，仅建议在「随机隧道 URL / 已限制来源 IP」场景下使用。

---

## 五、验证上线

Railway 加好 `VDL_PROXY_CN` 并重新部署后，硬刷新站点，贴一个 B站链接解析：
- 不再报「该地区不可播放」即成功；
- 仍报地区限制 → 检查 `VDL_PROXY_CN` 是否公网可达、代理是否在国内机器上运行、端口是否放行。
