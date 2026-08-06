"""
Shared loader for user-supplied supplemental metadata.

Two features use the identical pattern, and share this code so they cannot drift:

  {dataset}/cell-metadata/   keyed on `cell_id`  — extra per-cell columns
  {dataset}/edge-metadata/   keyed on `edge`     — extra per-edge columns

In both cases the user drops CSV / CSV.GZ / parquet into a subdirectory, the files
are outer-joined on the key, and the resulting columns surface automatically in the
corresponding color-by dropdown. The only difference is the name of the key column,
so everything here is parameterised on it.

Key-column resolution is deliberately forgiving, because the dominant source of
these files is R:

    write.csv(df, "cell-metadata/clusters.csv")   # row.names=TRUE is R's default

which produces a leading unnamed column of barcodes that pandas surfaces as
``Unnamed: 0``.
"""
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

_SUPPORTED = (".csv", ".csv.gz", ".parquet")


def read_table_with_key(path: Path, key: str, log_prefix: str) -> Optional[pd.DataFrame]:
    """Read one metadata file and promote its identifier column to `key`.

    Returns None when the file cannot be read or no identifier column can be
    identified — callers skip it rather than failing the whole request, so one
    malformed file never takes out the rest.
    """
    name = path.name.lower()

    if name.endswith(".parquet"):
        try:
            df = pd.read_parquet(path)
        except Exception as exc:
            print(f"[{log_prefix}] warning: could not read {path.name}: {exc}")
            return None
        if key not in df.columns:
            print(f"[{log_prefix}] skip {path.name}: no '{key}' column")
            return None
        return df

    if not (name.endswith(".csv") or name.endswith(".csv.gz")):
        return None

    # The common case: an R export whose first column is unnamed row names.
    # index_col=0 handles that, and also an explicitly-named leading id column.
    try:
        df = pd.read_csv(path, index_col=0)
        df.index.name = key
        return df.reset_index()
    except Exception:
        pass

    try:
        df = pd.read_csv(path)
    except Exception as exc:
        print(f"[{log_prefix}] warning: could not read {path.name}: {exc}")
        return None
    if key in df.columns:
        return df
    if "Unnamed: 0" in df.columns:
        return df.rename(columns={"Unnamed: 0": key})
    first = df.columns[0]
    if df[first].dtype == object and df[first].is_unique:
        return df.rename(columns={first: key})
    print(f"[{log_prefix}] skip {path.name}: cannot identify '{key}' column")
    return None


def load_supplemental(
    candidate_files: list[Path],
    key: str,
    log_prefix: str,
    coerce_key_to_str: bool = True,
) -> Optional[pd.DataFrame]:
    """Read and outer-join every supplied metadata file on `key`.

    Returns None when nothing usable was found, so callers can treat "no
    supplemental metadata" and "no such directory" identically.
    """
    frames: list[pd.DataFrame] = []
    for f in candidate_files:
        df = read_table_with_key(f, key, log_prefix)
        if df is None:
            continue
        try:
            if coerce_key_to_str:
                df[key] = df[key].astype(str)
            frames.append(df)
            print(f"[{log_prefix}] loaded supplemental metadata: {f.name} "
                  f"({len(df)} rows, {len(df.columns) - 1} extra columns)")
        except Exception as exc:
            print(f"[{log_prefix}] warning: could not load {f.name}: {exc}")

    if not frames:
        return None

    merged = frames[0]
    for frame in frames[1:]:
        # Only bring in columns we do not already have, so the join cannot produce
        # pandas' _x/_y suffixes and silently rename a user's column.
        new_cols = [key] + [c for c in frame.columns if c not in merged.columns]
        merged = merged.merge(frame[new_cols], on=key, how="outer")
    return merged


def collect_files(
    directory: Path,
    root: Optional[Path] = None,
    root_filter: Optional[Callable[[str], bool]] = None,
) -> list[Path]:
    """Candidate metadata files: everything in `directory`, plus optionally plain
    root-level CSVs that `root_filter` accepts.

    The root sweep exists because Xenium users habitually drop a metadata CSV
    beside the platform output rather than in a subdirectory; `root_filter` is how
    a reader excludes its own files from that sweep.
    """
    files: list[Path] = []
    if directory.is_dir():
        files.extend(sorted(directory.iterdir()))
    if root is not None and root_filter is not None:
        for f in sorted(root.iterdir()):
            if f.is_file() and f.name.lower().endswith(".csv") and root_filter(f.name.lower()):
                files.append(f)
    return files
