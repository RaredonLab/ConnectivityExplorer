/**
 * Dataset / image / edge-source picker for ONE panel.
 *
 * Its own module because both LayerPanel (single-panel mode, in the sidebar)
 * and Viewer (split mode, in each panel header) render it. Importing it from
 * LayerPanel instead would close the cycle Viewer -> LayerPanel -> App -> Viewer,
 * which ESM tolerates but which resolves to `undefined` often enough to be worth
 * avoiding outright.
 */
import React, { useEffect, useState } from "react";
import { useStore } from "../store";

const SECTION_HEADER = {
  fontSize: 10, fontFamily: "monospace", color: "#555",
  textTransform: "uppercase", letterSpacing: 1,
  marginTop: 14, marginBottom: 6, paddingBottom: 3,
  borderBottom: "1px solid #2a2a2a",
};

const SELECT_STYLE = {
  background: "#252525", color: "#ccc", border: "1px solid #3a3a3a",
  borderRadius: 3, padding: "2px 4px", fontFamily: "monospace",
  fontSize: 11, cursor: "pointer", width: "100%",
};

/**
 * Dataset + image picker for ONE panel.
 *
 * Exported because in split mode it renders inside each panel's header rather
 * than in the sidebar: with two panels able to show two datasets, a single
 * sidebar picker has no well-defined meaning. In single-panel mode LayerPanel
 * still renders it at the top, where it has always been.
 */
export function DatasetPicker({ panelIndex = 0, compact = false }) {
  const apiBase = useStore((s) => s.apiBase);
  const setPanelDataset = useStore((s) => s.setPanelDataset);
  const patchPanel = useStore((s) => s.patchPanel);
  const { dataset, activeImage } = useStore((s) => s.panels[panelIndex]);
  const setPanelEdgeFile = useStore((s) => s.setPanelEdgeFile);
  const edgeFile = useStore((s) => s.panels[panelIndex].edgeFile);
  const [datasets, setDatasets] = useState([]);
  const [images, setImages] = useState([]);
  const [edgeFiles, setEdgeFiles] = useState([]);

  // Fetch dataset list; auto-initialize to first entry if this panel has none.
  useEffect(() => {
    fetch(`${apiBase}/spatial/datasets`)
      .then((r) => r.ok ? r.json() : [])
      .then((list) => {
        if (!Array.isArray(list)) return;
        setDatasets(list);
        if (list.length > 0) {
          const cur = useStore.getState().panels[panelIndex].dataset;
          if (!cur || !list.includes(cur)) setPanelDataset(panelIndex, list[0]);
        }
      })
      .catch(() => {});
  }, [apiBase, panelIndex]); // eslint-disable-line react-hooks/exhaustive-deps

  // Fetch available images for this panel's dataset
  useEffect(() => {
    if (!dataset) return;
    fetch(`${apiBase}/spatial/${dataset}/images`)
      .then((r) => r.ok ? r.json() : [])
      .then((list) => { if (Array.isArray(list)) setImages(list); })
      .catch(() => setImages([]));
  }, [apiBase, dataset]); // eslint-disable-line react-hooks/exhaustive-deps

  // Edge sources for this panel's dataset (issue #46). Kept beside the dataset
  // picker because an edge file only means anything relative to one dataset.
  useEffect(() => {
    if (!dataset) { setEdgeFiles([]); return; }
    fetch(`${apiBase}/edges/${dataset}/files`)
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        const files = Array.isArray(data?.files) ? data.files : [];
        setEdgeFiles(files);
        const ids = files.map((f) => f.id);
        const cur = useStore.getState().panels[panelIndex].edgeFile;
        if (files.length > 0 && !ids.includes(cur)) {
          setPanelEdgeFile(panelIndex, data.default ?? ids[0]);
        }
      })
      .catch(() => setEdgeFiles([]));
  }, [apiBase, dataset, panelIndex]); // eslint-disable-line react-hooks/exhaustive-deps

  // Keep activeImage valid for whatever images the current dataset offers.
  // This is a separate, declarative effect rather than a branch inside the fetch
  // above because the fetch only re-runs on dataset change: with activeImage
  // captured in its closure, whether it got set depended on the order the two
  // state updates landed in, which left datasets whose only image is the
  // synthesised placeholder with activeImage stuck at null and no viewer at all.
  useEffect(() => {
    if (images.length > 0 && !images.includes(activeImage)) {
      patchPanel(panelIndex, { activeImage: images[0] });
    }
  }, [images, activeImage, panelIndex]); // eslint-disable-line react-hooks/exhaustive-deps

  if (datasets.length === 0) {
    return (
      <div style={{ marginBottom: compact ? 0 : 12, color: "#555", fontSize: 11, fontFamily: "monospace" }}>
        Connecting…
      </div>
    );
  }

  const sel = compact
    ? { ...SELECT_STYLE, fontSize: 10, padding: "1px 3px", width: "auto", maxWidth: 190 }
    : SELECT_STYLE;

  const edgePicker = edgeFiles.length > 1 && (
    <select value={edgeFile || ""} onChange={(e) => setPanelEdgeFile(panelIndex, e.target.value)}
            style={sel} title="Edge source for this panel">
      {edgeFiles.map((f) => <option key={f.id} value={f.id}>{f.label}</option>)}
    </select>
  );

  if (compact) {
    // Panel header: one line, no section labels — the panel it sits on is the label.
    return (
      <div style={{ display: "flex", gap: 5, alignItems: "center" }}>
        <select value={dataset || ""} onChange={(e) => setPanelDataset(panelIndex, e.target.value)}
                style={sel} title="Dataset shown in this panel">
          {datasets.map((d) => <option key={d} value={d}>{d}</option>)}
        </select>
        {images.length > 1 && (
          <select value={activeImage || ""}
                  onChange={(e) => patchPanel(panelIndex, { activeImage: e.target.value })}
                  style={sel} title="Background image">
            {images.map((img) => <option key={img} value={img}>{img}</option>)}
          </select>
        )}
        {edgePicker}
      </div>
    );
  }

  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ ...SECTION_HEADER, marginTop: 0 }}>Dataset</div>
      <select value={dataset || ""} onChange={(e) => setPanelDataset(panelIndex, e.target.value)}
              style={sel}>
        {datasets.map((d) => <option key={d} value={d}>{d}</option>)}
      </select>
      {images.length > 1 && (
        <>
          <div style={{ ...SECTION_HEADER, marginTop: 8 }}>Image</div>
          <select value={activeImage || ""}
                  onChange={(e) => patchPanel(panelIndex, { activeImage: e.target.value })}
                  style={sel}>
            {images.map((img) => <option key={img} value={img}>{img}</option>)}
          </select>
        </>
      )}
      {edgeFiles.length > 1 && (
        <>
          <div style={{ ...SECTION_HEADER, marginTop: 8 }}>Edge source</div>
          {edgePicker}
        </>
      )}
    </div>
  );
}
