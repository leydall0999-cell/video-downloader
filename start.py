#!/usr/bin/env python3
"""Railway/Render/Fly 等平台的容器启动入口。

部署平台把 PORT 作为真实环境变量注入容器，本脚本直接读取并启动 uvicorn，
完全避免 Dockerfile CMD 层面的 $PORT 变量替换/权限问题。
"""
import os
import uvicorn

port = int(os.environ.get("PORT", "8080"))
host = os.environ.get("HOST", "0.0.0.0")

print(f"▶ starting uvicorn on host={host} port={port}")
uvicorn.run("app:app", app_dir="server", host=host, port=port)
