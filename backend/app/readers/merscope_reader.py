"""
MERSCOPE (Vizgen MERFISH) dataset reader.

Expected output layout
----------------------
  cell_by_gene.csv          — cells × genes expression matrix (cell_id as first col)
  cell_metadata.csv         — EntityID, center_x, center_y, volume, ...
  detected_transcripts.csv  — x, y, z, gene, barcode_id, ...
  images/
    mosaic_*.tif            — multichannel morphology images
  manifest.json             — optional; contains microns_per_pixel

Coordinates
-----------
MERSCOPE outputs are in µm relative to the slide coordinate system.
The mosaic TIFF images have a known pixel scale (microns_per_pixel, typically
~0.108 µm/px) documented in manifest.json.  If the manifest is absent we fall
back to 0.108 as a safe default.
"""
import json
from pathlib import Path
from typing import Optional

import pandas as pd

from app.readers.base_reader import SpatialDatasetReader


_DEFAULT_PIXEL_SIZE = 0.108  # µm/px for standard MERSCOPE mosaic images


class MerscopeReader(SpatialDatasetReader):

    # Root CSVs that are MERSCOPE's own output, so the shared supplemental-metadata
    # loader never mistakes them for user-supplied columns.
    _ROOT_CSV_SKIP = frozenset({
        "cell_by_gene.csv", "cell_metadata.csv", "detected_transcripts.csv",
    })

    def __init__(self, dataset_path: Path):
        super().__init__(dataset_path)
        self._pixel_size: Optional[float] = None
        self._cells_cache: Optional[pd.DataFrame] = None

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def platform(self) -> str:
        return "merscope"

    @property
    def pixel_size(self) -> float:
        if self._pixel_size is None:
            manifest = self.path / "manifest.json"
            if manifest.exists():
                try:
                    with open(manifest) as f:
                        data = json.load(f)
                    self._pixel_size = float(
                        data.get("microns_per_pixel", _DEFAULT_PIXEL_SIZE)
                    )
                except Exception:
                    self._pixel_size = _DEFAULT_PIXEL_SIZE
            else:
                self._pixel_size = _DEFAULT_PIXEL_SIZE
        return self._pixel_size

    # ── Experiment metadata ───────────────────────────────────────────────────

    def info(self) -> dict:
        manifest = self.path / "manifest.json"
        data: dict = {"platform": self.platform}
        if manifest.exists():
            try:
                with open(manifest) as f:
                    data.update(json.load(f))
            except Exception:
                pass
        data["platform"] = self.platform  # ensure platform key not overwritten
        return data

    def capabilities(self) -> dict:
        return {
            "has_morphology": True,
            "has_transcripts": True,
            "has_boundaries": False,  # HDF5 boundary parsing not yet implemented
            "unit_label": "cell",
        }

    # ── Gene catalogue ────────────────────────────────────────────────────────

    def gene_list(self) -> list[str]:
        cbg = self.path / "cell_by_gene.csv"
        if not cbg.exists():
            return []
        try:
            header = pd.read_csv(cbg, nrows=0)
            # First column is cell_id; remaining are gene names
            genes = [c for c in header.columns[1:] if not c.startswith("Blank")]
            return genes
        except Exception:
            return []

    # ── Transcripts ───────────────────────────────────────────────────────────

    def transcripts(
        self,
        bbox: Optional[tuple] = None,
        genes: Optional[list[str]] = None,
        fraction: float = 1.0,
    ) -> dict:
        tx_file = self.path / "detected_transcripts.csv"
        if not tx_file.exists():
            return {"transcripts": [], "total": 0}
        try:
            df = pd.read_csv(tx_file, usecols=["x", "y", "gene"])
            df = df.rename(columns={"x": "x_location", "y": "y_location",
                                    "gene": "feature_name"})
            if bbox:
                xmin, ymin, xmax, ymax = self._bbox_to_native(bbox)
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
            df["x_location"] = df["x_location"] / self.pixel_size
            df["y_location"] = df["y_location"] / self.pixel_size
            return {"transcripts": self._to_records(df), "total": total}
        except Exception:
            return {"transcripts": [], "total": 0}

    # ── Cells ─────────────────────────────────────────────────────────────────

    def _load_cells(self) -> Optional[pd.DataFrame]:
        if self._cells_cache is not None:
            return self._cells_cache
        meta = self.path / "cell_metadata.csv"
        if not meta.exists():
            return None
        try:
            df = pd.read_csv(meta)
            # Normalize column names to the common schema
            rename = {}
            for c in df.columns:
                lc = c.lower()
                if lc in ("entityid", "cell_id"):
                    rename[c] = "cell_id"
                elif lc == "center_x":
                    rename[c] = "x_centroid"
                elif lc == "center_y":
                    rename[c] = "y_centroid"
            df = df.rename(columns=rename)
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
            xmin, ymin, xmax, ymax = self._bbox_to_native(bbox)
            df = df[
                (df["x_centroid"] >= xmin) & (df["x_centroid"] <= xmax) &
                (df["y_centroid"] >= ymin) & (df["y_centroid"] <= ymax)
            ]
        df = df.copy()
        for col in ("x_centroid", "y_centroid"):
            if col in df.columns:
                df[col] = df[col] / self.pixel_size
        return self._to_records(df)

    def cells_schema(self) -> dict:
        df = self._load_cells()
        if df is None:
            return {"columns": {}}
        return {"columns": {c: str(df[c].dtype) for c in df.columns if c != "cell_id"}}

    # ── Cell boundaries ───────────────────────────────────────────────────────

    def cell_boundaries(self, bbox: Optional[tuple] = None, fraction: float = 1.0) -> dict:
        # MERSCOPE cell boundaries are stored as HDF5 — not yet implemented
        return {"boundaries": [], "total": 0}

    # ── Expression ────────────────────────────────────────────────────────────

    def cell_expression(self, cell_id: str) -> dict:
        cbg = self.path / "cell_by_gene.csv"
        if not cbg.exists():
            return {}
        try:
            df = pd.read_csv(cbg, index_col=0)
            if cell_id not in df.index.astype(str):
                return {}
            row = df.loc[cell_id]
            return {g: int(v) for g, v in row.items() if v > 0}
        except Exception:
            return {}

    def cell_detail(self, cell_id: str) -> Optional[dict]:
        df = self._load_cells()
        if df is None:
            return None
        row = df[df["cell_id"] == cell_id]
        if row.empty:
            return None
        record = self._to_records(row)[0]
        for col in ("x_centroid", "y_centroid"):
            if col in record and record[col] is not None:
                record[col] = record[col] / self.pixel_size
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
        cbg = self.path / "cell_by_gene.csv"
        if not cbg.exists() or not genes:
            return {"type": "continuous", "values": {}, "min": 0.0, "max": 0.0}
        try:
            df = pd.read_csv(cbg, index_col=0)
            cols = [g for g in genes if g in df.columns]
            if not cols:
                return {"type": "continuous", "values": {}, "min": 0.0, "max": 0.0}
            summed = df[cols].sum(axis=1)
            values = {str(k): float(v) for k, v in summed.items()}
            return {"type": "continuous", "values": values,
                    "min": 0.0, "max": float(summed.max()) or 1.0}
        except Exception:
            return {"type": "continuous", "values": {}, "min": 0.0, "max": 0.0}

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
