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
_MUTATING_METHODS = {
    "add",
    "append",
    "clear",
    "discard",
    "extend",
    "insert",
    "pop",
    "popitem",
    "remove",
    "reverse",
    "setdefault",
    "sort",
    "update",
}
_CONTROL_FLOW_TYPES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith, ast.Match)
if hasattr(ast, "TryStar"):
    _CONTROL_FLOW_TYPES += (ast.TryStar,)


def _literal(node, env, allow_mutable_env=True):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.List):
        return [_literal(item, env, allow_mutable_env=False) for item in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_literal(item, env, allow_mutable_env=False) for item in node.elts)
    if isinstance(node, ast.Dict):
        result = {}
        for key, value in zip(node.keys, node.values):
            if key is None:
                raise UnsupportedStaticExpression("dict unpacking is not supported")
            result[_literal(key, env, allow_mutable_env=False)] = _literal(value, env, allow_mutable_env=False)
        return result
    if isinstance(node, ast.Name) and node.id in env:
        value = env[node.id]
        if not allow_mutable_env and _is_mutable_static_value(value):
            raise UnsupportedStaticExpression(f"mutable env reference {node.id!r} is not supported")
        return value
    raise UnsupportedStaticExpression(type(node).__name__)


def _is_mutable_static_value(value):
    return isinstance(value, (dict, list, set))


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


def _root_name(node):
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    if isinstance(node, ast.Name):
        return node.id
    return None


def _attribute_target_base_names(target):
    if isinstance(target, ast.Attribute):
        name = _root_name(target.value)
        return {name} if name else set()
    if isinstance(target, ast.Subscript):
        return _attribute_target_base_names(target.value)
    if isinstance(target, (ast.List, ast.Tuple)):
        names = set()
        for item in target.elts:
            names.update(_attribute_target_base_names(item))
        return names
    if isinstance(target, ast.Starred):
        return _attribute_target_base_names(target.value)
    return set()


def _class_attribute_mutation_target_names(stmt):
    names = set()

    class AttributeMutationVisitor(ast.NodeVisitor):
        def _visit_function_definition_expressions(self, node):
            for decorator in node.decorator_list:
                self.visit(decorator)
            self.visit(node.args)
            if node.returns is not None:
                self.visit(node.returns)
            for type_param in getattr(node, "type_params", ()):
                self.visit(type_param)

        def visit_FunctionDef(self, node):
            self._visit_function_definition_expressions(node)

        def visit_AsyncFunctionDef(self, node):
            self._visit_function_definition_expressions(node)

        def visit_ClassDef(self, node):
            for decorator in node.decorator_list:
                self.visit(decorator)
            for base in node.bases:
                self.visit(base)
            for keyword in node.keywords:
                self.visit(keyword.value)
            for type_param in getattr(node, "type_params", ()):
                self.visit(type_param)
            for child in node.body:
                self.visit(child)

        def visit_Lambda(self, node):
            self.visit(node.args)

        def visit_Assign(self, node):
            for target in node.targets:
                names.update(_attribute_target_base_names(target))
            self.visit(node.value)

        def visit_AnnAssign(self, node):
            names.update(_attribute_target_base_names(node.target))
            if node.value is not None:
                self.visit(node.value)

        def visit_AugAssign(self, node):
            names.update(_attribute_target_base_names(node.target))
            self.visit(node.value)

        def visit_Delete(self, node):
            for target in node.targets:
                names.update(_attribute_target_base_names(target))

        def visit_Call(self, node):
            if isinstance(node.func, ast.Attribute) and node.func.attr in _MUTATING_METHODS:
                names.update(_attribute_target_base_names(node.func.value))
            self.generic_visit(node)

    AttributeMutationVisitor().visit(stmt)
    return names


def _pattern_bound_names(pattern):
    names = set()
    if isinstance(pattern, ast.MatchAs):
        if pattern.name:
            names.add(pattern.name)
        if pattern.pattern is not None:
            names.update(_pattern_bound_names(pattern.pattern))
    elif isinstance(pattern, ast.MatchStar):
        if pattern.name:
            names.add(pattern.name)
    elif isinstance(pattern, ast.MatchMapping):
        if pattern.rest:
            names.add(pattern.rest)
        for subpattern in pattern.patterns:
            names.update(_pattern_bound_names(subpattern))
    elif isinstance(pattern, ast.MatchSequence):
        for subpattern in pattern.patterns:
            names.update(_pattern_bound_names(subpattern))
    elif isinstance(pattern, ast.MatchClass):
        for subpattern in pattern.patterns:
            names.update(_pattern_bound_names(subpattern))
        for subpattern in pattern.kwd_patterns:
            names.update(_pattern_bound_names(subpattern))
    elif isinstance(pattern, ast.MatchOr):
        for subpattern in pattern.patterns:
            names.update(_pattern_bound_names(subpattern))
    return names


