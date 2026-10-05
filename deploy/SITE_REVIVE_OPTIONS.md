# hanyuxz.top 复活方案与节点实测结论（2026-09-14 00:20）

## 一、当前状态（均为实测，非推断）

| 项目 | 实测结果 | 证据 |
|---|---|---|
| `hanyuxz.top` | ❌ **死的** | `GET https://hanyuxz.top/api/version` → `404 {"message":"Application not found"}`（Railway 边缘返回） |
| 域名 DNS | NS 在 **Cloudflare**（`jewel/aaron.ns.cloudflare.com`）；`@` 的 A 记录是**橙云代理 IP**（104.21.77.11 / 172.67.203.21）→ 源站指向**已死的 Railway** | `dig @223.5.5.5` |
| 广州节点 `8.138.223.3` | ✅ **完全健康** | `vdl-web` active；`http://8.138.223.3:8888/api/version` → **200**（公网 + 本地双确认）；`cn_proxy` / `vdl-cookie` / `vdl-update` 全 active；负载 0.2，磁盘 43% |
| `cn_tunnel_client` | ⏹ **已由 AI 停止并禁用**（上游已死，5 秒一次 `HTTP 404` 重连风暴，纯浪费） | `systemctl is-active` → inactive / disabled（可逆） |

## 二、三条硬边界（决定了可行路线，别再重复试错）

1. **广州节点出网 Cloudflare 443 被封。** `api.cloudflare.com` / `www.cloudflare.com` / `api.trycloudflare.com` 全部超时。
   → `cloudflared tunnel --url`（quick tunnel）**实弹失败**：`Post https://api.trycloudflare.com/tunnel: context deadline exceeded`。
   → `cloudflared tunnel login` 也不可用，**拿不到 `cert.pem`**。
2. **但 Tunnel 数据面 TCP 7844 可通**（`region1/2.v2.argotunnel.com`，IPv6）。
   → 若走隧道，**只能用「Zero Trust 后台建隧道 + token 模式」**，配置经边缘控制流下发，不依赖本机访问 443。
3. **阿里云安全组只放行了既有业务端口。** check-host.net 第三方多节点实测：
   - `8888` → **8/8 节点可达**（基准对照，方法有效）
   - `8080` → **0/8 节点可达**
   - 节点本机无 `ufw`、`iptables` DROP 规则为 0 → 拒绝发生在**安全组**层。
   → 橙云代理（源站端口仅支持 80/8080/8880/2052/2082/2086/2095）**必须先改安全组**。

> 参考：沙盒侧对 8080/8880/2082 亦返回 502，与上述结论一致。
> 临时用于探测的 8080/8880/2082 监听**已全部清理**，节点最终仅剩业务端口 22/53/631/8765/8888/18888/18731。

## 三、产品级判断（关键，影响选型）

VDL 的**视频流本身经源站转发**。把 Cloudflare 海外边缘插在链路中间，会同时带来：

- 免费版对中国大陆用户常调度到**美西边缘**，延迟显著上升；
- 大文件（视频）经「大陆源站 → 海外边缘 → 大陆用户」往返，吞吐明显劣化。

→ **对这个产品，「直连源站」优于「套 Cloudflare 代理」**。云朵代理适合做免备案门面，不适合做数据通路。

## 四、三条可选路线

| 方案 | 需要的操作 | 结果 | 优点 | 代价 / 风险 |
|---|---|---|---|---|
| **① 灰云直连（推荐，临时）** | **1 步**：Cloudflare DNS 把 `hanyuxz.top` 的 A 记录 IP 改为 `8.138.223.3`，代理状态改 **DNS only（灰云）** | `http://hanyuxz.top:8888` 立即可用 | 确定可用；**直连最快**；不动安全组、不动 NS | URL 带非标准端口；无 TLS（HTTP）；域名指向大陆服务器**属 ICP 备案灰色区**（非标准端口实操上不易被扫到） |
| **② 橙云代理 + 8080** | **2–3 步**：① 阿里云安全组放行 8080 ② 节点上加 8080 → 8888 转发 ③ A 记录 IP 改为 `8.138.223.3` 并保持**橙云**，SSL 模式设 Flexible | `https://hanyuxz.top`（干净域名 + HTTPS） | 免备案；隐藏源站；不改 NS | 链路经海外边缘**变慢**（见第三节）；多一步控制台操作 |
| **③ Cloudflare 命名隧道（token）** | **2–3 步**：Zero Trust 后台建隧道取 token → 我装到节点做 systemd 服务 → 后台加 Public Hostname `hanyuxz.top` → `localhost:8888` | 干净域名 + 免备案 + 无需开入站端口 | 架构最干净；不依赖安全组 | Zero Trust 首次开通步骤多；**token 模式在 443 被封前提下未实弹验证**，有失败可能 |

## 五、长期正解

- **免备案 + 能访问国外**：需要**境外公网入口**（Oracle 免费机 / 低成本境外 VPS）。
  Oracle 侧已提交客服工单 **260913-000450**（`store-oracle.custhelp.com`，主题 `Oracle Cloud Free Tier Account Creation`），排队中。
- **大陆最优体验**：`hanyuxz.top` 走 **ICP 备案 + 80/443 直连**（周期约 1–3 周，需实名材料）。这是唯一既快又合规的大陆入口方案。

## 六、方案 ① 的具体操作（Cloudflare 控制台）

1. 登录 Cloudflare → 选择域名 **hanyuxz.top** → 左侧 **DNS → Records**
2. 找到 **Type = A、Name = hanyuxz.top（@）** 的那条记录
3. 点 **Edit**，把 **Content / IPv4 address** 改为 `8.138.223.3`
4. 把 **Proxy status** 从 **Proxied（橙云）** 改为 **DNS only（灰云）**
5. **Save**
6. 生效后访问：`http://hanyuxz.top:8888`

> ⚠️ 只改这一条 A 记录。**不要动 NS 记录**，不要改其它子域。
> 改完告知 AI，AI 会用 `curl` 做端到端验收（`/api/version` 应返回 200）。
