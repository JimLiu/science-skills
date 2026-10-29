---
name: scvi-tools
description: >
  Probabilistic single-cell RNA-seq with scvi-tools — scVI for a
  batch-corrected latent space, scANVI for semi-supervised label transfer,
  and Bayesian differential expression. Reach for this skill to integrate
  scRNA-seq batches, embed cells for clustering, transfer annotations from a
  reference onto a query, or score differentially expressed genes per cluster.
  For spatial deconvolution / mapping use the cell2location, DestVI, or
  Tangram methods instead.
license: Apache-2.0
requirements: [gpu]
metadata:
  display-name: scvi-tools
---

# scvi-tools — scVI / scANVI

scvi-tools (Gayoso et al. 2022, github.com/scverse/scvi-tools, BSD-3-Clause)
wraps a family
of deep generative models for single-cell omics. The scRNA-seq core is **scVI**
(unsupervised batch-corrected latent embedding) and **scANVI** (scVI + a
classifier head for semi-supervised cell-type label transfer). Both expect
**raw integer UMI counts** and emit a low-dimensional `X_scVI` / `X_scANVI`
that drops into the scanpy neighbors → leiden → umap pipeline.

## How to run

### scVI — batch-corrected latent space

```python
import scanpy as sc
import scvi

adata = sc.read_h5ad("dataset.h5ad")
adata.layers["counts"] = adata.X.copy()          # preserve raw BEFORE any normalize/log1p
sc.pp.normalize_total(adata); sc.pp.log1p(adata) # optional, for HVG / plotting only
sc.pp.highly_variable_genes(adata, n_top_genes=2000, batch_key="batch", subset=True)

scvi.model.SCVI.setup_anndata(adata, layer="counts", batch_key="batch")
model = scvi.model.SCVI(adata, n_latent=30)
model.train(max_epochs=200, early_stopping=True, accelerator="gpu", devices=1)

adata.obsm["X_scVI"] = model.get_latent_representation()
adata.layers["scvi_normalized"] = model.get_normalized_expression(library_size=1e4)
```

### scANVI — label transfer from a partially-annotated reference

```python
lvae = scvi.model.SCANVI.from_scvi_model(
    model, labels_key="cell_type", unlabeled_category="Unknown",
)
lvae.train(max_epochs=20, n_samples_per_label=100, accelerator="gpu", devices=1)

adata.obsm["X_scANVI"] = lvae.get_latent_representation()
adata.obs["pred_cell_type"] = lvae.predict()
```

`accelerator="gpu", devices=1` is the PyTorch-Lightning spelling; the legacy
`use_gpu=` kwarg was **removed** in scvi-tools 1.x and now raises `TypeError`.

Hard labels hide uncertainty: `lvae.predict(soft=True)` returns per-cell
class probabilities (a DataFrame, one column per cell type). Keep
`.max(axis=1)` alongside the labels and treat cells below a threshold
(e.g. 0.8) as low-confidence instead of accepting every call.

### Query-to-reference mapping (scArches)

To annotate a new dataset against an **already-trained** reference atlas,
don't retrain from scratch — map the query onto the frozen reference
(scArches-style transfer learning; see the reference-mapping guide at
docs.scvi-tools.org):

```python
adata_query.layers["counts"] = adata_query.X.copy()  # raw counts, like the reference
adata_query.obs["cell_type"] = "Unknown"             # query starts unlabeled
scvi.model.SCANVI.prepare_query_anndata(adata_query, lvae)  # align genes; missing → zero-padded
query = scvi.model.SCANVI.load_query_data(adata_query, lvae)
query.train(max_epochs=100, plan_kwargs={"weight_decay": 0.0})

adata_query.obsm["X_scANVI"] = query.get_latent_representation()
adata_query.obs["pred_cell_type"] = query.predict()
```

The query also needs the reference's `batch` column in `.obs`.
`weight_decay: 0.0` keeps fine-tuning from eroding the frozen reference
latent space. The same classmethods exist on `scvi.model.SCVI` for an
unlabeled reference (embedding only, no label transfer).

