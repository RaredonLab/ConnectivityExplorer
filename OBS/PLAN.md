 # ConnectivityExplorer — Project Plan

## What We Are Building

A custom web-based spatial transcriptomics viewer that phenocopies the Xenium Explorer interface, with full support for additional connectivity data layers produced by the lab's custom R workflows. The tool will be used by an entire lab, deployed via Docker so each user can run their own local instance or the lab can host a shared server.

---

## Core Requirements

### Interface (phenocopy Xenium Explorer as closely as possible)
- Pan and zoom over high-resolution morphology images (deep zoom / tile pyramid)
- Multiple toggleable data layers with opacity and color controls
- Cell segmentation overlay
- Transcript dot overlay (colored by gene, filterable)
- Cell selection — click a cell to get expression readout
- Color-by-gene and **color-by-metadata** (critical feature)
- Annotation and region drawing tools
- Measurement tools
- Export (screenshot, selection)

### Additional Lab-Specific Layers
- **488 distinct ligand-receptor mechanism (LRM) connectivity layers** — the core reason this tool is being built
- Morphogen fields, transcript density maps, and other spatial outputs from R (these can be rasterized if needed, but see below)

---

## Key Architectural Decision: Tabular Edges, Not Raster Images

**Original approach**: R pipeline outputs one raster PNG per LRM → 488 images → tile pyramid per image.

**Better approach (adopted)**: Output a single **edge-list Parquet file** analogous to how Xenium stores transcripts.

### Why this is superior
- Structurally identical to Xenium's `transcripts.parquet` — same rendering pattern, line segments instead of dots
- Toggle 488 LRMs on/off instantly with no image I/O
- Color by LRM identity, ligand, receptor, strength, pathway, p-value, or any column
- Filter by strength threshold interactively via slider
- Encode strength as line width + opacity simultaneously
- Zoom-independent rendering (vector, never pixelated)
- One file instead of 488 images — vastly simpler data management
- WebGL (deck.gl LineLayer) handles millions of edges at 60fps

### Edge data contract (R pipeline output format)

```
edges.parquet
  x1        float   # source cell centroid x, Xenium pixel coordinates
  y1        float   # source cell centroid y
  x2        float   # target cell centroid x
  y2        float   # target cell centroid y
  lrm_id    int     # integer 1–488
  ligand    str
  receptor  str
  strength  float   # connectivity strength value
  cell_id_source  str  # optional, links to Xenium cell ID
  cell_id_target  str  # optional
  # any additional columns (p-value, pathway, etc.) become filterable attributes
```

**Coordinate space**: R pipeline outputs must use the same pixel coordinate system as the Xenium morphology image. Lab has confirmed this is achievable.

---

## Technology Stack

### Frontend
- **React** — UI framework
- **OpenSeadragon** — deep zoom viewer for morphology/immunostain tile pyramids
- **deck.gl** (WebGL) — high-performance rendering of transcripts (points), cell segments (polygons), and connectivity edges (lines)
- Layer panel, cell info panel, annotation tools built as React components

### Backend
- **FastAPI** (Python) — REST API
- Tile server: converts Xenium OME-TIFF → DZI tile pyramid (using **libvips**)
- Xenium data readers: `transcripts.parquet`, `cell_boundaries.parquet`, `cells.zarr.zip`, `cell_feature_matrix`
- Edge data server: serves `edges.parquet` filtered/paginated for the current viewport

### Deployment
- **Docker + docker-compose** — single command to launch, no environment setup
- Each lab member runs their own instance pointed at their own data folder
- OR single lab server deployment for shared access
- Data folder mounted as a Docker volume — tool never copies or modifies source data

---

## Project Structure

