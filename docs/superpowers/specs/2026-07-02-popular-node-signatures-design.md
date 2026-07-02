# Popular Node Signature Intelligence Design

## Context

UTFCN currently ranks replacements from the live ComfyUI node registry and a small curated `mappings.json` file. That works well for installed custom nodes because the backend can inspect real `INPUT_TYPES`, `RETURN_TYPES`, display names, and source modules. It is weaker for missing or uninstalled nodes because ComfyUI preserves only the serialized workflow slots, which often omit widget-level signature data.

ComfyUI-Manager and the Comfy Registry expose broad custom-node metadata. Manager's list is useful for pack discovery and repository URLs. The Registry can add popularity signals such as downloads, stars, search ranking, and preempted node names. GitHub repositories can often be scanned offline to recover node class declarations and signatures. This design uses those public sources to improve coverage without changing UTFCN's runtime safety model.

## Goals

- Add broad pre-scanned coverage for popular custom nodes and node packs, targeting up to 1000 ranked entries from Manager and/or the Comfy Registry per generation run.
- Improve replacement suggestions for missing or uninstalled nodes by matching against bundled signatures instead of relying only on sparse serialized workflow slots.
- Keep runtime startup and scan behavior local, deterministic, and fast. The ComfyUI server must not fetch GitHub or registry data during normal use.
- Preserve UTFCN's trust model: curated and exact matches are verified; heuristic matches are suggestions only; Force mode never applies heuristics.
- Make the generated data reproducible and reviewable so future updates can refresh coverage without hand-editing large JSON blobs.

## Non-Goals

- Do not treat Manager or Registry metadata alone as proof that a node is equivalent to a core node.
- Do not auto-install custom nodes or their dependencies.
- Do not execute arbitrary custom-node repository code during runtime.
- Do not silently replace missing nodes based only on similar names.
- Do not require ComfyUI-Manager to be installed.

## Proposed Approach

Add a generated data artifact named `popular_node_signatures.json` and an update script that can regenerate it from public metadata. The artifact is committed to the repo and loaded by the existing backend alongside `mappings.json` and `user_mappings.json`.

The update script is a developer tool, not a runtime dependency. It fetches Manager and/or Registry metadata, ranks entries by available popularity signals, scans reachable GitHub repositories, extracts ComfyUI node signatures when feasible, and writes normalized JSON. Repositories that cannot be fetched or parsed are skipped with a recorded reason in generation metadata.

Runtime matching then has three signature sources:

1. Live installed signatures from `nodes.NODE_CLASS_MAPPINGS`.
2. Curated mappings from `mappings.json` and `user_mappings.json`.
3. Bundled popular-node signatures from `popular_node_signatures.json`.

Installed custom nodes continue to prefer live signatures because they are the strongest local truth. Missing nodes use curated mappings first, then bundled signatures by node type, then the existing serialized-slot heuristic fallback.

## Data Artifact

`popular_node_signatures.json` is machine-generated and stable enough for code review. It contains:

- `schema_version`: integer for future migrations.
- `generated_at`: ISO timestamp.
- `sources`: generation inputs, including Manager list URL, Registry query details when used, and limits.
- `packs`: map of pack id to pack metadata such as title, repository, ranking signals, and extraction status.
- `nodes`: map of ComfyUI node type to normalized signature and pack metadata.

Each node entry contains:

- `type`: ComfyUI class mapping key.
- `display`: display name if discoverable.
- `pack`: normalized pack id.
- `repository`: source repository URL.
- `inputs`: map of input name to reduced UTFCN type string.
- `required`: list of required input names.
- `outputs`: ordered list of output type strings.
- `output_names`: ordered list of output names when discoverable.
- `confidence`: extraction confidence such as `static_exact`, `static_partial`, or `metadata_only`.

Only entries with usable signature data participate in exact/partial matching. Metadata-only entries may be used for display or diagnostics but must not create replacement candidates by themselves.

