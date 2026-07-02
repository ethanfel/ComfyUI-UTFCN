# Popular Node Signatures Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a generated popular-node signature artifact so UTFCN can make better replacement suggestions for missing or uninstalled ComfyUI nodes.

**Architecture:** Runtime remains local and deterministic: `utfcn_core.py` loads a committed `popular_node_signatures.json` artifact and uses it only as supplemental signature data. A separate standard-library generator in `tools/` fetches Manager/Registry metadata, scans GitHub repository contents into a cache, statically extracts ComfyUI node signatures with `ast`, and writes deterministic JSON. Tests use `unittest` and small fixture repositories so the feature can be verified without ComfyUI, GitHub, or third-party test dependencies.

**Tech Stack:** Python standard library, `unittest`, `ast`, `urllib.request`, existing UTFCN backend and frontend.

---

## File Structure

- Modify `utfcn_core.py`: add generated artifact loading, generated signature normalization, and missing-node matching that prefers generated signatures before serialized-slot fallback.
- Modify `__init__.py`: load `popular_node_signatures.json` during context rebuild and pass it to `utfcn_core.build_context()`.
- Create `tools/generate_popular_node_signatures.py`: developer-only generator for Manager/Registry ranking, repository caching, static AST extraction, and deterministic artifact writing.
- Create `tests/test_utfcn_core_generated.py`: backend unit tests for artifact loading, malformed data handling, curated priority, exact generated matches, partial generated matches, and metadata-only skips.
- Create `tests/test_generate_popular_node_signatures.py`: generator unit tests using temporary fixture repositories and metadata payloads.
- Create `popular_node_signatures.json`: generated artifact. Start with a small generated sample, then expand once extraction is verified.
- Modify `README.md`: document the generated artifact, refresh command, runtime no-network behavior, and trust rules.

## Task 1: Add Generated Artifact Loader

**Files:**
- Modify: `utfcn_core.py`
- Create: `tests/test_utfcn_core_generated.py`

- [ ] **Step 1: Write failing loader tests**

Create `tests/test_utfcn_core_generated.py` with this content:

```python
import json
import tempfile
import unittest
from pathlib import Path

import utfcn_core


class GeneratedSignatureLoaderTests(unittest.TestCase):
    def test_missing_generated_file_returns_empty_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated = utfcn_core.load_generated_signatures(tmp)

        self.assertEqual({}, generated["sigs"])
        self.assertEqual({}, generated["meta"])
        self.assertEqual({}, dict(generated["by_out"]))

    def test_loads_usable_static_signature(self):
        payload = {
            "schema_version": 1,
            "generated_at": "2026-07-02T00:00:00Z",
            "sources": {"limit": 1},
            "packs": {
                "sample-pack": {
                    "title": "Sample Pack",
                    "repository": "https://github.com/example/sample-pack",
                }
            },
            "nodes": {
                "SampleImageSize": {
                    "type": "SampleImageSize",
                    "display": "Sample Image Size",
                    "pack": "sample-pack",
                    "repository": "https://github.com/example/sample-pack",
                    "inputs": {"image": "IMAGE"},
                    "required": ["image"],
                    "outputs": ["INT", "INT"],
                    "output_names": ["width", "height"],
                    "confidence": "static_exact",
                }
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "popular_node_signatures.json").write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            generated = utfcn_core.load_generated_signatures(tmp)

        self.assertEqual({"image": "IMAGE"}, generated["sigs"]["SampleImageSize"]["inputs"])
        self.assertEqual({"image"}, generated["sigs"]["SampleImageSize"]["required"])
        self.assertEqual(["INT", "INT"], generated["sigs"]["SampleImageSize"]["outputs"])
        self.assertEqual(["width", "height"], generated["sigs"]["SampleImageSize"]["output_names"])
        self.assertEqual("sample-pack", generated["meta"]["SampleImageSize"]["pack"])
        self.assertEqual("Sample Image Size", generated["meta"]["SampleImageSize"]["display"])
        self.assertEqual(["SampleImageSize"], generated["by_out"]["INT"])

    def test_rejects_metadata_only_entries_for_matching(self):
        payload = {
            "schema_version": 1,
            "generated_at": "2026-07-02T00:00:00Z",
            "sources": {},
            "packs": {},
            "nodes": {
                "NameOnlyNode": {
                    "type": "NameOnlyNode",
                    "display": "Name Only",
                    "pack": "name-only",
                    "repository": "https://github.com/example/name-only",
                    "inputs": {},
                    "required": [],
                    "outputs": [],
                    "output_names": [],
                    "confidence": "metadata_only",
                }
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "popular_node_signatures.json").write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            generated = utfcn_core.load_generated_signatures(tmp)

        self.assertNotIn("NameOnlyNode", generated["sigs"])
        self.assertNotIn("NameOnlyNode", generated["meta"])
        self.assertEqual({}, dict(generated["by_out"]))

    def test_malformed_generated_file_returns_empty_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "popular_node_signatures.json").write_text("{broken", encoding="utf-8")
            generated = utfcn_core.load_generated_signatures(tmp)

        self.assertEqual({}, generated["sigs"])
        self.assertEqual({}, generated["meta"])
        self.assertEqual({}, dict(generated["by_out"]))

    def test_unsupported_schema_returns_empty_indexes(self):
        payload = {
            "schema_version": 99,
            "generated_at": "2026-07-02T00:00:00Z",
            "sources": {},
            "packs": {},
            "nodes": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "popular_node_signatures.json").write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            generated = utfcn_core.load_generated_signatures(tmp)

        self.assertEqual({}, generated["sigs"])
        self.assertEqual({}, generated["meta"])
        self.assertEqual({}, dict(generated["by_out"]))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the loader tests and verify they fail**

Run:

```bash
python -m unittest tests.test_utfcn_core_generated.GeneratedSignatureLoaderTests -v
```

Expected: FAIL with `AttributeError: module 'utfcn_core' has no attribute 'load_generated_signatures'`.

- [ ] **Step 3: Implement the generated artifact loader**

In `utfcn_core.py`, add this constant and functions after `_MAX_CANDIDATES`:

```python
_GENERATED_SCHEMA_VERSION = 1
_GENERATED_SIGNATURES_FILE = "popular_node_signatures.json"