def _named_expr_target_names(node):
    names = set()

    class NamedExprVisitor(ast.NodeVisitor):
        def visit_FunctionDef(self, child):
            return None

        def visit_AsyncFunctionDef(self, child):
            return None

        def visit_ClassDef(self, child):
            return None

        def visit_Lambda(self, child):
            return None

        def visit_NamedExpr(self, child):
            names.update(_target_names(child.target))
            self.visit(child.value)

    NamedExprVisitor().visit(node)
    return names


def _bound_names(stmt):
    names = set()
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(stmt.name)
    elif hasattr(ast, "TypeAlias") and isinstance(stmt, ast.TypeAlias):
        names.update(_target_names(stmt.name))
    elif isinstance(stmt, ast.Import):
        for alias in stmt.names:
            names.add(alias.asname or alias.name.split(".", 1)[0])
    elif isinstance(stmt, ast.ImportFrom):
        for alias in stmt.names:
            if alias.name != "*":
                names.add(alias.asname or alias.name)
    elif isinstance(stmt, (ast.With, ast.AsyncWith)):
        for item in stmt.items:
            if item.optional_vars is not None:
                names.update(_target_names(item.optional_vars))
    elif isinstance(stmt, ast.Match):
        for case in stmt.cases:
            names.update(_pattern_bound_names(case.pattern))
    elif isinstance(stmt, ast.ExceptHandler):
        if stmt.name:
            names.add(stmt.name)
    names.update(_named_expr_target_names(stmt))
    return names


def _has_wildcard_import(stmt):
    return isinstance(stmt, ast.ImportFrom) and any(alias.name == "*" for alias in stmt.names)


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


def _delete_target_names(stmt):
    if not isinstance(stmt, ast.Delete):
        return set()
    names = set()
    for target in stmt.targets:
        names.update(_target_names(target))
    return names


def _mutating_call_target_names(stmt):
    names = set()

    class MutatingCallVisitor(ast.NodeVisitor):
        def _visit_function_definition_expressions(self, node):
            for decorator in node.decorator_list:
                self.visit(decorator)
            self.visit(node.args)
            if node.returns is not None:
                self.visit(node.returns)
            for type_param in getattr(node, "type_params", ()):
                self.visit(type_param)

        def visit_FunctionDef(self, node):
            self._visit_function_definition_expressions(node)

        def visit_AsyncFunctionDef(self, node):
            self._visit_function_definition_expressions(node)

        def visit_ClassDef(self, node):
            for decorator in node.decorator_list:
                self.visit(decorator)
            for base in node.bases:
                self.visit(base)
            for keyword in node.keywords:
                self.visit(keyword.value)
            for type_param in getattr(node, "type_params", ()):
                self.visit(type_param)
            for child in node.body:
                self.visit(child)

        def visit_Lambda(self, node):
            self.visit(node.args)

        def visit_Call(self, node):
            if isinstance(node.func, ast.Attribute) and node.func.attr in _MUTATING_METHODS:
                names.update(_target_names(node.func.value))
            self.generic_visit(node)

    MutatingCallVisitor().visit(stmt)
    return names


