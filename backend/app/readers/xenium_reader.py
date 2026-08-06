"""
Reads standard Xenium output folders (v2/v3/v4).
All heavy I/O is deferred to per-method calls — no data is loaded at init.
"""
import json
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from app.readers import duck, spatial_cache
from app.readers.base_reader import _UNSET, SpatialDatasetReader

# Hard ceiling on transcripts returned in one response, independent of `fraction`.
# Guards against a request for fraction=1.0 over a whole-tissue viewport trying to
# serialize tens of millions of rows.
_MAX_TRANSCRIPTS = 200_000


class XeniumReader(SpatialDatasetReader):

    def __init__(self, dataset_path: Path):
        super().__init__(dataset_path)
        self._pixel_size: Optional[float] = None
        self._cells_full_cache = _UNSET

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def platform(self) -> str:
        return "xenium"

    @property
    def pixel_size(self) -> float:
        if self._pixel_size is None:
            meta = self.info()
            self._pixel_size = float(meta.get("pixel_size", 1.0))
        return self._pixel_size

    # ── Experiment metadata ───────────────────────────────────────────────────

    def info(self) -> dict:
        meta_file = self.path / "experiment.xenium"
        if not meta_file.exists():
            return {"platform": self.platform, "error": "experiment.xenium not found"}
        with open(meta_file) as f:
            data = json.load(f)
        data["platform"] = self.platform
        return data

    # ── Gene catalogue ────────────────────────────────────────────────────────

    def gene_list(self) -> list[str]:
        skip = ("Blank", "NegControl", "Unassigned", "DEPRECATED",
                "NegControlCodeword", "NegControlProbe", "antisense")

        # Primary: read gene names from cell_feature_matrix.h5
        h5 = self.path / "cell_feature_matrix.h5"
        if h5.exists():
            try:
                import h5py
                with h5py.File(h5, "r") as f:
                    names = f["matrix/features/name"][()].astype(str).tolist()
                filtered = [g for g in names if not any(g.startswith(p) for p in skip)]
                if filtered:
                    return filtered
            except Exception:
                pass

        # Fallback: extract unique feature_name values from transcripts.parquet.
        # This handles datasets exported without the cell feature matrix H5.
        tx = self.path / "transcripts.parquet"
        if tx.exists():
            try:
                import pyarrow.parquet as pq
                col = pq.read_table(tx, columns=["feature_name"])["feature_name"]
                names = sorted({v.as_py() for v in col if v.is_valid})
                return [g for g in names if not any(g.startswith(p) for p in skip)]
            except Exception:
                pass

        return []

    # ── Transcripts ───────────────────────────────────────────────────────────

    def transcripts(
        self,
        bbox: Optional[tuple] = None,
        genes: Optional[list[str]] = None,
        fraction: float = 1.0,
    ) -> dict:
        """Transcript detections in pixel space, bbox- and gene-filtered.

        Queried through DuckDB so the bbox and gene predicates push down into the
        parquet scan. Only matching row groups are read; a zoomed-in viewport on a
        multi-GB transcripts.parquet touches a small fraction of the file.

        ``total`` is the count *after* filtering but *before* sampling, because
        the frontend uses it to report "showing N of M" and to calibrate density.
        """
        path = self.path / "transcripts.parquet"
        if not path.exists():
            return {"transcripts": [], "total": 0}

        cols = duck.columns(path)
        if not {"x_location", "y_location"} <= cols:
            return {"transcripts": [], "total": 0}
        # Query the spatially-sorted copy when one exists, so the bbox predicate
        # can actually skip row groups. Falls back to `path` transparently.
        path = spatial_cache.sorted_path(
            path, self.path, "x_location", "y_location") or path
        # qv is absent from some exports — select only what the file actually has.
        select_cols = [c for c in ("x_location", "y_location", "feature_name", "qv")
                       if c in cols]
        select = ", ".join(f'"{c}"' for c in select_cols)

        conditions: list[str] = []
        params: list = []

        bbox_sql, bbox_params = duck.bbox_predicate(
            "x_location", "y_location",
            self._bbox_to_native(bbox) if bbox else None,
        )
        if bbox_sql:
            conditions.append(bbox_sql)
            params.extend(bbox_params)

        if genes and "feature_name" in cols:
            gene_sql, gene_params = duck.in_predicate("feature_name", genes)
            conditions.append(gene_sql)
            params.extend(gene_params)

        where = duck.where_clause(conditions)
        src = duck.scan(path)

        with duck.connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM {src} {where}", params
            ).fetchone()[0]
            total = int(total or 0)
            if total == 0:
                return {"transcripts": [], "total": 0}

            fraction = max(0.0001, min(1.0, fraction))
            sample_n = min(round(fraction * total), _MAX_TRANSCRIPTS)
            if sample_n <= 0:
                return {"transcripts": [], "total": total}

            sample = duck.reservoir_sample(sample_n) if sample_n < total else ""
            df = conn.execute(
                f"SELECT * FROM (SELECT {select} FROM {src} {where}) {sample}",
                params,
            ).df()

        ps = self.pixel_size
        df["x_location"] = df["x_location"] / ps
        df["y_location"] = df["y_location"] / ps
        return {"transcripts": duck.to_records(df), "total": total}

    # ── Cells ─────────────────────────────────────────────────────────────────

    def cells(self, bbox: Optional[tuple] = None) -> list[dict]:
        df = self._read_parquet("cells.parquet")
        if df is None:
            csv = self.path / "cells.csv.gz"
            df = pd.read_csv(csv) if csv.exists() else None
        if df is None:
            return []
        x_col = next((c for c in df.columns if "x_centroid" in c), None)
        y_col = next((c for c in df.columns if "y_centroid" in c), None)
        if bbox and x_col and y_col:
            xmin, ymin, xmax, ymax = self._bbox_to_native(bbox)
            if None not in (xmin, ymin, xmax, ymax):
                df = df[
                    (df[x_col] >= xmin) & (df[x_col] <= xmax) &
                    (df[y_col] >= ymin) & (df[y_col] <= ymax)
                ]
        df = df.copy()
        if x_col:
            df[x_col] = df[x_col] / self.pixel_size
        if y_col:
            df[y_col] = df[y_col] / self.pixel_size
        return self._to_records(df)

    def cells_schema(self) -> dict:
        try:
            df = self._cells_full()
        except Exception as exc:
            print(f"[xenium_reader] cells_schema fallback: {exc}")
            df = self._read_parquet("cells.parquet")
        if df is None:
            return {"columns": {}}
        return {
            "columns": {
                col: str(df[col].dtype)
                for col in df.columns
                if col != "cell_id"
            }
        }

    # ── Cell boundaries ───────────────────────────────────────────────────────

    def cell_boundaries(self, bbox: Optional[tuple] = None, fraction: float = 1.0) -> dict:
        """Cell polygon vertices in pixel space for cells visible in the bbox.

        Selection is per *cell*, not per vertex. A cell qualifies if any one of its
        vertices falls in the bbox, and then **all** of its vertices are returned.
        That matters at the viewport edge: filtering vertices directly (the previous
        behaviour) clipped boundary cells into partial polygons that rendered as
        torn shapes. Sampling likewise draws whole cells, so a sampled cell is never
        missing part of its outline.

        ``total`` is the number of distinct cells touching the bbox before sampling —
        ``useCellBoundaries`` divides its ~5K target by this to pick the next fraction,
        so it has to stay a pre-sample count.
        """
        path = self.path / "cell_boundaries.parquet"
        if not path.exists():
            return {"boundaries": [], "total": 0}

        cols = duck.columns(path)
        x_col = next((c for c in cols if "vertex_x" in c), None)
        y_col = next((c for c in cols if "vertex_y" in c), None)
        if not x_col or not y_col or "cell_id" not in cols:
            return {"boundaries": [], "total": 0}
        path = spatial_cache.sorted_path(path, self.path, x_col, y_col) or path

        bbox_sql, bbox_params = duck.bbox_predicate(
            x_col, y_col, self._bbox_to_native(bbox) if bbox else None
        )
        where = duck.where_clause([bbox_sql])
        src = duck.scan(path)
        select = f'"cell_id", "{x_col}", "{y_col}"'

        with duck.connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(DISTINCT cell_id) FROM {src} {where}", bbox_params
            ).fetchone()[0]
            total = int(total or 0)
            if total == 0:
                return {"boundaries": [], "total": 0}

            fraction = max(0.0001, min(1.0, fraction))
            sample_n = round(fraction * total)
            if sample_n <= 0:
                return {"boundaries": [], "total": total}

            # Resolve the visible cell ids first, sample among them, then fetch
            # every vertex belonging to a surviving id. The second scan re-reads
            # the parquet, but both scans are predicate-pushed and together still
            # read far less than materializing the whole file.
            sample = duck.reservoir_sample(sample_n) if sample_n < total else ""
            df = conn.execute(
                f"""
                WITH visible AS (
                    SELECT DISTINCT cell_id FROM {src} {where}
                ),
                keep AS (
                    SELECT cell_id FROM visible {sample}
                )
                SELECT {select} FROM {src}
                WHERE cell_id IN (SELECT cell_id FROM keep)
                """,
                bbox_params,
            ).df()

        ps = self.pixel_size
        df[x_col] = df[x_col] / ps
        df[y_col] = df[y_col] / ps
        return {"boundaries": duck.to_records(df), "total": total}

    # ── Expression ────────────────────────────────────────────────────────────

    def cell_expression(self, cell_id: str) -> dict:
        h5 = self.path / "cell_feature_matrix.h5"
        if not h5.exists():
            return {}
        try:
            import h5py
            import scipy.sparse as sp
            with h5py.File(h5, "r") as f:
                barcodes = f["matrix/barcodes"][()].astype(str).tolist()
                if cell_id not in barcodes:
                    return {}
                idx = barcodes.index(cell_id)
                gene_names = f["matrix/features/name"][()].astype(str).tolist()
                data = f["matrix/data"][()]
                indices = f["matrix/indices"][()]
                indptr = f["matrix/indptr"][()]
                mat = sp.csc_matrix(
                    (data, indices, indptr),
                    shape=(len(gene_names), len(barcodes)),
                )
                col = mat.getcol(idx).toarray().flatten()
            return {gene_names[i]: int(col[i]) for i in range(len(col)) if col[i] > 0}
        except Exception:
            return {}

    def cell_detail(self, cell_id: str) -> Optional[dict]:
        df = self._cells_full()
        if df is None:
            return None
        row = df[df["cell_id"] == cell_id]
        if row.empty:
            return None
        record = self._to_records(row)[0]
        ps = self.pixel_size
        for col in ("x_centroid", "y_centroid"):
            if col in record and record[col] is not None:
                record[col] = record[col] / ps
        record["expression"] = self.cell_expression(cell_id)
        return record

    def color_values(
        self,
        mode: str,
        field: Optional[str] = None,
        genes: Optional[list[str]] = None,
    ) -> dict:
        if mode == "gene_set":
            return self._color_values_gene_set(genes or [])
        return self._color_values_meta(field or "")

    def _color_values_gene_set(self, genes: list[str]) -> dict:
        h5 = self.path / "cell_feature_matrix.h5"
        if not h5.exists() or not genes:
            return {"type": "continuous", "values": {}, "min": 0.0, "max": 0.0}
        try:
            import h5py
            import scipy.sparse as sp
            with h5py.File(h5, "r") as f:
                barcodes = f["matrix/barcodes"][()].astype(str).tolist()
                gene_names = f["matrix/features/name"][()].astype(str).tolist()
                gene_set = set(genes)
                indices_to_sum = [i for i, g in enumerate(gene_names) if g in gene_set]
                if not indices_to_sum:
                    return {"type": "continuous", "values": {}, "min": 0.0, "max": 0.0}
                data = f["matrix/data"][()]
                idx = f["matrix/indices"][()]
                indptr = f["matrix/indptr"][()]
                mat = sp.csc_matrix(
                    (data, idx, indptr),
                    shape=(len(gene_names), len(barcodes)),
                )
                summed = np.asarray(mat[indices_to_sum, :].sum(axis=0)).flatten()
            vmax = float(summed.max()) if summed.max() > 0 else 1.0
            values = {barcodes[i]: float(summed[i]) for i in range(len(barcodes))}
            return {"type": "continuous", "values": values, "min": 0.0, "max": vmax}
        except Exception:
            return {"type": "continuous", "values": {}, "min": 0.0, "max": 0.0}

    def _color_values_meta(self, field: str) -> dict:
        df = self._cells_full()
        if df is None or field not in df.columns:
            return {"type": "continuous", "values": {}, "min": 0.0, "max": 0.0}
        col = df[field]
        cell_ids = df["cell_id"].astype(str).tolist()
        has_value = col.notna()
        is_categorical = (
            pd.api.types.is_string_dtype(col) or
            pd.api.types.is_object_dtype(col) or
            (pd.api.types.is_integer_dtype(col) and col.nunique() <= 30)
        )
        if is_categorical:
            categories = sorted(col[has_value].astype(str).unique().tolist())
            values = {
                cell_ids[i]: str(col.iloc[i])
                for i in range(len(cell_ids)) if has_value.iloc[i]
            }
            return {"type": "categorical", "values": values, "categories": categories}
        valid = col[has_value]
        if valid.empty:
            return {"type": "continuous", "values": {}, "min": 0.0, "max": 0.0}
        values = {
            cell_ids[i]: float(col.iloc[i])
            for i in range(len(cell_ids)) if has_value.iloc[i]
        }
        return {"type": "continuous", "values": values,
                "min": float(valid.min()), "max": float(valid.max())}

    # ── Supplemental metadata ─────────────────────────────────────────────────
    # The loader itself lives on SpatialDatasetReader so every platform gets it.
    # All Xenium contributes is the list of its own root CSVs to ignore.

    # Plain-CSV filenames in the dataset root that are standard Xenium outputs.
    # .csv.gz files are always skipped at root (exclusively Xenium data files).
    _ROOT_CSV_SKIP = frozenset({
        "cells.csv", "transcripts.csv", "metrics_summary.csv",
        "analysis_summary.csv", "gene_panel.csv",
    })

    def _cells_full(self) -> Optional[pd.DataFrame]:
        """cells.parquet merged with supplemental metadata. Cached."""
        if self._cells_full_cache is not _UNSET:
            return self._cells_full_cache  # type: ignore[return-value]
        self._cells_full_cache = self._merge_supplemental(
            self._read_parquet("cells.parquet")
        )
        return self._cells_full_cache  # type: ignore[return-value]

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _read_parquet(self, filename: str, columns: Optional[list[str]] = None) -> Optional[pd.DataFrame]:
        path = self.path / filename
        if not path.exists():
            return None
        try:
            return pq.read_table(path, columns=columns).to_pandas()
        except Exception:
            return pd.read_parquet(path, columns=columns)
