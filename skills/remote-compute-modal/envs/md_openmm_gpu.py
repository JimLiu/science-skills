"""OpenMM molecular dynamics — explicit/implicit-solvent MD + MM-GBSA on CUDA.

conda-forge via micromamba: OpenMM's CUDA platform plugin is a compiled
extension linked against a specific cudatoolkit and only ships there (the
PyPI wheel is CPU/OpenCL-only). cudatoolkit=11.8 is the pin the reference
campaign verified end-to-end (platforms=['Reference','CPU','CUDA'] on A10G).
"""

import modal

META = {
    "packages": ["openmm", "openmmforcefields", "pdbfixer", "mdtraj", "openff-toolkit"],
    "gpu_default": "A100",




    "egress_domains": [],
}


def build(
    *, secrets: dict[str, str] | None = None
) -> tuple["modal.Image", dict[str, "modal.Volume"], dict[str, str]]:
    secrets = secrets or {}
    img = (
        modal.Image.micromamba(python_version="3.11")
        .micromamba_install(
            "openmm=8.2.0",
            "openmmforcefields=0.15.1",
            "pdbfixer=1.12",
            "mdtraj=1.11.1",
            "openff-toolkit=0.18.0",
            "cudatoolkit=11.8",
            "numpy=2.4.6",
            channels=["conda-forge"],
        )






        .run_commands(
            "ln -sf /opt/conda/bin/python /usr/local/bin/python && "
            "ln -sf /opt/conda/bin/python3 /usr/local/bin/python3 && "
            "python -c 'import openmm, pdbfixer, mdtraj, openmmforcefields, "
            "openff.toolkit; print(openmm.Platform.getNumPlatforms())'"
        )
    )


    return img, {}, {}
