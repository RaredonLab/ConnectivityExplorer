/**
 * Annotations must belong to the panel they were drawn in.
 *
 * These are the first frontend tests in the repo. They exist because the split
 * screen shipped with annotations still global: `regions` and `measurements`
 * carried no panel, so a region drawn on one dataset was re-drawn on the other
 * at identical *image pixel* coordinates — a polygon over a 6.5 mm Visium
 * capture area reappearing over a 55 µm seqFISH ROI, where it means nothing.
 *
 * Two consequences were worse than the visual one, because they are silent:
 *
 *  - CSV export read `region.panelIndex ?? 0` while nothing ever *wrote*
 *    panelIndex, so every export resolved against panel 0's dataset. Exporting
 *    a region drawn in panel 1 posted panel 1's cell ids to panel 0's endpoint.
 *  - Measurement labels multiply `distPx` by the *rendering* panel's pixelSize,
 *    so one measurement read as two different distances in the two panels.
 *
 * All three are the same root cause, so they are tested together. Store logic
 * is plain JS and needs no DOM.
 */
import { describe, it, expect, beforeEach } from "vitest";
import { useStore } from "./store";

const S = () => useStore.getState();

beforeEach(() => {
  S().clearAnnotations();
});

describe("regions carry the panel they were drawn in", () => {
  it("records panelIndex on commit", () => {
    S().commitRegion({ id: 1, points: [[0, 0], [1, 0], [1, 1]], selectedCellIds: ["a"], color: [1, 2, 3] }, 1);
    expect(S().regions[0].panelIndex).toBe(1);
  });

  it("defaults to panel 0 when no panel is given", () => {
    S().commitRegion({ id: 2, points: [], selectedCellIds: [], color: [0, 0, 0] });
    expect(S().regions[0].panelIndex).toBe(0);
  });

  it("selects only the regions belonging to a panel", () => {
    S().commitRegion({ id: 1, points: [], selectedCellIds: [], color: [0, 0, 0] }, 0);
    S().commitRegion({ id: 2, points: [], selectedCellIds: [], color: [0, 0, 0] }, 1);
    S().commitRegion({ id: 3, points: [], selectedCellIds: [], color: [0, 0, 0] }, 1);

    expect(S().regionsForPanel(0).map((r) => r.id)).toEqual([1]);
    expect(S().regionsForPanel(1).map((r) => r.id)).toEqual([2, 3]);
  });

  it("treats a legacy region with no panelIndex as panel 0", () => {
    // Regions persisted or constructed before this change have no panelIndex.
    // Panel 0 is the only place they could have come from.
    useStore.setState({ regions: [{ id: 9, points: [], selectedCellIds: [], color: [0, 0, 0] }] });
    expect(S().regionsForPanel(0).map((r) => r.id)).toEqual([9]);
    expect(S().regionsForPanel(1)).toEqual([]);
  });
});

describe("measurements carry the panel they were drawn in", () => {
  it("records panelIndex and selects per panel", () => {
    S().addMeasurement({ id: 1, p1: [0, 0], p2: [10, 0], distPx: 10 }, 0);
    S().addMeasurement({ id: 2, p1: [0, 0], p2: [20, 0], distPx: 20 }, 1);

    expect(S().measurementsForPanel(0).map((m) => m.id)).toEqual([1]);
    expect(S().measurementsForPanel(1).map((m) => m.id)).toEqual([2]);
  });

  it("a measurement is never shown by the panel that did not make it", () => {
    // The label multiplies distPx by the rendering panel's pixelSize, so a
    // measurement leaking across panels reports a wrong distance rather than a
    // misplaced one. 100 px is 72.5 µm on Visium and 10.7 µm on seqFISH.
    S().addMeasurement({ id: 1, p1: [0, 0], p2: [100, 0], distPx: 100 }, 0);
    expect(S().measurementsForPanel(1)).toEqual([]);
  });
});

describe("the in-progress polygon belongs to one panel", () => {
  it("tracks which panel is drawing, and clears it on cancel", () => {
    S().addRegionPoint([1, 1], 1);
    expect(S().activeRegionPanel).toBe(1);
    expect(S().activeRegion).toEqual([[1, 1]]);

    S().cancelActiveRegion();
    expect(S().activeRegionPanel).toBeNull();
    expect(S().activeRegion).toEqual([]);
  });

  it("clears the drawing panel once the region is committed", () => {
    S().addRegionPoint([0, 0], 1);
    S().commitRegion({ id: 1, points: [[0, 0]], selectedCellIds: [], color: [0, 0, 0] }, 1);
    expect(S().activeRegion).toEqual([]);
    expect(S().activeRegionPanel).toBeNull();
  });
});

describe("clearing annotations is scoped to a panel", () => {
  beforeEach(() => {
    S().commitRegion({ id: 1, points: [], selectedCellIds: [], color: [0, 0, 0] }, 0);
    S().commitRegion({ id: 2, points: [], selectedCellIds: [], color: [0, 0, 0] }, 1);
    S().addMeasurement({ id: 3, p1: [0, 0], p2: [1, 1], distPx: 1 }, 0);
    S().addMeasurement({ id: 4, p1: [0, 0], p2: [1, 1], distPx: 1 }, 1);
  });

  it("clears only the given panel", () => {
    // The Clear button lives in each panel's own toolbar, so clearing from one
    // panel must not wipe the other panel's work.
    S().clearAnnotations(0);
    expect(S().regionsForPanel(0)).toEqual([]);
    expect(S().measurementsForPanel(0)).toEqual([]);
    expect(S().regionsForPanel(1).map((r) => r.id)).toEqual([2]);
    expect(S().measurementsForPanel(1).map((m) => m.id)).toEqual([4]);
  });

  it("clears everything when no panel is given", () => {
    S().clearAnnotations();
    expect(S().regions).toEqual([]);
    expect(S().measurements).toEqual([]);
  });
});

describe("#60 — the neighbourhood highlight is scoped and cleared", () => {
  const nb = { panelIndex: 1, cellId: "c1", data: { n_neighbors: 3 } };

  beforeEach(() => {
    useStore.setState({ neighborhood: null, selection: null });
  });

  it("is dropped when another cell is selected", () => {
    // A highlight left over from a previous cell would sit on unrelated tissue
    // and read as the answer for the cell now selected.
    useStore.setState({ neighborhood: nb });
    S().setSelectedCell({ cell_id: "c2" }, 1);
    expect(S().neighborhood).toBeNull();
  });

  it("is dropped when an edge is selected, and when selection is cleared", () => {
    useStore.setState({ neighborhood: nb });
    S().setSelectedEdge({ edge: "a|b" }, 0);
    expect(S().neighborhood).toBeNull();

    useStore.setState({ neighborhood: nb });
    S().clearSelection();
    expect(S().neighborhood).toBeNull();
  });

  it("is dropped when its own panel changes dataset", () => {
    // The ids and coordinates belong to the dataset that was showing.
    useStore.setState({ neighborhood: nb });
    S().setPanelDataset(1, "other-dataset");
    expect(S().neighborhood).toBeNull();
  });

  it("survives a dataset change in the other panel", () => {
    useStore.setState({ neighborhood: nb });
    S().setPanelDataset(0, "other-dataset");
    expect(S().neighborhood).toEqual(nb);
  });
});
