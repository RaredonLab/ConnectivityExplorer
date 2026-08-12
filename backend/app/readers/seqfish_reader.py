"""
seqFISH (Spatial Genomics GenePS) dataset reader.

Note this is the *commercial* platform. "seqFISH" also names the academic
Cai-lab method, which has no standard output layout; that is not what this
reads.

Expected output layout — current "v2" format
--------------------------------------------
A flat directory. Every file is prefixed with an ROI name; TissuePlex expects
one ROI per dataset folder.

  <roi>_CellCoordinates.csv    label, area, center_x, center_y
  <roi>_CellxGene.csv          unnamed first col = label; remaining cols = genes
  <roi>_TranscriptList.csv     name, x, y, [z]
  <roi>_DAPI.tiff              OME-TIFF (OME-XML despite the .tiff extension)
  <roi>_Segmentation.tiff      integer label mask
  <roi>_Boundaries.geojson     cell polygons, feature id == label

Legacy "v1" format
------------------
  <prefix>_CellCoordinates_section<N>.csv
  <prefix>_CxG_section<N>.csv
  <prefix>_TranscriptCoordinates_section<N>.csv
  <prefix>_DAPI_section<N>.ome.tiff
  <prefix>_CellMask_section<N>.tiff
  (no boundaries file — v1 ships only the label mask)

v1 transcripts carry a `cell` assignment column that v2 dropped; v2 adds `z`.
v1 has no GeoJSON, so it reports has_boundaries=False until mask polygonisation
is implemented.

Coordinates — the part that matters
-----------------------------------
A single seqFISH dataset mixes units, verified against the reference dataset:
its DAPI is 1000x1000 px at 0.107161 µm/px (107.16 µm across), and

  CellCoordinates center_x  1.82 -> 105.66   microns
  TranscriptList  x         0.00 -> 107.05   microns
  Boundaries      vertices  0    -> 999      pixels

So cells and transcripts must be divided by pixel_size while boundaries pass
through untouched. Applying one transform to everything puts cells and their own
outlines in different places, which reads as a rendering bug rather than a unit
bug. Worse, the convention differs across GenePS software versions, so it cannot
simply be hard-coded.

`_units_divisor()` therefore decides per table by comparing the table's extent to
the image width: a ratio near pixel_size means microns, a ratio near 1.0 means
pixels. On the reference dataset those ratios are 0.106 / 0.107 / 0.999, which
separates the cases by two orders of magnitude. The verdict is logged on load.
"""
import json
import math
import re
from pathlib import Path
from typing import Optional

import pandas as pd

from app.readers import duck, spatial_cache
from app.readers.base_reader import _UNSET, SpatialDatasetReader

# Fallback when the DAPI OME-XML carries no PhysicalSizeX. This is the documented
# GenePS value and matches the reference dataset (0.107161).
_DEFAULT_PIXEL_SIZE = 0.107

_MAX_TRANSCRIPTS = 200_000


