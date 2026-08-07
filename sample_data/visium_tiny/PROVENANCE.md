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

It also forces the *derived* pixel-size path, because a real classic Visium
`scalefactors_json.json` carries no `microns_per_pixel` — only the four keys 10x
actually writes. The generator uses 0.25 µm per fullres pixel and a 220 px spot
diameter; `55 / 220 = 0.25` recovers it exactly, and the NICHESv2 script's
independent check on the lattice reports a median nearest-neighbour distance of
100.0 µm, which is the Visium pitch.

## What it is not

The lattice geometry, the units and the file formats are faithful. **The biology is
not.** Counts are Poisson noise with three arbitrary x-axis domains over a 36-gene
panel chosen to form complete connectomedb2025 ligand-receptor pairs, so NICHESv2
has something to score. Nothing here should be read as a result.

No real Space Ranger `outs/` tree has been through this reader yet. A passing render
here is necessary, not sufficient — the same caveat `visium_hd_tiny/PROVENANCE.md`
makes, for the same reason.
