# Visium HD Tiny Mouse Brain — provenance and licence

A **subset** of 10x Genomics' public "Visium HD 3' Tiny Mouse Brain" dataset, kept in this
repository as the Visium HD test fixture.

| | |
|---|---|
| Source | https://www.10xgenomics.com/support/software/space-ranger/latest/resources/visium-hd-example-data |
| Download | `https://cf.10xgenomics.com/samples/spatial-exp/4.0.1/Visium_HD_Tiny_3prime_Dataset/Visium_HD_Tiny_3prime_Dataset_outs.zip` |
| Original size | 311,404,970 bytes · MD5 `7cab710801d3776125a7aa5a792cd7e4` |
| Space Ranger | 4.0.1 |
| Licence | **CC BY 4.0** — https://creativecommons.org/licenses/by/4.0/ |
| Attribution | © 10x Genomics. Used and redistributed under CC BY 4.0. |

10x produced this "Tiny" dataset themselves by downsampling a full run to a corner of the
tissue at reduced resolution. It is a genuine Space Ranger `outs/` tree, not a synthetic
mock, so it exercises the real directory layout.

## What was kept, and why

The original archive is 297 MB (616 MB unpacked, 162 files). Only what the reader actually
needs is committed here — about 26 MB:

```
binned_outputs/square_008um/   filtered_feature_bc_matrix.h5
                              spatial/{tissue_positions.parquet,
                                       scalefactors_json.json,
                                       tissue_hires_image.png}
binned_outputs/square_016um/   the same, plus tissue_lowres_image.png
spatial/tissue_hires_image.png the top-level morphology copy
segmented_outputs/cell_segmentations.geojson
metrics_summary.csv
```

Two bin sizes are kept so bin selection can be exercised; `square_002um` is omitted because
its positions file alone is 160 MB. The CytAssist TIFF (27.5 MB, duplicated four times in
the original) is omitted in favour of the 2.4 MB hires PNG.

`segmented_outputs/cell_segmentations.geojson` (951 real cell polygons, Space Ranger 4.x)
is **not currently read**. It is kept because it is only 0.57 MB and it is the input for a
possible future mode where Visium HD renders segmented cells instead of bins — re-fetching
it later would mean downloading the full 297 MB again.

## Numbers worth knowing

- `square_008um`: 702,244 bins total, **20,830 in tissue**
- `square_016um`: 175,561 bins total, **5,262 in tissue**
- Morphology `tissue_hires_image.png`: 6000 × 5394 RGB
- Very sparse — 32,062 total UMIs across the 8 µm bins, max 20 in any one bin, and about
  half the in-tissue bins have no counts at all. Gene-set colouring will look mostly empty,
  and that is the data, not a bug.

## The trap this fixture cannot catch

`tissue_hires_scalef` is **1.0** here and `microns_per_pixel` is **1.003** — both
effectively identity. Any code that forgets to multiply bin coordinates by
`tissue_hires_scalef` when overlaying the hires image, or that confuses pixels with
microns, will still look perfectly correct against this dataset and break on every real
one, where the scale factor runs roughly 0.02–0.2 and `microns_per_pixel` is nearer 0.25.
Treat a passing render here as necessary but not sufficient.
