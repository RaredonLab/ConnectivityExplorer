# TissuePlex — Claude Code Project Brief

This file is read automatically by Claude Code at the start of every session.
It provides full context on the architecture, design decisions, and current state
of the project so any Claude instance can contribute immediately.

---

## What This Is

A web-based spatial transcriptomics viewer supporting multiple platforms (Xenium,
seqFISH, Visium HD, MERSCOPE, CosMx) with connectivity layers produced by the lab's NICHESv2 R pipeline.
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

**Detection order** (first match wins — see `reader_factory.py::_DETECTORS`):
| Platform | Sentinel file |
|---|---|
| Xenium (10x Genomics) | `experiment.xenium` |
| Visium HD (10x Genomics) | `binned_outputs/square_*um/` (or a top-level `square_???um/`) |
| Visium classic (10x Genomics) | `spatial/scalefactors_json.json` + a `tissue_positions*` file |
| MERSCOPE (Vizgen) | `cell_by_gene.csv` or `cell_metadata.csv` |
| CosMx (Nanostring) | `*_tx_file.csv` |
| seqFISH (Spatial Genomics) | `*_CellCoordinates*.csv` — a glob, so registered **last** |

**Coordinate contract**: Every reader converts native coordinates to image pixel space
before returning data. The frontend always receives pixel coordinates.

**Capability flags**: `capabilities()` on the base class returns
`{has_morphology, has_transcripts, has_boundaries, unit_label}`. The frontend reads these
from `/spatial/{dataset}/info` and hides layers a platform cannot serve. Readers override
it to declare what they lack — this is how spot-based platforms suppress the transcript
and boundary layers rather than returning empty arrays for them.

**Implementation status:**
- Xenium: fully implemented
- Visium HD: implemented against a real Space Ranger 4.0.1 `outs/` tree — bins, bin
  outlines, expression, and both color-value modes. `has_transcripts: False` (bin-level
  UMI counts only, no molecule coordinates); `unit_label: "bin"`. See its own section.
- Visium classic (v1/v2): fully implemented — spots, spot outlines as circles,
  expression, and both color-value modes. `has_transcripts: False`;
  `unit_label: "spot"`. Shares `spaceranger.py` with Visium HD. Verified against real
  Space Ranger output (`V1_Mouse_Kidney`, `V1_Adult_Mouse_Brain`), including a
  registration check: 98.1% of `in_tissue` spots land on stained tissue and 99.5% of
  out-of-tissue spots land on bare slide. See its own section.
- MERSCOPE: cells, transcripts, genes, color-values (metadata + gene-set) implemented;
  `cell_boundaries()` returns empty and `has_boundaries: False` (HDF5 polygon format
  not yet parsed)
- CosMx: cells, transcripts, genes, metadata color-values implemented;
  gene-set color-values stub (requires transcript aggregation per cell);
  `has_boundaries: False` (boundaries are per-FOV label TIFFs)
- seqFISH (Spatial Genomics GenePS): fully implemented for the current **v2** layout —
  cells, transcripts, boundaries, expression, and both color-value modes. Legacy **v1**
  reads cells and transcripts but declares `has_boundaries: False`, because v1 ships only
  a label mask and polygonising it was deliberately deferred rather than adding a
  dependency. See the seqFISH section below — its coordinate handling is unlike any other
  reader and is the thing to understand before touching it.

