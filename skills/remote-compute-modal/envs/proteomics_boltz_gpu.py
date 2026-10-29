"""Boltz-2 structure prediction.

Standalone because boltz[cuda]'s cuequivariance dependency forces a torch /
nvidia-cublas / CUDA-toolkit version set that conflicts with the rest of
the proteomics stack. Let it have what it wants here.
"""

import modal

META = {
    "packages": ["boltz", "torch"],
    "gpu_default": "A100-80GB",




    "egress_domains": [
        "api.colabfold.com",
        "model-gateway.boltz.bio",
        "huggingface.co",
        "*.hf.co",
    ],
}


def build(
    *, secrets: dict[str, str] | None = None
) -> tuple["modal.Image", dict[str, "modal.Volume"], dict[str, str]]:
    secrets = secrets or {}






    img = (
        modal.Image.debian_slim(python_version="3.11")
        .pip_install("numpy==1.26.4")
        .pip_install("boltz[cuda]==2.2.1")




        .env(
            {
                "LD_LIBRARY_PATH": (
                    "/usr/local/lib/python3.11/site-packages/nvidia/cu13/lib"
                )
            }
        )
    )
    vols = {
        "/root/.boltz": modal.Volume.from_name(
            "claude-science-boltz-cache", create_if_missing=True
        ),
    }
    return img, vols, {}



HYDRATE = (
    "python",
    "-c",
    "from boltz.main import download_boltz2; "
    "from pathlib import Path; download_boltz2(Path('/root/.boltz'))",
)
