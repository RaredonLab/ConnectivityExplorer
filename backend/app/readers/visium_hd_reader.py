"""
10x Genomics Visium HD reader.

Visium HD tiles the capture area with square bins at 2 µm, 8 µm and 16 µm. There
are no per-molecule transcript coordinates — only bin-level UMI counts.

Expected directory layout (Space Ranger `outs/`)
------------------------------------------------
  dataset_dir/
    binned_outputs/
      square_002um/  square_008um/  square_016um/
        filtered_feature_bc_matrix.h5
        spatial/
          tissue_positions.parquet     barcode, in_tissue, array_row, array_col,
                                       pxl_row_in_fullres, pxl_col_in_fullres
          scalefactors_json.json       microns_per_pixel, spot_diameter_fullres,
                                       bin_size_um, tissue_hires_scalef, …
          tissue_hires_image.png       morphology (PNG, not TIFF)
    spatial/                           the same images, duplicated
    segmented_outputs/                 Space Ranger 4.x: real cell polygons

Note the bin directories are under ``binned_outputs/`` and that
``tissue_positions.parquet`` / ``scalefactors_json.json`` are **per bin**. The
top-level ``spatial/`` folder holds images only.

Bins are rendered as squares
----------------------------
A bin is literally a square of side ``spot_diameter_fullres`` pixels, so
``cell_boundaries()`` emits four vertices per bin rather than reporting
``has_boundaries: False``. That is an honest representation of the data, and it
means fill, outline, colour-by, picking and region selection all work through the
existing layers with no frontend change. Nothing renders `cells()` centroids —
boundaries are the only path to drawing a unit — so declaring no boundaries would
leave the canvas empty.

Coordinates
-----------
``pxl_col_in_fullres`` / ``pxl_row_in_fullres`` are already in **full-resolution
image pixels**, which is exactly TissuePlex's contract, so they pass through
untouched. ``pixel_size`` is ``microns_per_pixel`` from the scalefactors, and is
used only to label distances in µm.

**The morphology image is not always full-resolution.** ``tissue_hires_image.png``
is the fullres frame scaled by ``tissue_hires_scalef``, so bin coordinates must be
multiplied by that factor to land on it. On the bundled tiny dataset the factor is
exactly 1.0 and ``microns_per_pixel`` is 1.003 — both effectively identity — so a
missing multiply would look perfectly correct there and break on every real
dataset, where the factor runs ~0.02–0.2. The multiply is therefore explicit and
covered by an assertion in the reader tests rather than left implicit.
"""
import json
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from app.readers.base_reader import _UNSET, SpatialDatasetReader

# Space Ranger's own analysis default, and spatialdata-io's DEFAULT_BIN.
# Preferred when present; otherwise the coarsest bin available, since coarser
# means fewer bins to render.
_PREFERRED_BINS = ("square_008um", "square_016um", "square_002um")

_FALLBACK_MICRONS_PER_PIXEL = 1.0


