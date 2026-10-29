---
name: ligandmpnn
description: >
  Inverse-fold a backbone with ligand, nucleic-acid, and metal context using
  LigandMPNN (Dauparas et al. 2023, github.com/dauparas/LigandMPNN). Reach for
  this skill to redesign the residues lining a binding pocket around a bound
  small molecule or cofactor, to design metal-coordinating sites where the
  geometry must be respected, or to get threaded designed-sequence PDBs out of
  any MPNN run.
license: Apache-2.0
category: biomodels
metadata:
  display-name: LigandMPNN
  # github.com/dauparas/LigandMPNN/blob/main/LICENSE: MIT (© 2024 Justas
  # Dauparas). verified 2026-06-30
  third_party:
    - kind: weights
      name: LigandMPNN
      license: MIT
      terms_url: https://github.com/dauparas/LigandMPNN/blob/main/LICENSE
---

# LigandMPNN

LigandMPNN extends the ProteinMPNN graph with non-protein atoms — small
molecules, nucleic acids, and metals are visible to the network — so it is the
right inverse-folding tool whenever the design surface includes a bound ligand
or cofactor that vanilla `proteinmpnn` would ignore. The same `run.py` is also
the most convenient runner for the other MPNN families because, unlike the
original ProteinMPNN script, it threads designs back onto the input structure
and writes PDBs alongside the FASTA. Code and weights are MIT
(github.com/dauparas/LigandMPNN). The model is small enough to run on CPU —
for a handful of designs on one structure that is seconds and usually faster
than dispatching, so the normal path is local (pinned recipe below); a GPU helps
for batched campaigns.

## Running it

