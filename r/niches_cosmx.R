#!/usr/bin/env Rscript
## niches_cosmx.R --------------------------------------------------------------
##
## Run NICHESv2 on a Nanostring/Bruker CosMx dataset and write edges.parquet for
## TissuePlex.
##
##     Rscript r/niches_cosmx.R [dataset_dir] [--rad 30] [--species mouse]
##     Rscript r/niches_cosmx.R sample_data/cosmx-mousebrain
##
## Read niches_xenium.R first for the shape of the pipeline. CosMx adds two
## wrinkles that will silently misplace every edge if you skip them.
##
## 1. Cell identity is the (fov, cell_ID) PAIR
## -------------------------------------------
## `cell_ID` restarts at 1 in every field of view, so keying on it alone merges
## unrelated cells across the slide. The TissuePlex reader builds its cell_id as
## `<fov>_<cell_ID>` and the barcodes in edges.parquet must match exactly, or the
## edge layer will not join to a single cell. Note the column is spelled `cell_ID`
## in the metadata and expression files and `cellID` in the polygons file.
##
## 2. Coordinates are PIXELS in the slide frame, and the reader shifts them
## -----------------------------------------------------------------------
## CosMx exports carry no morphology image, so there is no external frame to
## define image-pixel space. The reader therefore shifts the whole dataset to its
## own corner:
##
##     origin = (min(CenterX_global_px) - 50, min(CenterY_global_px) - 50)
##     cell image position = CenterX_global_px - origin_x
##
## and TissuePlex divides the microns in edges.parquet by `pixel_size` to recover
## that. So the conversion here has to be shift THEN scale:
##
##     x_um = (CenterX_global_px - origin_x) * microns_per_pixel
##
## Skip the shift and the edge layer is offset by tens of thousands of pixels;
## skip the scale and it is off by a factor of ~8. The -50 margin is not cosmetic
## — it is part of the reader's origin and must be reproduced.
##
## `microns_per_pixel` is 0.12028 for CosMx (Bruker's documented value, matching
## the reader default). Override with --mpp if your instrument differs.
##
## Inputs (flat CosMx export, files usually prefixed with the experiment name)
##   *_exprMat_file.csv     fov, cell_ID, then one column per gene
##   *_metadata_file.csv    fov, cell_ID, CenterX_global_px, CenterY_global_px, ...
##
## Requires: NICHESv2 (dev branch), arrow, Matrix, data.table.

suppressPackageStartupMessages({
  library(Matrix); library(arrow); library(data.table); library(NICHESv2)
})

.this <- tryCatch({
  a <- commandArgs(trailingOnly = FALSE)
  normalizePath(sub("^--file=", "", a[grep("^--file=", a)][1]))
}, error = function(e) NA_character_)
source(file.path(if (is.na(.this)) "r" else dirname(.this), "niches_common.R"))

# Bruker's documented CosMx pixel size, and the TissuePlex reader default.
DEFAULT_MPP <- 0.12028
# Must match cosmx_reader.py::_origin(), which leaves this margin so units on the
# edge are not flush against the placeholder canvas border.
ORIGIN_MARGIN_PX <- 50

# ── Parameters ────────────────────────────────────────────────────────────────
args <- commandArgs(trailingOnly = TRUE)
opt  <- function(flag, default) {
  i <- match(flag, args); if (is.na(i) || i == length(args)) default else args[i + 1L]
}
positional <- args[!grepl("^--", args) &
                   !(seq_along(args) %in% (which(grepl("^--", args)) + 1L))]

DATASET  <- if (length(positional)) positional[1] else "sample_data/cosmx-mousebrain"
OUT      <- opt("--out", file.path(DATASET, "edges.parquet"))
SPECIES  <- opt("--species", "mouse")
# rad in MICRONS. CosMx cells run ~10 um, so 30 um reaches the first ring of
# neighbours. Raising it on a 40k-cell slide multiplies the row count fast.
RAD      <- as.numeric(opt("--rad", "30"))
CORES    <- as.integer(opt("--cores", "4"))
MINCOUNT <- as.integer(opt("--min-counts", "1"))
MPP      <- as.numeric(opt("--mpp", DEFAULT_MPP))
# Restrict to a subset of FOVs. A whole CosMx slide can produce tens of millions
# of edge x LRM rows; this keeps a demo or a pilot to a workable size.
FOVS     <- opt("--fovs", NA_character_)

cat("── NICHESv2 · CosMx (Nanostring/Bruker) ──────────────────────\n")
cat("  dataset :", DATASET, "\n  output  :", OUT, "\n")
cat("  species :", SPECIES, " rad:", RAD, "um  cores:", CORES, "\n")
cat("  um/px   :", MPP, "\n\n")