**Interface note**: every reader now matches the base signature (`fraction=`, dict return).
`base_reader.py`'s docstring for `cell_boundaries` still says `-> list[dict]` while all
implementations return the dict form — the docstring is the thing that is wrong.

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
      seqfish_reader.py      seqFISH / Spatial Genomics GenePS; v2 full, v1 partial.
                             Mixed µm/pixel coordinate handling — see its own section.
      duck.py                Shared DuckDB query helpers used by the spatial readers
      metadata_filter.py     Categorical-vs-continuous typing + the MetadataFilter
                             subsetting spec; shared by cells and edges (#35, #45)
      spatial_cache.py       Spatially-sorted parquet cache (build on first access)
      spaceranger.py         Shared base for the two 10x array platforms — scalefactors,
                             tissue_positions, the feature-matrix h5, and the hires
                             coordinate scaling. Read before touching either reader.
      visium_reader.py       Visium classic — spots as circle polygons; see its own section
      visium_hd_reader.py    Visium HD — bins as square polygons; see its own section
      merscope_reader.py     MERSCOPE implementation (inherits SpatialDatasetReader)
      cosmx_reader.py        CosMx implementation (inherits SpatialDatasetReader)
      edge_reader.py         reads edges.parquet; query_grouped(), query_scores(), lrm_catalogue(), edge_color_values(), edge_detail();
                             also loads edge-metadata/ supplemental annotations
      supplemental.py        Shared cell-metadata/ + edge-metadata/ loader (key-agnostic)
      layer_reader.py        generic parquet reader
    tiling/
      pyramid.py             OME-TIFF → DZI; pyvips streaming primary, tifffile+Pillow fallback
  requirements.txt           pinned deps; cffi<2.0 required for pyvips 2.2.3 compatibility
  Dockerfile
  tests/
    golden_snapshot.py       Reader regression guard — see Development Workflow
    golden_baseline.json     Recorded baseline (238 probes / 8 datasets)

frontend/
  src/
    App.jsx                  Root component; React ErrorBoundary + top-level layout.
                             NOTE: sibling of components/, not inside it.
    store.js                 Zustand store — ALL shared state lives here
    components/
      Viewer.jsx             Split-screen wrapper (Viewer) + per-panel logic (ViewerPanel)
      LayerPanel.jsx         Right-side panel: toggles, opacity, color-by, legends,
                             dataset/image picker, transcript species filter
      CellInfoPanel.jsx      Floating panel on cell click; shows color-by value highlight
      EdgeInfoPanel.jsx      Floating panel on edge/autocrine click
      AnnotationToolbar.jsx  Region drawing + measurement tools; ⊞ Split / □ Single toggle;
                             ⇔ Match zoom; per-panel rotation (⟲ / angle / ⟳)
      RenderingStatus.jsx    Per-panel loading badge, driven by the store's loadingKeys set
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
docker-compose.prod.yml      Production stack used by the cloud deployment
docker/docker-compose.yml    Legacy path (kept for compatibility)
Caddyfile                    Reverse proxy + TLS for the cloud deployment; optional basicauth
deploy.sh                    One-shot droplet bootstrap (see docs/cloud-deploy.md)
upload-data.sh               rsync datasets to a deployed server
sample_data/                 Partially gitignored — default data mount for local dev/demo.
                             mouse_ileum_tiny and seqfish_synthetic are tracked; larger
                             and licence-restricted datasets are ignored.
  make_edges.py              Synthetic edges.parquet generator
  make_seqfish.py            Synthetic seqFISH v2 ROI generator (committable fixture)
  make_visium.py             Synthetic classic Visium generator (committable fixture);
                             non-identity tissue_hires_scalef on purpose — see below
  make_edge_metadata.py      Demo edge-metadata/ for any dataset with edges. Three
                             columns derived from the edge file, one clearly-named
                             invented flag, plus a README in each folder saying which
                             is which. Nothing here is analysis output.
r/                           NICHESv2 → edges.parquet. See r/README.md.
  niches_xenium.R            Xenium — coordinates already µm; read this one first
  niches_seqfish.R           seqFISH — dense CSV counts, per-version coordinate units
  niches_visium_hd.R         Visium HD — pixel coordinates, must convert to µm
  niches_visium.R            Visium classic — same, but µm/px must be derived from the
                             55 µm spot spec; checks it against the 100 µm pitch
  niches_merscope.R          MERSCOPE — already µm; EntityID must be read as character
  niches_cosmx.R             CosMx — (fov, cell_ID) identity; shift to the reader's
                             origin, then px → µm
  niches_common.R            shared helpers (10x h5 reader, LR-coverage check, validation)
  *_PPLR.R                   older personal pipeline with hardcoded paths; reference only
docs/
  data_format.md             edges.parquet column spec for NICHESv2 R export
  setup.md                   Docker deployment guide (lab-facing)
  cloud-deploy.md            DigitalOcean deployment runbook (~$106–116/mo)
  public_datasets.md         Public datasets used for development, and the classic
                             Visium pair the reader was verified against
  split_screen_phase2.md     Spec for making panel *settings* per-panel. Phase 1
                             (per-panel datasets) shipped in v0.8.4; this is what
                             remains. Read before touching store.js's shared state.
  index.html                 The user manual, published to GitHub Pages at
                             https://raredonlab.github.io/TissuePlex/ — hand-written
                             HTML, no build step. Update it when a UI control changes.
  demo.gif                   README demo animation
OBS/                         Archived, superseded planning docs. Provenance only —
                             NOT a specification. See OBS/README.md.
NICHESv2_package_design.md   Design doc for the separate NICHESv2 R package (not this repo)
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
Real data comes from `export_to_TissuePlex()` in the NICHESv2 R package.

---

## Supplemental Cell Metadata

User-defined metadata (e.g. from external R analysis) can be loaded without modifying
the dataset output by placing files in a `cell-metadata/` subdirectory of the dataset.
The loader lives on `SpatialDatasetReader`, so it is available to every platform; a reader
opts in by calling `_merge_supplemental()` on its cells table (Xenium and seqFISH do).
All a platform contributes is `_ROOT_CSV_SKIP` / `_ROOT_CSV_SKIP_SUFFIXES` — the list of
its *own* root CSVs, so the loader never ingests platform output as user metadata. CosMx
needs the suffix form because it prefixes every file with the experiment name.

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

`_cells_full()` is cached per reader instance (one Docker request lifecycle).
`_load_supplemental_metadata()` is also cached on the base class, so the CSV is parsed once
regardless of how many color-by requests arrive.

---

## Supplemental Edge Metadata (edge-metadata/ folder)

The edge-side mirror of `cell-metadata/`, sharing its loader (`readers/supplemental.py`)
so the two cannot drift. Lets a user annotate cell pairs — a call, a confidence, a review
flag — without regenerating `edges.parquet` from R.

```
dataset_dir/
  edges.parquet
  edge-metadata/            ← create this directory
    annotations.csv         ← key column `edge` = "SendingCell|ReceivingCell"
    curation.parquet
```

Same rules as cell metadata: `.csv` / `.csv.gz` / `.parquet`, multiple files outer-joined,
and the key column resolved as an explicit `edge` column → `Unnamed: 0` (R's unnamed
rowname column) → the first column if it holds unique strings. So the R default works:

```r
write.csv(annotations_df, file.path(dataset_dir, "edge-metadata", "annotations.csv"))
```

**The folder sits beside the dataset, not beside the edge file.** `EdgeReader._dataset_dir`
walks up out of `edges/` when the edge file is nested, so one set of annotations applies
across every edge source in the dataset. Annotations describe cell pairs, which are a
property of the tissue rather than of one scoring run.

Three integration points, all in `edge_reader.py`:

- `schema()` merges supplemental columns into the returned column map. That is the *only*
  thing needed for them to appear in the edge color-by dropdown — `LayerPanel` builds that
  list straight from the schema, so no frontend change was required.
- `edge_color_values("metadata", field=…)` checks the parquet first, then the supplemental
  frame. Supplemental data is already one row per edge, so it skips the `GROUP BY`.
- `edge_detail()` attaches matches under a `metadata` key, which `EdgeInfoPanel` renders
  generically as an "Annotations" block above the LRM table.

Parquet wins a name collision (`schema()` uses `setdefault`): it is the authoritative
source, and a supplemental column silently shadowing a real one would be painful to debug.

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

**Frontend** — `edgeFile` is **per panel** (`panels[i].edgeFile`), so two panels can
compare two edge sources. It was global until v0.8.4; the deferral recorded here
(LRM catalogue and colour ranges are edge-file-specific, and one sidebar cannot drive
two of them) was resolved by Phase 1 of the split-screen work, which moved every
dataset-bound value into `panels[i]`. In split mode the picker sits in each panel's
header via `DatasetPicker`; in single-panel mode it stays in the Edge Data section of
`LayerPanel.jsx`. Either way it is shown only when the dataset has >1 edge file.
`setPanelEdgeFile` and `setPanelDataset` both reset the edge-file-scoped state (`lrmCatalogue`, `hiddenLrms`, `selectedEdge`,
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
- **Edge file**: `panels[i].edgeFile` (default `"edges.parquet"`) — which edge-source
  parquet that panel renders. Per-panel since v0.8.4. `setPanelEdgeFile` /
  `setPanelDataset` reset the edge-file-scoped state. See "Multiple Edge Files" above.
- **Layer visibility**: `layers` object — each layer has `visible` + `opacity`;
  `cellSegments` also has `outlineOpacity` (independent from fill opacity)
- **Cell color**: `cellColorEnabled`, `colorBy` (`mode`: off/gene_set/metadata, `field`),
  `cellColorPalette`, `cellColorClamp` (squish/oob cutoffs). `cellColorType` /
  `cellColorCategories` hold the type the backend actually returned, written by
  panel 0 — the LayerPanel reads these instead of guessing from the schema dtype.
- **Categorical override**: `categoricalOverrides`, keyed `cell::<field>` /
  `edge::<field>` → `true | false`; absent means auto-detect (issue #35).
- **Metadata filter**: `cellFilter` / `edgeFilter`, each
  `{ field, values, min, max, includeMissing }` or null (issue #45). `cellFilter`
  also governs edges — both endpoints must survive it. Both reset on dataset change
  (column names are dataset-specific); `edgeFilter` also resets on edge-file change.
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
- **Sampling**: `transcriptFraction` (default 0.1) and `cellBoundaryFraction`
  (`null` = auto) control how much of the viewport each hook requests;
  `transcriptStats` / `cellBoundaryStats` hold live `{shown, total}` counts that the
  LayerPanel displays. Both stats are written by panel 0 only.
- **Color overrides**: `categoryColorOverrides` (keyed `${field}::${category}`) and
  `transcriptColorOverrides` (keyed by gene name) hold user-picked swatch colors.
  Both reset on dataset change. `merge*` actions exist for bulk CSV import.
- **Loading**: `loadingKeys` — a Set of in-flight keys, one per panel
  (`panel-0`, `panel-1`). `RenderingStatus.jsx` shows a badge whenever it is non-empty.
  Each ViewerPanel ORs together every hook's `loading` flag into its own key.
- **Split-screen**: `panelCount` (1 or 2), `viewports` (array of two viewport objects,
  one per panel — `{xmin,ymin,xmax,ymax}` in image pixels), `pendingZoomMatch`
  (`null` or `{ fromPanel }` — consumed by the target panel to match zoom while
  keeping its own center). `requestZoomMatch(fromPanel)` / `clearZoomMatch()` are the
  corresponding actions.
- **Rotation**: `panelRotations` — `[deg, deg]`, one per panel, normalized to 0–359 by
  `setPanelRotation`. See the Rotation section below.
- **`viewportActual`**: the *un-expanded* OSD bounds per panel. Distinct from `viewports`,
  which is padded when a panel is rotated. Only ⇔ Match zoom reads it, so that matching
  uses the true visible width rather than the rotation-padded fetch bbox.

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

`toggleSelectedGene(gene)` **must agree with what the checkbox is showing**, and this
is the one thing to get right here. The picker renders `checked = selectedGenes === null
|| selectedGenes.has(gene)`, so in the null state *every box is ticked*. A click therefore
means **uncheck this one** — the action builds the allowlist "everything except this gene"
from the union of `panels[*].allGenes`. Toggling the last unchecked gene back on collapses
the Set to `null`, so "no filter" stays single-valued and hundreds of gene names stay out
of the request URL.

It used to start an allowlist containing *only* the clicked gene — the exact inverse. The
symptom did not look like a filter bug: on a 480-gene Xenium panel one click took the
transcript layer from 200,000 dots to ~360, which reads as "transcripts stopped working".
Present since v0.2.0 (`c52dbc0`).

**The filter is sent as an allowlist or as its complement, whichever is shorter**
(`genes=` vs `exclude_genes=`). This is not an optimisation — it is what keeps the
feature working at all. The list travels in the query string of a GET, and measured
against this stack, 479 of 480 genes is a **7,780-byte URL, ~220 bytes under nginx's
8 KB request-line limit**, while 600 genes returned **414**. Deselecting a handful of
genes from a large panel is the ordinary case and produces exactly that shape, so a
Xenium Prime 5K run would have failed on the first click. `exclude_genes` is resolved
back to an allowlist in `spatial.py` against `reader.gene_list()`, so no reader sees
the inverted form. `nginx.conf` also raises `large_client_header_buffers` to 64k, which
covers the worst case the complement rule can still produce (half a panel).

Note the two forms are equivalent to each other but **not** to sending no filter at all:
`gene_list()` omits controls and blanks, so any explicit selection drops them while
"no filter" keeps them (measured: 35,013 vs 34,997 rows in one viewport). That predates
this change and is why the picker never lists control probes.

An **empty** `selectedGenes` Set short-circuits the fetch. It means "show no species",
but omitting the gene parameter means "no filter" to the backend, so the request used to
return the full 200K-row cap for Viewer's client-side filter to discard — nothing drew,
which looked right, while ~20 MB was fetched per pan and the layer badge reported the
unfiltered total against an empty canvas.

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
8. Annotation layers (region fills, outlines, active region + vertices, measurement
   lines, endpoints, first-point marker)

Every layer receives `modelMatrix: rotModelMatrix` so rotation applies uniformly.

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

**Two panels can now show two different datasets.** Everything bound to *which
dataset a panel shows* lives in `panels[panelIndex]` (store.js `makePanel()`):
`dataset`, `activeImage`, `imageSize`, `platformCapabilities`, `pixelSize`,
`edgeFile`, `lrmCatalogue`, `allGenes`, the colour ranges, and the shown/total
stats. Each of those differs between datasets, so none of them can be global.

Style and choice settings — layer opacity, palettes, colour-by, filters, LRM
selection, edge geometry — deliberately stay **shared**: one sidebar drives both
panels, which is what makes a side-by-side comparison comparable. Making those
per-panel, with a link toggle and a push-to-other-panel button, is Phase 2 —
specified in `docs/split_screen_phase2.md`, not yet started. The sidebar
reconciles across panels with `hooks/usePanels.js`, whose rule is **union, then
degrade per panel**: offer a control if *either* panel can use it, and let the
panel that cannot render nothing. Intersecting instead would hide controls that
work perfectly well on one side, which is worse when the point is comparing
unlike things. `unit_label` becomes the neutral "unit" when the panels disagree.

Consequences worth knowing:

- **The dataset / image / edge-source pickers move into each panel's header** in
  split mode, because an image name or edge file only means something relative to
  one dataset. In single-panel mode they stay in the sidebar, unchanged.
- **Changing either panel's dataset resets the shared column-, gene- and
  mechanism-named settings** (filters, colour-by field, gene allowlist, hidden
  LRMs). It has to: a filter naming a column the new dataset lacks 400s on every
  viewport change. The cost is that switching one panel clears the other's
  filter. That goes away when these become per-panel.
- **Selection carries its panel index** (`selection = {panelIndex, kind, …}`), so
  `CellInfoPanel` / `EdgeInfoPanel` and region export resolve against the dataset
  that was actually clicked. `EdgeInfoPanel` used to be pinned to panel 0.
- **⇔ Match zoom matches physical scale, not fraction of image.** It used to
  divide both viewports by the local image width, i.e. match "the same proportion
  of the picture" — identical behaviour when both panels showed one dataset, and
  meaningless across two. 20% of a 6.5 mm Visium capture area and 20% of a 55 µm
  seqFISH ROI differ by 55×. It now converts through each panel's own `pixelSize`
  so the same number of microns spans the same screen width, exactly as a
  scalebar would. Verified across Visium↔seqFISH: 2997 µm vs 55 µm → 55 µm both.
- **`linkColorScale` (default on) shares one colour range across panels.** This
  is figure integrity, not preference: two viridis panels that each auto-ranged
  to their own data look comparable and are not — one's yellow might be 40 counts
  and the other's 4,000. An explicit clamp from the sliders always wins.

**What is per-panel (local state / per-instance):**
- OSD viewer instance (`viewerRef`)
- deck.gl ref (`deckRef`)
- deck.gl view state (`deckViewState`)
- Per-panel viewport in store (`viewports[panelIndex]`, `viewportActual[panelIndex]`)
- Rotation angle (`panelRotations[panelIndex]`) and the derived `rotModelMatrix`
- `osdOpenCount` — local counter incremented on each OSD `open` event; used as dep
  for the morphology opacity effect to ensure it fires regardless of whether
  `imageSize.w` changed (fixes the bug where morphology stayed visible after
  dataset switches with same-dimension images, and in panel 2 on first open)

**Annotations belong to the panel that drew them.** `regions` and `measurements`
each carry a `panelIndex`, and `ViewerPanel` renders only its own. This is not
cosmetic: coordinates are image pixels of *that panel's* dataset, so a polygon
over a 6.5 mm Visium capture area reappearing in a 55 µm seqFISH panel lands
nowhere meaningful. Two consequences were worse because they were silent — CSV
export resolves `selectedCellIds` against `panels[r.panelIndex].dataset` (it read
that field before anything wrote it, so every export used panel 0), and a
measurement label is `distPx * pixelSize` for its own panel (a 100 px line reads
100 µm on CosMx and 10.8 µm on MERSCOPE). `clearAnnotations(panelIndex)` is
likewise scoped, since the button lives in each panel's own toolbar; omitting the
index still clears everything. Anything created before this carries no
`panelIndex` and is treated as panel 0.

**Display settings live in `panels[i].settings`, not at the store root** (Phase 2a).
`makeSettings()` builds them — a factory, not a constant, because `layers` and
`hiddenLrms` are containers and sharing one object across panels would alias them.
Reads go through `usePanelSettings()` (`hooks/usePanelSettings.js`), which returns the
store merged with one panel's settings; the panel comes from `PanelIndexContext`, or is
passed explicitly by `ViewerPanel`, which already knows its index. Writes go through
`patchSettings(patch, panelIndex = null)` — a null index writes to **every** panel, which
is what keeps one sidebar driving both and makes 2a behaviour-identical to the global
state it replaced. Phase 2b adds the link toggle that lets them diverge; `patchSettings`
is the single place that decision will be made.

Note `setPanelDataset` resets only the *name-bound* settings (filters, colour-by, gene
allowlist, hidden LRMs). Geometry, palettes and layer visibility survive a dataset change
and always have — rebuilding the panel from `makePanel()` would silently wipe them.

**What is shared (global store):**
- All layer toggles, opacities, color-by settings, LRM filter, edge density, etc.
- `edgeFile` is **not** shared — it moved to `panels[i]` in Phase 1, along with the
  LRM catalogue and colour ranges that made sharing it incoherent.
- `selectedCell`, `selectedEdge` (global — EdgeInfoPanel only renders in panel 0)
- `imageSize` (both panels open the same DZI; panel 0 sets it, panel 1 may also set
  the same values redundantly — harmless)

**Guarded to panel 0 only** (to avoid double-writes):
- Platform info fetch (`/spatial/{dataset}/info`)
- `setCellColorRange`, `setEdgeColorRange`, `setEdgeColorClamp` updates
- EdgeInfoPanel rendering

---

## Per-Panel Rotation (issue #31)

Each panel can be rotated independently, via ⟲ / angle input / ⟳ in `AnnotationToolbar`.
`setPanelRotation(panelIndex, angle)` normalizes to 0–359.

Rotation has to be applied in **two** places that must stay consistent:

1. **OSD tiles** — `viewer.viewport.setRotation(panelRotation)` rotates the morphology
   image.
2. **deck.gl layers** — a column-major 4×4 `modelMatrix` from `makeRotMatrix(angle, cx, cy)`,
   pivoting around the *current viewport center*, passed to every layer.

Because the pivot is the viewport center, the matrix must be recomputed whenever the
viewport moves — which is why `syncDeckFromOSD` rebuilds it on every viewport-change event
rather than only when the angle changes.

Three consequences worth knowing before touching this:

- **Fetch bboxes are padded.** A rotated viewport rectangle covers more of the image than
  its axis-aligned bounds suggest, so `rotatedBbox()` grows the box outward (no-op at 0°
  and 180°). That padded box goes to `viewports`; the true bounds go to `viewportActual`.
  ⇔ Match zoom reads `viewportActual` so padding never inflates the matched zoom.
- **Picking and annotation clicks must inverse-rotate.** `screenToData()` projects screen →
  rotated view space, then calls `inverseRotate()` to get back to original image
  coordinates. Skip that and annotations land in the wrong place at any non-zero angle.
- **Measurement labels forward-rotate.** `forwardRotate()` maps an image-space midpoint
  into rotated view space before projecting it to a screen position for the HTML label.

The rotation effect depends on `[panelRotation, osdOpenCount]` so it re-applies after an
OSD reinitialization, not just on an angle change.

---

## Morphology Image Discovery

`GET /spatial/{dataset}/images` returns bare filename **stems** (no extension, no directory
prefix) for every `.ome.tiff` / `.ome.tif` / `.tiff` / `.tif` in the dataset root **and one
level of subdirectories**. This is what makes Xenium's multi-channel `morphology_focus/`
set selectable alongside the top-level `morphology.ome.tif`:

```
dataset_dir/
  morphology.ome.tif              → "morphology"
  morphology_focus/
    morphology_focus_0000.ome.tif → "morphology_focus_0000"
    morphology_focus_0001.ome.tif → "morphology_focus_0001"
```

`pyramid.py::_find_source()` resolves a stem back to a path by searching the **same two
locations in the same order** — root first, then subdirectories. These two functions are a
matched pair: if you change the search order or depth in one, change it in the other, or
the picker will list images the tile builder cannot open.

Hidden directories are skipped so `.dzi_cache` is never scanned. Stems are de-duplicated,
and root-level files are added first, so a root file always wins a name collision with a
subdirectory file. Names sort morphology-first, then alphabetically.

---

**⇔ Match zoom flow:**
`requestZoomMatch(fromPanel)` → both panels' effects fire → source panel early-returns
(`fromPanel === panelIndex`) → target panel reads `viewports[fromPanel]` via
`useStore.getState()`, computes OSD-normalised width/height, gets its own current center
via `viewport.getCenter(true)`, constructs new bounds at same size centered on its own
center, calls `viewport.fitBounds(newBounds, false)` (animated), then `clearZoomMatch()`.

---

## Viewport-Bounded Data Fetching

All data hooks (transcripts, cell boundaries, edges) are debounced (400 ms) and abort
in-flight requests when superseded. Rendering is no longer gated on a zoom threshold —
the old `fracW >= 0.7` / `fracW >= 0.5` skip conditions were removed so layers draw at
every zoom level including whole-tissue ("bird's-eye view", PR #28). Volume is instead
controlled by user-adjustable sampling fractions:

| Hook | Volume control | Bbox filter | Backend cap |
|---|---|---|---|
| `useTranscripts` | `transcriptFraction` (default 0.1) | Always sent | 200K rows |
| `useCellBoundaries` | `cellBoundaryFraction` (`null` = auto, targets ~5K cells) | Always sent | — |
| `useEdges` | `edgeDensity` (default 0.1) | Always sent | 500K grouped rows |

Each hook reports live `{shown, total}` counts into the store so the LayerPanel can show
what fraction of the data is actually on screen.

**Transcript sampling**: the backend uses `df.sample(n=...)` (random, not `head`) so the
returned transcripts are spatially uniform across the viewport rather than biased toward
whatever region appears first in the parquet row order. Cell boundaries sample *unique
cell IDs* before filtering rows, so a sampled cell keeps all of its vertices and never
renders as a partial polygon.

**Edge sampling** uses DuckDB `USING SAMPLE ... (bernoulli)` on the grouped result, so
each edge is included independently at probability `density` — spatially uniform, and
no sampling clause is emitted at all when `density = 1.0`.

**Edge aggregation**: `useEdges` POSTs to `/edges/{dataset}/query-grouped` which returns
one row per directed edge (GROUP BY edge, ORDER BY RANDOM()). For a 168M-row parquet
(~300K edges × 559 LRMs) this is ~500× fewer rows than the raw query. The `excluded_lrms`
list is sent in the request body so `visible_lrm_count` and `visible_score_sum` are
pre-computed server-side.

---

## Visium HD (readers/visium_hd_reader.py)

Space Ranger tiles the capture area with square bins at 2/8/16 µm. There are no
per-molecule detections — only bin-level UMI counts.

```
dataset_dir/
  binned_outputs/
    square_008um/               ← the bin directories live HERE, not at the top level
      filtered_feature_bc_matrix.h5
      spatial/
        tissue_positions.parquet    barcode, in_tissue, array_row, array_col,
                                    pxl_row_in_fullres, pxl_col_in_fullres
        scalefactors_json.json      microns_per_pixel, spot_diameter_fullres,
                                    bin_size_um, tissue_hires_scalef, …
        tissue_hires_image.png      morphology — a PNG, not a TIFF
  spatial/                      the same images again (duplicated into every bin dir too)
  segmented_outputs/            Space Ranger 4.x: real cell polygons as GeoJSON
```

Three things about this layout bite:

- **`square_*um/` is nested under `binned_outputs/`.** The detector originally globbed the
  dataset root, so no genuine Space Ranger output was ever detected.
- **`tissue_positions.parquet` and `scalefactors_json.json` are per bin.** The top-level
  `spatial/` folder holds images only.
- **Morphology is a PNG.** The tile pipeline was TIFF-only; `_SOURCE_EXTS` and
  `spatial._TIFF_EXTS` now include `.png`, and `_build_dzi_pillow()` handles it when
  libvips is unavailable (the tifffile fallback cannot open a PNG).

**Bins are served as square polygons.** A bin is literally a square of side
`spot_diameter_fullres`, so `cell_boundaries()` emits four vertices per bin instead of
declaring `has_boundaries: False`. This matters because **nothing renders `cells()`
centroids** — the boundary layers are the only path to drawing a unit — so a points-only
Visium HD reader would show an empty canvas. Emitting squares makes fill, outline,
colour-by, picking and region selection all work through the existing layers with no
frontend change at all.

**Coordinates.** See the shared section below — `pxl_col/row_in_fullres` are *full
resolution*, and the image TissuePlex renders is not.

The bundled fixture **cannot catch a missing scalefactor multiply**: it has
`tissue_hires_scalef = 1.0` and `microns_per_pixel = 1.003`, both effectively identity.
Real datasets run ~0.02–0.2 and ~0.25. A passing render here is necessary, not sufficient —
see `sample_data/visium_hd_tiny/PROVENANCE.md`. `sample_data/visium_tiny` was built with a
non-identity factor precisely to close that gap.

**Bin selection follows the edge file when there is one.** Barcodes are bin-size
specific — `s_008um_00172_00043-1` and `s_016um_00066_00065-1` name different things —
so serving a different bin than `edges.parquet` was built on leaves the two with *zero*
ids in common. The edges still draw, because they carry their own coordinates, but
nothing joins: clicking a bin finds no edge, the metadata filter drops every edge, and
the tissue graph floats free of the bins beneath it. The bundled fixture shipped that way
for two releases — `niches_visium_hd.R` defaulted to 16 µm while the reader defaulted to
8 µm.

`_bin_from_edges()` now reads one barcode from the edge file and prefers the matching
bin. Without edges it falls back to `square_008um` (Space Ranger's own analysis default,
and `spatialdata-io`'s `DEFAULT_BIN`), then to the coarsest bin present. `info()` reports
`bin` and `available_bins`; a bin picker in the UI, in the shape of the edge-file picker,
would still be the natural follow-up for datasets with no edges.

There is a real reason the two disagreed: **8 µm bins are usually too sparse to score.**
Regenerating the fixture at 8 µm gave 233,531 edges of which 83 were scored; at 16 µm it
is 66,001 edges across 69 LRMs. Whoever runs NICHESv2 makes that call, and the viewer now
follows it.

Unexploited: `segmented_outputs/cell_segmentations.geojson`, which Space Ranger 4.x emits
and which would turn this from a bin viewer into a single-cell one. The seqFISH reader
already has GeoJSON ring-parsing to lift.

---

## Visium classic (readers/visium_reader.py)

The original Visium slide: 4,992 spots, each **55 µm across on a 100 µm hexagonal
pitch**, over a 6.5 × 6.5 mm capture area. One to ten cells per spot, so a spot is a
neighbourhood, not a cell — say so in figure legends, and note that NICHESv2 on this
platform scores spot–spot signalling.

```
dataset_dir/                      ← point at the *contents* of Space Ranger's outs/
  filtered_feature_bc_matrix.h5
  spatial/
    tissue_positions.csv          barcode, in_tissue, array_row, array_col,
                                  pxl_row_in_fullres, pxl_col_in_fullres
    scalefactors_json.json        spot_diameter_fullres, tissue_hires_scalef,
                                  tissue_lowres_scalef, fiducial_diameter_fullres
    tissue_hires_image.png
    tissue_lowres_image.png
```

Three things differ from Visium HD, and all three are traps:

- **There is no `microns_per_pixel`**, and the obvious replacement is wrong by 18%.
  Classic Visium scalefactors carry only those four keys; 10x deliberately does not record
  image pixel size, noting that "prior knowledge of the image pixel sizes is not used".
  So `pixel_size` has to be derived — from the **100 µm lattice pitch**, measured off the
  positions table, *not* from `spot_diameter_fullres`.

  Measured independently on `V1_Mouse_Kidney` and `V1_Adult_Mouse_Brain`:

  | | kidney | brain |
  |---|---|---|
  | in-row pitch (fullres px) | 138.00 | 138.00 |
  | `spot_diameter_fullres` | 89.46 | 89.44 |
  | ratio | 0.6482 | 0.6481 |

  `spot_diameter_fullres` is Space Ranger's **detected spot footprint** — ~64.8 µm at that
  ratio, not the 55 µm nominal capture diameter. 10x's own docs say as much, describing
  classic Visium spot diameters as "approximately 60–70 µm" and warning they are
  estimates. Deriving from 55 µm yields 0.615 µm/px where the truth is 0.725, and
  reconstructs a 5.4 × 5.7 mm capture area against the specified 6.5 × 6.5 mm; the pitch
  derivation reconstructs 6.35 × 6.67 mm. The pitch is also better conditioned — it
  averages thousands of positions in a rigid array template, where the diameter is one
  estimate of a fuzzy edge.

  **This was shipped wrong and caught only by real data.** The first version of
  `make_visium.py` set `spot_diameter_fullres = 55 / microns_per_pixel`, which made the
  fixture a tautology: it confirmed whatever derivation the reader used. It now emits the
  real 0.648 ratio, so the bad derivation produces an 85 µm nearest-neighbour spacing
  against the 100 µm truth — the same failure the real datasets show.

  `info()` reports `pixel_size_source`: `"lattice_pitch"` normally, `"spot_diameter"` when
  the positions table is too sparse to measure a pitch (< 20 in-row samples) and the
  reader falls back on the 64.8 µm constant. Everything downstream inherits whichever was
  used — the measurement tool, and the placement of `edges.parquet` micron coordinates.

- **Spots are drawn at `spot_diameter_fullres`, i.e. the ~65 µm detected footprint**, not
  the 55 µm capture area. That matches Loupe, scanpy and squidpy, so TissuePlex does not
  render Visium differently from every other viewer; `info()` reports both
  `spot_capture_diameter_um` and `detected_spot_um` so the distinction is visible.
- **`tissue_positions_list.csv` (Space Ranger < 2.0) has no header row.** Read with
  pandas' default header inference it eats the first spot and mislabels every column.
  `spaceranger.py::_read_positions_table` names the columns explicitly for that filename.
- **`tissue_hires_scalef` is genuinely far from 1** — around 0.08. See below.

**Spots are drawn as 16-gons, not squares.** An HD bin really is a square; a Visium spot
is round, and a square grid over a hex lattice misrepresents both the shape and the gaps
between spots. 16 vertices is affordable here in a way it would not be on HD: a full
capture area is 4,992 spots (~80K vertices) against HD's hundreds of thousands of bins.

Detection requires *both* the scalefactors and a positions file, and is registered after
Visium HD. HD's top-level `spatial/` holds images only, so the two cannot currently
collide — ordering the more specific sentinel first means a future HD layout change
cannot silently reroute HD datasets here.

---

## The Space Ranger coordinate contract (readers/spaceranger.py)

Both 10x array platforms share a base class, because they agree on everything except
layout, unit shape and where the pixel size comes from. **Read this before touching
coordinates in either reader.**

`pxl_col_in_fullres` / `pxl_row_in_fullres` are pixels in the **original
full-resolution microscope image**, which Space Ranger does not ship. What it ships is
`tissue_hires_image.png`, the same frame scaled by `tissue_hires_scalef`. TissuePlex
builds its tile pyramid from that PNG and derives its whole coordinate space from it, so
every coordinate the readers return is multiplied by that factor, and `pixel_size` reports
µm per **hires** pixel.

This was wrong for a release. `hires_scalef` was computed and reported in `info()` but
never applied, while the module docstring claimed it was — invisible locally because the
HD fixture has the factor set to exactly 1.0. On real HD data it displaces everything by
1/scalef; on classic Visium, by about 12×. `sample_data/make_visium.py` therefore
generates a factor of 0.08, and the fixture check is decisive: 252/252 spots land on the
image with the multiply, 0/252 without it.

Folding the factor into `pixel_size` matters beyond distance labels — `EdgeReader` divides
the micron coordinates in `edges.parquet` by `pixel_size` to place edges, so a
fullres-based value would scatter the connectivity layer off the tissue.

**The caveat this leaves.** If a user drops their own full-resolution image into the
dataset folder and selects it, coordinates will be wrong by the same factor, because the
reader cannot know which image the viewer has open. Selecting `tissue_lowres_image` has
the same problem. Fixing it properly means per-image transforms, which is what
`spatialdata` does and what TissuePlex's single global image space does not model.

---

## seqFISH / Spatial Genomics (readers/seqfish_reader.py)

"seqFISH" names two unrelated things. The academic Cai-lab method has no standard output
layout; **this reader targets the commercial Spatial Genomics GenePS platform**, which
does. One flat directory, every file prefixed with an ROI name, one ROI per dataset folder
(several ROIs in one folder logs a warning and uses the first).

```
seqfish_dataset/
  Roi1_CellCoordinates.csv    label, area, center_x, center_y
  Roi1_CellxGene.csv          unnamed first col = label; remaining cols = genes
  Roi1_TranscriptList.csv     name, x, y, [z]      — no `cell`, no `qv` in v2
  Roi1_DAPI.tiff              OME-TIFF despite the .tiff extension; often pyramidal
  Roi1_Segmentation.tiff      integer label mask (unused — v2 uses the GeoJSON)
  Roi1_Boundaries.geojson     polygons; feature `id` == label
```

**A single dataset mixes coordinate systems, and this is the thing to get right.**
Measured on the reference dataset (1000×1000 px DAPI at 0.107161 µm/px = 107.16 µm across):

| Source | Extent | Units |
|---|---|---|
| `CellCoordinates.csv` `center_x` | 1.82 → 105.66 | **microns** |
| `TranscriptList.csv` `x` | 0.00 → 107.05 | **microns** |
| `Boundaries.geojson` vertices | 0 → 999 | **pixels** |

Cells and transcripts are divided by `pixel_size`; boundaries pass through untouched.
Applying one transform to everything puts cells and their own outlines in different
places — which reads as a rendering bug rather than a unit bug. Worse, the convention
differs across GenePS software versions, so it cannot be hard-coded.

`_units_divisor()` therefore decides **per table**, comparing that table's extent to the
image width: a ratio near `pixel_size` means microns, near 1.0 means pixels. On the
reference data the ratios are 0.106 / 0.107 / 0.999 — two orders of magnitude apart. The
verdict is logged on load, so if a dataset ever misdetects it is visible in the backend
output rather than silent.

The regression test for this is geometric, not a digest: **every cell centroid must fall
inside its own polygon.** 62/62 on the reference dataset and 36/36 on the synthetic
fixture, with zero false positives against a control. Re-run that check after touching
anything in the coordinate path.

Other things worth knowing:

- `pixel_size` comes from `PhysicalSizeX` in the **DAPI OME-XML** — not a manifest, unlike
  every other platform. Falls back to 0.107 (the documented GenePS value).
- `cell_area` is deliberately left in **µm²** to match Xenium, which never converts it, so
  the "µm²" label in `CellInfoPanel` is true on every platform.
- Cell identity comes from each GeoJSON feature's `id`, which equals `label`.
  `spatialdata-io` instead maps polygons to cells *positionally* and has an open issue
  about the fragility (scverse/spatialdata-io#249); a silent off-by-one there would draw
  every outline on the wrong cell. We join on `id` and fall back to position only if
  absent.
- GeoJSON rings are closed (first vertex repeated); the reader drops the duplicate because
  deck.gl closes polygons itself and Xenium boundaries do not repeat it.
- v2 dropped the transcript→cell assignment column and has no `qv`. Nothing needs them
  today, but expression can only come from `CellxGene.csv`, never from transcripts.

**Test data.** `sample_data/make_seqfish.py` generates a committable synthetic v2 ROI and
deliberately reproduces the mixed units, so a reader that got them wrong would fail on it.
The real reference dataset (`seqfish-2-test-dataset.zip`, scverse CI fixture) is public by
written permission from Spatial Genomics rather than under an open licence — usable
locally, gitignored, and must not be redistributed from this repo.

---

## Spatial Query Path (readers/duck.py)

`transcripts()` and `cell_boundaries()` query parquet through DuckDB rather than loading
it into pandas. `readers/duck.py` holds the shared pieces — `connect()`, `scan()`,
`columns()`, `bbox_predicate()`, `in_predicate()`, `reservoir_sample()`, `to_records()` —
so every reader builds queries the same way. `EdgeReader` predates it and has its own
equivalent helpers; the two should converge.

**The reason is memory, not raw speed.** The old path did
`pq.read_table(...).to_pandas()` and masked in pandas, which materializes the whole file
on every viewport change. Measured on a synthetic 40M-row / 0.78 GB transcripts file,
one zoomed-in viewport query:

| | peak RSS | wall time |
|---|---|---|
| pandas full read + mask | 2903 MB | 912 ms |
| DuckDB streaming | 233 MB | 1369 ms |

12× less memory. Production runs on a 16 GB droplet, so a multi-GB `transcripts.parquet`
under the old path would OOM well before it was slow. DuckDB is somewhat *slower* here
because the file is not spatially sorted (see What's Not Built Yet #1) — pruning cannot
skip anything, so it pays predicate-evaluation cost without the row-group savings. Fixing
the layout closes that gap and then some.

### Spatial index cache (readers/spatial_cache.py)

Streaming fixed memory but not speed, because the bbox predicate could not prune:
Xenium writes `transcripts.parquet` in acquisition order with huge row groups (the
bundled breast dataset is 1.1M rows in **2** row groups, the first spanning the whole
x-range), so statistics exclude nothing.

`spatial_cache.sorted_path()` rewrites the file sorted by a coarse spatial grid with
100K-row row groups, cached on disk and rebuilt when the source changes — the same
"derive an artifact on first access" pattern `ensure_pyramid` uses. Measured:

| Path | uncached | cached | |
|---|---|---|---|
| Xenium transcripts (600 MB, 40M rows) | 1180 ms | **44 ms** | 27× |
| seqFISH transcripts (229 MB CSV, 8M rows) | 1595 ms | **156 ms** | 10× |
| Xenium boundaries (40 MB, 3.6M vertices) | 102 ms | 73 ms | 1.4× |

Boundaries gain least by design: half that query is a `cell_id` semi-join to pull whole
polygons, which spatial sorting cannot help. For seqFISH the cache also converts CSV to
parquet, which is why it helps a format that cannot be range-scanned at all.

Four things to know:

- **`bbox_predicate()` inlines the bounds as SQL literals, and that is load-bearing.**
  DuckDB prunes row groups at plan time; with `?` parameters the values are unknown then,
  so it cannot prune. Measured on the sorted file: COUNT 6.8 ms with literals vs 155 ms
  bound; SELECT 35 ms vs 321 ms. On an unsorted file the two are identical, which is why
  this only started to matter once the cache existed. Inlining is safe because every value
  goes through `float()` and non-finite values are rejected — string filters such as gene
  names still go through `in_predicate()`. `edge_reader.py` still binds its bbox; that
  costs nothing today because edge parquet is unsorted, but it would have to change before
  an edge spatial index would pay off.
- **Small files are skipped** (`SPATIAL_CACHE_MIN_BYTES`, default 64 MB). Below that the
  build cost and extra disk are not repaid. This also keeps the bundled sample datasets
  uncached, so the golden baseline does not depend on whether a cache happens to exist.
- **A failed build returns None and the query uses the source file**, so indexing can
  never make a dataset unreadable. `SPATIAL_CACHE=0` disables it entirely.
- **The build runs in a background thread; queries use the unsorted file until it lands.**
  It used to build inline, which made the first transcript request on a real dataset
  unservable: sorting 132M rows takes ~94 s against nginx's 120 s `proxy_read_timeout`,
  and before the memory fix below it was killed outright. Either way the hook saw a
  non-ok response, returned `[]`, and the layer rendered empty — indistinguishable from
  "this platform has no transcripts". `_resolved` is left *unset* while a build is in
  flight, because it is the decided-forever cache: writing None into it would pin the
  source to the unsorted file for the life of the process even after the index landed.
- **Cache validity covers the sort columns, not just the source stamp.** The cache
  filename derives from the source stem alone, so without that check a file sorted on one
  column pair would be served for a query on another — sorted by the wrong axis, silently.

Row *order* differs between a sorted file and its source, so seeded reservoir sampling
draws a different subset. Totals and filter results are unaffected; it is only why
enabling the cache moves the sampled golden probes.

Things to preserve when editing these methods:

- **`total` is a pre-sample count.** Both endpoints return `{rows, total}` where `total` is
  the count *after* bbox/gene filtering but *before* sampling. `useCellBoundaries` divides
  its ~5K target by `total` to pick the next fraction, so returning a post-sample count
  makes the auto-fraction oscillate.
- **Boundaries select whole cells, never loose vertices.** A cell qualifies if *any* vertex
  falls in the bbox, and then all of its vertices are returned. Filtering vertices directly
  clips cells at the viewport edge into torn polygons — measured at 97 clipped cells on the
  bundled breast dataset before this changed.
- **Sampling is seeded** (`duck.SAMPLE_SEED`). Re-fetching an unchanged viewport must return
  the same rows or the layer visibly flickers.
- **`USING SAMPLE` goes on a subquery** wrapping the filtered SELECT. Applied alongside a
  WHERE clause, DuckDB may sample before filtering.
- **DuckDB cannot bind numpy scalars.** `bbox_predicate()` casts to builtin `float` for
  this reason.
- A fresh `connect()` per call is deliberate — DuckDB's global connection is not
  thread-safe and returns corrupt results under FastAPI's threadpool rather than raising.
  It costs ~5 ms, which is noise next to the scan.

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

**Autocrine edges are colored on the same scale as directed edges.** They were once
excluded from the `lrm_set` computation, which left the rings stuck on their default
orange whatever the color control said — the one edge type that ignored "color by LRM
set" — and they also skipped the `visible_lrm_count > 0` test, so hiding every
mechanism removed the lines but left a ring on every cell. Both are fixed: the layer
now derives from the same map and applies the same LRM filter. Sharing the scale is
safe because autocrine `score_sum` medians run 1.00–1.33× the directed medians across
the bundled datasets, so they neither dominate the range nor need one of their own.
Metadata mode never had the problem, because `edge_color_values` groups over the whole
parquet and so already covered autocrine rows.

Categorical data uses `QUAL_PALETTE` (20 visually distinct colors) from `colormap.js`.
Beyond 20 categories, `geneColor()` provides deterministic hash-based colors.

---

## Metadata Typing and Subsetting (readers/metadata_filter.py)

Two features share one module because they are the same question asked twice: *what
kind of thing is this column?* Issue #35 asks it to pick a colour scheme, issue #45
to pick a subset. `metadata_filter.py` answers both, and `base_reader` uses it for
the cells table while `edge_reader` uses it for the edge table, so the two panels
cannot drift apart.

### Categorical vs continuous (#35)

`is_categorical(col, forced)`:

- `forced=None` — auto: strings, objects, bools, pandas categoricals, and **integers
  with ≤ 30 distinct values** are categorical. That threshold is what makes Seurat
  cluster IDs work, since `fwrite` on a `@meta.data` writes them as ints.
- `forced=True` / `False` — the user's explicit "treat as categorical" choice.
  Forcing *continuous* on a text column is ignored: there is no gradient to draw,
  and honouring it would paint every unit one colour.

`sort_categories()` sorts numerically when every label parses as a number, so cluster
10 comes after cluster 2 rather than between 1 and 2.

**`_color_values_meta` now lives on the base class.** Every reader used to carry a
near-identical copy, and the six copies had already drifted — CosMx filled NaN with
`""`/`0` where the others dropped it, and only some passed `key=str` to `sorted`.
A reader now supplies only `_metadata_frame()`, the cells table it already builds.

The override travels as `categorical` on `POST /color-values` and
`POST /edge-color-values`, and lives in the store under `categoricalOverrides`
keyed `cell::<field>` / `edge::<field>`.

**The frontend no longer guesses the type from the schema dtype.** It could not: the
backend's rule also depends on cardinality, which the schema does not carry. The old
guess disagreed for exactly the columns issue #35 is about — an integer cluster column
drew discrete colours on the canvas while the panel showed a viridis bar with two
sliders that did nothing. Panel 0 now records the type the backend actually returned
(`cellColorType` / `cellColorCategories`), and `EdgeSection` asks directly for the
edge side. That also removed the duplicate `color-values` fetch both legends were
making for themselves.

### Subsetting (#45)

`MetadataFilter` is either a categorical allowlist (`values`, compared as strings so
it works whatever the dtype) or an inclusive numeric range (`vmin`/`vmax`), plus
`include_missing` — false by default, because a cell with no cluster call is not part
of "cluster 4".

**Filters are resolved and applied server-side, before sampling.** This is the whole
design constraint. Both the boundary and edge queries sample on the server, so a
client-side filter would leave a fraction of a subset: narrowing to a cluster holding
5% of cells at a 10% sample would draw 0.5% of the tissue. Filtering first means the
subset renders at full density.

- `SpatialDatasetReader.filter_cell_ids(spec)` resolves against `_metadata_frame()`
  and caches per (reader, spec) — the same filter is re-resolved on every pan.
  An unknown column raises `ValueError` → HTTP 400, rather than silently rendering
  everything while the panel shows an active filter.
- Each reader's `cell_boundaries()` takes `cell_ids` and **must apply it before the
  count and the sample**. The five implementations differ too much to share code:
  Xenium and CosMx join it into their DuckDB query, MERSCOPE skips non-matching rows
  before decoding WKB, Visium HD and seqFISH mask their in-memory frames.
- `EdgeReader.query_grouped()` takes `cell_ids` and `edge_filter`. An edge survives
  the cell filter only when **both** endpoints do — the point of "focus on 2–3 cell
  types" is the signalling within that subset, and a half-outside edge would run off
  to a cell that is not drawn. `edge_filter` becomes a real SQL predicate when the
  column is in the parquet, and a semi-join against a registered frame when it comes
  from `edge-metadata/`.

**Large id sets go through `duck.register_ids()`, not `IN (?, ?, …)`.** A filter can
keep hundreds of thousands of cells; binding that many parameters is unworkable and
the SQL text alone reaches megabytes. Registering a one-column frame makes it an
ordinary hash semi-join.

Two things that bit during implementation and are easy to reintroduce:

- **Boolean columns need lowering.** The categories the panel offers come from pandas
  (`"True"`), while DuckDB's `CAST(BOOLEAN AS VARCHAR)` yields `"true"`, so a literal
  comparison silently matches nothing. `edge_filter_sql` lowers both sides for boolean
  columns only — doing it for every column would merge genuinely distinct string labels.
- **The auto sample fraction must recalibrate after a filter.** `useCellBoundaries`
  picks its fraction from the previous fetch's total, which a filter invalidates, and
  nothing else would trigger another fetch — so the layer sat showing a tenth of an
  already-small subset until the user happened to pan. It now re-fetches once when the
  corrected fraction is >1.2× the one used. The threshold matters: the panel derives
  its displayed percentage from the *current* total, so a looser one leaves the readout
  advertising a fraction the canvas is not drawing at.

**Transcripts are deliberately not filtered.** Several platforms ship no
transcript→cell assignment at all (seqFISH v2 dropped the column), so the filter has
nothing to join on and would work on some datasets and not others.

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

Note: the dev server runs on port **5173**, set in `frontend/vite.config.js`. It must not
be 3000 — `docker compose` binds 3000 for the production frontend, so a dev server on 3000
collides with any running container. `.claude/launch.json` passes `--port 5173` explicitly
as well, so both entry points agree.

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

**Cloud deployment:** `docs/cloud-deploy.md` is a complete DigitalOcean runbook
(~$106–116/month: 16 GB / 4 vCPU droplet + 200 GB block storage). The moving parts are
`deploy.sh` (droplet bootstrap), `docker-compose.prod.yml` (production stack),
`Caddyfile` (reverse proxy + automatic TLS), and `upload-data.sh` (rsync datasets up).
Tuning knobs live in a `.env.prod` file that is gitignored and must be created by hand;
`DUCKDB_MEMORY_LIMIT` is the one to reach for if the backend OOMs on large edge files.

**`DUCKDB_MEMORY_LIMIT` must not have a fixed default, and this is a real failure mode.**
It used to default to `8GB` in both compose files. On a stock Docker Desktop VM — 7.8 GB
here — that authorises DuckDB to take all of RAM, so the spatial-index build over a real
Xenium `transcripts.parquet` was killed by the *VM's* OOM killer mid-request, restarting
uvicorn. The container memory limit does not catch this: compose declares 12 GB, which is
larger than the VM, and `docker inspect` reports `OOMKilled=false` because the container
limit was never reached. Only `RestartCount` climbing gives it away.

`duck.py::_default_memory_limit()` now takes 60% of the **minimum** of the cgroup limit and
physical RAM. Both numbers are needed — either can be the real ceiling and they routinely
disagree. `connect()` also sets `temp_directory` (under `CACHE_DIR`, the one writable
volume, since `/data` is mounted read-only): an in-memory DuckDB cannot spill without it,
so a sort larger than the cap fails outright instead of going out-of-core. Spilling is
what makes the 132M-row build possible at all rather than merely slower.

Other env knobs: `SPATIAL_CACHE=0` disables the spatial index entirely,
`SPATIAL_CACHE_MIN_BYTES` (default 64 MB) sets the size below which files are left alone,
and `CACHE_DIR` relocates both the DZI pyramids and the spatial index off the data volume.

**Access control is opt-in and off by default.** The Caddyfile supports `basicauth`, but
unless it is enabled anyone with the URL can view the data. There is no application-level
auth, no user accounts, and no per-dataset permissions.

**Regression guard.** `backend/tests/golden_snapshot.py` exercises every reader method
against all local datasets, digests the results, and diffs them against a recorded
baseline (238 probes across 8 datasets). Run it after any reader change:

```bash
cd backend && python3 tests/golden_snapshot.py          # check
cd backend && python3 tests/golden_snapshot.py --record # adopt intentional changes
```

Datasets absent from a checkout are skipped, so it works with only the committed fixtures.
Two determinism rules keep it honest: record-list digests are order-independent (because
`query_grouped` uses `ORDER BY RANDOM()`), and sampling is seeded (`duck.SAMPLE_SEED`).
If a probe changes and you cannot explain why, that is the point of the tool.

**Frontend tests** run under Vitest, added with the annotation fix in v0.8.5:

```bash
cd frontend && npm test        # vitest run
cd frontend && npm run test:watch
```

`src/store.annotations.test.js` is the first of them. Store logic is plain JS, so
these need no DOM and no jsdom dependency — reducers can be exercised directly
through `useStore.getState()`. Coverage is currently annotations only.

There is still **no CI and no linter** — no `.github/workflows`, no ESLint or Python lint
config. The snapshot is a guard, not a test suite: it catches "this changed" but does not
assert correctness. Be correspondingly careful with the OSD ↔ deck.gl coordinate bridge,
which it does not cover at all.

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
- **`list_images` and `_find_source` are a matched pair.** They must search the same
  locations in the same order (root, then one subdirectory level). Changing the depth or
  order in one without the other makes the picker list images the tile builder can't open.
- **Edge color clamp has two different defaults, by design.** `Viewer.jsx` auto-sets
  `edgeColorClamp.high` from the p95 of `visible_score_sum` so the initial view isn't
  washed out by outliers. But `useEdgeColors` computes its own fallback `hi` as `max`, not
  p95, so that "reset range" lands on a value matching the legend endpoints. They disagree
  intentionally — don't "fix" one in isolation.
- **`edges.py` validates path traversal; the other routers don't.** `edges.py::_reader`
  resolves `edge_file` and rejects anything escaping the dataset directory.
  `spatial.py::_reader`, `tiles.py`, and `layers.py` do a bare `DATA_ROOT / dataset` with
  no equivalent check. Harmless for a local single-user tool; worth closing before any
  deployment where the URL is reachable by someone untrusted.
- `zarr==2.18.2` is still pinned in requirements.txt although the readers use parquet and
  HDF5, not zarr. Likely stale; verify before removing.

---

## What's Not Built Yet

1. **Edge queries are not spatially indexed.** `transcripts()` and `cell_boundaries()`
   now go through `spatial_cache` (see the Spatial Query Path section), but `EdgeReader`
   does not. Two things would need to change: sort `edges.parquet` on `x1`/`y1`, and stop
   binding its bbox as `?` parameters, which defeats row-group pruning. Less urgent than
   it was for transcripts, because `query_grouped` already collapses the row count
   server-side.

2. **Supplemental cell metadata is not shown in the cell info panel** — `CellInfoPanel.jsx`
   renders a hardcoded field list (`cell_id`, x, y, `transcript_counts`, `total_counts`,
   `cell_area`, `nucleus_area`) plus expression. Supplemental columns merged by
   `_cells_full()` reach the color-by dropdown but only appear in the panel if one happens
   to be the active color-by field. `EdgeInfoPanel` does render its annotations
   generically — the cell panel should be brought in line with it.
   `sample_data/mouse_ileum_tiny/cell-metadata/example_clusters.csv` now exercises the
   feature locally. It carries a `seurat_clusters` column spanning 0–11 specifically so
   the demo data reproduces issue #35: twelve integer levels, where a lexicographic sort
   would put 10 and 11 between 1 and 2.

3. **Cell expression bar chart** — click panel shows cell metadata but not a sorted gene
   expression readout. `/spatial/{dataset}/expression/{cell_id}` exists; the UI does not.

4. **Reader interface drift** — `VisiumHDReader.transcripts()` / `.cell_boundaries()` use
   the old `limit=` signature and return `[]`. See the caveat under Platform Support.

5. **MERSCOPE cell boundaries** — HDF5 polygon data; `MerscopeReader.cell_boundaries()`
   returns empty and the reader declares `has_boundaries: False`.

6. **CosMx gene-set coloring and boundaries** — gene-set coloring requires per-cell
   expression aggregation from the transcript file; boundaries are per-FOV label TIFFs.
   Both are stubs.

7. **Visium HD expression** — `gene_list()`, `cell_expression()`, and gene-set color-values
   need `filtered_feature_bc_matrix.h5` parsing.

8. **Rendering performance** — edge rendering is fast now (query-grouped returns ~300K
   rows instead of 168M; colors computed client-side). Remaining: LOD for arrowheads at
   low zoom, transcript rendering at very high density.

9. **Authentication** — no application-level auth. Caddy `basicauth` is available for
   cloud deployments (`docs/cloud-deploy.md`) but is **opt-in and off by default**. There
   are no user accounts and no per-dataset permissions.

### Open GitHub issues

Both of the previously open issues (**#35** force-categorical toggle, **#45** select
cells/edges by metadata) are implemented — see the *Metadata Typing and Subsetting*
section. What each issue asked for but this pass did not deliver:

- **#35** — the choice is per column and per session, but is not persisted across a
  reload, and the legend has no per-category visibility checkbox. The filter section
  covers the "show only cluster 4" case that checkbox would have served.
- **#45** — filtering is on **one column at a time**. Composing two cell-side
  predicates ("cluster 4 *and* sample B") needs a list of filters rather than a single
  one; the backend `MetadataFilter` is already a value object, so the change is an
  `and`-list in the store and a loop in `filter_cell_ids`. Transcripts are excluded
  by design (no cell assignment on several platforms).
