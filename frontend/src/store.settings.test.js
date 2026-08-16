/**
 * Display settings live in `panels[i].settings` (Phase 2a).
 *
 * The contract this stage promises is *behaviour-preserving*: settings moved
 * out of the top level of the store, but every write still lands on every
 * panel, so one sidebar drives both exactly as before. These tests pin that
 * down, and pin down the two things easiest to get wrong while moving them.
 *
 * When Phase 2b adds `linkSettings`, the "writes reach every panel" tests here
 * become the *linked* case and gain unlinked counterparts. Deleting them
 * instead would be the tell that 2b broke the default.
 *
 * See docs/split_screen_phase2.md.
 */
import { describe, it, expect, beforeEach } from "vitest";
import { useStore, makePanel } from "./store";

const S = () => useStore.getState();
const settings = (i) => S().panels[i].settings;

// Built through the store's own factory so these tests cannot drift from the
// real defaults.
beforeEach(() => {
  useStore.setState({ panels: [makePanel(), makePanel()], panelCount: 2 });
});

describe("settings live on the panel, not the store root", () => {
  it("is not readable at the top level any more", () => {
    // Guards the migration: a stale `s.edgeWidth` read elsewhere would silently
    // be undefined rather than throwing, so assert the root really is clear.
    for (const key of ["layers", "edgeWidth", "colorBy", "hiddenLrms", "selectedGenes",
                       "cellFilter", "edgeFilter", "cellColorClamp", "edgeDensity"]) {
      expect(S()[key], `store root should not carry "${key}"`).toBeUndefined();
    }
  });

  it("gives every panel its own settings object", () => {
    // makeSettings() is a factory, not a shared constant: a panel created now
    // must not alias the containers of one created earlier, or editing the
    // layer map in one panel would edit it in both.
    expect(settings(0)).not.toBe(settings(1));
    expect(settings(0).layers).not.toBe(settings(1).layers);
    expect(settings(0).hiddenLrms).not.toBe(settings(1).hiddenLrms);
  });
});

describe("2a keeps every write in lockstep across panels", () => {
  it("propagates a scalar setter to both panels", () => {
    S().setEdgeWidth(7);
    expect(settings(0).edgeWidth).toBe(7);
    expect(settings(1).edgeWidth).toBe(7);
  });

  it("propagates a nested layer edit to both panels", () => {
    S().setLayerProp("transcripts", "visible", true);
    expect(settings(0).layers.transcripts.visible).toBe(true);
    expect(settings(1).layers.transcripts.visible).toBe(true);
    // and leaves its siblings alone
    expect(settings(0).layers.cellSegments.visible).toBe(true);
    expect(settings(0).layers.transcripts.opacity).toBe(0.8);
  });

  it("propagates a Set-valued setter", () => {
    S().toggleLrm("A|B");
    expect([...settings(0).hiddenLrms]).toEqual(["A|B"]);
    expect([...settings(1).hiddenLrms]).toEqual(["A|B"]);
  });

  it("a write to one panel cannot leak into the other", () => {
    // Panels may share a reference to a settings *value* — every setter builds a
    // new container rather than mutating, so sharing an immutable value is
    // correct and cheaper than cloning per panel. What must hold is that a
    // single-panel write leaves the other panel alone; that is the invariant
    // 2b's unlinked mode depends on, so it is pinned here rather than there.
    S().toggleLrm("A|B");                          // both panels now hold it
    S().patchSettings({ hiddenLrms: new Set(["X|Y"]) }, 1);
    expect([...settings(0).hiddenLrms]).toEqual(["A|B"]);
    expect([...settings(1).hiddenLrms]).toEqual(["X|Y"]);
  });

  it("propagates a keyed override map", () => {
    S().setCategoricalOverride("cell", "cluster", true);
    expect(settings(0).categoricalOverrides).toEqual({ "cell::cluster": true });
    expect(settings(1).categoricalOverrides).toEqual({ "cell::cluster": true });
  });
});

describe("patchSettings can target one panel", () => {
  it("writes only where told", () => {
    // Not reachable through the UI in 2a, but it is the mechanism 2b's link
    // toggle switches on, so it is worth having pinned before then.
    S().patchSettings({ edgeWidth: 9 }, 1);
    expect(settings(0).edgeWidth).toBe(2);
    expect(settings(1).edgeWidth).toBe(9);
  });
});

describe("a dataset change resets only the name-bound settings", () => {
  beforeEach(() => {
    S().setEdgeWidth(9);
    S().setCellColorPalette("plasma");
    S().setLayerProp("transcripts", "visible", true);
    S().toggleLrm("A|B");
    S().setColorBy("metadata", "seurat_clusters");
    S().setCellFilter({ field: "cluster", values: ["4"] });
    S().setSelectedGenes(new Set(["Gapdh"]));
  });

  it("clears filters, colour-by and mechanism selection", () => {
    S().setPanelDataset(0, "other-dataset");
    expect(settings(0).cellFilter).toBeNull();
    expect(settings(0).selectedGenes).toBeNull();
    expect(settings(0).colorBy).toEqual({ mode: "off", field: null });
    expect([...settings(0).hiddenLrms]).toEqual([]);
  });

  it("keeps geometry, palette and layer visibility", () => {
    // These were never reset by a dataset change. Rebuilding the panel from
    // makePanel() would have quietly wiped them — the one real regression risk
    // in moving settings onto the panel object.
    S().setPanelDataset(0, "other-dataset");
    expect(settings(0).edgeWidth).toBe(9);
    expect(settings(0).cellColorPalette).toBe("plasma");
    expect(settings(0).layers.transcripts.visible).toBe(true);
  });

  it("still resets the other panel too, as it did before 2a", () => {
    // The known cost documented in CLAUDE.md. Phase 2d narrows this to the one
    // panel; until then, changing it would be an unplanned behaviour change.
    S().setPanelDataset(0, "other-dataset");
    expect(settings(1).cellFilter).toBeNull();
    expect(settings(1).colorBy).toEqual({ mode: "off", field: null });
  });
});

describe("gene toggle reads and writes through settings", () => {
  beforeEach(() => {
    useStore.setState({
      panels: S().panels.map((p) => ({ ...p, allGenes: ["A", "B", "C"] })),
    });
  });

  it("first click on the all-selected state excludes that gene", () => {
    expect(settings(0).selectedGenes).toBeNull();
    S().toggleSelectedGene("B");
    expect([...settings(0).selectedGenes].sort()).toEqual(["A", "C"]);
    expect([...settings(1).selectedGenes].sort()).toEqual(["A", "C"]);
  });

  it("collapses back to no filter when everything is re-selected", () => {
    S().toggleSelectedGene("B");
    S().toggleSelectedGene("B");
    expect(settings(0).selectedGenes).toBeNull();
  });
});
