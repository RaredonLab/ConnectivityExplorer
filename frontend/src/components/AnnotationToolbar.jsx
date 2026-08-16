/**
 * Floating annotation toolbar — Pan / Draw Region / Measure / Clear
 * + per-panel rotation controls (⟲ / angle input / ⟳).
 */
import React from "react";
import { useStore } from "../store";

const MODES = [
  { id: "pan",    label: "Pan",        title: "Pan & zoom (default)" },
  { id: "region", label: "⬡ Region",   title: "Draw annotation region — click vertices, double-click to close" },
  { id: "measure",label: "⟷ Measure",  title: "Measure distance — click two points" },
];

const BTN = {
  background: "transparent",
  border: "none",
  borderRadius: 4,
  padding: "3px 8px",
  fontFamily: "monospace",
  fontSize: 11,
  cursor: "pointer",
};

const SEP = { width: 1, background: "#333", margin: "2px 2px" };

export default function AnnotationToolbar({ onScreenshot, panelIndex = 0 }) {
  const {
    annotationMode, setAnnotationMode, clearAnnotations, regions, measurements,
    panelCount, setPanelCount,
    requestZoomMatch,
    panelRotations, setPanelRotation,
  } = useStore();

  // Count and clear only this panel's own annotations: the toolbar is rendered
  // per panel, so a Clear here wiping the other panel's work would be a
  // surprise, and the button greying out because the *other* panel is empty
  // would be worse.
  const mine = (a) => a.filter((x) => (x.panelIndex ?? 0) === panelIndex);
  const hasAnnotations = mine(regions).length > 0 || mine(measurements).length > 0;
  const isSplit = panelCount >= 2;
  const rotation = panelRotations[panelIndex] ?? 0;

  const rotate = (delta) => setPanelRotation(panelIndex, rotation + delta);

  return (
    <div style={{
      position: "absolute",
      top: 10,
      left: "50%",
      transform: "translateX(-50%)",
      zIndex: 20,
      display: "flex",
      alignItems: "center",
      gap: 2,
      background: "rgba(20,20,20,0.85)",
      border: "1px solid #333",
      borderRadius: 6,
      padding: "3px 4px",
      pointerEvents: "all",
    }}>
      {MODES.map(({ id, label, title }) => (
        <button
          key={id}
          title={title}
          onClick={() => setAnnotationMode(id)}
          style={{
            ...BTN,
            background: annotationMode === id ? "#3a3a3a" : "transparent",
            color: annotationMode === id ? "#fff" : "#888",
            outline: annotationMode === id ? "1px solid #555" : "none",
          }}
        >
          {label}
        </button>
      ))}

      <div style={SEP} />

      <button title="Save screenshot as PNG" onClick={onScreenshot} style={{ ...BTN, color: "#888" }}>
        Save PNG
      </button>

      {/* ── Rotation controls ─────────────────────────────── */}
      <div style={SEP} />
      <button
        title="Rotate 90° counter-clockwise"
        onClick={() => rotate(-90)}
        style={{ ...BTN, color: "#888", padding: "3px 6px" }}
      >
        ⟲
      </button>
      <input
        type="number"
        min="0"
        max="359"
        step="1"
        value={rotation}
        title="Rotation angle (0–359°)"
        onChange={(e) => {
          const v = parseFloat(e.target.value);
          if (!isNaN(v)) setPanelRotation(panelIndex, v);
        }}
        style={{
          width: 38,
          background: rotation !== 0 ? "#1a2a1a" : "transparent",
          border: "1px solid #444",
          borderRadius: 3,
          color: rotation !== 0 ? "#9f9" : "#888",
          fontFamily: "monospace",
          fontSize: 11,
          textAlign: "center",
          padding: "2px 2px",
        }}
      />
      <span style={{ color: "#555", fontFamily: "monospace", fontSize: 11, marginLeft: 1 }}>°</span>
      <button
        title="Rotate 90° clockwise"
        onClick={() => rotate(90)}
        style={{ ...BTN, color: "#888", padding: "3px 6px" }}
      >
        ⟳
      </button>
      {rotation !== 0 && (
        <button
          title="Reset rotation to 0°"
          onClick={() => setPanelRotation(panelIndex, 0)}
          style={{ ...BTN, color: "#9f9", fontSize: 10, padding: "3px 5px" }}
        >
          ×
        </button>
      )}

      {panelIndex === 0 && (
        <>
          <div style={SEP} />
          <button
            title={isSplit ? "Return to single panel" : "Split view — compare two areas side by side"}
            onClick={() => setPanelCount(isSplit ? 1 : 2)}
            style={{
              ...BTN,
              color: isSplit ? "#7ab8f5" : "#888",
              outline: isSplit ? "1px solid #3a5a80" : "none",
              background: isSplit ? "#1a2a3a" : "transparent",
            }}
          >
            {isSplit ? "□ Single" : "⊞ Split"}
          </button>
        </>
      )}

      {isSplit && (
        <>
          <div style={SEP} />
          <button
            title={panelIndex === 0
              ? "Navigate panel 2 to match this view"
              : "Navigate panel 1 to match this view"}
            onClick={() => requestZoomMatch(panelIndex)}
            style={{ ...BTN, color: "#888" }}
          >
            ⇔ Match
          </button>
        </>
      )}

      {hasAnnotations && (
        <>
          <div style={SEP} />
          <button
            title="Clear this panel's annotations and measurements"
            onClick={() => clearAnnotations(panelIndex)}
            style={{ ...BTN, color: "#c44" }}
          >
            Clear
          </button>
        </>
      )}
    </div>
  );
}
