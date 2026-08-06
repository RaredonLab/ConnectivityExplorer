# Plan: seqFISH (Spatial Genomics GenePS) support

**Status: PROPOSAL — not yet approved, nothing implemented.**

## Context

TissuePlex needs to read seqFISH data alongside Xenium without regressing Xenium, and to
make cell metadata, edge data (NICHESv2), and edge metadata behave the same way on every
platform. Edge metadata does not exist for any platform today, so this is where that
contract gets defined.

**Decisions already made** (from the design questions):

| Decision | Choice |
|---|---|
| Target format | Spatial Genomics GenePS (the commercial platform) |
| Multi-ROI | One ROI per folder — no ROI-as-dataset machinery |
| Cell IDs | Bare integer label, `"42"` — matches the raw file, no R-side change |
| Edge metadata | `edge-metadata/` folder mirroring `cell-metadata/` |

Those choices remove the two most invasive parts of the original sketch. What remains is
a self-contained reader plus one genuinely shared piece of infrastructure.

---

## What we're building against

"seqFISH" names two unrelated things. **Academic seqFISH/seqFISH+** (Cai lab) is a method
with no standard output — published data is ad-hoc `.txt`/`.mat` tables, and the HuBMAP
schema covers *raw acquisition*, not analysis output. **Spatial Genomics Inc. GenePS** is
the commercial platform with a real, stable format. We target GenePS, confirmed.

I read the `spatialdata-io` reader source rather than relying on documentation prose, so
the following is what a working implementation actually expects.

### Layout (current "v2" format)

Flat directory, files prefixed by an ROI name, no subdirectories:

```
seqfish_dataset/
  Roi1_CellCoordinates.csv     label, area, center_x, center_y
  Roi1_CellxGene.csv           unnamed first col = label; remaining cols = genes (dense)
  Roi1_TranscriptList.csv      name, x, y, [z], [refid]
  Roi1_DAPI.tiff               OME-TIFF (OME-XML despite the .tiff extension), often pyramidal
  Roi1_Segmentation.tiff       integer label mask
  Roi1_Boundaries.geojson      cell polygons
```

**Sentinel:** `glob("*_CellCoordinates*.csv")`. There is no manifest or version file, so
detection has to be a glob. Registered last in `ReaderFactory` so it cannot shadow the
existing exact-filename sentinels.

### Legacy "v1" layout — also supported, per your decision

Spatial Genomics renamed things at some point. Both are handled; the reader picks a
variant by which filenames are present and records the choice in `info()`.

| v1 (legacy) | v2 (current) |
|---|---|
| `{prefix}_CellCoordinates_section{N}.csv` | `{roi}_CellCoordinates.csv` |
| `{prefix}_CxG_section{N}.csv` | `{roi}_CellxGene.csv` |
| `{prefix}_TranscriptCoordinates_section{N}.csv` | `{roi}_TranscriptList.csv` |
| `{prefix}_DAPI_section{N}.ome.tiff` | `{roi}_DAPI.tiff` |
| `{prefix}_CellMask_section{N}.tiff` | `{roi}_Segmentation.tiff` |
| *(no boundaries file)* | `{roi}_Boundaries.geojson` |
| transcripts have `cell`, no `z` | transcripts have `z`, no `cell` |

Two consequences: v1 has **no GeoJSON**, so boundaries must come from polygonising
`CellMask_section{N}.tiff`, which is more work than reading vertices and is the main cost
of v1 support — I would implement v1 cells/transcripts first and treat v1 boundaries as a
follow-on, declaring `has_boundaries: False` until it lands. And v1 *does* carry the
transcript→cell assignment that v2 dropped, so if it is present we should keep it.

The public SGI Mouse Kidney release is v1, which is the practical reason this matters.

### Four things that will bite, and the plan for each

**1. A single dataset mixes microns and pixels — now measured, not guessed.**
This was the biggest risk in the plan. It is real, and it is confirmed against the
downloaded dataset:

| Source | Extent | Verdict |
|---|---|---|
| `Roi1_DAPI.tiff` | 1000 × 1000 px, `PhysicalSizeX` = 0.107161 µm/px → 107.16 µm | reference |
| `CellCoordinates.csv` `center_x` | 1.82 → 105.66 | **microns** |
| `TranscriptList.csv` `x` | 0.0 → 107.05 | **microns** |
| `Boundaries.geojson` vertices | 0 → 999 | **pixels** |