def _assigned_names_in_control_flow(stmt):
    names = _mutating_call_target_names(stmt)

    class AssignmentVisitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            names.add(node.name)
            return None

        def visit_AsyncFunctionDef(self, node):
            names.add(node.name)
            return None

        def visit_ClassDef(self, node):
            names.add(node.name)
            return None

        def visit_Import(self, node):
            names.update(_bound_names(node))

        def visit_ImportFrom(self, node):
            names.update(_bound_names(node))

        def visit_Assign(self, node):
            names.update(_assignment_target_names(node))

        def visit_AnnAssign(self, node):
            names.update(_assignment_target_names(node))

        def visit_AugAssign(self, node):
            names.update(_assignment_target_names(node))

        def visit_Delete(self, node):
            names.update(_delete_target_names(node))

        def visit_ExceptHandler(self, node):
            names.update(_bound_names(node))
            self.generic_visit(node)

        def visit_TypeAlias(self, node):
            names.update(_bound_names(node))

        def visit_Expr(self, node):
            names.update(_mutating_call_target_names(node))
            names.update(_named_expr_target_names(node))

        def visit_With(self, node):
            names.update(_bound_names(node))
            self.generic_visit(node)

        def visit_AsyncWith(self, node):
            names.update(_bound_names(node))
            self.generic_visit(node)

        def visit_NamedExpr(self, node):
            names.update(_target_names(node.target))
            self.visit(node.value)

        def visit_Match(self, node):
            names.update(_bound_names(node))
            self.generic_visit(node)

        def visit_For(self, node):
            names.update(_assignment_target_names(node))
            self.generic_visit(node)

        def visit_AsyncFor(self, node):
            names.update(_assignment_target_names(node))
            self.generic_visit(node)

    AssignmentVisitor().visit(stmt)
    return names


def _has_wildcard_import_in_control_flow(stmt):
    found = False

    class WildcardImportVisitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            return None

        def visit_AsyncFunctionDef(self, node):
            return None

        def visit_ClassDef(self, node):
            return None

        def visit_ImportFrom(self, node):
            nonlocal found
            if _has_wildcard_import(node):
                found = True

    WildcardImportVisitor().visit(stmt)
    return found


def _has_module_wildcard_import(tree):
    for stmt in tree.body:
        if _has_wildcard_import(stmt):
            return True
        if isinstance(stmt, _CONTROL_FLOW_TYPES):
            if _has_wildcard_import_in_control_flow(stmt):
                return True
    return False


def _invalidate_class_bindings(class_bindings, names):
    if class_bindings is None:
        return
    for name in names:
        class_bindings.pop(name, None)


def _apply_module_stmt_to_env(stmt, env, class_bindings=None):
    names = _mutating_call_target_names(stmt)
    _invalidate_class_bindings(class_bindings, names)
    for name in names:
        env.pop(name, None)
    if isinstance(stmt, ast.ClassDef):
        if class_bindings is not None:
            if stmt.decorator_list:
                class_bindings.pop(stmt.name, None)
            else:
                class_bindings[stmt.name] = (stmt, dict(env))
        env.pop(stmt.name, None)
        return
    if isinstance(stmt, ast.Assign):
        names = _assignment_target_names(stmt)
        _invalidate_class_bindings(class_bindings, names)
        if len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            name = stmt.targets[0].id
            if (
                isinstance(stmt.value, ast.Name)
                and stmt.value.id in env
                and _is_mutable_static_value(env[stmt.value.id])
            ):
                env.pop(stmt.value.id, None)
                env.pop(name, None)
                return
            try:
                env[name] = _literal(stmt.value, env)
            except UnsupportedStaticExpression:
                env.pop(name, None)
        else:
            for name in names:
                env.pop(name, None)
        return
    if isinstance(stmt, ast.AnnAssign):
        names = _assignment_target_names(stmt)
        _invalidate_class_bindings(class_bindings, names)
        if stmt.value is None:
            return
        if isinstance(stmt.target, ast.Name):
            name = stmt.target.id
            if (
                isinstance(stmt.value, ast.Name)
                and stmt.value.id in env
                and _is_mutable_static_value(env[stmt.value.id])
            ):
                env.pop(stmt.value.id, None)
                env.pop(name, None)
                return
            try:
                env[name] = _literal(stmt.value, env)
            except UnsupportedStaticExpression:
                env.pop(name, None)
        else:
            for name in names:
                env.pop(name, None)
        return
    if isinstance(stmt, ast.AugAssign):
        names = _assignment_target_names(stmt)
        _invalidate_class_bindings(class_bindings, names)
        for name in names:
            env.pop(name, None)
        return
    if isinstance(stmt, ast.Delete):
        names = _delete_target_names(stmt)
        _invalidate_class_bindings(class_bindings, names)
        for name in names:
            env.pop(name, None)
        return
    if isinstance(stmt, ast.Expr):
        names = _bound_names(stmt)
        _invalidate_class_bindings(class_bindings, names)
        for name in names:
            env.pop(name, None)
        return
    if isinstance(stmt, _CONTROL_FLOW_TYPES):
        if _has_wildcard_import_in_control_flow(stmt):
            env.clear()
            if class_bindings is not None:
                class_bindings.clear()
            return
        names = _assigned_names_in_control_flow(stmt)
        _invalidate_class_bindings(class_bindings, names)
        for name in names:
            env.pop(name, None)
        return
    if _has_wildcard_import(stmt):
        env.clear()
        if class_bindings is not None:
            class_bindings.clear()
        return
    names = _bound_names(stmt)
    _invalidate_class_bindings(class_bindings, names)
    for name in names:
        env.pop(name, None)


