# Build context: repository root (see cloudbuild.yaml)
FROM python:3.12-slim

# ffmpeg decodes every source; node runs YouTube's JS challenge for yt-dlp
COPY --from=node:22-slim /usr/local/bin/node /usr/local/bin/node
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/app/.venv UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend backend

ENV PATH=/app/.venv/bin:$PATH PORT=8080 PYTHONUNBUFFERED=1
# Vertex auth comes from the Cloud Run service account (metadata server), never a key file.
CMD exec uvicorn commentary.server:app --app-dir backend --host 0.0.0.0 --port ${PORT} \
    --ws-ping-interval 20 --ws-ping-timeout 30 --timeout-keep-alive 75
