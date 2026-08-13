#!/usr/bin/env Rscript
## niches_merscope.R -----------------------------------------------------------
##
## Run NICHESv2 on a Vizgen MERSCOPE dataset and write edges.parquet for
## TissuePlex.
##
##     Rscript r/niches_merscope.R [dataset_dir] [--rad 30] [--species human]
##     Rscript r/niches_merscope.R sample_data/merscope-vpt-smallset
##
## MERSCOPE is the easy case for coordinates — `cell_metadata.csv` stores
## center_x / center_y in MICRONS already, which is what export_to_TissuePlex()
## wants, so nothing is converted. Read niches_xenium.R first; this is the same
## shape with a different pair of input files.
##
## The two things that do bite
## ---------------------------
## 1. **EntityID is a 19-digit integer.** R silently loses precision on those if
##    they are read as numeric, which corrupts every barcode and makes the join
##    against the count matrix fail — or worse, succeed partially. Both files are
##    read with the id column forced to character.
##
## 2. **Targeted panels often cannot be scored.** MERSCOPE panels run 100-500
##    genes chosen for cell typing, not for signalling, so a complete
##    ligand-receptor pair (both partners on the panel) is the exception. The
##    bundled VPT test set has 130 genes and exactly 2 scorable pairs. That is a
##    property of the panel, not a failure — check_lr_coverage() reports it before
##    NICHESv2 runs so you find out in one second rather than ten minutes.
##
## Inputs
##   cell_by_gene.csv     first column = EntityID, remaining columns = genes
##   cell_metadata.csv    EntityID, fov, volume, center_x, center_y, ...
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

# ── Parameters ────────────────────────────────────────────────────────────────
args <- commandArgs(trailingOnly = TRUE)
opt  <- function(flag, default) {
  i <- match(flag, args); if (is.na(i) || i == length(args)) default else args[i + 1L]
}
positional <- args[!grepl("^--", args) &
                   !(seq_along(args) %in% (which(grepl("^--", args)) + 1L))]

DATASET  <- if (length(positional)) positional[1] else "sample_data/merscope-vpt-smallset"
OUT      <- opt("--out", file.path(DATASET, "edges.parquet"))
# The bundled VPT test set is human. Vizgen ships mouse panels too — the gene
# symbols tell you which: uppercase is human, Title case is mouse.
SPECIES  <- opt("--species", "human")
# rad in MICRONS, matching center_x/center_y. MERSCOPE cells run ~10 um, so 30 um
# reaches the immediate neighbours.
RAD      <- as.numeric(opt("--rad", "30"))
CORES    <- as.integer(opt("--cores", "4"))
MINCOUNT <- as.integer(opt("--min-counts", "1"))

cat("── NICHESv2 · MERSCOPE (Vizgen) ──────────────────────────────\n")
cat("  dataset :", DATASET, "\n  output  :", OUT, "\n")
cat("  species :", SPECIES, " rad:", RAD, "um  cores:", CORES, "\n\n")

# ── 1. Counts: cells x genes -> genes x cells ────────────────────────────────
cat("reading counts…\n")
cbg.path <- file.path(DATASET, "cell_by_gene.csv")
if (!file.exists(cbg.path)) stop("no cell_by_gene.csv in ", DATASET)
# colClasses on the first column only: everything else is numeric counts.
cbg <- data.table::fread(cbg.path, colClasses = list(character = 1))
id.col <- names(cbg)[1]
cells.id <- as.character(cbg[[id.col]])
count.mtx <- Matrix::Matrix(t(as.matrix(cbg[, -1, with = FALSE])), sparse = TRUE)
colnames(count.mtx) <- cells.id
cat(sprintf("  %d genes x %d cells\n", nrow(count.mtx), ncol(count.mtx)))

# Vizgen panels carry Blank-* control probes; they are not genes.
ctrl <- grepl("^(Blank|blank)", rownames(count.mtx))
if (any(ctrl)) {
  cat(sprintf("  dropping %d blank control probes\n", sum(ctrl)))
  count.mtx <- count.mtx[!ctrl, , drop = FALSE]
}

# ── 2. Metadata: already microns, nothing to convert ─────────────────────────
cat("reading cell metadata…\n")
meta.path <- file.path(DATASET, "cell_metadata.csv")
if (!file.exists(meta.path)) stop("no cell_metadata.csv in ", DATASET)
md <- data.table::fread(meta.path, colClasses = list(character = 1))
names(md)[1] <- "cell_id"
for (need in c("center_x", "center_y"))
  if (!need %in% names(md)) stop("missing column '", need, "' in cell_metadata.csv")

meta.data <- data.frame(
  x = as.numeric(md$center_x),      # microns already — no conversion
  y = as.numeric(md$center_y),
  row.names = as.character(md$cell_id),
  stringsAsFactors = FALSE
)
cat("  coordinates already in microns — no conversion\n")
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
# Fails early and legibly when the panel carries no complete LR pair, which is
# the usual outcome for a small targeted panel.
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