def _collect_module_env(tree, class_bindings=None):
    env = {}
    for stmt in tree.body:
        _apply_module_stmt_to_env(stmt, env, class_bindings)
    return env


def normalise_input_spec(spec):
    first = spec[0] if isinstance(spec, (list, tuple)) and spec else spec
    if isinstance(first, list):
        return "COMBO"
    return str(first)


def _class_defs(tree):
    return {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}


def _is_mutable_env_reference(node, env):
    return isinstance(node, ast.Name) and node.id in env and _is_mutable_static_value(env[node.id])


def _class_attr(cls, name, env):
    value = _MISSING
    aliases = set()
    for stmt in cls.body:
        mutating_targets = _mutating_call_target_names(stmt)
        if aliases.intersection(mutating_targets):
            value = _INVALID
        if name in mutating_targets:
            value = _INVALID
        if isinstance(stmt, ast.Assign):
            target_names = _assignment_target_names(stmt)
            if (
                len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and stmt.targets[0].id != name
                and isinstance(stmt.value, ast.Name)
                and (stmt.value.id == name or stmt.value.id in aliases)
            ):
                aliases.add(stmt.targets[0].id)
                continue
            if aliases.intersection(target_names):
                value = _INVALID
                aliases.difference_update(target_names)
            if name not in target_names:
                continue
            if len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                if _is_mutable_env_reference(stmt.value, env):
                    value = _INVALID
                else:
                    try:
                        value = _literal(stmt.value, env)
                    except UnsupportedStaticExpression:
                        value = _INVALID
            else:
                value = _INVALID
            continue
        if isinstance(stmt, ast.AnnAssign):
            target_names = _assignment_target_names(stmt)
            if (
                isinstance(stmt.target, ast.Name)
                and stmt.target.id != name
                and isinstance(stmt.value, ast.Name)
                and (stmt.value.id == name or stmt.value.id in aliases)
            ):
                aliases.add(stmt.target.id)
                continue
            if aliases.intersection(target_names):
                value = _INVALID
                aliases.difference_update(target_names)
            if name not in target_names:
                continue
            if isinstance(stmt.target, ast.Name) and stmt.value is None:
                continue
            if not isinstance(stmt.target, ast.Name):
                value = _INVALID
            else:
                if _is_mutable_env_reference(stmt.value, env):
                    value = _INVALID
                else:
                    try:
                        value = _literal(stmt.value, env)
                    except UnsupportedStaticExpression:
                        value = _INVALID
            continue
        if isinstance(stmt, ast.AugAssign):
            target_names = _assignment_target_names(stmt)
            if aliases.intersection(target_names):
                value = _INVALID
                aliases.difference_update(target_names)
            if name in target_names:
                value = _INVALID
            continue
        if isinstance(stmt, ast.Delete):
            target_names = _delete_target_names(stmt)
            if aliases.intersection(target_names):
                value = _INVALID
                aliases.difference_update(target_names)
            if name in target_names:
                value = _INVALID
            continue
        if isinstance(stmt, ast.Expr):
            mutating_targets = _mutating_call_target_names(stmt)
            if aliases.intersection(mutating_targets):
                value = _INVALID
            if name in mutating_targets:
                value = _INVALID
            if name in _bound_names(stmt):
                value = _INVALID
            continue
        if isinstance(stmt, _CONTROL_FLOW_TYPES):
            target_names = _assigned_names_in_control_flow(stmt)
            if aliases.intersection(target_names):
                value = _INVALID
            if name in target_names:
                value = _INVALID
            if _has_wildcard_import_in_control_flow(stmt):
                value = _INVALID
            continue
        if name in _bound_names(stmt):
            value = _INVALID
    if value is _MISSING:
        return _MISSING
    if value is _INVALID:
        return _INVALID
    return value


