# bookie-breaker-bookie-emulator

[![CI](https://img.shields.io/github/actions/workflow/status/Bookie-Breaker/bookie-breaker-bookie-emulator/ci.yml?branch=main&label=CI&logo=githubactions&logoColor=white)](https://github.com/Bookie-Breaker/bookie-breaker-bookie-emulator/actions/workflows/ci.yml)
[![coverage](https://img.shields.io/codecov/c/github/Bookie-Breaker/bookie-breaker-bookie-emulator?logo=codecov&logoColor=white)](https://app.codecov.io/gh/Bookie-Breaker/bookie-breaker-bookie-emulator)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)
![uv](https://img.shields.io/badge/uv-managed-DE5FE9?logo=uv&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)
![Redis](https://img.shields.io/badge/Redis-7-DC382D?logo=redis&logoColor=white)

Paper-trading engine (port 8005). Places virtual single bets and parlays with
`X-Idempotency-Key` deduplication, captures odds at placement time, auto-grades on
`events:game.completed` (per-leg for parlays), and tracks bankroll, ROI, win rate, and CLV
against closing lines. All state lives in the `emulator` schema (Alembic migrations).

## Quickstart

### With Docker Compose (recommended)

```bash
task up  # from BookieBreaker/ root
```

### Standalone

```bash
cp .env.example .env  # fill in values
task bootstrap
task db:migrate       # apply Alembic migrations to the emulator schema
task dev
```

## API

Interactive docs at `http://localhost:8005/docs` when running. All endpoints live under
`/api/v1/emulator`. The exported OpenAPI artifact lives in
`bookie-breaker-docs/api-contracts/openapi/bookie-emulator.yaml` (regenerate with
`task spec:export`).

Full contract:
[bookie-emulator-api.md](https://github.com/Bookie-Breaker/bookie-breaker-docs/blob/main/api-contracts/bookie-emulator-api.md)

## Architecture Decisions

- [Paper Trading Mode (ADR-003)](https://github.com/Bookie-Breaker/bookie-breaker-docs/blob/main/decisions/003-paper-trading-mode.md)
- [Parlay Data Model (ADR-028)](https://github.com/Bookie-Breaker/bookie-breaker-docs/blob/main/decisions/028-parlay-data-model.md)

Betting workflow from edge to graded bet:
[Finding and Betting Edges playbook](https://github.com/Bookie-Breaker/bookie-breaker-docs/blob/main/playbooks/03-finding-and-betting-edges.md)

## Environment Variables

See `.env.example` for all variables with descriptions. Key ones: `DATABASE_URL`,
`LINES_SERVICE_URL`, `STATISTICS_SERVICE_URL`, `REDIS_URL`, `STARTING_BANKROLL_UNITS`,
`UNIT_SIZE_DOLLARS`, `GRADING_POLL_SECONDS`, `PORT=8005`.
