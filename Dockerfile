# API and web UI. On first start the container generates the synthetic warehouse into /app/data.
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=builder --chown=app /app/.venv /app/.venv
COPY --chown=app config ./config
# The app writes the DuckDB warehouse and owner actions (state.json) here at runtime.
RUN mkdir -p /app/data && chown app:app /app/data
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DATAWATCH_CONFIG=/app/config/warehouse.yml \
    DATAWATCH_DATA=/app/data \
    DATAWATCH_SCALE=1.0 \
    PORT=8855
USER app
EXPOSE 8855
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=3 \
    CMD ["datawatch", "healthcheck"]
CMD ["datawatch", "serve", "--host", "0.0.0.0", "--port", "8855"]