So cells and transcripts need dividing by `pixel_size`; boundaries must be passed through
untouched. Applying one global transform — in either direction — puts cells and their
own outlines in different places, which reads as a rendering bug rather than a unit bug.

The auto-detection heuristic is validated on this data: compute
`ratio = max(coord) / image_width_px`. A ratio near `pixel_size` (0.106, 0.107 above)
means microns; a ratio near 1.0 (0.999 above) means pixels. It cleanly separates all
three cases here with two orders of magnitude to spare. Read `PhysicalSizeX`/`PhysicalSizeY`
from the DAPI OME-XML for `pixel_size`, fall back to 0.107, apply the heuristic per source
table, and log the verdict on load.

**2. GeoJSON features carry the cell label in `id`** — verified: `id` values are strings
`"1"`, `"2"`, … matching `label` in `CellCoordinates.csv` exactly, all 62 unique.

This matters because `spatialdata-io` does *not* read it — it maps polygons to cells
**positionally** and has an open issue about the fragility (scverse/spatialdata-io#249).
We can join on `id` and be correct by construction, falling back to positional order only
if `id` is missing. Worth doing better than the reference implementation here, since a
silent off-by-one in this mapping would draw every outline on the wrong cell.

**3. v2 dropped the transcript→cell assignment.** Older exports had a `cell` column in
`TranscriptList.csv`; the current format removed it, so transcripts arrive unassigned.
Nothing in TissuePlex needs per-transcript cell assignment today (the transcript layer is
positional dots), so this costs us nothing now — but it means we cannot derive expression
from transcripts, and `CellxGene.csv` is the only expression source.

**4. There is no QV column.** Xenium's per-transcript quality filter has no seqFISH
equivalent. Not a blocker; just means that control has nothing to bind to.

### Column mapping

| Concept | Xenium | seqFISH |
|---|---|---|
| cell id | `cell_id` (string barcode) | `label` (int → `str(label)`) |
| centroid | `x_centroid`, `y_centroid` | `center_x`, `center_y` |
| cell area | `cell_area` | `area` |
| transcript position | `x_location`, `y_location` | `x`, `y` |
| transcript gene | `feature_name` | `name` |
| transcript quality | `qv` | *(none)* |
| boundaries | long parquet, `vertex_x`/`vertex_y` | GeoJSON polygons |
| counts | `cell_feature_matrix.h5` | `CellxGene.csv` (dense) |
| pixel size | `experiment.xenium` | DAPI OME-XML |

---

## Test data — downloaded and verified

`seqfish-2-test-dataset.zip` (1.7 MB, `s3.embl.de/spatialdata/raw_data/`) is now at
`sample_data/seqfish_instrument2/`. Real Spatial Genomics v2 output:

```
Roi1_CellCoordinates.csv    62 cells      label, area, center_x, center_y
Roi1_TranscriptList.csv     9,051 rows    name, x, y, z   (12 genes, z all = 1)
Roi1_CellxGene.csv          62 × 12       unnamed first col + gene columns
Roi1_DAPI.tiff              1000×1000 uint16, OME, 3 pyramid levels
Roi1_Segmentation.tiff      1000×1000 uint32, 62 labels
Roi1_Boundaries.geojson     62 Polygon features, ~20 vertices each
```

Confirmed v2: **`TranscriptList.csv` has no `cell` column**, so transcripts are
unassigned, and there is no `qv`.

Licence: public for CI by written permission from Spatial Genomics, not an open licence.
`sample_data/.gitignore` now excludes `seqfish*/` so it cannot be committed or
redistributed from this repo.

Because the fixture cannot be committed, I still want a **synthetic seqFISH generator** in
the shape of `sample_data/make_edges.py`, emitting a tiny valid ROI. That is what makes
the format testable in CI without licence questions.

Everything else found is unsuitable and worth recording so nobody re-searches it: the
squidpy `seqfish.h5ad` (32 MB) and the Bioconductor/Giotto fixtures are academic-format
AnnData or count tables with no transcripts, boundaries, or image; the SGI Mouse Kidney
release is email-gated and tens of GB; **GEO has essentially nothing** in GenePS format.

---

## Design

### 1. `SeqfishReader` (new)

Subclasses `SpatialDatasetReader` like every other platform, implements the same
interface, and returns pixel coordinates per the existing contract. One ROI per folder:
glob for `*_CellCoordinates.csv`, take the single match, and warn if there are several
rather than silently picking one.

- `cells()` — `CellCoordinates.csv`; `label`→`cell_id` (as string), `center_x/y`→
  `x_centroid`/`y_centroid`, `area`→`cell_area`
- `transcripts()` — `TranscriptList.csv`; `name`→`feature_name`, `x`/`y`→
  `x_location`/`y_location`
- `cell_boundaries()` — `Boundaries.geojson` flattened to the same long-format
  `{cell_id, vertex_x, vertex_y}` rows Xenium already produces, so `useCellBoundaries`
  needs no change at all
- `color_values()` / `cell_expression()` — from `CellxGene.csv`
- `capabilities()` — all three layers true, `unit_label: "cell"`
- Morphology — `Roi1_DAPI.tiff` is already OME and often pyramidal, so the existing
  `tiles.py` path should work unmodified. The subdirectory-aware `list_images` from
  v0.4.0 already handles finding it.

### 2. Normalising ingest cache (shared)

seqFISH ships CSV. CSV cannot be range-scanned, has no column statistics, and re-parses on
every request — strictly worse than the parquet path just optimised. `CellxGene.csv` for a
real run (670k cells × 1092 genes) is multi-GB.

Propose `ensure_normalized(dataset)`, cached on disk exactly like the DZI pyramid:

```
dataset_dir/
  .tissueplex_cache/
    transcripts.parquet        canonical columns, spatially sorted
    cells.parquet
    cell_boundaries.parquet    GeoJSON flattened to vertex rows
```

Three jobs at once: makes seqFISH queryable at Xenium speed; normalises column names so
the readers become thin adapters over one query implementation; and delivers the spatial
sort measured during the perf work — **COUNT 206 ms → 9 ms, SELECT 1010 ms → 29 ms** on a
40M-row file for a one-time 4.1 s build.

Xenium adopts it incrementally: if the cache is absent, query the original parquet as
today. Nothing breaks while it rolls out.

### 3. Edge metadata (new, cross-platform)

Mirror `cell-metadata/` exactly:

```
dataset_dir/
  edge-metadata/
    annotations.csv     key column `edge` = "SendingCell|ReceivingCell"
```

Outer-joined on `edge`, columns surfacing automatically in the edge color-by dropdown —
exactly how supplemental cell metadata reaches the cell color-by dropdown now. Same loader
shape, same caching, same column-resolution rules. Platform-agnostic by construction,
since it lives in `EdgeReader`.

### 4. Lift supplemental metadata into the base class

`_load_supplemental_metadata()` and `_read_csv_with_barcodes()` are Xenium-only today but
contain nothing Xenium-specific. Move them to `base_reader.py` so seqFISH gets
`cell-metadata/` for free and the other three readers can adopt it. Mostly a move, not a
rewrite — and it is the concrete form of the "similar across platforms" ask.

---

## Sequencing

1. **Golden-output snapshot of both Xenium datasets** — the regression guard (below).
2. Supplemental metadata moves to `base_reader` — no behaviour change.
3. Synthetic seqFISH fixture generator.
4. `SeqfishReader` + factory detection, against the fixture, then the real test dataset.
5. Boundaries (GeoJSON → long vertices) and the px/µm detection.
6. Normalising + spatial-sort cache; Xenium opted in second.
7. Edge metadata.

Steps 1–5 deliver a working seqFISH viewer. 6 and 7 are independent and can be deferred
or reordered.

---

## Not breaking Xenium

There is no test suite, so "without breaking current functionality" needs teeth. Before
any of this lands I will capture a **golden-output snapshot** — recorded responses from
every spatial and edge endpoint for both bundled Xenium datasets — and re-check it after
each step. Cheap to build, and it turns "I think Xenium still works" into something
actually verified. It is also the seed of a real test suite, which the repo currently
lacks entirely.
