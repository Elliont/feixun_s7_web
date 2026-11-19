# 单镜像，两个入口：python -m s7.ingest / python -m s7.web
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY s7 ./s7

# 非 root 运行
RUN useradd -u 10001 -m -s /usr/sbin/nologin app \
 && mkdir -p /data && chown -R app:app /data /app
USER 10001:10001

ENV S7_DATA_DIR=/data

# 默认入口由 compose 覆盖；此处给 web 以便单跑
EXPOSE 8080 30101
CMD ["python", "-m", "s7.web"]
