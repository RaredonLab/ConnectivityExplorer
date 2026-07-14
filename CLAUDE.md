# TissuePlex — Claude Code Project Brief

This file is read automatically by Claude Code at the start of every session.
It provides full context on the architecture, design decisions, and current state
of the project so any Claude instance can contribute immediately.

---

## What This Is

A web-based spatial transcriptomics viewer supporting multiple platforms (Xenium,
MERSCOPE, CosMx) with connectivity layers produced by the lab's NICHESv2 R pipeline.
Built because Xenium Explorer does not support cell-cell ligand-receptor mechanism
(LRM) visualization, and extended to be platform-agnostic.

**Core insight**: Rather than rasterizing 488 LRM outputs as PNG images, we store
connectivity data as a single `edges.parquet` file and render it as WebGL vector lines.
This allows instant toggling of 488 LRMs, coloring by any metadata, and zoom-independent
rendering. The `edges.parquet` format is platform-agnostic — it works with any spatial
dataset as long as cell barcodes match.

---

## Architecture

```
Browser
  OpenSeadragon    — pan/zoom over OME-TIFF tile pyramid (morphology image)
  deck.gl (WebGL)  — all data layers rendered as vectors, coordinate-synced to OSD
  React + Zustand  — UI state management

FastAPI backend
  /tiles     — OME-TIFF → DZI tile pyramid (pyvips/tifffile), tile serving
  /spatial   — platform-agnostic: transcripts, cell boundaries, cell metadata,
               gene expression, color-values, dataset list, per-dataset image list
  /edges     — edge list, LRM catalogue, per-edge color values, edge detail
  /layers    — generic parquet layer serving (extensible)
```

**Key architectural constraint**: OpenSeadragon handles all pan/zoom events. deck.gl
sits in an absolutely-positioned canvas on top, with its viewport synced to OSD via a
custom `syncDeckFromOSD` function on every OSD viewport-change event. All data is
returned in image pixel coordinates (native_coord / pixel_size).

---

## Platform Support & Reader Architecture

The backend uses an abstract reader pattern. All platform readers inherit from
`SpatialDatasetReader` (base_reader.py) and implement the same interface.
`ReaderFactory` auto-detects the platform from directory contents.

**Detection order:**
| Platform | Sentinel file |
|---|---|
| Xenium (10x Genomics) | `experiment.xenium` |
| MERSCOPE (Vizgen) | `cell_by_gene.csv` or `cell_metadata.csv` |
| CosMx (Nanostring) | `*_tx_file.csv` |

**Coordinate contract**: Every reader converts native coordinates to image pixel space
before returning data. The frontend always receives pixel coordinates.

**Implementation status:**
- Xenium: fully implemented
- MERSCOPE: cells, transcripts, genes, color-values (metadata + gene-set) implemented;
  cell boundaries stub (MERSCOPE uses HDF5 boundary format, not yet parsed)
- CosMx: cells, transcripts, genes, metadata color-values implemented;
  gene-set color-values stub (requires transcript aggregation per cell)

---

## Repository Layout

