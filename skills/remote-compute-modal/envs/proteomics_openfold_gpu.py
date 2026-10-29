"""OpenFold3 0.4.1 [cuequivariance].

[cuequivariance] extra requires torch>=2.7; openfold3 declares
rdkit + pdbeccdutils itself.

CLI: `run_openfold predict --query-json=<json> --output-dir=<dir>
     --inference-ckpt-path=/weights/checkpoints/of3-p2-155k.pt`

Query JSON shape (NOT AF3-style):
    {"queries": {"<name>": {"chains": [
        {"molecule_type": "protein", "chain_ids": ["A"],
         "sequence": "..."}]}}}

Gotcha: output mmCIF lacks _atom_site.occupancy — Bio.PDB.MMCIFParser
chokes; use gemmi or read B_iso_or_equiv (== per-atom pLDDT) directly.
"""

import modal

META = {
    "packages": ["openfold3", "torch"],
    "gpu_default": "A100-80GB",



    "egress_domains": ["api.colabfold.com"],
}

_ENV = {
    "OPENFOLD_CACHE": "/weights",



    "DS_BUILD_OPS": "0",
    "DS_SKIP_CUDA_CHECK": "1",
    "CUDA_HOME": "",
}
_CKPT_URL = (
    "https://openfold3-data.s3.amazonaws.com/openfold3-parameters/of3-p2-155k.pt"
)
_CKPT_SHA256 = "af09eac4f29cef856633af07558cb143226fe95ebbef2c20921769d4a5f4bee4"


def build(
    *, secrets: dict[str, str] | None = None
) -> tuple["modal.Image", dict[str, "modal.Volume"], dict[str, str]]:
    secrets = secrets or {}
    img = (
        modal.Image.from_registry(
            "nvidia/cuda:12.4.1-runtime-ubuntu22.04", add_python="3.11"
        )
        .apt_install("aria2", "libxrender1", "libxext6", "libexpat1", "gcc")



        .run_commands(
            "printf '#!/bin/sh\\necho \"Cuda compilation tools, release 12.4\"\\n'"
            " > /usr/local/bin/nvcc && chmod +x /usr/local/bin/nvcc"
        )
        .pip_install("openfold3[cuequivariance]==0.4.1")



















        .run_commands(
            "python3 -c '"
            "import importlib.util, pathlib, sys; "
            'spec = importlib.util.find_spec("openfold3") '
            'or sys.exit("openfold3 is not importable after pip install"); '
            "p = pathlib.Path(spec.origin).parent / "
            '"projects/of3_all_atom/config/model_config.py"; '
            "src = p.read_text(); "
            'old = chr(34) + "use_deepspeed_evo_attention" + chr(34) '
            '+ ": not _is_rocm"; '
            'new = chr(34) + "use_deepspeed_evo_attention" + chr(34) '
            '+ ": False"; '
            'sys.exit(f"patch target not found in {p} - openfold3 '
            'model_config drifted") if old not in src else '
            "p.write_text(src.replace(old, new, 1)); "
            'print(f"patched {p}")'
            "'"
        )
        .env(_ENV)
    )
    vols = {
        "/weights": modal.Volume.from_name(
            "claude-science-openfold3-weights", create_if_missing=True
        ),
    }
    return img, vols, dict(_ENV)





HYDRATE = (
    "bash",
    "-lc",
    "set -e; mkdir -p /weights/checkpoints; cd /weights/checkpoints; "


    f'echo "{_CKPT_SHA256}  of3-p2-155k.pt" | sha256sum -c - 2>/dev/null || '
    "{ rm -f of3-p2-155k.pt; "
    f"  aria2c -x16 -s16 -o of3-p2-155k.pt {_CKPT_URL}; "
    f'  echo "{_CKPT_SHA256}  of3-p2-155k.pt" | sha256sum -c -; }}',
)