def _input_types(cls, env):
    value = _MISSING
    for stmt in cls.body:
        if "INPUT_TYPES" in _mutating_call_target_names(stmt):
            value = _INVALID
        if isinstance(stmt, ast.FunctionDef) and stmt.name == "INPUT_TYPES":
            if len(stmt.body) != 1 or not isinstance(stmt.body[0], ast.Return):
                value = _INVALID
                continue
            try:
                candidate = _literal(stmt.body[0].value, env)
            except UnsupportedStaticExpression:
                value = _INVALID
                continue
            value = candidate if isinstance(candidate, dict) else _INVALID
            continue
        if isinstance(stmt, ast.AsyncFunctionDef) and stmt.name == "INPUT_TYPES":
            value = _INVALID
            continue
        if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            if "INPUT_TYPES" in _assignment_target_names(stmt):
                value = _INVALID
            continue
        if isinstance(stmt, ast.Delete):
            if "INPUT_TYPES" in _delete_target_names(stmt):
                value = _INVALID
            continue
        if isinstance(stmt, ast.Expr):
            if "INPUT_TYPES" in _mutating_call_target_names(stmt):
                value = _INVALID
            if "INPUT_TYPES" in _bound_names(stmt):
                value = _INVALID
            continue
        if isinstance(stmt, _CONTROL_FLOW_TYPES):
            if "INPUT_TYPES" in _assigned_names_in_control_flow(stmt):
                value = _INVALID
            if _has_wildcard_import_in_control_flow(stmt):
                value = _INVALID
            continue
        if "INPUT_TYPES" in _bound_names(stmt):
            value = _INVALID
    if value in (_MISSING, _INVALID):
        return None
    return value


def _mapping_value_name(value):
    if isinstance(value, str):
        return value
    if isinstance(value, ast.Name):
        return value.id
    return None


def _name_is_assigned(stmt, name):
    return name in _assignment_target_names(stmt)


def _module_dict_entries(node, env, class_bindings, value_converter):
    if not isinstance(node, ast.Dict):
        raise UnsupportedStaticExpression(type(node).__name__)
    result = {}
    for key, value in zip(node.keys, node.values):
        if key is None:
            raise UnsupportedStaticExpression("dict unpacking is not supported")
        key_value = _literal(key, env)
        try:
            hash(key_value)
        except TypeError as exc:
            raise UnsupportedStaticExpression("unhashable dict key") from exc
        if key_value in result:
            raise UnsupportedStaticExpression("duplicate dict key")
        converted_value = value_converter(value, env, class_bindings)
        if converted_value is None:
            raise UnsupportedStaticExpression("unsupported dict value")
        result[key_value] = converted_value
    return result


def _class_alias_sources(value, class_aliases, class_bindings):
    if not isinstance(value, ast.Name):
        return set()
    if value.id in class_aliases:
        return set(class_aliases[value.id])
    if value.id in class_bindings:
        return {value.id}
    return set()


def _update_class_aliases(stmt, class_aliases, class_bindings):
    rebound_names = _assignment_target_names(stmt) | _delete_target_names(stmt) | _bound_names(stmt)
    for name in rebound_names:
        class_aliases.pop(name, None)

    if isinstance(stmt, ast.ClassDef):
        if stmt.name in class_bindings and not stmt.decorator_list:
            class_aliases[stmt.name] = {stmt.name}
        return

    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
        sources = _class_alias_sources(stmt.value, class_aliases, class_bindings)
        if sources:
            class_aliases[stmt.targets[0].id] = sources
    elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
        sources = _class_alias_sources(stmt.value, class_aliases, class_bindings)
        if sources:
            class_aliases[stmt.target.id] = sources


def _expanded_class_attribute_names(names, class_aliases):
    expanded = set(names)
    for name in names:
        expanded.update(class_aliases.get(name, ()))
    return expanded


