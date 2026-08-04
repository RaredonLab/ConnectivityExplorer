# OBS — Obsolete planning documents

These documents describe earlier designs that have since been superseded by the shipped
implementation. They are kept for provenance and to explain why certain decisions were
made, **but they are not a specification and should not be used to guide new work.**

For current architecture, read `CLAUDE.md` at the repo root. For current data contracts
and deployment, read `docs/`.

| File | Written | Why it is obsolete |
|---|---|---|
| `PLAN.md` | 2026-05-01 | The original v1 plan, under the project's former name *ConnectivityExplorer*. Its edge schema (integer `lrm_id` 1–488, `strength`, `cell_id_source`/`cell_id_target`, Xenium pixel coordinates) was fully replaced by the NICHESv2 schema documented in `docs/data_format.md`. Still useful for the rationale behind storing connectivity as vector edges rather than 488 rasterized PNGs. |
| `PLAN_v2_2026-05-02.md` | 2026-05-02 | Status snapshot declaring v1 feature-complete. Its API reference lists the `/xenium/...` routes, which were replaced by the platform-agnostic `/spatial/...` router. Its state schema predates the `lrm_set` color mode and string-keyed `hiddenLrms`. Its P3 performance backlog is still partly relevant and has been carried into `CLAUDE.md`. |
| `EDGE_UI_PLAN.md` | 2026-05-02 | Design doc for the NICHESv2 edge-UI migration. Overtaken during implementation: it specifies **client-side** edge aggregation and a **server** round-trip for `lrm_set` coloring, and the shipped code does the opposite of both (server-side `/query-grouped`, client-side `lrm_set`). It also predates the tissue-graph layer, `edgeDensity`, `arrowStyle`, and `edgeColorClamp`. |

`NICHESv2_package_design.md` remains at the repo root: it documents the separate NICHESv2
R package rather than this codebase, and is still current for that package.
