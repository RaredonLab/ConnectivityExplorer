# Edge Data Format (NICHESv2)

This document specifies the `edges.parquet` file produced by `export_to_TissuePlex()` from the NICHESv2 R package. TissuePlex reads this file alongside any supported platform output folder.

> **Install NICHESv2 from the `dev` branch.** `export_to_TissuePlex()` exists only there —
> the `main` branch does not have it. `arrow` is also required but is only in `Suggests`,
> so install it explicitly:
>
> ```r
> install.packages("arrow")
> remotes::install_github("RaredonLab/NICHESv2", ref = "dev")
> ```
>
> **Coordinates must be in the same units the platform reader reports.**
> `export_to_TissuePlex()` copies `meta.data$x` / `$y` straight into `x1,y1,x2,y2` with no
> conversion, and TissuePlex divides those by the dataset's `pixel_size` on the assumption
> they are native µm. That is correct for Xenium, whose coordinates are already µm. For a
> platform whose coordinates are in **pixels** (Visium HD), convert to µm *before* calling
> `create_NICHESObject()`, or the edge layer will be offset from the cells by a factor of
> `pixel_size`.

The format is **platform-agnostic** — it works with Xenium, MERSCOPE, CosMx, Visium HD, or any other platform as long as the `sending_cell` / `receiving_cell` barcodes match those in the platform's cell/spot table.

---

## File location

Place `edges.parquet` in the root of the dataset folder:

```
dataset_folder/
  <platform sentinel files>   ← e.g. experiment.xenium, cell_by_gene.csv, etc.
  edges.parquet               ← produced by NICHESv2 R pipeline
```

TissuePlex discovers `edges.parquet` automatically — no configuration needed. If the file is absent, edge layers are hidden.

---

## Column specification

### Required columns

| Column | Type | Description |
|--------|------|-------------|
| `edge` | string | Directed edge ID: `"SenderBarcode\|ReceiverBarcode"` |
| `sending_cell` | string | Barcode of the sending cell/spot |
| `receiving_cell` | string | Barcode of the receiving cell/spot |
| `is_autocrine` | bool | `True` when `sending_cell == receiving_cell` |
| `lrm` | string | LRM string ID: `"ligand\|receptor"` (e.g. `"Tgfb1\|Tgfbr1"`) |
| `lrm_id` | int | Integer index for the LRM (1–N) |
| `ligand` | string | Ligand gene symbol |
| `receptor` | string | Receptor gene symbol |
| `score` | float | Raw NICHESv2 score for this (edge, LRM) pair |
| `score_norm` | float | Score normalized within the edge (sums to 1.0 across all LRMs for a given edge) |
| `x1` | float | Sending unit centroid X, **native µm coordinates** |
| `y1` | float | Sending unit centroid Y |
| `x2` | float | Receiving unit centroid X |
| `y2` | float | Receiving unit centroid Y |

### Cell-type columns

| Column | Type | Description |
|--------|------|-------------|
| `sending_type` | string | Cell/spot type label for the sending unit — **optional, often absent** |
| `receiving_type` | string | Cell/spot type label for the receiving unit — **optional, often absent** |

**Do not rely on `sending_type` / `receiving_type`.** `export_to_TissuePlex()`
populates them only when given a `celltype.col`, and of the scripts in `r/` only
`niches_xenium.R` exposes that (`--celltype`); the other five pass
`celltype.col = NULL`, so the columns are absent. The values in the bundled
`sample_data` fixtures are *simulated* by `make_edges.py`, not analysis output.

They are also frozen at scoring time, so they can disagree with a cells table
that has since been re-annotated through `cell-metadata/`. Anything that needs a
cell attribute per edge should resolve it against the cells table instead — see
`docs/edge_filter_independence.md`.

`export_to_TissuePlex()` **always writes these two columns**, so a file it produces has
exactly the 16 columns above, in that order. Their *values* are `NA` when
`celltype.col = NULL`, or when the named column is absent from `$edge.meta` (the exporter
warns and fills NA rather than failing). They are optional only for a hand-written
`edges.parquet`.

Any additional numeric or string columns are automatically available as metadata color-by
options in the edge layer panel.

---

## Data model

Each row represents one **(directed edge) × (LRM)** pair. A directed edge `A→B` and its reverse `B→A` are separate rows and may have different scores. Autocrine edges (`A→A`) are rendered as rings.

For an edge with M active LRMs there are M rows sharing the same `edge` value.

**Example** — edge `"cellA|cellB"` with 3 LRMs:

```
edge              sending_cell  receiving_cell  lrm             score  score_norm
cellA|cellB       cellA         cellB           Tgfb1|Tgfbr1    2.1    0.35
cellA|cellB       cellA         cellB           Il6|Il6ra       2.4    0.40
cellA|cellB       cellA         cellB           Wnt5a|Fzd1      1.5    0.25
```

