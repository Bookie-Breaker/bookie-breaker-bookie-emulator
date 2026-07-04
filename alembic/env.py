"""Alembic environment: async engine (asyncpg) driving sync-style migrations."""

import asyncio
import os

from sqlalchemy.engine import Connection

from alembic import context
from bookie_emulator.db.engine import create_engine
from bookie_emulator.db.tables import metadata

config = context.config

target_metadata = metadata

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://emulator_svc:localdev@localhost:5432/bookiebreaker?search_path=emulator,public",
)


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL.replace("postgres://", "postgresql://", 1),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table_schema="emulator",
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table_schema="emulator",
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_engine(DATABASE_URL)
    async with engine.connect() as connection:
        await connection.run_sync(_run_migrations)
        await connection.commit()
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
