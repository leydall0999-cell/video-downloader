# VDL 迁往 Oracle Cloud Free Tier（首尔/东京）—— 注册 + 部署 + 接流清单

> 目标：用 **¥0 永久免费**的 Oracle ARM 实例替代已死的 Railway `hanyuxz.top`，
> 承接网站入口 + 国外视频下载（广州实测出网国外被 GFW/阿里云白名单全废，必须境外节点）。
> 免备案（境外地域）。
>
> 决策背景（2026-09-13 实测）：阿里云广州节点 `8.138.223.3:8888` 回源/网站本身健康，
> 但**出网国外被墙**（YouTube/Vimeo/TikTok/http=000，仅 github 类白名单能通）。
> 故 VDL 下国外视频站必须境外节点。Oracle 免费机出网国外畅通且免费，优于香港 ECS（¥400–600/年）。

---

## 0. 架构（最终形态）

```
                境外访客 / 下载请求
                        │
                  Cloudflare 边缘（Anycast, 免备案）
                        │  Cloudflare Tunnel（出站长连，源站不暴露公网 IP/端口）
                        ▼
           Oracle 免费机（首尔/东京, Ubuntu 22.04）
             ├─ vdl-web.service  →  uvicorn app:app :8888 (127.0.0.1)
             └─ cloudflared      →  hanyuxz.top 隧道入
                        │
        （国内站 B站下载仍走广州 + 住宅 IP 兜底，与节点地域无关）
```

- **源站不直开 80/443**：只经 Tunnel 进，广州那套"裸 8888 公网"收掉。
- **免备案**：Tunnel 模式下源站不在大陆直开 Web 端口，合规。

---

## 1. 注册 Oracle Cloud（拿免费机）—— 点击级步骤

### 1.0 先在本机（Mac）生成 SSH 密钥（一次性）
打开「终端」粘贴：
```bash
ls ~/.ssh/id_ed25519.pub 2>/dev/null || ssh-keygen -t ed25519 -C "vdl-oracle"
cat ~/.ssh/id_ed25519.pub
```
最后一行会打印出一长串 `ssh-ed25519 AAAA...` —— **全选复制**，第 1.2 步要粘进 Oracle。

### 1.1 注册账号
1. 浏览器打开 **https://www.oracle.com/cloud/free/**（右上角语言可切中文）。
2. 点 **「开始免费试用 / Start for free」**。
3. 填邮箱 + 设置密码 → 收验证邮件点链接。
4. **账户信息**：
   - **账户名（Tenancy Name）**：随便起，如 `vdl-tenancy`（之后改不了，记住它）。
   - **首页区域（Home Region）**：下拉选 **日本东京（Tokyo）或 韩国首尔（Seoul）** ——
     ⚠️ 这个**一步定终身**，免费机只能在你选的首页区域稳定建；就近选东京/首尔。
   - 国家/地址：按你真实信息填（Visa 卡账单国一致更稳）。
5. **手机验证**：收短信/语音验证码填入。
6. **付款（Visa）**：填招行 Visa 全币种卡号 / 有效期 / CVV / 账单地址。
   - Oracle 会做一笔**预授权冻结（通常 $0~$1，偶尔临时冻结少量额度）**，**不是扣费**，几天后自动释放。
   - 验证通过即送 **Always Free** 额度（A1 ARM 4C24G + 2×AMD 1G + 200G 块存储 + 10G 对象存储）。
   - 绑卡成功会出现绿色 **「Thank you!」** 弹窗 → 点 **Close**（**不要刷新页面**，刷新会丢会话）。
   - ⚠️ 全程**不要点**任何「升级到付费 / Upgrade to Pay As You Go / 添加付费额度」按钮，
     Always Free 不需要升级，误点升级才会产生真实扣费风险。
7. 注册完 → 登录 **https://cloud.oracle.com**（用刚注册的邮箱/密码）。

#### 1.1.1 注册页实测踩坑（2026-09-13 亲历，全部已解决）

