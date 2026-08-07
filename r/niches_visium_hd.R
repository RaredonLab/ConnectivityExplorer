#!/usr/bin/env Rscript
## niches_visium_hd.R ----------------------------------------------------------
##
## Run NICHESv2 on a 10x Visium HD dataset and write edges.parquet for TissuePlex.
##
##     Rscript r/niches_visium_hd.R [dataset_dir] [--bin square_016um] [--rad 40]
##     Rscript r/niches_visium_hd.R sample_data/visium_hd_tiny
##
## This is the script to read if you care about coordinate units, because Visium
## HD is the platform where getting them wrong is silent.
##
##   tissue_positions.parquet stores pxl_col_in_fullres / pxl_row_in_fullres, in
##   full-resolution image PIXELS. export_to_TissuePlex() copies whatever you put
##   in meta.data$x/$y straight into x1..y2, and the TissuePlex backend then
##   divides those by the dataset's pixel_size assuming they are microns. Feed it
##   raw pixels and every edge lands offset from its bin by exactly that factor.
##
##   So we multiply by microns_per_pixel from scalefactors_json.json here, before
##   NICHESv2 ever sees the coordinates. `rad` is then in microns too, and
##   comparable to the value you would use on Xenium.
##
## The bin you pick must match the bin TissuePlex displays
## --------------------------------------------------------
## Barcodes are bin-size specific — `s_008um_00172_00043-1` and
## `s_016um_00066_00065-1` name different things. visium_hd_reader.py serves
## `square_008um` by default (Space Ranger's own analysis default, and
## spatialdata-io's DEFAULT_BIN), so edges generated at another bin size render
## from their own coordinates but join to nothing: clicking a bin finds no edge,
## the cell filter drops every edge, and the tissue graph is disconnected from
## the bins under it. The default here matches the reader for that reason.
##
## Bins are not cells. An 8 um bin may contain a cell, several, or none, so treat
## "communication" between bins as a neighbourhood statistic rather than a
## cell-cell claim. Avoid 2 um bins: mostly empty, and there are millions.
##
## Inputs
##   binned_outputs/<bin>/filtered_feature_bc_matrix.h5
##   binned_outputs/<bin>/spatial/tissue_positions.parquet
##   binned_outputs/<bin>/spatial/scalefactors_json.json
##
## Requires: NICHESv2 (dev branch), arrow, rhdf5, Matrix, jsonlite.

suppressPackageStartupMessages({
  library(Matrix); library(arrow); library(jsonlite); library(NICHESv2)
})

.this <- tryCatch({
  a <- commandArgs(trailingOnly = FALSE)
  normalizePath(sub("^--file=", "", a[grep("^--file=", a)][1]))
}, error = function(e) NA_character_)
source(file.path(if (is.na(.this)) "r" else dirname(.this), "niches_common.R"))

# ── Parameters ────────────────────────────────────────────────────────────────
args <- commandArgs(trailingOnly = TRUE)
opt  <- function(flag, default) {
  i <- match(flag, args); if (is.na(i) || i == length(args)) default else args[i + 1L]
}
positional <- args[!grepl("^--", args) &
                   !(seq_along(args) %in% (which(grepl("^--", args)) + 1L))]

DATASET <- if (length(positional)) positional[1] else "sample_data/visium_hd_tiny"
BIN     <- opt("--bin", "square_008um")   # MUST match the bin TissuePlex shows
OUT     <- opt("--out", file.path(DATASET, "edges.parquet"))
SPECIES <- opt("--species", "mouse")      # the 10x tiny demo is mouse brain
# rad in MICRONS. For 8 um bins, 24 um reaches the immediate neighbours plus a
# little; scale it with the bin size if you switch bins (40 um suited 16 um bins).
RAD     <- as.numeric(opt("--rad", "24"))
CORES   <- as.integer(opt("--cores", "4"))
MINCOUNT <- as.integer(opt("--min-counts", "1"))  # drop empty bins

