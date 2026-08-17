#!/usr/bin/env python3
"""从本机浏览器导出目标站点 Cookie（Netscape 格式）并上传到 ECS 持久化目录。

阶段 B1 辅助脚本：让 ECS worker 无需浏览器即可使用登录态。

用法示例：
    .build_venv/bin/python sync_cookie_to_ecs.py --host douyin.com
"""
from __future__ import annotations

import argparse
import http.cookiejar
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _import_downloader_helpers():
    # downloader.py 内部用相对/同目录导入 platforms/cookie_cache 等，
    # 所以把 server/ 目录本身加入 sys.path，以顶层模块身份 import。
    server_dir = Path(__file__).resolve().parent / "server"
    sys.path.insert(0, str(server_dir))
    import downloader

    return downloader._find_host_cookie_profile, downloader._cookie_domains_for_host


def export_cookies(host: str, output: Path) -> tuple[str, str] | None:
    find_profile, domains_for_host = _import_downloader_helpers()
    found = find_profile(host)
    if not found:
        print(f"❌ 本机浏览器未找到 {host} 的 Cookie，请先在浏览器登录该站点")
        return None
    browser, profile = found
    print(f"🍪 从 {browser} / {profile} 读取 {host} 的 Cookie...")

    from yt_dlp.cookies import extract_cookies_from_browser

    jar = extract_cookies_from_browser(browser, profile)
    domains = domains_for_host(host)
    print(f"   目标域候选: {domains}")

    # MozillaCookieJar 即 Netscape 格式，yt-dlp 的 cookies 选项可直接读取
    tmp_jar = http.cookiejar.MozillaCookieJar(str(output))
    kept = 0
    for cookie in jar:
        cookie_domain = cookie.domain.lstrip(".")
        for d in domains:
            if cookie_domain == d or cookie_domain.endswith("." + d):
                tmp_jar.set_cookie(cookie)
                kept += 1
                break
    tmp_jar.save(ignore_discard=True, ignore_expires=True)
    print(f"✅ 已导出 {kept} 条 Cookie 到 {output}")
    return browser, profile


def upload_to_ecs(local: Path, ecs_host: str, remote_path: str, ssh_key: str | None = None) -> None:
    cmd = ["scp", "-o", "ConnectTimeout=10"]
    if ssh_key:
        cmd.extend(["-i", ssh_key])
    cmd += [str(local), f"{ecs_host}:{remote_path}"]
    print(f"📤 上传: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)
    print(f"✅ 已上传到 {ecs_host}:{remote_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="导出本机浏览器 Cookie 并上传到 ECS")
    parser.add_argument("--host", required=True, help="站点 host，例如 douyin.com")
    parser.add_argument("--ecs", default="root@8.138.223.3", help="ECS 用户@主机")
    parser.add_argument("--dest", default="/opt/vdl-worker/cookies/{host}.txt", help="远程路径模板")
    parser.add_argument("--ssh-key", default=None, help="SSH 私钥路径")
    parser.add_argument("--output", default=None, help="本地临时文件路径")
    args = parser.parse_args()

    host = args.host.lstrip("https://").lstrip("http://").split("/")[0]
    remote_path = args.dest.format(host=host)
    local_path = Path(args.output or tempfile.NamedTemporaryFile(delete=False, suffix=f".{host}.cookies.txt").name)

    result = export_cookies(host, local_path)
    if not result:
        return 1

    upload_to_ecs(local_path, args.ecs, remote_path, args.ssh_key)
    local_path.unlink(missing_ok=True)
    print(f"🎉 {host} Cookie 已持久化到 ECS: {remote_path}")
    print("   下一步：ssh 到 ECS 运行 yt-dlp 验证解析。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