```
backend/
  app/
    main.py                  FastAPI entry point, CORS, router registration
    routers/
      tiles.py               DZI descriptor + tile serving; auto-builds pyramid on first request
      spatial.py             Platform-agnostic router: all /spatial/... endpoints
      xenium.py              DEPRECATED — kept for reference; not registered in main.py
      edges.py               edge query, LRM catalogue, edge color values, edge detail;
                             all endpoints take an edge_file param (multi-file support);
                             /files lists edge sources (top-level + edges/ folder)
      layers.py              generic parquet layer router
    readers/
      base_reader.py         Abstract base class — SpatialDatasetReader interface
      reader_factory.py      ReaderFactory: auto-detect platform, instantiate reader
      xenium_reader.py       Xenium implementation (inherits SpatialDatasetReader)
      merscope_reader.py     MERSCOPE implementation (inherits SpatialDatasetReader)
      cosmx_reader.py        CosMx implementation (inherits SpatialDatasetReader)
      edge_reader.py         reads edges.parquet; query_grouped(), lrm_catalogue(), edge_color_values(), edge_detail()
      layer_reader.py        generic parquet reader
    tiling/
      pyramid.py             OME-TIFF → DZI; pyvips streaming primary, tifffile+Pillow fallback
  requirements.txt           pinned deps; cffi<2.0 required for pyvips 2.2.3 compatibility
  Dockerfile

frontend/
  src/
    store.js                 Zustand store — ALL shared state lives here
    components/
      App.jsx                Root component; wraps everything in a React ErrorBoundary
      Viewer.jsx             Split-screen wrapper (Viewer) + per-panel logic (ViewerPanel)
      LayerPanel.jsx         Right-side panel: toggles, opacity, color-by, legends,
                             dataset/image picker, transcript species filter
      CellInfoPanel.jsx      Floating panel on cell click; shows color-by value highlight
      EdgeInfoPanel.jsx      Floating panel on edge/autocrine click
      AnnotationToolbar.jsx  Region drawing + measurement tools; ⊞ Split / □ Single toggle; ⇔ Match zoom
    hooks/
      useTranscripts.js      Viewport-bounded transcript fetch (bbox always sent; skip at low zoom)
      useCellBoundaries.js   Viewport-bounded cell boundary fetch (skip when fracW >= 0.5)
      useCellColors.js       POST color-values; maps cell_id → RGBA; supports clamp
      useEdgeColors.js       lrm_set: client-side from visible_score_sum; metadata: POST edge-color-values
      useEdges.js            Viewport-bounded edge fetch; POSTs to /query-grouped
    utils/
      colormap.js            Palette definitions (viridis/plasma/magma/inferno) + valueToColor()
      geneColor.js           Deterministic gene → color mapping
  vite.config.js             Dev server proxies /api → localhost:8000
  nginx.conf                 Production: proxies /api/ → backend:8000/
  Dockerfile                 Multi-stage: node build → nginx serve

docker-compose.yml           Repo root; mounts DATA_PATH (or sample_data/) as /data:ro
docker/docker-compose.yml    Legacy path (kept for compatibility)
sample_data/                 GITIGNORED — default data mount for local dev/demo
r/
  export_NICHESObject_for_viewer.R  draft R function for NICHESv2 → edges.parquet export
docs/
  data_format.md             edges.parquet column spec for NICHESv2 R export
  setup.md                   Docker deployment guide
  public_datasets.md         Links to public Xenium datasets used for development
```

---

## Data Model: edges.parquet (NICHESv2 Format)

One row per **(directed edge) × (LRM)**. This is the long/sparse format from NICHESv2.
Platform-agnostic — works with any spatial dataset as long as cell barcodes match.

| Column | Type | Notes |
|---|---|---|
| `edge` | string | `"SendingCell\|ReceivingCell"` — directed edge ID |
| `sending_cell` | string | Cell barcode matching the platform's cell_id |
| `receiving_cell` | string | Cell barcode |
| `is_autocrine` | bool | True when sending == receiving |
| `lrm` | string | `"ligand\|receptor"` mechanism ID |
| `lrm_id` | int | Integer index (1–N) |
| `ligand` | string | |
| `receptor` | string | |
| `score` | float | Raw NICHESv2 score |
| `score_norm` | float | Score normalized within edge (sums to 1) |
| `x1`, `y1` | float | Sending cell centroid, native µm coords |
| `x2`, `y2` | float | Receiving cell centroid |
| `sending_type` | string | Optional cell type label |
| `receiving_type` | string | Optional cell type label |

**Important**: Coordinates in edges.parquet are in native µm. The backend divides by
`pixel_size` (from the reader) when serving to the frontend.

The `sample_data/make_edges.py` script generates synthetic demo data in this format.
Real data comes from `export_for_TissuePlex()` in the NICHESv2 R package.

---

## Supplemental Cell Metadata

User-defined metadata (e.g. from external R analysis) can be loaded without modifying
the dataset output by placing files in a `cell-metadata/` subdirectory of the dataset.
Currently implemented in XeniumReader; the pattern should be ported to other readers.

```
dataset_dir/
  experiment.xenium   (or equivalent platform sentinel)
  cells.parquet
  cell-metadata/          ← create this directory
    my_metadata.csv       ← one or more files here
    clusters.csv
    pseudotime.parquet
```

