#!/usr/bin/env python3
"""
Generate a tiny synthetic classic Visium (10x, v1/v2) dataset.

Why this exists
---------------
Real Space Ranger output for a classic Visium run is a few hundred MB, and the
public 10x datasets are not redistributable from this repo. Without a committable
fixture the Visium reader has nothing to test against in a fresh checkout.

What it deliberately gets *awkward*, because that is what a reader gets wrong:

  * **``tissue_hires_scalef`` is not 1.0.** This is the whole point. Space Ranger
    reports spot positions in full-resolution pixels but only ships the *hires*
    PNG, so coordinates must be multiplied by that factor to land on the image.
    ``sample_data/visium_hd_tiny`` has the factor set to exactly 1.0, which is
    why a missing multiply went unnoticed there for a whole release. This fixture
    uses 0.08 — the realistic order of magnitude — so the same bug fails loudly.
  * **No ``microns_per_pixel`` key.** Classic Visium scalefactors carry only the
    four keys 10x actually writes, forcing ``pixel_size`` down the derived path
    (``55 µm / spot_diameter_fullres``) rather than the recorded one.
  * **A hexagonal lattice with the odd-row offset**, not a square grid, and with
    off-tissue spots present and flagged ``in_tissue = 0`` so the filter is
    exercised.

Usage
-----
    python make_visium.py --out visium_tiny
    python make_visium.py --out visium_tiny --rows 24 --cols 32 --seed 7
    python make_visium.py --out visium_legacy --legacy-positions   # pre-SR-2.0

Writes <out>/{filtered_feature_bc_matrix.h5, spatial/{tissue_positions.csv,
scalefactors_json.json, tissue_hires_image.png, tissue_lowres_image.png}}.
"""
from __future__ import annotations

import argparse
import json
import string
from pathlib import Path

import numpy as np

# Slide geometry, fixed by 10x: 55 µm spots on a 100 µm centre-to-centre pitch.
SPOT_DIAMETER_UM = 55.0
SPOT_PITCH_UM = 100.0

# Deliberately far from 1.0 — see the module docstring.
HIRES_SCALEF = 0.08
LOWRES_SCALEF = 0.024

# µm per full-resolution pixel. A realistic 20x brightfield scan.
MICRONS_PER_FULLRES_PX = 0.25

# Real mouse gene symbols forming complete ligand-receptor pairs in
# connectomedb2025, so NICHESv2 can actually score this fixture — the same
# reasoning as make_seqfish.py, where an invented panel made the edge pipeline
# impossible to demonstrate.
LR_PANEL = [
    "Tgfb1", "Tgfbr1", "Tgfbr2", "Wnt5a", "Fzd1", "Ror2", "Cxcl12", "Cxcr4",
    "Vegfa", "Kdr", "Flt1", "Pdgfb", "Pdgfrb", "Hgf", "Met", "Il6", "Il6ra",
    "Efnb1", "Ephb2", "Spp1", "Cd44", "Ccl2", "Ccr2", "Bmp4", "Bmpr2",
    "Fgf2", "Fgfr1", "Egf", "Egfr", "Jag1", "Dll4", "Notch1", "Apoe", "Lrp1",
    "Thbs1", "Cd47",
]


def barcode(i: int) -> str:
    """A plausible 16 nt Visium barcode with the standard ``-1`` suffix."""
    letters = "ACGT"
    out = []
    n = i
    for _ in range(16):
        out.append(letters[n % 4])
        n //= 4
    return "".join(out) + "-1"


