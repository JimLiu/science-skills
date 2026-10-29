# Science Skills

This repository contains the prompt, skill, agent, and MCP server assets for a
Claude Science style scientific computing assistant. The contents are mostly
runtime-facing configuration and documentation: skills teach the assistant how
to perform domain workflows, agents define specialized profiles, and MCP
servers expose scientific data and chemistry tools.

## Source

The `skills/` and `mcp-servers/` assets match **Claude Science 0.1.71**
(public release, macOS arm64, built October 26, 2026). They are copied directly
from the app's embedded asset archive, including helper code, dependencies,
licenses, and widget assets.

`VERSION` records this asset snapshot version. `SOURCE.json` records the app's
build metadata, source checksums, and file-tree checksums for both directories.
Upstream package manifests and skill metadata are preserved verbatim; their
dependency versions are independent of the app version. The base prompt and
agent profiles are retained from the earlier repository snapshot and are not
covered by this version marker.

The base prompt and bundled skills in this repository are sourced from the
Claude Science product. The public product page is available for context:
https://claude.com/product/claude-science.

## Repository Layout

| Path | Purpose |
| --- | --- |
| `SYSTEM_PROMPT.md` | Base system prompt and operating rules for the scientific computing agent. |
| `skills/` | Skill packages. Each skill is centered on a `SKILL.md` file, with optional helper code, references, requirements, and provider metadata. |
| `agents/` | Agent profile metadata and prompts for specialized roles such as onboarding, transcript review, bookmarking, and the general science agent. |
| `mcp-servers/` | Bundled MCP servers and widgets used by the agent for external scientific data and chemistry workflows. |

## Skill Catalog

The `skills/` directory includes workflows for several broad areas:

- Biomolecular modeling and design: `alphafold2`, `boltz`, `chai1`,
  `openfold3`, `esmfold2`, `fair-esm2`, `diffdock`, `proteinmpnn`,
  `ligandmpnn`, `solublempnn`, `evo2`, and `borzoi`.
- Scientific analysis and reporting: `literature-review`, `pdf-explore`,
  `figure-style`, `figure-composer`, `paper-narrative`, and
  `indication-dossier`.
- Single-cell and omics workflows: `scgpt` and `scvi-tools`.
- Runtime and environment operations: `remote-compute-modal`,
  `remote-compute-ssh`, `compute-env-setup`, `managed-model-endpoints`, and
  `using-model-endpoint`.
- Product and customization workflows: `customize`, `skill-creator`,
  `product-self-knowledge`, and `self-awareness`.

Most skills start with YAML front matter:

```yaml
---
name: example-skill
description: When and why this skill should be loaded.
license: Apache-2.0
---
```

The body of `SKILL.md` should give the operational procedure, expected inputs,
failure modes, validation steps, and any tool-specific constraints needed for
reliable execution.

## Agents

Agent profiles live under `agents/<name>/metadata.yaml`.

- `operon` is the general-purpose scientific computing agent.
- `onboarding` supports first-run user onboarding.
- `reviewer` audits another agent transcript for unsupported claims,
  fabrication, or plan deviation.
- `bookmarker` selects transcript spans worth preserving as navigation
  breadcrumbs.

These files are configuration assets, not standalone applications. Keep prompt
changes tightly scoped and preserve any eval or benchmark notes near the prompt
they justify.

## MCP Servers

The `mcp-servers/` directory contains bundled MCP integrations:

- `bio-tools` vendors multiple Python packages for biological and biomedical
  data retrieval. `run_server.py` launches a named stdio server from
  `mcp-servers/bio-tools/lib/`.
- `ketcher-chemistry` contains a chemistry MCP server and widget assets for
  structure editing and chemistry workflows.

The MCP code is runtime infrastructure. When changing it, prefer narrow edits
and verify both the server entry point and the shape of returned tool data.

## Working With This Repo

Useful inspection commands:

```bash
# List all skills
find skills -maxdepth 2 -name SKILL.md | sort

# List agent profiles
find agents -maxdepth 2 -name metadata.yaml | sort

# Show launchable bio-tools MCP servers
find mcp-servers/bio-tools/lib -maxdepth 2 -name server.py \
  | sed 's#^mcp-servers/bio-tools/lib/##; s#/server.py$##' \
  | sort
```

Before editing a skill or prompt:

1. Read the relevant `SKILL.md` or `metadata.yaml` completely.
2. Preserve existing safety, artifact, provenance, and validation rules unless
   the change explicitly updates them.
3. Keep examples executable and aligned with the current runtime surface.
4. Add or update references next to the skill when a workflow depends on
   external documentation or non-obvious operational knowledge.

## Maintenance Notes

- Use ASCII in new files unless the surrounding file already requires a wider
  character set.
- Avoid broad rewrites of prompts or generated MCP bundles without a concrete
  regression target.
- Treat bundled third-party services, model weights, and APIs as dependencies:
  document licenses, terms, privacy implications, and data movement in the
  relevant skill metadata when applicable.
- If a skill ships helper code such as `kernel.py`, keep the markdown
  instructions and helper APIs in sync.
