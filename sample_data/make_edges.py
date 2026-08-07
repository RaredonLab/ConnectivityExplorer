"""
Generate NICHESv2-aligned edges.parquet from cells.parquet.

Schema mirrors the NICHESv2 $edge.data + $edge.meta design:
  - Directional: A->B and B->A are separate edges with independent scores
  - Autocrine: cells may signal to themselves (sending_cell == receiving_cell)
  - Long format: one row per (edge, LRM), multiple LRMs per cell pair
  - Score columns: score (raw) + score_norm (proportional within edge)

Column reference:
  edge           "SendingCell|ReceivingCell"   (NICHESv2 edge ID convention)
  sending_cell   barcode string
  receiving_cell barcode string (== sending_cell for autocrine)
  is_autocrine   bool
  lrm            "ligand|receptor"             (NICHESv2 LRM ID convention)
  lrm_id         integer                       (legacy; keep for compatibility)
  ligand         string
  receptor       string
  score          float  raw score
  score_norm     float  score / sum(score) within edge (proportional)
  x1, y1         sending cell centroid (Xenium µm)
  x2, y2         receiving cell centroid (µm)  (== x1,y1 for autocrine)
  sending_type   string cell type (simulated)
  receiving_type string cell type (simulated)

Run from repo root:
    python sample_data/make_edges.py [--dataset mouse_ileum_tiny] [--k 6] [--lrms 3]
"""
import argparse
import json
import math
import random
from pathlib import Path

import numpy as np
import pandas as pd

# LRM catalogue — NICHESv2 "ligand|receptor" string IDs
LRM_CATALOGUE = [
    (1,  "Tgfb1",  "Tgfbr1"),
    (2,  "Tgfb1",  "Tgfbr2"),
    (3,  "Il6",    "Il6ra"),
    (4,  "Il6",    "Fgfr1"),
    (5,  "Wnt5a",  "Fzd1"),
    (6,  "Wnt5a",  "Ror2"),
    (7,  "Efnb1",  "EphB2"),
    (8,  "Efnb1",  "EphB4"),
    (9,  "Cxcl12", "Cxcr4"),
    (10, "Vegfa",  "Kdr"),
    (11, "Vegfa",  "Flt1"),
    (12, "Pdgfb",  "Pdgfrb"),
    (13, "Hgf",    "Met"),
    (14, "Egf",    "Egfr"),
    (15, "Notch1", "Dll4"),
    (16, "Notch2", "Jag1"),
    (17, "Spp1",   "Cd44"),
    (18, "Ccl2",   "Ccr2"),
    (19, "Bmp4",   "Bmpr2"),
    (20, "Fgf2",   "Fgfr1"),
]

# Simulated cell types (no real annotation in demo data)
CELL_TYPES = ["Epithelial", "Fibroblast", "Endothelial", "Immune", "Smooth_Muscle"]


def _lrm_string(ligand: str, receptor: str) -> str:
    return f"{ligand}|{receptor}"


def _score_for_pair(dist: float, rng: random.Random, np_rng) -> float:
    """Distance-attenuated score with Gaussian noise."""
    base = max(0.0, 3.5 - dist / 25.0)
    return float(np.clip(base + rng.gauss(0, 0.35), 0.01, 4.0))


def _load_cells(dataset_dir: Path) -> pd.DataFrame:
    """Cell ids and centroids in **microns**, for any supported platform.

    Xenium's `cells.parquet` is read directly when present, since that keeps this
    script usable with no backend on the path. Otherwise it falls back to the
    TissuePlex reader, which is what makes the generator work on seqFISH, CosMx,
    MERSCOPE and the two Visium platforms.

    The units conversion is the part to get right. Readers return centroids in
    **image pixel space**, while `edges.parquet` stores **microns** — the backend
    divides by `pixel_size` to place edges. So the reader path multiplies back up
    by `pixel_size`, and the round trip lands the edge exactly on its cell.
    """
    xen = dataset_dir / "cells.parquet"
    if xen.exists():
        return pd.read_parquet(xen)[["cell_id", "x_centroid", "y_centroid"]]

    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
    from app.readers.reader_factory import ReaderFactory

    reader = ReaderFactory.detect(dataset_dir)
    ps = reader.pixel_size
    rows = reader.cells()
    if not rows:
        raise SystemExit(f"{dataset_dir.name}: reader returned no cells")
    df = pd.DataFrame(rows)[["cell_id", "x_centroid", "y_centroid"]].dropna()
    df["x_centroid"] = df["x_centroid"].astype(float) * ps   # image px → µm
    df["y_centroid"] = df["y_centroid"].astype(float) * ps
    print(f"  read {len(df)} cells via the {reader.platform} reader "
          f"(pixel_size {ps:.5f} µm/px)")
    return df.reset_index(drop=True)


