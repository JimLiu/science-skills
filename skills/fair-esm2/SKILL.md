---
name: fair-esm2
description: >
  Embed proteins with Meta AI's ESM-2 (`fair-esm` package). Use this skill
  when: (1) Extracting per-residue or per-sequence embeddings for downstream
  ML, (2) Masked-LM likelihood / mutation effect scoring, (3) Contact
  prediction from a sequence.
license: Apache-2.0
category: biomodels
requirements: [gpu]
metadata:
  display-name: ESM-2
  # github.com/facebookresearch/esm/blob/main/LICENSE: MIT (© Meta Platforms,
  # Inc. and affiliates). verified 2026-06-30
  third_party:
    - kind: weights
      name: ESM-2
      provider: Meta AI
      license: MIT
      terms_url: https://github.com/facebookresearch/esm/blob/main/LICENSE
---

# fair-esm2 — ESM-2 (Meta AI)

ESM-2 code and weights are MIT (Meta AI, github.com/facebookresearch/esm).

> **Package disambiguation.** `pip install fair-esm` gives you `import esm`
> with `esm.pretrained.*` (ESM-1/2). Biohub's github.com/Biohub/esm fork
> (MIT) gives you `from esm.models.esmfold2 import ESMFold2InputBuilder` —
> see the **`esmfold2`** skill. Both share the `esm` namespace but are
> different libraries. This skill covers **fair-esm** (the Meta package).

## Prerequisites

| Requirement | Minimum | Recommended |
| ----------- | ------- | ----------- |
| Python      | 3.8+    | 3.11        |
| CUDA        | 11.7+   | 12.x        |
| GPU VRAM    | 8 GB (8M), 16 GB (650M) | 24 GB+ (650M / 3B) |

## How to run

### Embeddings

```python
import hashlib, os, torch, esm

# fair-esm downloads its checkpoints through torch.hub with no checksum and
# unpickles them in full. So fetch the 650M checkpoint and its contact-regression
# file first, each checked against its sha256, into the folder esm.pretrained
# reads, so the load below downloads nothing. A cached copy that fails the check
# is renamed aside (*.rejected, never loaded) or, on a read-only weight mount,
# stops the cell; a download that fails it is never moved into place. Only this
# pair is pinned (digests as in remote-compute-modal/env-setup.md), not 8M or 3B.
ESM2_650M_SHA256 = {
    "models/esm2_t33_650M_UR50D.pt": "ea9d0522b335a8778dea6535a65301f10208dece28cd5865482b0b1fc446168c",
    "regression/esm2_t33_650M_UR50D-contact-regression.pt": "8ffe6edbd4173dc8d45c2cd5cb27d43aad77ec26b4c768200c58ae1f96693575",
}
def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
ckpts = os.path.join(torch.hub.get_dir(), "checkpoints")
os.makedirs(ckpts, exist_ok=True)
for rel, digest in ESM2_650M_SHA256.items():
    dst = os.path.join(ckpts, os.path.basename(rel))
    if os.path.isfile(dst) and sha256_of(dst) != digest:
        aside, n = dst + ".rejected", 1
        while os.path.exists(aside):
            aside, n = f"{dst}.rejected.{n}", n + 1
        os.rename(dst, aside)
    if not os.path.isfile(dst):
        print("fetching", rel, "(checked against its sha256) ...", flush=True)
        torch.hub.download_url_to_file("https://dl.fbaipublicfiles.com/fair-esm/" + rel, dst, hash_prefix=digest, progress=False)

model, alphabet = esm.pretrained.esm2_t33_650M_UR50D()
model = model.eval().cuda()
bc = alphabet.get_batch_converter()

_, _, toks = bc([("ubq", "MQIFVKTLTGKTITLEVEPSDTIENVK")])
with torch.no_grad():
    out = model(toks.cuda(), repr_layers=[33])
emb = out["representations"][33]      # (1, L+2, 1280) — includes BOS/EOS
seq_emb = emb[0, 1:-1].mean(0)        # per-sequence mean
```

### Masked-LM scoring

```python
with torch.no_grad():
    out = model(toks.cuda(), repr_layers=[33])
logits = out["logits"][0, 1:-1]       # (L, |vocab|)
# WT marginal log-likelihood; for mutation scoring, mask the position and
# compare logit[mut] − logit[wt].
```

### Contact prediction