class SeqfishReader(SpatialDatasetReader):

    # seqFISH writes only ROI-prefixed CSVs, so exact names cannot be used.
    _ROOT_CSV_SKIP_SUFFIXES = (
        "_cellcoordinates.csv", "_cellxgene.csv", "_transcriptlist.csv",
        "_transcriptcoordinates.csv", "_cxg.csv",
    )

    def __init__(self, dataset_path: Path):
        super().__init__(dataset_path)
        self._layout_cache = _UNSET
        self._pixel_size_cache: Optional[float] = None
        self._image_size_cache: Optional[tuple] = None
        self._cxg_cache = _UNSET
        self._cells_full_cache = _UNSET
        self._divisor_log: set = set()

    # ── Layout discovery ──────────────────────────────────────────────────────

    @staticmethod
    def find_cell_coordinates(path: Path) -> list[Path]:
        """Every CellCoordinates file in a folder, v2 and v1 naming alike.

        This is also the platform sentinel — `ReaderFactory` calls it to detect
        seqFISH, so it must stay cheap and must not raise on odd directories.
        """
        try:
            return sorted(
                f for f in path.glob("*_CellCoordinates*.csv") if f.is_file()
            )
        except OSError:
            return []

    def _layout(self) -> dict:
        """Resolve the ROI prefix, format variant, and every member file path."""
        if self._layout_cache is not _UNSET:
            return self._layout_cache  # type: ignore[return-value]

        matches = self.find_cell_coordinates(self.path)
        if not matches:
            self._layout_cache = {}
            return {}

        if len(matches) > 1:
            print(f"[seqfish] {self.path.name}: {len(matches)} ROIs present "
                  f"({', '.join(m.name for m in matches)}); using {matches[0].name}. "
                  f"TissuePlex expects one ROI per folder — split them to see the rest.")

        chosen = matches[0]
        v1 = re.match(r"^(.*)_CellCoordinates_(section\d+)\.csv$", chosen.name)
        if v1:
            prefix, section = v1.group(1), v1.group(2)
            roi = f"{prefix}_{section}"
            layout = {
                "variant": "v1",
                "roi": roi,
                "cells": chosen,
                "counts": self._first(f"{prefix}_CxG_{section}.csv"),
                "transcripts": self._first(f"{prefix}_TranscriptCoordinates_{section}.csv"),
                "boundaries": None,   # v1 ships no GeoJSON
                "mask": self._first(f"{prefix}_CellMask_{section}.tiff"),
                "image": (self._first(f"{prefix}_DAPI_{section}.ome.tiff")
                          or self._first(f"{prefix}_DAPI_{section}.tiff")),
            }
        else:
            roi = chosen.name[: -len("_CellCoordinates.csv")]
            layout = {
                "variant": "v2",
                "roi": roi,
                "cells": chosen,
                "counts": self._first(f"{roi}_CellxGene.csv"),
                "transcripts": self._first(f"{roi}_TranscriptList.csv"),
                "boundaries": self._first(f"{roi}_Boundaries.geojson"),
                "mask": self._first(f"{roi}_Segmentation.tiff"),
                "image": (self._first(f"{roi}_DAPI.tiff")
                          or self._first(f"{roi}_DAPI.ome.tiff")),
            }
        self._layout_cache = layout
        return layout

    def _first(self, name: str) -> Optional[Path]:
        p = self.path / name
        return p if p.exists() else None

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def platform(self) -> str:
        return "seqfish"

    @property
    def pixel_size(self) -> float:
        """µm per image pixel, from the DAPI OME-XML PhysicalSizeX."""
        if self._pixel_size_cache is not None:
            return self._pixel_size_cache
        self._pixel_size_cache = _DEFAULT_PIXEL_SIZE
        img = self._layout().get("image")
        if img is not None:
            try:
                import tifffile
                with tifffile.TiffFile(img) as tif:
                    ome = tif.ome_metadata
                if ome:
                    m = re.search(r'PhysicalSizeX="([0-9.eE+-]+)"', ome)
                    if m:
                        val = float(m.group(1))
                        if val > 0:
                            self._pixel_size_cache = val
            except Exception as exc:
                print(f"[seqfish] could not read PhysicalSizeX from {img.name}: {exc}; "
                      f"falling back to {_DEFAULT_PIXEL_SIZE} µm/px")
        return self._pixel_size_cache

    def _image_size(self) -> Optional[tuple]:
        """(width, height) of the DAPI image in pixels, or None."""
        if self._image_size_cache is not None:
            return self._image_size_cache
        img = self._layout().get("image")
        if img is None:
            return None
        try:
            import tifffile
            with tifffile.TiffFile(img) as tif:
                shape = tif.series[0].shape
            h, w = shape[-2], shape[-1]
            self._image_size_cache = (int(w), int(h))
        except Exception as exc:
            print(f"[seqfish] could not read image dimensions from {img.name}: {exc}")
            return None
        return self._image_size_cache

    # ── Unit detection ────────────────────────────────────────────────────────

    def _units_divisor(self, max_x: float, max_y: float, label: str) -> float:
        """Return the divisor converting a table's coordinates to image pixels.

        A table in microns spans about `image_width_px * pixel_size`, so its
        max/width ratio lands near `pixel_size`. A table already in pixels spans
        the image itself, so the ratio lands near 1.0. Pick whichever hypothesis
        the observed ratio is closer to, in log space so the comparison is
        scale-free.
        """
        size = self._image_size()
        ps = self.pixel_size
        if not size or max_x <= 0 or ps <= 0:
            return 1.0
        width, height = size
        ratio = max(max_x / width, max_y / height) if height else max_x / width
        if ratio <= 0:
            return 1.0
        d_um = abs(math.log(ratio) - math.log(ps))
        d_px = abs(math.log(ratio) - math.log(1.0))
        micron = d_um < d_px
        if label not in self._divisor_log:
            self._divisor_log.add(label)
            print(f"[seqfish] {label}: extent ratio {ratio:.4f} vs pixel_size {ps:.6f} "
                  f"→ treating as {'MICRONS (divide by pixel_size)' if micron else 'PIXELS (no conversion)'}")
        return ps if micron else 1.0

    # ── Experiment metadata ───────────────────────────────────────────────────

    def info(self) -> dict:
        lay = self._layout()
        size = self._image_size()
        return {
            "platform": self.platform,
            "format_variant": lay.get("variant"),
            "roi": lay.get("roi"),
            "pixel_size": self.pixel_size,
            "image_width_px": size[0] if size else None,
            "image_height_px": size[1] if size else None,
        }

    def capabilities(self) -> dict:
        lay = self._layout()
        return {
            "has_morphology": lay.get("image") is not None,
            "has_transcripts": lay.get("transcripts") is not None,
            # v1 ships only a label mask; polygonising it is not implemented, so
            # the layer is declared unavailable rather than returning empty rows.
            "has_boundaries": lay.get("boundaries") is not None,
            "unit_label": "cell",
        }

    # ── Gene catalogue ────────────────────────────────────────────────────────

    def gene_list(self) -> list[str]:
        counts = self._layout().get("counts")
        if counts is None:
            return []
        try:
            cols = duck.csv_columns(counts)
        except Exception:
            return []
        # First column is the unnamed cell label; the rest are genes.
        return [c for c in cols[1:] if c]

    # ── Cells ─────────────────────────────────────────────────────────────────

    def _cells_raw(self) -> Optional[pd.DataFrame]:
        cells = self._layout().get("cells")
        if cells is None:
            return None
        df = pd.read_csv(cells)
        if "label" not in df.columns:
            return None
        out = pd.DataFrame({"cell_id": df["label"].astype(str)})
        div = self._units_divisor(
            float(df["center_x"].max()), float(df["center_y"].max()), "cells"
        )
        out["x_centroid"] = df["center_x"] / div
        out["y_centroid"] = df["center_y"] / div
        if "area" in df.columns:
            # cell_area stays in µm², matching Xenium — which never converts it — so the
            # "µm²" label in CellInfoPanel is true on every platform. Area arrives in the
            # same space as the centroids, squared: already µm² when the table is in
            # microns (div == pixel_size), px² when it is in pixels (div == 1.0).
            ps = self.pixel_size
            out["cell_area"] = df["area"] * ((ps / div) ** 2)

        # Per-cell counts from CellxGene. seqFISH v2 ships no transcript→cell
        # assignment, so unlike Xenium there is no molecule table to count; the
        # authoritative per-cell total is the CellxGene row sum (total detected
        # transcripts across all genes). Populate both fields the CellInfoPanel
        # renders — otherwise "transcripts" / "total counts" sit blank on every
        # seqFISH cell. Reuses the cached CellxGene frame, joined on cell_id.
        cxg = self._cxg()
        if cxg is not None:
            totals = cxg.sum(axis=1)  # Series indexed by cell_id
            counts = out["cell_id"].map(totals).fillna(0).astype(int)
            out["transcript_counts"] = counts
            out["total_counts"] = counts
        return out

    def _cells_full(self) -> Optional[pd.DataFrame]:
        if self._cells_full_cache is not _UNSET:
            return self._cells_full_cache  # type: ignore[return-value]
        self._cells_full_cache = self._merge_supplemental(self._cells_raw())
        return self._cells_full_cache  # type: ignore[return-value]

    def cells(self, bbox: Optional[tuple] = None) -> list[dict]:
        df = self._cells_full()
        if df is None:
            return []
        if bbox:
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
        record = self._to_records(row)[0]
        record["expression"] = self.cell_expression(cell_id)
        return record

    # ── Transcripts ───────────────────────────────────────────────────────────

    def transcripts(
        self,
        bbox: Optional[tuple] = None,
        genes: Optional[list[str]] = None,
        fraction: float = 1.0,
    ) -> dict:
        path = self._layout().get("transcripts")
        if path is None:
            return {"transcripts": [], "total": 0}

        cols = duck.csv_columns(path)
        if not {"x", "y"} <= set(cols):
            return {"transcripts": [], "total": 0}
        # For seqFISH this does double duty: CSV cannot be range-scanned or
        # row-group pruned at all, so the cache converts to parquet *and* sorts
        # spatially in one pass. Falls back to the CSV transparently.
        path = spatial_cache.sorted_path(path, self.path, "x", "y") or path
        src = duck.scan_any(path)

        # Detect units from the full extent once, then express the bbox in the
        # file's own space so the predicate can be pushed into the scan.
        with duck.connect() as conn:
            mx, my = conn.execute(f"SELECT MAX(x), MAX(y) FROM {src}").fetchone()
            div = self._units_divisor(float(mx or 0), float(my or 0), "transcripts")

            conditions: list[str] = []
            params: list = []
            if bbox and None not in bbox:
                native = tuple(v * div for v in bbox)
                sql, prm = duck.bbox_predicate("x", "y", native)
                if sql:
                    conditions.append(sql)
                    params.extend(prm)
            if genes and "name" in cols:
                sql, prm = duck.in_predicate("name", genes)
                conditions.append(sql)
                params.extend(prm)
            where = duck.where_clause(conditions)

            total = int(conn.execute(
                f"SELECT COUNT(*) FROM {src} {where}", params).fetchone()[0] or 0)
            if total == 0:
                return {"transcripts": [], "total": 0}

            fraction = max(0.0001, min(1.0, fraction))
            n = min(round(fraction * total), _MAX_TRANSCRIPTS)
            if n <= 0:
                return {"transcripts": [], "total": total}
            sample = duck.reservoir_sample(n) if n < total else ""
            select = ", ".join(f'"{c}"' for c in ("name", "x", "y") if c in cols)
            df = conn.execute(
                f"SELECT * FROM (SELECT {select} FROM {src} {where}) {sample}", params
            ).df()

        df = df.rename(columns={"name": "feature_name",
                                "x": "x_location", "y": "y_location"})
        df["x_location"] = df["x_location"] / div
        df["y_location"] = df["y_location"] / div
        return {"transcripts": duck.to_records(df), "total": total}

    # ── Boundaries ────────────────────────────────────────────────────────────

    def cell_boundaries(self, bbox: Optional[tuple] = None,
                        fraction: float = 1.0,
                        cell_ids: Optional[set] = None) -> dict:
        """Polygon vertices in pixel space, as long-format {cell_id, vertex_x,
        vertex_y} rows so the frontend needs no seqFISH-specific handling.

        Cell identity comes from each GeoJSON feature's `id`, which the reference
        dataset confirms equals `label`. spatialdata-io instead maps polygons to
        cells positionally and has an open issue about the fragility of that; a
        silent off-by-one would draw every outline on the wrong cell, so we join
        on the id and fall back to position only when it is absent.
        """
        path = self._layout().get("boundaries")
        if path is None:
            return {"boundaries": [], "total": 0}
        try:
            with open(path) as fh:
                gj = json.load(fh)
        except Exception as exc:
            print(f"[seqfish] could not read {path.name}: {exc}")
            return {"boundaries": [], "total": 0}

        features = gj.get("features") or []
        polys: list[tuple] = []          # (cell_id, [(x, y), ...])
        for i, feat in enumerate(features):
            geom = feat.get("geometry") or {}
            gtype, coords = geom.get("type"), geom.get("coordinates")
            if not coords:
                continue
            fid = feat.get("id")
            if fid is None:
                fid = (feat.get("properties") or {}).get("label", i + 1)
            cid = str(fid)
            rings = [coords[0]] if gtype == "Polygon" else \
                    [part[0] for part in coords] if gtype == "MultiPolygon" else []
            for ring in rings:
                pts = [(float(p[0]), float(p[1])) for p in ring if len(p) >= 2]
                # GeoJSON rings repeat the first vertex to close; deck.gl closes
                # polygons itself and Xenium boundaries do not repeat, so drop it.
                if len(pts) > 1 and pts[0] == pts[-1]:
                    pts = pts[:-1]
                if pts:
                    polys.append((cid, pts))

        # Metadata filter (issue #45) — applied before the bbox so it also governs
        # `total`, and therefore the fraction the frontend asks for next.
        if cell_ids is not None:
            polys = [(cid, pts) for cid, pts in polys if cid in cell_ids]

        if not polys:
            return {"boundaries": [], "total": 0}

        all_x = [p[0] for _, pts in polys for p in pts]
        all_y = [p[1] for _, pts in polys for p in pts]
        div = self._units_divisor(max(all_x), max(all_y), "boundaries")

        # A cell qualifies if any vertex is in view, and then all of its vertices
        # are returned — same rule as the Xenium reader, so polygons are never
        # clipped into torn shapes at the viewport edge.
        if bbox and None not in bbox:
            xmin, ymin, xmax, ymax = bbox
            polys = [
                (cid, pts) for cid, pts in polys
                if any(xmin <= x / div <= xmax and ymin <= y / div <= ymax
                       for x, y in pts)
            ]
        if not polys:
            return {"boundaries": [], "total": 0}

        cell_ids = sorted({cid for cid, _ in polys})
        total = len(cell_ids)
        fraction = max(0.0001, min(1.0, fraction))
        n = round(fraction * total)
        if n <= 0:
            return {"boundaries": [], "total": total}
        if n < total:
            # Deterministic subset: re-fetching an unchanged viewport must return
            # the same cells or the layer flickers.
            step = total / n
            keep = {cell_ids[min(total - 1, int(i * step))] for i in range(n)}
            polys = [(cid, pts) for cid, pts in polys if cid in keep]

        rows = [
            {"cell_id": cid, "vertex_x": x / div, "vertex_y": y / div}
            for cid, pts in polys for x, y in pts
        ]
        return {"boundaries": rows, "total": total}

    # ── Expression ────────────────────────────────────────────────────────────

    def _cxg(self) -> Optional[pd.DataFrame]:
        """Cell × gene counts, indexed by cell_id (string)."""
        if self._cxg_cache is not _UNSET:
            return self._cxg_cache  # type: ignore[return-value]
        counts = self._layout().get("counts")
        self._cxg_cache = None
        if counts is not None:
            try:
                df = pd.read_csv(counts, index_col=0)
                df.index = df.index.astype(str)
                df.index.name = "cell_id"
                self._cxg_cache = df
            except Exception as exc:
                print(f"[seqfish] could not read {counts.name}: {exc}")
        return self._cxg_cache  # type: ignore[return-value]

    def cell_expression(self, cell_id: str) -> dict:
        df = self._cxg()
        if df is None or str(cell_id) not in df.index:
            return {}
        row = df.loc[str(cell_id)]
        return {g: int(v) for g, v in row.items()
                if isinstance(v, (int, float)) and v > 0}

    # ── Color values ──────────────────────────────────────────────────────────

    def color_values(self, mode: str, field: Optional[str] = None,
                     genes: Optional[list[str]] = None,
                     categorical: Optional[bool] = None) -> dict:
        if mode == "gene_set":
            return self._color_values_gene_set(genes or [])
        return self._color_values_meta(field or "", categorical)

    def _metadata_frame(self):
        return self._cells_full()

    def _color_values_gene_set(self, genes: list[str]) -> dict:
        df = self._cxg()
        empty = {"type": "continuous", "values": {}, "min": 0.0, "max": 0.0}
        if df is None or not genes:
            return empty
        cols = [g for g in genes if g in df.columns]
        if not cols:
            return empty
        summed = df[cols].sum(axis=1)
        return {
            "type": "continuous",
            "values": {str(k): float(v) for k, v in summed.items()},
            "min": 0.0,
            "max": float(summed.max()) if len(summed) and summed.max() > 0 else 1.0,
        }

