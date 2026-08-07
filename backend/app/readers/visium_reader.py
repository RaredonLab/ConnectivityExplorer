"""
10x Genomics Visium (classic v1/v2) reader.

The original Visium slide: a 6.5 × 6.5 mm capture area carrying 4,992 barcoded
spots, each **55 µm across on a 100 µm hexagonal pitch**. One to ten cells per
spot, so a spot is a neighbourhood rather than a cell.

Expected directory layout (Space Ranger `outs/`)
------------------------------------------------
  dataset_dir/
    filtered_feature_bc_matrix.h5
    spatial/
      tissue_positions.csv          barcode, in_tissue, array_row, array_col,
                                    pxl_row_in_fullres, pxl_col_in_fullres
      scalefactors_json.json        spot_diameter_fullres, tissue_hires_scalef,
                                    tissue_lowres_scalef, fiducial_diameter_fullres
      tissue_hires_image.png
      tissue_lowres_image.png

Point the dataset folder at the contents of `outs/`, not at its parent — the same
convention every other reader here follows, and what the morphology-image scan
(root plus one subdirectory) expects.

Three things differ from Visium HD, and all three are traps
-----------------------------------------------------------
1. **There is no `microns_per_pixel`.** Classic Visium scalefactors carry only the
   four keys above; 10x deliberately does not record image pixel size, noting that
   "prior knowledge of the image pixel sizes is not used". So `pixel_size` has to
   be derived, and *which* constant you derive it from matters a great deal.

   It is derived from the **100 µm lattice pitch**, measured off the positions
   table. Not from `spot_diameter_fullres`, which is the obvious-looking choice
   and is wrong by 18%:

       measured on V1_Mouse_Kidney and V1_Adult_Mouse_Brain, independently
         in-row pitch                138.00 px  (both)
         spot_diameter_fullres        89.45 px  (both)
         ratio                         0.6482  (both)

   `spot_diameter_fullres` is Space Ranger's **detected spot footprint**, which at
   that ratio is ~64.8 µm — not the 55 µm nominal capture diameter. 10x's own
   documentation says as much, describing classic Visium spot diameters as
   "approximately 60-70 µm" and cautioning that they are estimates. Deriving from
   55 µm gives 0.615 µm/px where the truth is 0.725, and reconstructs a
   5.4 × 5.7 mm capture area against 10x's specified 6.5 × 6.5 mm. The pitch-based
   derivation reconstructs 6.35 × 6.67 mm.

   The pitch is also the better-conditioned measurement: it averages over
   thousands of spot positions in a rigid array template, where the diameter is
   one estimate of a fuzzy edge.

   `info()` reports `pixel_size_source` — `"lattice_pitch"` normally, or
   `"spot_diameter"` when the positions table is too sparse to measure a pitch and
   the reader falls back. Distances in the measurement tool and the placement of
   `edges.parquet` micron coordinates both inherit whichever was used.

2. **`tissue_positions_list.csv` (Space Ranger < 2.0) has no header row.** Reading
   it with pandas' default header inference eats the first spot and mislabels
   every column. Handled in `spaceranger.py::_read_positions_table`.

3. **`tissue_hires_scalef` is genuinely far from 1.** On classic Visium the hires
   PNG is ~2,000 px on its long edge against a fullres slide image an order of
   magnitude larger, so the factor runs around 0.08. Skip the multiply and every
   spot lands roughly 12× off the image. See the `spaceranger.py` docstring.

Spots are rendered as circles
-----------------------------
`cell_boundaries()` emits a regular polygon inscribing the 55 µm spot. Squares
would be wrong here in a way they are not for HD — an HD bin really is a square,
whereas a Visium spot is round, and drawing a square grid over a hex lattice
misrepresents both the shape and the gaps between spots.
"""
import math
from pathlib import Path
from typing import Optional

import numpy as np

from app.readers.base_reader import _UNSET
from app.readers.spaceranger import SpaceRangerReader

# Slide geometry, fixed by design. 10x: "Each spot is 55 µm in diameter with a
# 100 µm center to center distance between spots."
SPOT_DIAMETER_UM = 55.0
SPOT_PITCH_UM = 100.0

# What `spot_diameter_fullres` actually corresponds to, in µm. Measured as
# 0.6482 × pitch on both V1_Mouse_Kidney and V1_Adult_Mouse_Brain — i.e. Space
# Ranger's detected spot footprint, roughly 18% wider than the 55 µm capture
# spot. Used only when the pitch cannot be measured; see the module docstring.
DETECTED_SPOT_UM = 64.8

# Real Space Ranger writes all 4,992 slide positions. Measuring a pitch from a
# hand-trimmed table with only a handful of spots per row is not trustworthy.
_MIN_PITCH_SAMPLES = 20

# Vertices per spot polygon. Enough that a spot reads as round at the zoom levels
# where individual spots are distinguishable, and cheap: a full capture area is
# 4,992 spots, so ~80K vertices — two orders of magnitude below a Visium HD bin
# grid, which is why this can afford a rounder shape than HD's four corners.
SPOT_VERTICES = 16

_FALLBACK_MICRONS_PER_PIXEL = 1.0


