"""
Shared base for 10x Space Ranger output — classic Visium and Visium HD.

The two platforms differ in three things and agree on everything else:

============  ==========================  ==============================
              classic Visium              Visium HD
============  ==========================  ==============================
layout        ``spatial/`` at the root    ``binned_outputs/square_*um/``
unit shape    55 µm circle                square of ``bin_size_um``
pixel size    derived from the spot spec  ``microns_per_pixel`` key
============  ==========================  ==============================

Everything else — scalefactors, ``tissue_positions``, the 10x feature-matrix
HDF5, the cells table, gene-set colouring, and the sample-then-slice boundary
logic — is identical, so it lives here. Subclasses supply the three differences
through ``_spatial_dir()``, ``_matrix_path()``, ``_unit_diameter_fullres()`` and
``_unit_offsets()``.

Coordinate space: the hires image, not full resolution
------------------------------------------------------
``pxl_col_in_fullres`` / ``pxl_row_in_fullres`` are pixels in the **original
full-resolution microscope image**, which Space Ranger does not ship. What it
ships is ``tissue_hires_image.png``, the same frame scaled by
``tissue_hires_scalef``. TissuePlex builds its tile pyramid from that PNG and
derives its entire coordinate space from it, so every coordinate this reader
returns is multiplied by that factor.

Skip the multiply and the data lands off the image by 1/scalef — about 12× on a
typical classic Visium dataset, where the factor runs ~0.08. It is easy to skip
because the bundled Visium HD fixture has ``tissue_hires_scalef: 1.0``, so the
bug is invisible there; ``sample_data/make_visium.py`` deliberately generates a
non-identity factor for exactly this reason.

``pixel_size`` therefore reports µm per **hires** pixel, not per fullres pixel.
That matters beyond distance labels: ``EdgeReader`` divides the micron
coordinates in ``edges.parquet`` by ``pixel_size`` to place edges, so a
fullres-based value would scatter the connectivity layer off the tissue.

The caveat this leaves: if a user drops their own full-resolution image into the
dataset folder and selects it, coordinates will be wrong by the same factor,
because the reader has no way to know which image the viewer has open. Selecting
``tissue_lowres_image`` has the same problem. Both are recorded in the platform
docs rather than guessed at here.
"""
import json
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from app.readers.base_reader import _UNSET, SpatialDatasetReader