| 现象 | 真因 | 解法 |
|---|---|---|
| 「继续/Continue」按钮**灰色点不动** | **备用姓名（Alternate name）** 字段未填，且**不显示红字报错**，极易误判为浏览器/验证码问题 | 填上备用姓名（拼音即可）→ 按钮变亮。不是浏览器问题，**换 Safari 无用** |
| 「名字改不了，是灰色的」 | 中文翻译界面下部分字段被翻译层挡住/只读态 | 关掉浏览器翻译插件，或直接切英文页面填 |
| 城市栏「怎么输都不对」 | 把「中国广东省深圳市龙华区」整串塞进 City | City 栏**只填 `Shenzhen`**，然后**从下拉建议里点选**（必须点选，不能只打字） |
| 绑卡被 **declined** | 招行「**一键锁卡 → 境外消费 → 无卡交易**」被锁（卡本身没问题） | 掌上生活 App → 一键锁卡 → 境外消费-无卡交易 → **立即解锁** |
| 银行短信「**已过期或有效期输错**」 | Oracle 有效期是 **两个下拉框**，选错月份/年份 | 掏实体卡看 **`VALID THRU MM/YY`**，两个下拉各自选对，再填背面 **CVN（3 位）** |
| 短信说卡过期 | 卡真过期 | 换另一张 Visa/Mastercard；或走 **Render 免费层（无需信用卡）** |
| 注册完只记得邮箱，**想不起「云账户名称」** | 该名字在注册**第一步「账户信息」页**填（离绑卡很远，极易忘），却是登录三要素之一（邮箱/密码/云账户名） | ⚠️ 见下方「找回云账户名」专节——**登录页的「开启在线聊天」链接是坑，会跳到销售页，此路不通** |
| 登录页填云账户名报 **「这个名字不行，要不要再试一次?」** | **该租户在 Oracle 侧不存在** = 注册从未走到最后一步，「账号根本没建成」。注意：注册表单里自动建议的名字（如邮箱前缀）**不代表已存在**，别拿它当结论 | 唯一正路 = **完整重走一遍注册**（§1.1.2 节奏），全程守着 **1 分钟内点掉验证邮件**；⭐ **唯一可靠的"注册成功"标志 = 收到「Welcome to Oracle Cloud」欢迎邮件**（只收到 Verify 邮件 = 没成功） |

> 绑卡成功标志：**「Thank you!」绿框弹窗**（本次已达成）。
> 探测租户是否存在：**别用 `curl cloud.oracle.com/?tenant=<名>` 判断** —— 该页是纯前端 SPA，**真名与乱名都返 200**，无区分度。

#### 1.1.2 注册流程的真实顺序（别再中途离开）

`国家/姓名/邮箱/云账户名`（填完邮箱**立刻**发验证邮件）→ `地址` → `手机验证` → `绑卡` → **「创建账户」** → **点验证邮件链接激活**。

⚠️ 三个致命细节（2026-09-13 实测踩中）：

1. **验证邮件是第 2 步就发的**（不是最后一步），且时效极短（**实测 39 分钟即失效**）。
   正确姿势：同一浏览器开**双标签**（注册页 + Gmail），提交邮箱后 **1 分钟内**点掉验证链接；点慢了会退回**空白注册表单**，此前的填写全部作废。
2. **「绑卡 Thank you!」≠ 账号完成**。绑卡只代表"支付方式验证通过"，后面还有「创建账户」+「邮箱验证激活」两步。
3. **全程不许中断**：中途关标签或跑去做别的，会话丢失 → 落地空白表单。已绑的卡 Oracle 侧仍认，重走时不必重填卡信息。

#### 1.1.3 找回「云账户名称」（tenancy）—— 按成本从低到高试

❌ **大坑（实测）**：登录页 `cloud.oracle.com` 输入框下方那行小字「忘记了您的云账户名称？**开启在线聊天**」，
点下去会跳转 `oracle.com/corporate/contact`（**Oracle 销售/售前联系页**，只会推销），**根本不处理账号找回**。别再点了。

✅ 可行路子：

1. **翻浏览器历史（最快、免费）** — Chrome 按 `⌘Y` 打开历史 → 搜 `oracle`：
   - 命中 `cloud.oracle.com/?tenant=xxxx` → **`xxxx` 就是云账户名称** 🎉
   - 命中 `signup.cloud.oracle.com/?verify_email=eyJ...` → **把整条 URL 复制出来**：该链接的 JWT payload 是 base64url 编码，
     解出来常含 tenancy / email 字段，可读出云账户名。
2. **Gmail 全量搜** — 用 `from:oraclecloud.com`（**不要**用精确短语），并**检查垃圾邮件箱**：找**主题不是** "Verify your email" 的邮件
   （Welcome / Your Oracle Cloud account / 激活 之类），正文通常写明云账户名并附直达链接。
3. **Oracle 账户中心自查** — 用注册邮箱 + 注册时设的密码登录 `oracle.com`，右上角账户中心看关联的 Oracle Cloud / Cloud Accounts。
4. **兜底：干净重走一次注册**（确定性最高）— 若上面全拿不到，说明账号很可能压根没建成；
   重走一遍（见 §1.1.2 节奏），**并把「云账户名称」当场截图记下来**——这是以后每次登录都要填的。

### 1.2 建免费实例（Create Instance）
1. 控制台左上角 **☰ 菜单 → 计算（Compute）→ 实例（Instances）**。
2. 右上角 **创建实例（Create instance）**。
3. **名称**：`vdl-oracle`。
4. **镜像和形状**：
   - 点 **「更改镜像（Change image）」** → 选 **Ubuntu** → **Ubuntu 22.04 LTS** → 选 → 确认。
   - 点 **「更改形状（Change shape）」** → 左边选 **专用与旧版（Specialty and Legacy）** →
     形状 **VM.Standard.A1.Flex** → OCPU 填 **4**、内存 **24 GB**（都在 Always Free 额度内）。
     - 若 A1 显示「容量不足 / Capacity unavailable」：换可用区（AD2/AD3）或隔几小时再试；
       **别退而求其次选 AMD E2.1（仅 1G 内存，VDL 跑不动）**。
