# Issue #59 — independent sending / receiving edge filters

Status: **planned, not started.**

Upstream ask
([#59](https://github.com/RaredonLab/TissuePlex/issues/59)): designate a sending
cell type and a receiving cell type independently, and see the resulting graph
structure. The issue asks whether deriving this from *cell* metadata per edge is
efficient. It is — the machinery already exists and the added cost is one extra
hash semi-join.

Two directives from the lab shape this beyond what the issue text says, and both
change current behaviour:

1. **The tissue graph is ground truth and must not be filtered.** It is the total
   set of edges, shown or hidden, never subset. Only the edge-data layer drawn on
   top of it responds to filters.
2. **Cell filtration and edge filtration are completely independent.** Filtering
   cells must not remove edges, and filtering edges must not remove cells.
3. **Density is applied last, after every other filter.** It is a rendering-volume
   control, not a selection criterion.

---

## What already works

Half of #59 ships today and is worth knowing before building anything.
`sending_type` and `receiving_type` are real populated columns in
`edges.parquet`, they are not in `EDGE_FILTER_SKIP`, so the edge filter dropdown
already offers them. Measured on `mouse_ileum_tiny`:

| filter | edges | sending types in result | receiving types in result |
|---|---|---|---|
| none | 223 | all five | all five |
| `sending_type = Fibroblast` | 55 | `Fibroblast` only | all five |

So one-sided filtering works. **The blocker is that `edgeFilter` holds exactly
one filter** — the composition gap deliberately deferred in issue #45 — so
"sending = Fibroblast *and* receiving = Endothelial" cannot be expressed. That
limit, not the sending/receiving distinction, is what needs fixing.

## Directive 3 already holds — preserve it

Density is already last on the edge path, and this is worth stating as an
invariant because it is easy to break by accident. The grouped query samples the
*outer* select, wrapping the filtered and grouped subquery:

```sql
SELECT * FROM (
    SELECT …
    FROM edges
    WHERE <bbox AND cell-ids AND edge-filter>
    GROUP BY edge
    HAVING lrm_count >= 1
) USING SAMPLE <density> PERCENT (bernoulli)
LIMIT 500000
```

Everything selective runs first; the sample then draws from whatever survived, so
narrowing to a rare subset renders that subset at full density instead of a tenth
of it. The nesting is load-bearing — applied alongside the `WHERE`, DuckDB is free
to sample before filtering, which silently reintroduces the problem.

Every new predicate in this plan therefore belongs in the same `WHERE` clause, not
in post-processing, and the new tissue-graph endpoint must keep the same shape.

**This is also why the tissue graph needs its own query rather than a flag on the
shared one** — see below. Filter-then-sample and don't-filter-then-sample are
different orderings of the same pipeline, and one statement cannot do both.

## The three current couplings to break

`useEdges` issues **one** request whose result feeds **both** the tissue-graph
layer and the directed-edge layer (`Viewer.jsx`: `id: "tissue-graph"`,
`data: allDirectedEdges`). That request carries `cell_filter`, `edge_filter` and
`density`. Consequently, today:

- filtering **cells** removes edges, because `query_grouped(cell_ids=…)` emits
  `sending_cell IN S AND receiving_cell IN S`;
- filtering **edges** thins the tissue graph, because both layers read one array;
- the tissue graph is density-sampled by the same slider as the edge data.

LRM filtering is already correctly decoupled — the tissue graph reads `lrm_count`
while the edge layer reads `visible_lrm_count`, so hiding mechanisms leaves the
structural graph intact. That is the pattern the other two filters should follow.

## Design

### The tissue graph gets its own fetch

Not a flag on the shared result, and the reason is not obvious. The two layers
want *opposite* things from sampling:

- the tissue graph wants **all** edges, then sampled for drawing volume;
- the edge data wants **filtered first, then sampled**, so that narrowing to a
  rare subset draws that subset at full density rather than a tenth of it. That
  ordering is the whole point of resolving filters server-side (issue #45).

One query cannot do both, because density runs last (above). Returning every edge
with a `passes_filter` boolean would sample the rare subset away along with
everything else, quietly undoing #45 — the flag would be evaluated *after* the
sample had already thinned the rows it applies to. So: two requests, each keeping
filter-then-sample internally, with separate density controls.

Sizing, measured across the bundled datasets — unique directed edges:

| dataset | rows | unique edges | file |
|---|---|---|---|
| cosmx-mousebrain | 3,819,698 | 169,219 | 31 MB |
| visium_hd_tiny | 66,002 | 66,001 | 0.8 MB |
| xenium_human_breast_2fov | 134,277 | 44,759 | 3.2 MB |
| merscope-vpt-smallset | 21,587 | 14,503 | 0.3 MB |

Rendering 169K–300K line segments is not the constraint; deck.gl handles that.
The payload is — `query-grouped` returns ~15 fields per edge, so a whole-tissue
view of a real run is tens of MB. The tissue graph therefore keeps a density
slider of its own, and the existing 500K-row cap stays as the backstop. A
**lean projection** for this endpoint (`x1,y1,x2,y2` and nothing else) is worth
doing at the same time: the structural layer draws lines and needs no scores,
types or LRM counts, which is most of the payload.

### Edge filters become a list, and gain two cell-derived slots

`edgeFilter` becomes `edgeFilters: MetadataFilter[]`, and-ed together. That alone
closes #45's deferred half and makes "sending = X and receiving = Y" expressible
against `edges.parquet` columns.

For predicates on cell metadata that is *not* in `edges.parquet` — anything from
`cell-metadata/`, or a column the R export did not copy — two new spec slots
resolve through the existing cell path:

```
sendingFilter   : MetadataFilter | null   →  filter_cell_ids() → id set S_send
receivingFilter : MetadataFilter | null   →  filter_cell_ids() → id set S_recv
```

`SpatialDatasetReader.filter_cell_ids(spec)` already resolves a cell-metadata
predicate to an id set and caches per (reader, spec). `duck.register_ids()`
already turns a large set into a hash semi-join rather than an unusable
`IN (?, ?, …)` list. The only change in `edge_reader.py` is to stop applying one
set to both ends:

```python
# now
pred = duck.register_ids(conn, cell_ids, name="tp_cell_filter")
f'CAST("sending_cell" AS VARCHAR) {pred} AND CAST("receiving_cell" AS VARCHAR) {pred}'

# after — two frames, two predicates, either side optional
send = duck.register_ids(conn, sending_ids,   name="tp_send_filter")
recv = duck.register_ids(conn, receiving_ids, name="tp_recv_filter")
```

**Answering the issue's efficiency question directly:** this is the same cost as
today. One hash semi-join becomes two, on a query that already performs one, and
both run in the WHERE clause before the GROUP BY and before sampling.

### Cell filtering stops touching edges

`cellFilter` governs the cell layers only. The `cell_ids` argument stays on
`query_grouped` — the new sending/receiving slots use it — but the frontend
stops passing the cell-layer filter into the edge request.

**This reverses a decision recorded in CLAUDE.md**, which argued that an edge
should be drawn only when both endpoints are, since "a half-outside edge would
run off to a cell that is not drawn". The lab's position is that edge structure
is worth seeing independently of which cells are rendered, so edges may now
terminate on cells that are not drawn. That is intended, not an oversight, and
the docs need correcting rather than the behaviour.

## Consequences to get right

- **Autocrine edges** have `sending_cell == receiving_cell`, so an autocrine edge
  survives only when the cell satisfies *both* sides. Correct, but it means
  setting sending and receiving to disjoint groups hides every autocrine ring.
  Worth a note in the manual rather than a special case in code.
- **Direction is meaningless for the tissue graph**, which is undirected and now
  unfiltered anyway — so the question does not arise. This is the main reason
  directive 1 simplifies the feature rather than complicating it.
- **An empty id set means "nothing matches"**, and `register_ids` refuses an
  empty frame. `query_grouped` already short-circuits `cell_ids == set()` to
  `FALSE`; both new slots need the same guard, or a filter matching no cells
  silently returns every edge.
- **`edgeFilter` → `edgeFilters` is a store migration.** Anything persisted or
  copied between panels (`sanitiseSettings`) must handle both shapes, and the
  push guard must validate every filter in the list, not just the first.

## Surface

| | |
|---|---|
| `backend/app/readers/edge_reader.py` | split the cell-id predicate; accept a filter list |
| `backend/app/routers/edges.py` | request model gains `sending_filter`, `receiving_filter`, `edge_filters`; new lean structural endpoint |
| `frontend/src/hooks/useEdges.js` | second unfiltered fetch for the graph; stop sending `cell_filter` |
| `frontend/src/components/Viewer.jsx` | tissue-graph layer reads the new array |
| `frontend/src/components/LayerPanel.jsx` | sending/receiving pickers; own density slider for the graph |
| `frontend/src/store.js` | `edgeFilters` list, `sendingFilter`, `receivingFilter`, `tissueGraphDensity` |

`MetadataFilterSection` is already generic over a field list and a value fetcher,
so the two new pickers reuse it against the *cell* schema rather than the edge
schema.

## Staging

**1 — decouple, no new features.** Tissue graph gets its own unfiltered fetch and
density; `cellFilter` stops reaching the edge request. Directives 1 and 2, with
no change to what can be expressed. Independently useful and independently
reviewable.

**2 — `edgeFilter` becomes a list.** Closes #45's deferred half; makes
sending/receiving expressible wherever the columns exist in `edges.parquet`.

**3 — cell-derived sending/receiving slots.** The two id sets, for metadata that
is not in the edge file.

**4 — lean structural projection** for the tissue-graph endpoint, if the payload
proves to be the limit on real data. Measure before doing it.

**5 — docs.** CLAUDE.md's "cellFilter also governs edges" statement is wrong
after stage 1, in the store section and the metadata-subsetting section. The
manual's filter section needs the same correction, plus the autocrine note.

## Testing

Frontend store tests exist now (Vitest, 46), and the filter-targeting logic is
plain reducer code — the `edgeFilters` migration and the push-guard change belong
there. On the backend, the decisive check is a query-level one: for a dataset with
populated `sending_type`, assert that filtering sending alone leaves the receiving
distribution untouched (verified by hand above: 55 edges, one sending type, all
five receiving types), and that the tissue-graph endpoint returns an identical
count with and without every filter applied.
