# VPS Worker（国内解析代理）

本目录是 VPS（8.138.223.3）上 `/opt/vdl-worker/` 的**版本管理镜像**，存放国内平台
解析 worker + 守护进程源码。Railway 海外节点受跨境网络限制无法直连抖音/快手/微博等
反爬站点，改由这台国内 VPS 跑 Playwright 真实浏览器解析，再回调 Railway。

## 同步关系（重要）

- **源 = VPS `/opt/vdl-worker/`**，运行时改的是 VPS 上文件。
- **镜像 = 本目录 `vps_worker/`**，仅用于版本管理 + 回溯，不自动同步。
- 改完 VPS 代码后，**手动** `scp` 回本目录并提交（本回合已做一次全量拉取）。
- 反向部署：本目录改了 → `scp vps_worker/<file> root@8.138.223.3:/opt/vdl-worker/`
  再 `pkill -f vdl_cookie_daemon && cd /opt/vdl-worker && source .venv/bin/activate &&
  nohup python3 vdl_cookie_daemon.py > vdl_worker.log 2>&1 &` 重启 daemon。

## 文件职责

| 文件 | 作用 |
|---|---|
| `douyin_resolve.py` | 抖音/西瓜(ixigua)视频解析：Playwright 拦截 `douyinvod.com` 真实音视频流；含 `_normalize_douyin_url`（v.douyin 短链展开 + iesdouyin/ixigua 归一化）|
| `kuaishou_resolve.py` | 快手视频解析，返回合并后 mp4 直链 |
| `weibo_resolve.py` | 微博视频解析（CDN 需 Referer）|
| `ximalaya_album_resolve.py` | 喜马拉雅专辑/歌单整批解析 |
| `vdl_cookie_daemon.py` | 守护进程：HTTP `/v1/resolve?platform=xx` 端点，被 Railway `_call_vps_worker` 调用；同时承担 Cookie 池补推 |

## VPS 运维备忘

- 内存 1.6GB，**必须开 2GB swap**（`/swapfile`），否则 Playwright 起浏览器会 OOM kill。
- daemon 启动时间：`/opt/vdl-worker/vdl_cookie_daemon.py`（非 systemd，nohup 拉起）。
- 改 worker 后**必须重启 daemon** 才生效（常驻进程不热加载）。
