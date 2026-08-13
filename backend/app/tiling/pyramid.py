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


# Reserved image name for datasets with no morphology of their own. The viewer
# takes its coordinate space from the tile pyramid, so a dataset without an image
# would render nothing at all — this gives deck.gl a canvas to draw onto.
BLANK_IMAGE_NAME = "__blank__"

# Uniform fill for that canvas: dark enough that cell outlines and edges read
# against it, light enough to distinguish from empty viewer background.
_BLANK_LEVEL = 26


def ensure_blank_pyramid(dataset_path: Path, width: int, height: int) -> dict:
    """Create a placeholder pyramid of the given size, if not already cached.

    No tiles are generated. A blank pyramid is uniform, so one 256×256 tile is
    written and `get_tile_path` returns it for every level/column/row. At CosMx
    scale — roughly 47000 × 25000 px — materialising real tiles would mean tens
    of thousands of identical files for no benefit.
    """
    width = max(1, int(width))
    height = max(1, int(height))
    out_dir = _pyramid_root(dataset_path, BLANK_IMAGE_NAME)
    dzi_file = out_dir / f"{BLANK_IMAGE_NAME}.dzi"
    tile = out_dir / "blank_tile.jpeg"

    if dzi_file.exists() and tile.exists():
        try:
            d = get_dzi_descriptor(dataset_path, BLANK_IMAGE_NAME)
            if (d["Size"]["Width"], d["Size"]["Height"]) == (width, height):
                return {"status": "ready", "path": str(dzi_file)}
        except Exception:
            pass  # stale or unreadable → rewrite below

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        Image.new("L", (TILE_SIZE, TILE_SIZE), _BLANK_LEVEL).save(
            tile, quality=JPEG_QUALITY)
        _write_dzi_xml(out_dir, BLANK_IMAGE_NAME, width, height)
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
    if image_name == BLANK_IMAGE_NAME:
        # One uniform tile answers every request; see ensure_blank_pyramid.
        tile = _pyramid_root(dataset_path, BLANK_IMAGE_NAME) / "blank_tile.jpeg"
        return tile if tile.exists() else None
    tile = (
        _pyramid_root(dataset_path, image_name)
        / f"{image_name}_files"
        / str(level)
        / f"{col}_{row}.{fmt}"
    )
    return tile if tile.exists() else None


# Deepest-level base image held for on-demand tile synthesis, keyed by tiles_root.
# One at a time — each is tens of MB — so switching image releases the previous.
_SYNTH_BASE: dict[str, "Image.Image"] = {}


def _synth_base(tiles_root: Path, deepest: int, deep_w: int, deep_h: int):
    """Stitch the deepest built DZI level into a single Pillow image, cached.

    Built from the already-written JPEG tiles (no OME re-decode), so it inherits
    the same normalisation as the rest of the pyramid.
    """
    key = f"{tiles_root}#{deepest}"
    cached = _SYNTH_BASE.get(key)
    if cached is not None:
        return cached
    level_dir = tiles_root / str(deepest)
    if not level_dir.is_dir():
        return None
    base = Image.new("L", (deep_w, deep_h))
    for f in level_dir.glob(f"*.{TILE_FORMAT}"):
        try:
            c, r = (int(v) for v in f.stem.split("_"))
        except ValueError:
            continue
        x0 = max(0, c * TILE_SIZE - (OVERLAP if c > 0 else 0))
        y0 = max(0, r * TILE_SIZE - (OVERLAP if r > 0 else 0))
        with Image.open(f) as t:
            base.paste(t.convert("L"), (x0, y0))
    _SYNTH_BASE.clear()          # bound memory to a single base image
    _SYNTH_BASE[key] = base
    return base


