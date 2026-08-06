## niches_common.R ------------------------------------------------------------
##
## Helpers shared by the per-platform NICHESv2 scripts in this folder.
## Source it, don't run it:
##
##     source(file.path(dirname(this_file), "niches_common.R"))
##
## Everything platform-specific lives in the individual scripts. What is here is
## only the parts that are genuinely identical across platforms: reading a 10x
## HDF5 feature matrix, and validating the exported parquet.

suppressPackageStartupMessages({
  library(Matrix)
})

## Read a 10x-format feature matrix (.h5) into a sparse gene x cell dgCMatrix.
##
## Used by Xenium (cell_feature_matrix.h5) and Visium HD
## (filtered_feature_bc_matrix.h5) — the layout is the same for both.
## Requires the Bioconductor package rhdf5.
read_10x_h5 <- function(path) {
  if (!requireNamespace("rhdf5", quietly = TRUE))
    stop("rhdf5 is required to read ", basename(path), "\n",
         '  BiocManager::install("rhdf5")')
  if (!file.exists(path)) stop("not found: ", path)

  genes    <- as.character(rhdf5::h5read(path, "matrix/features/name"))
  barcodes <- as.character(rhdf5::h5read(path, "matrix/barcodes"))
  shape    <- as.integer(rhdf5::h5read(path, "matrix/shape"))

  m <- Matrix::sparseMatrix(
    i        = as.integer(rhdf5::h5read(path, "matrix/indices")),  # 0-based
    p        = as.integer(rhdf5::h5read(path, "matrix/indptr")),
    x        = as.numeric(rhdf5::h5read(path, "matrix/data")),
    dims     = shape,                                   # c(n_genes, n_cells)
    dimnames = list(genes, barcodes),
    index1   = FALSE
  )
  rhdf5::h5closeAll()

  # 10x panels can repeat a gene symbol; NICHESv2 matches the LR database on
  # rownames, so collapse duplicates rather than letting one silently win.
  if (anyDuplicated(rownames(m))) {
    dup <- sum(duplicated(rownames(m)))
    message(sprintf("  collapsing %d duplicated gene symbols by sum", dup))
    m <- Matrix::t(
      Matrix::fac2sparse(factor(rownames(m), levels = unique(rownames(m)))) %*% m
    )
    m <- Matrix::t(m)
  }
  m
}

## Align a count matrix and a metadata frame onto their shared barcodes.
##
## create_NICHESObject() requires meta.data rownames to be the barcodes, in the
## same order as the matrix columns. Mismatches here are the most common cause of
## a confusing failure deep inside the pipeline, so fail loudly and early.
align_counts_meta <- function(count.mtx, meta.data, label = "cells") {
  shared <- intersect(colnames(count.mtx), rownames(meta.data))
  if (length(shared) == 0L)
    stop("no shared barcodes between the count matrix and the metadata.\n",
         "  matrix e.g.: ", paste(head(colnames(count.mtx), 3), collapse = ", "), "\n",
         "  meta   e.g.: ", paste(head(rownames(meta.data), 3), collapse = ", "))
  dropped.m <- ncol(count.mtx)   - length(shared)
  dropped.d <- nrow(meta.data)   - length(shared)
  if (dropped.m > 0 || dropped.d > 0)
    message(sprintf("  aligned on %d %s (dropped %d from matrix, %d from metadata)",
                    length(shared), label, dropped.m, dropped.d))
  list(count.mtx = count.mtx[, shared, drop = FALSE],
       meta.data = meta.data[shared, , drop = FALSE])
}