## Extraction Strategy

The generator should start conservatively:

- Fetch Manager's `custom-node-list.json` and optionally the Registry `/nodes` API.
- Prefer entries with a GitHub repository URL.
- Rank by Registry downloads when available, then GitHub stars, search ranking, and Manager order as tie-breakers.
- Clone or fetch repository contents into a temporary cache outside the committed tree.
- Parse Python files with `ast` instead of importing repository code.
- Detect class-level `RETURN_TYPES`, `RETURN_NAMES`, `NODE_DISPLAY_NAME_MAPPINGS`, and `NODE_CLASS_MAPPINGS`.
- Extract common static `INPUT_TYPES` shapes when the method returns a literal dict or simple literal-compatible expression.
- Mark dynamic or unsupported shapes as skipped rather than guessing.

The first implementation can intentionally miss complex dynamic nodes. Coverage can improve over time by adding parser cases backed by fixtures.

## Backend Changes

Add a loader in `utfcn_core.py` for the generated artifact. If the file is absent, malformed, or has an unsupported schema version, UTFCN logs a warning and behaves as it does today.

Extend `build_context()` to include generated signatures in a separate namespace from live signatures. Live signatures and live source metadata remain authoritative for installed nodes. Generated signatures are used for uninstalled source nodes and as optional supplemental metadata.

Candidate ranking should keep the existing tiers:

- `curated`: explicit rule, verified.
- `exact`: identical signature, verified only when based on live installed source signatures or generated signatures with usable extracted data.
- `partial`: structurally feasible but not identical, suggestion only.

For missing nodes, matching order is:

1. Curated rule by node type.
2. Generated signature for that node type, if present and usable.
3. Serialized workflow signature fallback.

This order lets missing nodes benefit from full pre-scanned signatures while preserving the existing behavior for unknown nodes.

## Frontend Changes

The current frontend can remain mostly unchanged because it consumes backend candidates. Small UI improvements are acceptable if needed:

- Show generated-source matches with the existing tier labels.
- Keep Force mode limited to verified candidates returned by the backend.
- Keep preview behavior unchanged for partial matches.

No network access or GitHub-specific logic belongs in `web/utfcn.js`.

## Safety Rules

- Runtime never fetches remote metadata.
- Runtime never imports scanned third-party repository code from the generated dataset.
- A generated signature must include concrete input and output type data before it can influence matching.
- Metadata-only node names cannot produce verified matches.
- Name similarity can affect ranking only for suggestions, not Force mode.
- `user_mappings.json` continues to override shipped curated mappings for local user control.

## Testing Plan

Add focused backend tests that do not require a running ComfyUI instance:

- Rule loading still merges shipped and user mappings correctly.
- Generated artifact loading accepts the expected schema and rejects malformed data gracefully.
- Missing-node matching prefers curated rules over generated signatures.
- Generated exact signatures can produce verified exact matches against core targets.
- Generated partial matches remain unverified.
- Metadata-only entries do not produce candidates.

Add generator tests with small fixture repositories:

- Extracts static `NODE_CLASS_MAPPINGS` and `INPUT_TYPES`.
- Extracts `RETURN_TYPES` and optional `RETURN_NAMES`.
- Skips dynamic or unsupported `INPUT_TYPES` without failing the whole run.
- Produces deterministic JSON ordering.

Manual verification should include opening a workflow with a missing node whose type exists in the generated artifact and confirming the preview offers the expected replacement without installing the original pack.

## Rollout

1. Implement the generator and backend loader behind the generated artifact.
2. Add tests using small fixtures before generating the large dataset.
3. Generate an initial artifact from a limited sample and verify behavior.
4. Expand to up to 1000 ranked entries once extraction and matching are stable.
5. Document the refresh command and the trust model in `README.md`.

The initial commit may include a smaller sample artifact if full top-1000 extraction exposes parser or network edge cases. The runtime code should not depend on the artifact being complete.
