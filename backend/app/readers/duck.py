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
import tempfile
from pathlib import Path
from typing import Optional

import duckdb
import pyarrow.parquet as pq

def _available_memory_bytes() -> Optional[int]:
    """Memory this process may actually use, or None if it cannot be determined.

    Takes the **minimum** of the cgroup limit and physical RAM, because either
    can be the real ceiling and they routinely disagree. On Docker Desktop the
    container limit is whatever compose declares (12 GB here) while the Linux VM
    hosting it may have far less (7.8 GB) — trusting the cgroup alone invites the
    VM's OOM killer, which kills the process without the container ever reporting
    OOMKilled.
    """
    limits = []
    for p in ("/sys/fs/cgroup/memory.max",                    # cgroup v2
              "/sys/fs/cgroup/memory/memory.limit_in_bytes"):  # cgroup v1
        try:
            raw = Path(p).read_text().strip()
            if raw and raw != "max":
                v = int(raw)
                # v1 reports a sentinel near 2^63 to mean "unlimited".
                if 0 < v < (1 << 62):
                    limits.append(v)
        except (OSError, ValueError):
            pass
    try:
        limits.append(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (OSError, ValueError, AttributeError):
        pass
    return min(limits) if limits else None


def _default_memory_limit() -> str:
    """A memory cap that leaves room for everything else in the process.

    The old default was a flat ``8GB`` regardless of the machine. That is not a
    cap at all on a standard 8 GB Docker Desktop VM — it authorises DuckDB to
    take essentially all of RAM, and a large sort then dies to the OOM killer
    mid-request, taking uvicorn with it. Sizing from what is actually present
    keeps the same intent (bound the scan) while making the bound real.

    60% leaves headroom for the Python process, pyvips tile builds, and the
    page cache the parquet scan itself depends on.
    """
    total = _available_memory_bytes()
    if not total:
        return "4GB"                       # unknowable → conservative, not greedy
    mb = max(1024, int(total * 0.60 / (1024 * 1024)))
    return f"{mb}MB"


# .strip() matters: an unset variable and one set to "" or "   " must all fall
# through to the computed default. A whitespace value is truthy, so without it
# DuckDB receives `SET memory_limit='   '` and raises a ParserException.
_MEMORY_LIMIT = (os.getenv("DUCKDB_MEMORY_LIMIT") or "").strip() or _default_memory_limit()
_THREADS = os.getenv("DUCKDB_THREADS", "4")


def _temp_dir() -> Optional[str]:
    """Writable scratch directory for DuckDB to spill to, or None.

    Without this an in-memory DuckDB cannot spill, so any operation whose working
    set exceeds ``memory_limit`` fails outright instead of going out-of-core. The
    spatial-index build sorts the entire transcripts file, which on a real Xenium
    run is 132M rows — far past any sane cap — so spilling is what makes that
    build possible at all rather than merely slower.

    CACHE_DIR is the right home: it is the one writable volume the backend owns
    (/data is mounted read-only), and it already holds derived artifacts.
    """
    base = os.getenv("CACHE_DIR") or tempfile.gettempdir()
    try:
        d = Path(base) / "duckdb-tmp"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)
    except OSError:
        return None


_TEMP_DIR = _temp_dir()


def connect() -> duckdb.DuckDBPyConnection:
    """Return a fresh, isolated DuckDB connection.

    A new connection per call is deliberate: DuckDB's default global connection
    is not thread-safe, and sharing it under FastAPI's threadpool produces empty
    or corrupt result sets rather than an error.
    """
    conn = duckdb.connect()
    conn.execute(f"SET memory_limit='{_MEMORY_LIMIT}'")
    conn.execute(f"SET threads={_THREADS}")
    if _TEMP_DIR:
        conn.execute(f"SET temp_directory='{_TEMP_DIR}'")
    return conn


def scan(path: Path) -> str:
    """SQL FROM-clause fragment that reads a parquet file.

    Single quotes in the path are escaped so a path like ``/data/o'brien/x.parquet``
    cannot terminate the string literal.
    """
    return "read_parquet('{}')".format(str(path).replace("'", "''"))


