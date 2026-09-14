FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src_extract:/app/GPT-Register-Tool-main \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Asia/Shanghai

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gcc \
        g++ \
        libffi-dev \
        libnss3 \
        libssl-dev \
        npm \
        nodejs \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir --upgrade pip

COPY src_extract/pyproject.toml /app/src_extract/pyproject.toml
COPY src_extract/scripts/image_upscale/package.json /app/src_extract/scripts/image_upscale/package.json
COPY src_extract/scripts/image_upscale/package-lock.json /app/src_extract/scripts/image_upscale/package-lock.json
COPY GPT-Register-Tool-main/requirements.txt /app/GPT-Register-Tool-main/requirements.txt

RUN cd /app/src_extract/scripts/image_upscale \
    && npm ci --omit=dev \
    && node --input-type=module -e "import sharp from 'sharp'; if (typeof sharp !== 'function') process.exit(1)"

RUN /app/.venv/bin/pip install --no-cache-dir \
        "curl-cffi>=0.16.0,<0.17" \
        "fastapi>=0.136.0" \
        "pillow>=12.2.0" \
        "pybase64>=1.4.3" \
        "python-multipart>=0.0.26" \
        "tiktoken>=0.12.0" \
        "uvicorn>=0.44.0" \
        "sqlalchemy>=2.0.0" \
        "psycopg2-binary>=2.9.0" \
    && /app/.venv/bin/pip install --no-cache-dir -r /app/GPT-Register-Tool-main/requirements.txt \
    && /app/.venv/bin/python -m playwright install --with-deps chromium

COPY src_extract /app/src_extract
COPY GPT-Register-Tool-main /app/GPT-Register-Tool-main

RUN mkdir -p /app/data /app/GPT-Register-Tool-main/runtime /app/GPT-Register-Tool-main/sessions \
    && test -f /app/GPT-Register-Tool-main/config.json || cp /app/GPT-Register-Tool-main/config.example.json /app/GPT-Register-Tool-main/config.json \
    && /app/.venv/bin/python -m compileall -q /app/src_extract /app/GPT-Register-Tool-main/sms_tool

EXPOSE 80

CMD ["/app/.venv/bin/python", "-m", "uvicorn", "main:app", "--app-dir", "/app/src_extract", "--host", "0.0.0.0", "--port", "80"]
