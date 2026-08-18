/**
 * Cell info panel — shown when the user clicks a cell.
 * Fetches /spatial/{dataset}/cells/{cell_id} and displays metadata + expression.
 */
import React, { useEffect, useState } from "react";
import { useStore } from "../store";
import { usePanelSettings } from "../hooks/usePanelSettings";

const ROW = { display: "flex", justifyContent: "space-between", marginBottom: 3 };
const KEY = { color: "#666" };
const VAL = { color: "#ccc", textAlign: "right", marginLeft: 8, wordBreak: "break-all" };

export default function CellInfoPanel() {
  const apiBase = useStore((s) => s.apiBase);
  const selection = useStore((s) => s.selection);
  // Colour-by settings come from the panel that produced the click, not the tab
  // the sidebar happens to be on. Identical while the panels are linked; with
  // them unlinked, the active panel's gene selection would describe the wrong
  // cell.
  const { colorBy, cellColorEnabled, selectedGenes } =
    usePanelSettings(selection?.panelIndex ?? null);
  // Resolve against the panel that produced the click, not panel 0 — with two
  // datasets on screen, panel 0's would be the wrong one half the time.
  const selectedCell = selection?.kind === "cell" ? selection.cell : null;
  const owner = useStore((s) => s.panels[selection?.panelIndex ?? 0]);
  const dataset = owner?.dataset;
  const platformCapabilities = owner?.platformCapabilities;
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!selectedCell) { setDetail(null); return; }
    setLoading(true);
    fetch(`${apiBase}/spatial/${dataset}/cells/${selectedCell.cell_id}`)
      .then((r) => r.json())
      .then((d) => { setDetail(d); setLoading(false); })
      .catch(() => setLoading(false));
  }, [apiBase, dataset, selectedCell]);

  // Derive the color-by value for this cell from the already-loaded detail.
  // No extra fetch needed: metadata fields are in detail directly, and gene-set
  // sum is computed from detail.expression.
  const colorByInfo = cellColorEnabled && detail
    ? resolveColorByValue(detail, colorBy, selectedGenes)
    : null;

  return (
    <div style={{
      height: 260,
      borderTop: "1px solid #2a2a2a",
      padding: "10px 12px",
      color: "#ccc",
      fontFamily: "monospace",
      fontSize: 11,
      overflowY: "auto",
      background: "#1a1a1a",
    }}>
      <div style={{ fontWeight: "bold", marginBottom: 8, fontSize: 12, color: "#fff" }}>
        {(platformCapabilities?.unit_label ?? "Cell").charAt(0).toUpperCase() +
         (platformCapabilities?.unit_label ?? "Cell").slice(1)} Info
      </div>

      {!selectedCell && (
        <div style={{ color: "#444" }}>
          Click a {platformCapabilities?.unit_label ?? "cell"} to inspect
        </div>
      )}

      {loading && <div style={{ color: "#555" }}>Loading…</div>}

      {/* `detail` is state and outlives `selectedCell` by one render when the
          selection is cleared — changing dataset, for instance. Guarding on both
          stops stale detail being shown for a cell that is no longer selected,
          and stops the section below dereferencing a null selection. */}
      {detail && selectedCell && !loading && (
        <>
          {/* Color-by highlight — shown whenever cell coloring is active */}
          {colorByInfo && (
            <div style={{
              background: "#222",
              border: "1px solid #3a3a3a",
              borderRadius: 3,
              padding: "5px 7px",
              marginBottom: 7,
            }}>
              <div style={{ fontSize: 9, color: "#555", textTransform: "uppercase", letterSpacing: 1, marginBottom: 3 }}>
                {colorByInfo.label}
              </div>
              <div style={{ color: "#6cf", fontSize: 13, fontWeight: "bold" }}>
                {colorByInfo.display}
              </div>
            </div>
          )}

          {/* Core identity */}
          <MetaRow k="cell_id" v={detail.cell_id} />
          <MetaRow k="x" v={detail.x_centroid?.toFixed(1)} />
          <MetaRow k="y" v={detail.y_centroid?.toFixed(1)} />

          {/* Counts */}
          <Divider />
          <MetaRow k="transcripts" v={detail.transcript_counts} />
          <MetaRow k="total counts" v={detail.total_counts} />
          {/* Guarded like nucleus_area below: without the null check the optional
              chain yields undefined and the concatenation renders "undefined µm²"
              on any platform that does not report a cell area. */}
          {detail.cell_area != null && (
            <MetaRow k="cell area" v={detail.cell_area.toFixed(1) + " µm²"} />
          )}
          {detail.nucleus_area != null && (
            <MetaRow k="nucleus area" v={detail.nucleus_area.toFixed(1) + " µm²"} />
          )}

          {/* Expression */}
          {detail.expression && Object.keys(detail.expression).length > 0 && (
            <>
              <Divider label="expression" />
              {Object.entries(detail.expression)
                .sort((a, b) => b[1] - a[1])
                .map(([gene, count]) => (
                  <MetaRow key={gene} k={gene} v={count} accent />
                ))}
            </>
          )}

          <NeighborhoodSection
            dataset={dataset} cellId={selectedCell.cell_id}
            panelIndex={selection?.panelIndex ?? 0}
            unitLabel={platformCapabilities?.unit_label ?? "cell"}
          />
        </>
      )}
    </div>
  );
}

/**
 * Issue #60 — what this cell is connected to in the tissue graph.
 *
 * Fetched on demand rather than with the cell detail: it is a second query, and
 * most clicks are just "what is this cell". The result comes from the server
 * unfiltered and unsampled — see the store's `neighborhood` note for why it
 * cannot be derived from the edges the frontend already holds.
 */