**Supported formats**: `.csv`, `.csv.gz`, `.parquet`.  Multiple files are allowed and
are outer-joined on the barcode key.

**Barcode column resolution** (in order of precedence):
1. A column explicitly named `cell_id`
2. `Unnamed: 0` — pandas' name for R's unnamed rowname column from `write.csv(row.names=TRUE)`
3. The first column if it contains unique strings (generic fallback)
4. Parquet files: `cell_id` column required

Standard R export that works out of the box:
```r
write.csv(my_metadata_df, file.path(dataset_dir, "cell-metadata", "metadata.csv"))
# row.names=TRUE is R's default; barcodes go in the first unnamed column
```

**How it surfaces in the UI**: supplemental columns are merged into the cells table via
`XeniumReader._cells_full()`. They appear automatically in the "Cell metadata" color-by
dropdown. Continuous columns get a gradient colormap; string or low-cardinality integer
columns get discrete colors. The cell-click info panel also shows the supplemental fields.

`XeniumReader._cells_full()` is cached per reader instance (one Docker request lifecycle).
`_load_supplemental_metadata()` is also cached, so the CSV is only parsed once regardless
of how many color-by requests arrive.

---

## Multiple Edge Files (edges/ folder)

A dataset can carry more than one edge set so users can flip between different
computational approaches (e.g. raw-count vs. normalized scoring) on the **same**
tissue without duplicating the cell / transcript / boundary parquet files. This is
issue #46.

```
dataset_dir/
  experiment.xenium
  cells.parquet                        ← untouched by this feature
  transcripts.parquet                  ← untouched
  cell_boundaries.parquet              ← untouched
  edges.parquet                        ← optional legacy top-level file (still the default)
  edges/                               ← dedicated folder for additional edge sets
    edge.raw.minimum.parquet
    edge.normalized.product.parquet
```

Every file (top-level and in `edges/`) follows the same `edges.parquet` schema
documented below. Generate extra sets with
`sample_data/make_edges.py --out edges/<name>.parquet …`.

**Discovery** — `GET /edges/{dataset}/files` returns
`{ files: [{id, label}], default }`. It looks in exactly two places so it never
sweeps up cells/transcripts/boundary parquet: the legacy top-level `edges.parquet`
(listed first, kept as the default for backward compatibility) and every `*.parquet`
in the `edges/` subfolder. `id` is the value passed back as the `edge_file` query
param (e.g. `"edges/edge.raw.minimum.parquet"`); `label` is the display name
(folder + `.parquet` stripped).

**Backend** — every `/edges/*` endpoint already accepted an `edge_file` query param
(default `edges.parquet`); the reader cache in `edges.py` is keyed by
`(dataset, edge_file)`. `_reader()` resolves `edge_file` under the dataset directory
and rejects anything that escapes it (path-traversal guard → 400; missing file → 404).

**Frontend** — a single global `edgeFile` in the store applies to **all** open viewer
panels (see the Split-Screen note; a per-panel edge file was deliberately deferred
because LRM catalogue / color ranges are edge-file-specific and the sidebar is shared).
The picker is a `<select>` at the top of the Edge Data section in `LayerPanel.jsx`,
shown only when the dataset has >1 edge file. `setEdgeFile` and `setDataset` both
reset the edge-file-scoped state (`lrmCatalogue`, `hiddenLrms`, `selectedEdge`,
`edgeColorRange`, `edgeColorClamp`) so stale LRM/color state from the previous file
never leaks. `edgeFile` is threaded as `?edge_file=…` through all six edge fetch
sites: `useEdges` (query-grouped, query-scores), `useEdgeColors` (edge-color-values),
`EdgeSection` (schema, lrm-catalogue), `EdgeCategoricalLegend` (edge-color-values),
and `EdgeInfoPanel` (edge detail).

---

## State Management (store.js)

All shared state lives in a single Zustand store. Key sections:

- **Dataset / image**: `dataset` (null on init, auto-set from `/spatial/datasets`),
  `activeImage` (which OME-TIFF to show; auto-set from `/spatial/{dataset}/images`)
