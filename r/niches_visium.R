#!/usr/bin/env Rscript
## niches_visium.R -------------------------------------------------------------
##
## Run NICHESv2 on a classic 10x Visium dataset and write edges.parquet for
## TissuePlex.
##
##     Rscript r/niches_visium.R [dataset_dir] [--rad 150] [--out edges.parquet]
##     Rscript r/niches_visium.R sample_data/visium_tiny
##
## Read niches_visium_hd.R first — the coordinate problem is the same and is
## explained there at length. The short version: tissue_positions stores
## FULL-RESOLUTION PIXELS, export_to_TissuePlex() copies meta.data$x/$y straight
## into x1..y2, and the backend divides those by pixel_size assuming microns.
## Feed it raw pixels and every edge lands offset from its spot.
##
## The one thing classic Visium does differently, and it matters
## --------------------------------------------------------------
## There is no `microns_per_pixel` in scalefactors_json.json. 10x does not record
## the image pixel size for classic Visium. So the conversion has to come from the
## one physical constant the slide guarantees:
##
##     a Visium spot is 55 um across
##     => microns_per_pixel = 55 / spot_diameter_fullres
##
## 10x notes that spot diameters are estimates and recommends a calibrated
## microscope value where you have one. Pass --mpp to override.
##
## Spots are not cells. A 55 um spot holds roughly 1-10 cells, so an edge between
## two spots is a neighbourhood statistic, not a cell-cell claim. Say so in
## figure legends. The 100 um centre-to-centre pitch also sets the sensible `rad`
## floor: below ~110 um no spot has any neighbour at all.
##
## Inputs (Space Ranger `outs/`)
##   filtered_feature_bc_matrix.h5
##   spatial/tissue_positions.csv        (or tissue_positions_list.csv pre-SR-2.0)
##   spatial/scalefactors_json.json
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

# The slide spec: "Each spot is 55 um in diameter with a 100 um center to center
# distance between spots."
SPOT_DIAMETER_UM <- 55
SPOT_PITCH_UM    <- 100

# ── Parameters ────────────────────────────────────────────────────────────────
args <- commandArgs(trailingOnly = TRUE)
opt  <- function(flag, default) {
  i <- match(flag, args); if (is.na(i) || i == length(args)) default else args[i + 1L]
}
positional <- args[!grepl("^--", args) &
                   !(seq_along(args) %in% (which(grepl("^--", args)) + 1L))]

DATASET  <- if (length(positional)) positional[1] else "sample_data/visium_tiny"
OUT      <- opt("--out", file.path(DATASET, "edges.parquet"))
SPECIES  <- opt("--species", "mouse")
# rad in MICRONS. The lattice pitch is 100 um, so 150 um reaches the six
# immediate neighbours and nothing beyond; 250 um reaches the second ring.
RAD      <- as.numeric(opt("--rad", "150"))
CORES    <- as.integer(opt("--cores", "4"))
MINCOUNT <- as.integer(opt("--min-counts", "1"))
# Override the derived pixel size if you have a calibrated value (um per
# full-resolution pixel).
MPP_ARG  <- suppressWarnings(as.numeric(opt("--mpp", NA)))

cat("── NICHESv2 · Visium (classic) ───────────────────────────────\n")
cat("  dataset :", DATASET, "\n  output  :", OUT, "\n")
cat("  species :", SPECIES, " rad:", RAD, "um  cores:", CORES, "\n\n")

if (RAD < SPOT_PITCH_UM * 1.1)
  warning("rad ", RAD, " um is below the ", SPOT_PITCH_UM,
          " um spot pitch — most spots will have no neighbours.")

# ── 1. Counts ─────────────────────────────────────────────────────────────────
cat("reading counts…\n")
h5 <- file.path(DATASET, "filtered_feature_bc_matrix.h5")
if (!file.exists(h5)) stop("no filtered_feature_bc_matrix.h5 in ", DATASET,
                           " — point at the contents of Space Ranger's outs/")
count.mtx <- read_10x_h5(h5)
cat(sprintf("  %d genes x %d spots\n", nrow(count.mtx), ncol(count.mtx)))

