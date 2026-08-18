#!/usr/bin/env python3
"""
The edge pipeline, asserted (issue #59).

    all edges in viewport
      → density filter        (spatially random, deterministic per edge)
      → EDGESET A             → tissue-graph layer
      → sending filter
      → receiving filter
      → edge-table filters
      → EDGESET B             → edge-data layer

Three properties fall out, and all three are things a plausible refactor breaks
silently rather than loudly:

1. **The tissue graph is ground truth.** Its count must not move no matter what
   filters are set on the edge data. `query_structure` takes no filter arguments
   at all, so this is really asserting that nobody wired one in.
2. **B is a subset of A, at every density.** Edge data must never be drawn where
   the graph beneath it was sampled away. This is why density is a deterministic
   hash of the edge id rather than `USING SAMPLE`: two independent bernoulli
   draws at 10% overlap only ~1% of the time.
3. **Sampling is stable.** The same request twice returns the same edges, or the
   layer flickers on every pan.

Run against whatever datasets are present:

    python3 tests/edge_pipeline_check.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATA_ROOT", str(Path(__file__).resolve().parents[2] / "sample_data"))

from app.readers.metadata_filter import MetadataFilter          # noqa: E402
from app.routers import spatial, edges as edges_router          # noqa: E402

DATA_ROOT = Path(os.environ["DATA_ROOT"])
DENSITIES = (1.0, 0.5, 0.1)
failures: list[str] = []


def usable_field(frame):
    """A metadata column with real, varied values — not an all-NaN one."""
    for c in frame.columns:
        if c in ("x_centroid", "y_centroid", "cell_id"):
            continue
        if frame[c].notna().any() and frame[c].nunique() > 1:
            return c
    return None


def spec_for(reader, field):
    # color_values(mode, field, genes, categorical) — passing field positionally
    # into `genes` silently returns the empty result, which made spec_for return
    # None and skipped the subset assertion below on most datasets.
    cv = reader.color_values("metadata", field)
    if cv.get("categories"):
        return MetadataFilter.build(field, values=cv["categories"][:1])
    lo, hi = cv.get("min"), cv.get("max")
    if lo is None or hi is None or lo == hi:
        return None
    return MetadataFilter.build(field, vmin=lo + (hi - lo) * 0.4, vmax=hi)


def check(ds: str) -> str:
    sr = spatial._reader(ds)
    er = edges_router._reader(ds, "edges.parquet")
    field = usable_field(sr._metadata_frame())
    spec = spec_for(sr, field) if field else None
    ids = sr.filter_cell_ids(spec) if spec else None

    baseline = len(er.query_structure(density=1.0))
    for d in DENSITIES:
        A = {r["edge"] for r in er.query_structure(density=d)}
        B = {r["edge"] for r in er.query_grouped(density=d)}
        if B != A:
            failures.append(f"[{ds} d={d}] unfiltered edge data != graph "
                            f"({len(B)} vs {len(A)})")
        if ids:
            Bf = {r["edge"] for r in er.query_grouped(density=d, sending_ids=ids)}
            if not Bf <= A:
                failures.append(f"[{ds} d={d}] filtered edge data is not a subset "
                                f"of the graph ({len(Bf - A)} strays)")
        # Property 1: the graph is unmoved by anything the edge data did.
        if len(er.query_structure(density=1.0)) != baseline:
            failures.append(f"[{ds} d={d}] tissue graph count moved")

    # Property 3
    if {r["edge"] for r in er.query_structure(density=0.1)} != \
       {r["edge"] for r in er.query_structure(density=0.1)}:
        failures.append(f"[{ds}] sampling is not stable between identical calls")

    if ids:
        return f"{baseline:>8,} edges  filter=[{field}] -> {len(ids):,} cells"
    # Reported rather than passed over in silence: with no resolvable filter the
    # subset assertion above never runs, and a check that skips its own core
    # property while printing OK is worse than no check.
    return f"{baseline:>8,} edges  NO FILTER RESOLVED — subset assertion skipped"


found = sorted(p.name for p in DATA_ROOT.iterdir() if (p / "edges.parquet").exists())
if not found:
    print("no edge datasets present — nothing to check")
    sys.exit(0)

for ds in found:
    try:
        print(f"  OK   {ds:26} {check(ds)}")
    except Exception as exc:
        failures.append(f"[{ds}] {type(exc).__name__}: {exc}")
        print(f"  FAIL {ds:26} {type(exc).__name__}: {exc}")

print()
if failures:
    for f in failures:
        print("  ✗", f)
    sys.exit(1)
print(f"edge pipeline holds across {len(found)} datasets "
      f"at densities {', '.join(str(d) for d in DENSITIES)}.")