def get_or_synth_tile(
    dataset_path: Path, image_name: str, level: int, col: int, row: int, fmt: str
) -> Optional[Path]:
    """Return a tile path, synthesising it when the requested level is above what
    was built.

    Large JPEG2000 OME-TIFFs are only built down to the deepest level that fits
    MAX_TIFFFILE_DIM; the descriptor still advertises native size (so overlays
    align), so a client will request deeper levels that were never written. Rather
    than 404 (which renders black), upscale the deepest built level for that tile.
    Returns None when there is nothing to synthesise from (genuinely absent tile).
    """
    existing = get_tile_path(dataset_path, image_name, level, col, row, fmt)
    if existing is not None:
        return existing
    if image_name == BLANK_IMAGE_NAME:
        return None

    tiles_root = _pyramid_root(dataset_path, image_name) / f"{image_name}_files"
    if not tiles_root.is_dir():
        return None
    built = sorted(int(d.name) for d in tiles_root.iterdir()
                   if d.is_dir() and d.name.isdigit())
    if not built:
        return None
    deepest = built[-1]
    if level <= deepest:
        return None  # absent within the built range — not an upscale case

    try:
        d = get_dzi_descriptor(dataset_path, image_name)
    except FileNotFoundError:
        return None
    full_w, full_h = d["Size"]["Width"], d["Size"]["Height"]
    max_dzi_level = math.ceil(math.log2(max(full_w, full_h)))
    if level > max_dzi_level:
        return None

    def _dims(lvl: int) -> tuple[int, int]:
        return (max(1, math.ceil(full_w / 2 ** (max_dzi_level - lvl))),
                max(1, math.ceil(full_h / 2 ** (max_dzi_level - lvl))))

    level_w, level_h = _dims(level)
    deep_w, deep_h = _dims(deepest)

    base = _synth_base(tiles_root, deepest, deep_w, deep_h)
    if base is None:
        return None

    # Target tile bounds in this level's pixel space — identical geometry to the
    # builder, so a synthesised tile lines up with real ones at the boundary.
    x0 = col * TILE_SIZE - (OVERLAP if col > 0 else 0)
    y0 = row * TILE_SIZE - (OVERLAP if row > 0 else 0)
    x1 = min(x0 + TILE_SIZE + OVERLAP * 2, level_w)
    y1 = min(y0 + TILE_SIZE + OVERLAP * 2, level_h)
    x0 = max(x0, 0)
    y0 = max(y0, 0)
    if x1 <= x0 or y1 <= y0:
        return None

    # Map to deepest-level coordinates, crop, and upscale to the tile's size.
    sx, sy = deep_w / level_w, deep_h / level_h
    bx0, by0 = int(math.floor(x0 * sx)), int(math.floor(y0 * sy))
    bx1 = min(deep_w, max(bx0 + 1, int(math.ceil(x1 * sx))))
    by1 = min(deep_h, max(by0 + 1, int(math.ceil(y1 * sy))))
    crop = base.crop((bx0, by0, bx1, by1)).resize((x1 - x0, y1 - y0), Image.BILINEAR)

    level_dir = tiles_root / str(level)
    level_dir.mkdir(parents=True, exist_ok=True)
    out_path = level_dir / f"{col}_{row}.{fmt}"
    try:
        crop.save(out_path, quality=JPEG_QUALITY)
    except Exception:
        return None
    return out_path


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
    except Exception as exc:
        # pyvips not installed, libvips missing, or can't process this file.
        # Log the reason: this was silent before, which hid real failures such
        # as JPEG2000-in-TIFF (compression tag 34712) that libvips cannot decode
        # via libtiff even when built with OpenJPEG — the tifffile+imagecodecs
        # fallback is what actually handles those files.
        print(f"[pyramid] pyvips could not build {src.name} "
              f"({'processing error' if pyvips_started else 'unavailable'}): "
              f"{type(exc).__name__}: {exc} — falling back to tifffile/Pillow")
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

# Pixels; OME levels wider/taller than this are not loaded whole (a uint16 level
# at this size is ~0.5 GB, and normalisation transiently needs a few× that). The
# descriptor is always written at NATIVE size so overlay coordinates (which the
# readers emit as µm / pixel_size, i.e. native pixels) stay aligned with the
# tiles. Levels whose OME source exceeds this cap are simply left unbuilt and
# synthesised on demand by upscaling the deepest built level (get_or_synth_tile).
# Raise it (env override) on a box with spare RAM to build sharper top levels.
MAX_TIFFFILE_DIM = int(os.getenv("MAX_TIFFFILE_DIM", "16384"))


def _build_dzi_tifffile(src: Path, out_dir: Path, image_name: str) -> None:
    """
    Build DZI from OME-TIFF using tifffile + Pillow.

    Reads one OME pyramid level at a time (rather than all at once) to keep peak
    memory usage bounded. The descriptor is written at the image's NATIVE size so
    that cell/edge/transcript coordinates — which the readers emit in native pixel
    space (µm / pixel_size) — line up with the tiles. OME levels wider than
    MAX_TIFFFILE_DIM are too large to load whole, so the corresponding top DZI
    levels are left unbuilt and produced on demand by upscaling the deepest built
    level (see get_or_synth_tile). That keeps the image at native size while
    bounding both build time and peak memory.
    """
    import tifffile

    with tifffile.TiffFile(src) as tif:
        series = tif.series[0]
        ax = series.axes.upper()

        native_shape = series.levels[0].shape
        full_w, full_h = native_shape[-1], native_shape[-2]

        # Compute normalization from the smallest OME level
        lo, hi = _tiff_norm_stats(series, ax)

        max_dzi_level = math.ceil(math.log2(max(full_w, full_h)))
        tiles_root = out_dir / f"{image_name}_files"
        deepest_built = -1

        for dzi_level in range(max_dzi_level + 1):
            level_w = max(1, math.ceil(full_w / 2 ** (max_dzi_level - dzi_level)))
            level_h = max(1, math.ceil(full_h / 2 ** (max_dzi_level - dzi_level)))

            ome_idx = _pick_ome_level_idx(series, ax, full_w, full_h, level_w, level_h)
            ome_lvl = series.levels[ome_idx]
            if max(ome_lvl.shape[-1], ome_lvl.shape[-2]) > MAX_TIFFFILE_DIM:
                # Too large to load whole. Leave unbuilt — served on demand by
                # upscaling the deepest built level, which keeps the image at
                # native size so overlay coordinates stay aligned.
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
            deepest_built = dzi_level

    if deepest_built < max_dzi_level:
        print(f"[pyramid] {src.name}: native {full_w}x{full_h}; DZI levels above "
              f"{deepest_built} exceed MAX_TIFFFILE_DIM={MAX_TIFFFILE_DIM} and are "
              f"upscaled on demand. Raise MAX_TIFFFILE_DIM (more RAM) for sharper "
              f"top levels.")

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
