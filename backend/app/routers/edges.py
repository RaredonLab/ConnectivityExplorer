"""
Edge data router.
Serves edge-list data (x1,y1 → x2,y2 + arbitrary metadata columns) from
one or more Parquet files in the dataset folder.

Edges represent any kind of cell-to-cell relationship: ligand-receptor
mechanisms, morphogen gradients, proximity scores, etc. The schema is
open — any column beyond the required spatial ones is a filterable attribute.

Required columns in edges.parquet (or any registered edge file):
    x1, y1  — source cell centroid (Xenium pixel coords)
    x2, y2  — target cell centroid

All other columns are optional metadata that the frontend can filter/color by.
"""
from fastapi import APIRouter, HTTPException, Query
from pathlib import Path
from pydantic import BaseModel
import os
from typing import Optional, List

from app.readers.edge_reader import EdgeReader
from app.readers.metadata_filter import MetadataFilter

router = APIRouter()

DATA_ROOT = Path(os.getenv("DATA_ROOT", "/data"))

# Module-level cache: (dataset, edge_file) → EdgeReader instance.
# Keeps instance-level caches (_schema_cache, _lrm_catalogue_cache) alive across requests.
_reader_cache: dict[tuple, EdgeReader] = {}


def _reader(dataset: str, edge_file: str = "edges.parquet") -> EdgeReader:
    key = (dataset, edge_file)
    if key in _reader_cache:
        return _reader_cache[key]
    path = DATA_ROOT / dataset
    if not path.exists():
        raise HTTPException(404, f"Dataset '{dataset}' not found")
    # edge_file may be a plain filename ("edges.parquet") or a relative sub-path
    # inside the dataset ("edges/edge.raw.minimum.parquet"). Resolve it and confirm
    # it stays within the dataset folder — guards against path traversal ("../..").
    dataset_root = path.resolve()
    edge_path = (path / edge_file).resolve()
    try:
        edge_path.relative_to(dataset_root)
    except ValueError:
        raise HTTPException(400, f"Invalid edge file path '{edge_file}'")
    if not edge_path.exists():
        raise HTTPException(404, f"Edge file '{edge_file}' not found in dataset '{dataset}'")
    # Resolve pixel_size from the spatial reader so coordinate conversion is
    # correct for any platform (Xenium, MERSCOPE, CosMx, Visium HD, etc.)
    try:
        from app.readers.reader_factory import ReaderFactory
        pixel_size = ReaderFactory.detect(path).pixel_size
    except Exception:
        pixel_size = 1.0
    reader = EdgeReader(edge_path, pixel_size=pixel_size)
    _reader_cache[key] = reader
    return reader


@router.get("/{dataset}/schema")
def edge_schema(dataset: str, edge_file: str = Query("edges.parquet")):
    """Return column names and dtypes for the edge file."""
    return _reader(dataset, edge_file).schema()


@router.get("/{dataset}/lrm-catalogue")
def lrm_catalogue(dataset: str, edge_file: str = Query("edges.parquet")):
    """Return unique (lrm_id, ligand, receptor) rows, sorted by lrm_id."""
    return _reader(dataset, edge_file).lrm_catalogue()


@router.get("/{dataset}/layer-values")
def layer_values(dataset: str, column: str, edge_file: str = Query("edges.parquet")):
    """Return the distinct values (or min/max for numerics) for a given column."""
    return _reader(dataset, edge_file).column_summary(column)


@router.get("/{dataset}/query")
def query_edges(
    dataset: str,
    edge_file: str = Query("edges.parquet"),
    xmin: float = Query(None),
    ymin: float = Query(None),
    xmax: float = Query(None),
    ymax: float = Query(None),
    filters: Optional[str] = Query(
        None,
        description="JSON-encoded dict of {column: value_or_list} equality filters",
    ),
    min_strength: Optional[float] = Query(None, description="Minimum value of 'strength' column if present"),
    limit: int = Query(200_000),
):
    """
    Return edges filtered by viewport bounding box and optional column filters.
    Edges are included if either endpoint falls within the bbox.
    """
    import json
    parsed_filters = json.loads(filters) if filters else {}
    return _reader(dataset, edge_file).query(
        bbox=(xmin, ymin, xmax, ymax) if xmin is not None else None,
        filters=parsed_filters,
        min_strength=min_strength,
        limit=limit,
    )


