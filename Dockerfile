# two-stage uv build (rally/puffin pattern). deps resolve from the lockfile
# alone; the runtime stage carries only the venv plus source and assets.
FROM python:3.14-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen

FROM python:3.14-slim
WORKDIR /app
COPY --from=builder /app/.venv .venv
COPY src/ src/
COPY templates/ templates/
COPY static/ static/
COPY migrations/ migrations/
COPY prompts/ prompts/
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/src" \
    PYTHONUNBUFFERED=1 \
    TATTOO_DB_PATH=/data/tattoo.db \
    TATTOO_DIST_PATH=/dist
EXPOSE 8000
VOLUME /data
VOLUME /dist
# no HEALTHCHECK by decision (plan §9): logs are the observability surface
# started in-process rather than via the uvicorn CLI so the json log config
# is installed before the server configures its own plain-text logging
CMD ["python", "-m", "tattoo.serve"]
