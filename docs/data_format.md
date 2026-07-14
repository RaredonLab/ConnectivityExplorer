# Edge Data Format (NICHESv2)

This document specifies the `edges.parquet` file produced by `export_for_TissuePlex()` from the NICHESv2 R package. TissuePlex reads this file alongside any supported platform output folder.

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

### Optional but recommended

| Column | Type | Description |
|--------|------|-------------|
| `sending_type` | string | Cell/spot type label for the sending unit |
| `receiving_type` | string | Cell/spot type label for the receiving unit |

Any additional numeric or string columns are automatically available as metadata color-by options in the edge layer panel.

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

---

## Coordinate system

`x1`, `y1`, `x2`, `y2` should be in the **native coordinate system** of the platform (typically µm). The backend converts to image pixel coordinates using the `pixel_size` reported by each platform's reader.

| Platform | Coordinate source |
|---|---|
| Xenium | `x_centroid`, `y_centroid` from `cells.parquet` |
| MERSCOPE | `center_x`, `center_y` from `cell_metadata.csv` |
| CosMx | `x_global_px`, `y_global_px` from metadata (already in pixels) |
| Visium HD | `pxl_col_in_fullres`, `pxl_row_in_fullres` from `tissue_positions.parquet` |

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