- **Edge file**: `edgeFile` (default `"edges.parquet"`) — which edge-source parquet to
  render; global (applies to all panels). `setEdgeFile` / `setDataset` reset the
  edge-file-scoped state. See "Multiple Edge Files" above.
- **Layer visibility**: `layers` object — each layer has `visible` + `opacity`;
  `cellSegments` also has `outlineOpacity` (independent from fill opacity)
- **Cell color**: `cellColorEnabled`, `colorBy` (`mode`: off/gene_set/metadata, `field`),
  `cellColorPalette`, `cellColorClamp` (squish/oob cutoffs)
- **Transcript gene filter**: `selectedGenes` — `null` = no filter (show all);
  `Set<string>` = allowlist (show only those genes). Dataset-scoped; resets on
  dataset change. See Gene Filter section below.
- **Edge density**: `edgeDensity` — fraction of available viewport edges to render
  (0.01–1.0, default **0.1**). Applies to both the tissue graph layer and the directed
  edges layer. Slider is top-level in LayerPanel, between the two sections.
- **Edge style**: `edgeWidth`, `showArrowheads`, `arrowStyle` (full/half-harpoon),
  `arrowheadScale`, `edgeDirectional`, `edgeOffset` (perpendicular separation), `showAutocrine`
- **Edge color**: `edgeColorBy` (`mode`: default/lrm_set/metadata), `edgeColorPalette`,
  `edgeColorClamp`
- **LRM filter**: `hiddenLrms` (Set of "ligand|receptor" strings), `lrmCatalogue`
- **Selection**: `selectedCell`, `selectedEdge`
- **Annotations**: `regions`, `measurements`, `activeRegion`, `annotationMode`
- **Split-screen**: `panelCount` (1 or 2), `viewports` (array of two viewport objects,
  one per panel — `{xmin,ymin,xmax,ymax}` in image pixels), `pendingZoomMatch`
  (`null` or `{ fromPanel }` — consumed by the target panel to match zoom while
  keeping its own center). `requestZoomMatch(fromPanel)` / `clearZoomMatch()` are the
  corresponding actions.

---

## Dataset & Image Auto-Initialization

`dataset` starts as `null`. `LayerPanel.jsx::DatasetPicker` fetches
`/spatial/datasets` on mount and calls `setDataset(list[0])` if the current dataset
is null or no longer in the list. Similarly, `activeImage` is auto-set from
`/spatial/{dataset}/images` (OME-TIFFs in the dataset folder, morphology-first).

`Viewer.jsx` renders a "Loading datasets…" placeholder while `dataset === null` so
no hooks fire against a null dataset. All data hooks guard against non-ok HTTP
responses — each returns an empty array on 404/500 so a missing file never causes
a render crash.

---

## Transcript Gene Filter (selectedGenes)

The gene filter uses an **allowlist** model, not a denylist:

- `selectedGenes = null` — no filter; all transcripts are shown
- `selectedGenes = Set{...}` — only transcripts whose `feature_name` is in the set are rendered

The selection is built from `allGenes` (fetched once per dataset from
`/spatial/{dataset}/genes`), so it is stable across pan/zoom. The UI in
`LayerPanel.jsx::TranscriptSpeciesSection`:

- **Collapsed / no filter**: shows `all N genes` with a `select ▼` button
- **Collapsed / filter active**: shows `M / N genes selected`, a compact list of
  selected genes (each with a ✕ remove button), a `clear` button, and an `edit ▼` button
- **Expanded picker**: full gene list (searchable) with checkboxes, `all` (→ null)
  and `none` (→ empty Set) buttons

`toggleSelectedGene(gene)`: if `selectedGenes` is null, starts a new Set with just
that gene. If it's a Set, toggles membership. Opening the picker while null shows all
genes as checked; unchecking one starts an allowlist.

`useCellColors` `gene_set` mode: if `selectedGenes === null`, uses all `allGenes`;
otherwise uses `[...selectedGenes]`.

---

## deck.gl Layers (Viewer.jsx)

Layers rendered in order (bottom to top):