def _empty_generated_signatures():
    return {"sigs": {}, "meta": {}, "by_out": defaultdict(list)}


def _normalise_generated_signature(node_type, entry):
    if not isinstance(entry, dict):
        return None
    if str(entry.get("confidence") or "") == "metadata_only":
        return None

    inputs_raw = entry.get("inputs") or {}
    if not isinstance(inputs_raw, dict):
        return None
    outputs_raw = entry.get("outputs") or []
    if not isinstance(outputs_raw, list):
        return None

    inputs = {str(k): str(v) for k, v in inputs_raw.items() if k is not None}
    outputs = [str(v) for v in outputs_raw if v is not None]
    if not inputs and not outputs:
        return None

    required_raw = entry.get("required") or []
    if not isinstance(required_raw, list):
        required_raw = []
    output_names_raw = entry.get("output_names") or []
    if not isinstance(output_names_raw, list):
        output_names_raw = []

    sig = {
        "inputs": inputs,
        "required": {str(v) for v in required_raw if str(v) in inputs},
        "outputs": outputs,
        "output_names": [str(v) for v in output_names_raw],
    }
    meta = {
        "source": "generated",
        "pack": str(entry.get("pack") or ""),
        "display": str(entry.get("display") or entry.get("type") or node_type),
        "repository": str(entry.get("repository") or ""),
        "confidence": str(entry.get("confidence") or ""),
    }
    return sig, meta


def load_generated_signatures(base_dir):
    path = os.path.join(base_dir, _GENERATED_SIGNATURES_FILE)
    generated = _empty_generated_signatures()
    if not os.path.isfile(path):
        return generated

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        print(f"[UTFCN] failed to read {_GENERATED_SIGNATURES_FILE}: {e}")
        return generated

    if not isinstance(raw, dict) or raw.get("schema_version") != _GENERATED_SCHEMA_VERSION:
        print(f"[UTFCN] ignored {_GENERATED_SIGNATURES_FILE}: unsupported schema")
        return generated

    nodes = raw.get("nodes") or {}
    if not isinstance(nodes, dict):
        print(f"[UTFCN] ignored {_GENERATED_SIGNATURES_FILE}: nodes must be an object")
        return generated

    for node_type, entry in nodes.items():
        normalised = _normalise_generated_signature(str(node_type), entry)
        if normalised is None:
            continue
        sig, meta = normalised
        generated["sigs"][str(node_type)] = sig
        generated["meta"][str(node_type)] = meta
        generated["by_out"][_first_output_type(sig)].append(str(node_type))

    return generated
