# bookie-breaker-bookie-emulator

## Service Purpose

Python/FastAPI paper trading system. Accepts virtual bet placements, captures odds at placement time, grades bets on
game completion, and tracks performance metrics (ROI, win rate, CLV).

## Language & Conventions

- **Language:** Python 3.12
- **Framework:** FastAPI + uvicorn
- **Project layout:** `src/bookie_emulator/` package, `main.py` FastAPI entry point
- **Naming:** `snake_case.py` files, `snake_case` functions, `PascalCase` classes
- **Package manager:** uv
- **Testing:** pytest in `tests/`

## Key Files

- `src/bookie_emulator/main.py` — FastAPI app entry point (create_app factory + lifespan wiring)
- `src/bookie_emulator/api/` — Envelope, errors, schemas, cursor pagination, route handlers
- `src/bookie_emulator/core/` — Pure domain logic: odds math, bet grading, CLV, performance analytics
- `src/bookie_emulator/services/` — Bet placement, grading orchestration, fallback poller, bankroll, health
- `src/bookie_emulator/db/` — Async engine, SQLAlchemy Core tables, repositories
- `src/bookie_emulator/clients/` — statistics/lines service clients + game id reconciler
- `src/bookie_emulator/events/subscriber.py` — events:game.completed subscriber (automated grading)
- `alembic/` — Migrations for the `emulator` schema
- `pyproject.toml` — Dependencies and tool config

## Service-Specific Commands

```bash
task dev          # uvicorn with --reload on port 8005
task lint         # ruff check + format
task test         # pytest --cov (integration tests need Docker for testcontainers)
task typecheck    # mypy src/
task db:migrate   # alembic upgrade head against the emulator schema
task spec:export  # export OpenAPI artifact to bookie-breaker-docs
```

## Dependencies

- **PostgreSQL** (emulator schema) — Paper bets, grades, bankroll snapshots
- **Redis** — Subscribes to `events:game.completed` for bet grading
- **lines-service** (port 8001) — Captures current odds at bet placement, closing lines for CLV
- **statistics-service** (port 8002) — Game results for grading

## Environment Variables

See `.env.example`. Key: `DATABASE_URL`, `LINES_SERVICE_URL`, `STATISTICS_SERVICE_URL`, `REDIS_URL`,
`STARTING_BANKROLL_UNITS`, `UNIT_SIZE_DOLLARS`, `GRADING_POLL_SECONDS`, `PORT=8005`.
