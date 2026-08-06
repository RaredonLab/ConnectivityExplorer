"""
OME-TIFF → DZI tile pyramid.

Two backends:
  1. pyvips  — primary; uses libvips streaming so even a 100K×40K Z-stack
               never loads the full image into RAM.
  2. tifffile + Pillow  — fallback when libvips is not available (local dev).

DZI level numbering: level 0 = 1×1, level max = full resolution.
OME level numbering: level 0 = full resolution, level n = most downsampled.
"""
import math
import os
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

TILE_SIZE = 256
OVERLAP = 1
TILE_FORMAT = "jpeg"
JPEG_QUALITY = 85
DZI_CACHE_SUBDIR = ".dzi_cache"

# Extensions _find_source resolves, kept in step with spatial.list_images. PNG is
# included because Visium HD's morphology is tissue_hires_image.png, not a TIFF.
_SOURCE_EXTS = (".ome.tif", ".ome.tiff", ".tif", ".tiff", ".png")

_CACHE_DIR = os.getenv("CACHE_DIR")  # None → write alongside the data

# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def ensure_pyramid(dataset_path: Path, image_name: str) -> dict:
    """Build DZI pyramid from the OME-TIFF if not already cached. Idempotent."""
    src = _find_source(dataset_path, image_name)
    if src is None:
        return {"status": "error", "message": f"Source image '{image_name}' not found"}

    out_dir = _pyramid_root(dataset_path, image_name)
    dzi_file = out_dir / f"{image_name}.dzi"
    if dzi_file.exists():
        return {"status": "ready", "path": str(dzi_file)}

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        _build_dzi(src, out_dir, image_name)
        return {"status": "ready", "path": str(dzi_file)}
    except Exception as exc:
        shutil.rmtree(out_dir, ignore_errors=True)
        return {"status": "error", "message": str(exc)}


def get_dzi_descriptor(dataset_path: Path, image_name: str) -> dict:
    """Return the DZI descriptor as a dict."""
    dzi_file = _pyramid_root(dataset_path, image_name) / f"{image_name}.dzi"
    if not dzi_file.exists():
        raise FileNotFoundError(
            f"DZI not built for '{image_name}'. "
            f"POST /tiles/{{dataset}}/build-pyramid/{image_name} first."
        )
    tree = ET.parse(dzi_file)
    root = tree.getroot()
    ns = "http://schemas.microsoft.com/deepzoom/2008"
    size_el = root.find(f"{{{ns}}}Size")
    return {
        "xmlns": ns,
        "Format": root.attrib.get("Format", TILE_FORMAT),
        "Overlap": int(root.attrib.get("Overlap", OVERLAP)),
        "TileSize": int(root.attrib.get("TileSize", TILE_SIZE)),
        "Size": {
            "Width": int(size_el.attrib["Width"]),
            "Height": int(size_el.attrib["Height"]),
        },
    }


def get_tile_path(
    dataset_path: Path, image_name: str, level: int, col: int, row: int, fmt: str
) -> Optional[Path]:
    tile = (
        _pyramid_root(dataset_path, image_name)
        / f"{image_name}_files"
        / str(level)
        / f"{col}_{row}.{fmt}"
    )
    return tile if tile.exists() else None


# ──────────────────────────────────────────────────────────────────────────────
# Build dispatcher
# ──────────────────────────────────────────────────────────────────────────────

def _build_dzi(src: Path, out_dir: Path, image_name: str) -> None:
    """Try pyvips first; fall back to tifffile+Pillow on any failure."""
    pyvips_started = False
    try:
        import pyvips
        pyvips.version(0)   # confirms libvips C library loaded
        pyvips_started = True
        _build_dzi_pyvips(src, out_dir, image_name)
        return
    except Exception:
        # pyvips not installed, libvips missing, or can't process this file.
        # Clean up any partial output before falling back.
        if pyvips_started:
            for item in list(out_dir.iterdir()):
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
    # The tifffile fallback can only open TIFFs. Visium HD's morphology is a PNG,
    # so ordinary raster formats get a Pillow path instead — otherwise a machine
    # without libvips could list the image but never build its pyramid.
    if src.suffix.lower() in (".png", ".jpg", ".jpeg"):
        _build_dzi_pillow(src, out_dir, image_name)
        return
    _build_dzi_tifffile(src, out_dir, image_name)