```

- [ ] **Step 4: Run the loader tests and verify they pass**

Run:

```bash
python -m unittest tests.test_utfcn_core_generated.GeneratedSignatureLoaderTests -v
```

Expected: PASS for all 5 loader tests.

- [ ] **Step 5: Commit the loader**

Run:

```bash
git add utfcn_core.py tests/test_utfcn_core_generated.py
git commit -m "Add generated signature loader"
```

Expected: commit succeeds.

## Task 2: Use Generated Signatures For Missing-Node Matching

**Files:**
- Modify: `utfcn_core.py`
- Modify: `__init__.py`
- Modify: `tests/test_utfcn_core_generated.py`

- [ ] **Step 1: Add failing generated matching tests**

Append these tests above the `if __name__ == "__main__":` block in `tests/test_utfcn_core_generated.py`:

```python
class GeneratedSignatureMatchingTests(unittest.TestCase):
    def _ctx(self, rules=None, generated=None):
        live_sigs = {
            "CoreImageSize": {
                "inputs": {"image": "IMAGE"},
                "required": {"image"},
                "outputs": ["INT", "INT"],
                "output_names": ["width", "height"],
            },
            "CoreMaskInvert": {
                "inputs": {"mask": "MASK"},
                "required": {"mask"},
                "outputs": ["MASK"],
                "output_names": ["mask"],
            },
            "CuratedTarget": {
                "inputs": {"image": "IMAGE"},
                "required": {"image"},
                "outputs": ["INT", "INT"],
                "output_names": ["width", "height"],
            },
        }
        sources = {
            "CoreImageSize": {"source": "core", "pack": "nodes", "display": "Core Image Size"},
            "CoreMaskInvert": {"source": "core", "pack": "nodes", "display": "Core Mask Invert"},
            "CuratedTarget": {"source": "core", "pack": "nodes", "display": "Curated Target"},
        }
        by_out = utfcn_core.defaultdict(list)
        for name, sig in live_sigs.items():
            by_out[sig["outputs"][0]].append(name)
        return {
            "sources": sources,
            "sigs": live_sigs,
            "by_out": by_out,
            "rules": rules or {},
            "generated": generated or utfcn_core._empty_generated_signatures(),
        }

    def test_generated_exact_signature_matches_missing_node_as_verified(self):
        generated = utfcn_core._empty_generated_signatures()
        generated["sigs"]["SampleImageSize"] = {
            "inputs": {"image": "IMAGE"},
            "required": {"image"},
            "outputs": ["INT", "INT"],
            "output_names": ["width", "height"],
        }
        generated["meta"]["SampleImageSize"] = {
            "source": "generated",
            "pack": "sample-pack",
            "display": "Sample Image Size",
            "repository": "https://github.com/example/sample-pack",
            "confidence": "static_exact",
        }
        generated["by_out"]["INT"].append("SampleImageSize")

        result = utfcn_core.match(self._ctx(generated=generated), [{"type": "SampleImageSize"}])

        self.assertEqual("CoreImageSize", result["SampleImageSize"][0]["to"])
        self.assertEqual("exact", result["SampleImageSize"][0]["tier"])
        self.assertTrue(result["SampleImageSize"][0]["verified"])

    def test_curated_rule_stays_first_before_generated_exact_match(self):
        generated = utfcn_core._empty_generated_signatures()
        generated["sigs"]["SampleImageSize"] = {
            "inputs": {"image": "IMAGE"},
            "required": {"image"},
            "outputs": ["INT", "INT"],
            "output_names": ["width", "height"],
        }
        generated["meta"]["SampleImageSize"] = {
            "source": "generated",
            "pack": "sample-pack",
            "display": "Sample Image Size",
            "repository": "https://github.com/example/sample-pack",
            "confidence": "static_exact",
        }
        generated["by_out"]["INT"].append("SampleImageSize")
        rules = {
            "SampleImageSize": [
                {
                    "to": "CuratedTarget",
                    "note": "Curated replacement wins over generated exact signature.",
                }
            ]
        }

        result = utfcn_core.match(self._ctx(rules=rules, generated=generated), [{"type": "SampleImageSize"}])

        self.assertEqual("CuratedTarget", result["SampleImageSize"][0]["to"])
        self.assertEqual("curated", result["SampleImageSize"][0]["tier"])
        self.assertTrue(result["SampleImageSize"][0]["verified"])

    def test_generated_partial_signature_matches_but_is_not_verified(self):
        generated = utfcn_core._empty_generated_signatures()
        generated["sigs"]["SampleMaskInvert"] = {
            "inputs": {"masks": "MASK"},
            "required": {"masks"},
            "outputs": ["MASK"],
            "output_names": ["mask"],
        }
        generated["meta"]["SampleMaskInvert"] = {
            "source": "generated",
            "pack": "sample-pack",
            "display": "Sample Mask Invert",
            "repository": "https://github.com/example/sample-pack",
            "confidence": "static_exact",
        }
        generated["by_out"]["MASK"].append("SampleMaskInvert")

        result = utfcn_core.match(self._ctx(generated=generated), [{"type": "SampleMaskInvert"}])

        self.assertEqual("CoreMaskInvert", result["SampleMaskInvert"][0]["to"])
        self.assertEqual("partial", result["SampleMaskInvert"][0]["tier"])
        self.assertFalse(result["SampleMaskInvert"][0]["verified"])

    def test_serialized_signature_fallback_still_handles_unknown_generated_node(self):
        result = utfcn_core.match(
            self._ctx(),
            [
                {
                    "type": "SerializedMaskInvert",
                    "inputs": {"masks": "MASK"},
                    "outputs": ["MASK"],
                    "output_names": ["mask"],
                }
            ],
        )

        self.assertEqual("CoreMaskInvert", result["SerializedMaskInvert"][0]["to"])
        self.assertEqual("partial", result["SerializedMaskInvert"][0]["tier"])
        self.assertFalse(result["SerializedMaskInvert"][0]["verified"])
```

- [ ] **Step 2: Run matching tests and verify they fail**

Run:

```bash
python -m unittest tests.test_utfcn_core_generated.GeneratedSignatureMatchingTests -v
```

Expected: FAIL because `match()` ignores `ctx["generated"]`.

- [ ] **Step 3: Pass generated signatures through backend context**

Change the `build_context` signature in `utfcn_core.py` from:

```python
def build_context(rules):
```

to:

```python
def build_context(rules, generated=None):
```

Change the returned context at the end of `build_context()` from:

```python
    return {"sources": sources, "sigs": sigs, "by_out": by_out, "rules": rules}
```

to:

```python
    return {
        "sources": sources,
        "sigs": sigs,
        "by_out": by_out,
        "rules": rules,
        "generated": generated or _empty_generated_signatures(),
    }
```

In `__init__.py`, change `_get_ctx()` to load both rule and generated data:

```python
def _get_ctx(refresh=False):
    global _CTX_CACHE
    if refresh or _CTX_CACHE is None:
        rules = utfcn_core.load_rules(_DIR)
        generated = utfcn_core.load_generated_signatures(_DIR)
        _CTX_CACHE = utfcn_core.build_context(rules, generated)
    return _CTX_CACHE