function NeighborhoodSection({ dataset, cellId, panelIndex, unitLabel }) {
  const apiBase = useStore((s) => s.apiBase);
  const neighborhood = useStore((s) => s.neighborhood);
  const setNeighborhood = useStore((s) => s.setNeighborhood);
  const clearNeighborhood = useStore((s) => s.clearNeighborhood);
  const panel = useStore((s) => s.panels[panelIndex]);
  const { colorBy } = usePanelSettings(panelIndex);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const shown = neighborhood?.cellId === cellId ? neighborhood.data : null;

  const load = async () => {
    setLoading(true); setError(null);
    // Break the neighbourhood down by whatever the user is already colouring by;
    // asking them to pick a column again would repeat a choice they just made.
    const field = colorBy?.mode === "metadata" && colorBy.field ? colorBy.field : null;
    const ef = `?edge_file=${encodeURIComponent(panel?.edgeFile ?? "edges.parquet")}`;
    const q = field ? `${ef}&field=${encodeURIComponent(field)}` : ef;
    try {
      const r = await fetch(
        `${apiBase}/edges/${dataset}/neighborhood/${encodeURIComponent(cellId)}${q}`);
      if (!r.ok) { setError(`HTTP ${r.status}`); return; }
      setNeighborhood({ panelIndex, cellId, data: await r.json() });
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  const pixelSize = panel?.pixelSize ?? 1;

  return (
    <>
      <Divider label="neighbourhood" />
      {!shown && (
        <button
          onClick={load} disabled={loading}
          style={{
            width: "100%", padding: "4px 6px", marginBottom: 4,
            fontFamily: "monospace", fontSize: 11, cursor: "pointer",
            background: "transparent", color: "#8af",
            border: "1px solid #3a3a3a", borderRadius: 3,
          }}
        >
          {loading ? "loading…" : `show local neighbourhood`}
        </button>
      )}
      {error && <div style={{ color: "#c66", fontSize: 10 }}>{error}</div>}

      {shown && shown.n_neighbors === 0 && (
        <div style={{ color: "#777", fontSize: 10, marginBottom: 4 }}>
          This {unitLabel} has no connections in the tissue graph.
        </div>
      )}

      {shown && shown.n_neighbors > 0 && (
        <>
          <MetaRow k="neighbours" v={shown.n_neighbors} accent />
          <MetaRow k="local edges" v={shown.n_edges} />
          <MetaRow k="radius" v={`${shown.radius_um.toFixed(1)} µm`} />
          {shown.n_autocrine > 0 && <MetaRow k="autocrine" v="yes" />}

          {shown.composition?.length > 0 && (
            <>
              <Divider label={`by ${shown.composition_field}`} />
              {shown.composition.map((c) => (
                <MetaRow key={c.value} k={c.value}
                         v={`${c.n}  (${Math.round(c.n / shown.n_neighbors * 100)}%)`} />
              ))}
              {shown.composition_missing > 0 && (
                <MetaRow k="(no value)" v={shown.composition_missing} />
              )}
            </>
          )}
          {!shown.composition && (
            <div style={{ fontSize: 9, color: "#555", marginTop: 3 }}>
              Colour cells by a metadata column to see composition.
            </div>
          )}

          {shown.lrm_composition?.length > 0 && (
            <>
              <Divider label={`mechanisms (by ${shown.score_basis})`} />
              {shown.lrm_composition.map((m) => (
                <MetaRow key={m.lrm} k={m.lrm}
                         v={shown.score_basis === "score"
                            ? m.value.toFixed(2) : m.value} />
              ))}
            </>
          )}

          <button
            onClick={clearNeighborhood}
            style={{
              width: "100%", marginTop: 6, padding: "3px 6px",
              fontFamily: "monospace", fontSize: 10, cursor: "pointer",
              background: "transparent", color: "#888",
              border: "1px solid #3a3a3a", borderRadius: 3,
            }}
          >
            hide highlight
          </button>
        </>
      )}
    </>
  );
}

/**
 * Given a loaded cell detail and the current color-by state, return
 * { label, display } for the highlighted box, or null if nothing to show.
 */
function resolveColorByValue(detail, colorBy, selectedGenes) {
  if (!colorBy || colorBy.mode === "off") return null;

  if (colorBy.mode === "metadata" && colorBy.field) {
    const val = detail[colorBy.field];
    if (val == null) return null;
    const display = typeof val === "number"
      ? (Number.isInteger(val) ? String(val) : val.toFixed(4))
      : String(val);
    return { label: colorBy.field, display };
  }

  if (colorBy.mode === "gene_set") {
    const expr = detail.expression ?? {};
    let sum = 0;
    if (selectedGenes === null) {
      // All genes shown — sum everything in the expression dict
      sum = Object.values(expr).reduce((a, b) => a + b, 0);
      return { label: "gene set (all genes)", display: String(sum) };
    } else {
      selectedGenes.forEach((g) => { sum += expr[g] ?? 0; });
      const geneList = selectedGenes.size <= 3
        ? [...selectedGenes].sort().join(", ")
        : `${selectedGenes.size} genes`;
      return { label: `gene set (${geneList})`, display: String(sum) };
    }
  }

  return null;
}

function MetaRow({ k, v, accent }) {
  return (
    <div style={ROW}>
      <span style={KEY}>{k}</span>
      <span style={{ ...VAL, color: accent ? "#e8c84a" : "#ccc" }}>{v ?? "—"}</span>
    </div>
  );
}

function Divider({ label }) {
  return (
    <div style={{
      borderTop: "1px solid #2a2a2a",
      marginTop: 5, marginBottom: 5,
      fontSize: 9, color: "#444",
      textTransform: "uppercase", letterSpacing: 1,
    }}>
      {label}
    </div>
  );
}
