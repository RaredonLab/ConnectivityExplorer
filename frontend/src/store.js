import { create } from "zustand";

const API = import.meta.env.VITE_API_URL ?? "/api";

/**
 * One panel's *display* settings — how the data looks, as opposed to which data
 * it is.
 *
 * A factory rather than a constant because several values are mutable
 * containers (the `layers` map, `hiddenLrms`). Sharing one object across panels
 * would alias them: unhiding a mechanism in one panel would silently unhide it
 * in the other, which is the exact bug this structure exists to prevent.
 *
 * Phase 2a moved these out of the top level of the store, where they were
 * global. They are written to every panel at once for now (see `patchSettings`),
 * so behaviour is unchanged; 2b adds the link toggle that lets them diverge.
 * See docs/split_screen_phase2.md.
 */
export function makeSettings() {
  return {
    layers: {
      morphology:   { visible: false, opacity: 1.0 },
      transcripts:  { visible: false, opacity: 0.8 },
      cellSegments: { visible: true,  opacity: 1.0, outlineOpacity: 0.0 },
      tissueGraph:  { visible: true,  opacity: 0.05 },
      edges:        { visible: false, opacity: 0.25 },
    },

    // null = auto (the hook targets ~5k cells and adapts to viewport density);
    // a number is the user's slider override.
    cellBoundaryFraction: null,
    transcriptFraction: 0.1,

    // Values outside [low, high] map to the palette ends (oob::squish).
    cellColorClamp: { low: null, high: null },
    edgeColorClamp: { low: null, high: null },

    cellColorEnabled: false,
    colorBy: { mode: "off", field: null },   // 'off' | 'gene_set' | 'metadata'
    cellColorPalette: "viridis",

    edgeWidth: 2,
    showArrowheads: true,
    arrowStyle: "half",                       // 'full' chevron | 'half' harpoon
    arrowheadScale: 1.0,
    edgeDensity: 0.1,
    edgeMinStrength: 0,
    edgeColorBy: { mode: "lrm_set", field: null },
    edgeColorPalette: "viridis",
    edgeDirectional: true,
    edgeOffset: 0,
    showAutocrine: false,
    autocrineRadius: 14,
    autocrineLineWidth: 2,

    hiddenLrms: new Set(),
    selectedGenes: null,                      // null = no filter; Set = allowlist
    cellFilter: null,
    // Edge-side filters (issue #59). Independent of cellFilter: filtering cells
    // and filtering edges are separate actions, and an edge may terminate on a
    // cell that is not drawn.
    //   sending/receivingFilter — a *cell* metadata predicate on one endpoint;
    //     both set gives the intersection, one set leaves the other end free.
    //   edgeFilters — predicates on the edge table / edge-metadata, and-ed.
    sendingFilter: null,
    receivingFilter: null,
    edgeFilters: [],
    categoricalOverrides: {},                 // "cell::<field>" | "edge::<field>" -> bool
    categoryColorOverrides: {},               // "<field>::<category>" -> [r,g,b,a]
    transcriptColorOverrides: {},             // gene -> [r,g,b,a]
  };
}

/**
 * Copy a settings object deeply enough that the panels cannot alias.
 *
 * Only the mutable containers need rebuilding — every setter replaces rather
 * than mutates, so the leaves can be shared.
 */
function cloneSettings(src) {
  return {
    ...src,
    hiddenLrms: new Set(src.hiddenLrms),
    selectedGenes: src.selectedGenes === null ? null : new Set(src.selectedGenes),
    layers: Object.fromEntries(
      Object.entries(src.layers).map(([k, v]) => [k, { ...v }])),
    edgeFilters: [...(src.edgeFilters ?? [])],
    categoricalOverrides: { ...src.categoricalOverrides },
    categoryColorOverrides: { ...src.categoryColorOverrides },
    transcriptColorOverrides: { ...src.transcriptColorOverrides },
  };
}

