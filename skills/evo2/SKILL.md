---
name: evo2
description: >
  Score, embed, and generate DNA sequences with Evo 2, a long-context genomic
  foundation model. Use this skill when:
  (1) Computing per-nucleotide or per-sequence likelihoods for variant effect
      scoring,
  (2) Embedding genomic windows for downstream classification,
  (3) Generating DNA conditioned on a prefix,
  (4) Scoring regulatory or coding regions across species.
license: Apache-2.0
category: biomodels
requirements: [gpu]
metadata:
  display-name: Evo 2
  # github.com/ArcInstitute/evo2/blob/main/LICENSE: Apache-2.0 boilerplate.
  # HuggingFace model cards `arcinstitute/evo2_{40b_base,20b}` declare
  # `license: apache-2.0`. verified 2026-06-30
  third_party:
    - kind: weights
      name: Evo 2
      provider: Arc Institute
      license: Apache-2.0
      terms_url: https://github.com/ArcInstitute/evo2/blob/main/LICENSE
---

# Evo 2 — DNA Language Model

## Prerequisites

| Requirement | Minimum | Recommended      |
| ----------- | ------- | ---------------- |
| Python      | 3.11    | 3.12 (<3.13)     |
| CUDA        | 12.1+   | 12.4+            |
| GPU VRAM    | 24 GB (7B bf16) | 80 GB (40B) |
| RAM         | 32 GB   | 128 GB           |

## How to run

### Installation

```bash
pip install evo2==0.5.5   # pinned for supply-chain hygiene — bump via PR (matches envs/genomics_evo2_gpu.py)
# Pin the weights too: fetch the 7B checkpoint at a fixed Hugging Face revision (the
# one envs/genomics_evo2_gpu.py hydrates) now, so the load below names exactly that
# file and never resolves 'main'. (evo2_40b and evo2_1b_base are not pinned here.)
python -c 'from huggingface_hub import hf_hub_download; print(hf_hub_download("arcinstitute/evo2_7b", "evo2_7b.pt", revision="bda0089f92582d5baabf0f22d9fc85f3588f6b58"))'   # pinned 2026-06-28
```

### Loading and scoring

```python
import pathlib, re
from huggingface_hub import hf_hub_download
from huggingface_hub.constants import HF_HUB_CACHE
from huggingface_hub.utils import LocalEntryNotFoundError
from evo2 import Evo2

# the pinned 7B checkpoint fetched at install: with a commit-hash revision already in the
# cache hf_hub_download returns its path at once, no network; local_files_only makes a
# missing file fail here instead of starting a ~14 GB download in the kernel (keep
# HF_HOME the same for the install step and the kernel). Other models: Evo2("evo2_40b") etc.
REV = "bda0089f92582d5baabf0f22d9fc85f3588f6b58"
try:
    ckpt = hf_hub_download("arcinstitute/evo2_7b", "evo2_7b.pt", revision=REV, local_files_only=True)
except LocalEntryNotFoundError:
    # a cache filled through a Hugging Face mirror names the snapshot folder by the mirror's
    # own id and records that id in refs/<REV>: load that snapshot, still offline (never refs/main)
    ref = pathlib.Path(HF_HUB_CACHE, "models--arcinstitute--evo2_7b", "refs", REV)
    mid = ref.read_text().strip() if ref.is_file() else ""
    if not re.fullmatch(r"[0-9a-f]{40}", mid):   # only a commit id recorded for the pin; anything else fails as before
        raise
    ckpt = hf_hub_download("arcinstitute/evo2_7b", "evo2_7b.pt", revision=mid, local_files_only=True)
model = Evo2("evo2_7b", local_path=ckpt)   # keep the model name explicit: it selects the packaged config
seqs = ["ATCG" * 50, "GGGCTTAA" * 25]
ll = model.score_sequences(seqs)   # → list[float], mean per-token log-likelihood
print(ll)
```

### Generation

```python
out = model.generate(
    prompt_seqs=["ATGAAAGCT"],
    n_tokens=256,
    temperature=0.7,
)
print(out.sequences[0])
```

## Models

