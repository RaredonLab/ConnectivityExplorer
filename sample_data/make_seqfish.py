#!/usr/bin/env python3
"""
Generate a tiny synthetic seqFISH (Spatial Genomics GenePS) v2 dataset.

Why this exists
---------------
The real GenePS reference dataset is public for CI by written permission from
Spatial Genomics rather than under an open licence, so it cannot be committed
here. Without a committable fixture the seqFISH reader has nothing to test
against in a fresh checkout. This produces a valid, tiny v2 ROI that can be.

It deliberately reproduces the format's two awkward properties, because those
are exactly what a reader gets wrong:

  * **Mixed units.** CellCoordinates and TranscriptList are written in microns;
    Boundaries.geojson is written in pixels. That is what real GenePS output
    does, and a fixture in one uniform space would let a broken reader pass.
  * **Closed GeoJSON rings**, with the cell label in each feature's ``id``.

Usage
-----
    python make_seqfish.py --out seqfish_synthetic
    python make_seqfish.py --out seqfish_synthetic --cells 40 --genes 8 --seed 7

Writes <out>/Roi1_{CellCoordinates.csv,CellxGene.csv,TranscriptList.csv,
Boundaries.geojson,DAPI.tiff,Segmentation.tiff}.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

# Matches the reference dataset, and the documented GenePS value.
PIXEL_SIZE = 0.107161
IMAGE_PX = 512


def build(out: Path, n_cells: int, n_genes: int, seed: int) -> None:
    rng = np.random.default_rng(seed)
    out.mkdir(parents=True, exist_ok=True)

    extent_um = IMAGE_PX * PIXEL_SIZE
    genes = [f"Gene{i:02d}" for i in range(n_genes)]

    # ── Cell centroids on a jittered grid, so cells never overlap ────────────
    per_side = math.ceil(math.sqrt(n_cells))
    step_px = IMAGE_PX / (per_side + 1)
    radius_px = step_px * 0.34

    labels, cx_px, cy_px, radii = [], [], [], []
    for i in range(n_cells):
        r, c = divmod(i, per_side)
        jx, jy = rng.uniform(-0.12, 0.12, 2) * step_px
        labels.append(i + 1)
        cx_px.append((c + 1) * step_px + jx)
        cy_px.append((r + 1) * step_px + jy)
        radii.append(radius_px * rng.uniform(0.72, 1.0))

    cx_px = np.array(cx_px)
    cy_px = np.array(cy_px)
    radii = np.array(radii)

    # ── Boundaries: irregular closed polygons, in PIXEL space ───────────────
    features, mask = [], np.zeros((IMAGE_PX, IMAGE_PX), dtype=np.uint32)
    yy, xx = np.mgrid[0:IMAGE_PX, 0:IMAGE_PX]
    areas_px2 = []
    for i, label in enumerate(labels):
        n_v = int(rng.integers(10, 20))
        ang = np.sort(rng.uniform(0, 2 * np.pi, n_v))
        rad = radii[i] * rng.uniform(0.82, 1.18, n_v)
        vx = np.clip(cx_px[i] + rad * np.cos(ang), 0, IMAGE_PX - 1)
        vy = np.clip(cy_px[i] + rad * np.sin(ang), 0, IMAGE_PX - 1)
        ring = [[int(round(x)), int(round(y))] for x, y in zip(vx, vy)]
        ring.append(ring[0])          # GeoJSON rings are closed
        features.append({
            "type": "Feature",
            "id": str(label),         # the label; joined on by the reader
            "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {"objectType": "cell"},
        })
        # Shoelace area of the ring (px²), and a disc in the label mask.
        rx = np.array([p[0] for p in ring[:-1]], dtype=float)
        ry = np.array([p[1] for p in ring[:-1]], dtype=float)
        areas_px2.append(
            abs(np.dot(rx, np.roll(ry, -1)) - np.dot(ry, np.roll(rx, -1))) / 2
        )
        mask[(xx - cx_px[i]) ** 2 + (yy - cy_px[i]) ** 2 <= radii[i] ** 2] = label

    (out / "Roi1_Boundaries.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, indent=1)
    )

    # ── CellCoordinates: MICRONS (area in µm², matching the real format) ────
    with open(out / "Roi1_CellCoordinates.csv", "w") as fh:
        fh.write("label,area,center_x,center_y\n")
        for i, label in enumerate(labels):
            fh.write(f"{label},{areas_px2[i] * PIXEL_SIZE ** 2:.4f},"
                     f"{cx_px[i] * PIXEL_SIZE:.5f},{cy_px[i] * PIXEL_SIZE:.5f}\n")

    # ── Counts, with a gene gradient so colour-by shows structure ───────────
    counts = np.zeros((n_cells, n_genes), dtype=int)
    for j in range(n_genes):
        bias = 1.0 + 3.0 * (cx_px / IMAGE_PX) if j % 2 == 0 else \
               1.0 + 3.0 * (cy_px / IMAGE_PX)
        counts[:, j] = rng.poisson(bias * rng.uniform(1.5, 5.0))
    with open(out / "Roi1_CellxGene.csv", "w") as fh:
        fh.write("," + ",".join(genes) + "\n")   # unnamed first column
        for i, label in enumerate(labels):
            fh.write(f"{label}," + ",".join(str(v) for v in counts[i]) + "\n")

    # ── Transcripts: MICRONS, scattered inside their cell ───────────────────
    with open(out / "Roi1_TranscriptList.csv", "w") as fh:
        fh.write("name,x,y,z\n")
        for i in range(n_cells):
            for j, gene in enumerate(genes):
                for _ in range(int(counts[i, j])):
                    a = rng.uniform(0, 2 * np.pi)
                    d = radii[i] * math.sqrt(rng.uniform(0, 0.8))
                    x = np.clip(cx_px[i] + d * math.cos(a), 0, IMAGE_PX - 1)
                    y = np.clip(cy_px[i] + d * math.sin(a), 0, IMAGE_PX - 1)
                    fh.write(f"{gene},{x * PIXEL_SIZE:.6f},{y * PIXEL_SIZE:.6f},1\n")

    # ── DAPI (OME-TIFF carrying PhysicalSize) + label mask ─────────────────
    import tifffile
    img = np.zeros((IMAGE_PX, IMAGE_PX), dtype=np.uint16)
    for i in range(n_cells):
        d2 = (xx - cx_px[i]) ** 2 + (yy - cy_px[i]) ** 2
        img += (40000 * np.exp(-d2 / (2 * (radii[i] * 0.45) ** 2))).astype(np.uint16)
    tifffile.imwrite(
        out / "Roi1_DAPI.tiff", img,
        # pixel_size is read back from these OME-XML attributes by the reader.
        metadata={"PhysicalSizeX": PIXEL_SIZE, "PhysicalSizeY": PIXEL_SIZE,
                  "PhysicalSizeXUnit": "µm", "PhysicalSizeYUnit": "µm"},
        ome=True, compression="zlib",
    )
    # Written for format completeness. The reader does not use it: v2 takes
    # boundaries from the GeoJSON, and v1 mask polygonisation is not implemented.
    tifffile.imwrite(out / "Roi1_Segmentation.tiff", mask, compression="zlib")

    n_tx = int(counts.sum())
    print(f"wrote {out}/  —  {n_cells} cells, {n_genes} genes, {n_tx} transcripts, "
          f"{IMAGE_PX}x{IMAGE_PX}px @ {PIXEL_SIZE} µm/px ({extent_um:.1f} µm)")
    print("  CellCoordinates/TranscriptList in MICRONS, Boundaries in PIXELS "
          "(mirrors real GenePS output)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="seqfish_synthetic", help="output directory")
    ap.add_argument("--cells", type=int, default=36)
    ap.add_argument("--genes", type=int, default=10)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    build(Path(__file__).resolve().parent / a.out, a.cells, a.genes, a.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