```

- [ ] **Step 4: Use generated signatures in `match()` before serialized fallback**

Replace the body of `match()` in `utfcn_core.py` with:

```python
def match(ctx, items):
    """
    Match a batch of nodes given only their (possibly serialized) signature —
    used for UNINSTALLED / missing nodes in an open workflow.

    `items`: [ {"type": str, "inputs": {name: TYPE}, "outputs": [TYPE], "output_names": [..]} ].
    Serialized nodes only carry link slots (not widget values), so 'exact' rarely
    fires; curated rules (by type name), bundled generated signatures, and
    partial link-type matches do.

    Returns a mapping from source node type to candidate list.
    """
    out = {}
    generated = ctx.get("generated") or _empty_generated_signatures()
    generated_sigs = generated.get("sigs") or {}
    generated_meta = generated.get("meta") or {}

    for it in items:
        t = it.get("type")
        if not t or t in out:
            continue

        gen_sig = generated_sigs.get(t)
        if gen_sig is not None:
            gen_pack = (generated_meta.get(t) or {}).get("pack")
            found = _candidates_for(t, gen_sig, gen_pack, ctx)
            if found:
                out[t] = found
                continue

        inputs = {k: str(v) for k, v in (it.get("inputs") or {}).items()}
        sig = {
            "inputs": inputs,
            "required": set(inputs),
            "outputs": [str(x) for x in (it.get("outputs") or [])],
            "output_names": list(it.get("output_names") or []),
        }
        found = _candidates_for(t, sig, None, ctx)
        if found:
            out[t] = found
    return out
```

- [ ] **Step 5: Run backend generated-signature tests**

Run:

```bash
python -m unittest tests.test_utfcn_core_generated -v
```

Expected: PASS.

- [ ] **Step 6: Run a syntax check on runtime modules**

Run:

```bash
python -m py_compile utfcn_core.py __init__.py
```

Expected: no output and exit code 0.

- [ ] **Step 7: Commit generated matching**

Run:

```bash
git add utfcn_core.py __init__.py tests/test_utfcn_core_generated.py
git commit -m "Use generated signatures for missing node matching"
```

Expected: commit succeeds.

## Task 3: Build Static Repository Signature Extraction

**Files:**
- Create: `tools/generate_popular_node_signatures.py`
- Create: `tests/test_generate_popular_node_signatures.py`

- [ ] **Step 1: Write failing AST extraction tests**

Create `tests/test_generate_popular_node_signatures.py` with this content:

```python
import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from tools.generate_popular_node_signatures import (
    extract_repo_signatures,
    normalise_input_spec,
    write_artifact,
)


class StaticExtractionTests(unittest.TestCase):
    def test_normalise_input_spec_reduces_combo_lists(self):
        self.assertEqual("COMBO", normalise_input_spec((["nearest", "bilinear"],)))
        self.assertEqual("IMAGE", normalise_input_spec(("IMAGE",)))
        self.assertEqual("FLOAT", normalise_input_spec(("FLOAT", {"default": 1.0})))

    def test_extracts_static_node_mapping_and_signatures(self):
        source = '''
class FancySize:
    RETURN_TYPES = ("INT", "INT")
    RETURN_NAMES = ("width", "height")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
            "optional": {
                "scale": ("FLOAT", {"default": 1.0}),
                "mode": (["nearest", "bilinear"],),
            },
        }


NODE_CLASS_MAPPINGS = {
    "FancySize": FancySize,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "FancySize": "Fancy Size",
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "__init__.py").write_text(textwrap.dedent(source), encoding="utf-8")
            result = extract_repo_signatures(
                Path(tmp),
                {
                    "id": "sample-pack",
                    "title": "Sample Pack",
                    "repository": "https://github.com/example/sample-pack",
                    "rank": 1,
                },
            )

        self.assertIn("FancySize", result["nodes"])
        node = result["nodes"]["FancySize"]
        self.assertEqual("Fancy Size", node["display"])
        self.assertEqual("sample-pack", node["pack"])
        self.assertEqual({"image": "IMAGE", "scale": "FLOAT", "mode": "COMBO"}, node["inputs"])
        self.assertEqual(["image"], node["required"])
        self.assertEqual(["INT", "INT"], node["outputs"])
        self.assertEqual(["width", "height"], node["output_names"])
        self.assertEqual("static_exact", node["confidence"])

    def test_skips_dynamic_input_types_without_failing_repo(self):
        source = '''
def build_inputs():
    return {"required": {"image": ("IMAGE",)}}


class DynamicNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return build_inputs()


NODE_CLASS_MAPPINGS = {
    "DynamicNode": DynamicNode,
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "__init__.py").write_text(textwrap.dedent(source), encoding="utf-8")
            result = extract_repo_signatures(
                Path(tmp),
                {
                    "id": "dynamic-pack",
                    "title": "Dynamic Pack",
                    "repository": "https://github.com/example/dynamic-pack",
                    "rank": 1,
                },
            )

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_write_artifact_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp, "popular_node_signatures.json")
            write_artifact(
                out,
                sources={"manager_url": "https://example.invalid/manager.json", "limit": 1},
                packs={
                    "b-pack": {"id": "b-pack", "title": "B Pack", "status": "ok"},
                    "a-pack": {"id": "a-pack", "title": "A Pack", "status": "ok"},
                },
                nodes={
                    "BNode": {
                        "type": "BNode",
                        "display": "B Node",
                        "pack": "b-pack",
                        "repository": "https://github.com/example/b-pack",
                        "inputs": {},
                        "required": [],
                        "outputs": ["IMAGE"],
                        "output_names": ["image"],
                        "confidence": "static_exact",
                    },
                    "ANode": {
                        "type": "ANode",
                        "display": "A Node",
                        "pack": "a-pack",
                        "repository": "https://github.com/example/a-pack",
                        "inputs": {},
                        "required": [],
                        "outputs": ["IMAGE"],
                        "output_names": ["image"],
                        "confidence": "static_exact",
                    },
                },
            )
            parsed = json.loads(out.read_text(encoding="utf-8"))

        self.assertEqual(["a-pack", "b-pack"], list(parsed["packs"]))
        self.assertEqual(["ANode", "BNode"], list(parsed["nodes"]))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run extraction tests and verify they fail**