/**
 * Strip settings that name something the target dataset does not have.
 *
 * Copying settings across datasets is where this gets dangerous rather than
 * merely wrong-looking: a `cellFilter` naming a column the target lacks makes
 * the backend return 400 on *every* viewport change, so the panel stops
 * rendering entirely and the cause is invisible from the UI. Dropping the
 * setting degrades to "no filter", which is recoverable and obvious.
 *
 * `allowed` supplies the target's vocabulary — `{cellFields, edgeFields, genes,
 * lrms}`, each a Set or null to skip that check. The caller provides it because
 * metadata columns come from /cells/schema and /edges/schema, which the store
 * does not fetch; genes and LRMs it already holds per panel.
 *
 * `sameDataset` governs the colour clamps. A clamp is a range in the *source*
 * data's units — carrying [0, 4000] onto a dataset topping out at 70 paints
 * everything the bottom colour, which reads as a broken render rather than a
 * copied setting. Same dataset, the clamp is exactly what you meant to copy.
 */
export function sanitiseSettings(next, allowed, sameDataset) {
  const { cellFields = null, edgeFields = null, genes = null, lrms = null } = allowed ?? {};
  const has = (set, v) => set === null || set.has(v);
  const filterKeys = (obj, keep) =>
    Object.fromEntries(Object.entries(obj).filter(([k]) => keep(k)));

  if (next.colorBy?.mode === "metadata" && !has(cellFields, next.colorBy.field)) {
    next.colorBy = { mode: "off", field: null };
  }
  if (next.edgeColorBy?.mode === "metadata" && !has(edgeFields, next.edgeColorBy.field)) {
    next.edgeColorBy = { mode: "lrm_set", field: null };
  }
  if (next.cellFilter && !has(cellFields, next.cellFilter.field)) next.cellFilter = null;
  // The endpoint filters name *cell* columns even though they filter edges, so
  // they validate against cellFields — getting this wrong would drop every one of
  // them against a target whose edge table simply has different columns.
  if (next.sendingFilter && !has(cellFields, next.sendingFilter.field)) next.sendingFilter = null;
  if (next.receivingFilter && !has(cellFields, next.receivingFilter.field)) next.receivingFilter = null;
  // Every filter in the list is validated, not just the first.
  next.edgeFilters = (next.edgeFilters ?? []).filter((f) => has(edgeFields, f.field));

  if (next.selectedGenes && genes) {
    const keep = [...next.selectedGenes].filter((g) => genes.has(g));
    // No overlap at all means the allowlist says nothing about this panel.
    // Falling back to "no filter" matches what a dataset change does; an empty
    // Set would mean "show no species", which is a stranger thing to inherit.
    next.selectedGenes = keep.length ? new Set(keep) : null;
  }
  if (next.hiddenLrms && lrms) {
    next.hiddenLrms = new Set([...next.hiddenLrms].filter((l) => lrms.has(l)));
  }

  next.categoricalOverrides = filterKeys(next.categoricalOverrides, (k) => {
    const [scope, field] = k.split("::");
    return has(scope === "edge" ? edgeFields : cellFields, field);
  });
  next.categoryColorOverrides = filterKeys(next.categoryColorOverrides,
    (k) => has(cellFields, k.split("::")[0]));
  next.transcriptColorOverrides = filterKeys(next.transcriptColorOverrides,
    (k) => has(genes, k));

  if (!sameDataset) {
    next.cellColorClamp = { low: null, high: null };
    next.edgeColorClamp = { low: null, high: null };
  }
  return next;
}

/**
 * One panel's dataset-bound state.
 *
 * `dataset: null` on init; DatasetPicker fills it from /spatial/datasets.
 * Everything else is derived from whichever dataset is loaded, which is exactly
 * why it cannot live at the top level once two panels can show two datasets:
 * image dimensions, pixel size, capabilities, gene panel, LRM vocabulary and
 * value ranges all differ between them.
 */
