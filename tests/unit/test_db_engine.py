"""DSN translation for the async engine: libpq-style search_path params are
moved into asyncpg server_settings (matching the Go services' convention)."""

from sqlalchemy.ext.asyncio import AsyncEngine

from bookie_emulator.db.engine import DEFAULT_SEARCH_PATH, _split_dsn, create_engine


class TestSplitDsn:
    def test_extracts_search_path_and_rewrites_scheme(self) -> None:
        url, search_path = _split_dsn("postgres://svc:pw@db:5432/bookie?search_path=emulator,public")
        assert url == "postgresql+asyncpg://svc:pw@db:5432/bookie"
        assert search_path == "emulator,public"

    def test_defaults_search_path_when_absent(self) -> None:
        url, search_path = _split_dsn("postgres://svc:pw@db:5432/bookie")
        assert url == "postgresql+asyncpg://svc:pw@db:5432/bookie"
        assert search_path == DEFAULT_SEARCH_PATH

    def test_preserves_other_query_params(self) -> None:
        url, search_path = _split_dsn("postgresql://svc:pw@db/bookie?search_path=emulator&application_name=emu")
        assert url == "postgresql+asyncpg://svc:pw@db/bookie?application_name=emu"
        assert search_path == "emulator"


class TestCreateEngine:
    def test_builds_async_engine_with_translated_dsn(self) -> None:
        engine = create_engine("postgres://svc:pw@db:5432/bookie?search_path=emulator,public", pool_size=2)
        assert isinstance(engine, AsyncEngine)
        assert engine.url.drivername == "postgresql+asyncpg"
        assert engine.url.database == "bookie"
        assert "search_path" not in engine.url.query
