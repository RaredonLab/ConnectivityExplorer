# Issue #60 — local neighbourhood highlighting and summaries

Status: **implemented** in v0.8.7. Kept as the design record.

`GET /edges/{dataset}/neighborhood/{cell_id}?field=<cell column>` — unfiltered
and unsampled. The panel shows a **show local neighbourhood** button under the
cell detail; the canvas draws the connected cells, the clicked cell, and the
enclosing radius.

Upstream ask
([#60](https://github.com/RaredonLab/TissuePlex/issues/60)): click a cell, press a
button, and (a) see the local neighbourhood — the cells it is connected to in the
tissue graph — drawn on the canvas, and (b) get summary metrics: composition by
cell type or selected metadata, number of neighbours, number of local edges,
local edge composition.

---

## The trap: this cannot be computed client-side

The obvious implementation is to filter the `edges` array the frontend already
holds. It would appear to work and be **silently wrong**, for two independent
reasons:

- that array is **density-sampled** — 10% by default, so nine of ten neighbours
  are missing;
- it is **viewport-bounded**, so a neighbour just off-screen does not exist as
  far as the client is concerned, and the answer changes when you pan.

A neighbourhood is a property of the tissue, not of the current view. It needs a
backend query, and that query must ignore both density and the viewport.

Cost is not a concern — measured on the incident-edge scan:

| dataset | rows in file | incident rows | scan |
|---|---|---|---|
| cosmx-mousebrain | 3,819,698 | 172 | **15 ms** |
| xenium_human_breast_2fov | 134,277 | 30 | 3 ms |
| mouse_ileum_tiny | 669 | 30 | 1 ms |

No index, no cache.

## Definition

**The neighbourhood is every cell joined to the clicked cell by any edge in the
file** — unfiltered and unsampled, consistent with the tissue graph being ground
truth (issue #59). Direction is ignored for membership: a cell that only *sends*
to the clicked cell is still a neighbour.

Deliberately *not* affected by: density, the viewport, the sending/receiving
filters, the edge-table filters, the cell filter, or the LRM checklist. Those
control what is drawn; this describes what the tissue is.

## Drawing it

The issue asks for "a local radius that encompasses all of the cells within that
cell's neighborhood". Two things get drawn, because the radius alone would
mislead:

- **the connected cells themselves**, highlighted — this is the honest answer, since
  connectivity is anisotropic. A cell at a tissue boundary has neighbours on one
  side only, and a disc around it encloses many cells it is not connected to.
- **a circle at the enclosing radius**, which is what was asked for and gives the
  spatial scale at a glance.

The radius is also reported numerically, in µm via the panel's `pixel_size`, so
it can be quoted without measuring off the screen.

## Summary metrics

| metric | source |
|---|---|
| neighbours | distinct partner cells |
| local edges | directed edges incident to the cell |
| autocrine | whether the cell signals to itself |
| radius | max centroid distance, µm |
| composition | any cell metadata column, counted over the neighbours |
| edge composition | top LRMs over the incident edges, by score |

Composition uses the **cells table**, not the edge file's `sending_type` — same
reasoning as #59: that column is absent on five of six platforms, carries one
label, and is frozen at scoring time. Any cell metadata column is offerable, and
the panel defaults to the active colour-by field when there is one, since that is
what the user is already looking at.

## Surface

| | |
|---|---|
| `backend/app/readers/edge_reader.py` | `neighborhood(cell_id)` — incident edges, partners, radius, LRM composition |
| `backend/app/routers/edges.py` | `GET /{dataset}/neighborhood/{cell_id}`; joins cell metadata for composition |
| `frontend/src/components/CellInfoPanel.jsx` | a **Neighbourhood** button and the summary block |
| `frontend/src/components/Viewer.jsx` | two layers — highlighted neighbours, and the radius circle |
| `frontend/src/store.js` | `neighborhood` selection state, cleared with the cell selection |

The `/edge/{edge_id}` detail endpoint is the precedent for shape and for how the
info panel consumes it.

## Consequences to get right

- **Per panel.** The highlight belongs to the panel that was clicked, like
  annotations and selection. `selection` already carries `panelIndex`.
- **Clearing.** Selecting another cell, changing dataset, or closing the panel must
  drop the highlight, or it strands over unrelated tissue.
- **Platforms without boundaries.** MERSCOPE and CosMx return no polygons, so the
  highlight cannot be a filled cell outline everywhere. Draw it as points at the
  edge-file centroids, which every platform has.
- **A cell with no edges** is a normal outcome, not an error — the panel should say
  "no connections" rather than render an empty summary.