def _build_dzi_pillow(src: Path, out_dir: Path, image_name: str) -> None:
    """Build a DZI from an ordinary raster image (PNG/JPEG) with Pillow alone."""
    # Visium HD hires images are ~6000x5400, comfortably loadable; but Pillow's
    # decompression-bomb guard trips well below that, so raise it deliberately.
    Image.MAX_IMAGE_PIXELS = None
    with Image.open(src) as im:
        full = im.convert("L")
    _write_pyramid_from_image(full, out_dir, image_name)


# ──────────────────────────────────────────────────────────────────────────────
# Backend 1 — pyvips (streaming, handles very large images)
# ──────────────────────────────────────────────────────────────────────────────

def _build_dzi_pyvips(src: Path, out_dir: Path, image_name: str) -> None:
    """
    Build DZI using pyvips streaming pipeline.

    Handles ZYX OME-TIFFs by building a lazy max-intensity-projection pipeline.
    pyvips never loads the full image into RAM — it processes tiles on demand.
    """
    import pyvips

    # Probe the file to find the number of pages (Z planes)
    probe = pyvips.Image.new_from_file(str(src), access="sequential")
    n_pages = probe.get("n-pages") if probe.get_typeof("n-pages") else 1

    if n_pages > 1:
        # Build a lazy MIP pipeline: per-pixel max across all Z planes.
        # pyvips evaluates this tile-by-tile, never holding the full stack in RAM.
        pages = [
            pyvips.Image.new_from_file(str(src), page=i, access="sequential")
            for i in range(n_pages)
        ]
        img = pages[0]
        for p in pages[1:]:
            img = (p > img).ifthenelse(p, img)
    else:
        img = pyvips.Image.new_from_file(str(src), access="sequential")

    # Squeeze multi-band to single band if needed (e.g. RGB → luminance)
    if img.bands > 1:
        img = img.colourspace(pyvips.enums.Interpretation.B_W).extract_band(0)

    # Compute normalization range from a small thumbnail (fast — uses OME sub-levels)
    thumb = pyvips.Image.thumbnail(str(src), 512)
    if thumb.bands > 1:
        thumb = thumb.colourspace(pyvips.enums.Interpretation.B_W).extract_band(0)
    buf = thumb.write_to_memory()
    dtype = np.uint8 if thumb.format == pyvips.BandFormat.UCHAR else np.uint16
    arr = np.frombuffer(buf, dtype=dtype)
    lo = float(np.percentile(arr, 2))
    hi = float(np.percentile(arr, 98))
    del arr, buf, thumb

    # Apply linear normalization to uint8
    if img.format != pyvips.BandFormat.UCHAR:
        scale = 255.0 / max(hi - lo, 1.0)
        img = img.linear([scale], [-lo * scale])
        img = img.cast(pyvips.BandFormat.UCHAR)

    # Stream-write DZI tiles — pyvips processes one tile at a time.
    # depth="onepixel" (default) generates all pyramid levels down to 1×1.
    out_prefix = str(out_dir / image_name)
    img.dzsave(
        out_prefix,
        tile_size=TILE_SIZE,
        overlap=OVERLAP,
        suffix=f".{TILE_FORMAT}",
        Q=JPEG_QUALITY,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Backend 2 — tifffile + Pillow (pure Python fallback)
# ──────────────────────────────────────────────────────────────────────────────

def _build_dzi_tifffile(src: Path, out_dir: Path, image_name: str) -> None:
    """
    Build DZI from OME-TIFF using tifffile + Pillow.

    Reads one OME pyramid level at a time (rather than all at once) to keep
    peak memory usage bounded.  For images wider than MAX_TIFFFILE_DIM the
    smallest OME level that fits within the limit is used as the top DZI level,
    which is sufficient for laboratory-scale Xenium data.
    """
    import tifffile

    MAX_TIFFFILE_DIM = 16384  # pixels; skip OME levels larger than this

    with tifffile.TiffFile(src) as tif:
        series = tif.series[0]
        ax = series.axes.upper()
        n_levels = len(series.levels)

        full_shape = series.levels[0].shape
        if ax.startswith(("Z", "C")):
            full_h, full_w = full_shape[-2], full_shape[-1]
        else:
            full_h, full_w = full_shape[0], full_shape[1]

        # Compute normalization from the smallest OME level
        lo, hi = _tiff_norm_stats(series, ax)

        max_dzi_level = math.ceil(math.log2(max(full_w, full_h)))
        tiles_root = out_dir / f"{image_name}_files"

        for dzi_level in range(max_dzi_level + 1):
            level_w = max(1, math.ceil(full_w / 2 ** (max_dzi_level - dzi_level)))
            level_h = max(1, math.ceil(full_h / 2 ** (max_dzi_level - dzi_level)))

            # Pick smallest OME level >= target, but skip if still too large
            ome_idx = _pick_ome_level_idx(series, ax, full_w, full_h, level_w, level_h)
            ome_lvl = series.levels[ome_idx]
            ome_shape = ome_lvl.shape
            ome_w = ome_shape[-1]
            ome_h = ome_shape[-2]

            if max(ome_w, ome_h) > MAX_TIFFFILE_DIM:
                # This OME level is too large to load safely; skip — lower DZI
                # levels will be generated from smaller OME levels.
                continue

            arr = _read_ome_level_arr(ome_lvl, ax)
            arr_u8 = _to_uint8(arr, lo, hi)
            del arr

            pil_img = Image.fromarray(arr_u8, mode="L")
            del arr_u8

            if pil_img.width != level_w or pil_img.height != level_h:
                pil_img = pil_img.resize((level_w, level_h), Image.LANCZOS)

            level_dir = tiles_root / str(dzi_level)
            level_dir.mkdir(parents=True, exist_ok=True)

            n_cols = math.ceil(level_w / TILE_SIZE)
            n_rows = math.ceil(level_h / TILE_SIZE)

            for row in range(n_rows):
                for col in range(n_cols):
                    x0 = col * TILE_SIZE - (OVERLAP if col > 0 else 0)
                    y0 = row * TILE_SIZE - (OVERLAP if row > 0 else 0)
                    x1 = min(x0 + TILE_SIZE + OVERLAP * 2, level_w)
                    y1 = min(y0 + TILE_SIZE + OVERLAP * 2, level_h)
                    x0 = max(x0, 0)
                    y0 = max(y0, 0)
                    tile = pil_img.crop((x0, y0, x1, y1))
                    tile.save(level_dir / f"{col}_{row}.{TILE_FORMAT}", quality=JPEG_QUALITY)

            del pil_img

    _write_dzi_xml(out_dir, image_name, full_w, full_h)


def _tiff_norm_stats(series, ax: str) -> tuple[float, float]:
    """Compute 2nd–98th percentile range from the smallest OME level."""
    arr = _read_ome_level_arr(series.levels[-1], ax)
    if arr.dtype == np.uint8:
        return 0.0, 255.0
    lo = float(np.percentile(arr, 2))
    hi = float(np.percentile(arr, 98))
    return lo, max(hi, lo + 1.0)


def _read_ome_level_arr(lvl, ax: str) -> np.ndarray:
    """Read one OME level and return a 2-D (Y, X) array with MIP if ZYX."""
    arr = lvl.asarray()
    if ax.startswith("Z"):
        arr = arr.max(axis=0)
    elif ax.startswith("C"):
        arr = arr[0]
    elif len(arr.shape) == 3:
        arr = arr[0]
    return arr


def _pick_ome_level_idx(series, ax: str, full_w: int, full_h: int,
                        target_w: int, target_h: int) -> int:
    """Return index of smallest OME level whose dims are >= (target_w, target_h)."""
    n = len(series.levels)
    for i in range(n - 1, -1, -1):
        shape = series.levels[i].shape
        w = shape[-1]
        h = shape[-2]
        if w >= target_w and h >= target_h:
            return i
    return 0


def _to_uint8(arr: np.ndarray, lo: float, hi: float) -> np.ndarray:
    if arr.dtype == np.uint8:
        return arr
    clipped = np.clip(arr.astype(np.float32), lo, hi)
    return ((clipped - lo) / (hi - lo) * 255).astype(np.uint8)


def _write_pyramid_from_image(full: "Image.Image", out_dir: Path, image_name: str) -> None:
    """Write a complete DZI tile pyramid from a single in-memory Pillow image.

    Used by the PNG/JPEG fallback, where the source has no embedded pyramid and
    every level is produced by successive downscaling.
    """
    full_w, full_h = full.size
    max_dzi_level = math.ceil(math.log2(max(full_w, full_h, 1)))
    tiles_root = out_dir / f"{image_name}_files"

    for dzi_level in range(max_dzi_level + 1):
        level_w = max(1, math.ceil(full_w / 2 ** (max_dzi_level - dzi_level)))
        level_h = max(1, math.ceil(full_h / 2 ** (max_dzi_level - dzi_level)))
        lvl_img = (full if (level_w, level_h) == (full_w, full_h)
                   else full.resize((level_w, level_h), Image.LANCZOS))

        level_dir = tiles_root / str(dzi_level)
        level_dir.mkdir(parents=True, exist_ok=True)
        for row in range(math.ceil(level_h / TILE_SIZE)):
            for col in range(math.ceil(level_w / TILE_SIZE)):
                x0 = max(col * TILE_SIZE - (OVERLAP if col > 0 else 0), 0)
                y0 = max(row * TILE_SIZE - (OVERLAP if row > 0 else 0), 0)
                x1 = min(x0 + TILE_SIZE + OVERLAP * 2, level_w)
                y1 = min(y0 + TILE_SIZE + OVERLAP * 2, level_h)
                lvl_img.crop((x0, y0, x1, y1)).save(
                    level_dir / f"{col}_{row}.{TILE_FORMAT}", quality=JPEG_QUALITY)
        if lvl_img is not full:
            del lvl_img

    _write_dzi_xml(out_dir, image_name, full_w, full_h)


def _write_dzi_xml(out_dir: Path, image_name: str, width: int, height: int) -> None:
    dzi_xml = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<Image xmlns="http://schemas.microsoft.com/deepzoom/2008" '
        f'Format="{TILE_FORMAT}" Overlap="{OVERLAP}" TileSize="{TILE_SIZE}">\n'
        f'  <Size Width="{width}" Height="{height}"/>\n'
        f'</Image>\n'
    )
    (out_dir / f"{image_name}.dzi").write_text(dzi_xml)


# ──────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ──────────────────────────────────────────────────────────────────────────────

def _pyramid_root(dataset_path: Path, image_name: str) -> Path:
    if _CACHE_DIR:
        return Path(_CACHE_DIR) / dataset_path.name / image_name
    return dataset_path / DZI_CACHE_SUBDIR / image_name


def _find_source(dataset_path: Path, image_name: str) -> Optional[Path]:
    """Resolve an image stem from /spatial/{dataset}/images back to a file path.

    Searches the dataset root first, then one level of subdirectories, so
    multi-channel sets such as Xenium's ``morphology_focus/`` resolve. The
    root-first order matches ``spatial.list_images`` so a stem that exists in
    both places always resolves to the same file the picker listed.
    """
    for ext in _SOURCE_EXTS:
        candidate = dataset_path / f"{image_name}{ext}"
        if candidate.exists():
            return candidate
    for ext in _SOURCE_EXTS:
        for subdir in sorted(dataset_path.iterdir()):
            if subdir.is_dir() and not subdir.name.startswith("."):
                candidate = subdir / f"{image_name}{ext}"
                if candidate.exists():
                    return candidate
    return None
