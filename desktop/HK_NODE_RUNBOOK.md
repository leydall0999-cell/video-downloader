# 香港节点切换手册（阿里云/腾讯云香港轻量 替代 Railway）

> 2026-09-11 备货预案。触发条件：阿里香港轻量 2核2G 补货（或改买腾讯锐驰型）。
> 目标：`hanyuxz.top` 从已死的 Railway 切到香港机，大陆用户 220ms → 30-50ms，egress 费归零，免备案不变。

## 0. 购置参数（哪个有货买哪个）

| 项 | 阿里云轻量（首选） | 腾讯云轻量（备选） |
|---|---|---|
| 入口 | `https://swasnext.console.aliyun.com/buy?regionId=cn-hongkong` | `https://cloud.tencent.com/product/lighthouse` |
| 地域 | 中国香港 | 中国香港 |
| 套餐 | 2核2G / 40G SSD / 200M峰值 不限流量（¥24-39/月） | 锐驰型 2核2G 200Mbps（¥40-55/月）；入门型 20M/500G 勉强够冷启动 |
| 镜像 | Ubuntu 22.04 系统镜像（纯净，勿选宝塔/Docker 应用镜像） | 同左 |
| 时长 | 1 个月试水 | 1 个月 |

## 1. 部署（一条命令，约 10-15 分钟）

```bash
ssh-copy-id root@<IP>                      # 首次：输密码装公钥
bash desktop/deploy_hk.sh root@<IP>        # 部署 + 冒烟
# 验证: curl http://<IP>:8888/api/version
```

脚本做的事：swap 2G → `git archive web-dev` 传源码（不受本地 app-dev 工作树影响）→
Docker 构建（**含 bgutil PO token server**，香港数据中心 IP 访问 YouTube 必需）→
`docker run -e VDL_REGION=global -p 8888:8888 --restart unless-stopped`。

## 2. 域名切换（需你在 CF dashboard 操作，约 2 分钟）

1. Cloudflare → `hanyuxz.top` → DNS → A 记录 `@` 改为 **香港机 IP**；
2. **代理状态改为「仅 DNS」灰云**——关键！橙云会让大陆用户仍绕 CF 海外边缘（220ms），提速归零；
3. 然后跑 `bash desktop/deploy_hk.sh root@<IP> hanyuxz.top` 装 Caddy（自动签 TLS）；
4. 防火墙/轻量控制台放行 **80、443、8888、22**。

## 3. 代码侧改动（切域名当天，web-dev 分支）

- `web/app.js` 里 `UC_UPLOAD_ENDPOINTS = [location.origin, 'https://web-production-b9993.up.railway.app']`
  ——删掉 railway 兜底（或换成香港机域名），commit 后**两个节点都要重新部署**（广州跑 `run.sh` 的照常 rsync）；
- 广州节点 `/opt/vdl-worker` 同步最新 web-dev（保持 `VDL_REGION=cn` 不变）。

## 4. 验收清单

- [ ] `dig +short hanyuxz.top` → 香港机 IP（无 CF 段 IP = 灰云生效）
- [ ] 本机 `curl -w "%{time_connect}"` → TCP 连接 **<0.08s**（旧基线 0.17-0.22s）
- [ ] `https://hanyuxz.top/api/version` 200；首页/app.js 正常
- [ ] **真实下载一条 YouTube 链接**（验证 bgutil PO token 生效，无 "Sign in to confirm you're not a bot"）
- [ ] 晚高峰 21:00 复测延迟/丢包/下载吞吐（阿里国际 BGP 的赌点就在这）
- [ ] `/api/nodes` 两节点各报 `region=global` / `region=cn`

## 5. 收尾与回滚

- **Railway 退役**：不再续费即可（应用已删，无残留依赖——`UC_UPLOAD_ENDPOINTS` 改完后）；
- `cn_tunnel_client` 保持 `disabled`（回 Railway 时才 `systemctl enable --now`）；
- 广州机上之前装的 `cloudflared`（CF Tunnel 备用方案，已作废）：可留可卸，`/usr/local/bin/cloudflared` 删除即净；
- **回滚**：CF DNS A 记录改回/改指广州 `8.138.223.3`，入口退化为 `http://IP:8888`（CF 不代理非标端口，橙云方案对 8888 不可用）。

## 6. 已知风险

- 阿里香港是国际 BGP 非 CN2 GIA，**晚高峰质量待实测**（验收第 6 条）；
- 2核2G 并发转码能力有限，不够再升 2核4G；
- 版权投诉会到云厂商（香港机房同样受理），与 Railway 无本质差异。