```python
with torch.no_grad():
    out = model(toks.cuda(), repr_layers=[33], return_contacts=True)
contacts = out["contacts"][0]         # (L, L)
```

## Models

| Name                       | Layers | Dim  | Params | Use                        |
| -------------------------- | ------ | ---- | ------ | -------------------------- |
| `esm2_t6_8M_UR50D`         | 6      | 320  | 8 M    | Fast smoke / tiny embeddings |
| `esm2_t33_650M_UR50D`      | 33     | 1280 | 650 M  | Default embedding model    |
| `esm2_t36_3B_UR50D`        | 36     | 2560 | 3 B    | Best embeddings, 24 GB+    |

Only the 650M checkpoint and its contact-regression file are fetched against a pinned sha256 (the Embeddings block above); the 8M and 3B checkpoints are not pinned here and come through `torch.hub` unverified — to load one of them, drop the pre-fetch block as well, or the cell still downloads the 2.5 GB 650M pair first.

## Output format

`out["representations"][layer]` is `(B, L+2, D)`; slice `[ :, 1:-1, : ]` to
drop BOS/EOS. `out["contacts"]` (when `return_contacts=True`) is `(B, L, L)`.


## Remote compute

Needs ≥16 GB VRAM (650M model) and either pre-cached `.pt` checkpoints or
egress to `dl.fbaipublicfiles.com`. Read
`compute_details({provider, mode:'read'})` for an environment with `fair-esm`
and a torch-hub weight cache, then:

```python
c = host.compute.create(provider)
job = c.submit_job(
    intent="ESM-2 650M embeddings for 200 sequences — 1×GPU, ~2 min",
    inputs=[
        "seqs.fasta",
        "embed_esm2.py",
    ],
    # env activation is host-specific — put it IN the command string
    # (e.g. "conda run -n <env> python3 embed_esm2.py"; see compute_details)
    command="python3 embed_esm2.py",
    outputs=["embeddings.pt"],
    run_timeout_s=1800,
)
# the Job repr just printed itself (id, notes) — cell ends here; the kernel never waits on the job
```

Then call the `wait_for_notification` brain-tool. When the
`compute_done` notification arrives, act on its payload:

```python
save_artifacts(payload["output_files"])   # paths under hpc/<job_id>/
```

For the full record (`files`, `stdout_tail`, `notes`, …), re-enter the
kernel: `res = c.attach_job(job_id).result()` then `c.close(intent=...)`. See the
`remote-compute-ssh` / `remote-compute-modal` skill for the orchestration
details.

Inside `embed_esm2.py`, set `TORCH_HOME` to the provider's torch-hub cache
mount (path is in `compute_details`) so `esm.pretrained.*` resolves locally.


## Troubleshooting

| Symptom                                       | Cause                              | Fix                                   |
| --------------------------------------------- | ---------------------------------- | ------------------------------------- |
| `ModuleNotFoundError: No module named 'esm.models'` | You want Biohub's `esm` fork, not `fair-esm` | See `esmfold2` skill; this skill uses `esm.pretrained.*` |
| Slow first call                               | Downloading weights via torch.hub  | Set `TORCH_HOME` to a cached location |
| `RuntimeError: invalid hash value` from `download_url_to_file` | The 650M download did not match its pinned sha256 (truncated, or the file changed upstream); nothing was moved into place | Re-run the cell once; if it repeats, stop and report it rather than fetching the file another way |
| A `*.rejected` file in torch.hub's checkpoints folder (`$TORCH_HOME/hub/checkpoints`, default `~/.cache/torch/hub/checkpoints`) | A cached checkpoint failed the check and was renamed aside, not deleted; it is never loaded | Delete it when convenient: if the cell finished, it fetched a verified copy under the original name; if it stopped with `invalid hash value`, see the row above |
| `OSError: [Errno 30] Read-only file system` from the pre-fetch block, `TORCH_HOME` on a provider's weight mount | The mount's 650M checkpoint or contact-regression file failed its sha256 check, or is not where `TORCH_HOME` points, and a read-only mount cannot be changed; the cell stopped before any load | If `TORCH_HOME` is the path `compute_details` gives, stop and report it: the mount needs rebuilding with a verified pair. Meanwhile a writable `TORCH_HOME` gets the job through (the cell downloads a verified copy) if the host may reach `dl.fbaipublicfiles.com` |

---

**Next**: feed embeddings to a classifier. For structure prediction, use
`esmfold2`.
