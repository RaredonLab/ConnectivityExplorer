# Edge UI Redesign Plan
# Based on NICHESv2 data structure (NICHESv2_package_design.md)
# 2026-05-02

---

## New edge data schema (already generated)

`edges.parquet` now mirrors NICHESv2's `$edge.data + $edge.meta`:

| column | type | notes |
|---|---|---|
| `edge` | string | `"SendingCell\|ReceivingCell"` — NICHESv2 edge ID |
| `sending_cell` | string | barcode |
| `receiving_cell` | string | barcode (== sending_cell for autocrine) |
| `is_autocrine` | bool | SendingCell == ReceivingCell |
| `lrm` | string | `"ligand\|receptor"` — NICHESv2 LRM ID |
| `lrm_id` | int | legacy integer, kept for backward compat |
| `ligand` | string | |
| `receptor` | string | |
| `score` | float | raw LR product score |
| `score_norm` | float | score / sum(score within edge) |
| `x1, y1` | float | sending cell centroid (µm) |
| `x2, y2` | float | receiving cell centroid (µm), == x1,y1 for autocrine |
| `sending_type` | string | simulated cell type |
| `receiving_type` | string | simulated cell type |

Demo counts (mouse_ileum_tiny): 669 rows, 216 directed edges + 7 autocrine self-loops.

---

## Implementation components

### A. Backend (edge_reader.py + edges.py router)

**A1. Update `lrm_catalogue()`**
Return string `lrm` field alongside existing `lrm_id`, `ligand`, `receptor`.
```
[{ lrm_id: 1, lrm: "Tgfb1|Tgfbr1", ligand: "Tgfb1", receptor: "Tgfbr1" }, ...]
```

**A2. New `POST /{dataset}/edge-color-values`** (analogous to `/xenium/{dataset}/color-values`)

Request body:
```json
{ "mode": "lrm_set",  "lrms": ["Tgfb1|Tgfbr1", "Il6|Il6ra"] }
{ "mode": "metadata", "field": "sending_type" }
```

Response (lrm_set, continuous):
```json
{ "type": "continuous", "values": {"edge_id": score, ...}, "min": 0, "max": 6.2 }
```

Response (metadata, categorical):
```json
{ "type": "categorical", "values": {"edge_id": "Epithelial", ...}, "categories": [...] }
```

Implementation:
- **lrm_set**: group by `edge`, filter rows where `lrm` in requested list, sum `score` per edge → return edge→sum map + global min/max
- **metadata**: group by `edge`, take first value of `field` per edge (all rows in a directed edge share the same metadata), auto-detect categorical vs continuous by same logic as cell color

**A3. New `GET /{dataset}/edge/{edge_id}`** — edge click info panel

Returns all rows for a specific `edge` value (all LRMs for that directed pair):
```json
{
  "edge": "cellA|cellB",
  "sending_cell": "cellA", "receiving_cell": "cellB",
  "sending_type": "Epithelial", "receiving_type": "Immune",
  "is_autocrine": false,
  "lrms": [
    { "lrm": "Tgfb1|Tgfbr1", "lrm_id": 1, "score": 3.18, "score_norm": 0.33 },
    ...
  ]
}
```

---

### B. Frontend state (store.js)

Add to existing store:
```js
// Directional/autocrine rendering
showAutocrine: true,
setShowAutocrine: (v) => set({ showAutocrine: v }),
edgeDirectional: true,
setEdgeDirectional: (v) => set({ edgeDirectional: v }),

// Edge color (parallel to cell color)
edgeColorPalette: "viridis",
setEdgeColorPalette: (p) => set({ edgeColorPalette: p }),

// Edge click info
selectedEdge: null,
setSelectedEdge: (edge) => set({ selectedEdge: edge }),
```

Migrate `hiddenLrms` from `Set<int>` → `Set<string>` (string `lrm` IDs like `"Tgfb1|Tgfbr1"`).
Update `lrmCatalogue` type accordingly (store `lrm` string alongside integer ID).
Change `edgeColorBy.mode` from `"default"|"lrm_expression"|"metadata"` → `"default"|"lrm_set"|"metadata"`.

---

### C. New hook: `useEdgeColors` (frontend/src/hooks/useEdgeColors.js)

Mirrors `useCellColors`. Called in `Viewer.jsx`.

```js
useEdgeColors(apiBase, dataset, edgeColorBy, hiddenLrms, lrmCatalogue, palette, enabled)
// Returns: { colorValues: Map<edge_id, [r,g,b,a]>, type, vmin, vmax, categories, categoryColors, loading }
```

- `enabled` = `edgeColorBy.mode !== "default"`
- **lrm_set mode**: POST selected LRMs (all in `lrmCatalogue` minus `hiddenLrms`) → map edge→color
- **metadata mode**: POST field → map edge→color

---

### D. Edge rendering in Viewer.jsx

The EdgeLayer currently renders one line per row (one per edge×LRM). The new rendering pipeline:

**Step 1 — client-side aggregation** (useMemo in Viewer)
Group fetched edge rows by `edge` string:
```js
const edgeFeatures = useMemo(() => {
  // For each unique directed edge, compute:
  //   sourcePos, targetPos (in image pixel coords)
  //   totalScore = sum of score for visible LRMs
  //   is_autocrine, sending_cell, receiving_cell
  // Filter out hidden LRMs but keep edge if any LRM is visible
  ...
}, [edgeData, hiddenLrms, edgeMinStrength]);
```

**Step 2 — perpendicular offset** (when `edgeDirectional = true`)
For each directed edge A→B, compute a small perpendicular offset (+N px left-normal of the direction vector). This separates A→B visually from B→A since both exist in the data.
Offset magnitude: ~4 screen pixels, applied in the `getSourcePosition`/`getTargetPosition` accessors.

**Step 3 — LineLayer for directed edges**
```js
new LineLayer({
  data: edgeFeatures.filter(e => !e.is_autocrine),
  getSourcePosition: e => e.sourcePos,   // with perpendicular offset applied
  getTargetPosition: e => e.targetPos,
  getColor: e => colorValues?.get(e.edge) ?? DEFAULT_EDGE_COLOR,
  getWidth: 1.5,
  pickable: true,
  onClick: ({ object }) => setSelectedEdge(object.edge),
})
```

**Step 4 — ScatterplotLayer for autocrine edges** (when `showAutocrine = true`)
```js
new ScatterplotLayer({
  data: edgeFeatures.filter(e => e.is_autocrine),
  getPosition: e => e.sourcePos,
  getRadius: 12,    // ring around the cell
  filled: false,
  stroked: true,
  getLineColor: e => colorValues?.get(e.edge) ?? DEFAULT_EDGE_COLOR,
  getLineWidth: 1.5,
  pickable: true,
  onClick: ({ object }) => setSelectedEdge(object.edge),
})
```

**Step 5 — direction arrow indicators** (when `edgeDirectional = true`)
A separate `ScatterplotLayer` places small filled triangle markers at the midpoint of each directed edge, pointing toward the receiving cell. Rendered as small filled dots (5px radius) as a simple direction hint; a TextLayer rendering "▶" at midpoint is an alternative.

---

### E. LayerPanel.jsx — EdgeSection redesign

Current EdgeSection has: opacity slider, strength slider, LRM checklist with all/none, color-by picker.

New EdgeSection layout:
```
[✓] Edges  [opacity slider]
  [✓] Directional  [✓] Show autocrine
  
  ── Edge Color ─────────────────
  ○ Default (gray)
  ○ LRM Set   <N of M LRMs selected>
  ○ Metadata  [column dropdown]
              [palette picker] ← only for continuous
  [continuous legend or categorical legend]
  
  ── LRM Filter ─────────────────
  [All] [None]
  [✓] Tgfb1|Tgfbr1
  [✓] Il6|Il6ra
  ...

  ── Edge Filter ────────────────
  Min strength: [slider]
```

`ContinuousLegend` and `CategoricalLegend` reuse the same components as the cell color section.

The LRM checklist switches from integer `lrm_id` to string `lrm` IDs. Each row shows `ligand → receptor` (formatted from the `lrm` string).

---

### F. EdgeInfoPanel.jsx (new component)

A floating panel anchored to the bottom-right of the viewport (or as a side panel). Appears when `selectedEdge != null`.

```
┌─ Edge: cellA → cellB ─────────────── [×] ┐
│ Sending:   cellA  (Epithelial)            │
│ Receiving: cellB  (Immune)                │
│                                           │
│ LRM Scores                                │
│ ─────────────────────────────────────     │
│ Tgfb1|Tgfbr1    3.18  (33.4%)            │
│ Il6|Il6ra       3.11  (32.6%)            │
│ Efnb1|EphB2     3.25  (34.1%)            │
│                                           │
│ [Autocrine]  ← badge if is_autocrine      │
└───────────────────────────────────────────┘
```

Fetches from `GET /edges/{dataset}/edge/{edge_id}` on `selectedEdge` change.
Dismiss: click [×] or click elsewhere on the map.

---

## Implementation order

1. **Backend A1–A3** — update lrm_catalogue, add edge-color-values endpoint, add edge detail endpoint
2. **Store B** — add showAutocrine, edgeDirectional, selectedEdge, migrate hiddenLrms to string IDs
3. **useEdgeColors hook C** — new hook parallel to useCellColors
4. **Viewer edge rendering D** — aggregation, directional offset, LineLayer + autocrine ScatterplotLayer
5. **LayerPanel EdgeSection E** — redesigned with new controls
6. **EdgeInfoPanel F** — new click info component

Total estimated complexity: ~same as the cell color redesign. Each step is self-contained and testable.

---

## What does NOT change

- The edge bbox query (`GET /edges/{dataset}/query`) — still fetches all rows within viewport, still returns long-format data. The frontend does client-side aggregation.
- Existing `edgeMinStrength` filter — still applied during aggregation step.
- Layer visibility / opacity controls — unchanged.
- The broader annotation/region/measurement system — untouched.
