# Public Datasets for Development & Testing

This document lists publicly available datasets suitable for developing and testing
TissuePlex across platforms, tissue types, species, and data configurations. Most of it
covers 10x Xenium; see the classic Visium section at the end for the datasets the Visium
reader was verified against.

---

## Classic Visium — reader verification set

The two datasets the Visium reader was validated on. Both are Space Ranger 1.1.0, which
means they carry the **headerless** `tissue_positions_list.csv` — and that is the common
case, because every classic Visium dataset 10x serves without registration predates the
Space Ranger 2.0 rename. The CytAssist-era 2.x downloads return HTTP 403.

You only need two files per dataset, not the full `outs/`:

```bash
B=https://cf.10xgenomics.com/samples/spatial-exp/1.1.0/V1_Mouse_Kidney
mkdir -p sample_data/visium_mouse_kidney && cd sample_data/visium_mouse_kidney
curl -LO $B/V1_Mouse_Kidney_filtered_feature_bc_matrix.h5
curl -LO $B/V1_Mouse_Kidney_spatial.tar.gz
mv V1_Mouse_Kidney_filtered_feature_bc_matrix.h5 filtered_feature_bc_matrix.h5
tar xzf V1_Mouse_Kidney_spatial.tar.gz && rm V1_Mouse_Kidney_spatial.tar.gz
```

| Dataset | Size | Spots in tissue | Notes |
|---|---|---|---|
| `V1_Mouse_Kidney` | 12 MB + 8 MB | 1,438 of 4,992 | The verification dataset. `visium_mouse_*/` is gitignored. |
| `V1_Adult_Mouse_Brain` | 21 MB + 9 MB | — | Used only to cross-check the lattice geometry. |

**What they established**, none of which the synthetic fixture could:

- `tissue_hires_scalef` is 0.177 and 0.170 — nowhere near the 1.0 that hides a missing
  coordinate multiply.
- The in-row lattice pitch is exactly 138.00 fullres px on both, while
  `spot_diameter_fullres` is 89.45 — a ratio of 0.648, proving that field measures a
  ~64.8 µm detected footprint rather than the 55 µm capture spot. See the Visium section
  in CLAUDE.md; deriving `pixel_size` from 55 µm is wrong by 18%.
- Registration: 98.1% of `in_tissue` spots land on stained tissue and 99.5% of
  out-of-tissue spots land on bare slide, measured as mean inverse intensity under each
  spot polygon in the hires PNG.
- NICHESv2 end to end on the kidney: 1,438 spots, 9,666 edges, 2,479 LRMs, 3.2M rows. Top
  mechanisms include `Apoe|Lrp2` and `Cst3|Lrp2` — megalin-mediated proximal tubule
  uptake, which is the expected kidney biology.

---

## Tier 1 — Tiny Datasets (10–30 MB) — Primary Development

Artificially cropped to three 640px patches across two FOVs. Full Xenium output folder structure intact. Ideal for rapid iteration and CI testing.

### Xenium v3.0.0 Tiny

| Dataset | Size | Download |
|---|---|---|
| Mouse Ileum (MultiCellSeg) | 11.9 MB | https://cf.10xgenomics.com/samples/xenium/3.0.0/Xenium_Prime_MultiCellSeg_Mouse_Ileum_tiny/Xenium_Prime_MultiCellSeg_Mouse_Ileum_tiny_outs.zip |
| Mouse Ileum (Nuclear expansion) | 12 MB | https://cf.10xgenomics.com/samples/xenium/3.0.0/Xenium_Prime_Mouse_Ileum_tiny/Xenium_Prime_Mouse_Ileum_tiny_outs.zip |

### Xenium v4.0.0 Tiny

| Dataset | Size | Download |
|---|---|---|
| Human Kidney (Protein panel) | 20 MB | https://cf.10xgenomics.com/samples/xenium/4.0.0/Xenium_V1_Protein_Human_Kidney_tiny/Xenium_V1_Protein_Human_Kidney_tiny_outs.zip |
| Human Ovary (Nuclear expansion) | 27 MB | https://cf.10xgenomics.com/samples/xenium/4.0.0/Xenium_V1_Human_Ovary_tiny/Xenium_V1_Human_Ovary_tiny_outs.zip |
| Human Ovary (MultiCellSeg) | 29 MB | https://cf.10xgenomics.com/samples/xenium/4.0.0/Xenium_V1_MultiCellSeg_Human_Ovary_tiny/Xenium_V1_MultiCellSeg_Human_Ovary_tiny_outs.zip |

**Why use multiple tiny datasets**: v3 vs v4 format differences may affect parsing; MultiCellSeg vs nuclear expansion tests different segmentation overlay rendering; mouse vs human tests species-agnostic coordinate handling.

---

## Tier 2 — Small Full Datasets (278–379 MB) — Performance & Realism Testing

Two-FOV subsets. Realistic transcript density and cell counts. Good for validating rendering performance before testing on full lab data.

| Dataset | Size | Download |
|---|---|---|
| Human Breast Cancer (2 FOV) | 379 MB | https://cf.10xgenomics.com/samples/xenium/2.0.0/Xenium_V1_human_Breast_2fov/Xenium_V1_human_Breast_2fov_outs.zip |
| Human Lung (2 FOV) | 278 MB | https://cf.10xgenomics.com/samples/xenium/2.0.0/Xenium_V1_human_Lung_2fov/Xenium_V1_human_Lung_2fov_outs.zip |