class SpaceRangerReader(SpatialDatasetReader):

    # Space Ranger's own root CSVs, so the shared supplemental-metadata loader
    # never ingests them as user annotations.
    _ROOT_CSV_SKIP = frozenset({"metrics_summary.csv"})

    # Prefix used in log lines; subclasses override.
    _LOG = "spaceranger"

    def __init__(self, dataset_path: Path):
        super().__init__(dataset_path)
        self._scalefactors_cache = _UNSET
        self._positions_cache = _UNSET
        self._cells_full_cache = _UNSET
        self._matrix_cache = _UNSET

    # ── Subclass hooks ────────────────────────────────────────────────────────

    def _spatial_dir(self) -> Optional[Path]:
        """Directory holding scalefactors_json.json and tissue_positions.*."""
        raise NotImplementedError

    def _matrix_path(self) -> Optional[Path]:
        """Path to filtered_feature_bc_matrix.h5, or None."""
        raise NotImplementedError

    def _unit_diameter_fullres(self) -> float:
        """Width of one unit in **full-resolution** pixels."""
        raise NotImplementedError

    @staticmethod
    def _unit_offsets(radius: float) -> tuple:
        """Vertex offsets from a unit's centre, in image pixels.

        deck.gl closes the ring itself, so the first vertex must not be repeated
        — matching every other reader.
        """
        raise NotImplementedError

    def _microns_per_fullres_px(self) -> float:
        """µm per full-resolution pixel. Scaled to hires by `pixel_size`."""
        raise NotImplementedError

    # ── Scalefactors ──────────────────────────────────────────────────────────

    def _scalefactors(self) -> dict:
        if self._scalefactors_cache is not _UNSET:
            return self._scalefactors_cache  # type: ignore[return-value]
        self._scalefactors_cache = {}
        spatial = self._spatial_dir()
        if spatial is not None:
            f = spatial / "scalefactors_json.json"
            if f.exists():
                try:
                    self._scalefactors_cache = json.loads(f.read_text())
                except Exception as exc:
                    print(f"[{self._LOG}] could not read {f.name}: {exc}")
        return self._scalefactors_cache  # type: ignore[return-value]

    @property
    def hires_scalef(self) -> float:
        """Factor mapping full-resolution pixels onto tissue_hires_image.png."""
        try:
            v = float(self._scalefactors().get("tissue_hires_scalef", 1.0))
            return v if v > 0 else 1.0
        except (TypeError, ValueError):
            return 1.0

    @property
    def pixel_size(self) -> float:
        """µm per **hires** image pixel — the space this reader returns."""
        try:
            v = self._microns_per_fullres_px() / self.hires_scalef
            return v if v > 0 and np.isfinite(v) else 1.0
        except (TypeError, ValueError, ZeroDivisionError):
            return 1.0

    def unit_diameter_px(self) -> float:
        """Width of one unit in image (hires) pixels."""
        return self._unit_diameter_fullres() * self.hires_scalef

    # ── Positions ─────────────────────────────────────────────────────────────

    def _read_positions_table(self) -> Optional[pd.DataFrame]:
        """Raw tissue_positions in whichever of the three forms shipped.

        Space Ranger < 2.0 wrote ``tissue_positions_list.csv`` with **no header
        row**; 2.0 renamed it and added one. Reading the old file with the
        default header inference silently eats the first spot and mislabels
        every column after it, so the legacy name is read with explicit names.
        """
        spatial = self._spatial_dir()
        if spatial is None:
            return None
        cols = ["barcode", "in_tissue", "array_row", "array_col",
                "pxl_row_in_fullres", "pxl_col_in_fullres"]
        try:
            pq = spatial / "tissue_positions.parquet"
            if pq.exists():
                return pd.read_parquet(pq)
            csv = spatial / "tissue_positions.csv"
            if csv.exists():
                return pd.read_csv(csv)
            legacy = spatial / "tissue_positions_list.csv"
            if legacy.exists():
                return pd.read_csv(legacy, header=None, names=cols)
        except Exception as exc:
            print(f"[{self._LOG}] could not read tissue positions: {exc}")
        return None

    def _positions(self) -> pd.DataFrame:
        """In-tissue units with centroids in hires-image pixels, one row each."""
        if self._positions_cache is not _UNSET:
            return self._positions_cache  # type: ignore[return-value]
        self._positions_cache = pd.DataFrame()

        df = self._read_positions_table()
        if df is None:
            return self._positions_cache  # type: ignore[return-value]
        if not {"pxl_col_in_fullres", "pxl_row_in_fullres"} <= set(df.columns):
            print(f"[{self._LOG}] tissue positions lack pxl_col/row_in_fullres")
            return self._positions_cache  # type: ignore[return-value]

        # Most units fall outside the tissue; some carry negative pixel
        # coordinates because the capture area is larger than the image.
        if "in_tissue" in df.columns:
            df = df[df["in_tissue"] == 1]

        scale = self.hires_scalef
        out = pd.DataFrame({
            "cell_id": df["barcode"].astype(str) if "barcode" in df.columns
            else df.index.astype(str),
            "x_centroid": df["pxl_col_in_fullres"].astype(float) * scale,
            "y_centroid": df["pxl_row_in_fullres"].astype(float) * scale,
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

    def _metadata_frame(self):
        return self._cells_full()

    # ── Cells (spots / bins) ──────────────────────────────────────────────────

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

    # ── Boundaries ────────────────────────────────────────────────────────────

    def cell_boundaries(self, bbox: Optional[tuple] = None,
                        fraction: float = 1.0,
                        cell_ids: Optional[set] = None) -> dict:
        """Unit outlines as polygons, in image pixel space.

        A unit qualifies if its centroid lies in the bbox, and then all of its
        vertices are returned — the whole-unit rule every reader follows, so
        units are never clipped into partial shapes at the viewport edge.

        Nothing in the frontend renders `cells()` centroids; the boundary layers
        are the only path to drawing a unit. That is why both spot-based
        platforms synthesise polygons instead of declaring `has_boundaries:
        False`, which would leave the canvas empty.
        """
        df = self._cells_full()
        if df is None or df.empty:
            return {"boundaries": [], "total": 0}

        # Metadata filter (issue #45), before the bbox count and the sample.
        if cell_ids is not None:
            df = df[df["cell_id"].astype(str).isin(cell_ids)]

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
            # viewport must return the same units or the layer flickers.
            df = df.iloc[np.linspace(0, total - 1, n).astype(int)]

        offsets = self._unit_offsets(self.unit_diameter_px() / 2.0)
        ids = df["cell_id"].to_numpy()
        xs = df["x_centroid"].to_numpy()
        ys = df["y_centroid"].to_numpy()
        rows = [
            {"cell_id": ids[i], "vertex_x": float(xs[i] + dx), "vertex_y": float(ys[i] + dy)}
            for i in range(len(ids)) for dx, dy in offsets
        ]
        return {"boundaries": rows, "total": total}

    # ── No per-molecule transcripts on either platform ────────────────────────

    def transcripts(self, bbox: Optional[tuple] = None,
                    genes: Optional[list[str]] = None,
                    fraction: float = 1.0) -> dict:
        """Neither Visium nor Visium HD has molecule-level detections.

        Returns the same dict shape as every other reader rather than a bare
        list, so a caller that ignores `has_transcripts` gets an empty result
        instead of a TypeError.
        """
        return {"transcripts": [], "total": 0}

    # ── Expression (filtered_feature_bc_matrix.h5) ────────────────────────────

    def _matrix(self):
        """(barcodes, gene_names, csc_matrix), or None.

        The same 10x HDF5 layout Xenium uses for cell_feature_matrix.h5.
        """
        if self._matrix_cache is not _UNSET:
            return self._matrix_cache
        self._matrix_cache = None
        h5 = self._matrix_path()
        if h5 is None or not h5.exists():
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
            print(f"[{self._LOG}] could not read {h5.name}: {exc}")
        return self._matrix_cache

    def gene_list(self) -> list[str]:
        m = self._matrix()
        if m is None:
            return []
        _, names, _ = m
        # Deduplicate while preserving order — 10x references repeat a symbol.
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
                     genes: Optional[list[str]] = None,
                     categorical: Optional[bool] = None) -> dict:
        if mode == "gene_set":
            return self._color_values_gene_set(genes or [])
        return self._color_values_meta(field or "", categorical)

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

    # ── Morphology ────────────────────────────────────────────────────────────

    _MORPHOLOGY_NAMES = ("tissue_hires_image.png", "tissue_lowres_image.png")

    def _morphology_exists(self) -> bool:
        spatial = self._spatial_dir()
        for base in (spatial, self.path / "spatial"):
            if base is None:
                continue
            if any((base / n).exists() for n in self._MORPHOLOGY_NAMES):
                return True
        return False