def _final_module_dict(tree, name, value_converter, value_invalidated_by_names=None):
    value_invalidated_by_names = value_invalidated_by_names or (lambda _value, _names: False)
    value = _MISSING
    env = {}
    class_bindings = {}
    class_aliases = {}

    def advance_module_state(stmt):
        _apply_module_stmt_to_env(stmt, env, class_bindings)
        _update_class_aliases(stmt, class_aliases, class_bindings)

    for stmt in tree.body:
        class_attr_names = _expanded_class_attribute_names(
            _class_attribute_mutation_target_names(stmt),
            class_aliases,
        )
        if (
            value not in (_MISSING, _INVALID)
            and class_attr_names
            and value_invalidated_by_names(value, class_attr_names)
        ):
            value = _INVALID
        if name in _mutating_call_target_names(stmt):
            value = _INVALID
        if isinstance(stmt, ast.Assign):
            if not _name_is_assigned(stmt, name):
                if isinstance(stmt.value, ast.Name) and stmt.value.id == name:
                    value = _INVALID
                advance_module_state(stmt)
                continue
            if len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                try:
                    value = _module_dict_entries(stmt.value, env, class_bindings, value_converter)
                except UnsupportedStaticExpression:
                    value = _INVALID
            else:
                value = _INVALID
            advance_module_state(stmt)
            continue
        if isinstance(stmt, ast.AnnAssign):
            if not _name_is_assigned(stmt, name):
                if isinstance(stmt.value, ast.Name) and stmt.value.id == name:
                    value = _INVALID
                advance_module_state(stmt)
                continue
            if isinstance(stmt.target, ast.Name) and stmt.value is not None:
                try:
                    value = _module_dict_entries(stmt.value, env, class_bindings, value_converter)
                except UnsupportedStaticExpression:
                    value = _INVALID
            else:
                value = _INVALID
            advance_module_state(stmt)
            continue
        if isinstance(stmt, ast.AugAssign):
            if _name_is_assigned(stmt, name):
                value = _INVALID
            advance_module_state(stmt)
            continue
        if isinstance(stmt, ast.Delete):
            if name in _delete_target_names(stmt):
                value = _INVALID
            advance_module_state(stmt)
            continue
        if isinstance(stmt, ast.Expr):
            if name in _mutating_call_target_names(stmt):
                value = _INVALID
            if name in _bound_names(stmt):
                value = _INVALID
            advance_module_state(stmt)
            continue
        if isinstance(stmt, _CONTROL_FLOW_TYPES):
            if name in _assigned_names_in_control_flow(stmt):
                value = _INVALID
            if _has_wildcard_import_in_control_flow(stmt):
                value = _INVALID
            advance_module_state(stmt)
            continue
        if _has_wildcard_import(stmt):
            value = _INVALID
            advance_module_state(stmt)
            continue
        if name in _bound_names(stmt):
            value = _INVALID
        advance_module_state(stmt)
    if value in (_MISSING, _INVALID):
        return {}
    return value


def _mapping_value_binding(value, env, class_bindings):
    class_name = _mapping_value_name(value)
    if class_name is None:
        return None
    binding = class_bindings.get(class_name)
    if binding is None:
        return None
    return class_name, binding


def _node_mapping_invalidated_by_names(value, names):
    return any(class_name in names for class_name, _binding in value.values())


def _node_class_mappings(tree):
    if _has_module_wildcard_import(tree):
        return {}
    mappings = _final_module_dict(
        tree,
        "NODE_CLASS_MAPPINGS",
        _mapping_value_binding,
        _node_mapping_invalidated_by_names,
    )
    return {str(node_type): binding for node_type, (_class_name, binding) in mappings.items() if node_type}


def _display_mappings(tree):
    displays = _final_module_dict(
        tree,
        "NODE_DISPLAY_NAME_MAPPINGS",
        lambda value, env, _class_bindings: _literal(value, env),
    )
    return {str(k): str(v) for k, v in displays.items()}


def _signature_from_class(node_type, cls, display, pack_meta, class_env, input_env):
    input_types = _input_types(cls, input_env)
    return_types = _class_attr(cls, "RETURN_TYPES", class_env)
    return_names = _class_attr(cls, "RETURN_NAMES", class_env)
    if return_types is _INVALID or return_names is _INVALID:
        return None
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
    if return_names is _MISSING:
        output_names = []
    elif isinstance(return_names, (list, tuple)):
        output_names = [str(name) for name in return_names]
    else:
        return None

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
        mappings = _node_class_mappings(tree)
        displays = _display_mappings(tree)
        for node_type, binding in sorted(mappings.items()):
            cls, class_env = binding
            sig = _signature_from_class(node_type, cls, displays.get(node_type), pack_meta, class_env, env)
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