@router.get("/{dataset}/files")
def list_edge_files(dataset: str):
    """
    List the edge-source parquet files available for a dataset.

    Discovery is deliberately scoped so it never picks up cells / transcripts /
    boundary parquet files that live at the top level of the dataset:
      1. The legacy top-level ``edges.parquet`` (if present) — listed first so it
         remains the default; keeps single-file datasets working unchanged.
      2. Every ``*.parquet`` inside the dedicated ``edges/`` subfolder — the place
         users drop multiple pre-computed edge sets (see issue #46).

    Each returned entry is an identifier that can be passed straight back as the
    ``edge_file`` query param. ``label`` is a display name (folder + ``.parquet``
    stripped). ``default`` names the identifier the frontend should select first.
    """
    path = DATA_ROOT / dataset
    if not path.exists():
        raise HTTPException(404, f"Dataset '{dataset}' not found")

    files: list[dict] = []
    # Legacy top-level file first (stable default for existing datasets).
    legacy = path / "edges.parquet"
    if legacy.exists():
        files.append({"id": "edges.parquet", "label": "edges"})
    # Additional edge sets in the dedicated subfolder, sorted for stable ordering.
    edges_dir = path / "edges"
    if edges_dir.is_dir():
        for f in sorted(edges_dir.glob("*.parquet")):
            files.append({"id": f"edges/{f.name}", "label": f.stem})

    default = files[0]["id"] if files else "edges.parquet"
    return {"files": files, "default": default}


class MetadataFilterSpec(BaseModel):
    """A metadata restriction (issue #45), for either the cell or the edge table.

    `values` is a categorical allowlist; `min`/`max` an inclusive numeric range.
    Sending a field with neither is not an error — it means the user has picked a
    column but not yet narrowed it, and everything is returned.
    """
    field: Optional[str] = None
    values: Optional[List[str]] = None
    min: Optional[float] = None
    max: Optional[float] = None
    include_missing: bool = False

    def build(self) -> Optional[MetadataFilter]:
        return MetadataFilter.build(
            self.field, self.values, self.min, self.max, self.include_missing
        )


class EdgeGroupedQueryRequest(BaseModel):
    xmin: Optional[float] = None
    ymin: Optional[float] = None
    xmax: Optional[float] = None
    ymax: Optional[float] = None
    min_strength: Optional[float] = None
    density: float = 1.0   # fraction of viewport edges to return (0.01–1.0)

    # Endpoint filters (issue #59). Each is a *cell* metadata predicate resolved
    # against the cells table and applied to one end of the edge, so the two
    # compose as an intersection: sender in A, receiver in B. Either may be
    # omitted, leaving that end unconstrained.
    #
    # These are resolved against the cells table rather than the edge file's own
    # sending_type/receiving_type: those are absent on five of six platforms as
    # the r/ export scripts stand, carry one label where any cell column is
    # wanted, and are frozen at scoring time. See docs/edge_filter_independence.md.
    sending_filter: Optional[MetadataFilterSpec] = None
    receiving_filter: Optional[MetadataFilterSpec] = None

    # Filters on the edge table itself (or edge-metadata/), and-ed together.
    # A list rather than one filter — the composition gap deferred in #45.
    edge_filters: Optional[List[MetadataFilterSpec]] = None

    # Superseded. cell_filter applied one cell predicate to *both* endpoints and
    # tied edge visibility to the cell layer's filter; cell and edge filtering are
    # now independent actions. edge_filter is the pre-list singular form. Both are
    # still accepted so an older frontend against a newer backend keeps working.
    cell_filter: Optional[MetadataFilterSpec] = None
    edge_filter: Optional[MetadataFilterSpec] = None


def _cell_ids_for(dataset: str, spec: Optional[MetadataFilterSpec]) -> Optional[set]:
    """Resolve a cell-metadata filter through the *spatial* reader.

    The edge router has no cells table of its own, so it borrows the platform
    reader — which is also what keeps the cell-id vocabulary identical on both
    sides, the same assumption `edges.parquet` already makes when it stores
    barcodes in `sending_cell`.
    """
    built = spec.build() if spec else None
    if built is None:
        return None
    from app.routers import spatial
    try:
        return spatial._reader(dataset).filter_cell_ids(built)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


class EdgeStructureRequest(BaseModel):
    xmin: Optional[float] = None
    ymin: Optional[float] = None
    xmax: Optional[float] = None
    ymax: Optional[float] = None
    density: float = 1.0
    # No filter fields, deliberately. The tissue graph is the total set of edges;
    # see EdgeReader.query_structure.


