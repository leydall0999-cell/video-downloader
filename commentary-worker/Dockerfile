# 独立解说 worker 镜像
# 构建:  docker build -t commentary-worker .
# 运行:  docker run -d -p 8100:8100 -e WORKER_MAX_CONCURRENCY=2 commentary-worker
FROM python:3.11-slim

# 系统级依赖：ffmpeg（音频抽取/渲染）+ 中文字体（字幕不乱码）
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 8100
CMD ["uvicorn", "commentary_worker:app", "--host", "0.0.0.0", "--port", "8100"]
