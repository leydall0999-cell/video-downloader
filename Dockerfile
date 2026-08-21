# 视频下载站 · 容器镜像
# 用法：
#   docker build -t video-downloader .
#   docker run -p 8000:8000 -e PORT=8000 video-downloader

# ===== Stage 1: 编译 bgutil PO token provider（YouTube bot 检测绕过） =====
# bgutil server 依赖 canvas 原生模块（node-gyp 编译需 build-essential + cairo 系库）
# 与 Node >= 22（项目 package.json engines 要求）。
FROM node:22-slim AS bgutil-build
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git ca-certificates build-essential pkg-config \
        libcairo2-dev libpango1.0-dev libjpeg-dev libgif-dev librsvg2-dev libpixman-1-dev \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --single-branch --branch 1.3.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /bgutil
WORKDIR /bgutil/server
RUN npm ci && npx tsc

# ===== Stage 2: 运行时 =====
FROM python:3.12-slim

# 编译依赖（uvicorn[standard] 的 httptools/uvloop 需要 gcc）+ ffmpeg（合并音视频必需）
# + Node >= 22（bgutil PO token server 运行要求；Debian 自带 nodejs 仅 18，用 nodesource）
# + canvas 运行时库（bgutil server 编译产物运行时加载）
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ffmpeg curl ca-certificates gnupg \
        libcairo2 libpango-1.0-0 libjpeg62-turbo libgif7 librsvg2-2 \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ ./server/
COPY web/ ./web/
COPY yt_dlp_plugins/ ./yt_dlp_plugins/
COPY run.sh .
COPY start.py .

# bgutil PO token server（YouTube bot 检测绕过；start.py 启动时拉起，监听 127.0.0.1:4416）
COPY --from=bgutil-build /bgutil /opt/bgutil

ENV HOST=0.0.0.0
EXPOSE 8080

# 部署平台（Railway/Render/Fly 等）通过环境变量 PORT 注入端口。
# 用 start.py 直接启动 uvicorn：Python 运行时读取真实 PORT 环境变量，
# 完全避免 shell 变量替换、权限、CMD 解析等坑。
CMD ["python", "start.py"]
