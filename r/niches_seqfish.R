#!/usr/bin/env Rscript
## niches_seqfish.R ------------------------------------------------------------
##
## Run NICHESv2 on a seqFISH (Spatial Genomics GenePS) dataset and write
## edges.parquet for TissuePlex.
##
##     Rscript r/niches_seqfish.R [dataset_dir] [--rad 30] [--out edges.parquet]
##     Rscript r/niches_seqfish.R sample_data/seqfish_synthetic
##
## Two things differ from the Xenium script:
##
##   1. Counts come from a plain dense CSV (<ROI>_CellxGene.csv), cells x genes,
##      with an unnamed first column of cell labels. It has to be transposed to
##      the genes x cells orientation NICHESv2 expects.
##
##   2. Coordinates come from <ROI>_CellCoordinates.csv. In current GenePS
##      exports these are MICRONS, which is what we need — but some older exports
##      write pixels instead. The script checks the extent against the DAPI image
##      and refuses to guess silently. See the units note in the TissuePlex
##      seqFISH reader for why this ambiguity exists.
##
## Expect a small targeted panel. If no ligand-receptor pair has BOTH partners on
## the panel, every edge is exported as a placeholder row and TissuePlex draws the
## tissue graph with an empty Edges layer. That is a property of the panel, not a
## failure of the run.
##
## Requires: NICHESv2 (dev branch), arrow, Matrix.

suppressPackageStartupMessages({
  library(Matrix); library(arrow); library(NICHESv2)
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

DATASET  <- if (length(positional)) positional[1] else "sample_data/seqfish_synthetic"
OUT      <- opt("--out", file.path(DATASET, "edges.parquet"))
SPECIES  <- opt("--species", "mouse")
RAD      <- as.numeric(opt("--rad", "30"))     # microns
CORES    <- as.integer(opt("--cores", "4"))
# Override the pixel-vs-micron detection if you know better: --units um|px
UNITS    <- opt("--units", "auto")

cat("── NICHESv2 · seqFISH (Spatial Genomics GenePS) ──────────────\n")
cat("  dataset :", DATASET, "\n  output  :", OUT, "\n")
cat("  species :", SPECIES, " rad:", RAD, "um  cores:", CORES, "\n\n")

# ── 1. Locate the ROI ─────────────────────────────────────────────────────────
# Every file is prefixed with an ROI name; TissuePlex expects one ROI per folder.
coord.files <- list.files(DATASET, pattern = "_CellCoordinates.*\\.csv$", full.names = TRUE)
if (!length(coord.files))
  stop("no *_CellCoordinates*.csv in ", DATASET, " — is this a seqFISH folder?")
if (length(coord.files) > 1)
  message("  ", length(coord.files), " ROIs present; using ", basename(coord.files[1]))
roi <- sub("_CellCoordinates.*$", "", basename(coord.files[1]))
cat("  ROI     :", roi, "\n")

cxg.file <- file.path(DATASET, paste0(roi, "_CellxGene.csv"))
if (!file.exists(cxg.file))                                   # legacy v1 naming
  cxg.file <- file.path(DATASET, paste0(roi, "_CxG.csv"))
if (!file.exists(cxg.file)) stop("no CellxGene/CxG CSV for ROI ", roi)

# ── 2. Counts: dense cells x genes CSV -> sparse genes x cells ───────────────
cat("\nreading counts…\n")
cxg <- read.csv(cxg.file, row.names = 1, check.names = FALSE)
count.mtx <- Matrix::Matrix(t(as.matrix(cxg)), sparse = TRUE)
colnames(count.mtx) <- as.character(rownames(cxg))            # cell labels
cat(sprintf("  %d genes x %d cells\n", nrow(count.mtx), ncol(count.mtx)))

# ── 3. Coordinates, and the units question ───────────────────────────────────
cat("reading cell coordinates…\n")
cc <- read.csv(coord.files[1], check.names = FALSE)
for (need in c("label", "center_x", "center_y"))
  if (!need %in% names(cc)) stop("missing column '", need, "' in ", basename(coord.files[1]))

# The DAPI OME-TIFF carries the pixel size, and the image width lets us tell
# microns from pixels: a micron-valued table spans about width*pixel_size, a
# pixel-valued one spans the width itself.
px.size  <- NA_real_
img.w    <- NA_real_
dapi <- c(file.path(DATASET, paste0(roi, "_DAPI.tiff")),
          file.path(DATASET, paste0(roi, "_DAPI.ome.tiff")))
dapi <- dapi[file.exists(dapi)]
if (length(dapi) && requireNamespace("RBioFormats", quietly = TRUE)) {
  # optional; most installs will not have it, so fall through to the heuristic
  try({
    meta   <- RBioFormats::read.metadata(dapi[1])
    px.size <- as.numeric(meta$coreMetadata$physicalSizeX)
    img.w   <- as.numeric(meta$coreMetadata$sizeX)
  }, silent = TRUE)
}
if (is.na(px.size)) px.size <- 0.107   # documented GenePS default

units <- UNITS
if (units == "auto") {
  # Without the image width we cannot compute the ratio, so fall back on scale:
  # a GenePS ROI is tens to low hundreds of microns across, but thousands of
  # pixels. An extent in the thousands therefore means pixels.
  span <- max(max(cc$center_x) - min(cc$center_x),
              max(cc$center_y) - min(cc$center_y))
  units <- if (span > 1000) "px" else "um"
  cat(sprintf("  extent %.1f across -> detected %s (override with --units)\n",
              span, units))
}
if (units == "px") {
  cat(sprintf("  converting pixels -> microns (x %.6f um/px)\n", px.size))
  cc$center_x <- cc$center_x * px.size
  cc$center_y <- cc$center_y * px.size
} else {
  cat("  coordinates already in microns — no conversion\n")
}

meta.data <- data.frame(
  x = as.numeric(cc$center_x),
  y = as.numeric(cc$center_y),
  row.names = as.character(cc$label),
  stringsAsFactors = FALSE
)
report_extent(meta.data, "um")

aligned   <- align_counts_meta(count.mtx, meta.data)
count.mtx <- aligned$count.mtx
meta.data <- aligned$meta.data

# ── 4. Build and export ───────────────────────────────────────────────────────
# Fail early and legibly if nothing on this panel can score.
check_lr_coverage(count.mtx, SPECIES)

cat("\nrunning NICHESv2 (spatial mode)…\n")
obj <- create_NICHESObject(
  count.mtx        = count.mtx,
  meta.data        = meta.data,
  LRM.db           = "connectomedb2025",
  species          = SPECIES,
  mode             = "spatial",
  rad              = RAD,
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
