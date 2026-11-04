# syntax=docker/dockerfile:1

FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock README.md CHANGELOG.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY narratarr ./narratarr
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim
LABEL org.opencontainers.image.title="Narratarr" \
      org.opencontainers.image.description="An *arr-style audiobook manager with a built-in player." \
      org.opencontainers.image.source="https://github.com/narratarr/narratarr" \
      org.opencontainers.image.licenses="MIT"
COPY --from=build /app/.venv /app/.venv
COPY docker/entrypoint.sh /entrypoint.sh
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    NARRATARR_HOST=0.0.0.0 \
    NARRATARR_PORT=8484 \
    NARRATARR_DATA=/config \
    NARRATARR_LIBRARY=/audiobooks \
    NARRATARR_DOCKER=1 \
    PUID=1000 \
    PGID=1000
VOLUME ["/config", "/audiobooks"]
EXPOSE 8484
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"NARRATARR_PORT\"]}/api/health', timeout=4)"
ENTRYPOINT ["/entrypoint.sh"]
CMD ["narratarr"]
