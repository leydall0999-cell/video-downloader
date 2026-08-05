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
COPY yt_dlp_plugins/ ./yt_dlp_plugins/
COPY run.sh .
COPY start.py .

ENV HOST=0.0.0.0
EXPOSE 8080

# 部署平台（Railway/Render/Fly 等）通过环境变量 PORT 注入端口。
# 用 start.py 直接启动 uvicorn：Python 运行时读取真实 PORT 环境变量，
# 完全避免 shell 变量替换、权限、CMD 解析等坑。
CMD ["python", "start.py"]