1. `cell-segments-fill` — SolidPolygonLayer, cell fill colors
2. `cell-segments-outline` — PathLayer, cell boundaries
3. `transcripts` — ScatterplotLayer, transcript dots
4. `tissue-graph` — LineLayer, ALL unique undirected cell pairs (structural background, LRM-agnostic)
5. `edges-directed` — LineLayer, directed edges (LRM-filtered, colored)
6. `edges-arrowheads` — SolidPolygonLayer, filled arrowhead triangles (full or harpoon style)
7. `edges-autocrine` — ScatterplotLayer (stroked only), autocrine rings
8. Annotation layers (region fills, outlines, measurement lines)

**Tissue graph vs Edge data**: Tissue graph = binary structural layer (which cells are connected
at all, regardless of LRM). Edge data = quantitative/categorical overlay on top. Analogous to
cell segment outlines (structure) vs cell fill color (expression).

**Two LRM count fields** in `query_grouped` response:
- `lrm_count` — total LRM rows for this edge (used by tissue graph — show all structural pairs regardless of LRM filter)
- `visible_lrm_count` — LRMs not in `hiddenLrms` (used by directed edges — hide edge when 0)
- `visible_score_sum` — SUM(score) for non-excluded LRMs (used for client-side lrm_set color mapping)

**Directional rendering**: A→B and B→A are offset perpendicular to the edge axis so they
appear as two distinct parallel lines. Offset amount is tunable (`edgeOffset`, default 4px).
Both are offset to their own LEFT, so harpoon arrowheads on the outer side naturally form
the chemistry ⇌ notation.

**Picking**: OSD consumes pointer events. After each click, `deck.pickObject()` is called
manually at the click coordinates. Normal click: cell fill checked first, then edge layers.
Shift+click: edge layers checked first (useful when edges and cells overlap). The
`tissue-graph` layer is also pickable (selecting it opens the EdgeInfoPanel).
Results set `selectedCell` or `selectedEdge` in the store.

---

## Split-Screen Architecture

`Viewer.jsx` exports two components:
- **`ViewerPanel({ panelIndex })`** — contains all viewer logic: its own OSD instance,
  deck.gl canvas, data hook calls, click handlers, annotation overlay, and toolbar.
  Reads `viewports[panelIndex]` from the store for its own viewport-bounded fetches.
- **`Viewer`** (default export) — thin wrapper; renders `<ViewerPanel panelIndex={0} />`
  always, plus `<ViewerPanel panelIndex={1} />` when `panelCount >= 2`.

**What is per-panel (local state / per-instance):**
- OSD viewer instance (`viewerRef`)
- deck.gl ref (`deckRef`)
- deck.gl view state (`deckViewState`)
- Per-panel viewport in store (`viewports[panelIndex]`)
- `osdOpenCount` — local counter incremented on each OSD `open` event; used as dep
  for the morphology opacity effect to ensure it fires regardless of whether
  `imageSize.w` changed (fixes the bug where morphology stayed visible after
  dataset switches with same-dimension images, and in panel 2 on first open)

