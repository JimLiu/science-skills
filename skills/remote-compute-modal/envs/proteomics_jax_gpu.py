"""ColabFold 1.6.1 + jax 0.5.x/cu12 (PyPI plugin model — no version archaeology)."""

import modal

META = {
    "packages": ["colabfold", "jax", "alphafold"],
    "gpu_default": "A100",





    "egress_domains": ["api.colabfold.com"],
}

_ENV = {
    "JAX_COMPILATION_CACHE_DIR": "/root/.cache/jax",
    "TF_FORCE_UNIFIED_MEMORY": "0",
    "XLA_PYTHON_CLIENT_MEM_FRACTION": "0.95",
    "JAX_PLATFORMS": "cuda",
}


def build(
    *, secrets: dict[str, str] | None = None
) -> tuple["modal.Image", dict[str, "modal.Volume"], dict[str, str]]:
    secrets = secrets or {}
    img = (
        modal.Image.from_registry(
            "nvidia/cuda:12.4.1-runtime-ubuntu22.04", add_python="3.11"
        )
        .apt_install("aria2")
        .pip_install(
            "colabfold[alphafold-minus-jax]==1.6.1",
            "jax==0.5.3",




            "jax-cuda12-plugin[with-cuda]==0.5.3",
        )






        .run_commands(
            "python3 -c 'import colabfold.batch as b; print(b.__file__)' | "
            "xargs sed -i "
            '-e \'s/TF_FORCE_UNIFIED_MEMORY"] = "1"/TF_FORCE_UNIFIED_MEMORY"] = "0"/\' '
            '-e \'s/XLA_PYTHON_CLIENT_MEM_FRACTION"] = "4.0"/XLA_PYTHON_CLIENT_MEM_FRACTION"] = "0.95"/\''
        )
        .env(_ENV)
    )
    vols = {
        "/root/.cache/colabfold": modal.Volume.from_name(
            "claude-science-colabfold-weights", create_if_missing=True
        ),
        "/root/.cache/jax": modal.Volume.from_name(
            "claude-science-colabfold-jax-cache", create_if_missing=True
        ),
    }
    return img, vols, dict(_ENV)



HYDRATE = ("python", "-m", "colabfold.download")