export function makePanel() {
  return {
    dataset: null,
    activeImage: null,
    imageSize: { w: null, h: null },     // from the DZI descriptor, when OSD opens
    platformCapabilities: null,          // /spatial/{ds}/info -> has_transcripts, unit_label, …
    pixelSize: 1.0,                      // µm per image pixel; drives measurement + zoom match
    edgeFile: "edges.parquet",
    lrmCatalogue: [],
    allGenes: [],
    genesLoaded: false,
    cellColorRange: { vmin: null, vmax: null },
    edgeColorRange: { vmin: null, vmax: null },
    cellColorType: "continuous",
    cellColorCategories: [],
    transcriptStats: { shown: 0, total: 0 },
    cellBoundaryStats: { shown: 0, total: 0 },
    settings: makeSettings(),
  };
}

export const useStore = create((set, get) => ({
  apiBase: API,

  // ══ Per-panel state ═══════════════════════════════════════════════════════
  //
  // Everything here is bound to *a dataset*, so with two panels showing two
  // datasets it cannot be global. Style and choice settings (layer opacity,
  // palettes, filters, colour-by) stay global for now: one sidebar drives both
  // panels, which is what makes a side-by-side comparison comparable. Phase 2
  // splits those per panel behind sidebar tabs.
  //
  // Read as `panels[panelIndex]`. Panel 1 exists even in single mode so nothing
  // has to guard on panelCount.
  panels: [makePanel(), makePanel()],

  // Generic shallow patch. Specific transitions that must reset dependent state
  // (setPanelDataset, setPanelEdgeFile) are separate below.
  patchPanel: (i, patch) => set((s) => {
    const next = [...s.panels];
    next[i] = { ...next[i], ...patch };
    return { panels: next };
  }),

  // Switching a panel's dataset resets everything derived from the old one.
  // activeImage is cleared because image names are platform-specific
  // ("morphology" on Xenium, "Roi1_DAPI" on seqFISH), and the picker only learns
  // the new list asynchronously — without this, OSD spends that window asking
  // the new dataset for the old dataset's image and logging 404s.
  setPanelDataset: (i, dataset) => set((s) => {
    // Only the settings that name a *column, gene or mechanism* are reset. The
    // rest (widths, palettes, layer visibility, densities) survive a dataset
    // change and always have — resetting them would be a regression, which is
    // why this is a named patch rather than a fresh makeSettings().
    const RESET = {
      selectedGenes: null,
      hiddenLrms: new Set(),
      categoricalOverrides: {},
      cellFilter: null,
      sendingFilter: null,
      receivingFilter: null,
      edgeFilters: [],
      categoryColorOverrides: {},
      transcriptColorOverrides: {},
      colorBy: { mode: "off", field: null },
      cellColorClamp: { low: null, high: null },
      edgeColorClamp: { low: null, high: null },
    };
    // The panel whose dataset changed is always reset. The *others* are reset
    // only while the panels are linked, where they share one set of values and
    // leaving them would strand a filter naming a column the new dataset lacks
    // — which 400s on every viewport change.
    //
    // Unlinked, reaching across would contradict the toggle the user just set:
    // the sidebar says "editing panel 1 only" while an action on panel 2
    // destroys panel 1's work. That was the pre-2b behaviour and the cost
    // recorded in CLAUDE.md; the link toggle is what makes it fixable, so 2d
    // lands with 2b rather than after it.
    const panels = s.panels.map((p, idx) => {
      const base = idx === i ? { ...makePanel(), dataset } : p;
      const reset = idx === i || s.linkSettings;
      return { ...base, settings: reset ? { ...p.settings, ...RESET } : p.settings };
    });
    // Selections belong to a dataset; drop any that pointed at the old one.
    const sel = s.selection && s.selection.panelIndex === i ? null : s.selection;
    const nb = s.neighborhood && s.neighborhood.panelIndex === i ? null : s.neighborhood;
    return { panels, selection: sel, neighborhood: nb };
  }),

  // The LRM catalogue, colour range and edge filter are all specific to one
  // edges.parquet and must be re-derived when the source changes.
  setPanelEdgeFile: (i, edgeFile) => set((s) => {
    const next = [...s.panels];
    next[i] = {
      ...next[i], edgeFile, lrmCatalogue: [],
      edgeColorRange: { vmin: null, vmax: null },
    };
    const sel = s.selection && s.selection.panelIndex === i && s.selection.kind === "edge"
      ? null : s.selection;
    // Mechanism and edge-column names are specific to one edge file, so the
    // panel that changed is always cleared; the others only while linked.
    const panels = next.map((p, idx) => (idx === i || s.linkSettings)
      ? { ...p, settings: { ...p.settings, hiddenLrms: new Set(), edgeFilters: [] } }
      : p);
    return { panels, selection: sel };
  }),

  // ── Selection ─────────────────────────────────────────────────────────────
  // Carries the panel it came from, so the info panels know which dataset to
  // query. Without that they would resolve a panel-1 click against panel 0's
  // dataset and show the wrong cell.
  //   { panelIndex, kind: "cell" | "edge", cell? , edge? }
  selection: null,
  // Every selection change drops the neighbourhood with it. A highlight left
  // over from a previous cell would sit on unrelated tissue and look like the
  // answer for the cell now selected.
  setSelectedCell: (cell, panelIndex = 0) =>
    set({ selection: cell ? { panelIndex, kind: "cell", cell } : null,
          neighborhood: null }),
  setSelectedEdge: (edge, panelIndex = 0) =>
    set({ selection: edge ? { panelIndex, kind: "edge", edge } : null,
          neighborhood: null }),
  clearSelection: () => set({ selection: null, neighborhood: null }),

  // ── Local neighbourhood (issue #60) ───────────────────────────────────────
  // { panelIndex, cellId, data } — data is the /neighborhood response. Carries
  // its panel because the highlight is drawn in one panel only, like
  // annotations and selection: the coordinates are that dataset's image pixels.
  //
  // Computed server-side and never from the frontend's `edges` array, which is
  // density-sampled and viewport-bounded — deriving it here would silently
  // under-count neighbours and change as you pan.
  neighborhood: null,
  setNeighborhood: (nb) => set({ neighborhood: nb }),
  clearNeighborhood: () => set({ neighborhood: null }),

  // ── Categorical / continuous override (issue #35) ─────────────────────────
  // Keyed "cell::<field>" / "edge::<field>" → true | false. Absent means
  // auto-detect, which is what the backend does when `categorical` is null.
  // Seurat writes cluster IDs as integers, so dtype alone routes them to a
  // viridis gradient; this is how the user says "these are twenty categories".

  // ── Metadata subsetting (issue #45) ───────────────────────────────────────
  // A filter is { field, values: string[] | null, min, max, includeMissing }.
  // null means no filter. `values` is a categorical allowlist; min/max an
  // inclusive numeric range. Applied server-side before sampling, so narrowing
  // to a rare cluster shows all of it rather than a sample of a sample.
  //
  // cellFilter also governs edges: an edge is drawn only when BOTH endpoints
  // survive it — superseded by sendingFilter/receivingFilter, which apply to one
  // endpoint each and are independent of the cell layer entirely (issue #59).

  // ── Shared colour scale across panels ─────────────────────────────────────
  // On by default, and this is a figure-integrity setting rather than a
  // preference: two viridis panels that each auto-ranged to their own data look
  // comparable and are not. Panel A's yellow might be 40 counts and panel B's
  // 4,000. With this on, both panels map through one range computed across both,
  // so the single legend describes everything on screen.
  //
  // Unlock it when one panel's range is so much narrower that shared scaling
  // flattens it — then the panels are individually readable but not comparable,
  // which is the trade you are making knowingly.
  linkColorScale: true,
  setLinkColorScale: (v) => set({ linkColorScale: v }),


  // ── Viewport (image pixel coords, kept in sync with OpenSeadragon) ────────
  // One entry per panel; panel 1 is only used in split-screen mode.
  // viewports        — expanded bbox used by data-fetching hooks (may be larger than
  //                    the true visible area when the panel is rotated, to ensure all
  //                    visible corners are covered).
  // viewportActual   — un-expanded OSD bounds (true visible area); used only by the
  //                    ⇔ Match zoom feature so it matches the real viewport width.
  viewports: [null, null],
  setViewport: (viewport, panelIndex = 0) => set((s) => {
    const next = [...s.viewports];
    next[panelIndex] = viewport;
    return { viewports: next };
  }),
  viewportActual: [null, null],
  setViewportActual: (viewport, panelIndex = 0) => set((s) => {
    const next = [...s.viewportActual];
    next[panelIndex] = viewport;
    return { viewportActual: next };
  }),

  // ── Split-screen ──────────────────────────────────────────────────────────
  panelCount: 1,
  setPanelCount: (n) => set({ panelCount: n }),

  // Zoom-match request: set to { fromPanel } to tell the OTHER panel to adopt
  // the same zoom level (visible image area) while keeping its own center.
  // Consumed and cleared by the target ViewerPanel's useEffect.
  pendingZoomMatch: null,
  requestZoomMatch: (fromPanel) => set({ pendingZoomMatch: { fromPanel } }),
  clearZoomMatch: () => set({ pendingZoomMatch: null }),

  // ── Per-panel rotation ────────────────────────────────────────────────────
  // Rotation angle in degrees (0–359) for each panel.
  // Applied to OSD tile display (setRotation) and deck.gl layer modelMatrix.
  panelRotations: [0, 0],
  setPanelRotation: (panelIndex, angle) => set((s) => {
    const next = [...s.panelRotations];
    next[panelIndex] = ((Math.round(angle) % 360) + 360) % 360;
    return { panelRotations: next };
  }),

  // ── Display settings ──────────────────────────────────────────────────────
  //
  // These live in `panels[i].settings`, not at the top level. Every setter below
  // goes through `patchSettings`, which in Phase 2a writes to *all* panels — so
  // one sidebar still drives both and behaviour is identical to before the move.
  // Phase 2b adds `linkSettings` and `activePanel`, at which point this one
  // function becomes the single place where "write to one panel or all of them"
  // is decided. Keeping the named setters means call sites never had to change.

  // ── Which panel the sidebar edits, and whether edits propagate ────────────
  //
  // activePanel is the tab the sidebar is pointed at. It is also what the
  // sidebar *reads*, so with the panels unlinked the controls show the values
  // for the panel you are editing rather than some blend.
  //
  // linkSettings defaults to true, which reproduces the pre-2b behaviour
  // exactly: one sidebar drives both panels. That default is deliberate — two
  // panels that each drifted to their own palette and clamp look comparable and
  // are not, which is the same figure-integrity argument behind linkColorScale.
  activePanel: 0,
  setActivePanel: (i) => set({ activePanel: i }),

  linkSettings: true,
  // Re-linking *syncs*: every panel adopts the active panel's settings. The
  // alternative — start propagating future edits but leave the existing
  // divergence in place — leaves a control labelled "linked" over two panels
  // that visibly differ, and the next single edit converges them only
  // partially. Adopting one panel's state is the only reading of "linked" that
  // is true the moment you switch it on. Which panel wins is the tab you are
  // on, so it is visible and chosen rather than incidental.
  setLinkSettings: (v) => set((s) => {
    if (!v) return { linkSettings: false };
    const source = s.panels[s.activePanel]?.settings ?? s.panels[0].settings;
    return {
      linkSettings: true,
      panels: s.panels.map((p) => ({ ...p, settings: cloneSettings(source) })),
    };
  }),

  /**
   * Copy one panel's settings onto another.
   *
   * The explicit form of what the link toggle does continuously: explore either
   * side freely, then force the other to match. Unlike linking it is one-shot,
   * so the panels stay independent afterwards.
   *
   * `allowed` is the target's vocabulary; see sanitiseSettings. Passing null
   * copies verbatim, which is only safe when both panels show the same dataset.
   */
  pushSettings: (from, to, allowed = null) => set((s) => {
    const src = s.panels[from]?.settings;
    if (!src || !s.panels[to] || from === to) return {};
    const sameDataset = s.panels[from].dataset === s.panels[to].dataset;
    const next = sanitiseSettings(cloneSettings(src), allowed, sameDataset);
    return { panels: s.panels.map((p, i) => (i === to ? { ...p, settings: next } : p)) };
  }),

  /**
   * Merge a patch into panel settings.
   *
   * An explicit `panelIndex` always wins. Otherwise the link state decides: all
   * panels when linked, just the active one when not. This is the single place
   * that choice is made — every named setter routes through here, so none of
   * them has to know about tabs or linking.
   */
  patchSettings: (patch, panelIndex = null) =>
    set((s) => {
      const targets = panelIndex !== null
        ? [panelIndex]
        : (s.linkSettings ? s.panels.map((_, i) => i) : [s.activePanel]);
      return {
        panels: s.panels.map((p, i) =>
          targets.includes(i)
            ? { ...p, settings: { ...p.settings, ...patch } }
            : p),
      };
    }),

  /**
   * Read one setting. Defaults to the panel the sidebar is editing, which is
   * what read-modify-write setters need: unlinked, `toggleLrm` must toggle
   * against the active panel's Set, not panel 0's.
   */
  getSetting: (key, panelIndex = null) => {
    const s = get();
    const i = panelIndex ?? s.activePanel;
    return s.panels[i]?.settings?.[key];
  },

  // Read-modify-write on a nested map, so it derives from the active panel and
  // then goes through the ordinary targeting rule.
  setLayerProp: (id, prop, value) => {
    const cur = get().getSetting("layers");
    get().patchSettings({ layers: { ...cur, [id]: { ...cur[id], [prop]: value } } });
  },

  setCellBoundaryFraction: (v) => get().patchSettings({
    cellBoundaryFraction: v !== null ? Math.max(0.0001, Math.min(1.0, v)) : null,
  }),
  setTranscriptFraction: (f) =>
    get().patchSettings({ transcriptFraction: Math.max(0.0001, Math.min(1.0, f)) }),

  setCellColorClamp: (low, high) => get().patchSettings({ cellColorClamp: { low, high } }),
  setEdgeColorClamp: (low, high) => get().patchSettings({ edgeColorClamp: { low, high } }),

  setCellColorEnabled: (v) => get().patchSettings({ cellColorEnabled: v }),
  setColorBy: (mode, field) => get().patchSettings({ colorBy: { mode, field } }),
  setCellColorPalette: (p) => get().patchSettings({ cellColorPalette: p }),

  setEdgeWidth: (v) => get().patchSettings({ edgeWidth: v }),
  setShowArrowheads: (v) => get().patchSettings({ showArrowheads: v }),
  setArrowStyle: (v) => get().patchSettings({ arrowStyle: v }),
  setArrowheadScale: (v) => get().patchSettings({ arrowheadScale: v }),
  setEdgeDensity: (v) => get().patchSettings({ edgeDensity: v }),
  setEdgeMinStrength: (v) => get().patchSettings({ edgeMinStrength: v }),
  setEdgeColorBy: (mode, field) => get().patchSettings({ edgeColorBy: { mode, field } }),
  setEdgeColorPalette: (p) => get().patchSettings({ edgeColorPalette: p }),
  setEdgeDirectional: (v) => get().patchSettings({ edgeDirectional: v }),
  setEdgeOffset: (v) => get().patchSettings({ edgeOffset: v }),
  setShowAutocrine: (v) => get().patchSettings({ showAutocrine: v }),
  setAutocrineRadius: (v) => get().patchSettings({ autocrineRadius: v }),
  setAutocrineLineWidth: (v) => get().patchSettings({ autocrineLineWidth: v }),

  setCellFilter: (f) => get().patchSettings({ cellFilter: f }),
  setSendingFilter: (f) => get().patchSettings({ sendingFilter: f }),
  setReceivingFilter: (f) => get().patchSettings({ receivingFilter: f }),
  // Edge-table filters are a list, and-ed together. Index-addressed so the UI
  // can add, replace and remove rows without rebuilding the array itself.
  setEdgeFilterAt: (i, f) => {
    const next = [...(get().getSetting("edgeFilters") ?? [])];
    if (f === null) next.splice(i, 1); else next[i] = f;
    get().patchSettings({ edgeFilters: next.filter(Boolean) });
  },
  setEdgeFilters: (list) => get().patchSettings({ edgeFilters: list ?? [] }),

  // ── LRM mechanism filter ───────────────────────────────────────────────────
  // Keyed on the "ligand|receptor" string, so a mechanism present in both
  // datasets is one checkbox governing both — which is the point of a
  // comparison. The catalogue it is checked against is per panel
  // (panels[i].lrmCatalogue); the sidebar shows the union.
  toggleLrm: (lrm) => {
    const cur = get().getSetting("hiddenLrms") ?? new Set();
    const next = new Set(cur);
    if (next.has(lrm)) next.delete(lrm); else next.add(lrm);
    get().patchSettings({ hiddenLrms: next });
  },
  setAllLrmsVisible: () => get().patchSettings({ hiddenLrms: new Set() }),
  // "none" has to cover every mechanism visible in either panel, or one side
  // keeps drawing.
  hideAllLrms: () => {
    const all = get().panels.flatMap((p) => p.lrmCatalogue)
      .map((e) => e.lrm ?? `${e.ligand}|${e.receptor}`);
    get().patchSettings({ hiddenLrms: new Set(all) });
  },

  // ── Categorical / continuous override (issue #35) ─────────────────────────
  setCategoricalOverride: (scope, field, value) => {
    const next = { ...(get().getSetting("categoricalOverrides") ?? {}) };
    if (value === null || value === undefined) delete next[`${scope}::${field}`];
    else next[`${scope}::${field}`] = value;
    get().patchSettings({ categoricalOverrides: next });
  },

  // ── User-chosen colours ───────────────────────────────────────────────────
  setCategoryColorOverride: (field, cat, rgba) => get().patchSettings({
    categoryColorOverrides: {
      ...(get().getSetting("categoryColorOverrides") ?? {}), [`${field}::${cat}`]: rgba },
  }),
  mergeCategoryColorOverrides: (map) => get().patchSettings({
    categoryColorOverrides: { ...(get().getSetting("categoryColorOverrides") ?? {}), ...map },
  }),
  resetCategoryColorOverrides: () => get().patchSettings({ categoryColorOverrides: {} }),

  setTranscriptColorOverride: (gene, rgba) => get().patchSettings({
    transcriptColorOverrides: {
      ...(get().getSetting("transcriptColorOverrides") ?? {}), [gene]: rgba },
  }),
  mergeTranscriptColorOverrides: (map) => get().patchSettings({
    transcriptColorOverrides: { ...(get().getSetting("transcriptColorOverrides") ?? {}), ...map },
  }),
  resetTranscriptColorOverrides: () => get().patchSettings({ transcriptColorOverrides: {} }),

  // ── Annotations ───────────────────────────────────────────────────────────
  // annotationMode: current interaction mode
  annotationMode: "pan", // "pan" | "region" | "measure"
  setAnnotationMode: (mode) => set({ annotationMode: mode }),

  // Every annotation belongs to the panel it was drawn in, and this is not
  // cosmetic. Coordinates are image pixels of *that panel's* dataset, so a
  // polygon over a 6.5 mm Visium capture area reappearing in a panel showing a
  // 55 µm seqFISH ROI lands somewhere meaningless. Two silent consequences are
  // worse than the visual one: CSV export resolves the region's cell ids
  // against its panel's dataset, and a measurement label multiplies distPx by
  // its panel's pixelSize. Both give confidently wrong answers if an annotation
  // is read by the wrong panel.
  //
  // `panelIndex` is absent on anything created before this existed; the
  // selectors below treat that as panel 0, which is the only place it could
  // have come from.

  // activeRegion: vertices of the polygon currently being drawn (image px).
  // activeRegionPanel: which panel is drawing, so the in-progress outline and
  // its vertex markers do not also appear in the other panel.
  activeRegion: [],
  activeRegionPanel: null,
  addRegionPoint: (pt, panelIndex = 0) =>
    set((s) => ({ activeRegion: [...s.activeRegion, pt], activeRegionPanel: panelIndex })),
  cancelActiveRegion: () => set({ activeRegion: [], activeRegionPanel: null }),

  // regions: completed annotation polygons
  // each: { id, points [[x,y],...], selectedCellIds [str,...], color [r,g,b], panelIndex }
  regions: [],
  commitRegion: (region, panelIndex = 0) =>
    set((s) => ({
      regions: [...s.regions, { ...region, panelIndex }],
      activeRegion: [],
      activeRegionPanel: null,
    })),
  removeRegion: (id) =>
    set((s) => ({ regions: s.regions.filter((r) => r.id !== id) })),

  // measurements: [{id, p1:[x,y], p2:[x,y], distPx, panelIndex}]
  measurements: [],
  addMeasurement: (m, panelIndex = 0) =>
    set((s) => ({ measurements: [...s.measurements, { ...m, panelIndex }] })),
  removeMeasurement: (id) =>
    set((s) => ({ measurements: s.measurements.filter((m) => m.id !== id) })),

  regionsForPanel: (i) =>
    get().regions.filter((r) => (r.panelIndex ?? 0) === i),
  measurementsForPanel: (i) =>
    get().measurements.filter((m) => (m.panelIndex ?? 0) === i),

  // Scoped to a panel, because the Clear button lives in each panel's own
  // toolbar — clearing from one panel must not wipe the other's work. Omitting
  // the index clears everything, which is what a dataset-level reset wants.
  clearAnnotations: (panelIndex) =>
    set((s) => (panelIndex === undefined
      ? { activeRegion: [], activeRegionPanel: null, regions: [], measurements: [] }
      : {
          activeRegion: s.activeRegionPanel === panelIndex ? [] : s.activeRegion,
          activeRegionPanel: s.activeRegionPanel === panelIndex ? null : s.activeRegionPanel,
          regions: s.regions.filter((r) => (r.panelIndex ?? 0) !== panelIndex),
          measurements: s.measurements.filter((m) => (m.panelIndex ?? 0) !== panelIndex),
        })),

  // ── Rendering / loading state ─────────────────────────────────────────────
  // loadingKeys: Set of string keys currently in flight (one entry per panel).
  // The status badge is visible whenever loadingKeys.size > 0.
  loadingKeys: new Set(),
  setLoadingKey: (key, loading) => set((s) => {
    const next = new Set(s.loadingKeys);
    if (loading) next.add(key); else next.delete(key);
    return { loadingKeys: next };
  }),

  // ── Transcript species filter ──────────────────────────────────────────────
  // selectedGenes: null = no filter (show all); Set<string> = allowlist (show only these).
  // The selection is dataset-scoped and persists across pan/zoom.
  setSelectedGenes: (genes) => get().patchSettings({ selectedGenes: genes }),
  toggleSelectedGene: (gene) => {
    const s = get();
    {
      // The universe of genes is the union across the visible panels — the same
      // list the picker renders.
      const all = [...new Set(
        s.panels.slice(0, s.panelCount).flatMap((p) => p.allGenes ?? [])
      )];
      const selectedGenes = s.getSetting("selectedGenes");

      if (selectedGenes === null) {
        // "Show all" renders EVERY checkbox ticked, so a click here means
        // "uncheck this one" — exclude it and keep the rest.
        //
        // This used to start an allowlist containing only the clicked gene, the
        // exact opposite of what the click meant. On a 480-gene Xenium panel
        // that silently narrowed the transcript layer from 200,000 dots to ~360,
        // which reads as "transcripts are broken" rather than "you filtered to
        // one gene". The checkbox said checked; the click has to mean uncheck.
        if (all.length === 0) return;             // list not loaded yet — ignore
        return s.patchSettings({ selectedGenes: new Set(all.filter((g) => g !== gene)) });
      }

      const next = new Set(selectedGenes);
      if (next.has(gene)) next.delete(gene); else next.add(gene);
      // Back to everything selected is the same as no filter. Collapsing keeps
      // the semantics single-valued and keeps hundreds of gene names out of the
      // request URL.
      if (all.length > 0 && next.size === all.length) {
        return s.patchSettings({ selectedGenes: null });
      }
      return s.patchSettings({ selectedGenes: next });
    }
  },
}));

// Dev-only handle for debugging from the browser console.
if (typeof window !== "undefined" && import.meta.env?.DEV) window.__tpStore = useStore;
