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


def build_index(rules):
    """
    Build the full equivalence index for the current node registry.

    `rules` is the merged curated mapping: {sourceType: [ {to, note, inputs, widgets, outputs}, ... ]}.

    Returns:
        {
          "sources":    {type: {"source": "core"|"custom", "pack": str, "display": str}},
          "candidates": {customType: [candidate, ...]},   # only custom nodes with >=1 candidate
          "stats":      {...},
        }
    """
    import nodes  # imported here so the module stays importable outside ComfyUI

    classes = nodes.NODE_CLASS_MAPPINGS
    displays = getattr(nodes, "NODE_DISPLAY_NAME_MAPPINGS", {})

    sources, sigs = {}, {}
    for name, cls in classes.items():
        module = _module_of(cls)
        kind = _source_kind(module)
        sources[name] = {"source": kind, "pack": _pack_of(module), "display": displays.get(name, name)}
        sigs[name] = _signature(cls)

    # Bucket every potential *target* by its first output type so a source only
    # gets compared against nodes that could plausibly feed the same downstream.
    by_out = defaultdict(list)
    for name in classes:
        by_out[_first_output_type(sigs[name])].append(name)

    candidates = {}
    verified_count = 0
    for src_name, meta in sources.items():
        if meta["source"] != "custom":
            continue
        src_sig = sigs[src_name]
        src_pack = meta["pack"]
        found, seen = [], set()

        # --- tier 1: curated rules (ordered preference; core-first is the author's job) ---
        for rule in rules.get(src_name, []):
            to = rule.get("to")
            if not to or to == src_name or to not in classes or to in seen:
                continue
            seen.add(to)
            found.append(_candidate(to, sources, "curated", 1.0, rule))

        # --- tiers 2 & 3: signature matching within the same output bucket ---
        bucket = by_out.get(_first_output_type(src_sig), [])
        ranked = []
        for cand_name in bucket:
            if cand_name in seen or cand_name == src_name:
                continue
            cand_meta = sources[cand_name]
            # target must be core, or a DIFFERENT installed pack (fallback-to-available)
            if cand_meta["source"] == "custom" and cand_meta["pack"] == src_pack:
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

        # order: core before pack; exact before partial; higher score first
        ranked.sort(key=lambda r: (
            0 if sources[r[0]]["source"] == "core" else 1,
            0 if r[1] == "exact" else 1,
            -r[2],
        ))
        for cand_name, tier, sc in ranked:
            if cand_name in seen:
                continue
            seen.add(cand_name)
            found.append(_candidate(cand_name, sources, tier, sc, None))

        if found:
            candidates[src_name] = found[:_MAX_CANDIDATES]
            if any(c["verified"] for c in candidates[src_name]):
                verified_count += 1

    stats = {
        "nodes": len(sources),
        "custom": sum(1 for m in sources.values() if m["source"] == "custom"),
        "replaceable": len(candidates),
        "verified": verified_count,
    }
    return {"sources": sources, "candidates": candidates, "stats": stats}


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
