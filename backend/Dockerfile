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

# 前端 build 版本自刷机制（spec § 8 阶段 2）：CI 把 github.sha 烧进环境，
# snapshot 下发给前端比对。本地构建默认 "dev"（前端侧空值/dev 不触发提示）。
ARG BUILD_SHA=dev
ENV APP_BUILD_SHA=$BUILD_SHA

EXPOSE 8004

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -sf http://127.0.0.1:8004/api/v1/market/list || exit 1

# --workers 1：SSE broker 是进程内 pubsub，多 worker 下 trade 事件不会跨 worker 通知，
# 订阅在 worker A 而交易落在 worker B 的客户端永远收不到事件。修法本应是 Redis pubsub，
# 但当前规模 + 单核异步性能足够（DB 是瓶颈不是 CPU），所以暂保单 worker 换 SSE 一致性。
#
# --timeout-graceful-shutdown 4：uvicorn 默认无限等在途响应结束，而 SSE 长连接最长 1h
# 不会自己断——只要有一条 SSE 在线，lifespan shutdown（writer/flusher stop、engine
# dispose）永远跑不到，docker 在 stop_grace_period(8s) 后直接 SIGKILL。设 4s（< 8s）
# 让 uvicorn 超时后 cancel 流任务，正常走 lifespan shutdown。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8004", "--workers", "1", "--timeout-graceful-shutdown", "4"]