Run:

```bash
python -m unittest tests.test_generate_popular_node_signatures.StaticExtractionTests -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'tools.generate_popular_node_signatures'`.

- [ ] **Step 3: Create the generator extraction module**

Create `tools/generate_popular_node_signatures.py` with these imports and constants:

```python
#!/usr/bin/env python3
"""Generate UTFCN's popular_node_signatures.json artifact."""

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

SCHEMA_VERSION = 1
MANAGER_LIST_URL = "https://raw.githubusercontent.com/ltdrdata/ComfyUI-Manager/main/custom-node-list.json"
REGISTRY_NODES_URL = "https://api.comfy.org/nodes"
```

Add these literal-evaluation helpers:

```python
class UnsupportedStaticExpression(Exception):
    pass


def _literal(node, env):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.List):
        return [_literal(item, env) for item in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_literal(item, env) for item in node.elts)
    if isinstance(node, ast.Dict):
        result = {}
        for key, value in zip(node.keys, node.values):
            if key is None:
                raise UnsupportedStaticExpression("dict unpacking is not supported")
            result[_literal(key, env)] = _literal(value, env)
        return result
    if isinstance(node, ast.Name) and node.id in env:
        return env[node.id]
    raise UnsupportedStaticExpression(type(node).__name__)


def _collect_module_env(tree):
    env = {}
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign):
            continue
        if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
            continue
        try:
            env[stmt.targets[0].id] = _literal(stmt.value, env)
        except UnsupportedStaticExpression:
            continue
    return env


def normalise_input_spec(spec):
    first = spec[0] if isinstance(spec, (list, tuple)) and spec else spec
    if isinstance(first, list):
        return "COMBO"
    return str(first)
```

Add these class and mapping extraction helpers:

```python
def _class_defs(tree):
    return {node.name: node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}


def _class_attr(cls, name, env):
    for stmt in cls.body:
        if not isinstance(stmt, ast.Assign):
            continue
        for target in stmt.targets:
            if isinstance(target, ast.Name) and target.id == name:
                try:
                    return _literal(stmt.value, env)
                except UnsupportedStaticExpression:
                    return None
    return None


def _input_types(cls, env):
    for stmt in cls.body:
        if not isinstance(stmt, ast.FunctionDef) or stmt.name != "INPUT_TYPES":
            continue
        for child in stmt.body:
            if isinstance(child, ast.Return):
                try:
                    value = _literal(child.value, env)
                except UnsupportedStaticExpression:
                    return None
                return value if isinstance(value, dict) else None
    return None


def _mapping_value_name(value):
    if isinstance(value, str):
        return value
    if isinstance(value, ast.Name):
        return value.id
    return None


def _node_class_mappings(tree, env):
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "NODE_CLASS_MAPPINGS" for target in stmt.targets):
            continue
        if not isinstance(stmt.value, ast.Dict):
            continue
        mappings = {}
        for key, value in zip(stmt.value.keys, stmt.value.values):
            try:
                node_type = _literal(key, env)
            except UnsupportedStaticExpression:
                continue
            class_name = _mapping_value_name(value)
            if node_type and class_name:
                mappings[str(node_type)] = class_name
        return mappings
    return {}


def _display_mappings(tree, env):
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "NODE_DISPLAY_NAME_MAPPINGS" for target in stmt.targets):
            continue
        try:
            value = _literal(stmt.value, env)
        except UnsupportedStaticExpression:
            return {}
        if isinstance(value, dict):
            return {str(k): str(v) for k, v in value.items()}
    return {}
```

Add these signature extraction functions:

```python
def _signature_from_class(node_type, cls, display, pack_meta, env):
    input_types = _input_types(cls, env)
    return_types = _class_attr(cls, "RETURN_TYPES", env)
    return_names = _class_attr(cls, "RETURN_NAMES", env)
    if not isinstance(input_types, dict) or not isinstance(return_types, (list, tuple)):
        return None

    inputs = {}
    required = []
    for section in ("required", "optional"):
        values = input_types.get(section) or {}
        if not isinstance(values, dict):
            return None
        for name, spec in values.items():
            inputs[str(name)] = normalise_input_spec(spec)
            if section == "required":
                required.append(str(name))

    output_names = []
    if isinstance(return_names, (list, tuple)):
        output_names = [str(name) for name in return_names]

    return {
        "type": node_type,
        "display": display or node_type,
        "pack": pack_meta["id"],
        "repository": pack_meta.get("repository", ""),
        "inputs": inputs,
        "required": required,
        "outputs": [str(value) for value in return_types],
        "output_names": output_names,
        "confidence": "static_exact",
    }


def _python_files(repo_dir):
    skipped = {".git", "__pycache__", ".venv", "venv", "env", "site-packages"}
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in skipped]
        for filename in files:
            if filename.endswith(".py"):
                yield Path(root, filename)


def extract_repo_signatures(repo_dir, pack_meta):
    nodes = {}
    for path in sorted(_python_files(repo_dir)):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except UnicodeDecodeError:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"), filename=str(path))
        except SyntaxError:
            continue
        env = _collect_module_env(tree)
        mappings = _node_class_mappings(tree, env)
        displays = _display_mappings(tree, env)
        classes = _class_defs(tree)
        for node_type, class_name in sorted(mappings.items()):
            cls = classes.get(class_name)
            if cls is None:
                continue
            sig = _signature_from_class(node_type, cls, displays.get(node_type), pack_meta, env)
            if sig is not None:
                nodes[node_type] = sig

    pack = {
        "id": pack_meta["id"],
        "title": pack_meta.get("title", pack_meta["id"]),
        "repository": pack_meta.get("repository", ""),
        "rank": pack_meta.get("rank", 0),
        "status": "ok" if nodes else "no_static_nodes",
        "node_count": len(nodes),
    }
    return {"pack": pack, "nodes": nodes}
```

