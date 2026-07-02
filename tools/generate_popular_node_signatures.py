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


_MISSING = object()
_INVALID = object()


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


def _target_names(target):
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.List, ast.Tuple)):
        names = set()
        for item in target.elts:
            names.update(_target_names(item))
        return names
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if isinstance(target, (ast.Attribute, ast.Subscript)):
        return _target_names(target.value)
    return set()


def _assignment_target_names(stmt):
    if isinstance(stmt, ast.Assign):
        names = set()
        for target in stmt.targets:
            names.update(_target_names(target))
        return names
    if isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        return _target_names(stmt.target)
    if isinstance(stmt, (ast.For, ast.AsyncFor)):
        return _target_names(stmt.target)
    return set()


def _assigned_names_in_control_flow(stmt):
    names = set()

    class AssignmentVisitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            return None

        def visit_AsyncFunctionDef(self, node):
            return None

        def visit_ClassDef(self, node):
            return None

        def visit_Assign(self, node):
            names.update(_assignment_target_names(node))

        def visit_AnnAssign(self, node):
            names.update(_assignment_target_names(node))

        def visit_AugAssign(self, node):
            names.update(_assignment_target_names(node))

        def visit_For(self, node):
            names.update(_assignment_target_names(node))
            self.generic_visit(node)

        def visit_AsyncFor(self, node):
            names.update(_assignment_target_names(node))
            self.generic_visit(node)

    AssignmentVisitor().visit(stmt)
    return names


def _collect_module_env(tree):
    env = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            names = _assignment_target_names(stmt)
            if len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                name = stmt.targets[0].id
                try:
                    env[name] = _literal(stmt.value, env)
                except UnsupportedStaticExpression:
                    env.pop(name, None)
            else:
                for name in names:
                    env.pop(name, None)
            continue
        if isinstance(stmt, ast.AnnAssign):
            names = _assignment_target_names(stmt)
            if stmt.value is None:
                continue
            if isinstance(stmt.target, ast.Name):
                name = stmt.target.id
                try:
                    env[name] = _literal(stmt.value, env)
                except UnsupportedStaticExpression:
                    env.pop(name, None)
            else:
                for name in names:
                    env.pop(name, None)
            continue
        if isinstance(stmt, ast.AugAssign):
            for name in _assignment_target_names(stmt):
                env.pop(name, None)
            continue
        if isinstance(stmt, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try)):
            for name in _assigned_names_in_control_flow(stmt):
                env.pop(name, None)
    return env


def normalise_input_spec(spec):
    first = spec[0] if isinstance(spec, (list, tuple)) and spec else spec
    if isinstance(first, list):
        return "COMBO"
    return str(first)


def _class_defs(tree):
    return {node.name: node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}


def _class_attr(cls, name, env):
    value = _MISSING
    for stmt in cls.body:
        if isinstance(stmt, ast.Assign):
            if not any(isinstance(target, ast.Name) and target.id == name for target in stmt.targets):
                continue
            if len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                try:
                    value = _literal(stmt.value, env)
                except UnsupportedStaticExpression:
                    value = _INVALID
            else:
                value = _INVALID
            continue
        if isinstance(stmt, ast.AnnAssign):
            if not isinstance(stmt.target, ast.Name) or stmt.target.id != name:
                continue
            if stmt.value is None:
                continue
            try:
                value = _literal(stmt.value, env)
            except UnsupportedStaticExpression:
                value = _INVALID
            continue
        if isinstance(stmt, ast.AugAssign):
            if isinstance(stmt.target, ast.Name) and stmt.target.id == name:
                value = _INVALID
            continue
        if isinstance(stmt, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try)):
            if name in _assigned_names_in_control_flow(stmt):
                value = _INVALID
    if value in (_MISSING, _INVALID):
        return None
    return value


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
