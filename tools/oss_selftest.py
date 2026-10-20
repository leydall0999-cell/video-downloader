#!/usr/bin/env python3
"""对象存储直链自检（方案③，2026-10-20）。

用途：**在其他机器上填完配置后，3 秒验证签名与权限是否真的可用**。
签名错只会在真实 PUT 时暴露成 403 SignatureDoesNotMatch，本地单测查不出来 ——
（本地单测只能验证「算式和 SDK 一致」，验证不了「这把 AK 对这个 bucket 有没有写权限」）。

跑法（在仓库根目录）：

    # 用 ~/.video-downloader/object_store.json
    python tools/oss_selftest.py

    # 或直接给环境变量
    VDL_OSS_PROVIDER=aliyun_oss VDL_OSS_ENDPOINT=oss-cn-shenzhen.aliyuncs.com \
    VDL_OSS_BUCKET=mybucket VDL_OSS_AK=xxx VDL_OSS_SK=xxx \
    python tools/oss_selftest.py

它做的事：
  1) 打印脱敏配置（AK/SK 只显示首尾各 3 位）；
  2) 上传一个 1MB 随机文件到 <prefix>/_selftest/<时间戳>.bin（算上传速度）；
  3) 生成预签名 GET URL，下载回来逐字节比对（算下载速度）；
  4) 打印结论与失败原因（403/404/网络分别给不同提示）。

⚠️ 会在 bucket 里留一个 ~1MB 的 _selftest/*.bin 对象，介意就手动删掉。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "server"))

import object_store  # noqa: E402


def _mask(s: str) -> str:
    if not s:
        return "(空)"
    return (s[:3] + "*" * max(0, len(s) - 6) + s[-3:]) if len(s) > 8 else "*" * len(s)


def main() -> int:
    cfg = object_store.load_config()
    if cfg is None:
        print("✗ 未读到可用配置。")
        print(f"  配置文件：{object_store.config_path()}（不存在或字段不全）")
        print("  必需字段：provider / endpoint / bucket / access_key_id / access_key_secret")
        print("  也可用环境变量：VDL_OSS_ENDPOINT / VDL_OSS_BUCKET / VDL_OSS_AK / VDL_OSS_SK")
        return 2

    print("配置：")
    for k in ("provider", "endpoint", "bucket", "prefix", "cdn", "expires", "region", "path_style"):
        print(f"  {k:18} = {cfg.get(k)}")
    print(f"  {'access_key_id':18} = {_mask(cfg['access_key_id'])}")
    print(f"  {'access_key_secret':18} = {_mask(cfg['access_key_secret'])}")

    size = 1024 * 1024
    tmp = Path(tempfile.gettempdir()) / f"vdl_oss_selftest_{int(time.time())}.bin"
    tmp.write_bytes(os.urandom(size))
    key = f"{cfg['prefix']}/_selftest/{tmp.stem}.bin"

    try:
        t0 = time.time()
        res = object_store.upload_file(tmp, key, filename="自检.mp4")
        up_cost = max(1e-6, time.time() - t0)
        print(f"\n✓ 上传成功  key={res['key']}  {size/1048576:.1f}MB / {up_cost:.2f}s"
              f"  = {size/up_cost/1048576:.2f} MB/s  etag={res.get('etag') or '-'}")

        url = object_store.presign_get(key)
        if not url:
            print("✗ 预签名失败（presign_get 返回空）")
            return 1
        print(f"✓ 预签名直链：{url[:110]}{'…' if len(url) > 110 else ''}")

        import requests

        t0 = time.time()
        r = requests.get(url, timeout=60)
        get_cost = max(1e-6, time.time() - t0)
        if r.status_code != 200:
            print(f"✗ 直链下载 HTTP {r.status_code}：{r.text[:200]}")
            return 1
        if r.content != tmp.read_bytes():
            print("✗ 下载内容与上传内容不一致（可能被 CDN 缓存了别的对象）")
            return 1
        print(f"✓ 直链下载校验通过  {len(r.content)/1048576:.1f}MB / {get_cost:.2f}s"
              f"  = {len(r.content)/get_cost/1048576:.2f} MB/s")
        print(f"   Content-Disposition: {r.headers.get('Content-Disposition') or '(无)'}")
        print("\n🎉 对象存储直链可用。网页版「保存到本机」会自动走 CDN 直连。")
        return 0
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        print(f"\n✗ 失败：{msg[:400]}")
        low = msg.lower()
        if "403" in msg:
            print("  提示：AK/SK 不对，或这把 AK 没有该 bucket 的写权限（RAM 策略需 oss:PutObject）。")
        elif "404" in msg:
            print("  提示：bucket 名或 endpoint 地域写错（endpoint 必须是 oss-cn-xxx.aliyuncs.com 这种）。")
        elif "timed out" in low or "connection" in low or "proxy" in low:
            print("  提示：网络/代理问题。国内 OSS 一般直连最快，别把上传塞进代理。")
        return 1
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