**What is shared (global store):**
- All layer toggles, opacities, color-by settings, LRM filter, edge density, etc.
- `edgeFile` — the selected edge-source parquet applies to both panels. A per-panel
  edge file was deferred (issue #46 discussion): LRM catalogue + color ranges are
  edge-file-specific, and the single sidebar can't drive two different edge sets
  equally. Revisit if side-by-side comparison of different edge files is needed.
- `selectedCell`, `selectedEdge` (global — EdgeInfoPanel only renders in panel 0)
- `imageSize` (both panels open the same DZI; panel 0 sets it, panel 1 may also set
  the same values redundantly — harmless)

**Guarded to panel 0 only** (to avoid double-writes):
- Platform info fetch (`/spatial/{dataset}/info`)
- `setCellColorRange`, `setEdgeColorRange`, `setEdgeColorClamp` updates
- EdgeInfoPanel rendering

**⇔ Match zoom flow:**
`requestZoomMatch(fromPanel)` → both panels' effects fire → source panel early-returns
(`fromPanel === panelIndex`) → target panel reads `viewports[fromPanel]` via
`useStore.getState()`, computes OSD-normalised width/height, gets its own current center
via `viewport.getCenter(true)`, constructs new bounds at same size centered on its own
center, calls `viewport.fitBounds(newBounds, false)` (animated), then `clearZoomMatch()`.

---

## Viewport-Bounded Data Fetching

All data hooks (transcripts, cell boundaries, edges) are debounced and skip fetches
that would be wasted at the current zoom level:

| Hook | Skip condition | Bbox filter | Limit |
|---|---|---|---|
| `useTranscripts` | `fracW >= 0.7` | Always sent when viewport available | 50K (random sample) |
| `useCellBoundaries` | `fracW >= 0.5` | Always sent | 20K cells |
| `useEdges` | no viewport | Always sent | 10K–50K edges (grouped) |

`fracW = (xmax - xmin) / imageSize.w` — fraction of image width visible.

**Transcript sampling**: the backend uses `df.sample(n=limit)` (random, not `head`)
so the 50K returned transcripts are spatially uniform across the viewport rather than
biased toward whatever region appears first in the parquet row order.

**Edge aggregation**: `useEdges` POSTs to `/edges/{dataset}/query-grouped` which returns
one row per directed edge (GROUP BY edge, ORDER BY RANDOM()). For a 168M-row parquet
(~300K edges × 559 LRMs) this is ~500× fewer rows than the raw query. The `excluded_lrms`
list is sent in the request body so `visible_lrm_count` and `visible_score_sum` are
pre-computed server-side.

---

## Color System

`valueToColor(value, vmin, vmax, palette)` in `colormap.js` maps a scalar to RGBA.
`interpolateStops` clamps t to [0,1], so passing a tighter [lo, hi] window achieves
`oob::squish` behavior — values outside the window get the palette endpoints.

The `cellColorClamp` / `edgeColorClamp` store values are passed into the color hooks
and applied as `lo = clamp.low ?? dataMin`, `hi = clamp.high ?? dataMax`.

**Edge lrm_set coloring is fully client-side**: `useEdgeColors` computes colors
synchronously from `visible_score_sum` in the already-fetched edges array. No server
call is made for `lrm_set` mode. The p95 of `visible_score_sum` across the current
viewport is auto-set as `edgeColorClamp.high` so the color range adapts to the data
rather than being dominated by outlier edges.

Categorical data uses `QUAL_PALETTE` (20 visually distinct colors) from `colormap.js`.
Beyond 20 categories, `geneColor()` provides deterministic hash-based colors.

---

## Tile Pyramid

The backend uses pyvips when available (fast streaming, handles very large OME-TIFFs
without loading the full image into RAM) with a tifffile+Pillow fallback for
environments without libvips. Key details:

- Pyramids are built on first DZI request (auto-triggered by `tiles.py::dzi_descriptor`)
  and cached in `CACHE_DIR` (Docker volume `dzi_cache`, or alongside data in dev).
  The build is idempotent.
- **pyvips path**: detects availability with `pyvips.version(0)` (catches `OSError`
  when the C library is missing — `except ImportError` is not sufficient). Builds a
  lazy MIP pipeline across all Z-planes using `ifthenelse` chains; no full image
  in RAM. Calls `img.dzsave(...)` to stream tiles.
- **tifffile fallback**: reads one OME level at a time, skips levels too large for
  available RAM (guard: `MAX_TIFFFILE_DIM = 16384`), computes normalisation stats
  from the smallest available level.
- OME-TIFFs from Xenium use JPEG2000 compression — requires `imagecodecs` pip package.

---

## Development Workflow

**Local dev (no Docker):**
```bash
# Backend
cd backend
pip install -r requirements.txt
DATA_ROOT=../sample_data uvicorn app.main:app --reload

# Frontend (separate terminal)
cd frontend
npm install
npm run dev   # → http://localhost:5173, proxies /api → :8000
```

Note: dev server runs on port **5173** (not 3000) to avoid conflicting with Docker,
which binds port 3000. This is configured in `.claude/launch.json`.

**Docker — demo data (sample_data/):**
```bash
docker compose up --build   # first time or after code changes
docker compose up           # subsequent runs
docker compose down
```

**Docker — external data directory:**
```bash
DATA_PATH="/absolute/path/to/datasets" docker compose up --build
```
`DATA_PATH` must be an absolute host path with no colons. Drop any supported platform
output folder under `DATA_PATH` — TissuePlex auto-detects the platform on first access.

---

## Known Issues / Gotchas

- `pyvips==2.2.3` is incompatible with `cffi>=2.0` — pinned as `cffi<2.0` in requirements.txt
- `imagecodecs` is required for JPEG2000 OME-TIFFs (Xenium standard format)
- `pyvips` raises `OSError` (not `ImportError`) when the libvips C library is missing;
  catch `Exception` broadly or test with `pyvips.version(0)`
- The `/{edge_id:path}` FastAPI route converter is required to handle `|` in edge IDs
- `is_autocrine` from pandas parquet is `numpy.bool_` — must cast to `bool()` before JSON serialization
- OSD and deck.gl use different coordinate systems; the `syncDeckFromOSD` function in
  Viewer.jsx is the critical bridge — do not break it
- Docker volume specs use `:` as separator; host paths containing `:` (e.g. network
  mount paths on macOS) will cause `invalid volume specification` errors
- All data hooks must guard against non-ok HTTP responses (return `[]` on error);
  storing a `{"detail": "..."}` error object as the edges/transcripts/cells array
  causes deck.gl to throw "not iterable" errors in minified code
- `query_grouped` response rows must be sanitized (NaN/inf → None) before JSON
  serialization — `visible_score_sum` can be NaN when score column contains NaN values
- Reader instances are cached at router level (`_reader_cache` dicts in `edges.py` and
  `spatial.py`) so instance-level caches (`_cells_full_cache`, `_schema_cache`,
  `_lrm_catalogue_cache`) survive across requests. The LRM catalogue scan (1s on 168M rows)
  is cached per `EdgeReader` instance in `_lrm_catalogue_cache`.
- DuckDB binds `?` parameters in SQL text order, not logical clause order. In
  `query_grouped`, the SELECT CASE WHEN clauses appear before the WHERE clause, so
  `excl_params` must come before `where_params` in `all_params`.
- Real parquet files can have completely null LRM rows (lrm=null, ligand=null, receptor=null).
  The catalogue query filters these with `WHERE lrm IS NOT NULL`; the endpoint strips
  null entries from `excluded_lrms` with `[x for x in lst if x is not None]`; the
  Pydantic model uses `List[Optional[str]]` to accept them without 422 errors.
- **Morphology layer always-visible bug (fixed)**: The morphology opacity effect used
  `imageSize.w` as a dep to detect OSD open, but this fails when the new image has the
  same dimensions as the previous one (dep doesn't change → effect doesn't re-run →
  layer stays at OSD default full opacity). Fixed with `osdOpenCount` — a local
  `useState` counter incremented on every OSD `open` event, used as the effect dep
  instead. Also fixes panel 2 in split mode, where `imageSize.w` was already set by
  panel 0 before panel 2's OSD opened.
- **`Math.min/max` spread on large arrays** (fixed in `useEdgeColors.js`): spreading
  100K+ element arrays causes `RangeError: Maximum call stack size exceeded`. Use a
  `for` loop to find min/max instead of `Math.min(...arr)`.

---

## What's Not Built Yet

1. **R export function** — `export_for_TissuePlex()` is implemented in the NICHESv2 R
   package (separate repo). The draft in `r/export_NICHESObject_for_viewer.R` is
   superseded. See `docs/data_format.md` for the column spec.

2. **Cell expression bar chart** — click panel currently shows cell metadata but not a sorted
   gene expression readout. The `/spatial/{dataset}/expression/{cell_id}` endpoint exists
   but the UI component is not built.

3. **MERSCOPE cell boundaries** — MERSCOPE stores boundaries as HDF5 polygon data;
   `MerscopeReader.cell_boundaries()` is a stub returning `[]`.

4. **CosMx gene-set coloring** — requires per-cell expression aggregation from the
   transcript file; `CosMxReader._color_values_gene_set()` is a stub returning empty.

5. **Performance at scale** — edge rendering is now fast (query-grouped returns ~300K
   edges as 300K rows instead of 168M rows; colors computed client-side). Remaining
   bottlenecks: LOD for arrowheads at low zoom, transcript rendering at very high density.

6. **Authentication** — no auth. Fine for local/lab use, needs work for any public deployment.