Add deterministic artifact writing:

```python
def write_artifact(path, sources, packs, nodes):
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "sources": sources,
        "packs": {key: packs[key] for key in sorted(packs)},
        "nodes": {key: nodes[key] for key in sorted(nodes)},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
```

- [ ] **Step 4: Run extraction tests and verify they pass**

Run:

```bash
python -m unittest tests.test_generate_popular_node_signatures.StaticExtractionTests -v
```

Expected: PASS.

- [ ] **Step 5: Commit static extraction**

Run:

```bash
git add tools/generate_popular_node_signatures.py tests/test_generate_popular_node_signatures.py
git commit -m "Add popular node signature extractor"
```

Expected: commit succeeds.

## Task 4: Add Manager Metadata Ranking And Repository Fetching

**Files:**
- Modify: `tools/generate_popular_node_signatures.py`
- Modify: `tests/test_generate_popular_node_signatures.py`

- [ ] **Step 1: Add failing metadata tests**

Append these imports near the top of `tests/test_generate_popular_node_signatures.py`:

```python
from tools.generate_popular_node_signatures import (
    github_repo_url,
    normalise_manager_entries,
    rank_entries,
)
```

If this creates a duplicate import block, merge the imported names into the existing `from tools.generate_popular_node_signatures import` statement at the top of the file.

Append this test class above the `if __name__ == "__main__":` block:

```python
class MetadataRankingTests(unittest.TestCase):
    def test_github_repo_url_accepts_github_repository_links(self):
        self.assertEqual(
            "https://github.com/example/ComfyUI-Pack",
            github_repo_url("https://github.com/example/ComfyUI-Pack"),
        )
        self.assertEqual(
            "https://github.com/example/ComfyUI-Pack",
            github_repo_url("https://github.com/example/ComfyUI-Pack/blob/main/node.py"),
        )
        self.assertIsNone(github_repo_url("https://example.com/not-github"))

    def test_normalise_manager_entries_uses_reference_or_files(self):
        raw = {
            "custom_nodes": [
                {
                    "id": "pack-a",
                    "title": "Pack A",
                    "author": "Author A",
                    "reference": "https://github.com/example/pack-a",
                    "files": [],
                },
                {
                    "title": "Pack B",
                    "author": "Author B",
                    "reference": "https://example.com/not-github",
                    "files": ["https://github.com/example/pack-b"],
                },
            ]
        }

        entries = normalise_manager_entries(raw)

        self.assertEqual("pack-a", entries[0]["id"])
        self.assertEqual("https://github.com/example/pack-a", entries[0]["repository"])
        self.assertEqual("pack-b", entries[1]["id"])
        self.assertEqual("https://github.com/example/pack-b", entries[1]["repository"])

    def test_rank_entries_sorts_by_downloads_stars_then_manager_order(self):
        entries = [
            {"id": "third", "downloads": 1, "github_stars": 10, "manager_order": 0},
            {"id": "first", "downloads": 100, "github_stars": 0, "manager_order": 2},
            {"id": "second", "downloads": 100, "github_stars": 50, "manager_order": 1},
        ]

        ranked = rank_entries(entries, 3)

        self.assertEqual(["second", "first", "third"], [entry["id"] for entry in ranked])
        self.assertEqual([1, 2, 3], [entry["rank"] for entry in ranked])
```

- [ ] **Step 2: Run metadata tests and verify they fail**

Run:

```bash
python -m unittest tests.test_generate_popular_node_signatures.MetadataRankingTests -v
```

Expected: FAIL because metadata normalization and ranking functions are not defined.

- [ ] **Step 3: Implement metadata normalization and ranking**

Append these functions to `tools/generate_popular_node_signatures.py` before `extract_repo_signatures()`:

```python
def github_repo_url(value):
    if not value:
        return None
    parsed = urlparse(str(value))
    if parsed.netloc.lower() != "github.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    return f"https://github.com/{owner}/{repo}"


def _slug(value):
    text = str(value or "").strip().lower()
    chars = []
    last_dash = False
    for ch in text:
        ok = ch.isalnum()
        if ok:
            chars.append(ch)
            last_dash = False
        elif not last_dash:
            chars.append("-")
            last_dash = True
    return "".join(chars).strip("-") or "unnamed-pack"


def normalise_manager_entries(raw):
    entries = []
    for index, item in enumerate((raw or {}).get("custom_nodes") or []):
        candidates = [item.get("reference")]
        candidates.extend(item.get("files") or [])
        repository = None
        for candidate in candidates:
            repository = github_repo_url(candidate)
            if repository:
                break
        if not repository:
            continue
        pack_id = str(item.get("id") or _slug(item.get("title") or repository.rsplit("/", 1)[-1]))
        entries.append(
            {
                "id": pack_id,
                "title": str(item.get("title") or pack_id),
                "author": str(item.get("author") or ""),
                "repository": repository,
                "manager_order": index,
                "downloads": int(item.get("downloads") or 0),
                "github_stars": int(item.get("github_stars") or 0),
                "search_ranking": float(item.get("search_ranking") or 0),
            }
        )
    return entries


def rank_entries(entries, limit):
    unique = {}
    for entry in entries:
        repository = entry.get("repository")
        if not repository:
            continue
        previous = unique.get(repository)
        if previous is None:
            unique[repository] = dict(entry)
            continue
        current_key = (
            int(entry.get("downloads") or 0),
            int(entry.get("github_stars") or 0),
            float(entry.get("search_ranking") or 0),
            -int(entry.get("manager_order") or 0),
        )
        previous_key = (
            int(previous.get("downloads") or 0),
            int(previous.get("github_stars") or 0),
            float(previous.get("search_ranking") or 0),
            -int(previous.get("manager_order") or 0),
        )
        if current_key > previous_key:
            unique[repository] = dict(entry)

    ranked = sorted(
        unique.values(),
        key=lambda entry: (
            -int(entry.get("downloads") or 0),
            -int(entry.get("github_stars") or 0),
            -float(entry.get("search_ranking") or 0),
            int(entry.get("manager_order") or 0),
            str(entry.get("id") or ""),
        ),
    )
    for index, entry in enumerate(ranked[:limit], start=1):
        entry["rank"] = index
    return ranked[:limit]
```

