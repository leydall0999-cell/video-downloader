# 对象存储直链分发（方案③）配置说明

> 2026-10-20。解决「网页版点『保存到本机』，418MB 视频要下 49 分钟」的问题。

## 1. 问题是什么（实测数据，不是猜测）

浏览器版保存走的是 `/api/tasks/{id}/file?download=1` —— **部署节点单流回传**。
节点在海外（`hanyuxz.top` = Cloudflare → Railway）时，跨境**单连接**实测：

| 测试 | 结果 |
|---|---|
| 单连接拉 522KB 静态文件 | 122 KB/s（418MB ≈ 49 分钟，与用户截图一致） |
| 4 连接并发 | 285 KB/s |
| 8 连接并发 | 673 KB/s（≈5.5×，说明限的是**每条连接**） |

结论：瓶颈在跨境单流，不在用户磁盘/CPU/宽带。

## 2. 改成了什么

```
下载完成 → 节点上传一次到对象存储（OSS）
              ↓ 预签名直链（有效期可配）
        浏览器直连 OSS / CDN 下载  ← 跑满本地带宽
```

- 后端：`server/object_store.py` + `GET /api/tasks/{task_id}/direct_url`
- 前端：任务卡「保存到本机 ⬇」先问直链，拿到就整页跳过去；**任何一步不可用都回退原 /file**（保存这个动作永远能完成）
- 依赖：**零新增**。签名用 `hashlib/hmac` 手写（OSS V1 + AWS SigV4），不吃安装包体积

## 3. 配置

优先级：**环境变量 > 配置文件**。配置文件位置 `~/.video-downloader/object_store.json`
（`VDL_HOME` 可改目录）；容器部署直接给环境变量更省事。

| 字段 | 环境变量 | 说明 |
|---|---|---|
| provider | `VDL_OSS_PROVIDER` | `aliyun_oss`（默认）/ `s3`（R2、MinIO、COS 的 S3 口） |
| endpoint | `VDL_OSS_ENDPOINT` | 阿里云形如 `oss-cn-shenzhen.aliyuncs.com`（**只写域名，不带 bucket、不带 https://**） |
| bucket | `VDL_OSS_BUCKET` | bucket 名 |
| access_key_id | `VDL_OSS_AK` | RAM 用户 AK |
| access_key_secret | `VDL_OSS_SK` | RAM 用户 SK |
| prefix | `VDL_OSS_PREFIX` | 对象前缀，默认 `vdl/tasks` |
| cdn | `VDL_OSS_CDN` | 可选，CDN 加速域名（需回源到同一 bucket 且透传 query 签名） |
| expires | `VDL_OSS_EXPIRES` | 预签名有效期（秒），默认 `86400`，最大 7 天 |
| region | `VDL_OSS_REGION` | 仅 `s3`：R2 填 `auto` |
| path_style | `VDL_OSS_PATH_STYLE` | 仅 `s3`：MinIO 等需要 `1` |

示例见 `object_store.example.json`。

### 阿里云 OSS 开通步骤（约 5 分钟）

1. 控制台 → 对象存储 OSS → 创建 Bucket：
   - 地域选**离用户近**的（如华南1 深圳）
   - 读写权限选 **私有**（不要公共读，直链靠预签名）
   - 不开静态网站托管也可以
2. 右上角头像 → AccessKey 管理 → 建议建 **RAM 子用户**，只授 `AliyunOSSFullAccess`
   或自定义策略 `oss:PutObject` / `oss:GetObject`（只给这一个 bucket）
3. 把 endpoint / bucket / AK / SK 填进部署节点的环境变量（Railway → Variables）
4. 在**能连到该 bucket 的机器上**跑自检：

```bash
python tools/oss_selftest.py
# 期望看到：✓ 上传成功 … ✓ 直链下载校验通过 … 🎉
```

## 4. 生效范围与边界（如实说明）

- **生效**：网页版（浏览器访问）点「保存到本机」；同一个任务第二次下载、换设备、
  分享给别人下载，都是 CDN 全速。
- **不生效**：桌面 App（文件本来就在本机，保存是本地写盘，与这条链路无关）。
- **首次上传仍受节点出网带宽限制** —— 这是所有方案共同的天花板。区别是：
  旧方案「每次保存都单流回传」，新方案「上传一次，之后永远全速」。
- **会占存储**：成品在 bucket 里留一份。介意的话配一条 bucket 生命周期规则
  （例如 7 天后自动删除 `vdl/tasks/` 前缀）。
- **流量计费**：OSS 外网出流量按量计费（约 0.5 元/GB，各厂商/地域不同）。
  418MB ≈ 0.2 元/次。
- **安全边界**：预签名 URL 在有效期内**无需登录即可下载**。默认 24 小时，
  嫌长就调 `VDL_OSS_EXPIRES=3600`。
- **对象名是 ASCII slug**（中文标题会被转拼音式空值 → `file.mp4`），下载时的
  文件名靠上传时写入的 `Content-Disposition` 恢复，浏览器里仍是中文名。

## 5. 验证与排查

```bash
# 单测（签名算法 + 假 OSS 端到端，不联网）
PYTHONPATH=server:tests python -m pytest server/tests/test_object_store.py -q
```

| 现象 | 原因 | 处置 |
|---|---|---|
| 前端一直显示「云端加速准备中 0%」 | 节点上传慢（跨境/带宽小） | 正常，等它传完；或把部署迁到国内节点 |
| 保存直接开始下载、但很慢 | 未配置对象存储（回 404） | 检查 env 是否注入到**运行进程**（改完要重启/重新部署） |
| 403 `SignatureDoesNotMatch` | AK/SK 错、endpoint 带地域不符、路径风格不对 | 跑 `tools/oss_selftest.py`，按提示改 |
| 403 `AccessDenied` | RAM 没给 `oss:PutObject` | 补权限 |
| 404 `NoSuchBucket` | bucket 名或地域错 | endpoint 必须是 `oss-cn-xxx.aliyuncs.com` |
| 下载回来是旧文件 | CDN 缓存了同 key 旧对象 | 对象 key 含 task_id，理论不会；若自建 CDN 需开「忽略参数缓存」或短 TTL |