def build_positions(rows: int, cols: int, rng) -> list[dict]:
    """Hex lattice in fullres pixels, with an off-tissue margin.

    Visium's lattice offsets odd rows by half a pitch, which is why
    ``array_col`` steps by 2 within a row in real output. Reproduced so anything
    that reasons about the grid sees the real shape.
    """
    pitch_px = SPOT_PITCH_UM / MICRONS_PER_FULLRES_PX
    # Row spacing on a hex lattice is pitch * sqrt(3)/2.
    row_px = pitch_px * np.sqrt(3.0) / 2.0
    margin = pitch_px * 1.5

    # An ellipse of "tissue" inside the capture area; everything outside it is
    # captured but flagged in_tissue = 0, as on a real slide.
    cx, cy = (cols - 1) / 2.0, (rows - 1) / 2.0
    rx, ry = cols * 0.36, rows * 0.36

    out = []
    idx = 0
    for r in range(rows):
        for c in range(cols):
            x = margin + (c * pitch_px) + (pitch_px / 2.0 if r % 2 else 0.0)
            y = margin + (r * row_px)
            inside = ((c - cx) / rx) ** 2 + ((r - cy) / ry) ** 2 <= 1.0
            out.append({
                "barcode": barcode(idx),
                "in_tissue": 1 if inside else 0,
                "array_row": r,
                # Real Visium numbers columns in steps of 2, offset on odd rows.
                "array_col": c * 2 + (1 if r % 2 else 0),
                "pxl_row_in_fullres": float(y),
                "pxl_col_in_fullres": float(x),
            })
            idx += 1
    return out


def write_matrix(path: Path, barcodes: list[str], genes: list[str], counts) -> None:
    """Write a 10x-format filtered_feature_bc_matrix.h5 (CSC, genes × spots)."""
    import h5py
    import scipy.sparse as sp

    mat = sp.csc_matrix(counts)
    with h5py.File(path, "w") as f:
        g = f.create_group("matrix")
        g.create_dataset("barcodes", data=np.array(barcodes, dtype="S"))
        g.create_dataset("data", data=mat.data.astype(np.int32))
        g.create_dataset("indices", data=mat.indices.astype(np.int64))
        g.create_dataset("indptr", data=mat.indptr.astype(np.int64))
        g.create_dataset("shape", data=np.array(mat.shape, dtype=np.int32))
        feat = g.create_group("features")
        feat.create_dataset("name", data=np.array(genes, dtype="S"))
        feat.create_dataset("id", data=np.array(
            [f"ENSMUSG{i:011d}" for i in range(len(genes))], dtype="S"))
        feat.create_dataset("feature_type", data=np.array(
            ["Gene Expression"] * len(genes), dtype="S"))
        feat.create_dataset("genome", data=np.array(["mm10"] * len(genes), dtype="S"))


