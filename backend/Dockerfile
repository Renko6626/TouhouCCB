FROM python:3.13-slim

# curl 用于 HEALTHCHECK
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# 非 root 用户
RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

# 先装依赖（利用 Docker 层缓存，代码改动不重装依赖）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY app/ app/
COPY scripts/ scripts/
COPY run.py init_db.py ./
# Alembic 迁移基础设施（容器内跑 `alembic stamp head` / `alembic upgrade head` 需要）
COPY alembic/ alembic/
COPY alembic.ini ./

# data/ 目录由 volume 挂载，这里只建骨架
RUN mkdir -p data && chown appuser:appuser data

USER appuser

EXPOSE 8004

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -sf http://127.0.0.1:8004/api/v1/market/list || exit 1

# --workers 1：SSE broker 是进程内 pubsub，多 worker 下 trade 事件不会跨 worker 通知，
# 订阅在 worker A 而交易落在 worker B 的客户端永远收不到事件。修法本应是 Redis pubsub，
# 但当前规模 + 单核异步性能足够（DB 是瓶颈不是 CPU），所以暂保单 worker 换 SSE 一致性。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8004", "--workers", "1"]
