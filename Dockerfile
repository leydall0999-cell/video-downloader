# 视频下载站 · 容器镜像
# 用法：
#   docker build -t video-downloader .
#   docker run -p 8000:8000 -e PORT=8000 video-downloader
FROM python:3.12-slim

# 编译依赖（uvicorn[standard] 的 httptools/uvloop 需要 gcc）+ ffmpeg（合并音视频必需）
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ ./server/
COPY web/ ./web/
COPY run.sh .

ENV PORT=8000 \
    HOST=0.0.0.0
EXPOSE 8000

# 部署平台（Render/Railway/Fly 等）通过环境变量 PORT 注入端口；HOST 默认监听所有网卡
CMD ["sh", "-c", "uvicorn app:app --app-dir server --host ${HOST} --port ${PORT}"]