For later scArches mapping, the reference should be built with
`encode_covariates=True` set on the `scvi.model.SCVI(...)` constructor
(default `False`; scANVI inherits it via `from_scvi_model`) — the encoder
is only conditioned on batch/covariates when this is set, and an
un-conditioned encoder ignores the query's batch, so surgery can't adapt
the latent space to it. `load_query_data` still runs without it, but
mapping quality suffers; the reference-mapping tutorial at
docs.scvi-tools.org shows the full recommended `arches_params`.

## Differential expression

```python
de = model.differential_expression(
    groupby="leiden", group1="3",   # group2=None → vs. all other cells
    mode="change", delta=0.25,
)
top = de.sort_values("proba_de", ascending=False).head(50)
```

For one-vs-rest leave `group2` out — `"rest"` is scanpy's
`rank_genes_groups` convention, not scvi-tools'; here `group2` is a literal
category name and `"rest"` would match zero cells.

scvi-tools ≥1.4 defaults to `mode="vanilla"`, whose result columns are
exactly:

```
['proba_m1', 'proba_m2', 'bayes_factor', 'scale1', 'scale2', 'raw_mean1',
 'raw_mean2', 'non_zeros_proportion1', 'non_zeros_proportion2',
 'raw_normalized_mean1', 'raw_normalized_mean2', 'comparison', 'group1',
 'group2']
```

— no `lfc_*`, no `proba_de`, no `is_de_fdr_*`. **Pass `mode="change"`** to
get `lfc_mean` / `lfc_median` / `proba_de` / `is_de_fdr_0.05`. Sort on
`proba_de` (or on `bayes_factor` if you deliberately stayed in vanilla
mode).

Two further knobs (DE user guide, docs.scvi-tools.org): `fdr_target` sets
the posterior-expected-FDR level the `is_de_fdr_*` call column is computed
at (default 0.05), and `delta=None` swaps the fixed log-fold-change
threshold for one estimated from the data.

## Output format

| Key                            | What                                                   |
| ------------------------------ | ------------------------------------------------------ |
| `adata.obsm["X_scVI"]`         | `n_cells × n_latent` batch-corrected embedding         |
| `adata.obsm["X_scANVI"]`       | label-aware embedding (better separates known classes) |
| `adata.obs["pred_cell_type"]`  | scANVI predicted label per cell                        |
| `adata.layers["scvi_normalized"]` | decoded expression, library-size normalized         |
| DE dataframe                   | per-gene `lfc_*` / `proba_de` (with `mode="change"`)   |

## Remote compute

A100-class GPU recommended for >50k cells. The prebuilt **`singlecell_gpu`**
Modal env ships scvi-tools 1.4.2 + scanpy 1.11.5 + anndata 0.11.4 — read
`compute_details({provider: 'byoc:modal', mode: 'read'})` for the current
image ref, then:

```python
c = host.compute.create('modal', provider_params={
    'image':  '<image ref from compute_details>',   # e.g. im-...
    'gpu':    'A100',
    'cpu':    8,
    'memory': 32768,
    'timeout': 3600,          # container wall clock — optional; omitted → Settings default
})
job = c.submit_job(
    intent="scVI+scANVI on 80k cells — 1×A100, ~15 min",
    inputs=[
        "dataset.h5ad",
        "pipeline.py",
    ],
    command="python pipeline.py",
    outputs=["out/**"],
    run_timeout_s=2400,
)
# the Job repr just printed itself (id, notes, deadline) — cell ends here; the kernel never waits on the job
```

`h5ad_safe_obs` is auto-loaded into the **local** analysis kernel only — in
`pipeline.py` running on Modal, paste the helper at the top of the script
(or inline the `pd.Index(np.asarray(..., dtype=object))` coercion) before
`.write_h5ad()`.

Then call the `wait_for_notification` brain-tool. When `compute_done`
arrives, `save_artifacts(payload["output_files"])`. For the full record,
re-enter the kernel and bind the **compute handle** (not the job)
separately — `.close()` lives on the handle, not on the job:

