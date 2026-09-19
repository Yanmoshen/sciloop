# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
# SciLoop 后端镜像：Python 3.11 + asyncpg(异步) / psycopg(同步迁移)
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC

WORKDIR /app/backend

# 系统依赖：psycopg 用 binary wheel，无需 libpq-dev；curl 供 healthcheck 使用
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖（利用层缓存）；依赖清单与 pyproject.toml 保持一致
COPY pyproject.toml ./
RUN pip install --no-cache-dir "setuptools>=68" \
    && pip install --no-cache-dir \
        "fastapi>=0.115,<1.0" \
        "uvicorn[standard]>=0.30,<1.0" \
        "sqlalchemy>=2.0.30,<2.1" \
        "asyncpg>=0.29,<0.31" \
        "psycopg[binary]>=3.1,<4.0" \
        "alembic>=1.13,<2.0" \
        "pydantic>=2.7,<3.0" \
        "pydantic-settings>=2.3,<3.0" \
        "python-dotenv>=1.0,<2.0" \
        "jsonschema>=4.21" \
        "cryptography>=42" \
        "httpx>=0.27,<1.0" \
        "pymupdf>=1.24,<2.0" \
        "apscheduler>=3.10,<4.0" \
        "pytest>=8.2,<9.0" \
        "pytest-asyncio>=0.23,<1.0" \
        "ruff>=0.5,<1.0" \
        "black>=24.4,<25.0"

COPY . .

EXPOSE 8000

# 迁移由 compose 的 command 显式执行，保证「空库可重复执行」可见
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