class VisiumReader(SpaceRangerReader):

    _LOG = "visium"

    def __init__(self, dataset_path: Path):
        super().__init__(dataset_path)
        self._pitch_cache = _UNSET

    # ── Space Ranger hooks ────────────────────────────────────────────────────

    def _spatial_dir(self) -> Optional[Path]:
        d = self.path / "spatial"
        return d if d.is_dir() else None

    def _matrix_path(self) -> Optional[Path]:
        for name in ("filtered_feature_bc_matrix.h5", "raw_feature_bc_matrix.h5"):
            p = self.path / name
            if p.exists():
                return p
        return None

    def _lattice_pitch_fullres(self) -> Optional[float]:
        """Centre-to-centre spot spacing in full-resolution pixels, or None.

        Visium numbers columns in steps of 2 within a row, with odd rows offset by
        half a step, so the in-row spacing between consecutive `array_col` values
        two apart *is* the hex nearest-neighbour distance. Measured across every
        row and taken as a median, so a few missing spots cannot move it.
        """
        if self._pitch_cache is not _UNSET:
            return self._pitch_cache  # type: ignore[return-value]
        self._pitch_cache = None
        df = self._read_positions_table()
        if df is None or not {"array_row", "array_col", "pxl_col_in_fullres"} <= set(df.columns):
            return None
        steps: list[float] = []
        for _, g in df.groupby("array_row"):
            g = g.sort_values("array_col")
            dx = np.diff(g["pxl_col_in_fullres"].to_numpy(dtype=float))
            dc = np.diff(g["array_col"].to_numpy(dtype=float))
            steps.extend(dx[dc == 2].tolist())
        steps = [s for s in steps if np.isfinite(s) and s > 0]
        if len(steps) < _MIN_PITCH_SAMPLES:
            return None
        self._pitch_cache = float(np.median(steps))
        return self._pitch_cache  # type: ignore[return-value]

    def pixel_size_source(self) -> str:
        """Which constant `pixel_size` was derived from — see the module docstring."""
        if self._lattice_pitch_fullres() is not None:
            return "lattice_pitch"
        try:
            if float(self._scalefactors()["spot_diameter_fullres"]) > 0:
                return "spot_diameter"
        except (KeyError, TypeError, ValueError):
            pass
        return "fallback"

    def _microns_per_fullres_px(self) -> float:
        """Derived — classic Visium records no pixel size.

        Preference order, and the reason for it, is in the module docstring:
        the 100 µm lattice pitch beats `spot_diameter_fullres`, which measures a
        ~64.8 µm detected footprint rather than the 55 µm capture spot.

        Falls back to 1.0 rather than raising: an unreadable dataset should
        degrade the µm labels, not stop the tissue from rendering.
        """
        pitch = self._lattice_pitch_fullres()
        if pitch:
            return SPOT_PITCH_UM / pitch
        try:
            d = float(self._scalefactors()["spot_diameter_fullres"])
            if d > 0:
                return DETECTED_SPOT_UM / d
        except (KeyError, TypeError, ValueError):
            pass
        return _FALLBACK_MICRONS_PER_PIXEL

    def _unit_diameter_fullres(self) -> float:
        try:
            d = float(self._scalefactors()["spot_diameter_fullres"])
            if d > 0:
                return d
        except (KeyError, TypeError, ValueError):
            pass
        return 1.0

    @staticmethod
    def _unit_offsets(radius: float) -> tuple:
        """A regular SPOT_VERTICES-gon inscribing the spot circle.

        Counter-clockwise, first vertex not repeated — deck.gl closes the ring.
        """
        step = 2.0 * math.pi / SPOT_VERTICES
        return tuple(
            (radius * math.cos(i * step), radius * math.sin(i * step))
            for i in range(SPOT_VERTICES)
        )

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def platform(self) -> str:
        return "visium"

    def info(self) -> dict:
        sf = self._scalefactors()
        pos = self._positions()
        pitch = self._lattice_pitch_fullres()
        return {
            "platform": self.platform,
            "n_spots": int(len(pos)),
            # The slide constant, for reference. Note this is NOT what
            # spot_diameter_fullres measures — see detected_spot_um below.
            "spot_capture_diameter_um": SPOT_DIAMETER_UM,
            "spot_pitch_um": SPOT_PITCH_UM,
            "spot_diameter_fullres": sf.get("spot_diameter_fullres"),
            "spot_diameter_px": self.unit_diameter_px(),
            # What Space Ranger's detected diameter works out to in this dataset.
            # ~65 µm on real data, i.e. the printed footprint, not the capture spot.
            "detected_spot_um": (None if not sf.get("spot_diameter_fullres")
                                 else float(sf["spot_diameter_fullres"])
                                 * self._microns_per_fullres_px()),
            "lattice_pitch_fullres": pitch,
            "pixel_size": self.pixel_size,
            # Always inferred, never recorded by the instrument; this says from
            # what. See the module docstring.
            "pixel_size_derived": True,
            "pixel_size_source": self.pixel_size_source(),
            "tissue_hires_scalef": self.hires_scalef,
            "tissue_lowres_scalef": sf.get("tissue_lowres_scalef"),
        }

    def capabilities(self) -> dict:
        return {
            "has_morphology": self._morphology_exists(),
            # Spot-level UMI counts only; there are no molecule coordinates.
            "has_transcripts": False,
            "has_boundaries": self._spatial_dir() is not None,
            "unit_label": "spot",
        }

    # ── Detection helper ──────────────────────────────────────────────────────

    @staticmethod
    def looks_like_visium(path: Path) -> bool:
        """True for a classic Visium `outs/` folder.

        Requires both the scalefactors and a positions table: `spatial/` alone is
        too weak, since Visium HD also has a top-level `spatial/` folder — though
        that one holds images only, which is what keeps the two from colliding.
        Registered after Visium HD regardless, so the more specific sentinel wins.
        """
        spatial = path / "spatial"
        if not (spatial / "scalefactors_json.json").exists():
            return False
        return any((spatial / n).exists() for n in (
            "tissue_positions.csv",
            "tissue_positions_list.csv",
            "tissue_positions.parquet",
        ))