class VisiumHDReader(SpatialDatasetReader):

    # Space Ranger's own root CSVs, so the shared supplemental-metadata loader
    # never ingests them as user annotations.
    _ROOT_CSV_SKIP = frozenset({"metrics_summary.csv"})

    def __init__(self, dataset_path: Path):
        super().__init__(dataset_path)
        self._bin_dir_cache = _UNSET
        self._scalefactors_cache = _UNSET
        self._positions_cache = _UNSET
        self._cells_full_cache = _UNSET
        self._matrix_cache = _UNSET

    # ── Layout ────────────────────────────────────────────────────────────────

    def _bin_root(self) -> Path:
        """Directory holding the square_*um folders."""
        nested = self.path / "binned_outputs"
        return nested if nested.is_dir() else self.path

    def available_bins(self) -> list[str]:
        return sorted(d.name for d in self._bin_root().glob("square_*um") if d.is_dir())

    def _bin_dir(self) -> Optional[Path]:
        """The bin directory this reader serves."""
        if self._bin_dir_cache is not _UNSET:
            return self._bin_dir_cache  # type: ignore[return-value]
        root = self._bin_root()
        available = self.available_bins()
        chosen = None
        for name in _PREFERRED_BINS:
            if name in available:
                chosen = root / name
                break
        if chosen is None and available:
            # Unknown bin size — take the coarsest, i.e. the largest µm number.
            chosen = root / sorted(available)[-1]
        self._bin_dir_cache = chosen
        return chosen

    def _scalefactors(self) -> dict:
        if self._scalefactors_cache is not _UNSET:
            return self._scalefactors_cache  # type: ignore[return-value]
        self._scalefactors_cache = {}
        bin_dir = self._bin_dir()
        if bin_dir is not None:
            f = bin_dir / "spatial" / "scalefactors_json.json"
            if f.exists():
                try:
                    self._scalefactors_cache = json.loads(f.read_text())
                except Exception as exc:
                    print(f"[visium_hd] could not read {f.name}: {exc}")
        return self._scalefactors_cache  # type: ignore[return-value]

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def platform(self) -> str:
        return "visium_hd"

    @property
    def pixel_size(self) -> float:
        """µm per full-resolution image pixel."""
        v = self._scalefactors().get("microns_per_pixel")
        try:
            v = float(v)
            return v if v > 0 else _FALLBACK_MICRONS_PER_PIXEL
        except (TypeError, ValueError):
            return _FALLBACK_MICRONS_PER_PIXEL

    @property
    def hires_scalef(self) -> float:
        """Factor mapping full-resolution pixels onto tissue_hires_image.png."""
        try:
            return float(self._scalefactors().get("tissue_hires_scalef", 1.0))
        except (TypeError, ValueError):
            return 1.0

    def _bin_side_px(self) -> float:
        """Bin square side length, in full-resolution pixels."""
        sf = self._scalefactors()
        for key in ("spot_diameter_fullres",):
            try:
                v = float(sf[key])
                if v > 0:
                    return v
            except (KeyError, TypeError, ValueError):
                pass
        # Derive from the bin size in µm if the diameter is missing.
        try:
            return float(sf["bin_size_um"]) / self.pixel_size
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return 1.0

    # ── Experiment metadata ───────────────────────────────────────────────────

    def info(self) -> dict:
        bin_dir = self._bin_dir()
        sf = self._scalefactors()
        return {
            "platform": self.platform,
            "bin": bin_dir.name if bin_dir else None,
            "available_bins": self.available_bins(),
            "bin_size_um": sf.get("bin_size_um"),
            "pixel_size": self.pixel_size,
            "tissue_hires_scalef": self.hires_scalef,
            "bin_side_px": self._bin_side_px(),
        }

    def capabilities(self) -> dict:
        return {
            "has_morphology": self._morphology_exists(),
            # Bin-level UMI counts only; there are no molecule coordinates.
            "has_transcripts": False,
            # Bins are squares, and we emit them as polygons.
            "has_boundaries": self._bin_dir() is not None,
            "unit_label": "bin",
        }

    def _morphology_exists(self) -> bool:
        bin_dir = self._bin_dir()
        candidates = []
        if bin_dir is not None:
            candidates.append(bin_dir / "spatial" / "tissue_hires_image.png")
        candidates.append(self.path / "spatial" / "tissue_hires_image.png")
        return any(c.exists() for c in candidates)

    # ── Positions ─────────────────────────────────────────────────────────────

    def _positions(self) -> pd.DataFrame:
        """In-tissue bins with pixel-space centroids, indexed by barcode."""
        if self._positions_cache is not _UNSET:
            return self._positions_cache  # type: ignore[return-value]
        self._positions_cache = pd.DataFrame()
        bin_dir = self._bin_dir()
        if bin_dir is None:
            return self._positions_cache  # type: ignore[return-value]

        spatial = bin_dir / "spatial"
        pos = spatial / "tissue_positions.parquet"
        try:
            if pos.exists():
                df = pd.read_parquet(pos)
            else:
                csv = spatial / "tissue_positions.csv"
                if not csv.exists():
                    return self._positions_cache  # type: ignore[return-value]
                df = pd.read_csv(csv)
        except Exception as exc:
            print(f"[visium_hd] could not read tissue positions: {exc}")
            return self._positions_cache  # type: ignore[return-value]

        if not {"pxl_col_in_fullres", "pxl_row_in_fullres"} <= set(df.columns):
            print("[visium_hd] tissue positions lack pxl_col/row_in_fullres")
            return self._positions_cache  # type: ignore[return-value]

        # Most bins fall outside the tissue; some even carry negative pixel
        # coordinates because the capture area is larger than the image.
        if "in_tissue" in df.columns:
            df = df[df["in_tissue"] == 1]

        out = pd.DataFrame({
            "cell_id": df["barcode"].astype(str) if "barcode" in df.columns
            else df.index.astype(str),
            "x_centroid": df["pxl_col_in_fullres"].astype(float),
            "y_centroid": df["pxl_row_in_fullres"].astype(float),
        })
        for extra in ("array_row", "array_col"):
            if extra in df.columns:
                out[extra] = df[extra].to_numpy()
        out = out.reset_index(drop=True)
        self._positions_cache = out
        return out

    def _cells_full(self) -> Optional[pd.DataFrame]:
        if self._cells_full_cache is not _UNSET:
            return self._cells_full_cache  # type: ignore[return-value]
        pos = self._positions()
        self._cells_full_cache = self._merge_supplemental(
            None if pos.empty else pos
        )
        return self._cells_full_cache  # type: ignore[return-value]

    # ── Cells (bins) ──────────────────────────────────────────────────────────

    def cells(self, bbox: Optional[tuple] = None) -> list[dict]:
        df = self._cells_full()
        if df is None or df.empty:
            return []
        if bbox:
            xmin, ymin, xmax, ymax = bbox
            if None not in (xmin, ymin, xmax, ymax):
                df = df[(df["x_centroid"] >= xmin) & (df["x_centroid"] <= xmax) &
                        (df["y_centroid"] >= ymin) & (df["y_centroid"] <= ymax)]
        return self._to_records(df)

    def cells_schema(self) -> dict:
        df = self._cells_full()
        if df is None or df.empty:
            return {"columns": {}}
        return {"columns": {c: str(df[c].dtype) for c in df.columns if c != "cell_id"}}

    def cell_detail(self, cell_id: str) -> Optional[dict]:
        df = self._cells_full()
        if df is None or df.empty:
            return None
        row = df[df["cell_id"] == str(cell_id)]
        if row.empty:
            return None
        rec = self._to_records(row)[0]
        rec["expression"] = self.cell_expression(cell_id)
        return rec

    # ── Boundaries: each bin as a square ──────────────────────────────────────

    def cell_boundaries(self, bbox: Optional[tuple] = None,
                        fraction: float = 1.0) -> dict:
        """Bin outlines as square polygons, in pixel space.

        A bin qualifies if its centroid lies in the bbox, and then all four of its
        corners are returned — the same whole-unit rule the other readers use, so
        bins are never clipped into partial shapes at the viewport edge.
        """
        df = self._cells_full()
        if df is None or df.empty:
            return {"boundaries": [], "total": 0}

        if bbox:
            xmin, ymin, xmax, ymax = bbox
            if None not in (xmin, ymin, xmax, ymax):
                df = df[(df["x_centroid"] >= xmin) & (df["x_centroid"] <= xmax) &
                        (df["y_centroid"] >= ymin) & (df["y_centroid"] <= ymax)]
        total = len(df)
        if total == 0:
            return {"boundaries": [], "total": 0}

        fraction = max(0.0001, min(1.0, fraction))
        n = round(fraction * total)
        if n <= 0:
            return {"boundaries": [], "total": total}
        if n < total:
            # Deterministic, evenly spread subset — re-fetching an unchanged
            # viewport must return the same bins or the layer flickers.
            df = df.iloc[np.linspace(0, total - 1, n).astype(int)]

        half = self._bin_side_px() / 2.0
        ids = df["cell_id"].to_numpy()
        xs = df["x_centroid"].to_numpy()
        ys = df["y_centroid"].to_numpy()
        # Corners counter-clockwise; deck.gl closes the ring itself, so the first
        # vertex is not repeated (matching the other readers).
        offsets = ((-half, -half), (half, -half), (half, half), (-half, half))
        rows = [
            {"cell_id": ids[i], "vertex_x": float(xs[i] + dx), "vertex_y": float(ys[i] + dy)}
            for i in range(len(ids)) for dx, dy in offsets
        ]
        return {"boundaries": rows, "total": total}

    # ── No per-molecule transcripts ───────────────────────────────────────────

    def transcripts(self, bbox: Optional[tuple] = None,
                    genes: Optional[list[str]] = None,
                    fraction: float = 1.0) -> dict:
        """Visium HD has no molecule-level detections.

        Returns the same dict shape as every other reader rather than a bare list,
        so a caller that ignores `has_transcripts` gets an empty result instead of
        a TypeError.
        """
        return {"transcripts": [], "total": 0}

    # ── Expression (filtered_feature_bc_matrix.h5) ────────────────────────────

    def _matrix(self):
        """(barcodes, gene_names, csc_matrix) from the bin's feature matrix, or None.

        Same 10x HDF5 layout Xenium uses for cell_feature_matrix.h5.
        """
        if self._matrix_cache is not _UNSET:
            return self._matrix_cache
        self._matrix_cache = None
        bin_dir = self._bin_dir()
        if bin_dir is None:
            return None
        h5 = bin_dir / "filtered_feature_bc_matrix.h5"
        if not h5.exists():
            return None
        try:
            import h5py
            import scipy.sparse as sp
            with h5py.File(h5, "r") as f:
                barcodes = f["matrix/barcodes"][()].astype(str).tolist()
                names = f["matrix/features/name"][()].astype(str).tolist()
                mat = sp.csc_matrix(
                    (f["matrix/data"][()], f["matrix/indices"][()], f["matrix/indptr"][()]),
                    shape=(len(names), len(barcodes)),
                )
            self._matrix_cache = (barcodes, names, mat)
        except Exception as exc:
            print(f"[visium_hd] could not read {h5.name}: {exc}")
        return self._matrix_cache

    def gene_list(self) -> list[str]:
        m = self._matrix()
        if m is None:
            return []
        _, names, _ = m
        # Deduplicate while preserving order — 10x panels can repeat a symbol.
        seen, out = set(), []
        for g in names:
            if g not in seen:
                seen.add(g)
                out.append(g)
        return out

    def cell_expression(self, cell_id: str) -> dict:
        m = self._matrix()
        if m is None:
            return {}
        barcodes, names, mat = m
        try:
            idx = barcodes.index(str(cell_id))
        except ValueError:
            return {}
        col = mat.getcol(idx).toarray().ravel()
        nz = np.nonzero(col)[0]
        return {names[i]: int(col[i]) for i in nz}

    # ── Colour values ─────────────────────────────────────────────────────────

    def color_values(self, mode: str, field: Optional[str] = None,
                     genes: Optional[list[str]] = None) -> dict:
        if mode == "gene_set":
            return self._color_values_gene_set(genes or [])
        return self._color_values_meta(field or "")

    def _color_values_gene_set(self, genes: list[str]) -> dict:
        empty = {"type": "continuous", "values": {}, "min": 0.0, "max": 0.0}
        m = self._matrix()
        if m is None or not genes:
            return empty
        barcodes, names, mat = m
        wanted = set(genes)
        rows = [i for i, g in enumerate(names) if g in wanted]
        if not rows:
            return empty
        summed = np.asarray(mat[rows, :].sum(axis=0)).ravel()
        vmax = float(summed.max()) if summed.size and summed.max() > 0 else 1.0
        return {
            "type": "continuous",
            "values": {barcodes[i]: float(summed[i]) for i in range(len(barcodes))},
            "min": 0.0,
            "max": vmax,
        }

    def _color_values_meta(self, field: str) -> dict:
        empty = {"type": "continuous", "values": {}, "min": 0.0, "max": 0.0}
        df = self._cells_full()
        if df is None or df.empty or field not in df.columns:
            return empty
        col = df[field]
        ids = df["cell_id"].astype(str).tolist()
        has = col.notna()
        categorical = (
            pd.api.types.is_string_dtype(col) or pd.api.types.is_object_dtype(col)
            or (pd.api.types.is_integer_dtype(col) and col.nunique() <= 30)
        )
        if categorical:
            return {
                "type": "categorical",
                "values": {ids[i]: str(col.iloc[i]) for i in range(len(ids)) if has.iloc[i]},
                "categories": sorted(col[has].astype(str).unique().tolist(), key=str),
            }
        valid = col[has]
        if valid.empty:
            return empty
        return {
            "type": "continuous",
            "values": {ids[i]: float(col.iloc[i]) for i in range(len(ids)) if has.iloc[i]},
            "min": float(valid.min()),
            "max": float(valid.max()),
        }
