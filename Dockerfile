# Shop Mode API (ADR 0013): a Python service on Fly. Build:  fly deploy   (docker is run by fly)
# Only what the service needs is copied: no tests, no evals or their results, no shop-data, no .env.

FROM python:3.14-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY prompts ./prompts
RUN uv sync --frozen --no-dev

FROM python:3.14-slim
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app
# /app is created by WORKDIR as root; the app user must own the directory itself, not only what is
# copied into it, or anything the service creates relative to it raises PermissionError.
RUN mkdir -p /app && chown app:app /app
WORKDIR /app
COPY --from=build --chown=app:app /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    APP_DEPLOY_MODE=1 \
    APP_LLM_MODE=live \
    APP_TRACING_DISABLED=1 \
    APP_HOST=0.0.0.0 \
    APP_PORT=8080 \
    APP_API_PREFIX=/api/shop
USER app
EXPOSE 8080
CMD ["python", "-m", "app.shop.serve"]
