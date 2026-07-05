# bookie-breaker-bookie-emulator

Paper trading system for placing virtual bets and tracking prediction performance.

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

API documentation available at `http://localhost:8005/docs` when running. The exported OpenAPI artifact lives in
`bookie-breaker-docs/api-contracts/openapi/bookie-emulator.yaml` (regenerate with `task spec:export`).

## Architecture Decisions

- [Tech Stack Selection (ADR-010)](https://github.com/Bookie-Breaker/bookie-breaker-docs/blob/main/decisions/010-tech-stack-selection.md)

## Environment Variables

See `.env.example` for all variables with descriptions.
