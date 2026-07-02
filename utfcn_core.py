"""
UTFCN — Use The F***ing Core Nodes.  Backend analysis engine.

This module runs inside the ComfyUI server process, so it can see the live node
registry (``nodes.NODE_CLASS_MAPPINGS``) with every node's real INPUT_TYPES /
RETURN_TYPES and its source module.  That's exactly the ground truth needed to
answer the only interesting question here:

    "This custom node — is there a CORE node (or, failing that, a node from a
     DIFFERENT installed pack) that does the same job, and could I swap it in
     without breaking the graph?"

We answer it in three tiers, from most to least trustworthy:

    curated   a hand-written rule in mappings.json / user_mappings.json.
              Carries explicit input/widget/output name remaps.  Verified.
    exact     the candidate's signature (input name→type map + ordered output
              types) is IDENTICAL to the source's.  Safe to remap by name.
              Verified.
    partial   the candidate can structurally accept every input the source has
              and provides every output type the source has, but names / extra
              slots differ.  A *suggestion* only — never auto-applied.

The frontend consumes the result: `verified` candidates power auto-replace,
`partial` ones are shown for the user to confirm.
"""

import json
import os
from collections import Counter, defaultdict

# Top-level python modules we consider "core" (shipped with ComfyUI itself).
# server.py exposes each class's origin as RELATIVE_PYTHON_MODULE (default "nodes").
CORE_TOPLEVEL = ("nodes", "comfy_extras", "comfy_api_nodes", "comfy_api")

# Widget-ish primitive types.  These are values the user types, not graph links,
# so they matter for widget-value transfer but not for link compatibility.
WIDGET_TYPES = frozenset({"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"})


def _module_of(cls):
    return getattr(cls, "RELATIVE_PYTHON_MODULE", "nodes") or "nodes"


def _source_kind(module):
    top = module.split(".", 1)[0]
    if top == "custom_nodes":
        return "custom"
    if top in CORE_TOPLEVEL:
        return "core"
    return "core"  # anything unexpected is treated as first-party


def _pack_of(module):
    parts = module.split(".")
    if parts[0] == "custom_nodes" and len(parts) > 1:
        return parts[1]
    return parts[0]


def _spec_type(spec):
    """Reduce an INPUT_TYPES spec (``("IMAGE",)`` / ``(["a","b"], {...})``) to a type string."""
    t = spec[0] if isinstance(spec, (list, tuple)) and spec else spec
    if isinstance(t, list):          # a list of choices == a combo/dropdown widget
        return "COMBO"
    return str(t)


def _signature(cls):
    """Extract a comparable signature: inputs {name->type}, required names, ordered output types."""
    try:
        it = cls.INPUT_TYPES()
    except Exception:
        it = {}
    inputs, required = {}, set()
    for section in ("required", "optional"):
        for name, spec in (it.get(section) or {}).items():
            try:
                inputs[name] = _spec_type(spec)
            except Exception:
                inputs[name] = "*"
            if section == "required":
                required.add(name)
    outputs = [str(t) for t in (getattr(cls, "RETURN_TYPES", ()) or ())]
    out_names = [str(n) for n in (getattr(cls, "RETURN_NAMES", ()) or [])]
    return {"inputs": inputs, "required": required, "outputs": outputs, "output_names": out_names}


def _first_output_type(sig):
    return sig["outputs"][0] if sig["outputs"] else ""


def _is_exact(a, b):
    """Identical enough that a name-based remap is trivially safe."""
    return a["inputs"] == b["inputs"] and a["outputs"] == b["outputs"]


def _feasible(src, cand):
    """Can `cand` structurally stand in for `src`?  (accepts all its inputs, provides all its outputs)"""
    src_in = Counter(src["inputs"].values())
    cand_in = Counter(cand["inputs"].values())
    in_ok = not (src_in - cand_in)                       # every source input type available on candidate
    src_out = Counter(src["outputs"])
    cand_out = Counter(cand["outputs"])
    out_ok = not (src_out - cand_out)                    # candidate provides every source output type
    return in_ok and out_ok


def _score(src, cand):
    """Signature-overlap score in [0,1]; higher = more alike.  Rewards matching names too."""
    src_in, cand_in = Counter(src["inputs"].values()), Counter(cand["inputs"].values())
    src_out, cand_out = Counter(src["outputs"]), Counter(cand["outputs"])
    overlap = sum((src_in & cand_in).values()) + sum((src_out & cand_out).values())
    total = sum(src_in.values()) + sum(src_out.values())
    base = overlap / total if total else 0.0
    # small bonus for shared input names — a strong signal of a deliberate re-implementation
    shared_names = len(set(src["inputs"]) & set(cand["inputs"]))
    name_bonus = 0.15 * (shared_names / len(src["inputs"])) if src["inputs"] else 0.0
    return min(1.0, base + name_bonus)


# score below which a partial match isn't worth surfacing
_PARTIAL_THRESHOLD = 0.5
# max candidates returned per source node
_MAX_CANDIDATES = 6

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


def _normalise_rules(raw):
    """Accept both {source: {...single...}} and {source: [ {...}, {...} ]} shapes."""
    out = {}
    for src, val in (raw.get("rules") or {}).items():
        targets = val if isinstance(val, list) else [val]
        out[src] = [t for t in targets if isinstance(t, dict) and t.get("to")]
    return out


def load_rules(base_dir):
    """Load builtin mappings.json, then deep-merge user_mappings.json on top (user wins per source)."""
    merged = {}
    for fname in ("mappings.json", "user_mappings.json"):
        path = os.path.join(base_dir, fname)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                merged.update(_normalise_rules(json.load(f)))
        except Exception as e:  # a broken user file must never take the server down
            print(f"[UTFCN] failed to read {fname}: {e}")
    return merged


