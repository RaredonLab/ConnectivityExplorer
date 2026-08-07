/**
 * Layer panel — toggle, opacity, color-by, and layer controls.
 */
import React, { useEffect, useRef, useState } from "react";
import { useStore } from "../store";
import { APP_VERSION } from "../App";
import { legendGradient, QUAL_PALETTE } from "../utils/colormap";
import { geneColor } from "../utils/geneColor";

// ── Color conversion helpers ──────────────────────────────────────────────────
function rgbaToHex([r, g, b]) {
  return "#" + [r, g, b].map((v) => v.toString(16).padStart(2, "0")).join("");
}
function hexToRgba(hex) {
  const h = hex.replace("#", "");
  return [parseInt(h.slice(0,2),16), parseInt(h.slice(2,4),16), parseInt(h.slice(4,6),16), 255];
}

const SECTION_HEADER = {
  fontSize: 10,
  fontFamily: "monospace",
  color: "#555",
  textTransform: "uppercase",
  letterSpacing: 1,
  marginTop: 14,
  marginBottom: 6,
  paddingBottom: 3,
  borderBottom: "1px solid #2a2a2a",
};

const LABEL_STYLE = {
  fontSize: 11,
  color: "#aaa",
  fontFamily: "monospace",
  userSelect: "none",
  cursor: "pointer",
  display: "flex",
  alignItems: "center",
  gap: 6,
};

const SELECT_STYLE = {
  background: "#252525",
  color: "#ccc",
  border: "1px solid #3a3a3a",
  borderRadius: 3,
  padding: "2px 4px",
  fontFamily: "monospace",
  fontSize: 11,
  cursor: "pointer",
  width: "100%",
};

const INPUT_STYLE = {
  ...SELECT_STYLE,
  marginTop: 4,
};

const PALETTE_OPTIONS = ["viridis", "plasma", "magma", "inferno"];

function DatasetPicker() {
  const { apiBase, dataset, setDataset, activeImage, setActiveImage } = useStore();
  const [datasets, setDatasets] = useState([]);
  const [images, setImages] = useState([]);

  // Fetch dataset list; auto-initialize to first entry if store has no valid dataset
  useEffect(() => {
    fetch(`${apiBase}/spatial/datasets`)
      .then((r) => r.ok ? r.json() : [])
      .then((list) => {
        if (!Array.isArray(list)) return;
        setDatasets(list);
        if (list.length > 0) {
          const cur = useStore.getState().dataset;
          if (!cur || !list.includes(cur)) setDataset(list[0]);
        }
      })
      .catch(() => {});
  }, [apiBase]); // eslint-disable-line react-hooks/exhaustive-deps

  // Fetch available images for the current dataset
  useEffect(() => {
    if (!dataset) return;
    fetch(`${apiBase}/spatial/${dataset}/images`)
      .then((r) => r.ok ? r.json() : [])
      .then((list) => {
        if (!Array.isArray(list)) return;
        setImages(list);
      })
      .catch(() => setImages([]));
  }, [apiBase, dataset]); // eslint-disable-line react-hooks/exhaustive-deps

  // Keep activeImage valid for whatever images the current dataset offers.
  // This is a separate, declarative effect rather than a branch inside the fetch
  // above because the fetch only re-runs on dataset change: with activeImage
  // captured in its closure, whether it got set depended on the order the two
  // state updates landed in, which left datasets whose only image is the
  // synthesised placeholder with activeImage stuck at null and no viewer at all.
  useEffect(() => {
    if (images.length > 0 && !images.includes(activeImage)) {
      setActiveImage(images[0]);
    }
  }, [images, activeImage]); // eslint-disable-line react-hooks/exhaustive-deps

  if (datasets.length === 0) {
    return (
      <div style={{ marginBottom: 12, color: "#555", fontSize: 11, fontFamily: "monospace" }}>
        Connecting…
      </div>
    );
  }

  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ ...SECTION_HEADER, marginTop: 0 }}>Dataset</div>
      <select
        value={dataset || ""}
        onChange={(e) => setDataset(e.target.value)}
        style={SELECT_STYLE}
      >
        {datasets.map((d) => (
          <option key={d} value={d}>{d}</option>
        ))}
      </select>
      {images.length > 1 && (
        <>
          <div style={{ ...SECTION_HEADER, marginTop: 8 }}>Image</div>
          <select
            value={activeImage || ""}
            onChange={(e) => setActiveImage(e.target.value)}
            style={SELECT_STYLE}
          >
            {images.map((img) => (
              <option key={img} value={img}>{img}</option>
            ))}
          </select>
        </>
      )}
    </div>
  );
}

export default function LayerPanel() {
  const { platformCapabilities, apiBase } = useStore();
  const hasTranscripts = platformCapabilities?.has_transcripts ?? true;
  const hasBoundaries  = platformCapabilities?.has_boundaries  ?? true;
  // A dataset with no morphology gets a placeholder canvas, so the opacity
  // control would be a dead toggle over a flat fill.
  const hasMorphology  = platformCapabilities?.has_morphology  ?? true;
  const unitLabel      = platformCapabilities?.unit_label ?? "cell";
  const unitTitle      = unitLabel.charAt(0).toUpperCase() + unitLabel.slice(1);

  // Show the build-time version immediately; check the backend version via /health
  // and append a warning if they diverge (useful during development).
  const [backendVersion, setBackendVersion] = useState(null);
  useEffect(() => {
    fetch(`${apiBase}/health`).then((r) => r.ok ? r.json() : null).then((d) => {
      if (d?.version) setBackendVersion(d.version);
    }).catch(() => {});
  }, [apiBase]); // eslint-disable-line
  const versionMismatch = backendVersion && backendVersion !== APP_VERSION;
  const versionLabel = versionMismatch
    ? `v${APP_VERSION} (api: v${backendVersion})`
    : `v${APP_VERSION}`;

  return (
    <div style={{
      flex: 1,
      overflowY: "auto",
      padding: "10px 12px",
      color: "#ccc",
      fontFamily: "monospace",
      fontSize: 12,
      background: "#1e1e1e",
      display: "flex",
      flexDirection: "column",
    }}>
      <DatasetPicker />
      <div style={{ fontWeight: "bold", marginBottom: 10, fontSize: 13, color: "#fff" }}>Layers</div>

      <div style={SECTION_HEADER}>Core</div>
      {hasMorphology && <MorphologyRow />}
      {hasTranscripts && <TranscriptLayerRow />}
      {hasBoundaries  && <CellSegmentsRow unitTitle={unitTitle} />}

      <div style={SECTION_HEADER}>{unitTitle} Color</div>
      <ColorBySection unitLabel={unitLabel} />

      {/* Issue #45. Placed under the color section because picking the column to
          subset on is the same act as picking the column to colour by, and users
          almost always do the two together. */}
      <div style={SECTION_HEADER}>{unitTitle} Filter</div>
      <CellFilterSection unitLabel={unitLabel} />

      {hasTranscripts && (
        <>
          <div style={SECTION_HEADER}>Transcript Species</div>
          <TranscriptSpeciesSection />
        </>
      )}

      <div style={SECTION_HEADER}>Tissue Graph</div>
      <TissueGraphSection />

      <DensityRow />

      <div style={SECTION_HEADER}>Edge Data</div>
      <EdgeSection />

      <RegionsSection />

      {/* Version badge — always visible, subtle */}
      <div style={{
        marginTop: "auto", paddingTop: 16,
        fontSize: 9, color: versionMismatch ? "#a66" : "#444",
        textAlign: "right", fontFamily: "monospace", userSelect: "none",
        title: "TissuePlex build version",
      }}>
        TissuePlex {versionLabel}
      </div>
    </div>
  );
}

