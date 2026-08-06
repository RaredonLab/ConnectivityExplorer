#!/usr/bin/env Rscript
## niches_xenium.R -------------------------------------------------------------
##
## Run NICHESv2 on a 10x Xenium dataset and write edges.parquet for TissuePlex.
##
##     Rscript r/niches_xenium.R [dataset_dir] [--rad 30] [--out edges.parquet]
##     Rscript r/niches_xenium.R sample_data/mouse_ileum_tiny
##
## Xenium is the straightforward case: coordinates are already in microns, which
## is exactly what TissuePlex expects, so nothing needs converting. Start here if
## you are reading these scripts to learn the pipeline, then compare against
## niches_visium_hd.R, where the coordinates are in pixels and must be converted.
##
## Inputs (standard Xenium output folder)
##   cells.parquet             cell_id, x_centroid, y_centroid   (microns)
##   cell_feature_matrix.h5    10x HDF5, genes x cells
##
## Output
##   edges.parquet in the dataset folder — picked up automatically by TissuePlex.
##
## Requires: NICHESv2 (dev branch), arrow, rhdf5, Matrix.
##   install.packages(c("arrow","Matrix")); BiocManager::install("rhdf5")
##   remotes::install_github("RaredonLab/NICHESv2", ref = "dev")

suppressPackageStartupMessages({
  library(Matrix); library(arrow); library(NICHESv2)
})

## Locate niches_common.R relative to this script, so it works from any cwd.
.this <- tryCatch({
  a <- commandArgs(trailingOnly = FALSE)
  normalizePath(sub("^--file=", "", a[grep("^--file=", a)][1]))
}, error = function(e) NA_character_)
source(file.path(if (is.na(.this)) "r" else dirname(.this), "niches_common.R"))

# ── Parameters ────────────────────────────────────────────────────────────────
args    <- commandArgs(trailingOnly = TRUE)
opt     <- function(flag, default) {
  i <- match(flag, args); if (is.na(i) || i == length(args)) default else args[i + 1L]
}
positional <- args[!grepl("^--", args) &
                   !(seq_along(args) %in% (which(grepl("^--", args)) + 1L))]

DATASET  <- if (length(positional)) positional[1] else "sample_data/mouse_ileum_tiny"
OUT      <- opt("--out", file.path(DATASET, "edges.parquet"))
SPECIES  <- opt("--species", "mouse")     # ileum demo is mouse
# rad is in the same units as x/y, i.e. MICRONS for Xenium. 30 um reaches roughly
# the first ring of neighbours for typical Xenium cell density; tune per tissue
# with NICHESv2::Explore_Radius_Neighborhood().
RAD      <- as.numeric(opt("--rad", "30"))
CORES    <- as.integer(opt("--cores", "4"))
CELLTYPE <- opt("--celltype", NA_character_)  # optional column in cells.parquet

cat("── NICHESv2 · Xenium ─────────────────────────────────────────\n")
cat("  dataset :", DATASET, "\n  output  :", OUT, "\n")
cat("  species :", SPECIES, " rad:", RAD, "um  cores:", CORES, "\n\n")

# ── 1. Counts ─────────────────────────────────────────────────────────────────
cat("reading counts…\n")
count.mtx <- read_10x_h5(file.path(DATASET, "cell_feature_matrix.h5"))
cat(sprintf("  %d genes x %d cells\n", nrow(count.mtx), ncol(count.mtx)))

# Drop Xenium control probes — they are not real genes and can only add noise.
ctrl <- grepl("^(NegControl|BLANK|Blank|Unassigned|Deprecated|antisense|Intergenic)",
              rownames(count.mtx))
if (any(ctrl)) {
  cat(sprintf("  dropping %d control/blank probes\n", sum(ctrl)))
  count.mtx <- count.mtx[!ctrl, , drop = FALSE]
}

# ── 2. Metadata: rownames = barcodes, coordinates literally named x / y ───────
cat("reading cell metadata…\n")
cells <- as.data.frame(arrow::read_parquet(file.path(DATASET, "cells.parquet")))
meta.data <- data.frame(
  x = as.numeric(cells$x_centroid),   # already microns — no conversion
  y = as.numeric(cells$y_centroid),
  row.names = as.character(cells$cell_id),
  stringsAsFactors = FALSE
)
if (!is.na(CELLTYPE)) {
  if (!CELLTYPE %in% names(cells))
    stop("--celltype '", CELLTYPE, "' is not a column in cells.parquet")
  meta.data[[CELLTYPE]] <- as.character(cells[[CELLTYPE]])
}
report_extent(meta.data, "um")

aligned   <- align_counts_meta(count.mtx, meta.data)
count.mtx <- aligned$count.mtx
meta.data <- aligned$meta.data

# ── 3. Build the NICHESObject ─────────────────────────────────────────────────
# Fail early and legibly if nothing on this panel can score.
check_lr_coverage(count.mtx, SPECIES)

cat("\nrunning NICHESv2 (spatial mode)…\n")
obj <- create_NICHESObject(
  count.mtx        = count.mtx,
  meta.data        = meta.data,
  LRM.db           = "connectomedb2025",
  species          = SPECIES,
  mode             = "spatial",
  rad              = RAD,          # microns
  cell.type.col    = if (is.na(CELLTYPE)) NULL else CELLTYPE,
  method           = "product",
  normalize.method = "prop",
  n.cores          = CORES,
  verbose          = TRUE
)

# ── 4. Export ─────────────────────────────────────────────────────────────────
cat("\nexporting…\n")
export_to_TissuePlex(
  obj,
  output.path  = OUT,
  x.col        = "x",
  y.col        = "y",
  celltype.col = if (is.na(CELLTYPE)) NULL else CELLTYPE,
  n.threads    = CORES
)

validate_edges_parquet(OUT)
cat("\ndone — reload TissuePlex to see the edge layer.\n")
