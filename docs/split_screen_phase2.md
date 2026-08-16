# Split screen, Phase 2 — per-panel settings

Status: **planned, not started.** Phase 1 shipped in v0.8.4 (PR #56).

Phase 1 made the two panels able to show two *different datasets*. Phase 2 makes
their *settings* independent, with an explicit way to re-link them. This document
exists because the Phase 2 plan was previously held only in conversation and was
lost; treat it as the specification.

---

## Where Phase 1 left things

Everything bound to *which dataset a panel shows* lives in `panels[panelIndex]`
(`store.js::makePanel()`): dataset, image, image size, capabilities, pixel size,
edge file, LRM catalogue, gene panel, colour ranges, and shown/total stats.

Everything describing *how the data looks* is still global: layer visibility and
opacity, palettes, colour-by, filters, LRM selection, edge geometry, sampling
fractions. One sidebar drives both panels, reconciled by `hooks/usePanels.js`
under a union-then-degrade rule.

That was the right first cut — one sidebar driving both panels is what makes a
side-by-side comparison *comparable*, and it is still the behaviour most users
want most of the time. Phase 2 does not take it away; it makes it a mode rather
than a constraint.

## What Phase 2 is for

Three concrete problems, in descending order of how often they bite.

**1. Changing one panel's dataset clears the other panel's settings.**
`setPanelDataset` resets every shared setting that names a column, gene or
mechanism — `selectedGenes`, `hiddenLrms`, `categoricalOverrides`, `cellFilter`,
`edgeFilter`, `colorBy`, the colour clamps, the colour overrides. It has to:
a filter naming a column the new dataset lacks returns 400 on every viewport
change. But because those settings are global, resetting them for one panel
resets them for both. Set up a careful view on the left, change the dataset on
the right, and the left panel's filter is gone. This is documented in CLAUDE.md
as a known cost and is the single strongest reason to do Phase 2.

**2. You cannot compare two renderings of the same data.** Same dataset in both
panels, cluster colouring on the left and gene expression on the right, is
currently impossible — `colorBy` is one value. The same is true of two LRM
selections, two filters, or two palettes.

**3. Genuinely unlike datasets want unlike settings.** A Visium spot panel and a
Xenium transcript panel do not want the same opacity, sampling fraction or edge
density. Union-then-degrade offers the control; it cannot give each panel its own
value.

## The design decision

**Per-panel settings with a global link toggle, defaulting to linked.** Reads
always resolve to `panels[i].settings[key]`. Writes go to the panel the sidebar is
editing, or to every panel when `linkSettings` is true.

Two alternatives were considered and rejected:

- *Global settings plus per-panel overrides* (`override[i][key] ?? shared[key]`).
  Smaller diff, but two sources of truth for every value. A slider then has to
  answer "am I showing the shared value or this panel's override, and which does
  dragging me write to?" — and the reset in problem 1 has to decide whether it
  clears the override, the shared value, or both. Ambiguity in exactly the place
  the current design is already confusing.

- *Always independent, no link.* Matches "explore either side freely" but breaks
  the default case: every setting change would need doing twice, and two panels
  drifting silently apart is precisely the figure-integrity problem
  `linkColorScale` was added to prevent.

The link toggle gives today's behaviour by default, independence on request, and
one unambiguous source of truth per panel.

**The push button comes along for free.** Once settings are per-panel,
"copy panel 1's settings to panel 2" is an object copy. This was the original
request — explore on either side, then force-match the other — and it is the
natural escape hatch when panels have drifted and you want them comparable again
without redoing the work.

## What moves, and what does not

| Moves into `panels[i].settings` | Stays global |
|---|---|
| `layers` (visibility + opacity, incl. `outlineOpacity`) | `panelCount`, `panelRotations`, `viewports`, `viewportActual` |
| `transcriptFraction`, `cellBoundaryFraction` | `selection` (already carries `panelIndex`) |
| `cellColorEnabled`, `colorBy`, `cellColorPalette`, `cellColorClamp` | `linkColorScale`, `linkSettings`, `activePanel` |
| `edgeColorBy`, `edgeColorPalette`, `edgeColorClamp` | `loadingKeys`, `apiBase` |
| `edgeWidth`, `edgeDensity`, `edgeMinStrength`, `edgeOffset`, `edgeDirectional` | `pendingZoomMatch` |
| `showArrowheads`, `arrowStyle`, `arrowheadScale` | |
| `showAutocrine`, `autocrineRadius`, `autocrineLineWidth` | |
| `hiddenLrms`, `selectedGenes` | |
| `cellFilter`, `edgeFilter`, `categoricalOverrides` | |
| `categoryColorOverrides`, `transcriptColorOverrides` | |

Roughly 30 keys move.

`linkColorScale` stays global and keeps its current meaning — it links the
computed colour *range* across panels, which is a separate question from whether
the two panels share a *palette* setting. Both linked is the common case; the
combination "same palette, independent ranges" is deliberately still reachable.

## Implementation surface

Measured, not estimated:

| | count | note |
|---|---|---|
| Files reading a moving setting | **4** | `LayerPanel.jsx`, `Viewer.jsx`, `AnnotationToolbar.jsx`, `CellInfoPanel.jsx` |
| Bare `useStore()` destructure sites | **17** | `const { layers, setLayerProp } = useStore()` |
| Selector-style `useStore((s) => …)` | **25** | across 6 files |
| Section components in `LayerPanel` | **19** | each reads the store directly |
| **Data hooks needing changes** | **0** | — |

The last row is the important one. `useTranscripts`, `useCellBoundaries`,
`useEdges`, `useCellColors` and `useEdgeColors` read nothing from the store —
every setting arrives as a prop from `ViewerPanel`. So once `ViewerPanel` reads
`panels[panelIndex].settings` instead of the globals, per-panel settings reach
the right fetches with no change to the fetch layer at all.

`ViewerPanel` is similarly cheap: it already receives `panelIndex`, so its reads
change shape but need no new plumbing.

**`LayerPanel` is the actual work.** Its 19 section components each call
`useStore()` directly and have no idea which panel they are editing. Threading a
`panelIndex` prop through all of them would be noisy and easy to get half-right.
Use a `PanelSettingsContext` holding the active panel index and a
`usePanelSettings()` hook returning `{...panels[i].settings, ...setters}`. Most
sites then change by one identifier — `useStore()` becomes `usePanelSettings()` —
and the destructuring below it is untouched.

> Aside worth fixing while in here: bare `useStore()` subscribes to the *whole*
> store, so every one of those 17 components re-renders on any state change,
> including every viewport update during a pan. Moving to a scoped hook is a
> natural moment to narrow those subscriptions.

## Staged plan

Each stage is independently reviewable and leaves the app working.

**2a — container and hook, behaviour frozen. DONE.** The ~30 keys live in
`panels[i].settings`, built by `makeSettings()`. `hooks/usePanelSettings.js` adds
`PanelIndexContext` and `usePanelSettings()`, which returns the store merged with
that panel's settings — so the 17 call sites changed by one identifier and their
destructuring was untouched. Every write goes through `patchSettings(patch,
panelIndex = null)`, which with a null index writes to all panels. No UI change.

Verified behaviour-preserving by capturing all 28 effective setting values before
the change and diffing after: **no differences**. `store.settings.test.js` (13
tests) pins the contract, including the one real regression risk — a dataset
change must reset the name-bound settings *only*, since rebuilding the panel from
`makePanel()` would also have wiped geometry, palettes and layer visibility,
which a dataset change never touched.

**2b — sidebar tabs and the link toggle.** Add `activePanel` and
`linkSettings: true`. `usePanelIndex` already falls back to `s.activePanel ?? 0`,
so introducing the key is enough to make the sidebar follow the active tab. `setSetting` writes to all panels when linked, to
`activePanel` when not. Tabs render only when `panelCount === 2`. Single-panel
mode gets no tabs and writes to panel 0, so it is untouched.

**2c — push settings.** `pushSettings(from, to)` deep-copies
`panels[from].settings` into `panels[to]`. One button per panel header, labelled
with its direction. Needs a guard: settings naming a column, gene or LRM that the
target dataset does not have must be dropped rather than copied, or the target
panel starts 400ing on every viewport change — the same failure mode as
problem 1. Validate against the target's schema/gene list before writing.

**2d — narrow the dataset-change reset.** With settings per-panel,
`setPanelDataset(i, …)` resets only `panels[i].settings`. This is the payoff for
problem 1 and is a two-line change once 2a has landed — but only correct after
2a, which is why it is last rather than first.

**2e — docs.** CLAUDE.md's Split-Screen section, and `docs/index.html` (the
hosted manual) for the tabs, the link toggle and the push button.

