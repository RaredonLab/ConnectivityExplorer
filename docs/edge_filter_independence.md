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

## What already works, and why it does not count

`sending_type` / `receiving_type` are in `edges.parquet` and are not in
`EDGE_FILTER_SKIP`, so the edge filter dropdown already offers them. Filtering
one side works mechanically — measured on `mouse_ileum_tiny`:

| filter | edges | sending types in result | receiving types in result |
|---|---|---|---|
| none | 223 | all five | all five |
| `sending_type = Fibroblast` | 55 | `Fibroblast` only | all five |

**This is not the feature, and the demo above is misleading.** Tracing where
those labels come from:

| source | populates the column? |
|---|---|
| `sample_data/make_edges.py` | yes — its own docstring says `cell type (simulated)` |
| `r/niches_xenium.R` | only with an explicit `--celltype <column>` flag |
| `niches_cosmx/merscope/seqfish/visium/visium_hd.R` | **no** — all pass `celltype.col = NULL` |

So the five tidy labels above (`Endothelial`, `Immune`, `Fibroblast`, …) are
**invented by the fixture generator**. On real output the column is absent on
five of six platforms as the export scripts are written today, and present on
Xenium only if the user opted in. It also carries exactly one column, where the
ask is "any cell metadata column" — `mouse_ileum_tiny`'s cells table has
`cluster`, `region`, `pseudotime` and `seurat_clusters`, none of which appears in
the edge file.

The conclusion is not "half of #59 ships" — it is that the mechanism exists but
is pointed at the wrong source. The real blocker is twofold: `edgeFilter` holds
exactly one filter (the composition gap deferred in #45), and the only column it
can reach is one that mostly is not there.

### Should `sending_type` exist at all?

Worth deciding separately, and it is a NICHESv2-side question more than a
TissuePlex one. The case against: it duplicates cell metadata into the edge
table, freezing it at scoring time, and creates two sources of truth for the same
question — re-annotate cells through `cell-metadata/` and the edge file still says
the old thing. The case for: if NICHESv2 *used* the label when scoring, then it is
provenance rather than duplication, and the honest record of what produced the
number. Which of those is true is not answerable from this repo.

Either way it should not back the two dropdowns. If it stays, document it as
"the label used at scoring time — may be absent, may be stale", and leave it
reachable through the edge-column filter where that framing is visible.

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

### Two cell-metadata pickers, sending and receiving

This is the feature, not an add-on to an edge-column filter. The edge section
gets **two dropdowns side by side** — *sending cell* and *receiving cell* — and
each selects **any cell metadata column**, the same vocabulary the cell filter
offers. An edge is drawn when **both** sides are satisfied: the intersection.

```
Sending cell                 Receiving cell
[ region        ▾ ]          [ cluster       ▾ ]
[x] crypt                    [x] 3
[ ] mid                      [ ] 4
[ ] villus                   [x] 7
```

Either side may be left unset, which leaves that end unconstrained — so one
dropdown alone answers "everything sent *from* crypt cells, to anywhere".

### Resolve through the cells table, never the edge file's own labels

Filtering on the edge file's own `sending_type` would be a plain SQL predicate,
cheaper than resolving an id set. It is still the wrong source, for three reasons
set out above: the column is **absent** on five of six platforms as the export
scripts stand, it carries **one** label where the ask is any column, and it is
**frozen** at scoring time so it can silently disagree with a re-annotated cells
table.

A fast path for the one case where it happens to be present would mean two code
paths answering the same question differently depending on which one ran — the
kind of disagreement that is very hard to notice in a figure.

So both dropdowns resolve through `filter_cell_ids()` against the cells table,
uniformly, with no fast path. The parquet's own labels stay reachable through the
separate edge-column filter, where they are honestly labelled as the as-scored
values.

### The backend change

`filter_cell_ids(spec)` already resolves a cell-metadata predicate to an id set
and caches per (reader, spec). `duck.register_ids()` already turns a large set
into a hash semi-join rather than an unusable `IN (?, ?, …)` list. The only change
in `edge_reader.py` is to stop applying one set to both ends:

```python
# now — one set, both endpoints
pred = duck.register_ids(conn, cell_ids, name="tp_cell_filter")
f'CAST("sending_cell" AS VARCHAR) {pred} AND CAST("receiving_cell" AS VARCHAR) {pred}'

# after — two frames, two predicates, either side optional
send = duck.register_ids(conn, sending_ids,   name="tp_send_filter")
recv = duck.register_ids(conn, receiving_ids, name="tp_recv_filter")
```

Both predicates go in the same `WHERE` clause, so they run before the `GROUP BY`
and before the sample — the ordering directive 3 requires.

**Answering the issue's efficiency question directly:** this is the same cost as
today. One hash semi-join becomes two, on a query that already performs one.

### Edge-column filters stay, and become a list

Separately from the two pickers, `edgeFilter` becomes `edgeFilters:
MetadataFilter[]`, and-ed together. This is what `edge-metadata/` annotations
(a confidence, a review flag) and the parquet's own columns are filtered through,
and it closes the composition gap deferred in #45. It is a smaller, independent
win — the two cell-metadata pickers are what #59 actually asks for.


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
there. On the backend the decisive checks are query-level, and should use a **cell
metadata column** rather than `sending_type`, which the fixture only simulates:
filtering sending alone must leave the receiving distribution untouched; the two
sides together must return the intersection; and the tissue-graph endpoint must
return an identical count with and without every filter applied, which is
directive 1 stated as an assertion.
