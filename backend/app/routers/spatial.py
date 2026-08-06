"""
Platform-agnostic spatial data router.

Replaces the Xenium-specific xenium.py router.  All dataset access goes through
ReaderFactory, which auto-detects the platform and returns the appropriate reader.
Supported platforms: Xenium, MERSCOPE, CosMx (see readers/reader_factory.py).
"""
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pathlib import Path
from typing import List, Optional
import io
import os

from app.readers.reader_factory import ReaderFactory

router = APIRouter()

DATA_ROOT = Path(os.getenv("DATA_ROOT", "/data"))

# Module-level cache: dataset name → reader instance.
# Keeps instance-level caches (_cells_full_cache, etc.) alive across requests.
_reader_cache: dict[str, object] = {}


def _reader(dataset: str):
    if dataset in _reader_cache:
        return _reader_cache[dataset]
    path = DATA_ROOT / dataset
    if not path.exists():
        raise HTTPException(404, f"Dataset '{dataset}' not found")
    try:
        reader = ReaderFactory.detect(path)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    _reader_cache[dataset] = reader
    return reader


# ── Dataset discovery ─────────────────────────────────────────────────────────

@router.get("/datasets")
def list_datasets():
    """Return all dataset directory names recognised as a supported spatial platform."""
    if not DATA_ROOT.exists():
        return []
    return sorted(
        d.name for d in DATA_ROOT.iterdir()
        if d.is_dir() and ReaderFactory.is_dataset(d)
    )


@router.get("/platforms")
def list_platforms():
    """Return the supported platform identifiers."""
    return ReaderFactory.supported_platforms()


# ── Per-dataset endpoints ─────────────────────────────────────────────────────

@router.get("/{dataset}/info")
def dataset_info(dataset: str):
    """Experiment metadata, platform identifier, and capability flags."""
    r = _reader(dataset)
    return {**r.info(), "capabilities": r.capabilities()}


_TIFF_EXTS = (".ome.tiff", ".ome.tif", ".tiff", ".tif")


def _strip_tiff_ext(name: str) -> Optional[str]:
    """Return the filename stem if it is a TIFF variant, else None."""
    for ext in _TIFF_EXTS:
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return None


@router.get("/{dataset}/images")
def list_images(dataset: str):
    """Base names of available morphology images (OME-TIFF / TIFF) in a dataset folder.

    Searches the dataset root and one level of subdirectories, so multi-channel
    sets such as Xenium's ``morphology_focus/`` are selectable alongside the
    top-level ``morphology.ome.tif``.

    Returns bare filename stems with no extension and no directory prefix;
    ``pyramid._find_source`` resolves a stem back to a path by searching the
    same two locations in the same order.
    """
    path = DATA_ROOT / dataset
    if not path.exists():
        raise HTTPException(404, f"Dataset '{dataset}' not found")

    names: list[str] = []
    seen: set[str] = set()

    def _add(f: Path) -> None:
        stem = _strip_tiff_ext(f.name)
        if stem and stem not in seen:
            seen.add(stem)
            names.append(stem)

    # Root-level images first, so a top-level stem always wins a name collision
    # with a subdirectory file — matching _find_source's resolution order.
    for f in sorted(path.iterdir()):
        if f.is_file():
            _add(f)

    # One subdirectory level; skip hidden dirs so .dzi_cache is never scanned.
    for sub in sorted(path.iterdir()):
        if sub.is_dir() and not sub.name.startswith("."):
            for f in sorted(sub.iterdir()):
                if f.is_file():
                    _add(f)

    # Morphology variants first, then alphabetical
    names.sort(key=lambda n: (not n.startswith("morphology"), n))
    return names


@router.get("/{dataset}/genes")
def gene_list(dataset: str):
    """All assayed gene names (excluding controls/blanks)."""
    return _reader(dataset).gene_list()


@router.get("/{dataset}/transcripts")
def transcripts(
    dataset: str,
    xmin: float = Query(None),
    ymin: float = Query(None),
    xmax: float = Query(None),
    ymax: float = Query(None),
    genes: list[str] = Query(None),
    fraction: float = Query(1.0),
):
    """Transcript records filtered by bounding box and/or gene list.
    Returns {"transcripts": [...], "total": N} where total is the pre-sample count."""
    return _reader(dataset).transcripts(
        bbox=(xmin, ymin, xmax, ymax) if xmin is not None else None,
        genes=genes,
        fraction=fraction,
    )


@router.get("/{dataset}/cells")
def cells(
    dataset: str,
    xmin: float = Query(None),
    ymin: float = Query(None),
    xmax: float = Query(None),
    ymax: float = Query(None),
):
    """Cell records (centroid + metadata) filtered by bounding box."""
    return _reader(dataset).cells(
        bbox=(xmin, ymin, xmax, ymax) if xmin is not None else None,
    )


@router.get("/{dataset}/cells/schema")
def cells_schema(dataset: str):
    """Column names and dtypes for the cells table."""
    return _reader(dataset).cells_schema()


@router.get("/{dataset}/cells/{cell_id}")
def cell_detail(dataset: str, cell_id: str):
    """Full metadata + expression for a single cell."""
    detail = _reader(dataset).cell_detail(cell_id)
    if detail is None:
        raise HTTPException(404, f"Cell '{cell_id}' not found")
    return detail


@router.get("/{dataset}/expression/{cell_id}")
def cell_expression(dataset: str, cell_id: str):
    """Gene expression vector for a single cell."""
    return _reader(dataset).cell_expression(cell_id)


@router.get("/{dataset}/cell-boundaries")
def cell_boundaries(
    dataset: str,
    xmin: float = Query(None),
    ymin: float = Query(None),
    xmax: float = Query(None),
    ymax: float = Query(None),
    fraction: float = Query(1.0),
):
    """Cell polygon boundaries filtered by bounding box.
    fraction: 0–1 fraction of cells in viewport to return (randomly sampled)."""
    return _reader(dataset).cell_boundaries(
        bbox=(xmin, ymin, xmax, ymax) if xmin is not None else None,
        fraction=max(0.0001, min(1.0, fraction)),
    )


@router.post("/{dataset}/cells/export")
def export_cells(dataset: str, cell_ids: List[str]):
    """Return cell metadata for the given cell IDs as CSV."""
    import pandas as pd
    reader = _reader(dataset)
    all_cells = reader.cells()
    if not all_cells:
        return StreamingResponse(io.StringIO(""), media_type="text/csv")
    df = pd.DataFrame(all_cells)
    subset = df[df["cell_id"].isin(set(cell_ids))]
    buf = io.StringIO()
    subset.to_csv(buf, index=False)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{dataset}_cells.csv"'},
    )


class ColorValuesRequest(BaseModel):
    mode: str
    field: Optional[str] = None
    genes: Optional[List[str]] = None


@router.post("/{dataset}/color-values")
def color_values_post(dataset: str, body: ColorValuesRequest):
    """Per-cell color values for gene_set or metadata coloring."""
    return _reader(dataset).color_values(body.mode, body.field, body.genes)
