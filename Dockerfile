# syntax=docker/dockerfile:1.7
# 单应用容器:SQLite 是唯一事实来源,/data 是唯一需要持久化的目录。

# ---------- 构建阶段:用 uv 按锁文件安装依赖 ----------
FROM python:3.13-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# ---------- 运行阶段 ----------
FROM python:3.13-slim-bookworm AS runtime

# 基础镜像固定到明确的小版本标签;升级时先拉取并用 digest 固定:
#   docker pull python:3.13-slim-bookworm
#   docker inspect --format='{{index .RepoDigests 0}}' python:3.13-slim-bookworm
LABEL org.opencontainers.image.title="SisiCalendar" \
      org.opencontainers.image.description="本地日程与订阅日历服务" \
      org.opencontainers.image.version="0.1.0"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    DATA_DIR=/data \
    HOST=0.0.0.0 \
    PORT=8000 \
    PATH="/opt/venv/bin:$PATH"

RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /app /data/export /data/snapshots /data/backups \
 && chown -R app:app /data /app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=app:app alembic.ini pyproject.toml uv.lock ./
COPY --chown=app:app app ./app
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app templates ./templates
COPY --chown=app:app static ./static

USER app

EXPOSE 8000

# 健康检查走容器内部的回环地址,与 compose 的健康检查一致
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"]

# 迁移在应用启动流程内完成(app.bootstrap),因此这里直接起服务即可
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
