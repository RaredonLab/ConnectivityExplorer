"""
10x Genomics Visium HD reader.

Visium HD tiles the capture area with square bins at 2 µm, 8 µm and 16 µm. There
are no per-molecule transcript coordinates — only bin-level UMI counts.

Expected directory layout (Space Ranger `outs/`)
------------------------------------------------
  dataset_dir/
    binned_outputs/
      square_002um/  square_008um/  square_016um/
        filtered_feature_bc_matrix.h5
        spatial/
          tissue_positions.parquet     barcode, in_tissue, array_row, array_col,
                                       pxl_row_in_fullres, pxl_col_in_fullres
          scalefactors_json.json       microns_per_pixel, spot_diameter_fullres,
                                       bin_size_um, tissue_hires_scalef, …
          tissue_hires_image.png       morphology (PNG, not TIFF)
    spatial/                           the same images, duplicated
    segmented_outputs/                 Space Ranger 4.x: real cell polygons

Note the bin directories are under ``binned_outputs/`` and that
``tissue_positions.parquet`` / ``scalefactors_json.json`` are **per bin**. The
top-level ``spatial/`` folder holds images only.

Bins are rendered as squares
----------------------------
A bin is literally a square of side ``spot_diameter_fullres`` pixels, so
``cell_boundaries()`` emits four vertices per bin rather than reporting
``has_boundaries: False``. That is an honest representation of the data, and it
means fill, outline, colour-by, picking and region selection all work through the
existing layers with no frontend change.

Everything not specific to bins — scalefactors, positions, the feature matrix,
the coordinate scaling, colour values — lives in ``spaceranger.py``, shared with
the classic Visium reader. **Read that module's docstring before touching
coordinates here**; in particular ``tissue_hires_scalef`` is applied there, and
the bundled fixture has it set to 1.0 so a regression would be invisible locally.

Not yet used: ``segmented_outputs/cell_segmentations.geojson``. Space Ranger 4.x
emits real per-cell polygons, which would turn this from a bin viewer into a
single-cell one. The seqFISH reader already has GeoJSON ring-parsing to lift.
"""
from pathlib import Path
from typing import Optional

from app.readers.base_reader import _UNSET
from app.readers.spaceranger import SpaceRangerReader

# Space Ranger's own analysis default, and spatialdata-io's DEFAULT_BIN.
# Preferred when present; otherwise the coarsest bin available, since coarser
# means fewer bins to render.
_PREFERRED_BINS = ("square_008um", "square_016um", "square_002um")

_FALLBACK_MICRONS_PER_PIXEL = 1.0


class VisiumHDReader(SpaceRangerReader):

    _LOG = "visium_hd"

    def __init__(self, dataset_path: Path):
        super().__init__(dataset_path)
        self._bin_dir_cache = _UNSET

    # ── Layout ────────────────────────────────────────────────────────────────

    def _bin_root(self) -> Path:
        """Directory holding the square_*um folders."""
        nested = self.path / "binned_outputs"
        return nested if nested.is_dir() else self.path

    def available_bins(self) -> list[str]:
        return sorted(d.name for d in self._bin_root().glob("square_*um") if d.is_dir())

    def _bin_dir(self) -> Optional[Path]:
        """The bin directory this reader serves."""
        if self._bin_dir_cache is not _UNSET:
            return self._bin_dir_cache  # type: ignore[return-value]
        root = self._bin_root()
        available = self.available_bins()
        chosen = None
        for name in _PREFERRED_BINS:
            if name in available:
                chosen = root / name
                break
        if chosen is None and available:
            # Unknown bin size — take the coarsest, i.e. the largest µm number.
            chosen = root / sorted(available)[-1]
        self._bin_dir_cache = chosen
        return chosen

    # ── Space Ranger hooks ────────────────────────────────────────────────────

    def _spatial_dir(self) -> Optional[Path]:
        bin_dir = self._bin_dir()
        return None if bin_dir is None else bin_dir / "spatial"

    def _matrix_path(self) -> Optional[Path]:
        bin_dir = self._bin_dir()
        return None if bin_dir is None else bin_dir / "filtered_feature_bc_matrix.h5"

    def _microns_per_fullres_px(self) -> float:
        """Visium HD states this outright, unlike classic Visium."""
        try:
            v = float(self._scalefactors().get("microns_per_pixel"))
            return v if v > 0 else _FALLBACK_MICRONS_PER_PIXEL
        except (TypeError, ValueError):
            return _FALLBACK_MICRONS_PER_PIXEL

    def _unit_diameter_fullres(self) -> float:
        """Bin square side length, in full-resolution pixels."""
        sf = self._scalefactors()
        try:
            v = float(sf["spot_diameter_fullres"])
            if v > 0:
                return v
        except (KeyError, TypeError, ValueError):
            pass
        # Derive from the bin size in µm if the diameter is missing.
        try:
            return float(sf["bin_size_um"]) / self._microns_per_fullres_px()
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return 1.0

    @staticmethod
    def _unit_offsets(radius: float) -> tuple:
        """A bin is a square. Corners counter-clockwise, ring left open."""
        return ((-radius, -radius), (radius, -radius),
                (radius, radius), (-radius, radius))

    # ── Experiment metadata ───────────────────────────────────────────────────

    def _bin_side_px(self) -> float:
        """Bin side length in image pixels. Kept as the platform-specific name."""
        return self.unit_diameter_px()

    @property
    def platform(self) -> str:
        return "visium_hd"

    def info(self) -> dict:
        bin_dir = self._bin_dir()
        sf = self._scalefactors()
        return {
            "platform": self.platform,
            "bin": bin_dir.name if bin_dir else None,
            "available_bins": self.available_bins(),
            "bin_size_um": sf.get("bin_size_um"),
            "pixel_size": self.pixel_size,
            "tissue_hires_scalef": self.hires_scalef,
            "bin_side_px": self._bin_side_px(),
        }

    def capabilities(self) -> dict:
        return {
            "has_morphology": self._morphology_exists(),
            # Bin-level UMI counts only; there are no molecule coordinates.
            "has_transcripts": False,
            # Bins are squares, and we emit them as polygons.
            "has_boundaries": self._bin_dir() is not None,
            "unit_label": "bin",
        }
