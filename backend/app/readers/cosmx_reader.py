"""
Nanostring CosMx SMI dataset reader.

Expected output layout
----------------------
  *_tx_file.csv             — transcript positions: x_global_px, y_global_px, target
  *_metadata_file.csv       — cell metadata: cell_ID, x_centroid, y_centroid, ...
  *_fov_positions_file.csv  — field-of-view offsets (used to build global coords)
  CellComposite/            — per-FOV composite TIFF images (optional)
  CellLabels/               — per-FOV cell label TIFFs (optional)

Coordinates
-----------
CosMx transcripts are reported in global pixel coordinates (*_global_px columns).
Cell centroids in the metadata file are also in global pixel space.
pixel_size defaults to 0.18 µm/px (CosMx standard; may vary by run — check
experiment metadata if available).
"""
import glob
from pathlib import Path
from typing import Optional

import pandas as pd

from app.readers.base_reader import SpatialDatasetReader


_DEFAULT_PIXEL_SIZE = 0.18  # µm/px for standard CosMx output


class CosMxReader(SpatialDatasetReader):

    # CosMx prefixes every output with the experiment name (`<expt>_tx_file.csv`),
    # so the shared supplemental-metadata loader has to match by suffix rather than
    # by exact filename to avoid ingesting the platform's own tables.
    _ROOT_CSV_SKIP_SUFFIXES = (
        "_tx_file.csv", "_metadata_file.csv", "_fov_positions_file.csv",
        "_exprmat_file.csv",
    )

    def __init__(self, dataset_path: Path):
        super().__init__(dataset_path)
        self._cells_cache: Optional[pd.DataFrame] = None

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def platform(self) -> str:
        return "cosmx"

    @property
    def pixel_size(self) -> float:
        # CosMx does not embed pixel size in a standard manifest.
        # Override here if a run-specific value is available.
        return _DEFAULT_PIXEL_SIZE

    # ── Experiment metadata ───────────────────────────────────────────────────

    def info(self) -> dict:
        return {
            "platform": self.platform,
            "pixel_size": self.pixel_size,
            "path": str(self.path),
        }

    def capabilities(self) -> dict:
        return {
            "has_morphology": True,
            "has_transcripts": True,
            "has_boundaries": False,  # label TIFF boundary parsing not yet implemented
            "unit_label": "cell",
        }

    # ── Gene catalogue ────────────────────────────────────────────────────────

    def gene_list(self) -> list[str]:
        tx_file = self._find_file("*_tx_file.csv")
        if tx_file is None:
            return []
        try:
            df = pd.read_csv(tx_file, usecols=["target"], nrows=0)
            # Read just the unique targets from first chunk to avoid full load
            chunks = pd.read_csv(tx_file, usecols=["target"], chunksize=100_000)
            genes: set[str] = set()
            for chunk in chunks:
                genes.update(chunk["target"].dropna().unique())
            return sorted(g for g in genes if not g.startswith("Negative"))
        except Exception:
            return []

    # ── Transcripts ───────────────────────────────────────────────────────────

    def transcripts(
        self,
        bbox: Optional[tuple] = None,
        genes: Optional[list[str]] = None,
        fraction: float = 1.0,
    ) -> dict:
        tx_file = self._find_file("*_tx_file.csv")
        if tx_file is None:
            return {"transcripts": [], "total": 0}
        try:
            df = pd.read_csv(
                tx_file,
                usecols=["x_global_px", "y_global_px", "target"],
            )
            df = df.rename(columns={
                "x_global_px": "x_location",
                "y_global_px": "y_location",
                "target": "feature_name",
            })
            if bbox:
                # bbox is in pixel space; CosMx coords are already in pixels
                xmin, ymin, xmax, ymax = bbox
                df = df[
                    (df["x_location"] >= xmin) & (df["x_location"] <= xmax) &
                    (df["y_location"] >= ymin) & (df["y_location"] <= ymax)
                ]
            if genes:
                df = df[df["feature_name"].isin(genes)]
            total = len(df)
            fraction = max(0.0001, min(1.0, fraction))
            sample_n = min(round(fraction * total), 200_000)
            df = (df.sample(n=sample_n, random_state=42).copy()
                  if sample_n < total else df.copy())
            return {"transcripts": self._to_records(df), "total": total}
        except Exception:
            return {"transcripts": [], "total": 0}

    # ── Cells ─────────────────────────────────────────────────────────────────

    def _load_cells(self) -> Optional[pd.DataFrame]:
        if self._cells_cache is not None:
            return self._cells_cache
        meta_file = self._find_file("*_metadata_file.csv")
        if meta_file is None:
            return None
        try:
            df = pd.read_csv(meta_file)
            rename = {}
            for c in df.columns:
                lc = c.lower()
                if lc == "cell_id":
                    rename[c] = "cell_id"
                elif "x_centroid" in lc:
                    rename[c] = "x_centroid"
                elif "y_centroid" in lc:
                    rename[c] = "y_centroid"
            df = df.rename(columns=rename)
            if "cell_id" not in df.columns and "cell_ID" in df.columns:
                df = df.rename(columns={"cell_ID": "cell_id"})
            df["cell_id"] = df["cell_id"].astype(str)
            self._cells_cache = df
            return df
        except Exception:
            return None

    def cells(self, bbox: Optional[tuple] = None) -> list[dict]:
        df = self._load_cells()
        if df is None:
            return []
        if bbox and "x_centroid" in df.columns and "y_centroid" in df.columns:
            xmin, ymin, xmax, ymax = bbox
            df = df[
                (df["x_centroid"] >= xmin) & (df["x_centroid"] <= xmax) &
                (df["y_centroid"] >= ymin) & (df["y_centroid"] <= ymax)
            ]
        return self._to_records(df.copy())

    def cells_schema(self) -> dict:
        df = self._load_cells()
        if df is None:
            return {"columns": {}}
        return {"columns": {c: str(df[c].dtype) for c in df.columns if c != "cell_id"}}

    # ── Cell boundaries ───────────────────────────────────────────────────────

    def cell_boundaries(self, bbox: Optional[tuple] = None, fraction: float = 1.0) -> dict:
        # CosMx boundaries are per-FOV label TIFFs — not yet implemented
        return {"boundaries": [], "total": 0}

    # ── Expression ────────────────────────────────────────────────────────────

    def cell_expression(self, cell_id: str) -> dict:
        # CosMx does not provide a pre-computed cell × gene matrix in CSV form.
        # Expression must be aggregated from the transcript file — expensive at scale.
        # Return empty for now; implement with caching when needed.
        return {}

    def cell_detail(self, cell_id: str) -> Optional[dict]:
        df = self._load_cells()
        if df is None:
            return None
        row = df[df["cell_id"] == cell_id]
        if row.empty:
            return None
        record = self._to_records(row)[0]
        record["expression"] = self.cell_expression(cell_id)
        return record

    def color_values(
        self,
        mode: str,
        field: Optional[str] = None,
        genes: Optional[list[str]] = None,
    ) -> dict:
        if mode == "gene_set":
            # Gene-set coloring requires aggregating transcripts per cell — stub
            return {"type": "continuous", "values": {}, "min": 0.0, "max": 0.0}
        return self._color_values_meta(field or "")

    def _color_values_meta(self, field: str) -> dict:
        df = self._load_cells()
        if df is None or field not in df.columns:
            return {"type": "continuous", "values": {}, "min": 0.0, "max": 0.0}
        col = df[field]
        cell_ids = df["cell_id"].astype(str).tolist()
        is_categorical = (
            pd.api.types.is_string_dtype(col) or
            pd.api.types.is_object_dtype(col) or
            (pd.api.types.is_integer_dtype(col) and col.nunique() <= 30)
        )
        if is_categorical:
            labels = col.fillna("").astype(str).tolist()
            categories = sorted(col.dropna().astype(str).unique().tolist())
            return {"type": "categorical",
                    "values": {cell_ids[i]: labels[i] for i in range(len(cell_ids))},
                    "categories": categories}
        filled = col.fillna(0)
        return {"type": "continuous",
                "values": {cell_ids[i]: float(filled.iloc[i]) for i in range(len(cell_ids))},
                "min": float(filled.min()), "max": float(filled.max())}

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _find_file(self, pattern: str) -> Optional[Path]:
        """Return the first file matching a glob pattern in the dataset directory."""
        matches = sorted(self.path.glob(pattern))
        return matches[0] if matches else None
