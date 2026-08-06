"""
MERSCOPE (Vizgen MERFISH) dataset reader.

Expected output layout
----------------------
  cell_by_gene.csv          cells × genes; first column literally named `cell`
  cell_metadata.csv         EntityID, fov, volume, center_x, center_y,
                            min_x, min_y, max_x, max_y, ...
  detected_transcripts.csv  barcode_id, global_x, global_y, global_z,
                            x, y, fov, gene, transcript_id, [cell_id]
  cell_boundaries.parquet   MERSCOPE software v232+ — WKB polygons, microns
  cell_boundaries/          v231 and earlier — feature_data_<fov>.hdf5
  images/
    micron_to_mosaic_pixel_transform.csv   3×3 affine, space-delimited
    mosaic_<stain>_z<n>.tif
    manifest.json                          not always present

Coordinates — the thing to get right
------------------------------------
``detected_transcripts.csv`` has **two** coordinate pairs and they are not
interchangeable:

    global_x / global_y   microns, whole-slide frame   ← use these
    x / y                 pixels, FOV-LOCAL frame

`x`/`y` restart near zero in every field of view. Reading them instead of the
global pair stacks all FOVs on top of each other. Measured on Vizgen's own test
dataset, that put transcripts across 843–18111 px while the cells they belong to
spanned 16–3872 px — a 4.7× mismatch, and the transcript layer landed nowhere
near the tissue. `cell_metadata.csv`'s `center_x`/`center_y` are microns in the
same frame as `global_x`/`global_y`, so those two agree and only the transcripts
were wrong.

`pixel_size` comes from ``images/micron_to_mosaic_pixel_transform.csv``, a 3×3
affine mapping microns → mosaic pixels. Its scale term is pixels *per* micron, so
``pixel_size = 1 / M[0][0]``. On the reference data that is 1/9.2625 = 0.10796,
which is where the commonly quoted 0.108 comes from.
"""
import json
import struct
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from app.readers import duck
from app.readers.base_reader import _UNSET, SpatialDatasetReader

# Fallback when the transform file is missing. Matches the measured 0.10796.
_DEFAULT_PIXEL_SIZE = 0.108

_MAX_TRANSCRIPTS = 200_000


def _wkb_polygons(blob: bytes) -> list[list[tuple]]:
    """Decode a WKB Polygon / MultiPolygon into a list of exterior rings.

    Vizgen writes cell boundaries as geoparquet WKB. Decoding the handful of
    geometry types that appear here is a few lines of struct unpacking against
    the OGC spec, which is preferable to adding shapely — a C-extension
    dependency — to a requirements file that is pinned tightly enough to need
    `cffi<2.0` for pyvips.

    Only exterior rings are returned; interior rings (holes) are ignored, since
    the viewer draws filled cell outlines.
    """
    def read_geom(buf: memoryview, off: int) -> tuple[list, int]:
        (order,) = struct.unpack_from("<B", buf, off); off += 1
        e = "<" if order == 1 else ">"
        (gtype,) = struct.unpack_from(e + "I", buf, off); off += 4
        gtype &= 0xFF  # strip SRID/Z/M flags
        rings: list = []
        if gtype == 3:                                   # Polygon
            (n_rings,) = struct.unpack_from(e + "I", buf, off); off += 4
            for r in range(n_rings):
                (n_pts,) = struct.unpack_from(e + "I", buf, off); off += 4
                pts = struct.unpack_from(e + f"{n_pts * 2}d", buf, off)
                off += n_pts * 16
                if r == 0:                               # exterior ring only
                    rings.append(list(zip(pts[0::2], pts[1::2])))
        elif gtype == 6:                                 # MultiPolygon
            (n_poly,) = struct.unpack_from(e + "I", buf, off); off += 4
            for _ in range(n_poly):
                sub, off = read_geom(buf, off)
                rings.extend(sub)
        else:
            raise ValueError(f"unsupported WKB geometry type {gtype}")
        return rings, off

    rings, _ = read_geom(memoryview(blob), 0)
    return rings