## Report what actually landed in edges.parquet.
##
## Note the na.rm = TRUE throughout. export_to_TissuePlex() writes every edge in
## $edge.list, and edges with no scored LR signal get a single placeholder row
## with NA lrm/score/score_norm so TissuePlex can still draw the tissue graph.
## Validation that omits na.rm reports failure whenever placeholders exist, which
## is almost always — the demo script bundled with NICHESv2 has that bug.
validate_edges_parquet <- function(path) {
  if (!requireNamespace("arrow", quietly = TRUE)) {
    message("  (install 'arrow' to validate the output)")
    return(invisible(NULL))
  }
  pq <- arrow::read_parquet(path)
  scored <- !is.na(pq$lrm)

  cat("\n── ", basename(path), " ──\n", sep = "")
  cat(sprintf("  rows            : %d\n", nrow(pq)))
  cat(sprintf("  columns         : %d  (%s)\n", ncol(pq),
              paste(names(pq), collapse = ", ")))
  cat(sprintf("  unique edges    : %d\n", length(unique(pq$edge))))
  cat(sprintf("  autocrine edges : %d\n",
              length(unique(pq$edge[pq$is_autocrine]))))
  cat(sprintf("  scored rows     : %d  across %d LRMs\n",
              sum(scored), length(unique(pq$lrm[scored]))))
  cat(sprintf("  placeholder rows: %d  (edges with no LR signal — expected)\n",
              sum(!scored)))

  if (any(scored)) {
    cat(sprintf("  score range     : %.4g .. %.4g\n",
                min(pq$score, na.rm = TRUE), max(pq$score, na.rm = TRUE)))
    sums <- tapply(pq$score_norm[scored], pq$edge[scored], sum, na.rm = TRUE)
    ok   <- all(abs(sums - 1) < 1e-6)
    cat(sprintf("  score_norm sums to 1 per scored edge: %s\n", ok))
    top <- sort(table(pq$lrm[scored]), decreasing = TRUE)
    cat("  top LRMs        : ",
        paste(sprintf("%s (%d)", names(head(top, 5)), head(top, 5)),
              collapse = ", "), "\n", sep = "")
  } else {
    cat("  NOTE: no scored rows. Every edge is a placeholder, so TissuePlex will\n")
    cat("        draw the tissue graph but the Edges layer will be empty. This is\n")
    cat("        normal for a small targeted panel where no ligand-receptor pair\n")
    cat("        has BOTH partners on the panel.\n")
  }
  invisible(pq)
}

## Check how many ligand-receptor pairs are actually scorable on this panel.
##
## NICHESv2 needs BOTH partners of a pair present in the count matrix. On a small
## targeted panel that is often true of nothing at all, and the failure surfaces
## as "No valid LR pairs remain after gene filtering" thrown from deep inside
## compute_CellToCell(). Checking up front turns that into an explanation.
##
## Returns the number of complete pairs; stops with guidance when it is zero.
check_lr_coverage <- function(count.mtx, species, db = "connectomedb2025") {
  lrm <- NICHESv2::load_LRM_database(db = db, species = species, verbose = FALSE)
  present <- rownames(count.mtx)[Matrix::rowSums(count.mtx) > 0]

  # Complexes list several genes; every component must be present.
  has.all <- function(field) {
    vapply(strsplit(field, "[,&+]"), function(parts)
      all(trimws(parts) %in% present), logical(1))
  }
  complete <- has.all(lrm$ligand) & has.all(lrm$receptor)
  n <- sum(complete)

  cat(sprintf("  LR database     : %s (%s), %d pairs\n", db, species, nrow(lrm)))
  cat(sprintf("  scorable pairs  : %d  (both partners expressed on this panel)\n", n))
  if (n > 0) {
    ex <- head(paste0(lrm$ligand[complete], "->", lrm$receptor[complete]), 5)
    cat("  e.g.            : ", paste(ex, collapse = ", "), "\n", sep = "")
  } else {
    stop("no ligand-receptor pair has both partners expressed in this dataset.\n",
         "  NICHESv2 cannot score anything and will abort.\n",
         "  Panel size: ", length(present), " expressed genes of ",
         nrow(count.mtx), " measured.\n",
         "  Options:\n",
         "    - check --species (currently '", species, "') matches the tissue\n",
         "    - use a dataset with a larger panel, or whole-transcriptome data\n",
         "    - confirm the count matrix is not empty (a tiny demo subset may be)\n",
         call. = FALSE)
  }
  invisible(n)
}

## Coordinates handed to create_NICHESObject() must be in microns, because
## export_to_TissuePlex() copies them verbatim into x1..y2 and the TissuePlex
## backend divides those by the dataset's pixel_size. Platforms that report
## pixels (Visium HD, CosMx) must be converted first — see docs/data_format.md.
report_extent <- function(meta.data, unit = "um") {
  cat(sprintf("  extent          : x %.1f..%.1f, y %.1f..%.1f (%s)\n",
              min(meta.data$x), max(meta.data$x),
              min(meta.data$y), max(meta.data$y), unit))
}
