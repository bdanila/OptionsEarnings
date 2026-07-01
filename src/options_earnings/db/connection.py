from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Iterator

import duckdb

_SCHEMA_TEXT: str | None = None


def _schema_sql() -> str:
    global _SCHEMA_TEXT
    if _SCHEMA_TEXT is None:
        _SCHEMA_TEXT = resources.files("options_earnings.db").joinpath("schema.sql").read_text(encoding="utf-8")
    return _SCHEMA_TEXT


def init_db(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(_schema_sql())


def open_db(path: str | Path) -> duckdb.DuckDBPyConnection:
    p = Path(path)
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(p))
    init_db(conn)
    return conn


@contextmanager
def get_conn(path: str | Path) -> Iterator[duckdb.DuckDBPyConnection]:
    conn = open_db(path)
    try:
        yield conn
    finally:
        conn.close()


def open_memory() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    init_db(conn)
    return conn
