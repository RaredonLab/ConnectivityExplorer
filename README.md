# TissuePlex

**v0.8.8**

An interactive spatial transcriptomics viewer for exploring cell-cell communication from [NICHESv2](https://github.com/RaredonLab/NICHESv2) directly on the tissue image.

![TissuePlex demo](docs/demo.gif)

📖 **[User manual](https://raredonlab.github.io/TissuePlex/)** — step-by-step setup and a reference for every control in the interface. No programming experience assumed.

---

## What it does

Spatial transcriptomics platforms (Xenium, seqFISH, Visium, Visium HD, MERSCOPE, CosMx) produce high-resolution images with hundreds of genes measured per cell. NICHESv2 infers which cells are communicating and through which ligand-receptor mechanisms (LRMs). TissuePlex bridges those two outputs: it overlays the NICHESv2 communication graph on the tissue image and lets you explore it interactively.

**Key capabilities:**

- **Toggle individual LRMs in real time** — select any subset of 100s of ligand-receptor mechanisms and instantly see which cell pairs are communicating through them
- **Color edges by communication score or metadata** — visualize LRM set strength, cell type, or any custom column from your analysis as a continuous or categorical color scale
- **Click any edge for full detail** — inspect every active LRM for a given cell pair with their individual scores
- **Directed edges with arrowheads** — A→B and B→A are visually distinct; autocrine communication renders as a ring per cell, on the same color scale and mechanism filter as the edges
- **Multiple edge sets per dataset** — drop several `.parquet` files into an `edges/` folder and flip between scoring approaches on the same tissue without duplicating the image or cell data
- **Pan and zoom on high-resolution morphology images** — OME-TIFF tile pyramid with smooth zoom from whole-tissue to single-cell scale
- **Multi-channel morphology** — Xenium `morphology_focus/` channels are selectable alongside the top-level morphology image
- **Cross-platform metadata** — the `cell-metadata/` convention works the same way on every platform that supports it, so annotation workflows transfer between Xenium and seqFISH unchanged
- **Split-screen comparison** — two independently navigable panels sharing one set of layer controls, with a match-zoom button
- **Per-panel rotation** — rotate either panel to any angle to align tissue orientation
- **Transcript dot overlay** — per-gene colored dots, filterable by gene species, with hover tooltips
- **Cell/spot segmentation** — polygon boundaries with color-by-gene-set or color-by-metadata, and editable per-category colors
- **Autocrine signalling** — self-signalling drawn as a ring per cell, colored by the same scale as the directed edges and obeying the same mechanism filter
- **Metadata filtering** — restrict the view to a subset of cells or edges (a sample, a few cell types, a value range). Applied server-side before sampling, so a rare cluster renders at full density instead of being sampled away
- **Treat-as-categorical toggle** — integer-coded cluster IDs get a discrete editable palette rather than a viridis gradient, with the numeric order preserved in the legend
- **Region drawing and measurement tools** — annotate areas, export cell selections, save PNG screenshots
- **Supplemental metadata** — drop any CSV or parquet into a `cell-metadata/` folder to add custom color-by columns (clusters, pseudotime, etc.) without touching the original data
- **Multi-dataset support** — switch between datasets without restarting; each is auto-detected by platform

---

## Supported platforms

| Platform | Vendor | Morphology | Transcripts | Cell segments | Edges |
|---|---|:---:|:---:|:---:|:---:|
| **Xenium** | 10x Genomics | ✓ | ✓ | ✓ | ✓ |
| **seqFISH** | Spatial Genomics | ✓ | ✓ | ✓ | ✓ |
| **Visium HD** | 10x Genomics | ✓ | — | ✓ (bins) | ✓ |
| **Visium** | 10x Genomics | ✓ | — | ✓ (spots) | ✓ |
| **MERSCOPE** | Vizgen | ✓ | ✓ | ✓ | ✓ |
| **CosMx** | Nanostring | placeholder | ✓ | ✓ | ✓ |

All nine bundled demo datasets ship `edges.parquet` and an `edge-metadata/` folder, so the connectivity layer and the annotation workflow have something to show on every platform. Two caveats worth knowing: the MERSCOPE panel carries only **2** complete ligand-receptor pairs and the seqFISH reference panel **none**, because targeted panels are chosen for cell typing rather than signalling — `check_lr_coverage()` reports this before NICHESv2 runs, and `seqfish_instrument2` therefore uses synthetic edges from `make_edges.py`. The `edge-metadata/` folders are generated demo annotations, not analysis output; each carries a README saying which columns are derived and which is invented.

seqFISH means the commercial **Spatial Genomics GenePS** output, not the academic seqFISH/seqFISH+ method, which has no standard file layout; the current v2 layout is fully supported, and legacy v1 reads cells and transcripts but not boundaries.

The two array-based platforms have no per-molecule transcript coordinates — only spot- or bin-level UMI counts — so they synthesise a polygon per unit instead: a square for a Visium HD bin, a circle for a 55 µm Visium spot. Fill, colour-by, picking and region selection then all work through the same layers. CosMx datasets often ship no morphology image, in which case TissuePlex renders the data onto a blank canvas sized to the tissue. Each reader declares what it supports via a capability flag, and the UI hides layers the platform cannot serve — and relabels itself, so a Visium dataset says "spot" wherever a Xenium one says "cell".

The edge connectivity layer (NICHESv2 output) works with any platform — it is platform-agnostic as long as cell barcodes match.

---

## Quick start

**Requirements:** [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Mac/Windows) or Docker Engine + Compose v2 (Linux). Nothing else needed on the host.

### Demo with sample data

```bash
git clone https://github.com/RaredonLab/TissuePlex.git
cd TissuePlex
docker compose up --build
```

Open **http://localhost:3000**.

### Your own data

```bash
DATA_PATH=/absolute/path/to/your/datasets docker compose up --build
```

`DATA_PATH` should be a **parent folder** containing one or more platform output directories. TissuePlex auto-detects the platform from each subdirectory's contents:

```
/your/datasets/
  xenium_run_A/
    experiment.xenium       ← Xenium sentinel
    morphology.ome.tif
    morphology_focus/       ← optional; extra channels appear in the image picker
    cells.parquet
    transcripts.parquet
    cell_boundaries.parquet
    edges.parquet           ← NICHESv2 output (optional)
    edges/                  ← optional; additional edge sets to flip between
      raw_minimum.parquet
      normalized_product.parquet

  seqfish_run_B/            ← Spatial Genomics GenePS; one ROI per folder
    Roi1_CellCoordinates.csv    ← seqFISH sentinel
    Roi1_CellxGene.csv
    Roi1_TranscriptList.csv
    Roi1_Boundaries.geojson
    Roi1_DAPI.tiff
    edges.parquet

  visium_hd_run_C/          ← Space Ranger outs/
    binned_outputs/
      square_008um/         ← Visium HD sentinel
        filtered_feature_bc_matrix.h5
        spatial/{tissue_positions.parquet, scalefactors_json.json, tissue_hires_image.png}
    edges.parquet

  visium_run_D/
    filtered_feature_bc_matrix.h5
    spatial/                ← Visium sentinel: scalefactors_json.json +
      scalefactors_json.json     tissue_positions.csv
      tissue_positions.csv
      tissue_hires_image.png
    edges.parquet

  merscope_run_E/
    cell_by_gene.csv        ← MERSCOPE sentinel
    cell_metadata.csv
    detected_transcripts.csv
    edges.parquet

  cosmx_run_F/
    my_experiment_tx_file.csv   ← CosMx sentinel
    edges.parquet
```

If `edges.parquet` is absent the edge layers are hidden — all other layers work normally. When a dataset has more than one edge source, a dropdown appears at the top of the Edge Data section; the selection applies to every open panel.

The first launch builds DZI tile pyramids from OME-TIFF morphology images. This takes ~30 seconds per dataset and is cached across restarts.

### Deploying to a server

[docs/cloud-deploy.md](docs/cloud-deploy.md) is a step-by-step DigitalOcean runbook (~$106–116/month) covering droplet setup, block storage for data, DNS, automatic TLS via Caddy, and data upload. Note that **access control is opt-in**: unless you enable Caddy's `basicauth`, anyone with the URL can view the data.

---

## NICHESv2 workflow

TissuePlex is designed as a downstream visualization step for [NICHESv2](https://github.com/RaredonLab/NICHESv2). After running NICHESv2 on your spatial dataset, export the connectivity object:

```r
# In R, after running NICHESv2:
export_to_TissuePlex(
  niches_object,
  output.path = "/your/datasets/xenium_run_A/edges.parquet",
  celltype.col = "Type.6"
)
```

Working examples of the full pipeline — exporting Seurat metadata, running NICHESv2, and exporting the parquet — are in [`r/`](r/). Those scripts have hardcoded paths and are meant to be read and adapted, not run as-is.

Then launch TissuePlex — the edge layer will appear automatically.

The `edges.parquet` format is one row per **(directed edge) × (LRM)**. A→B and B→A are separate rows. Any additional columns in the file (cell types, scores, custom metadata) are automatically available as color-by options in the UI. See [docs/data_format.md](docs/data_format.md) for the full column specification.

---

## Supplemental cell metadata

To add custom annotation columns (clusters, pseudotime, leiden labels, etc.) to the cell color-by menu without modifying the original platform output:

```
dataset_folder/
  experiment.xenium
  cells.parquet
  cell-metadata/          ← create this directory
    clusters.csv          ← barcodes in first column (or cell_id column)
    pseudotime.parquet
```

Standard R export works out of the box:

```r
write.csv(my_metadata, file.path(dataset_dir, "cell-metadata", "metadata.csv"))
```

Columns appear automatically in the **Cell Color** and **Cell Filter** dropdowns. Continuous columns get a gradient; string and low-cardinality integer columns get discrete colors. Use **treat as categorical** to override that guess either way — a Seurat cluster column with more than 30 levels still gets discrete colors, and a coded column you want as a gradient can have one.

## Supplemental edge metadata

The same idea for cell *pairs* — annotate edges without regenerating `edges.parquet` from R:

```
dataset_folder/
  edges.parquet
  edge-metadata/          ← create this directory
    annotations.csv       ← key column `edge` = "SendingCell|ReceivingCell"
```

```r
write.csv(annotations_df, file.path(dataset_dir, "edge-metadata", "annotations.csv"))
```

Columns appear automatically in the **edge color** dropdown, and show as an *Annotations* block when you click an edge. The folder sits beside the dataset rather than beside the edge file, so one set of annotations applies across every edge source — annotations describe cell pairs, which belong to the tissue rather than to one scoring run.

`sample_data/mouse_ileum_tiny` ships worked examples of both `cell-metadata/` and `edge-metadata/`.

---

## Development setup

```bash
# Backend (FastAPI + DuckDB)
cd backend
pip install -r requirements.txt
DATA_ROOT=../sample_data uvicorn app.main:app --reload

# Frontend (React + Vite) — in a separate terminal
cd frontend
npm install
npm run dev   # → http://localhost:5173, proxies /api → :8000
```

The dev server uses port 5173 so it does not collide with `docker compose`, which binds 3000 for the production frontend.

There is currently no automated test suite, CI, or linter — changes are verified by running the app.

---

## Architecture

```
Browser
  OpenSeadragon   — pan/zoom over OME-TIFF tile pyramid
  deck.gl (WebGL) — all data layers; coordinate-synced to OSD

FastAPI backend
  /tiles    — OME-TIFF → DZI tile pyramid (pyvips / tifffile fallback)
  /spatial  — transcripts, cell boundaries, cell metadata, gene expression
  /edges    — edge query, LRM catalogue, per-edge color values, edge detail
  /layers   — generic parquet layer serving
```

All data is served directly from parquet files via DuckDB — no database setup or import step. Tile pyramids are built on first access and cached.

### Adding a new platform

1. Create `backend/app/readers/my_platform_reader.py` extending `SpatialDatasetReader`
2. Implement the abstract methods (`cells`, `transcripts`, `cell_boundaries`, etc.)
3. Override `capabilities()` to declare which layers the platform supports
4. Register a detector in `reader_factory.py`

The frontend automatically adapts its layer controls to the capabilities your reader declares.

---

## Roadmap

Tracked in [GitHub issues](https://github.com/RaredonLab/TissuePlex/issues).

[#45](https://github.com/RaredonLab/TissuePlex/issues/45) (select by metadata) and [#35](https://github.com/RaredonLab/TissuePlex/issues/35) (treat-as-categorical toggle) are both implemented. Natural follow-ups, neither yet built:

- Combining more than one filter at a time — today it is one column, so "cluster 4 *and* sample B" needs two passes
- Persisting the categorical choice and the palette across reloads
- Filtering transcripts, which needs a transcript→cell assignment that several platforms do not ship

Large datasets are handled by a spatial index built automatically on first access, alongside the tile pyramid: files over 64 MB are rewritten sorted by a spatial grid, which makes viewport queries roughly 27× faster on a 40M-row transcript file and also converts seqFISH CSV to parquet along the way. Set `SPATIAL_CACHE=0` to disable it.

---

## Citation

If you use TissuePlex in published work, please cite: *(preprint / paper link — coming soon)*