# ── 2. Positions ──────────────────────────────────────────────────────────────
cat("reading spot positions…\n")
pos.cols <- c("barcode", "in_tissue", "array_row", "array_col",
              "pxl_row_in_fullres", "pxl_col_in_fullres")
pos.modern <- file.path(DATASET, "spatial", "tissue_positions.csv")
pos.legacy <- file.path(DATASET, "spatial", "tissue_positions_list.csv")
if (file.exists(pos.modern)) {
  pos <- read.csv(pos.modern)
} else if (file.exists(pos.legacy)) {
  # Space Ranger < 2.0 wrote this file with NO header row. Reading it with
  # header = TRUE silently eats the first spot and mislabels every column.
  pos <- read.csv(pos.legacy, header = FALSE, col.names = pos.cols)
} else {
  stop("no tissue_positions.csv or tissue_positions_list.csv in ",
       file.path(DATASET, "spatial"))
}
pos <- pos[pos$in_tissue == 1, , drop = FALSE]
cat(sprintf("  %d in-tissue spots\n", nrow(pos)))

# ── 3. The conversion: fullres pixels -> microns ─────────────────────────────
sf.path <- file.path(DATASET, "spatial", "scalefactors_json.json")
sf      <- jsonlite::fromJSON(sf.path)
if (is.finite(MPP_ARG) && MPP_ARG > 0) {
  mpp <- MPP_ARG
  cat(sprintf("  microns_per_pixel: %.6f  (supplied via --mpp)\n", mpp))
} else {
  spot.px <- as.numeric(sf$spot_diameter_fullres)
  if (!is.finite(spot.px) || spot.px <= 0)
    stop("spot_diameter_fullres missing or invalid in ", sf.path,
         " — pass --mpp <um per fullres pixel> instead")
  mpp <- SPOT_DIAMETER_UM / spot.px
  cat(sprintf("  microns_per_pixel: %.6f  (DERIVED from %.1f um / %.1f px)\n",
              mpp, SPOT_DIAMETER_UM, spot.px))
  cat("    note: 10x calls spot diameters estimates; pass --mpp if you have a\n")
  cat("    calibrated microscope value.\n")
}

meta.data <- data.frame(
  x = as.numeric(pos$pxl_col_in_fullres) * mpp,
  y = as.numeric(pos$pxl_row_in_fullres) * mpp,
  row.names = as.character(pos$barcode),
  stringsAsFactors = FALSE
)
report_extent(meta.data, "um")

# Sanity check the derivation against the known lattice pitch: nearest-neighbour
# distance on a Visium slide must come out near 100 um. If it does not, either
# spot_diameter_fullres is wrong or the positions are not in fullres pixels, and
# every downstream distance is off by the same factor.
if (nrow(meta.data) > 1) {
  s   <- meta.data[sample(nrow(meta.data), min(200, nrow(meta.data))), ]
  d   <- as.matrix(dist(s[, c("x", "y")]))
  diag(d) <- Inf
  nn  <- median(apply(d, 1, min))
  cat(sprintf("  median nearest-neighbour distance: %.1f um (expect ~%d)\n",
              nn, SPOT_PITCH_UM))
  if (nn < SPOT_PITCH_UM * 0.5 || nn > SPOT_PITCH_UM * 2)
    warning("nearest-neighbour spacing is far from the ", SPOT_PITCH_UM,
            " um Visium pitch — check spot_diameter_fullres or pass --mpp.")
}

aligned   <- align_counts_meta(count.mtx, meta.data, "spots")
count.mtx <- aligned$count.mtx
meta.data <- aligned$meta.data

if (MINCOUNT > 0) {
  keep <- Matrix::colSums(count.mtx) >= MINCOUNT
  cat(sprintf("  dropping %d spots with < %d counts (%d remain)\n",
              sum(!keep), MINCOUNT, sum(keep)))
  count.mtx <- count.mtx[, keep, drop = FALSE]
  meta.data <- meta.data[keep, , drop = FALSE]
}

# ── 4. Build and export ───────────────────────────────────────────────────────
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