def scan_csv(path: Path) -> str:
    """SQL FROM-clause fragment that reads a CSV file.

    Platforms that ship CSV instead of parquet (seqFISH) still stream through
    DuckDB rather than pandas. CSV has no column statistics so nothing can be
    pruned, but the scan is still streamed rather than materialized, which is
    what keeps peak memory flat on a multi-GB transcript list.
    """
    return "read_csv_auto('{}')".format(str(path).replace("'", "''"))


def scan_any(path: Path) -> str:
    """FROM-clause fragment for either a parquet or CSV source.

    Lets a caller stay agnostic about whether it is reading a platform's native
    CSV or a derived parquet (e.g. the spatially-sorted cache).
    """
    return scan_csv(path) if str(path).lower().endswith((".csv", ".csv.gz")) \
        else scan(path)


def columns(path: Path) -> set[str]:
    """Column names in a parquet file, read from its footer (no data scan)."""
    return set(pq.read_schema(path).names)


def csv_columns(path: Path) -> list[str]:
    """Column names of a CSV, read from the header row only."""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        header = fh.readline().rstrip("\r\n")
    return [c.strip().strip('"') for c in header.split(",")]


def bbox_predicate(x_col: str, y_col: str, bbox: tuple) -> tuple[str, list]:
    """Build a bounding-box WHERE fragment and its bind parameters.

    ``bbox`` must already be in the file's native coordinate space. Returns
    ``("", [])`` when the bbox is absent or has any None component, so callers
    can splice the result unconditionally.

    **The bounds are inlined as literals, not bound as ``?`` parameters, and that
    is load-bearing.** DuckDB prunes parquet row groups by comparing the filter
    against per-group statistics at plan time; with bound parameters the values
    are not yet known, so it cannot prune and scans everything. Measured on a
    40M-row spatially-sorted file, one viewport query:

        COUNT   literals 6.8 ms   vs  ? params 155 ms
        SELECT  literals  35 ms   vs  ? params 321 ms

    On an unsorted file the two are identical (~180 ms) because there is nothing
    to prune either way — which is why this only started to matter once the
    spatial cache existed. Revert this to bound parameters and the entire spatial
    index quietly stops paying for itself.

    Inlining is safe here because these are numbers, never user-controlled text:
    every value is passed through ``float()``, so nothing but a finite numeric
    literal can reach the SQL. Non-finite values are rejected rather than
    formatted, since ``inf``/``nan`` do not round-trip as SQL literals. String
    filters such as gene names must still go through ``in_predicate``.
    """
    if not bbox:
        return "", []
    xmin, ymin, xmax, ymax = bbox
    if None in (xmin, ymin, xmax, ymax):
        return "", []
    try:
        # float() also normalises numpy scalars, which DuckDB cannot bind and
        # whose repr() is not valid SQL.
        vals = [float(xmin), float(xmax), float(ymin), float(ymax)]
    except (TypeError, ValueError):
        return "", []
    if not all(math.isfinite(v) for v in vals):
        return "", []
    x0, x1, y0, y1 = (repr(v) for v in vals)
    return (
        f'("{x_col}" >= {x0} AND "{x_col}" <= {x1} AND '
        f'"{y_col}" >= {y0} AND "{y_col}" <= {y1})',
        [],
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


def register_ids(conn, ids, name: str = "tp_filter", col: str = "cell_id") -> str:
    """Register a set of ids as a relation and return a semi-join predicate on it.

    The metadata filter (issue #45) can keep hundreds of thousands of cells, which
    is far past the point where an ``IN (?, ?, …)`` list is workable — DuckDB has
    to bind every parameter, and the SQL text itself grows to megabytes. Handing
    the ids over as a one-column frame instead makes it an ordinary hash semi-join.

    ``ids`` must not be empty; an empty filter means "nothing matches" and callers
    should short-circuit rather than build a query that cannot return rows.
    """
    import pandas as pd

    frame = pd.DataFrame({col: [str(i) for i in ids]})
    conn.register(name, frame)
    return f"IN (SELECT \"{col}\" FROM {name})"


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
