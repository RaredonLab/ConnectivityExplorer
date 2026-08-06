"""
Abstract base class for spatial transcriptomics dataset readers.

Each concrete reader handles one platform's output format and is responsible
for normalizing all coordinates to image pixel space before returning data.
The router layer is platform-agnostic — it only calls the methods defined here.
"""
import math
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

import pandas as pd

from app.readers import metadata_filter, supplemental
from app.readers.metadata_filter import MetadataFilter


_UNSET = object()  # sentinel: "not yet loaded" vs "loaded, no data"


class SpatialDatasetReader(ABC):
    """
    Interface every platform reader must satisfy.

    Coordinate contract
    -------------------
    All coordinates returned to the API are in **image pixel space**:
      native_coord / pixel_size → pixel_coord
    Each reader is responsible for this conversion internally.  The frontend
    always receives pixel coordinates and never needs to know the native unit.
    """

    # Filenames in the dataset root that belong to the platform itself rather than
    # to the user, and must never be picked up as supplemental metadata. Subclasses
    # override this with their own output filenames. `.csv.gz` at the root is always
    # skipped, since every platform uses that only for its own data files.
    _ROOT_CSV_SKIP: frozenset = frozenset()

    # Suffix patterns for platforms whose output filenames carry a run-specific
    # prefix (CosMx writes `<experiment>_tx_file.csv`), where an exact-name set
    # cannot work.
    _ROOT_CSV_SKIP_SUFFIXES: tuple = ()

    def __init__(self, dataset_path: Path):
        self.path = dataset_path
        self._supp_meta_cache = _UNSET
        self._filter_id_cache: dict = {}

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    @abstractmethod
    def platform(self) -> str:
        """Short lowercase platform identifier, e.g. 'xenium', 'merscope', 'cosmx'."""
        ...

    @property
    @abstractmethod
    def pixel_size(self) -> float:
        """Native coordinate units per image pixel (e.g. µm/px).
        Used to convert native → pixel space: pixel = native / pixel_size."""
        ...

    # ── Experiment metadata ───────────────────────────────────────────────────

    @abstractmethod
    def info(self) -> dict:
        """Experiment-level metadata as a JSON-serializable dict.
        Must include at minimum: {'platform': self.platform}."""
        ...

    # ── Gene catalogue ────────────────────────────────────────────────────────

    @abstractmethod
    def gene_list(self) -> list[str]:
        """All assayed gene names, excluding controls and blanks."""
        ...

    # ── Spatial data ──────────────────────────────────────────────────────────

    @abstractmethod
    def transcripts(
        self,
        bbox: Optional[tuple] = None,
        genes: Optional[list[str]] = None,
        fraction: float = 1.0,
    ) -> dict:
        """Transcript records in pixel space.
        Returns {"transcripts": list[dict], "total": int} where total is the
        pre-sample count after bbox/gene filtering.
        Required keys per record: x_location, y_location, feature_name."""
        ...

    @abstractmethod
    def cells(self, bbox: Optional[tuple] = None) -> list[dict]:
        """Cell records in pixel space.
        Required keys: cell_id, x_centroid, y_centroid."""
        ...

    @abstractmethod
    def cells_schema(self) -> dict:
        """Column names and dtype strings: {'columns': {col: dtype_str, ...}}."""
        ...

    @abstractmethod
    def cell_boundaries(
        self,
        bbox: Optional[tuple] = None,
        fraction: float = 1.0,
        cell_ids: Optional[set] = None,
    ) -> dict:
        """Boundary vertex records in pixel space.

        Returns {"boundaries": list[dict], "total": int}; each record carries
        cell_id, vertex_x, vertex_y.

        fraction : 0 < f ≤ 1.0 — randomly sample this fraction of cells in viewport.
        cell_ids : when given, restrict to these cell ids (issue #45's metadata
            filter, already resolved by `filter_cell_ids`).

        **Apply `cell_ids` before sampling and before computing `total`.** Sampling
        first would draw from the whole viewport and leave only the small fraction
        of the subset that happened to survive, so filtering to a rare cluster would
        empty the canvas rather than isolate it.
        """
        ...

    @abstractmethod
    def cell_detail(self, cell_id: str) -> Optional[dict]:
        """Full metadata + expression for one cell, or None if not found.
        Must include an 'expression' key: {gene: count}."""
        ...

    @abstractmethod
    def cell_expression(self, cell_id: str) -> dict:
        """Non-zero gene expression counts for one cell: {gene: count}."""
        ...

    @abstractmethod
    def color_values(
        self,
        mode: str,
        field: Optional[str] = None,
        genes: Optional[list[str]] = None,
        categorical: Optional[bool] = None,
    ) -> dict:
        """Per-cell values for coloring.
        Returns one of:
          {type:'continuous', values:{cell_id:float}, min:float, max:float}
          {type:'categorical', values:{cell_id:str},  categories:[str,...]}

        `categorical` is the user's explicit override for a metadata column:
        None auto-detects, True/False force the interpretation (issue #35).
        """
        ...

    # ── Metadata typing, colouring and filtering (platform-agnostic) ──────────
    #
    # These sit on the base class because they are pure pandas over whatever frame
    # the reader calls its cells table. Every platform previously carried its own
    # near-identical copy of `_color_values_meta`, and the copies had already
    # drifted — one filled NaN with 0, others dropped it; some sorted categories
    # with `key=str` and some without. All a reader supplies now is the frame.

    def _metadata_frame(self) -> Optional[pd.DataFrame]:
        """The cells table used for colouring and filtering, including supplemental
        columns, or None. Must contain a `cell_id` column.

        Readers override this to point at whatever they already build — usually
        `_cells_full()`. The default returns None, which degrades to "no metadata
        columns" rather than raising.
        """
        return None

    def _color_values_meta(self, field: str,
                           categorical: Optional[bool] = None) -> dict:
        """Per-cell values for one metadata column, typed for the frontend."""
        empty = {"type": "continuous", "values": {}, "min": 0.0, "max": 0.0}
        df = self._metadata_frame()
        if df is None or df.empty or "cell_id" not in df.columns \
                or field not in df.columns:
            return empty

        col = df[field]
        ids = df["cell_id"].astype(str).tolist()
        has = col.notna().tolist()

        if metadata_filter.is_categorical(col, categorical):
            labels = col.astype(str).tolist()
            values = {ids[i]: labels[i] for i in range(len(ids)) if has[i]}
            return {
                "type": "categorical",
                "values": values,
                "categories": metadata_filter.sort_categories(set(values.values())),
            }

        # to_numeric rather than float(): a forced-continuous request can land on a
        # column holding stray text, and coercing those rows to NaN drops them the
        # same way a genuinely missing value is dropped.
        numeric = pd.to_numeric(col, errors="coerce")
        valid = numeric.notna().tolist()
        vals = numeric.tolist()
        values = {ids[i]: float(vals[i]) for i in range(len(ids)) if valid[i]}
        if not values:
            return empty
        finite = [v for v in values.values() if math.isfinite(v)]
        if not finite:
            return empty
        return {"type": "continuous", "values": values,
                "min": min(finite), "max": max(finite)}

    def filter_cell_ids(self, spec: Optional[MetadataFilter]) -> Optional[set]:
        """Resolve a metadata filter to the set of cell ids it keeps (issue #45).

        Returns None when there is nothing to filter, so callers can pass the
        result straight through and treat None as "no restriction".

        Raises ValueError for an unknown column. Silently ignoring it would render
        the full dataset while the panel showed an active filter, which reads as
        the filter being broken rather than misspelled.

        Cached per (reader instance, filter), since the same filter is re-resolved
        on every viewport change while the user pans.
        """
        if spec is None:
            return None
        df = self._metadata_frame()
        if df is None or "cell_id" not in df.columns:
            raise ValueError("this dataset exposes no cell metadata to filter on")
        if spec.field not in df.columns:
            raise ValueError(f"unknown cell metadata column '{spec.field}'")
        if spec in self._filter_id_cache:
            return self._filter_id_cache[spec]
        keep = df.loc[spec.mask(df[spec.field]), "cell_id"].astype(str)
        ids = set(keep.tolist())
        self._filter_id_cache[spec] = ids
        return ids

    # ── Platform capabilities ─────────────────────────────────────────────────

    def capabilities(self) -> dict:
        """Platform capability flags consumed by the frontend to show/hide layers.

        Defaults represent a fully-featured imaging-based platform.  Override in
        readers that lack specific capabilities (e.g. spot-based platforms with
        no per-molecule transcript coordinates or no polygon boundaries).

        Keys
        ----
        has_morphology  : bool  — dataset includes a tile-able morphology image
        has_transcripts : bool  — individual molecule/transcript detections are available
        has_boundaries  : bool  — polygon cell/spot boundary vertices are available
        unit_label      : str   — display name for spatial units ("cell", "spot", "bin")
        """
        return {
            "has_morphology": True,
            "has_transcripts": True,
            "has_boundaries": True,
            "unit_label": "cell",
        }

    def data_extent(self) -> Optional[tuple]:
        """Bounding box of this dataset's units in image pixel space.

        Used to size a placeholder canvas for datasets that ship no morphology
        image — the viewer derives its whole coordinate space from the tile
        pyramid, so without one there is nothing for deck.gl to draw onto.

        Returns (xmin, ymin, xmax, ymax), or None when there is nothing to
        measure. The default reads centroids from `cells()`; a reader with a
        cheaper source should override.
        """
        try:
            cells = self.cells()
        except Exception:
            return None
        xs = [c["x_centroid"] for c in cells
              if c.get("x_centroid") is not None and math.isfinite(c["x_centroid"])]
        ys = [c["y_centroid"] for c in cells
              if c.get("y_centroid") is not None and math.isfinite(c["y_centroid"])]
        if not xs or not ys:
            return None
        return (min(xs), min(ys), max(xs), max(ys))

    # ── Supplemental cell metadata (platform-agnostic) ────────────────────────
    #
    # Users add their own per-cell columns (clusters, pseudotime, phenotype calls)
    # by dropping CSV/parquet into a `cell-metadata/` subdirectory, without touching
    # the platform's own output. These live on the base class so every reader gets
    # the feature; only `_ROOT_CSV_SKIP` is platform-specific.

    def _load_supplemental_metadata(self) -> Optional[pd.DataFrame]:
        """
        Merge user-defined cell metadata from:
          1. {dataset}/cell-metadata/  — all CSV / parquet
          2. {dataset}/                — plain .csv only, skipping platform filenames
        Multiple files are outer-joined on cell_id.  Cached per reader instance.

        The loading itself lives in `supplemental.py`, shared with the `edge-metadata/`
        feature so the two cannot drift apart.
        """
        if self._supp_meta_cache is not _UNSET:
            return self._supp_meta_cache  # type: ignore[return-value]
        files = supplemental.collect_files(
            self.path / "cell-metadata",
            root=self.path,
            root_filter=lambda n: not self._is_platform_csv(n),
        )
        self._supp_meta_cache = supplemental.load_supplemental(
            files, key="cell_id", log_prefix=self.platform
        )
        return self._supp_meta_cache  # type: ignore[return-value]

    def _is_platform_csv(self, lowercase_name: str) -> bool:
        """True if a root-level CSV is the platform's own output, not user metadata."""
        if lowercase_name in self._ROOT_CSV_SKIP:
            return True
        return any(lowercase_name.endswith(s) for s in self._ROOT_CSV_SKIP_SUFFIXES)

    def _merge_supplemental(self, cells: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
        """Left-join supplemental metadata onto a platform cells table.

        Either side may be absent: with no supplemental files this returns `cells`
        unchanged, and with no cells table it returns the supplemental frame alone
        (so metadata-only datasets still expose their columns).
        """
        supp = self._load_supplemental_metadata()
        if cells is None and supp is None:
            return None
        if supp is None:
            return cells
        if cells is None:
            return supp
        new_cols = [c for c in supp.columns if c not in cells.columns]
        if not new_cols:
            return cells
        return cells.merge(supp[["cell_id"] + new_cols], on="cell_id", how="left")

    # ── Shared utilities ──────────────────────────────────────────────────────

    def _to_px(self, val: float) -> float:
        """Convert one native coordinate to pixel space."""
        return val / self.pixel_size

    def _bbox_to_native(self, bbox: tuple) -> tuple:
        """Convert an image-pixel bounding box to native coordinate space."""
        xmin, ymin, xmax, ymax = bbox
        ps = self.pixel_size
        return xmin * ps, ymin * ps, xmax * ps, ymax * ps

    @staticmethod
    def _to_records(df: pd.DataFrame) -> list[dict]:
        """Convert a DataFrame to JSON-safe records (NaN/Inf → None)."""
        return [
            {k: (None if isinstance(v, float) and not math.isfinite(v) else v)
             for k, v in row.items()}
            for row in df.to_dict(orient="records")
        ]