def write_image(path: Path, w: int, h: int, spots, radius: float, rng) -> None:
    """A grey H&E-ish PNG with a faint blob under each in-tissue spot.

    Not decorative: the reader's whole coordinate contract is "these pixels are
    the hires image", so a fixture whose image dimensions do not match the scaled
    coordinates would let a broken scaling pass.
    """
    from PIL import Image

    img = np.full((h, w, 3), 236, dtype=np.uint8)
    img[..., 2] = 240
    yy, xx = np.mgrid[0:h, 0:w]
    for s in spots:
        cx, cy = s["hx"], s["hy"]
        # Bounding box around the spot; skip the full-frame distance computation.
        x0, x1 = max(0, int(cx - radius)), min(w, int(cx + radius) + 1)
        y0, y1 = max(0, int(cy - radius)), min(h, int(cy + radius) + 1)
        if x1 <= x0 or y1 <= y0:
            continue
        d = np.hypot(xx[y0:y1, x0:x1] - cx, yy[y0:y1, x0:x1] - cy)
        m = d <= radius
        img[y0:y1, x0:x1][m] = (
            img[y0:y1, x0:x1][m] * 0.55 + np.array([120, 80, 150]) * 0.45
        ).astype(np.uint8)
    Image.fromarray(img).save(path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="visium_tiny")
    ap.add_argument("--rows", type=int, default=22)
    ap.add_argument("--cols", type=int, default=28)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--legacy-positions", action="store_true",
                    help="write headerless tissue_positions_list.csv (Space Ranger < 2.0)")
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    out = Path(args.out)
    (out / "spatial").mkdir(parents=True, exist_ok=True)

    positions = build_positions(args.rows, args.cols, rng)
    in_tissue = [p for p in positions if p["in_tissue"] == 1]

    spot_diameter_fullres = SPOT_DIAMETER_UM / MICRONS_PER_FULLRES_PX

    # Image is the fullres frame scaled by HIRES_SCALEF.
    max_x = max(p["pxl_col_in_fullres"] for p in positions)
    max_y = max(p["pxl_row_in_fullres"] for p in positions)
    pad = spot_diameter_fullres * 1.5
    hires_w = int(round((max_x + pad) * HIRES_SCALEF))
    hires_h = int(round((max_y + pad) * HIRES_SCALEF))

    # ── positions ─────────────────────────────────────────────────────────────
    cols = ["barcode", "in_tissue", "array_row", "array_col",
            "pxl_row_in_fullres", "pxl_col_in_fullres"]
    lines = []
    if not args.legacy_positions:
        lines.append(",".join(cols))
    for p in positions:
        lines.append(",".join(str(p[c]) for c in cols))
    name = "tissue_positions_list.csv" if args.legacy_positions else "tissue_positions.csv"
    (out / "spatial" / name).write_text("\n".join(lines) + "\n")

    # ── scalefactors: exactly the four keys 10x writes, and no more ───────────
    (out / "spatial" / "scalefactors_json.json").write_text(json.dumps({
        "spot_diameter_fullres": spot_diameter_fullres,
        "fiducial_diameter_fullres": spot_diameter_fullres * 2.5,
        "tissue_hires_scalef": HIRES_SCALEF,
        "tissue_lowres_scalef": LOWRES_SCALEF,
    }, indent=4) + "\n")

    # ── images ────────────────────────────────────────────────────────────────
    hires_spots = [{"hx": p["pxl_col_in_fullres"] * HIRES_SCALEF,
                    "hy": p["pxl_row_in_fullres"] * HIRES_SCALEF} for p in in_tissue]
    write_image(out / "spatial" / "tissue_hires_image.png", hires_w, hires_h,
                hires_spots, spot_diameter_fullres * HIRES_SCALEF / 2.0, rng)
    lowres_spots = [{"hx": p["pxl_col_in_fullres"] * LOWRES_SCALEF,
                     "hy": p["pxl_row_in_fullres"] * LOWRES_SCALEF} for p in in_tissue]
    write_image(out / "spatial" / "tissue_lowres_image.png",
                int(round((max_x + pad) * LOWRES_SCALEF)),
                int(round((max_y + pad) * LOWRES_SCALEF)),
                lowres_spots, spot_diameter_fullres * LOWRES_SCALEF / 2.0, rng)

    # ── counts ────────────────────────────────────────────────────────────────
    # Three spatial "domains" across the tissue so colour-by and NICHESv2 have
    # real structure to find rather than uniform noise.
    genes = LR_PANEL
    xs = np.array([p["pxl_col_in_fullres"] for p in in_tissue])
    domain = np.digitize(xs, np.quantile(xs, [1 / 3, 2 / 3]))
    counts = np.zeros((len(genes), len(in_tissue)), dtype=np.int32)
    for j, d in enumerate(domain):
        base = rng.poisson(3.0, size=len(genes))
        # Each domain over-expresses one third of the panel.
        lo, hi = d * len(genes) // 3, (d + 1) * len(genes) // 3
        base[lo:hi] += rng.poisson(18.0, size=hi - lo)
        counts[:, j] = base
    write_matrix(out / "filtered_feature_bc_matrix.h5",
                 [p["barcode"] for p in in_tissue], genes, counts)

    (out / "metrics_summary.csv").write_text(
        "Number of Spots Under Tissue,Mean Reads per Spot\n"
        f"{len(in_tissue)},{int(counts.sum() / max(1, len(in_tissue)))}\n")

    print(f"wrote {out}/")
    print(f"  {len(positions)} spots ({len(in_tissue)} in tissue), {len(genes)} genes")
    print(f"  hires image {hires_w}x{hires_h} px  (tissue_hires_scalef={HIRES_SCALEF})")
    print(f"  spot_diameter_fullres={spot_diameter_fullres:.2f} px "
          f"-> derived pixel_size={MICRONS_PER_FULLRES_PX / HIRES_SCALEF:.4f} um/hires-px")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
