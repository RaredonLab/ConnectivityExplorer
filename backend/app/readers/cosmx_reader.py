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

from app.readers import duck
from app.readers.base_reader import SpatialDatasetReader


# Bruker documents a 120 nm pixel edge and states "multiply the pixel value by
# 0.12028 µm per pixel"; Giotto hardcodes the same constant. The previous 0.18
# here was 50% too large, which scaled every reported distance and — because the
# edge reader divides x1/y1 by pixel_size — misplaced the whole edge layer.
_DEFAULT_PIXEL_SIZE = 0.12028  # µm/px, per Bruker CosMx documentation


class CosMxReader(SpatialDatasetReader):

    # CosMx prefixes every output with the experiment name (`<expt>_tx_file.csv`),
    # so the shared supplemental-metadata loader has to match by suffix rather than
    # by exact filename to avoid ingesting the platform's own tables.
    # Suffix form because CosMx normally prefixes every output with the
    # experiment name. The bare names are listed too: some public exports drop
    # the prefix entirely, and without them the supplemental-metadata loader
    # treats the platform's own exprMat/polygons/tx tables as user metadata and
    # tries to outer-join hundreds of MB of them.
    _ROOT_CSV_SKIP_SUFFIXES = (
        "_tx_file.csv", "_metadata_file.csv", "_fov_positions_file.csv",
        "_exprmat_file.csv", "-polygons.csv",
    )
    _ROOT_CSV_SKIP = frozenset({
        "tx_file.csv", "metadata_file.csv", "fov_positions_file.csv",
        "exprmat_file.csv", "expr_mat_file.csv", "polygons.csv",
    })

    def __init__(self, dataset_path: Path):
        super().__init__(dataset_path)
        self._cells_cache: Optional[pd.DataFrame] = None
        self._origin_cache: Optional[tuple] = None

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
            "has_morphology": (self.path / "CellComposite").is_dir(),
            "has_transcripts": True,
            "has_boundaries": self._polygon_file() is not None,
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
            x0, y0 = self._origin()
            df["x_location"] = df["x_location"] - x0
            df["y_location"] = df["y_location"] - y0
            if bbox:
                # bbox arrives in the shifted frame, matching what cells report.
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
        """Cell metadata keyed on the composite (fov, cell_ID) identity.

        cell_ID restarts at 1 in every field of view, so it is not unique on its
        own — keying on it alone silently merges unrelated cells from different
        FOVs. Newer exports add a study-wide `cell` column; when present that is
        used directly, otherwise "<fov>_<cell_ID>" is synthesised.

        Centroids come from CenterX_global_px / CenterY_global_px, which are
        already whole-slide pixels, so no FOV offset has to be applied.
        """
        if self._cells_cache is not None:
            return self._cells_cache
        meta_file = self._find_file("*_metadata_file.csv") or self._find_file("metadata_file.csv")
        if meta_file is None:
            return None
        try:
            df = pd.read_csv(meta_file)
        except Exception as exc:
            print(f"[cosmx] could not read {meta_file.name}: {exc}")
            return None

        cols = {c.lower(): c for c in df.columns}
        xc = cols.get("centerx_global_px")
        yc = cols.get("centery_global_px")
        if xc is None or yc is None:
            # Fall back to local coordinates plus the FOV origin if the export
            # only carries the per-FOV frame.
            xl, yl = cols.get("centerx_local_px"), cols.get("centery_local_px")
            fovs = self._fov_offsets()
            if xl and yl and fovs is not None and "fov" in cols:
                df["_gx"] = df[xl] + df[cols["fov"]].map(fovs["x"]).fillna(0)
                df["_gy"] = df[yl] + df[cols["fov"]].map(fovs["y"]).fillna(0)
                xc, yc = "_gx", "_gy"
            else:
                print("[cosmx] no CenterX/Y_global_px in metadata; cannot place cells")
                return None

        x0, y0 = self._origin()
        out = pd.DataFrame({
            "x_centroid": df[xc].astype(float) - x0,
            "y_centroid": df[yc].astype(float) - y0,
        })
        if cols.get("cell"):                       # study-wide unique id
            out.insert(0, "cell_id", df[cols["cell"]].astype(str))
        elif cols.get("fov") and cols.get("cell_id"):
            out.insert(0, "cell_id",
                       df[cols["fov"]].astype(str) + "_" + df[cols["cell_id"]].astype(str))
        elif cols.get("cell_id"):
            out.insert(0, "cell_id", df[cols["cell_id"]].astype(str))
        else:
            return None

        # Carry the remaining numeric/string metadata through for colour-by.
        for c in df.columns:
            lc = c.lower()
            if lc in ("cell_id", "cell", "centerx_global_px", "centery_global_px",
                      "centerx_local_px", "centery_local_px") or c in ("_gx", "_gy"):
                continue
            out[c] = df[c].to_numpy()

        self._cells_cache = self._merge_supplemental(out)
        return self._cells_cache

    def _origin(self) -> tuple:
        """(x0, y0) subtracted from every coordinate, in global pixels.

        CosMx global pixel coordinates are in the slide frame, so a run can start
        tens of thousands of pixels from the origin and extend to negative values
        where the FOV grid runs above it — the mouse-brain set spans
        x 128749..175643, y -9980..15588.

        TissuePlex's contract is image pixel space, and CosMx exports carry no
        morphology image to define that space. With no external frame, the only
        sensible origin is the data's own corner, so the whole dataset is shifted
        to start at (0, 0). Without this the placeholder canvas would have to span
        from the slide origin, leaving 43% of cells at negative coordinates and
        off the canvas entirely.

        Cells, transcripts and boundaries all subtract the same value, so they
        stay registered with each other.
        """
        if self._origin_cache is not None:
            return self._origin_cache
        self._origin_cache = (0.0, 0.0)
        meta = self._find_file("*_metadata_file.csv") or self._find_file("metadata_file.csv")
        if meta is not None:
            try:
                cols = {c.lower(): c for c in duck.csv_columns(meta)}
                xc, yc = cols.get("centerx_global_px"), cols.get("centery_global_px")
                if xc and yc:
                    with duck.connect() as conn:
                        x0, y0 = conn.execute(
                            f'SELECT MIN("{xc}"), MIN("{yc}") FROM {duck.scan_csv(meta)}'
                        ).fetchone()
                    if x0 is not None and y0 is not None:
                        # Leave a small margin so units on the edge are not flush
                        # against the canvas border.
                        self._origin_cache = (float(x0) - 50.0, float(y0) - 50.0)
            except Exception as exc:
                print(f"[cosmx] could not determine coordinate origin: {exc}")
        return self._origin_cache

    def _fov_offsets(self):
        """{'x': {fov: x0}, 'y': {fov: y0}} from the FOV positions file, or None."""
        f = self._find_file("*_fov_positions_file.csv") or self._find_file("fov_positions_file.csv")
        if f is None:
            return None
        try:
            d = pd.read_csv(f)
        except Exception:
            return None
        cols = {c.lower(): c for c in d.columns}
        fov = cols.get("fov")
        gx, gy = cols.get("x_global_px"), cols.get("y_global_px")
        if not (fov and gx and gy):
            return None
        return {"x": dict(zip(d[fov], d[gx])), "y": dict(zip(d[fov], d[gy]))}

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

    def _polygon_file(self) -> Optional[Path]:
        """The cell-outline vertex CSV, if this export includes one.

        Standard AtoMx flat-file exports ship `<expt>-polygons.csv` (note the
        hyphen, unlike every other file). The 2021 NSCLC release predates it and
        has only per-FOV label TIFFs, which is why this is checked rather than
        assumed.
        """
        for pat in ("*-polygons.csv", "polygons.csv"):
            hit = sorted(self.path.glob(pat))
            if hit:
                return hit[0]
        return None

    def cell_boundaries(self, bbox: Optional[tuple] = None,
                        fraction: float = 1.0,
                        cell_ids: Optional[set] = None) -> dict:
        """Cell outlines in pixel space from the polygons CSV.

        One row per vertex, already in global pixels, so no FOV offset has to be
        applied. Cell identity is the (fov, cellID) pair — cellID restarts at 1 in
        every field of view, so keying on it alone merges unrelated cells.
        Note the column is `cellID` here and `cell_ID` everywhere else.
        """
        path = self._polygon_file()
        if path is None:
            return {"boundaries": [], "total": 0}

        cols = set(duck.csv_columns(path))
        if not {"x_global_px", "y_global_px"} <= cols:
            return {"boundaries": [], "total": 0}
        id_col = "cellID" if "cellID" in cols else ("cell_ID" if "cell_ID" in cols else None)
        if id_col is None:
            return {"boundaries": [], "total": 0}
        has_fov = "fov" in cols

        src = duck.scan_csv(path)
        key = (f'CAST("fov" AS VARCHAR) || \'_\' || CAST("{id_col}" AS VARCHAR)'
               if has_fov else f'CAST("{id_col}" AS VARCHAR)')
        # The incoming bbox is in the shifted frame that cells() reports, but the
        # CSV holds raw slide-frame global pixels — so shift the bbox back before
        # filtering. Note CosMx coordinates are already pixels, so _bbox_to_native
        # (which multiplies by pixel_size for micron platforms) must NOT be used.
        native_bbox = None
        if bbox and None not in bbox:
            x0, y0 = self._origin()
            native_bbox = (bbox[0] + x0, bbox[1] + y0, bbox[2] + x0, bbox[3] + y0)
        sql, params = duck.bbox_predicate("x_global_px", "y_global_px", native_bbox)

        with duck.connect() as conn:
            if sql:
                # Whole-cell rule: a cell qualifies if any vertex is in view, and
                # then all of its vertices are returned, so outlines are never
                # clipped into torn shapes at the viewport edge.
                visible = conn.execute(
                    f"SELECT DISTINCT {key} AS cid FROM {src} WHERE {sql}", params
                ).df()["cid"].tolist()
                # Metadata filter (issue #45) — narrow the visible set before the
                # count so `total` and the sample both describe the subset.
                if cell_ids is not None:
                    visible = [c for c in visible if str(c) in cell_ids]
                if not visible:
                    return {"boundaries": [], "total": 0}
                total = len(visible)
                fraction = max(0.0001, min(1.0, fraction))
                n = round(fraction * total)
                if n <= 0:
                    return {"boundaries": [], "total": total}
                keep = ([visible[i] for i in
                         __import__("numpy").linspace(0, total - 1, n).astype(int)]
                        if n < total else visible)
                ph = ", ".join("?" for _ in keep)
                df = conn.execute(
                    f'SELECT {key} AS cell_id, "x_global_px", "y_global_px" '
                    f"FROM {src} WHERE {key} IN ({ph})", keep).df()
            else:
                df = conn.execute(
                    f'SELECT {key} AS cell_id, "x_global_px", "y_global_px" '
                    f"FROM {src}").df()
                if cell_ids is not None:
                    df = df[df["cell_id"].astype(str).isin(cell_ids)]
                total = df["cell_id"].nunique()
                fraction = max(0.0001, min(1.0, fraction))
                n = round(fraction * total)
                if n <= 0:
                    return {"boundaries": [], "total": total}
                if n < total:
                    ids = sorted(df["cell_id"].unique())
                    import numpy as _np
                    keep = {ids[i] for i in _np.linspace(0, len(ids) - 1, n).astype(int)}
                    df = df[df["cell_id"].isin(keep)]

        x0, y0 = self._origin()
        df = df.rename(columns={"x_global_px": "vertex_x", "y_global_px": "vertex_y"})
        df["vertex_x"] = df["vertex_x"] - x0
        df["vertex_y"] = df["vertex_y"] - y0
        # Global pixels are the platform's native frame, and TissuePlex serves
        # image pixels — but the base contract divides by pixel_size, so these
        # are already in the right space and pass through unchanged.
        return {"boundaries": duck.to_records(df), "total": int(total)}

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
        categorical: Optional[bool] = None,
    ) -> dict:
        if mode == "gene_set":
            # Gene-set coloring requires aggregating transcripts per cell — stub
            return {"type": "continuous", "values": {}, "min": 0.0, "max": 0.0}
        return self._color_values_meta(field or "", categorical)

    def _metadata_frame(self):
        return self._load_cells()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _find_file(self, pattern: str) -> Optional[Path]:
        """First file matching a glob in the dataset directory.

        Also tries the pattern with its leading `*_` stripped. CosMx normally
        prefixes every output with the experiment name, but some public exports
        drop the prefix, and `*_tx_file.csv` cannot match a bare `tx_file.csv`
        because the glob requires a literal underscore.
        """
        matches = sorted(self.path.glob(pattern))
        if matches:
            return matches[0]
        if pattern.startswith("*_"):
            matches = sorted(self.path.glob(pattern[2:]))
            if matches:
                return matches[0]
        return None