---

## Tier 3 — Full Public Datasets (GBs) — Stress Testing & Showcase

Full-scale datasets for final performance validation and demo. Download selectively — these are large.

Available via the 10x Genomics datasets portal: https://www.10xgenomics.com/datasets

Notable options:
- Human Breast Cancer (full, multiple replicates) — most widely used Xenium reference dataset
- Human Lung Cancer — with multimodal segmentation
- Human Renal Cell Carcinoma (FFPE, Protein + Gene Expression) — tests protein panel rendering
- Human Ovarian Cancer (Xenium Prime 5K) — high gene panel count
- Human Lymph Node (Xenium Prime 5K preview)
- Human Pancreatic Cancer
- Mouse Brain — important for testing non-human tissue and neuronal morphology

---

## Other Public Sources

### Zenodo
- Spatial transcriptomics (Xenium) from early postnatal lung: https://zenodo.org/records/17155546 (~10–11 GB, 5 ROIs)
- Xenium benchmarking data (AnnData format): https://zenodo.org/records/11120307
- Imaging spatial transcriptomics platform benchmarking: https://zenodo.org/records/16848917
- Human brain tissue in situ sequencing: https://zenodo.org/records/15425563
- Retrosplenial cortex single-cell spatial: https://zenodo.org/records/16697913

### NCBI GEO
- GSE264334: https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE264334
- GSE300007 — multi-platform imaging spatial transcriptomics comparison (includes Xenium raw data)

---

## Recommended Download Script

Run from the repo root. Downloads Tier 1 (all tiny datasets) and the two Tier 2 datasets.

```bash
#!/bin/bash
mkdir -p sample_data
cd sample_data

# Tier 1 — tiny datasets
curl -O https://cf.10xgenomics.com/samples/xenium/3.0.0/Xenium_Prime_MultiCellSeg_Mouse_Ileum_tiny/Xenium_Prime_MultiCellSeg_Mouse_Ileum_tiny_outs.zip
curl -O https://cf.10xgenomics.com/samples/xenium/3.0.0/Xenium_Prime_Mouse_Ileum_tiny/Xenium_Prime_Mouse_Ileum_tiny_outs.zip
curl -O https://cf.10xgenomics.com/samples/xenium/4.0.0/Xenium_V1_Protein_Human_Kidney_tiny/Xenium_V1_Protein_Human_Kidney_tiny_outs.zip
curl -O https://cf.10xgenomics.com/samples/xenium/4.0.0/Xenium_V1_Human_Ovary_tiny/Xenium_V1_Human_Ovary_tiny_outs.zip
curl -O https://cf.10xgenomics.com/samples/xenium/4.0.0/Xenium_V1_MultiCellSeg_Human_Ovary_tiny/Xenium_V1_MultiCellSeg_Human_Ovary_tiny_outs.zip

# Tier 2 — 2-FOV performance datasets
curl -O https://cf.10xgenomics.com/samples/xenium/2.0.0/Xenium_V1_human_Breast_2fov/Xenium_V1_human_Breast_2fov_outs.zip
curl -O https://cf.10xgenomics.com/samples/xenium/2.0.0/Xenium_V1_human_Lung_2fov/Xenium_V1_human_Lung_2fov_outs.zip

# Unzip all
for f in *.zip; do
  dirname="${f%.zip}"
  unzip "$f" -d "$dirname" && rm "$f"
done

echo "Done. Datasets in sample_data/:"
ls -lh
```

---

## Dataset Feature Coverage Matrix

Use this to select datasets that exercise specific viewer features during development.

| Feature to test | Recommended dataset |
|---|---|
| Basic tile rendering + pan/zoom | Mouse Ileum tiny (smallest, fastest) |
| MultiCellSeg segmentation overlay | Mouse Ileum MultiCellSeg or Ovary MultiCellSeg tiny |
| Protein panel / immunostain layers | Human Kidney tiny (protein panel) |
| Human tissue + dense transcripts | Human Breast 2-FOV |
| Large morphology image performance | Human Lung 2-FOV |
| High gene panel count | Human Ovarian Cancer (Xenium Prime 5K) — Tier 3 |
| Non-human / neuronal tissue | Mouse Brain — Tier 3 |
| v3 format compatibility | Any v3.0.0 dataset |
| v4 format compatibility | Any v4.0.0 dataset |

---

## Notes on Xenium Output Format Versions

The Xenium output folder structure has evolved across software versions. Key differences that may affect parsing:

- **v2.x**: `transcripts.csv.gz` + `transcripts.parquet` both present; `cells.zarr.zip` for cell metadata
- **v3.x**: Introduces Xenium Prime panel support; MultiCellSeg segmentation option added
- **v4.x**: Latest format; protein panel support; updated cell segmentation outputs

The backend Xenium reader should detect the version from `experiment.xenium` and handle format differences gracefully. Testing against both v3 and v4 tiny datasets during development is strongly recommended.

---

## Reference

- 10x Genomics Xenium example data index: https://www.10xgenomics.com/support/software/xenium-onboard-analysis/latest/resources/xenium-example-data
- Xenium output file format documentation: https://cf.10xgenomics.com/supp/xenium/xenium_documentation.html
- 10x Genomics public datasets portal: https://www.10xgenomics.com/datasets
