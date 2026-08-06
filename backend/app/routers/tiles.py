"""
Tile serving router.
Serves DZI tile pyramids generated from OME-TIFF morphology images.
Step 2 (tile pipeline) will flesh out the pyramid generation; this scaffold
exposes the endpoints that the frontend OpenSeadragon viewer expects.
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from pathlib import Path
import os

from app.tiling.pyramid import (
    BLANK_IMAGE_NAME, ensure_blank_pyramid, ensure_pyramid,
    get_dzi_descriptor, get_tile_path,
)

router = APIRouter()

DATA_ROOT = Path(os.getenv("DATA_ROOT", "/data"))

# Padding around the data extent for the placeholder canvas, so units sitting
# right at the edge are not flush against the border.
_BLANK_MARGIN_FRAC = 0.02


def _ensure_blank(dataset: str, dataset_path: Path) -> dict:
    """Build the placeholder pyramid, sized from the dataset's own data extent."""
    from app.readers.reader_factory import ReaderFactory
    try:
        extent = ReaderFactory.detect(dataset_path).data_extent()
    except Exception as exc:
        return {"status": "error", "message": f"cannot measure data extent: {exc}"}
    if extent is None:
        return {"status": "error",
                "message": f"dataset '{dataset}' has no morphology image and no "
                           f"measurable data extent to size a canvas from"}
    _, _, xmax, ymax = extent
    # The canvas spans the origin to the far corner: OSD images start at (0, 0),
    # so anchoring there keeps deck.gl's pixel coordinates and the tile grid in
    # the same frame. Units at negative coordinates still draw — they simply sit
    # outside the image bounds, which OSD allows panning to.
    w = int(xmax * (1 + _BLANK_MARGIN_FRAC)) + 1
    h = int(ymax * (1 + _BLANK_MARGIN_FRAC)) + 1
    return ensure_blank_pyramid(dataset_path, w, h)


@router.get("/{dataset}/dzi/{image_name}.dzi")
def dzi_descriptor(dataset: str, image_name: str):
    """
    Return the DZI XML descriptor for a morphology image.
    Builds the tile pyramid on first access if not already cached.
    """
    dataset_path = DATA_ROOT / dataset
    if not dataset_path.exists():
        raise HTTPException(404, f"Dataset '{dataset}' not found")
    if image_name == BLANK_IMAGE_NAME:
        result = _ensure_blank(dataset, dataset_path)
    else:
        # Auto-build pyramid on first request (idempotent — no-op if already built)
        result = ensure_pyramid(dataset_path, image_name)
    if result.get("status") == "error":
        raise HTTPException(404, result.get("message", "Could not build pyramid"))
    try:
        d = get_dzi_descriptor(dataset_path, image_name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Image xmlns="{d["xmlns"]}" Format="{d["Format"]}" '
        f'Overlap="{d["Overlap"]}" TileSize="{d["TileSize"]}">'
        f'<Size Width="{d["Size"]["Width"]}" Height="{d["Size"]["Height"]}"/>'
        f'</Image>'
    )
    return Response(content=xml, media_type="application/xml")


@router.get("/{dataset}/dzi/{image_name}_files/{level}/{col}_{row}.{fmt}")
def tile(dataset: str, image_name: str, level: int, col: int, row: int, fmt: str):
    """Return a single DZI tile."""
    dataset_path = DATA_ROOT / dataset
    tile_path = get_tile_path(dataset_path, image_name, level, col, row, fmt)
    if tile_path is None or not tile_path.exists():
        raise HTTPException(404, "Tile not found")
    return FileResponse(tile_path, media_type=f"image/{fmt}")


@router.post("/{dataset}/build-pyramid/{image_name}")
def build_pyramid(dataset: str, image_name: str):
    """Trigger DZI pyramid generation for a given OME-TIFF. Idempotent."""
    dataset_path = DATA_ROOT / dataset
    if not dataset_path.exists():
        raise HTTPException(404, f"Dataset '{dataset}' not found")
    result = ensure_pyramid(dataset_path, image_name)
    return result