class MerscopeReader(SpatialDatasetReader):

    # Root CSVs that are MERSCOPE's own output, so the shared supplemental-metadata
    # loader never mistakes them for user-supplied columns.
    _ROOT_CSV_SKIP = frozenset({
        "cell_by_gene.csv", "cell_metadata.csv", "detected_transcripts.csv",
    })

    def __init__(self, dataset_path: Path):
        super().__init__(dataset_path)
        self._pixel_size_cache: Optional[float] = None
        self._cells_full_cache = _UNSET
        self._boundary_path_cache = _UNSET

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def platform(self) -> str:
        return "merscope"

    @property
    def pixel_size(self) -> float:
        """µm per mosaic pixel, from the micron→mosaic affine."""
        if self._pixel_size_cache is not None:
            return self._pixel_size_cache
        self._pixel_size_cache = _DEFAULT_PIXEL_SIZE
        m = self._transform()
        if m is not None and m[0][0] > 0:
            # The matrix is pixels-per-micron, so invert for microns-per-pixel.
            self._pixel_size_cache = 1.0 / float(m[0][0])
        return self._pixel_size_cache

    def _transform(self) -> Optional[np.ndarray]:
        """The 3×3 micron→mosaic-pixel affine, or None."""
        for p in (self.path / "images" / "micron_to_mosaic_pixel_transform.csv",
                  self.path / "micron_to_mosaic_pixel_transform.csv"):
            if p.exists():
                try:
                    return np.genfromtxt(p)
                except Exception as exc:
                    print(f"[merscope] could not read {p.name}: {exc}")
        return None

    # ── Experiment metadata ───────────────────────────────────────────────────

    def info(self) -> dict:
        data: dict = {"platform": self.platform, "pixel_size": self.pixel_size}
        manifest = self.path / "images" / "manifest.json"
        if not manifest.exists():
            manifest = self.path / "manifest.json"
        if manifest.exists():
            try:
                data.update(json.loads(manifest.read_text()))
            except Exception:
                pass
        data["platform"] = self.platform
        data["pixel_size"] = self.pixel_size
        data["has_boundary_file"] = self._boundary_file() is not None
        return data

    def capabilities(self) -> dict:
        images = self.path / "images"
        return {
            "has_morphology": images.is_dir() and any(images.glob("mosaic_*.tif")),
            "has_transcripts": (self.path / "detected_transcripts.csv").exists(),
            "has_boundaries": self._boundary_file() is not None,
            "unit_label": "cell",
        }

    # ── Gene catalogue ────────────────────────────────────────────────────────

    def gene_list(self) -> list[str]:
        cbg = self.path / "cell_by_gene.csv"
        if not cbg.exists():
            return []
        try:
            cols = duck.csv_columns(cbg)
        except Exception:
            return []
        # First column is the cell key ("cell"); Blank* are control probes.
        return [c for c in cols[1:] if c and not c.lower().startswith("blank")]

    # ── Transcripts ───────────────────────────────────────────────────────────

    def transcripts(
        self,
        bbox: Optional[tuple] = None,
        genes: Optional[list[str]] = None,
        fraction: float = 1.0,
    ) -> dict:
        path = self.path / "detected_transcripts.csv"
        if not path.exists():
            return {"transcripts": [], "total": 0}

        cols = set(duck.csv_columns(path))
        # global_* is the whole-slide micron frame. Plain x/y are FOV-local
        # pixels and must not be used — see the module docstring.
        if {"global_x", "global_y"} <= cols:
            xcol, ycol = "global_x", "global_y"
        else:
            print("[merscope] detected_transcripts.csv has no global_x/global_y; "
                  "cannot place transcripts in the slide frame")
            return {"transcripts": [], "total": 0}
        gene_col = "gene" if "gene" in cols else None

        src_path = spatial_cache_sorted(self, path, xcol, ycol)
        src = duck.scan_any(src_path)

        conditions, params = [], []
        bbox_sql, bbox_params = duck.bbox_predicate(
            xcol, ycol, self._bbox_to_native(bbox) if bbox else None)
        if bbox_sql:
            conditions.append(bbox_sql)
            params.extend(bbox_params)
        if genes and gene_col:
            sql, prm = duck.in_predicate(gene_col, genes)
            conditions.append(sql)
            params.extend(prm)
        where = duck.where_clause(conditions)

        select_cols = [xcol, ycol] + ([gene_col] if gene_col else [])
        select = ", ".join(f'"{c}"' for c in select_cols)

        with duck.connect() as conn:
            total = int(conn.execute(
                f"SELECT COUNT(*) FROM {src} {where}", params).fetchone()[0] or 0)
            if total == 0:
                return {"transcripts": [], "total": 0}
            fraction = max(0.0001, min(1.0, fraction))
            n = min(round(fraction * total), _MAX_TRANSCRIPTS)
            if n <= 0:
                return {"transcripts": [], "total": total}
            sample = duck.reservoir_sample(n) if n < total else ""
            df = conn.execute(
                f"SELECT * FROM (SELECT {select} FROM {src} {where}) {sample}",
                params).df()

        df = df.rename(columns={xcol: "x_location", ycol: "y_location",
                                gene_col: "feature_name"} if gene_col else
                       {xcol: "x_location", ycol: "y_location"})
        ps = self.pixel_size
        df["x_location"] = df["x_location"] / ps
        df["y_location"] = df["y_location"] / ps
        return {"transcripts": duck.to_records(df), "total": total}

    # ── Cells ─────────────────────────────────────────────────────────────────

    def _cells_raw(self) -> Optional[pd.DataFrame]:
        meta = self.path / "cell_metadata.csv"
        if not meta.exists():
            return None
        try:
            df = pd.read_csv(meta)
        except Exception as exc:
            print(f"[merscope] could not read cell_metadata.csv: {exc}")
            return None
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
        if "cell_id" not in df.columns:
            return None
        df["cell_id"] = df["cell_id"].astype(str)
        ps = self.pixel_size
        for c in ("x_centroid", "y_centroid"):
            if c in df.columns:
                df[c] = df[c] / ps          # microns → image pixels
        return df

    def _cells_full(self) -> Optional[pd.DataFrame]:
        if self._cells_full_cache is not _UNSET:
            return self._cells_full_cache  # type: ignore[return-value]
        self._cells_full_cache = self._merge_supplemental(self._cells_raw())
        return self._cells_full_cache  # type: ignore[return-value]

    def cells(self, bbox: Optional[tuple] = None) -> list[dict]:
        df = self._cells_full()
        if df is None:
            return []
        if bbox and {"x_centroid", "y_centroid"} <= set(df.columns):
            xmin, ymin, xmax, ymax = bbox
            if None not in (xmin, ymin, xmax, ymax):
                df = df[(df["x_centroid"] >= xmin) & (df["x_centroid"] <= xmax) &
                        (df["y_centroid"] >= ymin) & (df["y_centroid"] <= ymax)]
        return self._to_records(df)

    def cells_schema(self) -> dict:
        df = self._cells_full()
        if df is None:
            return {"columns": {}}
        return {"columns": {c: str(df[c].dtype) for c in df.columns if c != "cell_id"}}

    def cell_detail(self, cell_id: str) -> Optional[dict]:
        df = self._cells_full()
        if df is None:
            return None
        row = df[df["cell_id"] == str(cell_id)]
        if row.empty:
            return None
        rec = self._to_records(row)[0]
        rec["expression"] = self.cell_expression(cell_id)
        return rec

    # ── Boundaries ────────────────────────────────────────────────────────────

    def _boundary_file(self) -> Optional[Path]:
        """The polygon parquet, under any of the names Vizgen tooling emits."""
        if self._boundary_path_cache is not _UNSET:
            return self._boundary_path_cache  # type: ignore[return-value]
        self._boundary_path_cache = None
        for name in ("cell_boundaries.parquet",      # instrument software v232+
                     "cell_micron_space.parquet",    # vizgen-postprocessing
                     "cellpose_micron_space.parquet",
                     "watershed_micron_space.parquet"):
            p = self.path / name
            if p.exists():
                self._boundary_path_cache = p
                break
        return self._boundary_path_cache  # type: ignore[return-value]

    def cell_boundaries(self, bbox: Optional[tuple] = None,
                        fraction: float = 1.0,
                        cell_ids: Optional[set] = None) -> dict:
        """Cell polygons in pixel space, from the geoparquet boundary file.

        Software v231 and earlier wrote per-FOV HDF5 instead
        (`cell_boundaries/feature_data_<fov>.hdf5`); that path is not implemented,
        and such datasets report `has_boundaries: False` rather than returning
        empty rows.
        """
        path = self._boundary_file()
        if path is None:
            return {"boundaries": [], "total": 0}
        try:
            df = pd.read_parquet(path, columns=["EntityID", "ZIndex", "Geometry"])
        except Exception as exc:
            print(f"[merscope] could not read {path.name}: {exc}")
            return {"boundaries": [], "total": 0}
        if df.empty:
            return {"boundaries": [], "total": 0}

        # One row per (cell × z-plane). Keep a single plane so each cell yields
        # one outline; z 0 is what spatialdata-io uses for de-duplication.
        if "ZIndex" in df.columns:
            zs = sorted(df["ZIndex"].dropna().unique())
            if zs:
                df = df[df["ZIndex"] == zs[0]]

        ps = self.pixel_size
        rows: list[dict] = []
        seen: list[str] = []
        for entity, blob in zip(df["EntityID"].astype(str), df["Geometry"]):
            if blob is None:
                continue
            # Metadata filter (issue #45): skip before decoding the WKB, which is
            # the expensive part, and before `total` so sampling sees the subset.
            if cell_ids is not None and entity not in cell_ids:
                continue
            try:
                rings = _wkb_polygons(bytes(blob))
            except Exception:
                continue
            if not rings:
                continue
            # Largest ring: a cell occasionally decodes to several fragments.
            ring = max(rings, key=len)
            # Geoparquet coordinates are microns; convert to image pixels.
            pts = [(x / ps, y / ps) for x, y in ring]
            if len(pts) > 1 and pts[0] == pts[-1]:
                pts = pts[:-1]          # deck.gl closes rings itself
            if pts:
                seen.append(entity)
                rows.append({"cell_id": entity, "pts": pts})

        if bbox and None not in bbox:
            xmin, ymin, xmax, ymax = bbox
            rows = [r for r in rows
                    if any(xmin <= x <= xmax and ymin <= y <= ymax for x, y in r["pts"])]
        total = len(rows)
        if total == 0:
            return {"boundaries": [], "total": 0}

        fraction = max(0.0001, min(1.0, fraction))
        n = round(fraction * total)
        if n <= 0:
            return {"boundaries": [], "total": total}
        if n < total:
            rows = [rows[i] for i in np.linspace(0, total - 1, n).astype(int)]

        out = [{"cell_id": r["cell_id"], "vertex_x": x, "vertex_y": y}
               for r in rows for x, y in r["pts"]]
        return {"boundaries": out, "total": total}

    # ── Expression ────────────────────────────────────────────────────────────

    def cell_expression(self, cell_id: str) -> dict:
        cbg = self.path / "cell_by_gene.csv"
        if not cbg.exists():
            return {}
        try:
            cols = duck.csv_columns(cbg)
            key = cols[0]
            with duck.connect() as conn:
                df = conn.execute(
                    f'SELECT * FROM {duck.scan_csv(cbg)} WHERE CAST("{key}" AS VARCHAR) = ?',
                    [str(cell_id)]).df()
            if df.empty:
                return {}
            row = df.iloc[0]
            return {g: int(v) for g, v in row.items()
                    if g != key and not str(g).lower().startswith("blank")
                    and pd.notna(v) and v > 0}
        except Exception as exc:
            print(f"[merscope] cell_expression failed: {exc}")
            return {}

    # ── Colour values ─────────────────────────────────────────────────────────

    def color_values(self, mode: str, field: Optional[str] = None,
                     genes: Optional[list[str]] = None,
                     categorical: Optional[bool] = None) -> dict:
        if mode == "gene_set":
            return self._color_values_gene_set(genes or [])
        return self._color_values_meta(field or "", categorical)

    def _metadata_frame(self):
        return self._cells_full()

    def _color_values_gene_set(self, genes: list[str]) -> dict:
        empty = {"type": "continuous", "values": {}, "min": 0.0, "max": 0.0}
        cbg = self.path / "cell_by_gene.csv"
        if not cbg.exists() or not genes:
            return empty
        try:
            cols = duck.csv_columns(cbg)
            key = cols[0]
            want = [g for g in genes if g in cols]
            if not want:
                return empty
            expr = " + ".join(f'COALESCE("{g}", 0)' for g in want)
            with duck.connect() as conn:
                df = conn.execute(
                    f'SELECT CAST("{key}" AS VARCHAR) AS cid, {expr} AS total '
                    f"FROM {duck.scan_csv(cbg)}").df()
        except Exception as exc:
            print(f"[merscope] gene-set colouring failed: {exc}")
            return empty
        if df.empty:
            return empty
        vals = dict(zip(df["cid"], df["total"].astype(float)))
        vmax = float(df["total"].max())
        return {"type": "continuous", "values": vals,
                "min": 0.0, "max": vmax if vmax > 0 else 1.0}


def spatial_cache_sorted(reader, path: Path, xcol: str, ycol: str) -> Path:
    """Spatially-sorted copy of a transcript table, or the original."""
    from app.readers import spatial_cache
    return spatial_cache.sorted_path(path, reader.path, xcol, ycol) or path