```python
c = host.compute.create('modal')
res = c.attach_job(job_id).result()   # a read, never a wait: files, stdout_tail, notes, ...
c.close(intent="scVI run done — stop the sandbox")
```

See the `remote-compute-modal` skill for orchestration details.

## Gotchas

| Gotcha | What happens / fix |
|---|---|
| `differential_expression()` defaults to `mode="vanilla"` (scvi-tools ≥1.4) | `KeyError: 'lfc_mean'` / `'proba_de'` when sorting — pass `mode="change"` to get `lfc_*`/`proba_de`/`is_de_fdr_*`; in vanilla mode sort on `bayes_factor`. |
| `adata.obs` index/columns are `string[pyarrow]` (`ArrowStringArray`) | `.write_h5ad()` dies with `IORegistryError: No method registered for writing <class 'pandas.arrays.ArrowStringArray'>` (anndata #2377). Coerce before writing: `adata.obs = h5ad_safe_obs(adata.obs)` (kernel helper — local kernel only; inline the coercion in remote `pipeline.py`). **`.astype(str)` alone is not enough** — on a pyarrow-backed Index/Series it returns another Arrow-backed array; round-trip through `np.asarray(..., dtype=object)`. `anndata.settings.allow_write_nullable_strings = True` does **not** cover Arrow-backed strings. |
| `use_gpu=` kwarg | Removed in 1.x → `TypeError: train() got an unexpected keyword argument 'use_gpu'`. Use `accelerator="gpu", devices=1`. |
| Log-normalized data fed to `setup_anndata` | Silent garbage — scVI's NB likelihood needs raw integer counts. Stash counts in `adata.layers["counts"]` *before* normalize/log1p and pass `layer="counts"`. |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `KeyError: 'lfc_mean'` (or `'proba_de'`, `'is_de_fdr_0.05'`) on DE result | Add `mode="change"` to `differential_expression()`; the default vanilla mode has no LFC columns. |
| `IORegistryError: No method registered for writing <class 'pandas.arrays.ArrowStringArray'>` on `.write_h5ad()` | `adata.obs = h5ad_safe_obs(adata.obs)` (and `adata.var` if needed) before writing. The `allow_write_nullable_strings` flag does not help here. |
| `TypeError: ... unexpected keyword argument 'use_gpu'` | Replace with `accelerator="gpu", devices=1`. |
| `ValueError: ... non-negative integers` / NB loss explodes | `layer="counts"` points at log/float data — restore raw counts. |
| `MisconfigurationException: No supported gpu backend found` | No CUDA visible — drop `accelerator`/`devices` to fall back to CPU, or dispatch via Remote compute. |
| `UnicodeEncodeError: 'ascii' codec can't encode character ...` writing a summary / printing | Container has no `LANG` so Python defaults to ASCII. Open files with `encoding="utf-8"` and/or `sys.stdout.reconfigure(encoding="utf-8")` at script top. The prebuilt `singlecell_gpu` env sets `PYTHONIOENCODING=utf-8`, so this only bites user-built images. |
| `AttributeError: ... object has no attribute 'close'` on a job handle | You chained `host.compute.create(...).attach_job(...)` and called `.close()` on the job. Bind the compute handle separately and close that — see Remote compute above. |
| Batches stay separated on the `X_scVI` UMAP (under-correction) | Integrate harder: raise `n_top_genes` (e.g. 4000) or train longer; double-check `batch_key` names the real technical batch. |
| Known-distinct cell types merge after integration (over-correction) | Use scANVI with whatever labels exist — the label-aware embedding keeps known classes separated. (Moving a variable from `batch_key` to `categorical_covariate_keys` does not help: both are conditioned on identically.) |

---

**Next**: cluster on `X_scVI` with scanpy (`sc.pp.neighbors(use_rep="X_scVI")`
→ `sc.tl.leiden` → `sc.tl.umap`); for spatial deconvolution train
cell2location / DestVI / Tangram on the scRNA-seq reference.
