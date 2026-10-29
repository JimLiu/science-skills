"""evo2 — long-context genomic foundation model.

Use the 7b model: `Evo2('evo2_7b')`. The 1b variants require Transformer
Engine for FP8 input projections; TE's import-time CUDA init hangs in
Modal's CPU image-save phase and TE 1.13 hard-caps flash-attn ≤2.6.3, so
this env intentionally omits TE. evo2 0.5+ falls back to bf16 projections
for 7b without it (with a benign UserWarning).

`m.generate(...)` returns a `GenerationOutput`, not a tuple.
"""

import modal

META = {
    "packages": ["evo2", "torch", "flash_attn"],
    "gpu_default": "H100",



    "egress_domains": [],
}

_ENV = {"HF_HOME": "/weights", "HF_HUB_OFFLINE": "1"}


def build(
    *, secrets: dict[str, str] | None = None
) -> tuple["modal.Image", dict[str, "modal.Volume"], dict[str, str]]:
    secrets = secrets or {}
    img = (
        modal.Image.from_registry(
            "nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04", add_python="3.12"
        )
        .apt_install("build-essential", "git")
        .pip_install(
            "torch==2.7.1",
            "packaging~=24.0",
            "ninja~=1.11",
            "wheel~=0.43",
            "setuptools~=70.0",
            "psutil~=5.9",
            "einops~=0.8",
        )
        .env({"CC": "gcc", "CXX": "g++"})
        .run_commands(
            "MAX_JOBS=8 pip install --no-build-isolation flash-attn==2.8.0.post2"
        )
        .pip_install("evo2==0.5.5")
        .env(_ENV)
    )
    vols = {
        "/weights": modal.Volume.from_name(
            "claude-science-evo2-weights", create_if_missing=True
        ),
    }
    return img, vols, dict(_ENV)






HYDRATE = (
    "env",
    "HF_HUB_OFFLINE=0",
    "python",
    "-c",





    "from huggingface_hub import snapshot_download; "
    "from huggingface_hub.constants import HF_HUB_CACHE; "
    "import pathlib; "
    "sha='bda0089f92582d5baabf0f22d9fc85f3588f6b58'; "
    "snapshot_download('arcinstitute/evo2_7b', revision=sha); "
    "r=pathlib.Path(HF_HUB_CACHE)/'models--arcinstitute--evo2_7b'/'refs'; "
    "r.mkdir(parents=True, exist_ok=True); "
    "(r/'main').write_text(sha)",
)