- [ ] **Step 4: Implement fetch, cache, and CLI functions**

Append these functions to `tools/generate_popular_node_signatures.py` after `write_artifact()`:

```python
def fetch_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "ComfyUI-UTFCN signature generator"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _repo_cache_name(repository):
    parsed = urlparse(repository)
    parts = [part for part in parsed.path.split("/") if part]
    return "__".join(parts[:2])


def fetch_repository(repository, cache_dir):
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / _repo_cache_name(repository)
    if target.exists():
        return target
    tmp = Path(tempfile.mkdtemp(prefix="utfcn-repo-", dir=str(cache_dir)))
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", repository, str(tmp)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        shutil.move(str(tmp), str(target))
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
    return target


def build_artifact(limit, out_path, cache_dir, manager_url=MANAGER_LIST_URL):
    manager_raw = fetch_json(manager_url)
    entries = rank_entries(normalise_manager_entries(manager_raw), limit)
    packs = {}
    nodes = {}

    for entry in entries:
        pack_meta = {
            "id": entry["id"],
            "title": entry["title"],
            "repository": entry["repository"],
            "rank": entry["rank"],
        }
        try:
            repo_dir = fetch_repository(entry["repository"], cache_dir)
            extracted = extract_repo_signatures(repo_dir, pack_meta)
        except Exception as exc:
            extracted = {
                "pack": {
                    "id": entry["id"],
                    "title": entry["title"],
                    "repository": entry["repository"],
                    "rank": entry["rank"],
                    "status": f"fetch_or_extract_failed: {exc.__class__.__name__}",
                    "node_count": 0,
                },
                "nodes": {},
            }
        packs[entry["id"]] = extracted["pack"]
        nodes.update(extracted["nodes"])

    write_artifact(
        out_path,
        sources={
            "manager_url": manager_url,
            "limit": limit,
            "ranked_entries": len(entries),
        },
        packs=packs,
        nodes=nodes,
    )
    return {"packs": packs, "nodes": nodes}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--out", type=Path, default=Path("popular_node_signatures.json"))
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/utfcn-popular-node-repos"))
    parser.add_argument("--manager-url", default=MANAGER_LIST_URL)
    args = parser.parse_args(argv)

    result = build_artifact(args.limit, args.out, args.cache_dir, args.manager_url)
    print(
        f"[UTFCN] wrote {args.out} with {len(result['packs'])} pack(s) "
        f"and {len(result['nodes'])} node signature(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run generator tests**

Run:

```bash
python -m unittest tests.test_generate_popular_node_signatures -v
```

Expected: PASS.

- [ ] **Step 6: Run a syntax check on the generator**

Run:

```bash
python -m py_compile tools/generate_popular_node_signatures.py
```

Expected: no output and exit code 0.

- [ ] **Step 7: Commit metadata and fetching support**

Run:

```bash
git add tools/generate_popular_node_signatures.py tests/test_generate_popular_node_signatures.py
git commit -m "Add popular node metadata ranking"
```

Expected: commit succeeds.

## Task 5: Generate And Load A Small Initial Artifact

**Files:**
- Create: `popular_node_signatures.json`
- Modify: `tests/test_utfcn_core_generated.py`

- [ ] **Step 1: Generate a small artifact sample**

Run:

```bash
python tools/generate_popular_node_signatures.py --limit 10 --out popular_node_signatures.json --cache-dir /tmp/utfcn-popular-node-repos
```

Expected: command prints `[UTFCN] wrote popular_node_signatures.json with 10 pack(s)` and exits 0. The node signature count may be 0 if the first 10 Manager entries are dynamic or fail static extraction.

- [ ] **Step 2: Validate the generated JSON shape**

Run:

```bash
python -m json.tool popular_node_signatures.json >/tmp/utfcn-popular-node-signatures.json
```

Expected: no output and exit code 0.

- [ ] **Step 3: Add a regression test that the repository artifact loads**

Append this test to `GeneratedSignatureLoaderTests` in `tests/test_utfcn_core_generated.py`:

```python
    def test_repository_artifact_loads_when_present(self):
        repo_dir = Path(__file__).resolve().parents[1]
        generated = utfcn_core.load_generated_signatures(str(repo_dir))

        self.assertIn("sigs", generated)
        self.assertIn("meta", generated)
        self.assertIn("by_out", generated)
