# Running NICHESv2 to produce `edges.parquet`

One script per platform. They all do the same three things — build a gene × cell count
matrix, build a metadata frame with coordinates **in microns**, run NICHESv2 and export —
but the first two steps differ enough per platform to be worth reading separately.

| Script | Platform | The interesting part |
|---|---|---|
| `niches_xenium.R` | Xenium | The simple case. Coordinates are already µm; nothing to convert. **Read this first.** |
| `niches_seqfish.R` | seqFISH (Spatial Genomics GenePS) | Counts are a dense cells × genes CSV needing transposition; coordinate units vary by GenePS version, so they are detected and reported. |
| `niches_visium_hd.R` | Visium HD | Coordinates are **pixels** and must be multiplied by `microns_per_pixel` before NICHESv2 sees them. |
| `niches_merscope.R` | MERSCOPE (Vizgen) | The easy case: `cell_metadata.csv` is already in microns. But EntityID is a 19-digit integer that R silently mangles as numeric, and small panels often carry no complete LR pair. |
| `niches_cosmx.R` | CosMx (Nanostring/Bruker) | Cell identity is the `(fov, cell_ID)` pair, and coordinates are slide-frame **pixels** that must be shifted to the reader's origin *then* scaled to microns. |
| `niches_visium.R` | Visium (classic) | Same pixel problem, but there is no `microns_per_pixel` to multiply by — it is derived from the 55 µm spot spec, then sanity-checked against the known 100 µm lattice pitch. |
| `niches_common.R` | — | Shared helpers only: the 10x HDF5 reader, barcode alignment, LR-coverage check, output validation. Not runnable. |

The older `*_PPLR.R` scripts are a personal analysis pipeline with hardcoded paths, kept
for reference. The seven above are the ones to copy — one per supported platform.

**Barcodes must match what the viewer serves.** `export_to_TissuePlex()` writes cell ids
into `sending_cell` / `receiving_cell`, and TissuePlex joins the edge layer to its units on
those strings. Get the id scheme wrong and the edges still draw — they carry their own
coordinates — but nothing joins: clicking a unit finds no edge and the metadata filter
drops everything. Visium HD is the live example: barcodes are bin-size specific, so an
edge file built at 16 µm shares zero ids with the 8 µm bins. The reader now picks its bin
from the edge file to keep the two in step.

## Setup

```r
install.packages(c("arrow", "Matrix", "jsonlite", "remotes"))
BiocManager::install("rhdf5")                                   # Xenium / Visium
remotes::install_github("RaredonLab/NICHESv2", ref = "dev")     # note: dev, not main
```

`export_to_TissuePlex()` exists **only on the `dev` branch**, and `arrow` is only in
NICHESv2's `Suggests`, so both of those lines matter.

## Usage

```bash
Rscript r/niches_xenium.R    sample_data/xenium_human_breast_2fov --species human --rad 30
Rscript r/niches_seqfish.R   sample_data/seqfish_synthetic        --species mouse  --rad 12
Rscript r/niches_visium_hd.R sample_data/visium_hd_tiny --bin square_016um --rad 40
Rscript r/niches_visium.R    sample_data/visium_tiny    --species mouse  --rad 150
Rscript r/niches_merscope.R  sample_data/merscope-vpt-smallset --species human --rad 30
Rscript r/niches_cosmx.R     sample_data/cosmx-mousebrain      --species mouse --rad 20
```

Each writes `edges.parquet` into the dataset folder, where TissuePlex picks it up with no
configuration. Use `--out` to write elsewhere — for example into an `edges/` subfolder, so
several runs can be compared from the dropdown:

```bash
Rscript r/niches_xenium.R sample_data/xenium_human_breast_2fov \
  --rad 50 --out sample_data/xenium_human_breast_2fov/edges/niches_rad50.parquet
```

Common flags: `--species` (human/mouse/rat/pig), `--rad` (neighbourhood radius **in µm**),
`--cores`, `--out`. Xenium also takes `--celltype <column>` to carry a cell-type label from
`cells.parquet` through into `sending_type` / `receiving_type`.

## The one thing to get right

`export_to_TissuePlex()` copies `meta.data$x` / `$y` verbatim into `x1..y2`, and the
TissuePlex backend divides those by the dataset's `pixel_size` assuming microns. So
**coordinates must be in microns before NICHESv2 runs**. Xenium and current seqFISH already
are; Visium HD and CosMx are in pixels and must be converted first.

Get it wrong and nothing errors — the edge layer simply sits offset from the cells by
exactly `pixel_size`. If edges look shifted or wildly out of scale, check this before
anything else. `docs/data_format.md` has the per-platform conversion table.

## Reading the output

Each script ends with a summary. What to look for:

- **`score_norm sums to 1 per scored edge: TRUE`** — the normalisation is intact.
- **`placeholder rows`** — every edge in the neighbourhood graph is exported, and those
  with no ligand-receptor signal get one row with `lrm`/`score`/`score_norm` all `NA`.
  This is deliberate: it lets TissuePlex draw the **Tissue Graph** layer (which bins or
  cells are neighbours) separately from the **Edges** layer (which pairs have signal).
  A large placeholder count is normal.
- **`scorable pairs`** — checked before the run. A small targeted panel often has *no* pair
  with both partners present, in which case the script stops with an explanation instead
  of failing deep inside NICHESv2.

## What the demo datasets actually produce

Measured on this repo's fixtures:

| Dataset | Cells/bins | Edges | Scored rows | LRMs | Note |
|---|---|---|---|---|---|
| `xenium_human_breast_2fov` | 7,275 | 181,523 | 189,063 | 32 | Real data; top hits `CDH1\|CDH1`, `CXCL12\|CXCR4` |
| `seqfish_synthetic` | 36 | 250 | 7,364 | 30 | Panel chosen so every gene is half of a real LR pair |
| `visium_hd_tiny` | 3,321 | 66,001 | 147 | 69 | Very shallow — 32k UMIs total, so little scores |
| `mouse_ileum_tiny` | 36 | — | — | — | **Cannot run**: its matrix is a near-empty placeholder (467 counts, 7 expressed genes), so no pair scores |

`mouse_ileum_tiny` is a rendering fixture, not an expression one. That is why the repo's
committed `edges.parquet` for it is synthetic, from `sample_data/make_edges.py`.
