FROM python:3.12-slim

# 自托管部署：控制平面（FastAPI）。对象存储与数据库由 compose 中的
# postgres/minio 提供，运行时通过环境变量注入连接串。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖，利用层缓存。
COPY apps/api/requirements.txt /app/apps/api/requirements.txt
COPY apps/api/requirements-prod.txt /app/apps/api/requirements-prod.txt
RUN pip install -r /app/apps/api/requirements-prod.txt

COPY packages /app/packages
COPY apps/api /app/apps/api

ENV PYTHONPATH=/app:/app/apps/api
WORKDIR /app/apps/api

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/platform/health || exit 1

CMD ["python", "-X", "utf8", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]