@router.post("/{dataset}/query-structure")
def query_edge_structure(dataset: str, body: EdgeStructureRequest,
                         edge_file: str = Query("edges.parquet")):
    """Every edge in the viewport, unfiltered — the tissue-graph layer.

    Separate from /query-grouped so that filtering the edge *data* can never
    subset the structural graph, and so the two can be sampled independently.
    """
    bbox = (body.xmin, body.ymin, body.xmax, body.ymax) \
        if body.xmin is not None else None
    return _reader(dataset, edge_file).query_structure(
        bbox=bbox, density=max(0.001, min(1.0, body.density)))


@router.post("/{dataset}/query-grouped")
def query_edges_grouped(dataset: str, body: EdgeGroupedQueryRequest,
                        edge_file: str = Query("edges.parquet")):
    """
    Return one row per directed edge (pre-aggregated by edge).
    ~500x fewer rows than /query for LRM-rich parquet files.
    Returns structural columns + score_sum (unfiltered total).
    LRM-filter-aware scores are served by /query-scores.
    """
    bbox = (body.xmin, body.ymin, body.xmax, body.ymax) \
        if body.xmin is not None else None
    density = max(0.001, min(1.0, body.density))
    try:
        # Legacy cell_filter means "both endpoints", i.e. the same set on each.
        legacy = _cell_ids_for(dataset, body.cell_filter)
        sending = _cell_ids_for(dataset, body.sending_filter)
        receiving = _cell_ids_for(dataset, body.receiving_filter)
        if legacy is not None:
            sending = legacy if sending is None else sending & legacy
            receiving = legacy if receiving is None else receiving & legacy

        filters = [f.build() for f in (body.edge_filters or [])]
        if body.edge_filter is not None:
            filters.append(body.edge_filter.build())

        return _reader(dataset, edge_file).query_grouped(
            bbox=bbox,
            density=density,
            sending_ids=sending,
            receiving_ids=receiving,
            edge_filters=[f for f in filters if f is not None],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


class EdgeScoreQueryRequest(BaseModel):
    xmin: Optional[float] = None
    ymin: Optional[float] = None
    xmax: Optional[float] = None
    ymax: Optional[float] = None
    # Exactly one of included_lrms / excluded_lrms should be provided.
    # The frontend sends whichever produces the smaller IN-list:
    #   included_lrms — when the visible set is small (fast WHERE lrm IN path)
    #   excluded_lrms — when the excluded set is small (CASE WHEN path)
    included_lrms: Optional[List[Optional[str]]] = None
    excluded_lrms: Optional[List[Optional[str]]] = None


@router.post("/{dataset}/query-scores")
def query_edge_scores(dataset: str, body: EdgeScoreQueryRequest,
                      edge_file: str = Query("edges.parquet")):
    """
    Return per-edge LRM visibility scores: {edge, visible_lrm_count, visible_score_sum}.
    Lightweight complement to /query-grouped — only two aggregate columns, no coordinates.
    Re-runs whenever hiddenLrms changes without requiring a full structural re-fetch.
    """
    bbox = (body.xmin, body.ymin, body.xmax, body.ymax) \
        if body.xmin is not None else None
    # Strip nulls that can arise from null lrm values in the parquet
    included = [x for x in (body.included_lrms or []) if x is not None] \
        if body.included_lrms is not None else None
    excluded = [x for x in (body.excluded_lrms or []) if x is not None] \
        if body.excluded_lrms is not None else None
    return _reader(dataset, edge_file).query_scores(
        bbox=bbox,
        included_lrms=included,
        excluded_lrms=excluded,
    )


class EdgeColorRequest(BaseModel):
    mode: str                        # "lrm_set" | "metadata"
    lrms: Optional[List[str]] = None # for lrm_set: list of "ligand|receptor" strings
    field: Optional[str] = None      # for metadata: column name
    # None = auto-detect; True/False force the interpretation (issue #35).
    categorical: Optional[bool] = None


@router.post("/{dataset}/edge-color-values")
def edge_color_values(dataset: str, body: EdgeColorRequest,
                      edge_file: str = Query("edges.parquet")):
    """
    Return per-directed-edge color values.
    lrm_set: sum score for the supplied LRM list, one value per edge.
    metadata: return first value of `field` per edge (auto-detects cat/continuous
    unless `categorical` overrides it).
    """
    return _reader(dataset, edge_file).edge_color_values(
        body.mode, body.lrms, body.field, body.categorical
    )


@router.get("/{dataset}/edge/{edge_id:path}")
def edge_detail(dataset: str, edge_id: str,
                edge_file: str = Query("edges.parquet")):
    """Return all LRM rows for a single directed edge (for the info panel)."""
    detail = _reader(dataset, edge_file).edge_detail(edge_id)
    if detail is None:
        raise HTTPException(404, f"Edge '{edge_id}' not found")
    return detail
