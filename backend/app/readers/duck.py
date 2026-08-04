"""
Shared DuckDB helpers for parquet-backed readers.

Every reader that queries a large parquet file should go through here rather
than loading the file into pandas. The win is predicate pushdown: DuckDB reads
only the row groups whose statistics can satisfy the WHERE clause, so a viewport
query touches a fraction of the file instead of all of it.

The alternative — ``pq.read_table(path).to_pandas()`` followed by a pandas mask —
reads and materializes every row on every request, which is what made transcript
and boundary panning slow on full-size datasets.
"""
import math
import os
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

_MEMORY_LIMIT = os.getenv("DUCKDB_MEMORY_LIMIT", "8GB")
_THREADS = os.getenv("DUCKDB_THREADS", "4")


def connect() -> duckdb.DuckDBPyConnection:
    """Return a fresh, isolated DuckDB connection.

    A new connection per call is deliberate: DuckDB's default global connection
    is not thread-safe, and sharing it under FastAPI's threadpool produces empty
    or corrupt result sets rather than an error.
    """
    conn = duckdb.connect()
    conn.execute(f"SET memory_limit='{_MEMORY_LIMIT}'")
    conn.execute(f"SET threads={_THREADS}")
    return conn


def scan(path: Path) -> str:
    """SQL FROM-clause fragment that reads a parquet file.

    Single quotes in the path are escaped so a path like ``/data/o'brien/x.parquet``
    cannot terminate the string literal.
    """
    return "read_parquet('{}')".format(str(path).replace("'", "''"))


def columns(path: Path) -> set[str]:
    """Column names in a parquet file, read from its footer (no data scan)."""
    return set(pq.read_schema(path).names)


def bbox_predicate(x_col: str, y_col: str, bbox: tuple) -> tuple[str, list]:
    """Build a bounding-box WHERE fragment and its bind parameters.

    ``bbox`` must already be in the file's native coordinate space. Returns
    ``("", [])`` when the bbox is absent or has any None component, so callers
    can splice the result unconditionally.
    """
    if not bbox:
        return "", []
    xmin, ymin, xmax, ymax = bbox
    if None in (xmin, ymin, xmax, ymax):
        return "", []
    # Cast to builtin float: DuckDB cannot bind numpy scalars, which is what you
    # get whenever a bound comes from a pandas/numpy computation rather than the
    # router's float query params.
    return (
        f'("{x_col}" >= ? AND "{x_col}" <= ? AND "{y_col}" >= ? AND "{y_col}" <= ?)',
        [float(xmin), float(xmax), float(ymin), float(ymax)],
    )


def where_clause(conditions: list[str]) -> str:
    """Join conditions into a WHERE clause, or return '' when there are none."""
    conditions = [c for c in conditions if c]
    return f"WHERE {' AND '.join(conditions)}" if conditions else ""


def in_predicate(col: str, values: list) -> tuple[str, list]:
    """Build an IN (...) fragment. Empty values yield a never-true predicate."""
    if not values:
        return "FALSE", []
    placeholders = ", ".join("?" for _ in values)
    return f'"{col}" IN ({placeholders})', list(values)


# Sampling is seeded so that re-fetching an unchanged viewport returns the same
# rows. Without this, every refetch reshuffles which transcripts are drawn and
# the layer visibly flickers.
SAMPLE_SEED = 42


def reservoir_sample(n: int) -> str:
    """``USING SAMPLE`` clause drawing exactly n rows, or '' to keep all rows.

    Reservoir sampling gives an exact row count (unlike bernoulli, which gives an
    expected count), matching the pre-DuckDB ``df.sample(n=...)`` behaviour.
    Always attach this to a subquery wrapping the filtered SELECT — applied
    directly alongside a WHERE clause, DuckDB may sample before filtering.
    """
    if n <= 0:
        return ""
    return f"USING SAMPLE reservoir({int(n)} ROWS) REPEATABLE ({SAMPLE_SEED})"


def to_records(df) -> list[dict]:
    """DataFrame to JSON-safe records (NaN/Inf → None)."""
    return [
        {k: (None if isinstance(v, float) and not math.isfinite(v) else v)
         for k, v in row.items()}
        for row in df.to_dict(orient="records")
    ]
