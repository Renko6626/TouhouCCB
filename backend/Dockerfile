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
COPY run.py init_db.py ./

# data/ 目录由 volume 挂载，这里只建骨架
RUN mkdir -p data && chown appuser:appuser data

USER appuser

EXPOSE 8004

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -sf http://127.0.0.1:8004/api/v1/market/list || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8004", "--workers", "2"]