cat("── NICHESv2 · Visium HD ──────────────────────────────────────\n")
cat("  dataset :", DATASET, "\n  bin     :", BIN, "\n  output  :", OUT, "\n")
cat("  species :", SPECIES, " rad:", RAD, "um  cores:", CORES, "\n\n")

bin.dir <- file.path(DATASET, "binned_outputs", BIN)
if (!dir.exists(bin.dir)) {
  # Tolerate a hand-assembled folder without the binned_outputs/ wrapper.
  alt <- file.path(DATASET, BIN)
  if (dir.exists(alt)) bin.dir <- alt else
    stop("no ", BIN, " under ", DATASET, "\n  available: ",
         paste(basename(Sys.glob(file.path(DATASET, "binned_outputs", "square_*um"))),
               collapse = ", "))
}

# ── 1. Counts ─────────────────────────────────────────────────────────────────
cat("reading counts…\n")
count.mtx <- read_10x_h5(file.path(bin.dir, "filtered_feature_bc_matrix.h5"))
cat(sprintf("  %d genes x %d bins\n", nrow(count.mtx), ncol(count.mtx)))

# ── 2. Positions, converted pixels -> microns ────────────────────────────────
cat("reading bin positions…\n")
pos <- as.data.frame(arrow::read_parquet(
  file.path(bin.dir, "spatial", "tissue_positions.parquet")))
pos <- pos[pos$in_tissue == 1, , drop = FALSE]   # most bins are off-tissue
cat(sprintf("  %d in-tissue bins\n", nrow(pos)))

sf.path <- file.path(bin.dir, "spatial", "scalefactors_json.json")
sf      <- jsonlite::fromJSON(sf.path)
mpp     <- as.numeric(sf$microns_per_pixel)
if (!is.finite(mpp) || mpp <= 0)
  stop("microns_per_pixel missing or invalid in ", sf.path)
cat(sprintf("  microns_per_pixel: %.6f  (bin size %.0f um)\n", mpp, sf$bin_size_um))

# THE conversion. Without it x1..y2 are pixels and the edge layer is misplaced.
meta.data <- data.frame(
  x = as.numeric(pos$pxl_col_in_fullres) * mpp,
  y = as.numeric(pos$pxl_row_in_fullres) * mpp,
  row.names = as.character(pos$barcode),
  stringsAsFactors = FALSE
)
cat(sprintf("  converted pixels -> microns (x %.6f)\n", mpp))
report_extent(meta.data, "um")

aligned   <- align_counts_meta(count.mtx, meta.data, "bins")
count.mtx <- aligned$count.mtx
meta.data <- aligned$meta.data

# Empty bins contribute nothing but inflate the neighbour graph enormously.
if (MINCOUNT > 0) {
  keep <- Matrix::colSums(count.mtx) >= MINCOUNT
  cat(sprintf("  dropping %d bins with < %d counts (%d remain)\n",
              sum(!keep), MINCOUNT, sum(keep)))
  count.mtx <- count.mtx[, keep, drop = FALSE]
  meta.data <- meta.data[keep, , drop = FALSE]
}

# ── 3. Build and export ───────────────────────────────────────────────────────
check_lr_coverage(count.mtx, SPECIES)

cat("\nrunning NICHESv2 (spatial mode)…\n")
obj <- create_NICHESObject(
  count.mtx        = count.mtx,
  meta.data        = meta.data,
  LRM.db           = "connectomedb2025",
  species          = SPECIES,
  mode             = "spatial",
  rad              = RAD,          # microns, matching meta.data$x/$y
  method           = "product",
  normalize.method = "prop",
  n.cores          = CORES,
  verbose          = TRUE
)

cat("\nexporting…\n")
export_to_TissuePlex(obj, output.path = OUT,
                     x.col = "x", y.col = "y",
                     celltype.col = NULL, n.threads = CORES)

validate_edges_parquet(OUT)
cat("\ndone — reload TissuePlex to see the edge layer.\n")