```bash
# niche deps + SHA pinned for supply-chain hygiene — bump via PR. SHA matches env-setup.md
# Pattern (a); torch/numpy left unpinned (already in the local kernel — pinning would downgrade).
pip install torch numpy biopython==1.87 ProDy==2.6.1 ml_collections==1.1.0 dm-tree==0.1.10
git clone https://github.com/dauparas/LigandMPNN.git ligandmpnn
# One command: if the clone or the pinned checkout fails the cell stops here — do not
# cd in by hand then: the steps below would run on unpinned code.
git -C ligandmpnn checkout 26ec57ac976ade5379920dbd43c7f97a91cf82de && cd ligandmpnn || exit 1
# repo pins numpy 1.23; the np.int alias is gone in >=1.24 (python, not sed -i: the same on macOS and Linux)
python -c "import re, pathlib; p = pathlib.Path('openfold/np/residue_constants.py'); p.write_text(re.sub(r'np\.int\b', 'np.int64', p.read_text()))"
bash get_model_params.sh ./model_params
# the checkpoints are unsigned pickles fetched with no checksum (and a torch older
# than 2.6 runs a pickle's code on load): verify all 15 before run.py loads any —
# a mismatched or truncated file is renamed aside (*.rejected, *.rejected.1, …) and the cell stops
# here, so nothing below runs on it ("|| exit 1" ends a script or tool cell; pasted into
# an interactive shell it would close that shell — drop it there and stop by hand on an error)
# (digests: remote-compute-modal/env-setup.md, Pattern (a) — the same table)
python - <<'EOF' || exit 1
import hashlib, pathlib, sys
if not pathlib.Path("run.py").is_file():   # only ever inside the LigandMPNN clone
    sys.exit("not inside the LigandMPNN clone — nothing checked, nothing renamed or removed")
want = {
    "global_label_membrane_mpnn_v_48_020.pt": "89ef0abddbfb956c4c7c02dcba6523c0b5152733055f7509292365eaecc38b21",
    "ligandmpnn_sc_v_32_002_16.pt": "799a9de6c1c72bb0c7ceb37998391a6fd0e3e21cb42928ee31a28313a3b1b46a",
    "ligandmpnn_v_32_005_25.pt": "ee07a7afb53bce98a0b1d33996bbe3b46b0831df5ce0684d3cbd55eed1aa9263",
    "ligandmpnn_v_32_010_25.pt": "161cd264061fda9680cbb940255522ae42f2966c552d045d87913d9452a80970",
    "ligandmpnn_v_32_020_25.pt": "42caaa5cdb380867d2b31c30de4ab53ea89171279b3a0732ddf94bc4d3cb6981",
    "ligandmpnn_v_32_030_25.pt": "ed13e130787f70b6c385efba56949220d16a1d1a98073ada3002b3701a6a8ecf",
    "per_residue_label_membrane_mpnn_v_48_020.pt": "1e62e193bee12f1b64fff87ae3ca9da95b0f60b15839ae3cbe48e4983ef055ba",
    "proteinmpnn_v_48_002.pt": "925f2ca1007bf9b02e0e7f420ff00eb91f50fcc2722f64b42e644ae95adaa131",
    "proteinmpnn_v_48_010.pt": "db866fae956a28661f926053d630610c55e9fc4bc03922f2aeeb98a37435ccce",
    "proteinmpnn_v_48_020.pt": "c9cb4a671d79604111231f8dbfc7c590e06f1197453b7a6854ac6661a642f5bd",
    "proteinmpnn_v_48_030.pt": "c34b7bfb38418ea30989fda3314f4781ac4e3920f9825731cf555f1fed44ac66",
    "solublempnn_v_48_002.pt": "0877f840978fe770be6fcec025784d8f50c438571db3260c05e41aa207a7c448",
    "solublempnn_v_48_010.pt": "79562f7444f72c84595a1c96010713864865a616f4f3967633493041e169fa6e",
    "solublempnn_v_48_020.pt": "7af52d090172c230c7f0e9d21e02203f6b3a38b16db58d3c7a3960e0a9a6e31a",
    "solublempnn_v_48_030.pt": "1dd63f1e9fc68a133cc9ef859edf43b489e5ac581cb5624e0b9ec848ff062421",
}
bad = [f for f, d in want.items()
       if not (p := pathlib.Path("model_params", f)).is_file()
       or hashlib.sha256(p.read_bytes()).hexdigest() != d]
aside = []   # never leave a mismatched pickle where run.py would load it: set it aside under a free name, unloadable but not deleted
for f in bad:
    if (p := pathlib.Path("model_params", f)).is_file():
        q, n = p.with_name(p.name + ".rejected"), 1
        while q.exists(): q, n = p.with_name(f"{p.name}.rejected.{n}"), n + 1
        p.rename(q); aside.append(q.name)
sys.exit(f"checkpoint digest mismatch or missing: {bad} (set aside as: {aside}) — refetch with 'bash get_model_params.sh ./model_params', then rerun this check before run.py" if bad else 0)
EOF
python run.py \
  --model_type ligand_mpnn \
  --checkpoint_ligand_mpnn ./model_params/ligandmpnn_v_32_010_25.pt \
  --pdb_path complex.pdb \
  --out_folder out \
  --batch_size 8 --number_of_batches 4 \
  --temperature 0.1 \
  --fixed_residues "A45 A46 A47 A48"
```

Residue selections are space-separated `{chain}{resnum}` tokens inside one
quoted string (`"A45 A46 B10"`; insertion codes append directly, `"B82A"`).
That is the format for `--fixed_residues` and `--redesigned_residues`;
`--bias_AA_per_residue` and `--omit_AA_per_residue` instead take a path to a
JSON file whose keys use the same `{chain}{resnum}` form, and
`--chains_to_design` is comma-separated (`"A,B"`). If you want to redesign only the pocket, naming the pocket residues
in `--redesigned_residues` is usually shorter than fixing everything else.

