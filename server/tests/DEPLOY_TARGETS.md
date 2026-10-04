# 部署目标清单（改前必读）

**这个项目有两台机器同时对外提供服务。** 任何修复只落在一台上，都等于没修。

| 角色 | 主机 | 部署路径 | 对外入口 | 职责 |
|---|---|---|---|---|
| **cn（规范）** | `8.138.223.3` | `/opt/vdl-worker` | `hanyuxz.top`（经 CF 隧道） | 网页端主服务 + 授权中心（`:8902`，会员/积分真源） |
| **hk（海外）** | `47.82.101.79` | `/opt/vdl` | `hk.hanyuxz.top`（**DNS 直接解析，不经 CF**） | 网页端海外副本 + 海外下载中转 |
| 桌面 | 本机 | `/Applications/视频工坊.app` | `:8321` | 独立构建产物（改 `server/*.py` 必须全量重建进 `.so`） |

## 🔴 铁律

1. **改 `server/` 或 `web/` 后，两台都要下发。** hk 直连公网、无 CF 兜底，安全修复漏一台 = 漏洞仍可被利用。
2. **hk 只能从 cn 两跳 ssh**（`ssh root@8.138.223.3` 再 `ssh root@47.82.101.79`），`scp` 不能直连。
3. **hk 落位必须用脚本文件法**（把 `.sh` 送过去再 `bash /tmp/x.sh`）——
   `ssh 'bash -s' <<EOF` heredoc 在本环境**静默失败**，看着像成功其实没执行。
4. **落位前先 `cp` 备份**（`.bak-<时间戳>`），改错了能回滚。
5. **下发后必须重启对应服务并验指纹**：
   - cn：`systemctl restart vdl-web`（纯静态 CSS 免重启）
   - hk：`systemctl restart vdl-web`（**改了 `.py` 必须重启**；重启后 bind 需 5~8 秒）
6. **三端 sha256 逐字节比对**（本机 → cn → hk），别只看一端就说完成。

## 事故记录（别再犯）

- **2026-10-04**：cn 上修好了 `/api/member/activate` 白嫖后门（加 `user_is_admin` 门禁）并删掉
  个人中心的占位功能行，但 **hk 从 2026-10-01 起就没再同步**，落后 4 个前端提交、含 1 个安全修复。
  在 hk 上注册普通账号实测：`POST /api/member/activate {"code":"download_year"}` →
  `{"ok":true,...}`，`download_member.active=True`（白嫖一年会员）。
  而 `hk.hanyuxz.top` 是 DNS 直连、公网可触达 ⇒ 门禁形同虚设。
  **发现时的定性错误**：先把它当"界面旧副本"记了一笔，没意识到它同样有活跃攻击面。

## 怎么查两端有没有漂移

```bash
# 完整漂移检查（需 ssh，会跑真实接口；两跳）
bash server/tests/check_deploy_drift.sh

# 只比文件指纹（更常用）
bash server/tests/check_deploy_drift.sh --files-only
```

脚本只**报告**差异、不自动同步 —— 同步前先确认线上没有别的热修会被覆盖
（`diff <(git show HEAD~1:web/app.js) <(ssh ... cat /opt/vdl-worker/web/app.js)`）。