def make_edges(dataset_dir: Path, k_neighbors: int = 6, lrms_per_pair: int = 3,
               autocrine_fraction: float = 0.15, seed: int = 42) -> pd.DataFrame:
    rng = random.Random(seed)
    np_rng = np.random.default_rng(seed)

    cells = _load_cells(dataset_dir)
    coords = cells[["x_centroid", "y_centroid"]].values  # (N, 2) µm
    N = len(cells)
    barcodes = cells["cell_id"].tolist()

    # Assign a simulated cell type to each cell (deterministic from seed)
    np_rng_types = np.random.default_rng(seed + 1)
    cell_types = {bc: CELL_TYPES[np_rng_types.integers(len(CELL_TYPES))] for bc in barcodes}

    # Build directed k-NN edge list: A->B and B->A treated independently
    directed_pairs = set()
    for i in range(N):
        dists = np.linalg.norm(coords - coords[i], axis=1)
        dists[i] = np.inf
        neighbours = np.argsort(dists)[:k_neighbors]
        for j in neighbours:
            directed_pairs.add((i, j))  # directed: i sends to j

    directed_pairs = sorted(directed_pairs)

    # Optionally add autocrine edges (~autocrine_fraction of cells)
    autocrine_cells = []
    for i, bc in enumerate(barcodes):
        if rng.random() < autocrine_fraction:
            autocrine_cells.append(i)

    records = []

    def _add_edge_records(i, j, is_autocrine: bool):
        ci = cells.iloc[i]
        cj = cells.iloc[j]
        bc_send = barcodes[i]
        bc_recv = barcodes[j]
        edge_id = f"{bc_send}|{bc_recv}"
        dist = 0.0 if is_autocrine else float(np.linalg.norm(coords[i] - coords[j]))

        chosen_lrms = rng.sample(LRM_CATALOGUE, k=min(lrms_per_pair, len(LRM_CATALOGUE)))
        raw_scores = [_score_for_pair(dist, rng, np_rng) for _ in chosen_lrms]
        total = sum(raw_scores)

        for (lrm_id, ligand, receptor), raw in zip(chosen_lrms, raw_scores):
            records.append({
                "edge":           edge_id,
                "sending_cell":   bc_send,
                "receiving_cell": bc_recv,
                "is_autocrine":   is_autocrine,
                "lrm":            _lrm_string(ligand, receptor),
                "lrm_id":         lrm_id,
                "ligand":         ligand,
                "receptor":       receptor,
                "score":          round(raw, 4),
                "score_norm":     round(raw / total, 6) if total > 0 else 0.0,
                "x1":             float(ci.x_centroid),
                "y1":             float(ci.y_centroid),
                "x2":             float(cj.x_centroid),
                "y2":             float(cj.y_centroid),
                "sending_type":   cell_types[bc_send],
                "receiving_type": cell_types[bc_recv],
            })

    for (i, j) in directed_pairs:
        _add_edge_records(i, j, is_autocrine=False)

    for i in autocrine_cells:
        _add_edge_records(i, i, is_autocrine=True)

    df = pd.DataFrame(records)

    n_directed = sum(1 for r in records if not r["is_autocrine"])
    n_autocrine = sum(1 for r in records if r["is_autocrine"])
    n_unique_edges = df[~df["is_autocrine"]]["edge"].nunique()
    n_autocrine_edges = df[df["is_autocrine"]]["edge"].nunique()
    print(f"Generated {len(df)} edge×LRM rows:")
    print(f"  {n_directed} rows from {n_unique_edges} directed edges "
          f"(k={k_neighbors}, {lrms_per_pair} LRMs/pair)")
    print(f"  {n_autocrine} rows from {n_autocrine_edges} autocrine self-loops")
    print(f"  Cells: {N}  |  x1 range: {df.x1.min():.1f}–{df.x1.max():.1f} µm")
    print(f"  score range: {df.score.min():.2f}–{df.score.max():.2f}")
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="mouse_ileum_tiny")
    ap.add_argument("--k", type=int, default=6, help="k nearest neighbours per cell")
    ap.add_argument("--lrms", type=int, default=3, help="LRM assignments per cell pair")
    ap.add_argument("--autocrine-fraction", type=float, default=0.15,
                    help="Fraction of cells with autocrine self-loops")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="edges.parquet",
                    help="Output path relative to the dataset dir. Use e.g. "
                         "'edges/edge.raw.minimum.parquet' to add a selectable "
                         "edge set to the dataset's edges/ folder (issue #46).")
    args = ap.parse_args()

    here = Path(__file__).parent
    dataset_dir = here / args.dataset

    df = make_edges(dataset_dir, k_neighbors=args.k, lrms_per_pair=args.lrms,
                    autocrine_fraction=args.autocrine_fraction, seed=args.seed)

    out = dataset_dir / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
