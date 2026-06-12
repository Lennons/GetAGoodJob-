# ── Stage 1: Frontend build ──
FROM node:20-alpine AS frontend
WORKDIR /src
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ .
RUN npm run build

# ── Stage 2: Production ──
FROM python:3.11-slim
LABEL org.opencontainers.image.title="BOSS 直聊助手"
LABEL org.opencontainers.image.version="1.1.1"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl gnupg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install patchright && patchright install chromium

COPY app/ ./app/
COPY alembic/ ./alembic/
COPY alembic.ini .
COPY --from=frontend /src/public/ ./public/

EXPOSE 8788
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8788"]
