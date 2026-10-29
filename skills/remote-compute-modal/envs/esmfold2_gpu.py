"""Biohub ESMFold2 / ESMC stack (esmfold2 skill family).

Standalone because the skill pins requires-python=">=3.12,<3.13" — every
other proteomics env is python 3.11. Install validated live on A100-80GB:
torch 2.7.1+cu126, Biohub transformers fork, esm @ f652b471.

flash-attn is deliberately NOT installed: the ~5x trunk speedup comes from
the vendored Triton fused backend (set_kernel_backend("fused"), bundled
with the GPU torch wheel); flash-attn only accelerates ESMC's attention
path and added zero measured trunk speedup in QA. Skipping it also lets
this build on Modal's CPU build infra (no nvcc compile fallback risk).

NOTE: ESMFold2Model (transformers path) cannot be imported without a live
CUDA driver — Triton autotune fires at import time. Do not smoke-import it
in a CPU sandbox; use `from esm.models.esmfold2 import ...` for CPU-side
API checks.
"""

import modal

META = {
    "packages": ["esm", "transformers", "torch"],
    "gpu_default": "A100-80GB",


    "egress_domains": [],
}




_TRANSFORMERS_PIN = (
    "transformers @ git+https://github.com/Biohub/transformers.git"
    "@3a8956fb4d4ea16b0ec8e71deef2c2909b6a5cbf"
)
_ESM_PIN = "esm @ git+https://github.com/Biohub/esm.git@f652b471d29da828b31e9b7a9cf7d0a7803240f5"


def build(
    *, secrets: dict[str, str] | None = None
) -> tuple["modal.Image", dict[str, "modal.Volume"], dict[str, str]]:
    secrets = secrets or {}
    img = (


        modal.Image.from_registry(
            "nvidia/cuda:12.4.1-devel-ubuntu22.04", add_python="3.12"
        )
        .apt_install("git")
        .pip_install(
            "torch==2.7.1",
            "einops==0.8.2",
            "biotite==1.6.0",
            "rdkit==2026.3.3",
            "msgpack-numpy==0.4.8",
            "biopython==1.87",
            "scikit-learn==1.9.0",
            "brotli==1.2.0",
            "attrs==26.1.0",
            "pandas==3.0.3",
            "cloudpathlib==0.24.0",
            "httpx==0.28.1",
            "tenacity==9.1.4",
            "zstd==1.5.7.3",
            "pydssp==0.9.1",
            "pygtrie==2.5.0",
            "accelerate==1.14.0",







            "huggingface_hub==0.36.2",
            "safetensors==0.8.0",
            "numpy==2.4.6",
            "networkx==3.6.1",
            "sentencepiece==0.2.1",
            "tokenizers==0.22.2",
            "regex==2026.5.9",
            "packaging==26.2",
            "filelock==3.29.3",
            "pyyaml==6.0.3",
            "typing_extensions==4.15.0",
            _TRANSFORMERS_PIN,
        )





        .pip_install(_ESM_PIN, extra_options="--no-deps")










        .env({"HF_HOME": "/datavol_esm/hf", "HF_HUB_OFFLINE": "1"})
    )
    vols = {
        "/datavol_esm": modal.Volume.from_name(
            "claude-science-esm-cache", create_if_missing=True
        ),
    }
    return img, vols, {"HF_HOME": "/datavol_esm/hf", "HF_HUB_OFFLINE": "1"}

























HYDRATE = (
    "env",
    "HF_HUB_OFFLINE=0",
    "python",
    "-c",
    "from huggingface_hub import snapshot_download\n"
    "from huggingface_hub.constants import HF_HUB_CACHE\n"
    "import pathlib\n"
    "pins = {\n"
    "  'biohub/ESMFold2':      '1ebf0e3481a5184eb6171d40615c79e384b48796',\n"
    "  'biohub/ESMFold2-Fast': 'b28d8ace5e05e61e5bec1e6820cfd3e221819d12',\n"
    "  'biohub/ESMC-6B':       '45b0fa5d7fb06faefbd5e3b89bdcef35d564e79a',\n"
    "}\n"
    "for repo, sha in pins.items():\n"
    "  snapshot_download(repo, revision=sha)\n"
    "  d = pathlib.Path(HF_HUB_CACHE, 'models--' + repo.replace('/', '--'), 'refs')\n"
    "  d.mkdir(parents=True, exist_ok=True)\n"
    "  (d / 'main').write_text(sha)\n",
)
