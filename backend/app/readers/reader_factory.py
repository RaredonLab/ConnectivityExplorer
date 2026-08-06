"""
Platform auto-detection and reader instantiation.

Detection is based on the presence of platform-specific sentinel files in the
dataset directory.  Priority order matters when files from multiple platforms
could theoretically co-exist in one folder (unlikely in practice).

Supported platforms (detection order):
  Xenium (10x Genomics)    — experiment.xenium
  Visium HD (10x Genomics) — square_???um/ subdirectory
  MERSCOPE (Vizgen)        — cell_by_gene.csv or cell_metadata.csv
  CosMx (Nanostring)       — *_tx_file.csv
  seqFISH (Spatial Genomics) — *_CellCoordinates*.csv  (glob; registered last)

To add a new platform: define a detector function, a factory function, and
call _register(detector, factory) below.
"""
from pathlib import Path

from app.readers.base_reader import SpatialDatasetReader

# Sentinel descriptions used in the "unrecognised platform" error message.
_SENTINEL_DESCRIPTIONS: list[tuple[str, str]] = []

# Ordered list of (detector_fn, reader_factory_fn) pairs.
_DETECTORS: list[tuple] = []


def _register(detect_fn, reader_fn, sentinel_desc: str):
    _DETECTORS.append((detect_fn, reader_fn))
    _SENTINEL_DESCRIPTIONS.append((reader_fn.__name__, sentinel_desc))


# ── Detectors ─────────────────────────────────────────────────────────────────

def _is_xenium(path: Path) -> bool:
    return (path / "experiment.xenium").exists()


def _is_visium_hd(path: Path) -> bool:
    # Space Ranger nests the bin directories under binned_outputs/. The bare
    # top-level glob is kept as a fallback for hand-assembled folders, but real
    # Space Ranger output only ever matches the first form — globbing the root
    # alone silently failed to detect any genuine Visium HD dataset.
    return (any(path.glob("binned_outputs/square_*um"))
            or any(path.glob("square_???um")))


def _is_merscope(path: Path) -> bool:
    return (
        (path / "cell_by_gene.csv").exists() or
        (path / "cell_metadata.csv").exists()
    )


def _is_cosmx(path: Path) -> bool:
    # CosMx flat files are normally prefixed with the experiment name, but some
    # public exports drop the prefix entirely ("metadata_file.csv"), which the
    # `*_tx_file.csv` glob cannot match since it requires a literal underscore.
    # Several sentinels are checked because exports vary in which files ship:
    # the 2021 NSCLC release has no polygons, and some sets have no tx file.
    for pat in ("*_tx_file.csv", "tx_file.csv",
                "*_metadata_file.csv", "metadata_file.csv",
                "*-polygons.csv", "polygons.csv",
                "*_exprMat_file.csv", "exprMat_file.csv"):
        if any(path.glob(pat)):
            return True
    return False


def _is_seqfish(path: Path) -> bool:
    # seqFISH ships no manifest or version file, so detection has to be a glob over
    # ROI-prefixed filenames. Registered last for that reason — a glob is weaker
    # evidence than an exact sentinel and must not shadow the platforms above.
    from app.readers.seqfish_reader import SeqfishReader
    return bool(SeqfishReader.find_cell_coordinates(path))


# ── Factories ─────────────────────────────────────────────────────────────────

def _make_xenium(path: Path) -> SpatialDatasetReader:
    from app.readers.xenium_reader import XeniumReader
    return XeniumReader(path)


def _make_visium_hd(path: Path) -> SpatialDatasetReader:
    from app.readers.visium_hd_reader import VisiumHDReader
    return VisiumHDReader(path)


def _make_merscope(path: Path) -> SpatialDatasetReader:
    from app.readers.merscope_reader import MerscopeReader
    return MerscopeReader(path)


def _make_cosmx(path: Path) -> SpatialDatasetReader:
    from app.readers.cosmx_reader import CosMxReader
    return CosMxReader(path)


def _make_seqfish(path: Path) -> SpatialDatasetReader:
    from app.readers.seqfish_reader import SeqfishReader
    return SeqfishReader(path)


_register(_is_xenium,    _make_xenium,    "experiment.xenium (Xenium / 10x)")
_register(_is_visium_hd, _make_visium_hd, "square_???um/ directory (Visium HD / 10x)")
_register(_is_merscope,  _make_merscope,  "cell_by_gene.csv or cell_metadata.csv (MERSCOPE / Vizgen)")
_register(_is_cosmx,     _make_cosmx,     "*_tx_file.csv (CosMx / Nanostring)")
_register(_is_seqfish,   _make_seqfish,   "*_CellCoordinates*.csv (seqFISH / Spatial Genomics)")


class ReaderFactory:

    @staticmethod
    def detect(path: Path) -> SpatialDatasetReader:
        """Instantiate the appropriate reader for the given dataset directory.
        Raises ValueError if no platform is recognised."""
        for detect, make in _DETECTORS:
            if detect(path):
                return make(path)
        sentinels = "; ".join(desc for _, desc in _SENTINEL_DESCRIPTIONS)
        raise ValueError(
            f"Cannot detect spatial platform for '{path.name}'. "
            f"Expected one of: {sentinels}."
        )

    @staticmethod
    def is_dataset(path: Path) -> bool:
        """Return True if the directory looks like a supported spatial dataset."""
        return any(detect(path) for detect, _ in _DETECTORS)

    @staticmethod
    def supported_platforms() -> list[str]:
        """Names of all registered platforms, in detection-priority order."""
        return ["xenium", "visium_hd", "merscope", "cosmx", "seqfish"]
