#!/usr/bin/env python3
"""Generate UTFCN's popular_node_signatures.json artifact."""

import ast
import json
import os
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
MANAGER_LIST_URL = "https://raw.githubusercontent.com/ltdrdata/ComfyUI-Manager/main/custom-node-list.json"
REGISTRY_NODES_URL = "https://api.comfy.org/nodes"


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
        name = stmt.targets[0].id
        try:
            env[name] = _literal(stmt.value, env)
        except UnsupportedStaticExpression:
            env.pop(name, None)
            continue
    return env


def normalise_input_spec(spec):
    first = spec[0] if isinstance(spec, (list, tuple)) and spec else spec
    if isinstance(first, list):
        return "COMBO"
    return str(first)


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
        if not any(
            isinstance(target, ast.Name) and target.id == "NODE_CLASS_MAPPINGS"
            for target in stmt.targets
        ):
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
        if not any(
            isinstance(target, ast.Name) and target.id == "NODE_DISPLAY_NAME_MAPPINGS"
            for target in stmt.targets
        ):
            continue
        try:
            value = _literal(stmt.value, env)
        except UnsupportedStaticExpression:
            return {}
        if isinstance(value, dict):
            return {str(k): str(v) for k, v in value.items()}
    return {}


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
        dirs[:] = [dirname for dirname in dirs if dirname not in skipped]
        for filename in files:
            if filename.endswith(".py"):
                yield Path(root, filename)


def _parse_python_file(path):
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except UnicodeDecodeError:
        try:
            return ast.parse(path.read_text(encoding="utf-8", errors="ignore"), filename=str(path))
        except SyntaxError:
            return None
    except SyntaxError:
        return None


def extract_repo_signatures(repo_dir, pack_meta):
    nodes = {}
    for path in sorted(_python_files(repo_dir)):
        tree = _parse_python_file(path)
        if tree is None:
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


def _sorted_json_value(value):
    if isinstance(value, dict):
        return {key: _sorted_json_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_sorted_json_value(item) for item in value]
    return value


def write_artifact(path, sources, packs, nodes):
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "sources": _sorted_json_value(sources),
        "packs": _sorted_json_value(packs),
        "nodes": _sorted_json_value(nodes),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