find1 <- function(patterns) {
  for (p in patterns) {
    hit <- Sys.glob(file.path(DATASET, p))
    if (length(hit)) return(hit[1])
  }
  NA_character_
}

# ── 1. Counts: cells x genes -> genes x cells ────────────────────────────────
cat("reading counts…\n")
expr.path <- find1(c("*_exprMat_file.csv", "exprMat_file.csv"))
if (is.na(expr.path)) stop("no *_exprMat_file.csv in ", DATASET)
expr <- data.table::fread(expr.path)
for (need in c("fov", "cell_ID"))
  if (!need %in% names(expr)) stop("missing column '", need, "' in ", basename(expr.path))

# THE key. Must match cosmx_reader.py, which builds "<fov>_<cell_ID>".
expr.ids <- paste0(expr$fov, "_", expr$cell_ID)
gene.cols <- setdiff(names(expr), c("fov", "cell_ID"))
count.mtx <- Matrix::Matrix(t(as.matrix(expr[, ..gene.cols])), sparse = TRUE)
colnames(count.mtx) <- expr.ids
rm(expr); invisible(gc())
cat(sprintf("  %d genes x %d cells\n", nrow(count.mtx), ncol(count.mtx)))

# CosMx panels carry Negative/SystemControl probes; they are not genes.
ctrl <- grepl("^(Negative|NegPrb|SystemControl|Custom)", rownames(count.mtx))
if (any(ctrl)) {
  cat(sprintf("  dropping %d control probes\n", sum(ctrl)))
  count.mtx <- count.mtx[!ctrl, , drop = FALSE]
}

# ── 2. Positions: shift to the reader's origin, then pixels -> microns ───────
cat("reading cell metadata…\n")
meta.path <- find1(c("*_metadata_file.csv", "metadata_file.csv"))
if (is.na(meta.path)) stop("no *_metadata_file.csv in ", DATASET)
md <- data.table::fread(meta.path)
for (need in c("fov", "cell_ID", "CenterX_global_px", "CenterY_global_px"))
  if (!need %in% names(md)) stop("missing column '", need, "' in ", basename(meta.path))

if (!is.na(FOVS)) {
  keep.fov <- as.integer(strsplit(FOVS, ",")[[1]])
  md <- md[md$fov %in% keep.fov, ]
  cat(sprintf("  restricted to FOV %s -> %d cells\n", FOVS, nrow(md)))
}

# The origin is computed over the WHOLE metadata file, not the FOV subset, so a
# subset stays registered with what the reader draws.
md.all <- data.table::fread(meta.path, select = c("CenterX_global_px", "CenterY_global_px"))
x0 <- min(md.all$CenterX_global_px, na.rm = TRUE) - ORIGIN_MARGIN_PX
y0 <- min(md.all$CenterY_global_px, na.rm = TRUE) - ORIGIN_MARGIN_PX
rm(md.all); invisible(gc())
cat(sprintf("  origin (min - %d px): x0=%.1f  y0=%.1f\n", ORIGIN_MARGIN_PX, x0, y0))

meta.data <- data.frame(
  x = (as.numeric(md$CenterX_global_px) - x0) * MPP,   # shift THEN scale
  y = (as.numeric(md$CenterY_global_px) - y0) * MPP,
  row.names = paste0(md$fov, "_", md$cell_ID),
  stringsAsFactors = FALSE
)
cat(sprintf("  shifted to the reader origin, then px -> um (x %.5f)\n", MPP))
report_extent(meta.data, "um")

aligned   <- align_counts_meta(count.mtx, meta.data)
count.mtx <- aligned$count.mtx
meta.data <- aligned$meta.data

if (MINCOUNT > 0) {
  keep <- Matrix::colSums(count.mtx) >= MINCOUNT
  cat(sprintf("  dropping %d cells with < %d counts (%d remain)\n",
              sum(!keep), MINCOUNT, sum(keep)))
  count.mtx <- count.mtx[, keep, drop = FALSE]
  meta.data <- meta.data[keep, , drop = FALSE]
}

# ── 3. Build and export ───────────────────────────────────────────────────────
check_lr_coverage(count.mtx, SPECIES)

cat(sprintf("\nrunning NICHESv2 (spatial mode) on %d cells…\n", ncol(count.mtx)))
if (ncol(count.mtx) > 20000)
  cat("  note: a slide this size can produce tens of millions of edge x LRM rows.\n",
      "        Use --fovs or a smaller --rad if the output is unwieldy.\n", sep = "")

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