```

- [ ] **Step 4: Run backend tests with the artifact present**

Run:

```bash
python -m unittest tests.test_utfcn_core_generated -v
```

Expected: PASS.

- [ ] **Step 5: Commit the initial artifact**

Run:

```bash
git add popular_node_signatures.json tests/test_utfcn_core_generated.py
git commit -m "Add initial popular node signature artifact"
```

Expected: commit succeeds.

## Task 6: Document Generated Signatures

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update README behavior documentation**

In `README.md`, add this paragraph after the "Works on uninstalled (\"missing\") nodes" section:

```markdown
### Popular missing-node signatures

UTFCN ships a generated `popular_node_signatures.json` artifact built from
ComfyUI-Manager / Registry metadata and static scans of public GitHub repos.
The file helps match common missing nodes by their real node signatures even
when the original pack is not installed. It is loaded locally at ComfyUI startup;
UTFCN does not contact GitHub, ComfyUI-Manager, or the Registry while you use the
editor.
```

In the "How it decides what's equivalent" section, replace the uninstalled-node paragraph with:

```markdown
For an **uninstalled** node, UTFCN tries curated rules by name first, then any
bundled generated signature for that node type, then the serialized link
signature preserved in the workflow. Generated exact signatures can produce
verified exact matches, but name-only metadata never can; loose structural
matches remain suggestions.
```

Add this subsection before "Install":

````markdown
## Refreshing the generated popular-node artifact

Maintainers can refresh the bundled signature artifact with:

```bash
python tools/generate_popular_node_signatures.py --limit 1000 --out popular_node_signatures.json --cache-dir /tmp/utfcn-popular-node-repos
```

The generator uses only Python's standard library plus `git`. It parses custom
node repositories statically with `ast`; it does not import or execute the
downloaded node code. Repositories with dynamic signatures are skipped until a
parser case exists for them.
````

- [ ] **Step 2: Run documentation sanity checks**

Run:

```bash
python -m json.tool popular_node_signatures.json >/tmp/utfcn-popular-node-signatures.json
python -m py_compile utfcn_core.py __init__.py tools/generate_popular_node_signatures.py
python -m unittest tests.test_utfcn_core_generated tests.test_generate_popular_node_signatures -v
```

Expected: all commands exit 0, and unittest reports PASS for every test.

- [ ] **Step 3: Commit documentation**

Run:

```bash
git add README.md
git commit -m "Document popular node signatures"
```

Expected: commit succeeds.

## Task 7: Expand Artifact Toward The Ranked Limit

**Files:**
- Modify: `popular_node_signatures.json`

- [ ] **Step 1: Generate the larger artifact**

Run:

```bash
python tools/generate_popular_node_signatures.py --limit 1000 --out popular_node_signatures.json --cache-dir /tmp/utfcn-popular-node-repos
```

Expected: command exits 0 and prints a pack count up to 1000 plus a node signature count.

- [ ] **Step 2: Inspect artifact size and top-level counts**

Run:

```bash
python - <<'PY'
import json
from pathlib import Path

path = Path("popular_node_signatures.json")
data = json.loads(path.read_text(encoding="utf-8"))
print("packs", len(data.get("packs", {})))
print("nodes", len(data.get("nodes", {})))
print("bytes", path.stat().st_size)
PY
```

Expected: `packs` is greater than 0, `nodes` is greater than 0, and `bytes` is a positive integer.

- [ ] **Step 3: Run full verification**

Run:

```bash
python -m json.tool popular_node_signatures.json >/tmp/utfcn-popular-node-signatures.json
python -m py_compile utfcn_core.py __init__.py tools/generate_popular_node_signatures.py
python -m unittest tests.test_utfcn_core_generated tests.test_generate_popular_node_signatures -v
```

Expected: all commands exit 0, and unittest reports PASS for every test.

- [ ] **Step 4: Commit expanded artifact**

Run:

```bash
git add popular_node_signatures.json
git commit -m "Expand popular node signature artifact"
```

Expected: commit succeeds.

## Task 8: Final Integration Review

**Files:**
- Review: `utfcn_core.py`
- Review: `__init__.py`
- Review: `tools/generate_popular_node_signatures.py`
- Review: `tests/test_utfcn_core_generated.py`
- Review: `tests/test_generate_popular_node_signatures.py`
- Review: `README.md`
- Review: `popular_node_signatures.json`

- [ ] **Step 1: Check worktree status**

Run:

```bash
git status --short
```

Expected: no output.

- [ ] **Step 2: Review recent commits**

Run:

```bash
git log --oneline -8
```

Expected: shows commits for loader, matching, extractor, metadata ranking, initial artifact, docs, and expanded artifact. If the implementation used fewer commits because initial and expanded artifact were combined, the log still shows a coherent sequence of completed feature commits.

- [ ] **Step 3: Final verification**

Run:

```bash
python -m json.tool mappings.json >/tmp/utfcn-mappings.json
python -m json.tool user_mappings.json >/tmp/utfcn-user-mappings.json
python -m json.tool popular_node_signatures.json >/tmp/utfcn-popular-node-signatures.json
python -m py_compile utfcn_core.py __init__.py tools/generate_popular_node_signatures.py
python -m unittest tests.test_utfcn_core_generated tests.test_generate_popular_node_signatures -v
```

Expected: all commands exit 0, and unittest reports PASS for every test.

- [ ] **Step 4: Summarize implementation results**

Prepare a concise final summary with:

```text
Implemented:
- Generated signature artifact loader and matching integration.
- Static generator for Manager/GitHub-sourced node signatures.
- Backend and generator tests.
- README documentation.

Verified:
- JSON validation for shipped JSON files.
- Python compile checks.
- unittest suite.

Artifact:
- packs: <actual pack count>
- nodes: <actual node signature count>
```