Under `--out_folder` you get `seqs/<stem>.fa` (headers carry
`overall_confidence` and `ligand_confidence`), `backbones/<stem>_{1..N}.pdb`
with the designed sequence threaded onto the input coordinates, and — with
`--pack_side_chains 1` — full-atom packed models in `packed/`. The threaded
PDBs are the reason to prefer this runner even for protein-only jobs.

## Model types — which one to pick

| `--model_type` | sees | use |
|---|---|---|
| `ligand_mpnn` | backbone + ligand/NA/metal atoms | binding-pocket or active-site design |
| `protein_mpnn` | backbone only | protein–protein; same weights as `proteinmpnn` |
| `soluble_mpnn` | backbone only, soluble-trained | expression-biased prior; see `solublempnn` |
| `*_membrane_mpnn` | backbone + membrane label | transmembrane designs |

Each model type has its own `--checkpoint_<type>` flag; the wrong pairing is
caught at load time, but the default checkpoint path is relative to the repo,
so run from inside the clone or pass the absolute path.

## ProDy compiles from source on py3.11 — `pip install` fails without a C compiler

`run.py` imports ProDy unconditionally for ligand atom parsing. On py3.11 the
prebuilt wheel is missing on PyPI, so `pip install ProDy` compiles from source
and needs a working C/C++ compiler. On Modal's `add_python` bases the default
`CXX=clang++` points at a missing binary — `apt_install("build-essential")`
and export `CC=gcc CXX=g++` before the install. On most CPU-local Python
distributions the sdist builds in ~10 s if no wheel matches your Python.

## Turning ligand context off changes the answer, not the model

`--ligand_mpnn_use_atom_context 0` keeps the ligand-aware weights but masks the
ligand atoms at inference. That is useful for an ablation — the difference
between context-on and context-off tells you how much the ligand is shaping the
design — but it is **not** equivalent to running `protein_mpnn`, which uses a
different checkpoint trained without those features. For a fair protein-only
baseline, switch `--model_type`.

## Stripped HETATM or a chain filter silently drops the ligand — the design comes back pocket-blind

LigandMPNN does not warn when no ligand atoms are found; it just runs as if
`--model_type protein_mpnn` had been picked. The two common ways this happens
are an input PDB whose HETATM records were stripped by an upstream
clean-up step, and `--parse_these_chains_only` naming the protein chains but
not the ligand's. If `ligand_confidence` in the FASTA header is missing or
zero across every design, the model never saw the ligand — fix the input, do
not trust the sequences.

## Errors worth recognizing

| You see | It means / do this |
|---|---|
| `ModuleNotFoundError: No module named 'tree'` | `pip install dm-tree==0.1.10` — the vendored openfold imports it unconditionally. |
| `module 'numpy' has no attribute 'int'` | Run the numpy patch line (the `python -c` one-liner above) on `openfold/np/residue_constants.py`, or pin `numpy<1.24` (py≤3.11 only). |
| `error: command 'clang' failed` while `pip install ProDy` | See the ProDy gotcha above — `apt_install("build-essential")` and `env({"CC":"gcc","CXX":"g++"})`. |
| `FileNotFoundError` for `model_params/...` | Checkpoints not fetched (or set aside by the check as `*.rejected`, `*.rejected.1`, …) — run `bash get_model_params.sh ./model_params` from inside the clone, then the verification block above, before `run.py`. |
| `checkpoint digest mismatch or missing: [...]` | A `.pt` is missing or did not match its pinned sha256 (truncated download, or the file changed upstream; a mismatched copy is renamed to `*.rejected` — or `*.rejected.1`, … if that name is taken — not deleted) — rerun `bash get_model_params.sh ./model_params` and then the verification block, before `run.py`; if it persists, stop and report it rather than loading the file. On a Mac without `wget`, `get_model_params.sh` itself fetches nothing (install wget first). |

---

**Next:** fold the designs in complex with the ligand via `boltz` or `chai1`
(both accept SMILES/CCD) and filter on ipTM and ligand placement.
