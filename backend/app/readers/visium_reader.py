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
   "prior knowledge of the image pixel sizes is not used". So `pixel_size` is
   *derived* from the one physical constant the slide guarantees — a spot is
   55 µm — giving `55 / spot_diameter_fullres` µm per fullres pixel.

   10x cautions that spot diameters are estimates and recommends a calibrated
   microscope value instead. That caveat is inherited here: distances in the
   measurement tool, and the placement of `edges.parquet` micron coordinates, are
   only as good as that estimate. It is reported as `pixel_size_derived: true` in
   `info()` so the uncertainty is visible rather than implied.

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

from app.readers.spaceranger import SpaceRangerReader

# The physical spot diameter, fixed by the slide design. 10x: "Each spot is
# 55 µm in diameter with a 100 µm center to center distance between spots."
SPOT_DIAMETER_UM = 55.0

# Vertices per spot polygon. Enough that a spot reads as round at the zoom levels
# where individual spots are distinguishable, and cheap: a full capture area is
# 4,992 spots, so ~80K vertices — two orders of magnitude below a Visium HD bin
# grid, which is why this can afford a rounder shape than HD's four corners.
SPOT_VERTICES = 16

_FALLBACK_MICRONS_PER_PIXEL = 1.0


class VisiumReader(SpaceRangerReader):

    _LOG = "visium"

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

    def _microns_per_fullres_px(self) -> float:
        """Derived from the 55 µm spot spec — classic Visium records no pixel size.

        Falls back to 1.0 rather than raising: an unusable `spot_diameter_fullres`
        should degrade the µm labels, not stop the dataset from rendering.
        """
        try:
            d = float(self._scalefactors()["spot_diameter_fullres"])
            if d > 0:
                return SPOT_DIAMETER_UM / d
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
        return {
            "platform": self.platform,
            "n_spots": int(len(pos)),
            "spot_diameter_um": SPOT_DIAMETER_UM,
            "spot_diameter_fullres": sf.get("spot_diameter_fullres"),
            "spot_diameter_px": self.unit_diameter_px(),
            "pixel_size": self.pixel_size,
            # Flagged because it is inferred from the spot size rather than
            # recorded by the instrument — see the module docstring.
            "pixel_size_derived": True,
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