5. **主键（SSH）**：点 **「添加 SSH 密钥」→ 粘贴公钥** → 把 1.0 步复制的 `ssh-ed25519 ...` 粘进去 → 添加。
6. **虚拟云网络**：保持默认「新建 VCN」（向导会自动建安全列表，含 22 入站）。
   - 确认 **「分配公有 IPv4 地址」= 是（Yes）**（SSH 管理需要；Tunnel 本身不靠这个入站）。
7. **启动卷**：默认 **40 GB**（免费额度 200G 内，够用）。
8. 点 **「创建（Create）** → 等 2~3 分钟变「正在运行（Running）」。
9. 在实例详情页复制 **公有 IP 地址（Public IP）** —— 后面给 AI 部署用。

> ⚠️ 注册偶有机器人验证/区域额度紧张；A1 机型通常东京/首尔可建。建不出换可用区或隔天再试。
> ⚠️ Oracle Ubuntu 镜像**默认登录用户是 `ubuntu`（不是 root）**；后续 AI 用 `ssh ubuntu@<IP>` + `sudo` 操作。

---

## 2. 部署（把 IP 给 AI，AI 全程 SSH 操作，你不用动手）

AI 会在 Oracle 实例上跑 `deploy/oracle_setup.sh`（本仓库内），脚本做：

1. 基线加固：建 `vdl` 非 root 用户、`ufw` 只放 22（Tunnel 不占额外入端口）、禁 root 密码登录。
2. 装依赖：`python3-venv`、`ffmpeg`、`git`、`curl`、`ca-certificates`。
3. 拉代码：`git clone` 你的 `app-dev` 分支到 `/opt/vdl`。
4. 建 venv + `pip install -r requirements.txt`（torch 在 requirements.txt 里是注释态，不装；
   扩散档按需另装；ARM aarch64 下 onnxruntime/pymupdf 均有多平台 wheel）。
5. 写 `vdl-web.service`（systemd），`uvicorn app:app --host 127.0.0.1 --port 8888`，
   工作目录 `/opt/vdl/server`，DOWNLOAD_DIR 指向 `/opt/vdl/downloads`（持久盘）。
6. 装 `cloudflared`，写 Tunnel 配置（token 由你从 Cloudflare 建 tunnel 后提供）。

---

## 3. 第一步实测（最关键的"别白注册"验证）

部署完、**切流量之前**，AI 先 SSH 进 Oracle 实测出网国外：

```bash
curl -4 -s -o /dev/null -w "youtube http=%{http_code} time=%{time_total}s\n" -m 12 https://www.youtube.com
curl -4 -s -o /dev/null -w "vimeo  http=%{http_code}\n" -m 12 https://vimeo.com
curl -4 -s -o /dev/null -w "github http=%{http_code} speed=%{speed_download}\n" -m 20 https://github.com
```

- **YouTube http=200** → 出网国外通，正式接流。
- **仍 http=000** → 罕见（Oracle 境外通常通），暂停接流，排查网络/安全组，别切。

> 这一步用数据确认"免费机真能下国外视频"，再动 DNS，避免白忙。

---

## 4. 接 Cloudflare Tunnel（hanyuxz.top 切到 Oracle）

1. 你登录 Cloudflare → Zero Trust → Networks → Tunnels → Create Tunnel → 选 **cloudflared**。
2. 复制安装命令里的 **tunnel token**（一长串）。
3. 把 token 给 AI，AI 在 Oracle 上：
   - `cloudflared service install <TOKEN>`
   - 在 Tunnel 配置里加 Public Hostname：`hanyuxz.top` → `http://localhost:8888`。
   - （可选）`www.hanyuxz.top`、`hk.hanyuxz.top` 同样指向 8888。
4. 确认 `https://hanyuxz.top/api/version` 返回 200（不再是 Railway 的 404 Application not found）。

---

## 5. 收尾 / 广州节点去留

- **广州 `8.138.223.3` 继续保留**，退为：国内回源 + B站下载的住宅 IP 兜底层 +
  cn_tunnel_client 对端（若用 Tunnel 模式，广州经 Tunnel 连 Oracle，不再裸 8888 公网）。
- B站封机房 IP 那摊子事与节点地域无关（香港/Oracle 也解决不了），仍走住宅 IP 兜底。
- 旧 Railway 应用已死，直接弃用或留作双活备份，不再投入。

---

## 6. 回滚

若 Oracle 出问题：把 Cloudflare Tunnel 的 Public Hostname 改回指向广州 `http://8.138.223.3:8888`
（广州 vdl-web 一直健康），即可秒级回广州，不影响网站可用性。

---

## 费用小结

| 项 | 费用 |
|---|---|
| Oracle 免费机 A1（4C24G/40G） | **¥0 / 永久** |
| Cloudflare Tunnel | **¥0** |
| 域名 hanyuxz.top（已购） | ¥14/年（续费） |
| 合计 | **≈ ¥14/年**（对比香港 ECS ¥400–600/年） |

> 唯一隐性成本：Oracle 注册需 Visa 验证 + 偶尔额度紧张；你已有 Visa，门槛已跨过。
