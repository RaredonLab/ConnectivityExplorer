#!/usr/bin/env python3
"""
DuckDB is configured correctly however DUCKDB_MEMORY_LIMIT is (or is not) set.

Why this exists
---------------
`DUCKDB_MEMORY_LIMIT` stopped having a fixed default in v0.8.4, because a flat
`8GB` on a stock ~8 GB Docker Desktop VM let DuckDB take all of RAM and the
spatial-index build was OOM-killed mid-request. Both compose files now pass it
through empty so the limit is sized from memory actually available.

That broke every `/edges` endpoint. `edge_reader.py` had its own connection
setup reading `os.getenv("DUCKDB_MEMORY_LIMIT", "8GB")` — and that default only
applies when the variable is *absent*. Set-but-empty yields `""`, so the driver
got `SET memory_limit=''` and raised

    duckdb.duckdb.ParserException: Parser Error: Memory limit must have a number

on every edge query. Two modules defaulting the same environment variable two
different ways is the actual defect; EdgeReader now goes through
`duck.connect()`, so there is one rule.

The golden snapshot cannot catch this: it exercises readers in whatever
environment it happens to run in, and the failure only appears when the variable
is set to an empty string.

    python3 tests/duckdb_config_check.py
"""
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DATA_ROOT = Path(os.getenv("DATA_ROOT", Path(__file__).resolve().parents[2] / "sample_data"))

# The values that have actually appeared in a compose file or a shell, plus the
# shapes a user might reasonably put in .env.prod.
CASES = [
    ("unset", None),
    ("empty", ""),          # ${DUCKDB_MEMORY_LIMIT:-} — the one that broke edges
    ("blank", "   "),
    ("explicit", "512MB"),
]

failures = []


def check_connect(label):
    """A connection must be usable, whatever the environment says."""
    import importlib
    from app.readers import duck
    importlib.reload(duck)
    limit = duck._MEMORY_LIMIT
    with duck.connect() as conn:
        conn.execute("SELECT 1").fetchone()
    # A bare number is not a valid DuckDB memory limit either.
    if not any(limit.upper().endswith(u) for u in ("KB", "MB", "GB", "TB", "B")):
        failures.append(f"[{label}] memory_limit {limit!r} has no unit suffix")
    return limit


def check_edges(label):
    """Every edge endpoint shares one connection helper — exercise a real query."""
    from app.readers.edge_reader import EdgeReader
    found = sorted(DATA_ROOT.glob("*/edges.parquet"))
    if not found:
        return "no edge datasets present — skipped"
    src = found[0]
    reader = EdgeReader(src, pixel_size=1.0)
    n = len(reader.lrm_catalogue())
    reader.query_grouped(bbox=None, density=1.0)
    return f"{src.parent.name}: {n} LRMs"


for label, value in CASES:
    if value is None:
        os.environ.pop("DUCKDB_MEMORY_LIMIT", None)
    else:
        os.environ["DUCKDB_MEMORY_LIMIT"] = value
    try:
        limit = check_connect(label)
        detail = check_edges(label)
        print(f"  OK   {label:<9} {str(value)!r:<10} -> memory_limit={limit:<10} {detail}")
    except Exception as exc:
        failures.append(f"[{label}] {type(exc).__name__}: {exc}")
        print(f"  FAIL {label:<9} {str(value)!r:<10} -> {type(exc).__name__}: {exc}")
        traceback.print_exc()

print()
if failures:
    for f in failures:
        print("  ✗", f)
    sys.exit(1)
print("DuckDB configuration is valid for every DUCKDB_MEMORY_LIMIT form.")