```
ConnectivityExplorer/
  backend/
    app/
      main.py              # FastAPI app entry point
      routers/
        tiles.py           # OME-TIFF tile serving
        xenium.py          # transcripts, segments, cell metadata
        edges.py           # connectivity edge data
      readers/
        xenium_reader.py   # parses standard Xenium output folder
        edge_reader.py     # parses edges.parquet
      tiling/
        pyramid.py         # libvips OME-TIFF → DZI conversion
    requirements.txt
    Dockerfile
  frontend/
    src/
      components/
        Viewer.jsx          # OpenSeadragon + deck.gl canvas
        LayerPanel.jsx      # layer list, toggles, opacity, color
        CellInfoPanel.jsx   # cell click → expression readout
        AnnotationTools.jsx # region drawing, measurement
      hooks/
      utils/
    package.json
    Dockerfile
  docker/
    docker-compose.yml
  sample_data/              # gitignored — mount point for Xenium data
  docs/
    data_format.md          # edge Parquet spec for R pipeline
    setup.md
  PLAN.md                   # this file
```

---

## Development Datasets

### Primary (development iteration)
**Xenium 3.0.0 Mouse Ileum tiny — 11.9 MB**
```bash
curl -O https://cf.10xgenomics.com/samples/xenium/3.0.0/Xenium_Prime_MultiCellSeg_Mouse_Ileum_tiny/Xenium_Prime_MultiCellSeg_Mouse_Ileum_tiny_outs.zip
unzip Xenium_Prime_MultiCellSeg_Mouse_Ileum_tiny_outs.zip -d sample_data/
```

### Secondary (performance validation)
**Xenium 2.0.0 Human Breast 2-FOV — 379 MB**
```bash
curl -O https://cf.10xgenomics.com/samples/xenium/2.0.0/Xenium_V1_human_Breast_2fov/Xenium_V1_human_Breast_2fov_outs.zip
unzip Xenium_V1_human_Breast_2fov_outs.zip -d sample_data/
```

Standard Xenium output folder structure:
```
experiment.xenium
morphology_mip.ome.tif
morphology_focus.ome.tif
transcripts.parquet
cells.zarr.zip
cell_feature_matrix.zarr.zip
cell_boundaries.parquet
analysis.zarr.zip
analysis_summary.html
```

---

## Build Sequence

1. **Backend scaffold** — FastAPI skeleton, project structure, Docker config
2. **Tile pipeline** — OME-TIFF → DZI pyramid, tile serving endpoint
3. **Viewer core** — OpenSeadragon loading morphology tiles, pan/zoom working
4. **Transcript layer** — deck.gl ScatterplotLayer rendering transcripts.parquet dots
5. **Cell segment layer** — deck.gl PolygonLayer for cell boundaries
6. **Layer panel UI** — toggle, opacity, color controls
7. **Cell click + expression readout** — click cell → info panel
8. **Color-by-gene and color-by-metadata** — expression/metadata-driven cell coloring
9. **Connectivity edge layer** — deck.gl LineLayer for edges.parquet, LRM toggle panel
10. **Annotation and drawing tools** — region selection, measurement
11. **Docker packaging** — docker-compose, volume mounts, build/run instructions
12. **Performance validation** — test with Human Breast 2-FOV dataset

---

## Open Questions / Decisions Pending

- Shared lab server vs. individual local deployment — both supported by Docker, decide later
- Whether additional R outputs (morphogen fields, density maps) will be served as raster tile pyramids alongside the edge data, or if the edge approach covers all needed layers
- Authentication / access control if hosted on a shared server (not needed for local deployment)

---

## Notes for Reinitializing Claude Code

When you open this repo in a new Claude Code session, this document contains the full plan. The next immediate steps are:

1. Confirm the repo is initialized (git, package.json or pyproject.toml not yet present — we are starting fresh)
2. Download the Mouse Ileum tiny dataset into `sample_data/`
3. Begin with **Step 1: Backend scaffold** — FastAPI skeleton, folder structure, Dockerfile, docker-compose
4. Then **Step 2: Tile pipeline** — this is the foundational rendering piece everything else depends on

The user's lab generates connectivity data as edge-list data frames in R. The R pipeline output format (edges.parquet spec above) has been agreed upon. The R pipeline does not yet exist in this repo — it is separate lab infrastructure. The viewer should validate against synthetic/mock edge data during development until real R outputs are available.
