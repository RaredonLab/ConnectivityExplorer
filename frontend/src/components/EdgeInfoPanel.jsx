/**
 * Floating info panel shown when the user clicks a directed edge or autocrine ring.
 * Fetches all LRM rows for the selected edge from GET /edges/{dataset}/edge/{edge_id}.
 */
import React, { useEffect, useState } from "react";

const PANEL_STYLE = {
  position: "absolute",
  bottom: 48,
  right: 12,
  width: 260,
  background: "rgba(18,18,18,0.95)",
  border: "1px solid #3a3a3a",
  borderRadius: 6,
  fontFamily: "monospace",
  fontSize: 11,
  color: "#ccc",
  boxShadow: "0 4px 20px rgba(0,0,0,0.7)",
  zIndex: 50,
  pointerEvents: "all",
  overflow: "hidden",
};

const ROW_STYLE = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "baseline",
  padding: "3px 0",
  borderBottom: "1px solid #222",
};

export default function EdgeInfoPanel({ apiBase, dataset, edgeId, edgeFile = "edges.parquet", onClose }) {
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!edgeId) return;
    setLoading(true);
    setDetail(null);
    const efParam = `?edge_file=${encodeURIComponent(edgeFile)}`;
    fetch(`${apiBase}/edges/${dataset}/edge/${encodeURIComponent(edgeId)}${efParam}`)
      .then((r) => (r.ok ? r.json() : null))
      .then(setDetail)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [apiBase, dataset, edgeId, edgeFile]);

  const totalScore = detail?.lrms?.reduce((s, r) => s + (r.score ?? 0), 0) ?? 0;

  return (
    <div style={PANEL_STYLE}>
      {/* Header */}
      <div style={{
        display: "flex", justifyContent: "space-between", alignItems: "center",
        padding: "6px 10px", borderBottom: "1px solid #2a2a2a",
        background: "rgba(255,150,0,0.08)",
      }}>
        <span style={{ fontSize: 10, color: "#f90", fontWeight: "bold", letterSpacing: 0.5 }}>
          {detail?.is_autocrine ? "AUTOCRINE" : "EDGE"}
        </span>
        <button
          onClick={onClose}
          style={{
            background: "none", border: "none", color: "#777",
            fontSize: 14, cursor: "pointer", padding: "0 2px", lineHeight: 1,
          }}
        >×</button>
      </div>

      <div style={{ padding: "8px 10px" }}>
        {loading && <div style={{ color: "#555", fontSize: 10 }}>loading…</div>}

        {detail && (
          <>
            {/* Cell identifiers */}
            <div style={{ marginBottom: 6 }}>
              <CellRow label="Send" cell={detail.sending_cell} type={detail.sending_type} />
              {!detail.is_autocrine && (
                <CellRow label="Recv" cell={detail.receiving_cell} type={detail.receiving_type} />
              )}
            </div>

            {/* User annotations from the dataset's edge-metadata/ folder.
                Rendered generically so any column the user adds shows up without
                a frontend change, matching how cell-metadata reaches the cell panel. */}
            {detail.metadata && Object.keys(detail.metadata).length > 0 && (
              <div style={{ marginBottom: 7 }}>
                <div style={{ fontSize: 9, color: "#555", marginBottom: 3, textTransform: "uppercase", letterSpacing: 0.8 }}>
                  Annotations
                </div>
                {Object.entries(detail.metadata).map(([k, v]) => (
                  <div key={k} style={{ ...ROW_STYLE, borderBottom: "none", padding: "1px 0" }}>
                    <span style={{ color: "#666", overflow: "hidden", textOverflow: "ellipsis",
                                   whiteSpace: "nowrap", maxWidth: 130 }} title={k}>{k}</span>
                    <span style={{ color: "#8cf", flexShrink: 0, marginLeft: 6, textAlign: "right",
                                   overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 110 }}
                          title={String(v)}>
                      {typeof v === "number" && !Number.isInteger(v) ? v.toFixed(3) : String(v)}
                    </span>
                  </div>
                ))}
              </div>
            )}

            {/* LRM table */}
            <div style={{ fontSize: 9, color: "#555", marginBottom: 3, textTransform: "uppercase", letterSpacing: 0.8 }}>
              LRM scores ({detail.lrms?.length ?? 0} mechanisms)
            </div>
            <div style={{ maxHeight: 160, overflowY: "auto" }}>
              {(detail.lrms ?? []).map((r, i) => {
                const lrmLabel = r.lrm ?? `${r.ligand}|${r.receptor}`;
                const pct = totalScore > 0 ? ((r.score ?? 0) / totalScore * 100).toFixed(0) : "—";
                return (
                  <div key={i} style={ROW_STYLE}>
                    <span style={{
                      color: "#aaa", overflow: "hidden", textOverflow: "ellipsis",
                      whiteSpace: "nowrap", maxWidth: 150,
                    }} title={lrmLabel}>
                      {r.ligand} → {r.receptor}
                    </span>
                    <span style={{ color: "#f90", flexShrink: 0, marginLeft: 6 }}>
                      {(r.score ?? 0).toFixed(2)}
                      <span style={{ color: "#555", marginLeft: 4 }}>({pct}%)</span>
                    </span>
                  </div>
                );
              })}
            </div>

            {/* Total */}
            <div style={{
              display: "flex", justifyContent: "space-between",
              marginTop: 5, paddingTop: 5, borderTop: "1px solid #2a2a2a",
            }}>
              <span style={{ color: "#666" }}>total</span>
              <span style={{ color: "#fff", fontWeight: "bold" }}>{totalScore.toFixed(2)}</span>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function CellRow({ label, cell, type }) {
  return (
    <div style={{ display: "flex", alignItems: "baseline", gap: 6, marginBottom: 3 }}>
      <span style={{ color: "#555", width: 28, flexShrink: 0 }}>{label}</span>
      <span style={{ color: "#ddd", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 }}
            title={cell}>{cell}</span>
      {type && (
        <span style={{ color: "#888", flexShrink: 0, fontSize: 10 }}>{type}</span>
      )}
    </div>
  );
}
