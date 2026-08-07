# visium_tiny — synthetic classic Visium fixture

Generated, not real data. Reproduce with:

```bash
python sample_data/make_visium.py --out sample_data/visium_tiny
Rscript r/niches_visium.R sample_data/visium_tiny --rad 150
```

Synthetic because real Space Ranger output is a few hundred MB and the public 10x
Visium datasets are not redistributable from this repo. 788 KB here, including the
NICHESv2 edge layer.

## What it is for

Unlike `visium_hd_tiny`, which is real 10x data, this exists to make one specific
bug impossible to reintroduce.

`tissue_hires_scalef` is **0.08**, not 1.0.

Space Ranger reports spot positions in full-resolution pixels but only ships the
*hires* PNG, so coordinates must be multiplied by that factor to land on the image.
`visium_hd_tiny` has the factor set to exactly 1.0, so the multiply being absent
was invisible there — and it was in fact absent for a whole release, while the
reader docstring claimed otherwise. The check that catches it:

| | spots on image |
|---|---|
| with the multiply | 252 / 252 |
| without it | 0 / 252 |

### `spot_diameter_fullres` is the detected footprint, not the capture spot

The second thing this fixture exists for, and it was learned the hard way.

A real `scalefactors_json.json` carries no `microns_per_pixel`, so `pixel_size` must
be derived. The first version of this generator set
`spot_diameter_fullres = 55 µm / microns_per_pixel`, which made the fixture a
**tautology**: whatever derivation the reader used, the fixture agreed with it. The
reader derived from the 55 µm capture spot and the fixture happily confirmed it.

Real data says otherwise. On `V1_Mouse_Kidney` and `V1_Adult_Mouse_Brain` the in-row
lattice pitch is exactly 138.00 fullres px while `spot_diameter_fullres` is 89.45 — a
ratio of 0.648, so that field measures a ~64.8 µm *detected* footprint, not the 55 µm
capture spot. Deriving from 55 µm is 18% wrong.

The generator now emits the real ratio (259.2 px for a 0.25 µm/px scan), so:

| derivation | pixel_size | measured nearest-neighbour |
|---|---|---|
| 100 µm lattice pitch (correct) | 3.125 | **100.0 µm** |
| 55 µm capture spot (the bug) | 2.652 | 84.9 µm |

which is the same failure signature the real datasets show (99.3 vs 84.2 µm).

## What it is not

The lattice geometry, the units and the file formats are faithful. **The biology is
not.** Counts are Poisson noise with three arbitrary x-axis domains over a 36-gene
panel chosen to form complete connectomedb2025 ligand-receptor pairs, so NICHESv2
has something to score. Nothing here should be read as a result.

The reader has since been verified against real Space Ranger output — see the classic
Visium section of `docs/public_datasets.md`. That verification is what corrected the
pixel-size derivation above, which is the standing argument for not trusting a
synthetic fixture on its own: it can only test what you already believed.
