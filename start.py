#!/usr/bin/env python3
"""Railway/Render/Fly 等平台的容器启动入口。

部署平台把 PORT 作为真实环境变量注入容器，本脚本直接读取并启动 uvicorn，
完全避免 Dockerfile CMD 层面的 $PORT 变量替换/权限问题。
"""
import os
import socket
import subprocess
import time

import uvicorn


def _start_bgutil_pot_server() -> None:
    """启动 bgutil PO token server（YouTube bot 检测绕过，可选）。

    背景：2025 后 YouTube 对数据中心 IP 的匿名请求返回
    "Sign in to confirm you're not a bot"，yt-dlp 仅靠 player_client 轮换
    无法绕过。bgutil-ytdlp-pot-provider 的 HTTP server 用 Node 执行 YouTube
    BotGuard JS 生成 proof-of-origin token，yt-dlp 通过 pip 插件
    （youtubepot-bgutilhttp，默认 base_url=http://127.0.0.1:4416）自动获取。

    服务器源码由 Dockerfile 多阶段构建编译到 /opt/bgutil/server/build/main.js；
    本地开发环境（无该文件）直接跳过，不影响其他平台。
    """
    main_js = "/opt/bgutil/server/build/main.js"
    if not os.path.exists(main_js):
        print("[bgutil] PO token server 未安装（仅 Docker 镜像内置），跳过 YouTube token 服务")
        return
    # 已在运行则跳过
    try:
        with socket.create_connection(("127.0.0.1", 4416), timeout=0.5):
            print("[bgutil] PO token server 已在运行（127.0.0.1:4416）")
            os.environ["YT_DLP_POT_PROVIDER_URL"] = "http://127.0.0.1:4416"
            return
    except OSError:
        pass
    try:
        proc = subprocess.Popen(
            ["node", main_js],
            stdout=open("/tmp/bgutil-pot-server.log", "ab"),
            stderr=subprocess.STDOUT,
            close_fds=True,
        )
        # 等待端口就绪（最多 10s）
        for _ in range(20):
            try:
                with socket.create_connection(("127.0.0.1", 4416), timeout=1):
                    break
            except OSError:
                time.sleep(0.5)
        os.environ["YT_DLP_POT_PROVIDER_URL"] = "http://127.0.0.1:4416"
        print(f"[bgutil] PO token server 已启动 pid={proc.pid}（127.0.0.1:4416）")
    except Exception as e:  # noqa: BLE001
        print(f"[bgutil] PO token server 启动失败（不影响主服务）: {e}")


_start_bgutil_pot_server()

port = int(os.environ.get("PORT", "8080"))
host = os.environ.get("HOST", "0.0.0.0")

print(f"▶ starting uvicorn on host={host} port={port}")
uvicorn.run("app:app", app_dir="server", host=host, port=port)