## Risks

**Frontend test coverage is minimal.** Vitest now exists (`npm test` in
`frontend/`), but it covers only annotation panel-scoping — the golden-snapshot
guard is still backend-only, and a 30-key state migration across 19 components
has almost no automated check behind it. This remains the main risk in the plan.
Store logic is plain JS and testable without a DOM, so 2a is a good excuse to
cover the settings reducers as they move.
Mitigations: keep 2a strictly behaviour-preserving so it can be verified by
comparing before/after; exercise both single and split mode against at least two
platforms; and check the OSD ↔ deck.gl bridge explicitly, since the guard has
never covered it.

**`LayerPanel.jsx` is 1,750 lines** and will be touched throughout. Worth
splitting the section components into their own files as part of 2a — but as a
separate commit from the state migration, so the diff of each stays readable.

**Persistence is still absent.** None of this survives a reload, which matters
more once a user can build up two differently-configured panels. Out of scope
here, but it becomes a more visible gap after Phase 2, not less.

## Out of scope, but adjacent

**Annotations were global — fixed before this plan started.** `regions` and
`measurements` carried no panel index, so every annotation drew in both panels at
identical image-pixel coordinates, CSV export always resolved against panel 0's
dataset, and measurement labels used the *rendering* panel's `pixelSize`. Both
were silent wrong answers rather than visible breakage. Each annotation now
carries `panelIndex`, and `store.annotations.test.js` covers the scoping.

**Per-panel edge file already exists** (`panels[i].edgeFile`, Phase 1). CLAUDE.md
described it as global in three places, contradicting the code; corrected when this
plan was written rather than deferred, since a stale architecture note is worse than
a missing one.
