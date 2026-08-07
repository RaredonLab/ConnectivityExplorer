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
## the image pixel size for classic Visium, so the conversion must be derived —
## and the obvious-looking derivation is wrong by 18%.
##
##   DON'T:  55 / spot_diameter_fullres
##   DO:     100 / <in-row lattice pitch, measured from tissue_positions>
##
## `spot_diameter_fullres` is Space Ranger's DETECTED spot footprint, not the
## 55 um capture spot. Measured on V1_Mouse_Kidney and V1_Adult_Mouse_Brain it is
## 0.6482 x the lattice pitch, i.e. ~64.8 um — which is why 10x's own docs
## describe classic Visium spot diameters as "approximately 60-70 um" and warn
## that they are estimates. The 100 um centre-to-centre pitch is a hard geometric
## constant and averages over thousands of positions, so that is what we use.
##
## Pass --mpp to override with a calibrated microscope value.
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
SPOT_DIAMETER_UM <- 55    # capture spot
SPOT_PITCH_UM    <- 100   # centre-to-centre; the constant we actually derive from
DETECTED_SPOT_UM <- 64.8  # what spot_diameter_fullres measures — see above

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
pos.all <- pos                       # pitch is measured over the whole array
pos <- pos[pos$in_tissue == 1, , drop = FALSE]
cat(sprintf("  %d in-tissue spots (of %d on the slide)\n", nrow(pos), nrow(pos.all)))

# ── 3. The conversion: fullres pixels -> microns ─────────────────────────────
sf.path <- file.path(DATASET, "spatial", "scalefactors_json.json")
sf      <- jsonlite::fromJSON(sf.path)
if (is.finite(MPP_ARG) && MPP_ARG > 0) {
  mpp <- MPP_ARG
  cat(sprintf("  microns_per_pixel: %.6f  (supplied via --mpp)\n", mpp))
} else {
  # Measure the in-row lattice pitch: within a row, array_col steps by 2 and the
  # spacing between those spots IS the hex nearest-neighbour distance. Median over
  # every row, so missing spots cannot move it.
  steps <- unlist(lapply(split(pos.all, pos.all$array_row), function(g) {
    g  <- g[order(g$array_col), ]
    dx <- diff(g$pxl_col_in_fullres); dc <- diff(g$array_col)
    dx[dc == 2]
  }), use.names = FALSE)
  steps <- steps[is.finite(steps) & steps > 0]
  if (length(steps) >= 20) {
    pitch.px <- stats::median(steps)
    mpp <- SPOT_PITCH_UM / pitch.px
    cat(sprintf("  microns_per_pixel: %.6f  (from %.0f um pitch / %.2f px)\n",
                mpp, SPOT_PITCH_UM, pitch.px))
    spot.px <- as.numeric(sf$spot_diameter_fullres)
    if (is.finite(spot.px))
      cat(sprintf("    (spot_diameter_fullres %.1f px = %.1f um detected, not the %.0f um\n"
                  , spot.px, spot.px * mpp, SPOT_DIAMETER_UM),
          "     capture spot — do not derive from it)\n", sep = "")
  } else {
    spot.px <- as.numeric(sf$spot_diameter_fullres)
    if (!is.finite(spot.px) || spot.px <= 0)
      stop("cannot measure the lattice pitch and spot_diameter_fullres is invalid in ",
           sf.path, " — pass --mpp <um per fullres pixel> instead")
    mpp <- DETECTED_SPOT_UM / spot.px
    cat(sprintf("  microns_per_pixel: %.6f  (FALLBACK: %.1f um detected / %.1f px)\n",
                mpp, DETECTED_SPOT_UM, spot.px))
    cat("    warning: too few positions to measure the lattice pitch.\n")
  }
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