### Unscored edges carry a placeholder row

`export_to_TissuePlex()` exports **every** edge in `$edge.list`, not only those with
signal. An edge present in `$edge.list` but absent from `$edge.data` gets a **single row**
with all six LRM/score fields null:

```
edge              sending_cell  receiving_cell  lrm   lrm_id  ligand  receptor  score  score_norm
cellC|cellD       cellC         cellD           NA    NA      NA      NA        NA     NA
```

This is deliberate, not corrupt output: it lets TissuePlex draw the complete tissue graph —
which cell pairs are neighbours at all — independently of which pairs have ligand-receptor
signal. That is exactly the split between the **Tissue Graph** layer (structure) and the
**Edges** layer (signal).

Consequences worth knowing:

- A real file routinely contains rows where `lrm`, `lrm_id`, `ligand`, `receptor`, `score`
  and `score_norm` are all null. The backend expects this: the LRM catalogue query filters
  `WHERE lrm IS NOT NULL`, the `excluded_lrms` list strips nulls, and the Pydantic model
  uses `List[Optional[str]]` so a null cannot trigger a 422.
- Any validation you write must use `na.rm = TRUE`. Checking `min(score)` or that
  `score_norm` sums to 1.0 per edge without it returns `NA`/`FALSE` whenever placeholders
  exist — i.e. almost always. The demo script bundled with NICHESv2 has this bug; it looks
  like a failed export when nothing is wrong.

---

## Coordinate system

`x1`, `y1`, `x2`, `y2` must be in **microns**, on every platform. The backend always divides
them by that dataset's `pixel_size` to reach image pixel space, so anything already in
pixels lands wrong by exactly that factor.

This is the single easiest thing to get wrong, because some platforms report their cell
coordinates in pixels rather than µm. Convert before building the NICHESObject:

| Platform | Cell coordinate source | Native unit | To get µm for `x1..y2` |
|---|---|---|---|
| Xenium | `x_centroid`, `y_centroid` (`cells.parquet`) | µm | use as-is |
| seqFISH | `center_x`, `center_y` (`*_CellCoordinates.csv`) | µm *(usually — see note)* | use as-is |
| MERSCOPE | `center_x`, `center_y` (`cell_metadata.csv`) | µm | use as-is |
| CosMx | `x_global_px`, `y_global_px` | **pixels** | × `pixel_size` (≈0.18) |
| Visium HD | `pxl_col_in_fullres`, `pxl_row_in_fullres` | **pixels** | × `microns_per_pixel` (`scalefactors_json.json`) |

The `pixel_size` each reader reports is on `GET /spatial/{dataset}/info`, so you can read
the exact value rather than relying on the defaults above.

> **seqFISH caveat.** Its CSV coordinates are µm in current GenePS exports but pixels in
> some older ones, and the reader auto-detects which per table (it logs the verdict on
> load). Check the backend log for that dataset before assuming.

A quick way to confirm you got it right: the edge layer should sit exactly on top of the
cell layer. A uniform scale mismatch between the two — edges clustered near the origin, or
spread far beyond the tissue — is this bug rather than a rendering fault.

---

## Minimal R export

```r
library(arrow)

# edges_df: data.frame with columns as specified above
write_parquet(edges_df, file.path(dataset_dir, "edges.parquet"))
```

---

## Multiple edge sets per dataset

To compare different computational approaches (e.g. raw vs. normalized scoring) on
the same tissue without duplicating the cell / transcript / boundary files, write
additional edge files (same schema) into an `edges/` subfolder of the dataset:

```
dataset_dir/
  edges.parquet                          # optional legacy default
  edges/
    edge.raw.minimum.parquet
    edge.normalized.product.parquet
```

```r
dir.create(file.path(dataset_dir, "edges"), showWarnings = FALSE)
write_parquet(edges_raw,        file.path(dataset_dir, "edges", "edge.raw.minimum.parquet"))
write_parquet(edges_normalized, file.path(dataset_dir, "edges", "edge.normalized.product.parquet"))
```

The viewer shows a dropdown (top of the Edge Data panel) to flip between them; the
dropdown label is the filename with the folder and `.parquet` extension stripped.
The top-level `edges.parquet`, if present, remains the default selection. See
`GET /edges/<dataset>/files`.

---

## Validation

```bash
# Check schema
curl http://localhost:8000/edges/<dataset>/schema

# Check LRM catalogue (should list all unique LRMs)
curl http://localhost:8000/edges/<dataset>/lrm-catalogue

# Check a specific edge (URL-encode the | character as %7C)
curl "http://localhost:8000/edges/<dataset>/edge/cellA%7CcellB"
```