def build_context(rules, generated=None):
    """
    Snapshot the live node registry once (signatures + source of every node).

    Returned context is reused by build_index() (the /utfcn/scan payload) and by
    match() (per-workflow matching of UNINSTALLED nodes), so the expensive walk
    only happens on refresh.

    `rules` is the merged curated mapping: {sourceType: [ {to, note, inputs, widgets, outputs}, ... ]}.
    """
    import nodes  # imported here so the module stays importable outside ComfyUI

    classes = nodes.NODE_CLASS_MAPPINGS
    displays = getattr(nodes, "NODE_DISPLAY_NAME_MAPPINGS", {})

    sources, sigs = {}, {}
    for name, cls in classes.items():
        module = _module_of(cls)
        sources[name] = {"source": _source_kind(module), "pack": _pack_of(module), "display": displays.get(name, name)}
        sigs[name] = _signature(cls)

    # Bucket every potential *target* by its first output type so a source only
    # gets compared against nodes that could plausibly feed the same downstream.
    by_out = defaultdict(list)
    for name in classes:
        by_out[_first_output_type(sigs[name])].append(name)

    return {
        "sources": sources,
        "sigs": sigs,
        "by_out": by_out,
        "rules": rules,
        "generated": generated or _empty_generated_signatures(),
    }


def _candidates_for(src_name, src_sig, src_pack, ctx):
    """
    Rank replacement candidates for one source node.

    `src_sig` may be None (an uninstalled node we know only by name) — then only
    curated rules apply. If a signature is given (installed node, or a missing
    node's serialized signature), exact/partial tiers are added too.
    `src_pack` is None for uninstalled/unknown sources (skips same-pack exclusion).
    """
    sources, sigs, by_out, rules = ctx["sources"], ctx["sigs"], ctx["by_out"], ctx["rules"]
    found, seen = [], set()

    # --- tier 1: curated rules (ordered preference; core-first is the author's job) ---
    for rule in rules.get(src_name, []):
        to = rule.get("to")
        if not to or to == src_name or to not in sources or to in seen:
            continue
        seen.add(to)
        found.append(_candidate(to, sources, "curated", 1.0, rule))

    # --- tiers 2 & 3: signature matching within the same output bucket ---
    if src_sig is not None:
        ranked = []
        for cand_name in by_out.get(_first_output_type(src_sig), []):
            if cand_name in seen or cand_name == src_name:
                continue
            cand_meta = sources[cand_name]
            # target must be core, or a DIFFERENT installed pack (fallback-to-available)
            if cand_meta["source"] == "custom" and src_pack is not None and cand_meta["pack"] == src_pack:
                continue
            cand_sig = sigs[cand_name]
            if not _feasible(src_sig, cand_sig):
                continue
            if _is_exact(src_sig, cand_sig):
                ranked.append((cand_name, "exact", 1.0))
            else:
                sc = _score(src_sig, cand_sig)
                if sc >= _PARTIAL_THRESHOLD:
                    ranked.append((cand_name, "partial", sc))

        ranked.sort(key=lambda r: (
            0 if sources[r[0]]["source"] == "core" else 1,   # core before pack
            0 if r[1] == "exact" else 1,                      # exact before partial
            -r[2],                                            # higher score first
        ))
        for cand_name, tier, sc in ranked:
            if cand_name in seen:
                continue
            seen.add(cand_name)
            found.append(_candidate(cand_name, sources, tier, sc, None))

    return found[:_MAX_CANDIDATES]


def build_index(ctx):
    """
    Build the /utfcn/scan payload from a context.

    Covers INSTALLED custom nodes (curated + signature tiers) AND uninstalled
    source types that a curated rule targets an installed node for — so a rule
    still fires on a node whose pack you never installed.

    Returns { "sources": {...}, "candidates": {srcType: [candidate,...]}, "stats": {...} }.
    """
    sources = ctx["sources"]
    candidates = {}

    for src_name, meta in sources.items():
        if meta["source"] != "custom":
            continue
        found = _candidates_for(src_name, ctx["sigs"][src_name], meta["pack"], ctx)
        if found:
            candidates[src_name] = found

    # curated rules whose SOURCE isn't installed (the "replace a missing node
    # without installing its pack" case) — no signature, so curated-only.
    uninstalled = 0
    for src_name in ctx["rules"]:
        if src_name in sources or src_name in candidates:
            continue
        found = _candidates_for(src_name, None, None, ctx)
        if found:
            candidates[src_name] = found
            uninstalled += 1

    stats = {
        "nodes": len(sources),
        "custom": sum(1 for m in sources.values() if m["source"] == "custom"),
        "replaceable": len(candidates),
        "verified": sum(1 for cl in candidates.values() if any(c["verified"] for c in cl)),
        "uninstalled": uninstalled,
    }
    return {"sources": sources, "candidates": candidates, "stats": stats}


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


def _candidate(to, sources, tier, score, rule):
    meta = sources[to]
    cand = {
        "to": to,
        "to_display": meta["display"],
        "source": meta["source"],          # "core" | "custom"
        "pack": meta["pack"],
        "tier": tier,                      # "curated" | "exact" | "partial"
        "verified": tier in ("curated", "exact"),
        "score": round(float(score), 3),
    }
    if rule:
        # explicit name remaps travel to the frontend so the swap is exact
        for key in ("inputs", "widgets", "outputs"):
            if isinstance(rule.get(key), dict):
                cand[key] = rule[key]
        if rule.get("note"):
            cand["note"] = rule["note"]
    return cand
