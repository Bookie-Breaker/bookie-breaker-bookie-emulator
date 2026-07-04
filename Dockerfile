FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ src/

FROM python:3.12-slim-bookworm

RUN useradd --uid 10001 --create-home appuser

WORKDIR /app

COPY --from=builder --chown=appuser:appuser /app/.venv .venv
COPY --from=builder --chown=appuser:appuser /app/src src

USER appuser

ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app/src

EXPOSE 8005

CMD ["uvicorn", "bookie_emulator.main:app", "--host", "0.0.0.0", "--port", "8005"]
