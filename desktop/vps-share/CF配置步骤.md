# Cloudflare 一次性配置（约 2 分钟）

**目的**：让 `https://share.hanyuxz.top` 指向你的服务器 `8.138.223.3:8888`，
这样手机（微信 / QQ / 浏览器）扫码后就能打开分享页。

---

## 第 1 步：加一条 DNS 记录

1. 打开 <https://dash.cloudflare.com> 并登录
2. 点进域名 **hanyuxz.top**
3. 左侧菜单点 **DNS** → **记录 / Records**
4. 点 **添加记录 / Add record**，按下面填：

   | 字段 | 填什么 |
   |---|---|
   | 类型 Type | `A` |
   | 名称 Name | `share` |
   | IPv4 地址 | `8.138.223.3` |
   | 代理状态 Proxy status | **「已代理 / Proxied」—— 橙色云朵要点亮 ☁️** |

5. 点 **保存 / Save**

> ⚠️ 代理状态必须是橙色云（Proxied）。选成灰色云（DNS only）会失败：
> 因为服务器 80/443 端口没有开放，只有 Cloudflare 代理模式才能提供 HTTPS。

---

## 第 2 步：加一条「源站规则」（关键，不能省）

**为什么需要**：这台服务器的安全组只开放了 `8888` 端口，`80`/`443` 不通。
Cloudflare 默认回源到 `443`，会打不开，必须让它改连 `8888`。

1. 左侧菜单点 **规则 / Rules** → **源站规则 / Origin Rules**
2. 点 **创建规则 / Create rule**
3. 规则名称：随便填，例如 `share-origin-8888`
4. **传入请求匹配 / If incoming requests match**：
   - 字段 Field：`主机名 / Hostname`
   - 运算符 Operator：`等于 / equals`
   - 值 Value：`share.hanyuxz.top`

   （若界面是表达式编辑器，直接粘：`http.host eq "share.hanyuxz.top"`）

5. **然后 / Then** 选择 **重写到… / Rewrite to…**
   - **目标端口 / Destination Port** 填 `8888`
   - Host 头保持默认，**不要改**
6. 点 **部署 / Deploy**

---

## 完成标志

浏览器打开 <https://share.hanyuxz.top/healthz>，应看到：

```json
{"ok": true, "service": "vdl-share", "files": 0, "bytes": 0}
```

看到这行就说明域名链路通了，告诉 AI 一声即可，剩下的它会自动验证。

---

## 打不开时怎么排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `522` / `521` | 回源没连上 | 检查第 2 步的目标端口是否为 `8888`；确认规则已「部署」 |
| `1016` / DNS 报错 | 记录没生效 | 检查第 1 步是否保存成功、云朵是否为橙色 |
| `404` 且响应头带 `x-railway-fallback` | 还指着旧的 Railway | 说明 DNS 记录没改到 `8.138.223.3`（可能加了同名记录） |
| 浏览器提示证书错误 | 代理被关掉了 | 把云朵改回橙色（Proxied） |

---

## 附：为什么不直接用 IP 或 http 域名

- **纯 IP 链接**：微信扫描会直接拦截，打不开
- **http://**：微信内打开会被提示风险，且视频可能无法播放
- **不开代理（灰云）**：服务器 80/443 不通，签不了 HTTPS 证书
- 所以：**域名 + 橙色云代理 + 回源改 8888**，是目前这套环境下唯一可行的组合