| Name        | Params | Context | VRAM (bf16) | Notes                              |
| ----------- | ------ | ------- | ----------- | ---------------------------------- |
| `evo2_7b`   | 7 B    | 1 M nt  | ~22 GB      | Default; fits on a single 24 GB+ GPU; the recipe above pins its checkpoint to a fixed revision |
| `evo2_40b`  | 40 B   | 1 M nt  | ~78 GB      | H100 80 GB or multi-GPU; not pinned here (loads upstream's current revision) |
| `evo2_1b_base` | 1 B | 8 K nt  | ~6 GB       | FP8 path requires sm_89+ (H100); not pinned here |

## Output format

`score_sequences` returns a `list[float]` (or `np.ndarray`) of mean log-likelihoods,
one per input sequence. More negative ⇒ less likely under the model. For variant
effect, compute `Δll = ll_alt - ll_ref` over a fixed window.

`generate` returns a `GenerationOutput` with `.sequences` (list[str]), `.logits`
(list[Tensor]), and `.logprobs_mean` (list[float]) — always populated, no flag required.

## Decision tree

```
Need a DNA model?
│
├─ Per-base/per-sequence likelihood, generation → Evo 2 ✓
├─ Predict experimental tracks (expression, accessibility) → borzoi
└─ Protein, not DNA → fair-esm2 / esmfold2
```


## Remote compute

7B/40B inference is GPU-bound (≥24 GB / 80 GB VRAM). Read
`compute_details({provider, mode:'read'})` for an environment with `evo2` +
`flash-attn` and a pre-cached HF weight mount, then submit:

```python
c = host.compute.create(provider)
job = c.submit_job(
    intent="Evo2-7B score 200bp variant window — 1×GPU, ~2 min",
    inputs=["score_evo2.py"],
    command="python3 score_evo2.py",   # env selection is host-specific — see compute_details for your provider
    outputs=["scores.json"],
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

Inside `score_evo2.py`, point `HF_HOME` at the provider's weight-cache mount
(path is in `compute_details`) and set `HF_HUB_OFFLINE=1` so the loader
doesn't try to write `refs/` into a read-only mount — both before `huggingface_hub`
or `evo2` is imported (or in the job's environment), since the cache path is fixed
at import. The 7B mount must hold the
pinned revision: hydrate it with `revision="bda0089f92582d5baabf0f22d9fc85f3588f6b58"`
as the bundled Modal environment and the SSH `genomics-gpu` preset's populate
recipe do (the 40B mount stays unpinned); on a 7B mount built from another commit
the load line fails with `LocalEntryNotFoundError` — rebuild that mount at the
pinned revision rather than loading what it holds. A mount filled at the pinned revision
through a Hugging Face mirror lists the snapshot under the mirror's own id instead; the load block finds
it through the cache's `refs/` entry for the pinned revision, which such a fetch records.
Weight footprint:
~14 GB (7B: one 13.8 GB checkpoint per the Hugging Face file listing at the
pinned revision, not re-measured here), ~80 GB (40B).


## Typical performance

| Task                        | 7B on H100 | Notes                       |
| --------------------------- | ---------- | --------------------------- |
| Model load (cached)         | ~5-7 min   | Weights fetched at install; the first call loads them from the cache |
| `score_sequences`, 200×200bp| ~10-20 s   | After load                  |
| `generate`, 1×512 nt        | ~15 s      |                             |

## Troubleshooting

| Symptom                              | Cause                          | Fix                                        |
| ------------------------------------ | ------------------------------ | ------------------------------------------ |
| `Transformer Engine not installed`   | No FP8 — falls back to bf16    | Informational only on non-H100; ignore     |
| OOM on load                          | 40B on <80 GB GPU              | Use `evo2_7b` or shard with `device_map`   |
| `LocalEntryNotFoundError` on load     | The pinned checkpoint is not in this `HF_HOME`: the install step was not run with it, or (remote) the weight mount was hydrated at another revision | Locally, run the install step's `hf_hub_download` line, then load again; on a provider mount, first check `HF_HOME` was set before the imports and list `$HF_HOME/hub/models--arcinstitute--evo2_7b/snapshots/` to see which commit the mount holds (a mirror-filled cache shows the mirror's id there and still loads if `refs/<pinned revision>` names it), then rebuild it at the pinned revision if it holds neither; keep `local_files_only=True` (turning it off would download inside the kernel) |
| HF tries to write `refs/main`        | `HF_HOME` points at RO mount   | Set `HF_HUB_OFFLINE=1`                     |
| `dtype mismatch` in `score_sequences`| Passing tensors not strings    | Pass `list[str]`; the API tokenizes for you |

---

**Next**: pair with `borzoi` to predict track-level effects of the same
variants.
