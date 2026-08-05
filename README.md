# TissuePlex

An interactive spatial transcriptomics viewer for exploring cell-cell communication from [NICHESv2](https://github.com/RaredonLab/NICHESv2) directly on the tissue image.

![TissuePlex demo](docs/demo.gif)

---

## What it does

Spatial transcriptomics platforms (Xenium, seqFISH, Visium HD, MERSCOPE, CosMx) produce high-resolution images with hundreds of genes measured per cell. NICHESv2 infers which cells are communicating and through which ligand-receptor mechanisms (LRMs). TissuePlex bridges those two outputs: it overlays the NICHESv2 communication graph on the tissue image and lets you explore it interactively.

**Key capabilities:**

- **Toggle individual LRMs in real time** — select any subset of 100s of ligand-receptor mechanisms and instantly see which cell pairs are communicating through them
- **Color edges by communication score or metadata** — visualize LRM set strength, cell type, or any custom column from your analysis as a continuous or categorical color scale
- **Click any edge for full detail** — inspect every active LRM for a given cell pair with their individual scores
- **Directed edges with arrowheads** — A→B and B→A are visually distinct; autocrine communication renders as rings
- **Multiple edge sets per dataset** — drop several `.parquet` files into an `edges/` folder and flip between scoring approaches on the same tissue without duplicating the image or cell data
- **Pan and zoom on high-resolution morphology images** — OME-TIFF tile pyramid with smooth zoom from whole-tissue to single-cell scale
- **Multi-channel morphology** — Xenium `morphology_focus/` channels are selectable alongside the top-level morphology image
- **Cross-platform metadata** — the `cell-metadata/` convention works the same way on every platform that supports it, so annotation workflows transfer between Xenium and seqFISH unchanged
- **Split-screen comparison** — two independently navigable panels sharing one set of layer controls, with a match-zoom button
- **Per-panel rotation** — rotate either panel to any angle to align tissue orientation
- **Transcript dot overlay** — per-gene colored dots, filterable by gene species, with hover tooltips
- **Cell/spot segmentation** — polygon boundaries with color-by-gene-set or color-by-metadata, and editable per-category colors
- **Region drawing and measurement tools** — annotate areas, export cell selections, save PNG screenshots
- **Supplemental metadata** — drop any CSV or parquet into a `cell-metadata/` folder to add custom color-by columns (clusters, pseudotime, etc.) without touching the original data
- **Multi-dataset support** — switch between datasets without restarting; each is auto-detected by platform

---

## Supported platforms

| Platform | Vendor | Morphology | Transcripts | Cell segments | Edges |
|---|---|:---:|:---:|:---:|:---:|
| **Xenium** | 10x Genomics | ✓ | ✓ | ✓ | ✓ |
| **seqFISH** | Spatial Genomics | ✓ | ✓ | ✓ | ✓ |
| **Visium HD** | 10x Genomics | ✓ | — | — | ✓ |
| **MERSCOPE** | Vizgen | — | ✓ | — | ✓ |
| **CosMx** | Nanostring | — | ✓ | — | ✓ |

Xenium and seqFISH are the complete implementations. seqFISH means the commercial **Spatial Genomics GenePS** output, not the academic seqFISH/seqFISH+ method, which has no standard file layout; the current v2 layout is fully supported, and legacy v1 reads cells and transcripts but not boundaries.

The other readers cover cells, transcripts, and metadata coloring; boundary parsing is platform-specific and not yet implemented for them (MERSCOPE stores polygons in HDF5, CosMx in per-FOV label TIFFs). Visium HD renders bins as points rather than polygons and has no per-molecule transcript coordinates. Each reader declares what it supports via a capability flag, and the UI hides layers the platform cannot serve.

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

  visium_hd_run_C/
    square_008um/           ← Visium HD sentinel
    edges.parquet

  merscope_run_D/
    cell_by_gene.csv        ← MERSCOPE sentinel
    cell_metadata.csv
    detected_transcripts.csv
    edges.parquet

  cosmx_run_E/
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

Columns appear automatically in the **Cell Color** dropdown. Continuous columns get a gradient; string and low-cardinality integer columns get discrete colors.

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

Tracked in [GitHub issues](https://github.com/RaredonLab/TissuePlex/issues). Currently open:

- **[#45](https://github.com/RaredonLab/TissuePlex/issues/45)** — select cells and edges by metadata, so you can focus on a sample or a few cell types instead of the whole dataset
- **[#35](https://github.com/RaredonLab/TissuePlex/issues/35)** — a "treat as categorical" toggle for numeric metadata columns, so integer-coded cluster IDs get a discrete editable palette instead of a continuous gradient

Also known and not yet addressed: transcript and cell-boundary queries read their full parquet file on every viewport change rather than pushing the bbox filter down to DuckDB the way the edge queries do. This is the main performance limit on very large datasets.

---

## Citation

If you use TissuePlex in published work, please cite: *(preprint / paper link — coming soon)*
