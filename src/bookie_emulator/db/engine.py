"""Async engine construction with DSN translation.

DATABASE_URL uses the libpq-style form with a ``search_path`` query param
(matching the Go services' convention). asyncpg does not accept
``search_path`` as a URL parameter, so it is translated into a
``server_settings`` connect arg here.
"""

from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

DEFAULT_SEARCH_PATH = "emulator,public"


def _split_dsn(database_url: str) -> tuple[str, str]:
    """Return (sqlalchemy_url, search_path) from a libpq-style DSN."""
    parts = urlsplit(database_url)
    query = parse_qs(parts.query)
    search_path = ",".join(query.pop("search_path", [DEFAULT_SEARCH_PATH]))
    scheme = "postgresql+asyncpg"
    rebuilt = urlunsplit((scheme, parts.netloc, parts.path, urlencode(query, doseq=True), parts.fragment))
    return rebuilt, search_path


def create_engine(database_url: str, pool_size: int = 5) -> AsyncEngine:
    url, search_path = _split_dsn(database_url)
    return create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=5,
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": search_path}},
    )