// ── Color By section ──────────────────────────────────────────────────────────
function ColorBySection({ unitLabel = "cell" }) {
  const {
    platformCapabilities,
    apiBase, dataset,
    cellColorEnabled, setCellColorEnabled,
    colorBy, setColorBy,
    cellColorPalette, setCellColorPalette,
    allGenes, setAllGenes, setGenesLoaded,
    selectedGenes,
    cellColorRange,
    cellColorClamp, setCellColorClamp,
    categoricalOverrides, setCategoricalOverride,
    cellColorType, cellColorCategories,
  } = useStore();

  const [cellSchema, setCellSchema] = useState(null);
  const hasTranscripts = platformCapabilities?.has_transcripts ?? true;
  const unitTitle = unitLabel.charAt(0).toUpperCase() + unitLabel.slice(1);

  // Fetch full gene list and cell schema once per dataset
  useEffect(() => {
    // `dataset` is null until DatasetPicker resolves the list; fetching then
    // just 404s against /spatial/null/... and buries real errors in the console.
    if (!dataset) return;
    setGenesLoaded(false);
    fetch(`${apiBase}/spatial/${dataset}/genes`)
      .then((r) => r.ok ? r.json() : [])
      .then((g) => { if (Array.isArray(g)) setAllGenes(g); })
      .catch(() => {})
      .finally(() => setGenesLoaded(true));
    fetch(`${apiBase}/spatial/${dataset}/cells/schema`)
      .then((r) => r.ok ? r.json() : null)
      .then((s) => { if (s) setCellSchema(s); })
      .catch(() => {});
  }, [apiBase, dataset]); // eslint-disable-line react-hooks/exhaustive-deps

  const { mode, field } = colorBy;
  const selectedCount = selectedGenes === null ? allGenes.length : selectedGenes.size;

  // How the selected column is actually being coloured. This comes from the
  // backend's response (via the store, written by panel 0) rather than from the
  // schema dtype: the backend also treats a low-cardinality integer column as
  // categorical, so guessing from dtype used to draw a gradient legend with two
  // dead sliders over a canvas that was already showing discrete colours.
  const fieldDtype = field && cellSchema ? cellSchema.columns[field] : null;
  const isCategorical = mode === "metadata" && !!field && cellColorType === "categorical";

  // The override is only meaningful for a numeric column — a text column has no
  // gradient to fall back to, so there is nothing to offer.
  const isNumericField = !!fieldDtype &&
    /^(int|uint|float|Int|UInt|Float)/.test(fieldDtype);
  const overrideKey = `cell::${field}`;
  const override = categoricalOverrides[overrideKey] ?? null;

  return (
    <div style={{ marginBottom: 6 }}>
      {/* On/off toggle row */}
      <label style={{ ...LABEL_STYLE, marginBottom: 6 }}>
        <input
          type="checkbox"
          checked={cellColorEnabled}
          onChange={(e) => setCellColorEnabled(e.target.checked)}
          style={{ accentColor: "#6cf" }}
        />
        Color {unitLabel}s
      </label>

      {cellColorEnabled && (
        <>
          {/* Mode selector */}
          <select
            value={mode}
            onChange={(e) => { setColorBy(e.target.value, null); setCellColorClamp(null, null); }}
            style={SELECT_STYLE}
          >
            <option value="off">— choose mode —</option>
            <option value="gene_set">Gene set (selected species)</option>
            <option value="metadata">{unitTitle} metadata</option>
          </select>

          {/* Gene set info. The "use Transcript Species" pointer is only shown
              where that section exists — platforms without molecule coordinates
              (Visium, Visium HD) hide it, so the hint would name a control the
              user cannot find. Those platforms always sum the whole panel. */}
          {mode === "gene_set" && (
            <div style={{ marginTop: 4, fontSize: 10, color: "#888" }}>
              {selectedCount} of {allGenes.length} genes selected
              <span style={{ color: "#555" }}>
                {hasTranscripts ? " (use Transcript Species to adjust)" : " (whole panel)"}
              </span>
            </div>
          )}

          {/* Metadata column + palette */}
          {mode === "metadata" && (
            <>
              <select
                value={field ?? ""}
                onChange={(e) => { setColorBy("metadata", e.target.value || null); setCellColorClamp(null, null); }}
                style={{ ...SELECT_STYLE, marginTop: 4 }}
              >
                <option value="">— select column —</option>
                {cellSchema?.columns && Object.keys(cellSchema.columns).map((c) => (
                  <option key={c} value={c}>{c}</option>
                ))}
              </select>

              {/* Issue #35: integer-coded cluster IDs arrive as ints and would
                  otherwise be drawn as a gradient. Unchecking forces the reverse,
                  which is how you get a gradient over a column the auto-rule
                  called categorical. */}
              {isNumericField && (
                <label style={{ ...LABEL_STYLE, marginTop: 5, fontSize: 10, color: "#888" }}>
                  <input
                    type="checkbox"
                    checked={isCategorical}
                    onChange={(e) => {
                      setCategoricalOverride("cell", field, e.target.checked);
                      setCellColorClamp(null, null);
                    }}
                    style={{ accentColor: "#6cf" }}
                  />
                  treat as categorical
                  {override !== null && (
                    <button
                      onClick={(e) => {
                        e.preventDefault();
                        setCategoricalOverride("cell", field, null);
                      }}
                      title="Go back to auto-detection for this column"
                      style={{ ...CHIP_STYLE, marginLeft: 4, color: "#666" }}
                    >
                      auto
                    </button>
                  )}
                </label>
              )}
            </>
          )}

          {/* Palette picker — only for continuous modes */}
          {(mode === "gene_set" || (mode === "metadata" && field && !isCategorical)) && (
            <select
              value={cellColorPalette}
              onChange={(e) => setCellColorPalette(e.target.value)}
              style={{ ...SELECT_STYLE, marginTop: 4 }}
            >
              {PALETTE_OPTIONS.map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
          )}

          {/* Legend */}
          {mode === "gene_set" && (
            <ClampableLegend label="gene set expression" palette={cellColorPalette}
              vmin={cellColorRange.vmin} vmax={cellColorRange.vmax}
              clamp={cellColorClamp} setClamp={setCellColorClamp} accentColor="#6cf" />
          )}
          {mode === "metadata" && field && !isCategorical && (
            <ClampableLegend label={field} palette={cellColorPalette}
              vmin={cellColorRange.vmin} vmax={cellColorRange.vmax}
              clamp={cellColorClamp} setClamp={setCellColorClamp} accentColor="#6cf" />
          )}
          {mode === "metadata" && field && isCategorical && (
            <CategoricalLegend field={field} categories={cellColorCategories} />
          )}
        </>
      )}
    </div>
  );
}

function ClampableLegend({ label, palette, vmin, vmax, clamp, setClamp, accentColor = "#f90" }) {
  const fmt = (v) => (v == null ? "" : Math.abs(v) < 0.01 || Math.abs(v) >= 1000
    ? v.toExponential(1) : v.toFixed(2));
  const hasData = vmin != null && vmax != null && vmax > vmin;
  const low  = clamp?.low  ?? vmin;
  const high = clamp?.high ?? vmax;
  const step = hasData ? (vmax - vmin) / 200 : 0.01;
  const clamped = clamp?.low != null || clamp?.high != null;

  return (
    <div style={{ marginTop: 6 }}>
      <div style={{ height: 8, borderRadius: 2, background: legendGradient(palette), marginBottom: 2 }} />
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 9, color: "#555", marginBottom: hasData ? 4 : 0 }}>
        <span>{hasData ? fmt(low) : "low"}</span>
        <span style={{ color: "#666", overflow: "hidden", textOverflow: "ellipsis", maxWidth: 100 }}>{label}</span>
        <span>{hasData ? fmt(high) : "high"}</span>
      </div>
      {hasData && (
        <div style={{ fontSize: 9, color: "#444" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 4, marginBottom: 2 }}>
            <span style={{ width: 18, color: "#444" }}>lo</span>
            <input type="range" min={vmin} max={vmax} step={step}
              value={low ?? vmin}
              onChange={(e) => setClamp(parseFloat(e.target.value), clamp?.high ?? null)}
              style={{ flex: 1, accentColor, cursor: "pointer" }} />
            <span style={{ width: 36, textAlign: "right", color: "#555" }}>{fmt(low)}</span>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
            <span style={{ width: 18, color: "#444" }}>hi</span>
            <input type="range" min={vmin} max={vmax} step={step}
              value={high ?? vmax}
              onChange={(e) => setClamp(clamp?.low ?? null, parseFloat(e.target.value))}
              style={{ flex: 1, accentColor, cursor: "pointer" }} />
            <span style={{ width: 36, textAlign: "right", color: "#555" }}>{fmt(high)}</span>
          </div>
          {clamped && (
            <button onClick={() => setClamp(null, null)}
              style={{ ...CHIP_STYLE, marginTop: 3, color: "#888" }}>
              reset range
            </button>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Editable per-category swatches.
 *
 * `categories` comes from the store, where panel 0 records whatever the backend
 * returned for the active column. This component used to re-POST /color-values
 * for itself, which duplicated a request the viewer had already made and — once
 * the categorical override existed — would have asked without it, so the legend
 * could disagree with the canvas it describes.
 */
function CategoricalLegend({ field, categories = [] }) {
  const {
    categoryColorOverrides,
    setCategoryColorOverride,
    mergeCategoryColorOverrides,
    resetCategoryColorOverrides,
  } = useStore();

  const fileInputRef = useRef(null);

  // Resolve display color for a category: override → QUAL_PALETTE → hash
  // Must mirror the logic in useCellColors.js so legend stays in sync.
  function resolveColor(cat, i) {
    const override = categoryColorOverrides[`${field}::${cat}`];
    if (override) return override;
    return i < QUAL_PALETTE.length ? QUAL_PALETTE[i] : [...geneColor(cat), 255];
  }

  // Parse imported CSV: two columns — category label, hex color.
  // Header row is optional and auto-detected.
  function handleImportCSV(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    e.target.value = "";          // allow re-importing same file
    const reader = new FileReader();
    reader.onload = (ev) => {
      const lines = ev.target.result.split(/\r?\n/).filter((l) => l.trim());
      const map = {};
      for (const line of lines) {
        // Support comma or tab separators
        const sep = line.includes("\t") ? "\t" : ",";
        const parts = line.split(sep).map((p) => p.trim().replace(/^"|"$/g, ""));
        if (parts.length < 2) continue;
        const [label, hex] = parts;
        if (!hex || !hex.match(/^#?[0-9a-fA-F]{6}$/)) continue; // skip invalid / header
        const normalHex = hex.startsWith("#") ? hex : `#${hex}`;
        map[`${field}::${label}`] = hexToRgba(normalHex);
      }
      if (Object.keys(map).length > 0) mergeCategoryColorOverrides(map);
    };
    reader.readAsText(file);
  }

  if (!categories.length) return null;

  const hasOverrides = categories.some((cat) => categoryColorOverrides[`${field}::${cat}`]);

  return (
    <div style={{ marginTop: 6 }}>
      {/* Category rows */}
      {categories.map((cat, i) => {
        const [r, g, b] = resolveColor(cat, i);
        const hexVal = rgbaToHex([r, g, b]);
        return (
          <div key={cat} style={{ display: "flex", alignItems: "center", gap: 5, marginBottom: 3 }}>
            {/* Clickable swatch — wraps a hidden <input type="color"> */}
            <label title="Click to change color" style={{ cursor: "pointer", flexShrink: 0, position: "relative", display: "flex" }}>
              <div style={{
                width: 10, height: 10, borderRadius: 2,
                background: `rgb(${r},${g},${b})`,
                outline: "1px solid rgba(255,255,255,0.15)",
              }} />
              <input
                type="color"
                value={hexVal}
                onChange={(e) => setCategoryColorOverride(field, cat, hexToRgba(e.target.value))}
                style={{ position: "absolute", opacity: 0, width: 0, height: 0, pointerEvents: "none" }}
                tabIndex={-1}
              />
            </label>
            <span style={{ fontSize: 10, color: "#aaa", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 }}
                  title={cat}>{cat}</span>
          </div>
        );
      })}

      {/* Import / export / reset row */}
      <div style={{ display: "flex", gap: 6, marginTop: 6, alignItems: "center" }}>
        <button
          onClick={() => fileInputRef.current?.click()}
          title="Load a CSV with columns: category, #hexcolor"
          style={{
            fontSize: 9, fontFamily: "monospace", color: "#6cf",
            background: "none", border: "1px solid #2a4a5a", borderRadius: 3,
            padding: "2px 6px", cursor: "pointer",
          }}
        >
          import palette
        </button>
        <button
          onClick={() => {
            const rows = ["category,color",
              ...categories.map((cat, i) => {
                const [r, g, b] = resolveColor(cat, i);
                return `"${cat.replace(/"/g, '""')}",${rgbaToHex([r, g, b])}`;
              }),
            ];
            const blob = new Blob([rows.join("\n")], { type: "text/csv" });
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = `${field}_palette.csv`;
            a.click();
            URL.revokeObjectURL(url);
          }}
          title="Save current colors as a CSV file"
          style={{
            fontSize: 9, fontFamily: "monospace", color: "#6cf",
            background: "none", border: "1px solid #2a4a5a", borderRadius: 3,
            padding: "2px 6px", cursor: "pointer",
          }}
        >
          export palette
        </button>
        {hasOverrides && (
          <button
            onClick={resetCategoryColorOverrides}
            title="Restore default colors"
            style={{
              fontSize: 9, fontFamily: "monospace", color: "#888",
              background: "none", border: "1px solid #333", borderRadius: 3,
              padding: "2px 6px", cursor: "pointer",
            }}
          >
            reset
          </button>
        )}
        <input
          ref={fileInputRef}
          type="file"
          accept=".csv,.tsv,.txt"
          onChange={handleImportCSV}
          style={{ display: "none" }}
        />
      </div>
    </div>
  );
}

// ── Metadata filter (issue #45) ───────────────────────────────────────────────
/**
 * Restrict the view to a subset of units by one metadata column.
 *
 * One component serves both the cell filter and the edge filter — they differ
 * only in which endpoint supplies the column list and the distinct values, so
 * those arrive as props. The chosen filter is written to the store and travels
 * to the backend, which applies it *before* sampling; doing it client-side would
 * leave a sample of a subset rather than the subset.
 *
 * Two shapes, chosen by what the backend says the column is:
 *   categorical — checkboxes, one per value (this is the "focus on 2–3 cell
 *                 types" case from the issue)
 *   continuous  — inclusive min/max bounds
 */
function MetadataFilterSection({
  scope, columns, filter, setFilter, fetchValues, unitLabel = "cell",
}) {
  const [meta, setMeta] = useState(null);      // { type, categories, min, max }
  const [loading, setLoading] = useState(false);
  const field = filter?.field ?? "";

  // Load the distinct values / range for the selected column.
  useEffect(() => {
    if (!field) { setMeta(null); return; }
    let cancelled = false;
    setLoading(true);
    fetchValues(field)
      .then((d) => { if (!cancelled && d) setMeta(d); })
      .catch(() => { if (!cancelled) setMeta(null); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [field, fetchValues]);

  const selected = new Set(filter?.values ?? []);
  const active = (filter?.values?.length ?? 0) > 0 ||
                 filter?.min != null || filter?.max != null;

  function chooseField(next) {
    // Values and bounds belong to the old column; carrying them over would
    // silently filter on labels that do not exist in the new one.
    setFilter(next ? { field: next, values: null, min: null, max: null } : null);
  }

  function toggle(cat) {
    const next = new Set(selected);
    if (next.has(cat)) next.delete(cat); else next.add(cat);
    setFilter({ ...filter, values: next.size ? [...next] : null });
  }

  return (
    <div style={{ marginBottom: 6 }}>
      <select value={field} onChange={(e) => chooseField(e.target.value)} style={SELECT_STYLE}>
        <option value="">— no {unitLabel} filter —</option>
        {columns.map((c) => <option key={c} value={c}>{c}</option>)}
      </select>

      {field && loading && (
        <div style={{ fontSize: 9, color: "#555", marginTop: 4 }}>loading values…</div>
      )}

      {field && !loading && meta?.type === "categorical" && (
        <div style={{ marginTop: 5 }}>
          <div style={{ maxHeight: 150, overflowY: "auto", paddingRight: 2 }}>
            {(meta.categories ?? []).map((cat) => (
              <label key={cat} style={{ ...LABEL_STYLE, fontSize: 10, marginBottom: 2 }}>
                <input
                  type="checkbox"
                  checked={selected.has(cat)}
                  onChange={() => toggle(cat)}
                  style={{ accentColor: "#6cf" }}
                />
                <span style={{
                  overflow: "hidden", textOverflow: "ellipsis",
                  whiteSpace: "nowrap", flex: 1,
                }} title={cat}>{cat}</span>
              </label>
            ))}
          </div>
          <div style={{ display: "flex", gap: 6, marginTop: 4, alignItems: "center" }}>
            <button style={{ ...CHIP_STYLE, color: "#6cf" }}
                    onClick={() => setFilter({ ...filter, values: [...(meta.categories ?? [])] })}>
              all
            </button>
            <button style={{ ...CHIP_STYLE, color: "#888" }}
                    onClick={() => setFilter({ ...filter, values: null })}>
              none
            </button>
            <span style={{ fontSize: 9, color: "#555", marginLeft: "auto" }}>
              {/* No selection is "show everything", not "show nothing" — an empty
                  allowlist would blank the canvas the moment a column is picked. */}
              {selected.size
                ? `${selected.size} of ${(meta.categories ?? []).length} shown`
                : "all shown"}
            </span>
          </div>
        </div>
      )}

      {field && !loading && meta?.type === "continuous" && (
        <div style={{ marginTop: 5, display: "flex", gap: 4, alignItems: "center" }}>
          <span style={{ fontSize: 9, color: "#555" }}>min</span>
          <input
            type="number" placeholder={fmtBound(meta.min)}
            value={filter?.min ?? ""}
            onChange={(e) => setFilter({
              ...filter, min: e.target.value === "" ? null : parseFloat(e.target.value),
            })}
            style={{ ...SELECT_STYLE, marginTop: 0, width: 0, flex: 1 }}
          />
          <span style={{ fontSize: 9, color: "#555" }}>max</span>
          <input
            type="number" placeholder={fmtBound(meta.max)}
            value={filter?.max ?? ""}
            onChange={(e) => setFilter({
              ...filter, max: e.target.value === "" ? null : parseFloat(e.target.value),
            })}
            style={{ ...SELECT_STYLE, marginTop: 0, width: 0, flex: 1 }}
          />
        </div>
      )}

      {active && (
        <button onClick={() => setFilter(null)}
                style={{ ...CHIP_STYLE, marginTop: 5, color: "#f96" }}>
          clear filter
        </button>
      )}
    </div>
  );
}

function fmtBound(v) {
  if (v == null) return "";
  return Math.abs(v) >= 1000 || (v !== 0 && Math.abs(v) < 0.01)
    ? v.toExponential(1) : String(Math.round(v * 1000) / 1000);
}

function CellFilterSection({ unitLabel = "cell" }) {
  const { apiBase, dataset, cellFilter, setCellFilter, categoricalOverrides } = useStore();
  const [columns, setColumns] = useState([]);

  useEffect(() => {
    if (!dataset) return;
    fetch(`${apiBase}/spatial/${dataset}/cells/schema`)
      .then((r) => (r.ok ? r.json() : null))
      .then((s) => setColumns(s?.columns ? Object.keys(s.columns) : []))
      .catch(() => setColumns([]));
  }, [apiBase, dataset]);

  // Honour the same categorical override the color panel uses, so a column the
  // user has declared categorical offers checkboxes here rather than a range.
  const fetchValues = React.useCallback((field) => {
    const categorical = categoricalOverrides[`cell::${field}`] ?? null;
    return fetch(`${apiBase}/spatial/${dataset}/color-values`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: "metadata", field, categorical }),
    }).then((r) => (r.ok ? r.json() : null));
  }, [apiBase, dataset, categoricalOverrides]);

  return (
    <MetadataFilterSection
      scope="cell" columns={columns} filter={cellFilter} setFilter={setCellFilter}
      fetchValues={fetchValues} unitLabel={unitLabel}
    />
  );
}

function EdgeFilterSection() {
  const { apiBase, dataset, edgeFile, edgeFilter, setEdgeFilter, categoricalOverrides } = useStore();
  const [columns, setColumns] = useState([]);
  const efParam = `?edge_file=${encodeURIComponent(edgeFile)}`;

  useEffect(() => {
    if (!dataset) return;
    fetch(`${apiBase}/edges/${dataset}/schema${efParam}`)
      .then((r) => (r.ok ? r.json() : null))
      // Structural and per-LRM columns are not edge attributes to subset on:
      // one edge has many LRM rows, so "lrm = X" is a mechanism filter, which
      // the LRM checklist below already does properly.
      .then((s) => setColumns(
        s?.columns
          ? Object.keys(s.columns).filter((c) => !EDGE_FILTER_SKIP.has(c))
          : []
      ))
      .catch(() => setColumns([]));
  }, [apiBase, dataset, efParam]);

  const fetchValues = React.useCallback((field) => {
    const categorical = categoricalOverrides[`edge::${field}`] ?? null;
    return fetch(`${apiBase}/edges/${dataset}/edge-color-values${efParam}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: "metadata", field, categorical }),
    }).then((r) => (r.ok ? r.json() : null));
  }, [apiBase, dataset, efParam, categoricalOverrides]);

  if (!columns.length) return null;
  return (
    <MetadataFilterSection
      scope="edge" columns={columns} filter={edgeFilter} setFilter={setEdgeFilter}
      fetchValues={fetchValues} unitLabel="edge"
    />
  );
}

const EDGE_FILTER_SKIP = new Set([
  "edge", "sending_cell", "receiving_cell", "x1", "y1", "x2", "y2",
  "lrm", "lrm_id", "ligand", "receptor", "score", "score_norm",
]);

// ── Morphology row ────────────────────────────────────────────────────────────
function MorphologyRow() {
  const { layers, setLayerProp } = useStore();
  const state = layers.morphology ?? { visible: true, opacity: 1.0 };
  return (
    <LayerRowBase
      label="Morphology" color="#888"
      visible={state.visible} opacity={state.opacity}
      onToggle={(v) => setLayerProp("morphology", "visible", v)}
      onOpacity={(v) => setLayerProp("morphology", "opacity", v)}
    />
  );
}

function LayerRow({ id, label, color }) {
  const { layers, setLayerProp } = useStore();
  const state = layers[id] ?? { visible: true, opacity: 0.8 };
  return (
    <LayerRowBase
      label={label} color={color}
      visible={state.visible} opacity={state.opacity}
      onToggle={(v) => setLayerProp(id, "visible", v)}
      onOpacity={(v) => setLayerProp(id, "opacity", v)}
    />
  );
}

function LayerRowBase({ label, color, visible, opacity, onToggle, onOpacity }) {
  return (
    <div style={{ marginBottom: 8 }}>
      <label style={LABEL_STYLE}>
        <input
          type="checkbox"
          checked={visible}
          onChange={(e) => onToggle(e.target.checked)}
          style={{ accentColor: color, width: 13, height: 13, cursor: "pointer" }}
        />
        <span style={{
          display: "inline-block", width: 10, height: 10, borderRadius: 2,
          background: color, flexShrink: 0, opacity: visible ? 1 : 0.3,
        }} />
        <span style={{ color: visible ? "#ddd" : "#555" }}>{label}</span>
      </label>
      {visible && (
        <div style={{ display: "flex", alignItems: "center", gap: 6, paddingLeft: 26, marginTop: 3 }}>
          <input
            type="range" min={0} max={1} step={0.01} value={opacity}
            onChange={(e) => onOpacity(parseFloat(e.target.value))}
            style={{ flex: 1, accentColor: color, cursor: "pointer" }}
          />
          <span style={{ color: "#555", width: 28, textAlign: "right" }}>
            {Math.round(opacity * 100)}%
          </span>
        </div>
      )}
    </div>
  );
}

function TranscriptLayerRow() {
  const { layers, setLayerProp, transcriptFraction, setTranscriptFraction, transcriptStats } = useStore();
  const state = layers.transcripts ?? { visible: true, opacity: 0.8 };
  const { shown, total } = transcriptStats;

  const pctShown = total > 0 ? (shown / total * 100) : null;
  const fmt = (n) => n >= 1000 ? `${(n / 1000).toFixed(0)}k` : String(n);

  return (
    <div style={{ marginBottom: 8 }}>
      <label style={LABEL_STYLE}>
        <input
          type="checkbox"
          checked={state.visible}
          onChange={(e) => setLayerProp("transcripts", "visible", e.target.checked)}
          style={{ accentColor: "#e88", width: 13, height: 13, cursor: "pointer" }}
        />
        <span style={{
          display: "inline-block", width: 10, height: 10, borderRadius: 2,
          background: "#e88", flexShrink: 0, opacity: state.visible ? 1 : 0.3,
        }} />
        <span style={{ color: state.visible ? "#ddd" : "#555", flex: 1 }}>Transcripts</span>
        {/* Live shown / total stat */}
        {state.visible && total > 0 && (
          <span style={{ fontSize: 9, color: pctShown >= 99.5 ? "#6c6" : "#666", fontFamily: "monospace" }}>
            {fmt(shown)}/{fmt(total)} ({pctShown < 1 ? "<1" : Math.round(pctShown)}%)
          </span>
        )}
      </label>

      {state.visible && (
        <div style={{ paddingLeft: 26, marginTop: 3 }}>
          {/* Opacity */}
          <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 3 }}>
            <span style={{ fontSize: 10, color: "#555", width: 42, flexShrink: 0 }}>opacity</span>
            <input type="range" min={0} max={1} step={0.01} value={state.opacity}
              onChange={(e) => setLayerProp("transcripts", "opacity", parseFloat(e.target.value))}
              style={{ flex: 1, accentColor: "#e88", cursor: "pointer" }} />
            <span style={{ color: "#555", width: 28, textAlign: "right" }}>
              {Math.round(state.opacity * 100)}%
            </span>
          </div>
          {/* Sample fraction */}
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <span style={{ fontSize: 10, color: "#555", width: 42, flexShrink: 0 }}>sample</span>
            <input type="range" min={0.01} max={1} step={0.01} value={transcriptFraction}
              onChange={(e) => setTranscriptFraction(parseFloat(e.target.value))}
              style={{ flex: 1, accentColor: "#e88", cursor: "pointer" }} />
            <span style={{ color: transcriptFraction >= 0.995 ? "#6c6" : "#555", width: 28, textAlign: "right" }}>
              {transcriptFraction >= 0.995 ? "100%" : `${Math.round(transcriptFraction * 100)}%`}
            </span>
          </div>
        </div>
      )}
    </div>
  );
}

const BOUNDARY_TARGET = 5_000;
const BOUNDARY_SEED   = 50_000;

function CellSegmentsRow({ unitTitle = "Cell" }) {
  const {
    layers, setLayerProp,
    cellBoundaryFraction, setCellBoundaryFraction,
    cellBoundaryStats,
  } = useStore();
  const state = layers.cellSegments ?? { visible: true, opacity: 0.6, outlineOpacity: 0.8 };
  const { shown, total } = cellBoundaryStats;
  const pctShown = total > 0 ? (shown / total * 100) : null;
  const fmt = (n) => n >= 1000 ? `${(n / 1000).toFixed(0)}k` : String(n);

  // When in auto mode, mirror what the hook computes so the slider stays in sync.
  const isAuto = cellBoundaryFraction === null;
  const autoFrac = total > 0
    ? Math.min(1.0, BOUNDARY_TARGET / total)
    : Math.min(1.0, BOUNDARY_TARGET / BOUNDARY_SEED);
  const sliderValue = isAuto ? autoFrac : cellBoundaryFraction;
  const isAt100 = sliderValue >= 0.995;

  return (
    <div style={{ marginBottom: 8 }}>
      <label style={LABEL_STYLE}>
        <input type="checkbox" checked={state.visible}
          onChange={(e) => setLayerProp("cellSegments", "visible", e.target.checked)}
          style={{ accentColor: "#6cf", width: 13, height: 13, cursor: "pointer" }} />
        <span style={{
          display: "inline-block", width: 10, height: 10, borderRadius: 2,
          background: "#6cf", flexShrink: 0, opacity: state.visible ? 1 : 0.3,
        }} />
        <span style={{ color: state.visible ? "#ddd" : "#555", flex: 1 }}>{unitTitle} Segments</span>
        {/* Live shown / total stat */}
        {state.visible && total > 0 && (
          <span style={{ fontSize: 9, color: pctShown >= 99.5 ? "#6c6" : "#666", fontFamily: "monospace" }}>
            {fmt(shown)}/{fmt(total)} ({pctShown < 1 ? "<1" : Math.round(pctShown)}%)
          </span>
        )}
      </label>
      {state.visible && (
        <div style={{ paddingLeft: 26, marginTop: 3 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 3 }}>
            <span style={{ fontSize: 10, color: "#555", width: 42, flexShrink: 0 }}>fill</span>
            <input type="range" min={0} max={1} step={0.01} value={state.opacity}
              onChange={(e) => setLayerProp("cellSegments", "opacity", parseFloat(e.target.value))}
              style={{ flex: 1, accentColor: "#6cf", cursor: "pointer" }} />
            <span style={{ color: "#555", width: 28, textAlign: "right" }}>
              {Math.round(state.opacity * 100)}%
            </span>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 3 }}>
            <span style={{ fontSize: 10, color: "#555", width: 42, flexShrink: 0 }}>outline</span>
            <input type="range" min={0} max={1} step={0.01}
              value={state.outlineOpacity ?? 0.8}
              onChange={(e) => setLayerProp("cellSegments", "outlineOpacity", parseFloat(e.target.value))}
              style={{ flex: 1, accentColor: "#6cf", cursor: "pointer" }} />
            <span style={{ color: "#555", width: 28, textAlign: "right" }}>
              {Math.round((state.outlineOpacity ?? 0.8) * 100)}%
            </span>
          </div>
          {/* Sample fraction — slider + auto/manual indicator */}
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <span style={{ fontSize: 10, color: "#555", width: 42, flexShrink: 0 }}>sample</span>
            <input type="range" min={0.01} max={1} step={0.01} value={sliderValue}
              onChange={(e) => setCellBoundaryFraction(parseFloat(e.target.value))}
              style={{ flex: 1, accentColor: "#6cf", cursor: "pointer" }} />
            <span style={{
              color: isAt100 ? "#6c6" : "#555", width: 28, textAlign: "right", flexShrink: 0,
            }}>
              {isAt100 ? "100%" : `${Math.round(sliderValue * 100)}%`}
            </span>
            {/* Auto tag / reset button */}
            {isAuto ? (
              <span style={{
                fontSize: 9, color: "#4a8", background: "#162b1e", border: "1px solid #4a8",
                borderRadius: 3, padding: "1px 4px", flexShrink: 0, cursor: "default",
              }}>auto</span>
            ) : (
              <button
                onClick={() => setCellBoundaryFraction(null)}
                title="Reset to auto"
                style={{
                  fontSize: 10, color: "#666", background: "none", border: "1px solid #444",
                  borderRadius: 3, padding: "1px 4px", cursor: "pointer", flexShrink: 0,
                  lineHeight: 1,
                }}>↺</button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Transcript species section ────────────────────────────────────────────────
function TranscriptSpeciesSection() {
  const {
    allGenes, genesLoaded, selectedGenes, setSelectedGenes, toggleSelectedGene,
    transcriptColorOverrides, setTranscriptColorOverride,
    mergeTranscriptColorOverrides, resetTranscriptColorOverrides,
  } = useStore();
  const [expanded, setExpanded] = useState(false);
  const [search, setSearch] = useState("");
  const fileInputRef = useRef(null);

  const filterActive = selectedGenes !== null;
  const selectedList = filterActive ? [...selectedGenes].sort() : [];

  const pickerGenes = search.trim()
    ? allGenes.filter((g) => g.toLowerCase().includes(search.toLowerCase()))
    : allGenes;

  // Resolve display color: override first, then deterministic hash
  function resolveColor(gene) {
    const ov = transcriptColorOverrides[gene];
    if (ov) return ov;
    return [...geneColor(gene), 255];
  }

  function handleImportCSV(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    e.target.value = "";
    const reader = new FileReader();
    reader.onload = (ev) => {
      const lines = ev.target.result.split(/\r?\n/).filter((l) => l.trim());
      const map = {};
      for (const line of lines) {
        const sep = line.includes("\t") ? "\t" : ",";
        const parts = line.split(sep).map((p) => p.trim().replace(/^"|"$/g, ""));
        if (parts.length < 2) continue;
        const [gene, hex] = parts;
        if (!hex || !hex.match(/^#?[0-9a-fA-F]{6}$/)) continue;
        const normalHex = hex.startsWith("#") ? hex : `#${hex}`;
        map[gene] = hexToRgba(normalHex);
      }
      if (Object.keys(map).length > 0) mergeTranscriptColorOverrides(map);
    };
    reader.readAsText(file);
  }

  function handleExportCSV() {
    const genesToExport = filterActive ? selectedList : allGenes;
    const rows = ["gene,color",
      ...genesToExport.map((gene) => {
        const [r, g, b] = resolveColor(gene);
        return `"${gene.replace(/"/g, '""')}",${rgbaToHex([r, g, b])}`;
      }),
    ];
    const blob = new Blob([rows.join("\n")], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "transcripts_palette.csv";
    a.click();
    URL.revokeObjectURL(url);
  }

  const hasOverrides = Object.keys(transcriptColorOverrides).length > 0;

  if (!genesLoaded) {
    return <div style={{ color: "#3a3a3a", paddingLeft: 4, marginBottom: 6, fontSize: 11 }}>— loading genes…</div>;
  }
  if (allGenes.length === 0) {
    return <div style={{ color: "#3a3a3a", paddingLeft: 4, marginBottom: 6, fontSize: 11 }}>— no gene list available</div>;
  }

  return (
    <div style={{ marginBottom: 8 }}>
      {/* Status + action buttons */}
      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
        <span style={{ fontSize: 10, color: "#555", flex: 1 }}>
          {filterActive
            ? `${selectedGenes.size} / ${allGenes.length} genes selected`
            : `all ${allGenes.length} genes`}
        </span>
        {filterActive && (
          <button
            onClick={() => { setSelectedGenes(null); setExpanded(false); }}
            style={CHIP_STYLE}
          >
            clear
          </button>
        )}
        <button onClick={() => setExpanded((e) => !e)} style={CHIP_STYLE}>
          {expanded ? "▲" : filterActive ? "edit ▼" : "select ▼"}
        </button>
      </div>

      {/* Compact selected-gene list (filter active, picker closed) */}
      {filterActive && !expanded && (
        <div style={{ maxHeight: 110, overflowY: "auto", marginBottom: 4 }}>
          {selectedList.length === 0 && (
            <div style={{ fontSize: 11, color: "#555", paddingLeft: 2 }}>no genes selected</div>
          )}
          {selectedList.map((gene) => {
            const [r, g, b] = resolveColor(gene);
            return (
              <div key={gene} style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 11, color: "#e88", padding: "1px 0" }}>
                <label title="Click to change color" style={{ cursor: "pointer", flexShrink: 0, display: "flex" }}>
                  <div style={{ width: 10, height: 10, borderRadius: 2, background: `rgb(${r},${g},${b})`, outline: "1px solid rgba(255,255,255,0.15)" }} />
                  <input type="color" value={rgbaToHex([r, g, b])}
                    onChange={(e) => setTranscriptColorOverride(gene, hexToRgba(e.target.value))}
                    style={{ position: "absolute", opacity: 0, width: 0, height: 0, pointerEvents: "none" }} tabIndex={-1} />
                </label>
                <span style={{ flex: 1 }}>{gene}</span>
                <button
                  onClick={() => toggleSelectedGene(gene)}
                  style={{ background: "transparent", border: "none", color: "#844", cursor: "pointer", fontSize: 11, padding: "0 2px", lineHeight: 1 }}
                >✕</button>
              </div>
            );
          })}
        </div>
      )}

      {/* Expanded gene picker */}
      {expanded && (
        <div>
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="filter genes…"
            style={{ ...SELECT_STYLE, marginBottom: 4 }}
          />
          <div style={{ display: "flex", gap: 4, marginBottom: 4 }}>
            <button onClick={() => setSelectedGenes(null)} style={CHIP_STYLE}>all</button>
            <button onClick={() => setSelectedGenes(new Set())} style={CHIP_STYLE}>none</button>
          </div>
          <div style={{ maxHeight: 180, overflowY: "auto" }}>
            {pickerGenes.map((gene) => {
              const checked = selectedGenes === null || selectedGenes.has(gene);
              const [r, g, b] = resolveColor(gene);
              return (
                // Row split into swatch-label + checkbox-label so clicking the
                // swatch doesn't also toggle the checkbox (nested-label issue).
                <div key={gene} style={{ display: "flex", alignItems: "center", gap: 4, marginBottom: 3 }}>
                  <label title="Click to change color" style={{ cursor: "pointer", flexShrink: 0, display: "flex" }}>
                    <div style={{ width: 10, height: 10, borderRadius: 2, background: `rgb(${r},${g},${b})`, outline: "1px solid rgba(255,255,255,0.15)" }} />
                    <input type="color" value={rgbaToHex([r, g, b])}
                      onChange={(e) => setTranscriptColorOverride(gene, hexToRgba(e.target.value))}
                      style={{ position: "absolute", opacity: 0, width: 0, height: 0, pointerEvents: "none" }} tabIndex={-1} />
                  </label>
                  <label style={{ ...LABEL_STYLE, flex: 1, marginBottom: 0 }}>
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => toggleSelectedGene(gene)}
                      style={{ accentColor: "#e88", width: 12, height: 12, cursor: "pointer", flexShrink: 0 }}
                    />
                    <span style={{ marginLeft: 2, color: checked ? "#ccc" : "#444", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {gene}
                    </span>
                  </label>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Palette import / export / reset — always visible once genes are loaded */}
      <div style={{ display: "flex", gap: 6, marginTop: 5, alignItems: "center" }}>
        <button onClick={() => fileInputRef.current?.click()}
          title="Load a CSV with columns: gene, #hexcolor"
          style={{ fontSize: 9, fontFamily: "monospace", color: "#6cf", background: "none", border: "1px solid #2a4a5a", borderRadius: 3, padding: "2px 6px", cursor: "pointer" }}>
          import palette
        </button>
        <button onClick={handleExportCSV}
          title={filterActive ? "Export colors for selected genes" : "Export colors for all genes"}
          style={{ fontSize: 9, fontFamily: "monospace", color: "#6cf", background: "none", border: "1px solid #2a4a5a", borderRadius: 3, padding: "2px 6px", cursor: "pointer" }}>
          export palette
        </button>
        {hasOverrides && (
          <button onClick={resetTranscriptColorOverrides}
            title="Restore default gene colors"
            style={{ fontSize: 9, fontFamily: "monospace", color: "#888", background: "none", border: "1px solid #333", borderRadius: 3, padding: "2px 6px", cursor: "pointer" }}>
            reset
          </button>
        )}
        <input ref={fileInputRef} type="file" accept=".csv,.tsv,.txt"
          onChange={handleImportCSV} style={{ display: "none" }} />
      </div>
    </div>
  );
}

const CHIP_STYLE = {
  background: "#2a2a2a",
  color: "#777",
  border: "1px solid #3a3a3a",
  borderRadius: 3,
  padding: "1px 5px",
  fontFamily: "monospace",
  fontSize: 10,
  cursor: "pointer",
};

// ── Density row (top-level — applies to tissue graph + edge data) ─────────────
function DensityRow() {
  const { edgeDensity, setEdgeDensity } = useStore();
  return (
    <div style={{ marginBottom: 8 }}>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: "#555", marginBottom: 2 }}>
        <span>density: {Math.round(edgeDensity * 100)}%{edgeDensity >= 1.0 ? " (all)" : ""}</span>
        <span style={{ color: "#3a3a3a" }}>tissue graph + edges</span>
      </div>
      <input
        type="range" min={0.01} max={1} step={0.01}
        value={edgeDensity}
        onChange={(e) => setEdgeDensity(parseFloat(e.target.value))}
        style={{ width: "100%", accentColor: "#888", cursor: "pointer" }}
      />
    </div>
  );
}

// ── Tissue graph section ──────────────────────────────────────────────────────
function TissueGraphSection() {
  const { layers, setLayerProp } = useStore();
  const state = layers.tissueGraph ?? { visible: true, opacity: 0.25 };
  return (
    <div style={{ marginBottom: 8 }}>
      <LayerRowBase
        label="Tissue Graph"
        color="#888"
        visible={state.visible}
        opacity={state.opacity}
        onToggle={(v) => setLayerProp("tissueGraph", "visible", v)}
        onOpacity={(v) => setLayerProp("tissueGraph", "opacity", v)}
      />
    </div>
  );
}

// ── Edge section ──────────────────────────────────────────────────────────────
// Columns that are identity/spatial and shouldn't appear in metadata picker
const EDGE_SKIP_COLS = new Set(["x1", "y1", "x2", "y2", "edge", "sending_cell", "receiving_cell", "is_autocrine", "lrm_id", "lrm", "ligand", "receptor", "score", "score_norm"]);

function EdgeSection() {
  const {
    apiBase, dataset,
    edgeFile, setEdgeFile,
    layers, setLayerProp,
    edgeMinStrength, setEdgeMinStrength,
    edgeColorBy, setEdgeColorBy,
    edgeColorPalette, setEdgeColorPalette,
    edgeDirectional, setEdgeDirectional,
    edgeOffset, setEdgeOffset,
    showAutocrine, setShowAutocrine,
    autocrineRadius, setAutocrineRadius,
    autocrineLineWidth, setAutocrineLineWidth,
    edgeWidth, setEdgeWidth,
    showArrowheads, setShowArrowheads,
    arrowStyle, setArrowStyle,
    arrowheadScale, setArrowheadScale,
    lrmCatalogue, setLrmCatalogue,
    hiddenLrms, toggleLrm, setAllLrmsVisible, hideAllLrms,
    edgeColorRange,
    edgeColorClamp, setEdgeColorClamp,
    categoricalOverrides, setCategoricalOverride,
  } = useStore();
  const state = layers.edges ?? { visible: true, opacity: 0.9 };
  const [localStrength, setLocalStrength] = useState(edgeMinStrength ?? 0);
  const commitTimer = useRef(null);
  const [edgeSchema, setEdgeSchema] = useState(null);
  const [lrmSearch, setLrmSearch] = useState("");
  // List of edge-source files in the current dataset: [{id, label}].
  const [edgeFiles, setEdgeFiles] = useState([]);

  // ── edge-source parquet list (issue #46) ────────────────────────────────────
  // Fetch the available edge files for the dataset and select the backend-provided
  // default (legacy top-level edges.parquet if present, else the first). Falls back
  // gracefully to a single implicit "edges.parquet" if the endpoint returns nothing.
  useEffect(() => {
    if (!dataset) return;
    fetch(`${apiBase}/edges/${dataset}/files`)
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        const files = Array.isArray(data?.files) ? data.files : [];
        setEdgeFiles(files);
        if (files.length > 0) {
          const cur = useStore.getState().edgeFile;
          const ids = files.map((f) => f.id);
          if (!ids.includes(cur)) setEdgeFile(data.default ?? ids[0]);
        }
      })
      .catch(() => setEdgeFiles([]));
  }, [apiBase, dataset]); // eslint-disable-line react-hooks/exhaustive-deps

  const efParam = `?edge_file=${encodeURIComponent(edgeFile)}`;
  useEffect(() => {
    if (!dataset) return;
    fetch(`${apiBase}/edges/${dataset}/schema${efParam}`)
      .then((r) => (r.ok ? r.json() : null))
      .then(setEdgeSchema)
      .catch(() => {});
    if (lrmCatalogue.length === 0) {
      fetch(`${apiBase}/edges/${dataset}/lrm-catalogue${efParam}`)
        .then((r) => (r.ok ? r.json() : []))
        .then(setLrmCatalogue)
        .catch(() => {});
    }
  }, [apiBase, dataset, efParam]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleStrength = (e) => {
    const v = parseFloat(e.target.value);
    setLocalStrength(v);
    clearTimeout(commitTimer.current);
    commitTimer.current = setTimeout(() => setEdgeMinStrength(v), 300);
  };

  // Metadata columns for color-by (exclude spatial/identity cols)
  const metaCols = edgeSchema?.columns
    ? Object.keys(edgeSchema.columns).filter((c) => !EDGE_SKIP_COLS.has(c))
    : [];

  const { mode, field } = edgeColorBy;
  const selectedLrmCount = lrmCatalogue.length - hiddenLrms.size;

  // How the selected edge metadata column is typed. Asking the backend rather
  // than reading the dtype matters for the same reason it does on the cell side:
  // the auto-rule also calls a low-cardinality integer column categorical, and an
  // explicit override can flip either way (issue #35).
  const fieldDtype = field && edgeSchema ? edgeSchema.columns[field] : null;
  const isNumericField = !!fieldDtype && /^(int|uint|float|double|Int|UInt|Float)/.test(fieldDtype);
  const edgeOverrideKey = `edge::${field}`;
  const edgeOverride = categoricalOverrides[edgeOverrideKey] ?? null;

  const [edgeMeta, setEdgeMeta] = useState(null);   // { type, categories }
  useEffect(() => {
    if (mode !== "metadata" || !field) { setEdgeMeta(null); return; }
    let cancelled = false;
    fetch(`${apiBase}/edges/${dataset}/edge-color-values${efParam}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: "metadata", field, categorical: edgeOverride }),
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (!cancelled && d) setEdgeMeta({ type: d.type, categories: d.categories ?? [] }); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [apiBase, dataset, efParam, mode, field, edgeOverride]);

  const isCategorical = mode === "metadata" && !!field && edgeMeta?.type === "categorical";

  return (
    <div style={{ marginBottom: 8 }}>
      {/* Edge-source picker (issue #46) — only shown when the dataset has more than
          one edge file. Applies to every open viewer panel. */}
      {edgeFiles.length > 1 && (
        <div style={{ marginBottom: 8 }}>
          <select
            value={edgeFile}
            onChange={(e) => setEdgeFile(e.target.value)}
            style={SELECT_STYLE}
            title="Which edge-source parquet to render"
          >
            {edgeFiles.map((f) => (
              <option key={f.id} value={f.id}>{f.label}</option>
            ))}
          </select>
        </div>
      )}
      <LayerRowBase
        label="Edges"
        color="#f90"
        visible={state.visible}
        opacity={state.opacity}
        onToggle={(v) => setLayerProp("edges", "visible", v)}
        onOpacity={(v) => setLayerProp("edges", "opacity", v)}
      />
      {state.visible && (
        <div style={{ paddingLeft: 26, marginTop: 4 }}>
          {/* Edge style toggles */}
          <div style={{ display: "flex", gap: 10, marginBottom: 6, flexWrap: "wrap" }}>
            <label style={LABEL_STYLE}>
              <input type="checkbox" checked={edgeDirectional}
                onChange={(e) => setEdgeDirectional(e.target.checked)}
                style={{ accentColor: "#f90" }} />
              Directional
            </label>
            <label style={LABEL_STYLE}>
              <input type="checkbox" checked={showArrowheads && edgeDirectional}
                disabled={!edgeDirectional}
                onChange={(e) => setShowArrowheads(e.target.checked)}
                style={{ accentColor: "#f90" }} />
              Arrowheads
            </label>
            <label style={LABEL_STYLE}>
              <input type="checkbox" checked={showAutocrine}
                onChange={(e) => setShowAutocrine(e.target.checked)}
                style={{ accentColor: "#f90" }} />
              Autocrine
            </label>
          </div>

          {/* Edge width */}
          <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 6 }}>
            <span style={{ fontSize: 10, color: "#555", whiteSpace: "nowrap", width: 72 }}>
              width: {edgeWidth.toFixed(1)}
            </span>
            <input
              type="range" min={0.5} max={8} step={0.5}
              value={edgeWidth}
              onChange={(e) => setEdgeWidth(parseFloat(e.target.value))}
              style={{ flex: 1, accentColor: "#f90", cursor: "pointer" }}
            />
          </div>

          {/* Offset slider (directional mode only) */}
          {edgeDirectional && (
            <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 6 }}>
              <span style={{ fontSize: 10, color: "#555", whiteSpace: "nowrap", width: 72 }}>
                offset: {edgeOffset.toFixed(1)}
              </span>
              <input
                type="range" min={0} max={20} step={0.5}
                value={edgeOffset}
                onChange={(e) => setEdgeOffset(parseFloat(e.target.value))}
                style={{ flex: 1, accentColor: "#f90", cursor: "pointer" }}
              />
            </div>
          )}

          {/* Autocrine controls */}
          {showAutocrine && (
            <div style={{ marginBottom: 6 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
                <span style={{ fontSize: 10, color: "#555", whiteSpace: "nowrap", width: 72 }}>
                  ring r: {autocrineRadius}
                </span>
                <input
                  type="range" min={4} max={40} step={1}
                  value={autocrineRadius}
                  onChange={(e) => setAutocrineRadius(parseInt(e.target.value, 10))}
                  style={{ flex: 1, accentColor: "#f90", cursor: "pointer" }}
                />
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
                <span style={{ fontSize: 10, color: "#555", whiteSpace: "nowrap", width: 72 }}>
                  ring w: {autocrineLineWidth.toFixed(1)}
                </span>
                <input
                  type="range" min={0.5} max={8} step={0.5}
                  value={autocrineLineWidth}
                  onChange={(e) => setAutocrineLineWidth(parseFloat(e.target.value))}
                  style={{ flex: 1, accentColor: "#f90", cursor: "pointer" }}
                />
              </div>
            </div>
          )}

          {/* Arrowhead controls (only when arrowheads + directional are on) */}
          {edgeDirectional && showArrowheads && (
            <div style={{ marginBottom: 8 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 6 }}>
                <span style={{ fontSize: 10, color: "#555", whiteSpace: "nowrap", width: 72 }}>
                  arrow: {arrowheadScale.toFixed(2)}×
                </span>
                <input
                  type="range" min={0.25} max={3} step={0.25}
                  value={arrowheadScale}
                  onChange={(e) => setArrowheadScale(parseFloat(e.target.value))}
                  style={{ flex: 1, accentColor: "#f90", cursor: "pointer" }}
                />
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <label style={LABEL_STYLE}>
                  <input type="radio" name="arrowStyle" value="full"
                    checked={arrowStyle === "full"}
                    onChange={() => setArrowStyle("full")}
                    style={{ accentColor: "#f90" }} />
                  Full
                </label>
                <label style={LABEL_STYLE}>
                  <input type="radio" name="arrowStyle" value="half"
                    checked={arrowStyle === "half"}
                    onChange={() => setArrowStyle("half")}
                    style={{ accentColor: "#f90" }} />
                  Harpoon
                </label>
              </div>
            </div>
          )}

          {/* Strength filter */}
          <div style={{ fontSize: 10, color: "#555", marginBottom: 2 }}>
            min strength: {localStrength.toFixed(2)}
          </div>
          <input
            type="range" min={0} max={4} step={0.05}
            value={localStrength}
            onChange={handleStrength}
            style={{ width: "100%", accentColor: "#f90", cursor: "pointer", marginBottom: 8 }}
          />

          {/* ── Edge Color ──────────────────────────────────────────────── */}
          <div style={{ fontSize: 10, color: "#555", marginBottom: 3 }}>edge color</div>
          <select value={mode} onChange={(e) => { setEdgeColorBy(e.target.value, null); setEdgeColorClamp(null, null); }} style={SELECT_STYLE}>
            <option value="default">Default (uniform)</option>
            <option value="lrm_set">LRM Set (selected mechanisms)</option>
            <option value="metadata">Metadata column</option>
          </select>

          {mode === "lrm_set" && (
            <div style={{ marginTop: 4, fontSize: 10, color: "#888" }}>
              {selectedLrmCount} of {lrmCatalogue.length} LRMs selected
              <span style={{ color: "#555" }}> (use checklist below)</span>
            </div>
          )}

          {mode === "metadata" && (
            <>
              <select
                value={field ?? ""}
                onChange={(e) => { setEdgeColorBy("metadata", e.target.value || null); setEdgeColorClamp(null, null); }}
                style={{ ...SELECT_STYLE, marginTop: 4 }}
              >
                <option value="">— select column —</option>
                {metaCols.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
              {field && isNumericField && (
                <label style={{ ...LABEL_STYLE, marginTop: 5, fontSize: 10, color: "#888" }}>
                  <input
                    type="checkbox"
                    checked={isCategorical}
                    onChange={(e) => {
                      setCategoricalOverride("edge", field, e.target.checked);
                      setEdgeColorClamp(null, null);
                    }}
                    style={{ accentColor: "#f90" }}
                  />
                  treat as categorical
                  {edgeOverride !== null && (
                    <button
                      onClick={(ev) => { ev.preventDefault(); setCategoricalOverride("edge", field, null); }}
                      title="Go back to auto-detection for this column"
                      style={{ ...CHIP_STYLE, marginLeft: 4, color: "#666" }}
                    >
                      auto
                    </button>
                  )}
                </label>
              )}
            </>
          )}

          {/* Palette — only for continuous color modes */}
          {(mode === "lrm_set" || (mode === "metadata" && field && !isCategorical)) && (
            <select
              value={edgeColorPalette}
              onChange={(e) => setEdgeColorPalette(e.target.value)}
              style={{ ...SELECT_STYLE, marginTop: 4 }}
            >
              {PALETTE_OPTIONS.map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
          )}

          {/* Legend */}
          {mode === "lrm_set" && (
            <ClampableLegend label="LRM set score" palette={edgeColorPalette}
              vmin={edgeColorRange.vmin} vmax={edgeColorRange.vmax}
              clamp={edgeColorClamp} setClamp={setEdgeColorClamp} accentColor="#f90" />
          )}
          {mode === "metadata" && field && !isCategorical && (
            <ClampableLegend label={field} palette={edgeColorPalette}
              vmin={edgeColorRange.vmin} vmax={edgeColorRange.vmax}
              clamp={edgeColorClamp} setClamp={setEdgeColorClamp} accentColor="#f90" />
          )}
          {mode === "metadata" && field && isCategorical && (
            <EdgeCategoricalLegend categories={edgeMeta?.categories ?? []} />
          )}

          {/* ── Edge metadata filter (issue #45) ─────────────────────── */}
          {/* Distinct from the LRM checklist below: this subsets *edges* by an
              attribute of the pair (a curation call, a confidence), whereas the
              checklist subsets the mechanisms scored on every edge. */}
          <div style={{ ...SECTION_HEADER, marginTop: 10 }}>Edge Filter</div>
          <EdgeFilterSection />

          {/* ── LRM Mechanisms checklist ─────────────────────────────── */}
          {lrmCatalogue.length > 0 && (
            <div style={{ marginTop: 10 }}>
              <div style={{ ...SECTION_HEADER, marginTop: 6 }}>LRM Mechanisms</div>
              <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
                <span style={{ fontSize: 10, color: "#555", flex: 1 }}>
                  {selectedLrmCount} / {lrmCatalogue.length} active
                </span>
                <button onClick={setAllLrmsVisible} style={CHIP_STYLE}>all</button>
                <button onClick={hideAllLrms} style={CHIP_STYLE}>none</button>
              </div>
              <input
                type="text"
                value={lrmSearch}
                onChange={(e) => setLrmSearch(e.target.value)}
                placeholder="filter mechanisms…"
                style={{ ...SELECT_STYLE, marginBottom: 4 }}
              />
              <div style={{ maxHeight: 160, overflowY: "auto" }}>
                {lrmCatalogue
                  .filter((e) =>
                    !lrmSearch.trim() ||
                    `${e.ligand} ${e.receptor}`.toLowerCase().includes(lrmSearch.toLowerCase())
                  )
                  .map((entry) => {
                    const lrmStr = entry.lrm ?? `${entry.ligand}|${entry.receptor}`;
                    const active = !hiddenLrms.has(lrmStr);
                    return (
                      <label key={lrmStr} style={{ ...LABEL_STYLE, marginBottom: 3, display: "flex" }}>
                        <input
                          type="checkbox"
                          checked={active}
                          onChange={() => toggleLrm(lrmStr)}
                          style={{ accentColor: "#f90", width: 12, height: 12, cursor: "pointer", flexShrink: 0 }}
                        />
                        <span style={{
                          marginLeft: 4,
                          color: active ? "#ccc" : "#444",
                          overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                        }}>
                          {entry.ligand} → {entry.receptor}
                        </span>
                      </label>
                    );
                  })
                }
              </div>
            </div>
          )}
        </div>
      )}

    </div>
  );
}

/** Read-only swatch list. Categories come from EdgeSection, which already asked
 *  the backend for the column's type — one fetch, one answer, no chance of the
 *  legend describing a different typing decision than the canvas is using. */
function EdgeCategoricalLegend({ categories = [] }) {
  if (!categories.length) return null;
  return (
    <div style={{ marginTop: 6 }}>
      {categories.map((cat, i) => {
        const [r, g, b] = QUAL_PALETTE[i % QUAL_PALETTE.length];
        return (
          <div key={cat} style={{ display: "flex", alignItems: "center", gap: 5, marginBottom: 3 }}>
            <div style={{ width: 10, height: 10, borderRadius: 2, background: `rgb(${r},${g},${b})`, flexShrink: 0 }} />
            <span style={{ fontSize: 10, color: "#aaa", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                  title={cat}>{cat}</span>
          </div>
        );
      })}
    </div>
  );
}

function RegionsSection() {
  const { apiBase, dataset, regions, removeRegion } = useStore();
  if (regions.length === 0) return null;

  const exportRegion = async (region) => {
    const res = await fetch(`${apiBase}/spatial/${dataset}/cells/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(region.selectedCellIds),
    });
    if (!res.ok) return;
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${dataset}_region_${region.id}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <>
      <div style={SECTION_HEADER}>Regions</div>
      {regions.map((r) => {
        const [rv, gv, bv] = r.color;
        const swatch = `rgb(${rv},${gv},${bv})`;
        return (
          <div key={r.id} style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 5 }}>
            <div style={{ width: 10, height: 10, borderRadius: 2, background: swatch, flexShrink: 0 }} />
            <span style={{ flex: 1, fontSize: 11, color: "#aaa" }}>
              {r.selectedCellIds.length} cells
            </span>
            <button
              title="Export cells as CSV"
              onClick={() => exportRegion(r)}
              style={{ background: "transparent", border: "1px solid #3a3a3a", color: "#8af", borderRadius: 3, padding: "1px 6px", fontFamily: "monospace", fontSize: 10, cursor: "pointer" }}
            >
              CSV
            </button>
            <button
              title="Remove region"
              onClick={() => removeRegion(r.id)}
              style={{ background: "transparent", border: "none", color: "#c44", fontFamily: "monospace", fontSize: 12, cursor: "pointer", padding: "0 2px" }}
            >
              ×
            </button>
          </div>
        );
      })}
    </>
  );
}

function PlaceholderRow({ label }) {
  return <div style={{ color: "#3a3a3a", paddingLeft: 4, marginBottom: 6 }}>{label}</div>;
}
