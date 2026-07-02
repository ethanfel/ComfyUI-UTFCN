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
DEFAULT_GENERATED_AT = "1970-01-01T00:00:00Z"


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
_CLASS_SIGNATURE_ATTRS = {"INPUT_TYPES", "RETURN_NAMES", "RETURN_TYPES"}
_DYNAMIC_NAMESPACE_MUTATION = object()
_NAMESPACE_FUNCTIONS = {"globals", "locals", "vars"}
_NAMESPACE_DUNDER_MUTATORS = {"__delitem__", "__setitem__"}


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
            key_value = _literal(key, env, allow_mutable_env=False)
            try:
                result[key_value] = _literal(value, env, allow_mutable_env=False)
            except TypeError as exc:
                raise UnsupportedStaticExpression("unhashable dict key") from exc
        return result
    if isinstance(node, ast.Name) and node.id in env:
        value = env[node.id]
        if value is _INVALID:
            raise UnsupportedStaticExpression(f"unsupported env reference {node.id!r}")
        if not allow_mutable_env and _is_mutable_static_value(value):
            raise UnsupportedStaticExpression(f"mutable env reference {node.id!r} is not supported")
        return value
    raise UnsupportedStaticExpression(type(node).__name__)


def _invalidate_env_name(env, name):
    if name == "classmethod":
        env[name] = _INVALID
    else:
        env.pop(name, None)


def _invalidate_env_names(env, names):
    for name in names:
        _invalidate_env_name(env, name)


def _is_mutable_static_value(value):
    if isinstance(value, (dict, list, set)):
        return True
    if isinstance(value, tuple):
        return any(_is_mutable_static_value(item) for item in value)
    return False


def _namespace_call_function_name(node):
    if not isinstance(node, ast.Call):
        return None
    if not isinstance(node.func, ast.Name) or node.func.id not in _NAMESPACE_FUNCTIONS:
        return None
    if node.args or node.keywords:
        return None
    return node.func.id


def _namespace_subscript_name(node):
    if not isinstance(node, ast.Subscript):
        return None
    if _namespace_call_function_name(node.value) is None:
        return None
    if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
        return node.slice.value
    return None


def _namespace_lookup_name(node):
    if not isinstance(node, ast.Call):
        return None
    if not isinstance(node.func, ast.Attribute) or node.func.attr != "get":
        return None
    if _namespace_call_function_name(node.func.value) is None:
        return None
    if not node.args:
        return None
    if isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return node.args[0].value
    return None


def _target_names(target):
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Call):
        name = _namespace_lookup_name(target)
        return {name} if name is not None else set()
    if isinstance(target, (ast.List, ast.Tuple)):
        names = set()
        for item in target.elts:
            names.update(_target_names(item))
        return names
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if isinstance(target, ast.Attribute):
        return _target_names(target.value)
    if isinstance(target, ast.Subscript):
        name = _namespace_subscript_name(target)
        if name is not None:
            return {name}
        return _target_names(target.value)
    return set()


def _direct_target_names(target):
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.List, ast.Tuple)):
        names = set()
        for item in target.elts:
            names.update(_direct_target_names(item))
        return names
    if isinstance(target, ast.Starred):
        return _direct_target_names(target.value)
    return set()


def _root_name(node, namespace_aliases=None):
    namespace_aliases = namespace_aliases or set()
    while True:
        name = _namespace_lookup_name(node)
        if name is not None:
            return name
        name = _namespace_subscript_name(node)
        if name is not None:
            return name
        name = _namespace_alias_lookup_name(node, namespace_aliases)
        if name is not None:
            return name
        name = _namespace_alias_subscript_name(node, namespace_aliases)
        if name is not None:
            return name
        if not isinstance(node, (ast.Attribute, ast.Subscript)):
            break
        node = node.value
    if isinstance(node, ast.Name):
        return node.id
    return None


def _getattr_signature_target_names(node, namespace_aliases=None):
    if not isinstance(node, ast.Call):
        return set()
    if not isinstance(node.func, ast.Name) or node.func.id != "getattr":
        return set()
    if len(node.args) < 2:
        return set()
    name = _root_name(node.args[0], namespace_aliases)
    if name is None:
        return set()
    attr = node.args[1]
    if (
        isinstance(attr, ast.Constant)
        and isinstance(attr.value, str)
        and attr.value not in _CLASS_SIGNATURE_ATTRS
    ):
        return set()
    return {name}


def _getattr_mutating_method_target_names(node):
    if not isinstance(node, ast.Call):
        return set()
    if not isinstance(node.func, ast.Call):
        return set()
    getattr_call = node.func
    if not isinstance(getattr_call.func, ast.Name) or getattr_call.func.id != "getattr":
        return set()
    if len(getattr_call.args) < 2:
        return set()
    method = getattr_call.args[1]
    if isinstance(method, ast.Constant) and isinstance(method.value, str):
        if method.value not in _MUTATING_METHODS:
            return set()
    return _target_names(getattr_call.args[0])


def _namespace_mutating_call_target_names(node):
    if not isinstance(node, ast.Call):
        return set()
    if not isinstance(node.func, ast.Attribute):
        return set()
    if _namespace_call_function_name(node.func.value) is None:
        return set()
    if node.func.attr in _NAMESPACE_DUNDER_MUTATORS:
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            return {node.args[0].value}
        return {_DYNAMIC_NAMESPACE_MUTATION}
    if node.func.attr not in _MUTATING_METHODS:
        return set()
    if node.func.attr != "update":
        return {_DYNAMIC_NAMESPACE_MUTATION}

    names = set()
    for keyword in node.keywords:
        if keyword.arg is None:
            names.add(_DYNAMIC_NAMESPACE_MUTATION)
        else:
            names.add(keyword.arg)
    if node.args or not names:
        names.add(_DYNAMIC_NAMESPACE_MUTATION)
    return names


def _name_invalidated_by(name, names):
    return name in names or _DYNAMIC_NAMESPACE_MUTATION in names


def _attribute_target_base_names(target, namespace_aliases=None):
    if isinstance(target, ast.Attribute):
        name = _root_name(target.value, namespace_aliases)
        return {name} if name else set()
    names = _getattr_signature_target_names(target, namespace_aliases)
    if names:
        return names
    if isinstance(target, ast.Subscript):
        return _attribute_target_base_names(target.value, namespace_aliases)
    if isinstance(target, (ast.List, ast.Tuple)):
        names = set()
        for item in target.elts:
            names.update(_attribute_target_base_names(item, namespace_aliases))
        return names
    if isinstance(target, ast.Starred):
        return _attribute_target_base_names(target.value, namespace_aliases)
    return set()


def _setattr_delattr_target_names(node, namespace_aliases=None):
    if not isinstance(node, ast.Call):
        return set()
    if not isinstance(node.func, ast.Name) or node.func.id not in {"delattr", "setattr"}:
        return set()
    if len(node.args) < 2:
        return set()
    attr = node.args[1]
    if (
        isinstance(attr, ast.Constant)
        and isinstance(attr.value, str)
        and attr.value not in _CLASS_SIGNATURE_ATTRS
    ):
        return set()
    name = _root_name(node.args[0], namespace_aliases)
    return {name} if name else set()


def _class_attribute_mutation_target_names(stmt, namespace_aliases=None):
    namespace_aliases = namespace_aliases or set()
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
                names.update(_attribute_target_base_names(target, namespace_aliases))
            self.visit(node.value)

        def visit_AnnAssign(self, node):
            names.update(_attribute_target_base_names(node.target, namespace_aliases))
            if node.value is not None:
                self.visit(node.value)

        def visit_AugAssign(self, node):
            names.update(_attribute_target_base_names(node.target, namespace_aliases))
            self.visit(node.value)

        def visit_Delete(self, node):
            for target in node.targets:
                names.update(_attribute_target_base_names(target, namespace_aliases))

        def visit_Call(self, node):
            names.update(_setattr_delattr_target_names(node, namespace_aliases))
            names.update(_getattr_mutating_method_target_names(node))
            names.update(_namespace_mutating_call_target_names(node))
            if isinstance(node.func, ast.Attribute) and node.func.attr in _MUTATING_METHODS:
                names.update(_attribute_target_base_names(node.func.value, namespace_aliases))
            self.generic_visit(node)

    AttributeMutationVisitor().visit(stmt)
    return names


def _signature_attribute_reference_names(node, namespace_aliases=None):
    namespace_aliases = namespace_aliases or set()
    names = set()

    class SignatureAttributeReferenceVisitor(ast.NodeVisitor):
        def visit_Attribute(self, child):
            if child.attr in _CLASS_SIGNATURE_ATTRS:
                name = _root_name(child.value, namespace_aliases)
                if name is not None:
                    names.add(name)
            self.generic_visit(child)

        def visit_Call(self, child):
            names.update(_getattr_signature_target_names(child, namespace_aliases))
            self.generic_visit(child)

    SignatureAttributeReferenceVisitor().visit(node)
    return names


def _class_attribute_observed_target_names(stmt, namespace_aliases=None):
    namespace_aliases = namespace_aliases or set()
    names = set()

    class AttributeObservationVisitor(ast.NodeVisitor):
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
            if isinstance(node.func, ast.Attribute):
                names.update(_signature_attribute_reference_names(node.func.value, namespace_aliases))
            for arg in node.args:
                names.update(_signature_attribute_reference_names(arg, namespace_aliases))
            for keyword in node.keywords:
                names.update(_signature_attribute_reference_names(keyword.value, namespace_aliases))
            self.generic_visit(node)

    AttributeObservationVisitor().visit(stmt)
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
        def _visit_function_definition_expressions(self, child):
            for decorator in child.decorator_list:
                self.visit(decorator)
            self.visit(child.args)
            if child.returns is not None:
                self.visit(child.returns)
            for type_param in getattr(child, "type_params", ()):
                self.visit(type_param)

        def visit_FunctionDef(self, child):
            self._visit_function_definition_expressions(child)

        def visit_AsyncFunctionDef(self, child):
            self._visit_function_definition_expressions(child)

        def visit_ClassDef(self, child):
            for decorator in child.decorator_list:
                self.visit(decorator)
            for base in child.bases:
                self.visit(base)
            for keyword in child.keywords:
                self.visit(keyword.value)
            for type_param in getattr(child, "type_params", ()):
                self.visit(type_param)
            for stmt in child.body:
                self.visit(stmt)

        def visit_Lambda(self, child):
            self.visit(child.args)

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
            names.update(_setattr_delattr_target_names(node))
            names.update(_getattr_mutating_method_target_names(node))
            names.update(_namespace_mutating_call_target_names(node))
            if isinstance(node.func, ast.Attribute) and node.func.attr in _MUTATING_METHODS:
                names.update(_target_names(node.func.value))
            self.generic_visit(node)

    MutatingCallVisitor().visit(stmt)
    return names


def _referenced_names(node):
    names = set()

    class ReferenceVisitor(ast.NodeVisitor):
        def visit_Call(self, child):
            name = _namespace_lookup_name(child)
            if name is not None:
                names.add(name)
            self.generic_visit(child)

        def visit_Subscript(self, child):
            name = _namespace_subscript_name(child)
            if name is not None:
                names.add(name)
            self.generic_visit(child)

        def visit_Name(self, child):
            names.add(child.id)

    ReferenceVisitor().visit(node)
    return names


def _arbitrary_call_observed_names(stmt):
    names = set()

    class ArbitraryCallVisitor(ast.NodeVisitor):
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
            names.update(_referenced_names(node.func))
            if isinstance(node.func, ast.Attribute):
                names.update(_referenced_names(node.func.value))
            for arg in node.args:
                names.update(_referenced_names(arg))
            for keyword in node.keywords:
                names.update(_referenced_names(keyword.value))
            self.generic_visit(node)

    ArbitraryCallVisitor().visit(stmt)
    return names


def _has_arbitrary_call(stmt):
    found = False

    class ArbitraryCallPresenceVisitor(ast.NodeVisitor):
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
            nonlocal found
            found = True

    ArbitraryCallPresenceVisitor().visit(stmt)
    return found


def _definition_time_referenced_names(stmt):
    names = set()

    def collect_function_definition_expressions(node):
        for decorator in node.decorator_list:
            names.update(_referenced_names(decorator))
        names.update(_referenced_names(node.args))
        if node.returns is not None:
            names.update(_referenced_names(node.returns))
        for type_param in getattr(node, "type_params", ()):
            names.update(_referenced_names(type_param))

    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        collect_function_definition_expressions(stmt)
    elif isinstance(stmt, ast.ClassDef):
        for decorator in stmt.decorator_list:
            names.update(_referenced_names(decorator))
        for base in stmt.bases:
            names.update(_referenced_names(base))
        for keyword in stmt.keywords:
            names.update(_referenced_names(keyword.value))
        for type_param in getattr(stmt, "type_params", ()):
            names.update(_referenced_names(type_param))
    elif isinstance(stmt, ast.Lambda):
        names.update(_referenced_names(stmt.args))

    return names


def _class_body_expression_referenced_names(stmt):
    if not isinstance(stmt, ast.Expr):
        return set()

    names = set()

    class ClassBodyExpressionReferenceVisitor(ast.NodeVisitor):
        def visit_Call(self, child):
            name = _namespace_lookup_name(child)
            if name is not None:
                names.add(name)
            self.generic_visit(child)

        def visit_Subscript(self, child):
            name = _namespace_subscript_name(child)
            if name is not None:
                names.add(name)
            self.generic_visit(child)

        def visit_Lambda(self, child):
            self.visit(child.args)

        def visit_FunctionDef(self, child):
            return None

        def visit_AsyncFunctionDef(self, child):
            return None

        def visit_ClassDef(self, child):
            return None

        def visit_Name(self, child):
            names.add(child.id)

    ClassBodyExpressionReferenceVisitor().visit(stmt.value)
    return names


def _assigned_names_in_control_flow(stmt):
    names = _mutating_call_target_names(stmt) | _arbitrary_call_observed_names(stmt)

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
            names.update(_arbitrary_call_observed_names(node))
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


def _is_trivially_safe_class_def(stmt):
    return (
        isinstance(stmt, ast.ClassDef)
        and not stmt.decorator_list
        and not stmt.bases
        and not stmt.keywords
        and not getattr(stmt, "type_params", ())
    )


def _namespace_assignment_target_names(target):
    name = _namespace_subscript_name(target)
    if name is not None:
        return {name}
    if isinstance(target, ast.Attribute):
        return _namespace_assignment_target_names(target.value)
    if isinstance(target, ast.Subscript):
        return _namespace_assignment_target_names(target.value)
    if isinstance(target, (ast.List, ast.Tuple)):
        names = set()
        for item in target.elts:
            names.update(_namespace_assignment_target_names(item))
        return names
    if isinstance(target, ast.Starred):
        return _namespace_assignment_target_names(target.value)
    return set()


def _class_body_global_names(cls):
    names = set()

    class GlobalVisitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            return None

        def visit_AsyncFunctionDef(self, node):
            return None

        def visit_ClassDef(self, node):
            return None

        def visit_Global(self, node):
            names.update(node.names)

    for stmt in cls.body:
        GlobalVisitor().visit(stmt)
    return names


def _class_body_module_mutation_names(cls):
    global_names = _class_body_global_names(cls)
    names = set()
    namespace_aliases = set()

    def add_assignment_targets(stmt):
        names.update(_assignment_target_names(stmt).intersection(global_names))
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                names.update(_namespace_assignment_target_names(target))
        elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
            names.update(_namespace_assignment_target_names(stmt.target))
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            names.update(_namespace_assignment_target_names(stmt.target))

    class ClassBodyMutationVisitor(ast.NodeVisitor):
        def _visit_function_definition_expressions(self, node):
            names.update(_mutating_call_target_names(node))
            names.update(_namespace_alias_mutation_target_names(node, set()))

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
            names.update(_class_body_module_mutation_names(node))

        def visit_Assign(self, node):
            add_assignment_targets(node)
            self.visit(node.value)

        def visit_AnnAssign(self, node):
            add_assignment_targets(node)
            if node.value is not None:
                self.visit(node.value)

        def visit_AugAssign(self, node):
            add_assignment_targets(node)
            self.visit(node.value)

        def visit_Delete(self, node):
            names.update(_delete_target_names(node).intersection(global_names))
            for target in node.targets:
                names.update(_namespace_assignment_target_names(target))

        def visit_For(self, node):
            add_assignment_targets(node)
            self.generic_visit(node)

        def visit_AsyncFor(self, node):
            add_assignment_targets(node)
            self.generic_visit(node)

        def visit_With(self, node):
            for item in node.items:
                if item.optional_vars is not None:
                    names.update(_target_names(item.optional_vars).intersection(global_names))
                    names.update(_namespace_assignment_target_names(item.optional_vars))
            self.generic_visit(node)

        def visit_AsyncWith(self, node):
            for item in node.items:
                if item.optional_vars is not None:
                    names.update(_target_names(item.optional_vars).intersection(global_names))
                    names.update(_namespace_assignment_target_names(item.optional_vars))
            self.generic_visit(node)

        def visit_Import(self, node):
            names.update(_bound_names(node).intersection(global_names))

        def visit_ImportFrom(self, node):
            names.update(_bound_names(node).intersection(global_names))

        def visit_Call(self, node):
            names.update(_namespace_mutating_call_target_names(node))
            self.generic_visit(node)

    for stmt in cls.body:
        names.update(_namespace_alias_mutation_target_names(stmt, namespace_aliases))
        ClassBodyMutationVisitor().visit(stmt)
        _update_namespace_aliases(stmt, namespace_aliases)
    return names


def _class_body_namespace_mutation_names(cls):
    names = set()
    namespace_aliases = set()
    for stmt in cls.body:
        names.update(_namespace_alias_mutation_target_names(stmt, namespace_aliases))
        _update_namespace_aliases(stmt, namespace_aliases)
    return names


def _apply_module_stmt_to_env(stmt, env, class_bindings=None):
    names = _mutating_call_target_names(stmt)
    if isinstance(stmt, ast.ClassDef):
        names.update(_class_body_module_mutation_names(stmt))
    if _DYNAMIC_NAMESPACE_MUTATION in names:
        env.clear()
        if class_bindings is not None:
            class_bindings.clear()
    else:
        _invalidate_class_bindings(class_bindings, names)
        _invalidate_env_names(env, names)
    observed_names = _arbitrary_call_observed_names(stmt)
    for name in observed_names:
        if name in env and _is_mutable_static_value(env[name]):
            _invalidate_env_name(env, name)
    if _has_arbitrary_call(stmt):
        env.clear()
        _invalidate_env_name(env, "classmethod")
        if class_bindings is not None:
            class_bindings.clear()
    if isinstance(stmt, ast.ClassDef):
        if class_bindings is not None:
            if _is_trivially_safe_class_def(stmt):
                class_bindings[stmt.name] = (stmt, dict(env))
            else:
                class_bindings.pop(stmt.name, None)
        _invalidate_env_name(env, stmt.name)
        return
    if isinstance(stmt, ast.Assign):
        names = _assignment_target_names(stmt)
        _invalidate_class_bindings(class_bindings, names)
        if len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            name = stmt.targets[0].id
            subscript_root = _mutable_env_subscript_root(stmt.value, env)
            if subscript_root is not None:
                env.pop(subscript_root, None)
                _invalidate_env_name(env, name)
                return
            if (
                isinstance(stmt.value, ast.Name)
                and stmt.value.id in env
                and _is_mutable_static_value(env[stmt.value.id])
            ):
                env.pop(stmt.value.id, None)
                _invalidate_env_name(env, name)
                return
            try:
                env[name] = _literal(stmt.value, env)
            except UnsupportedStaticExpression:
                _invalidate_env_name(env, name)
        else:
            _invalidate_env_names(env, names)
        return
    if isinstance(stmt, ast.AnnAssign):
        names = _assignment_target_names(stmt)
        _invalidate_class_bindings(class_bindings, names)
        if stmt.value is None:
            return
        if isinstance(stmt.target, ast.Name):
            name = stmt.target.id
            subscript_root = _mutable_env_subscript_root(stmt.value, env)
            if subscript_root is not None:
                env.pop(subscript_root, None)
                _invalidate_env_name(env, name)
                return
            if (
                isinstance(stmt.value, ast.Name)
                and stmt.value.id in env
                and _is_mutable_static_value(env[stmt.value.id])
            ):
                env.pop(stmt.value.id, None)
                _invalidate_env_name(env, name)
                return
            try:
                env[name] = _literal(stmt.value, env)
            except UnsupportedStaticExpression:
                _invalidate_env_name(env, name)
        else:
            _invalidate_env_names(env, names)
        return
    if isinstance(stmt, ast.AugAssign):
        names = _assignment_target_names(stmt)
        _invalidate_class_bindings(class_bindings, names)
        _invalidate_env_names(env, names)
        return
    if isinstance(stmt, ast.Delete):
        names = _delete_target_names(stmt)
        _invalidate_class_bindings(class_bindings, names)
        _invalidate_env_names(env, names)
        return
    if isinstance(stmt, ast.Expr):
        names = _bound_names(stmt)
        _invalidate_class_bindings(class_bindings, names)
        _invalidate_env_names(env, names)
        return
    if isinstance(stmt, _CONTROL_FLOW_TYPES):
        if _has_wildcard_import_in_control_flow(stmt):
            env.clear()
            if class_bindings is not None:
                class_bindings.clear()
            return
        names = _assigned_names_in_control_flow(stmt)
        _invalidate_class_bindings(class_bindings, names)
        _invalidate_env_names(env, names)
        return
    if _has_wildcard_import(stmt):
        env.clear()
        if class_bindings is not None:
            class_bindings.clear()
        return
    names = _bound_names(stmt)
    _invalidate_class_bindings(class_bindings, names)
    _invalidate_env_names(env, names)


def _collect_module_env(tree, class_bindings=None):
    env = {}
    for stmt in tree.body:
        _apply_module_stmt_to_env(stmt, env, class_bindings)
    return env


def normalise_input_spec(spec):
    if not isinstance(spec, (list, tuple)) or not spec:
        return None
    first = spec[0]
    if isinstance(first, list):
        return "COMBO" if all(isinstance(value, str) for value in first) else None
    return first if isinstance(first, str) else None


def _class_defs(tree):
    return {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}


def _is_mutable_env_reference(node, env):
    return isinstance(node, ast.Name) and node.id in env and _is_mutable_static_value(env[node.id])


def _mutable_env_subscript_root(node, env):
    if not isinstance(node, ast.Subscript):
        return None
    name = _root_name(node)
    if name in env and _is_mutable_static_value(env[name]):
        return name
    return None


def _input_types_decorators_are_supported(decorators, classmethod_shadowed):
    for decorator in decorators:
        if not isinstance(decorator, ast.Name) or decorator.id != "classmethod":
            return False
        if classmethod_shadowed:
            return False
    return True


def _unpack_target_value_pairs(target, value):
    if not isinstance(target, (ast.Tuple, ast.List)) or not isinstance(value, (ast.Tuple, ast.List)):
        return ()

    targets = target.elts
    values = value.elts
    starred_indices = [index for index, item in enumerate(targets) if isinstance(item, ast.Starred)]
    if not starred_indices:
        if len(targets) != len(values):
            return ()
        return tuple(zip(targets, values))

    if len(starred_indices) != 1:
        return ()

    starred_index = starred_indices[0]
    prefix_count = starred_index
    suffix_count = len(targets) - starred_index - 1
    if len(values) < prefix_count + suffix_count:
        return ()

    pairs = [(targets[index], values[index]) for index in range(prefix_count)]
    star_stop = len(values) - suffix_count if suffix_count else len(values)
    pairs.append((targets[starred_index], ast.Tuple(elts=values[prefix_count:star_stop], ctx=ast.Load())))
    if suffix_count:
        target_suffix = targets[-suffix_count:]
        value_suffix = values[-suffix_count:]
        pairs.extend(zip(target_suffix, value_suffix))
    return tuple(pairs)


def _alias_target_name(target):
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Starred) and isinstance(target.value, ast.Name):
        return target.value.id
    return None


def _class_attr_alias_sources(value, name, aliases):
    if isinstance(value, ast.Name):
        return value.id == name or value.id in aliases
    if isinstance(value, (ast.Tuple, ast.List)):
        return any(_class_attr_alias_sources(item, name, aliases) for item in value.elts)
    return False


def _update_class_attr_aliases_from_unpack(target, value, name, aliases):
    found = False
    for target_item, value_item in _unpack_target_value_pairs(target, value):
        target_name = _alias_target_name(target_item)
        if target_name is None:
            continue
        if _class_attr_alias_sources(value_item, name, aliases):
            aliases.add(target_name)
            found = True
    return found


def _input_types_alias_sources(value, aliases):
    if isinstance(value, ast.Name):
        return value.id in _CLASS_SIGNATURE_ATTRS or value.id in aliases
    if isinstance(value, (ast.Tuple, ast.List)):
        return any(_input_types_alias_sources(item, aliases) for item in value.elts)
    return False


def _update_input_types_aliases_from_unpack(target, value, aliases):
    found = False
    for target_item, value_item in _unpack_target_value_pairs(target, value):
        target_name = _alias_target_name(target_item)
        if target_name is None:
            continue
        if _input_types_alias_sources(value_item, aliases):
            aliases.add(target_name)
            found = True
    return found


def _class_attr(cls, name, env):
    value = _MISSING
    sticky_invalid = False
    aliases = set()
    namespace_mutations = _class_body_namespace_mutation_names(cls)
    if _name_invalidated_by(name, namespace_mutations):
        return _INVALID
    for stmt in cls.body:
        mutating_targets = _mutating_call_target_names(stmt)
        observed_targets = _arbitrary_call_observed_names(stmt)
        expression_references = _class_body_expression_referenced_names(stmt)
        has_arbitrary_call = _has_arbitrary_call(stmt)
        if has_arbitrary_call:
            value = _INVALID
            sticky_invalid = True
        if aliases.intersection(mutating_targets):
            value = _INVALID
        if name in mutating_targets:
            value = _INVALID
        if aliases.intersection(observed_targets):
            value = _INVALID
        if name in observed_targets:
            value = _INVALID
        if aliases.intersection(expression_references):
            value = _INVALID
        if name in expression_references:
            value = _INVALID
        if isinstance(stmt, ast.Assign):
            target_names = _assignment_target_names(stmt)
            if len(stmt.targets) > 1 and _class_attr_alias_sources(stmt.value, name, aliases):
                target_aliases = []
                for target in stmt.targets:
                    target_name = _alias_target_name(target)
                    if target_name is None:
                        value = _INVALID
                        target_aliases = []
                        break
                    target_aliases.append(target_name)
                aliases.update(alias for alias in target_aliases if alias != name)
                if name not in target_names:
                    continue
            if (
                len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and stmt.targets[0].id != name
                and _class_attr_alias_sources(stmt.value, name, aliases)
            ):
                aliases.add(stmt.targets[0].id)
                continue
            if (
                len(stmt.targets) == 1
                and name not in target_names
                and _update_class_attr_aliases_from_unpack(stmt.targets[0], stmt.value, name, aliases)
            ):
                continue
            if aliases.intersection(target_names):
                value = _INVALID
                aliases.difference_update(target_names)
            if name not in target_names:
                continue
            if sticky_invalid:
                value = _INVALID
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
            if sticky_invalid:
                value = _INVALID
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


def _input_types(cls, env, decorator_env):
    value = _MISSING
    sticky_invalid = False
    aliases = set()
    classmethod_shadowed = "classmethod" in decorator_env
    namespace_mutations = _class_body_namespace_mutation_names(cls)
    if _name_invalidated_by("INPUT_TYPES", namespace_mutations):
        return None
    for stmt in cls.body:
        mutating_targets = _mutating_call_target_names(stmt)
        observed_targets = _arbitrary_call_observed_names(stmt)
        definition_references = _definition_time_referenced_names(stmt)
        expression_references = _class_body_expression_referenced_names(stmt)
        has_arbitrary_call = _has_arbitrary_call(stmt)
        protected_definition_references = _CLASS_SIGNATURE_ATTRS | aliases
        input_types_invalidated = (
            has_arbitrary_call
            or "INPUT_TYPES" in mutating_targets
            or bool(aliases.intersection(mutating_targets))
            or "INPUT_TYPES" in observed_targets
            or bool(aliases.intersection(observed_targets))
            or bool(definition_references.intersection(protected_definition_references))
            or bool(expression_references.intersection(protected_definition_references))
        )
        if input_types_invalidated:
            value = _INVALID
            sticky_invalid = True
        if isinstance(stmt, ast.FunctionDef) and stmt.name == "INPUT_TYPES":
            if has_arbitrary_call:
                value = _INVALID
                sticky_invalid = True
                continue
            if input_types_invalidated or sticky_invalid:
                continue
            if not _input_types_decorators_are_supported(stmt.decorator_list, classmethod_shadowed):
                value = _INVALID
                sticky_invalid = True
                continue
            if len(stmt.body) != 1 or not isinstance(stmt.body[0], ast.Return):
                value = _INVALID
                sticky_invalid = True
                continue
            try:
                candidate = _literal(stmt.body[0].value, env)
            except UnsupportedStaticExpression:
                value = _INVALID
                sticky_invalid = True
                continue
            if isinstance(candidate, dict):
                value = candidate
            else:
                value = _INVALID
                sticky_invalid = True
            continue
        if isinstance(stmt, ast.AsyncFunctionDef) and stmt.name == "INPUT_TYPES":
            value = _INVALID
            sticky_invalid = True
            continue
        rebound_names = _assignment_target_names(stmt) | _delete_target_names(stmt) | _bound_names(stmt)
        aliases.difference_update(rebound_names)
        if "classmethod" in (
            _assignment_target_names(stmt)
            | _delete_target_names(stmt)
            | _bound_names(stmt)
            | mutating_targets
        ):
            classmethod_shadowed = True
        if isinstance(stmt, ast.Assign):
            target_names = _assignment_target_names(stmt)
            if len(stmt.targets) > 1 and _input_types_alias_sources(stmt.value, aliases):
                target_aliases = []
                for target in stmt.targets:
                    target_name = _alias_target_name(target)
                    if target_name is None:
                        value = _INVALID
                        target_aliases = []
                        break
                    target_aliases.append(target_name)
                aliases.update(alias for alias in target_aliases if alias != "INPUT_TYPES")
                if "INPUT_TYPES" not in target_names:
                    continue
            if (
                len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and stmt.targets[0].id != "INPUT_TYPES"
                and _input_types_alias_sources(stmt.value, aliases)
            ):
                aliases.add(stmt.targets[0].id)
                continue
            if (
                len(stmt.targets) == 1
                and "INPUT_TYPES" not in target_names
                and _update_input_types_aliases_from_unpack(stmt.targets[0], stmt.value, aliases)
            ):
                continue
            if "INPUT_TYPES" in target_names:
                value = _INVALID
            continue
        if isinstance(stmt, ast.AnnAssign):
            target_names = _assignment_target_names(stmt)
            if (
                isinstance(stmt.target, ast.Name)
                and stmt.target.id != "INPUT_TYPES"
                and stmt.value is not None
                and _input_types_alias_sources(stmt.value, aliases)
            ):
                aliases.add(stmt.target.id)
                continue
            if "INPUT_TYPES" in target_names:
                value = _INVALID
            continue
        if isinstance(stmt, ast.AugAssign):
            if "INPUT_TYPES" in _assignment_target_names(stmt):
                value = _INVALID
            continue
        if isinstance(stmt, ast.Delete):
            if "INPUT_TYPES" in _delete_target_names(stmt):
                value = _INVALID
            continue
        if isinstance(stmt, ast.Expr):
            if "INPUT_TYPES" in mutating_targets:
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


def _class_alias_sources(value, class_aliases, class_bindings, namespace_aliases=None):
    namespace_aliases = namespace_aliases or set()
    if isinstance(value, ast.Name):
        if value.id in class_aliases:
            return set(class_aliases[value.id])
        if value.id in class_bindings:
            return {value.id}
        return set()
    if isinstance(value, (ast.Tuple, ast.List)):
        sources = set()
        for item in value.elts:
            sources.update(_class_alias_sources(item, class_aliases, class_bindings, namespace_aliases))
        return sources

    name = _namespace_subscript_name(value) or _namespace_lookup_name(value)
    name = name or _namespace_alias_subscript_name(value, namespace_aliases)
    name = name or _namespace_alias_lookup_name(value, namespace_aliases)
    if name in class_aliases:
        return set(class_aliases[name])
    if name in class_bindings:
        return {name}
    return set()


def _update_class_alias_from_unpack(target, value, class_aliases, class_bindings, namespace_aliases):
    for target_item, value_item in _unpack_target_value_pairs(target, value):
        target_name = _alias_target_name(target_item)
        if target_name is None:
            continue
        sources = _class_alias_sources(value_item, class_aliases, class_bindings, namespace_aliases)
        if sources:
            class_aliases[target_name] = sources


def _update_class_aliases(stmt, class_aliases, class_bindings, namespace_aliases=None):
    namespace_aliases = namespace_aliases or set()
    rebound_names = _assignment_target_names(stmt) | _delete_target_names(stmt) | _bound_names(stmt)
    for name in rebound_names:
        class_aliases.pop(name, None)

    if isinstance(stmt, ast.ClassDef):
        if stmt.name in class_bindings and _is_trivially_safe_class_def(stmt):
            class_aliases[stmt.name] = {stmt.name}
        return

    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
        sources = _class_alias_sources(stmt.value, class_aliases, class_bindings, namespace_aliases)
        if sources:
            class_aliases[stmt.targets[0].id] = sources
    elif isinstance(stmt, ast.Assign) and len(stmt.targets) > 1:
        sources = _class_alias_sources(stmt.value, class_aliases, class_bindings, namespace_aliases)
        if sources:
            for target in stmt.targets:
                target_name = _alias_target_name(target)
                if target_name is not None:
                    class_aliases[target_name] = sources
    elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        _update_class_alias_from_unpack(
            stmt.targets[0],
            stmt.value,
            class_aliases,
            class_bindings,
            namespace_aliases,
        )
    elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
        sources = _class_alias_sources(stmt.value, class_aliases, class_bindings, namespace_aliases)
        if sources:
            class_aliases[stmt.target.id] = sources


def _expanded_class_attribute_names(names, class_aliases):
    expanded = set(names)
    for name in names:
        expanded.update(class_aliases.get(name, ()))
    return expanded


def _class_attribute_alias_sources(
    value,
    class_attribute_aliases,
    class_aliases,
    class_bindings,
    namespace_aliases=None,
):
    namespace_aliases = namespace_aliases or set()
    if isinstance(value, ast.Name):
        return set(class_attribute_aliases.get(value.id, ()))
    if isinstance(value, (ast.Tuple, ast.List)):
        sources = set()
        for item in value.elts:
            sources.update(
                _class_attribute_alias_sources(
                    item,
                    class_attribute_aliases,
                    class_aliases,
                    class_bindings,
                    namespace_aliases,
                )
            )
        return sources

    names = set()
    if isinstance(value, ast.Attribute) and value.attr in _CLASS_SIGNATURE_ATTRS:
        name = _root_name(value.value, namespace_aliases)
        if name is not None:
            names.add(name)
    else:
        names.update(_getattr_signature_target_names(value, namespace_aliases))

    sources = set()
    for name in names:
        if name in class_aliases:
            sources.update(class_aliases[name])
        if name in class_bindings:
            sources.add(name)
    return sources


def _update_class_attribute_alias_from_unpack(
    target,
    value,
    class_attribute_aliases,
    class_aliases,
    class_bindings,
    namespace_aliases,
):
    for target_item, value_item in _unpack_target_value_pairs(target, value):
        target_name = _alias_target_name(target_item)
        if target_name is None:
            continue
        sources = _class_attribute_alias_sources(
            value_item,
            class_attribute_aliases,
            class_aliases,
            class_bindings,
            namespace_aliases,
        )
        if sources:
            class_attribute_aliases[target_name] = sources


def _class_attribute_alias_invalidated_names(stmt, class_attribute_aliases):
    names = (
        _mutating_call_target_names(stmt)
        | _arbitrary_call_observed_names(stmt)
        | _assignment_target_names(stmt)
        | _delete_target_names(stmt)
        | _bound_names(stmt)
    )
    invalidated = set()
    for name in names:
        invalidated.update(class_attribute_aliases.get(name, ()))
    return invalidated


def _update_class_attribute_aliases(
    stmt,
    class_attribute_aliases,
    class_aliases,
    class_bindings,
    namespace_aliases=None,
):
    namespace_aliases = namespace_aliases or set()
    rebound_names = _assignment_target_names(stmt) | _delete_target_names(stmt) | _bound_names(stmt)
    for name in rebound_names:
        class_attribute_aliases.pop(name, None)

    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
        sources = _class_attribute_alias_sources(
            stmt.value,
            class_attribute_aliases,
            class_aliases,
            class_bindings,
            namespace_aliases,
        )
        if sources:
            class_attribute_aliases[stmt.targets[0].id] = sources
    elif isinstance(stmt, ast.Assign) and len(stmt.targets) > 1:
        sources = _class_attribute_alias_sources(
            stmt.value,
            class_attribute_aliases,
            class_aliases,
            class_bindings,
            namespace_aliases,
        )
        if sources:
            for target in stmt.targets:
                target_name = _alias_target_name(target)
                if target_name is not None:
                    class_attribute_aliases[target_name] = sources
    elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        _update_class_attribute_alias_from_unpack(
            stmt.targets[0],
            stmt.value,
            class_attribute_aliases,
            class_aliases,
            class_bindings,
            namespace_aliases,
        )
    elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
        sources = _class_attribute_alias_sources(
            stmt.value,
            class_attribute_aliases,
            class_aliases,
            class_bindings,
            namespace_aliases,
        )
        if sources:
            class_attribute_aliases[stmt.target.id] = sources


def _module_class_attribute_invalidated_names(
    stmt,
    class_aliases,
    class_attribute_aliases,
    namespace_aliases=None,
):
    namespace_aliases = namespace_aliases or set()
    names = _expanded_class_attribute_names(
        _class_attribute_mutation_target_names(stmt, namespace_aliases),
        class_aliases,
    )
    names.update(
        _expanded_class_attribute_names(
            _class_attribute_observed_target_names(stmt, namespace_aliases),
            class_aliases,
        )
    )
    names.update(_class_attribute_alias_invalidated_names(stmt, class_attribute_aliases))
    return names


def _module_dict_alias_sources(value, name, aliases, namespace_aliases=None):
    if isinstance(value, ast.Name):
        if value.id == name:
            return {name}
        return set(aliases.get(value.id, ()))
    if isinstance(value, (ast.Tuple, ast.List)):
        sources = set()
        for item in value.elts:
            sources.update(_module_dict_alias_sources(item, name, aliases, namespace_aliases))
        return sources

    namespace_name = _namespace_subscript_name(value) or _namespace_lookup_name(value)
    if namespace_aliases is not None:
        namespace_name = (
            namespace_name
            or _namespace_alias_subscript_name(value, namespace_aliases)
            or _namespace_alias_lookup_name(value, namespace_aliases)
        )
    if namespace_name == name:
        return {name}
    return set()


def _update_module_dict_alias_from_unpack(target, value, name, aliases, namespace_aliases):
    for target_item, value_item in _unpack_target_value_pairs(target, value):
        target_name = _alias_target_name(target_item)
        if target_name is None:
            continue
        sources = _module_dict_alias_sources(value_item, name, aliases, namespace_aliases)
        if sources:
            aliases[target_name] = sources


def _module_dict_alias_invalidated(stmt, aliases):
    names = (
        _mutating_call_target_names(stmt)
        | _arbitrary_call_observed_names(stmt)
        | _assignment_target_names(stmt)
        | _delete_target_names(stmt)
        | _bound_names(stmt)
    )
    return any(name in aliases for name in names)


def _namespace_alias_sources(value, aliases):
    if _namespace_call_function_name(value) is not None:
        return True
    if isinstance(value, ast.Name):
        return value.id in aliases
    if isinstance(value, (ast.Tuple, ast.List)):
        return any(_namespace_alias_sources(item, aliases) for item in value.elts)
    return False


def _namespace_alias_subscript_name(node, aliases):
    if not isinstance(node, ast.Subscript):
        return None
    if not isinstance(node.value, ast.Name) or node.value.id not in aliases:
        return None
    if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
        return node.slice.value
    return None


def _namespace_alias_lookup_name(node, aliases):
    if not isinstance(node, ast.Call):
        return None
    if not isinstance(node.func, ast.Attribute) or node.func.attr != "get":
        return None
    if not isinstance(node.func.value, ast.Name) or node.func.value.id not in aliases:
        return None
    if not node.args:
        return None
    if isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return node.args[0].value
    return None


def _namespace_alias_target_names(target, aliases):
    name = _namespace_alias_subscript_name(target, aliases)
    if name is not None:
        return {name}
    if isinstance(target, (ast.Tuple, ast.List)):
        names = set()
        for item in target.elts:
            names.update(_namespace_alias_target_names(item, aliases))
        return names
    if isinstance(target, ast.Starred):
        return _namespace_alias_target_names(target.value, aliases)
    if isinstance(target, (ast.Attribute, ast.Subscript)):
        return _namespace_alias_target_names(target.value, aliases)
    return set()


def _namespace_alias_mutation_target_names(stmt, aliases):
    names = set()

    class NamespaceAliasMutationVisitor(ast.NodeVisitor):
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
                names.update(_namespace_alias_target_names(target, aliases))
            self.visit(node.value)

        def visit_AnnAssign(self, node):
            names.update(_namespace_alias_target_names(node.target, aliases))
            if node.value is not None:
                self.visit(node.value)

        def visit_AugAssign(self, node):
            names.update(_namespace_alias_target_names(node.target, aliases))
            self.visit(node.value)

        def visit_Delete(self, node):
            for target in node.targets:
                names.update(_namespace_alias_target_names(target, aliases))

        def visit_Call(self, node):
            names.update(_namespace_mutating_call_target_names(node))
            if isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name) and node.func.value.id in aliases:
                    if node.func.attr in _NAMESPACE_DUNDER_MUTATORS:
                        if (
                            node.args
                            and isinstance(node.args[0], ast.Constant)
                            and isinstance(node.args[0].value, str)
                        ):
                            names.add(node.args[0].value)
                        else:
                            names.add(_DYNAMIC_NAMESPACE_MUTATION)
                    elif node.func.attr == "update":
                        for keyword in node.keywords:
                            names.add(_DYNAMIC_NAMESPACE_MUTATION if keyword.arg is None else keyword.arg)
                        if node.args or not node.keywords:
                            names.add(_DYNAMIC_NAMESPACE_MUTATION)
                    elif node.func.attr in _MUTATING_METHODS:
                        names.add(_DYNAMIC_NAMESPACE_MUTATION)
                namespace_name = _namespace_alias_subscript_name(
                    node.func.value,
                    aliases,
                ) or _namespace_alias_lookup_name(node.func.value, aliases)
                if namespace_name is not None and node.func.attr in _MUTATING_METHODS:
                    names.add(namespace_name)
            self.generic_visit(node)

    NamespaceAliasMutationVisitor().visit(stmt)
    return names


def _update_namespace_aliases(stmt, aliases):
    direct_names = set()
    if isinstance(stmt, ast.Assign):
        for target in stmt.targets:
            direct_names.update(_direct_target_names(target))
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        direct_names.update(_direct_target_names(stmt.target))
    elif isinstance(stmt, ast.Delete):
        for target in stmt.targets:
            direct_names.update(_direct_target_names(target))
    direct_names.update(_bound_names(stmt))
    aliases.difference_update(direct_names)

    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
        if _namespace_alias_sources(stmt.value, aliases):
            aliases.add(stmt.targets[0].id)
    elif isinstance(stmt, ast.Assign) and len(stmt.targets) > 1:
        if _namespace_alias_sources(stmt.value, aliases):
            for target in stmt.targets:
                target_name = _alias_target_name(target)
                if target_name is not None:
                    aliases.add(target_name)
    elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        for target_item, value_item in _unpack_target_value_pairs(stmt.targets[0], stmt.value):
            target_name = _alias_target_name(target_item)
            if target_name is not None and _namespace_alias_sources(value_item, aliases):
                aliases.add(target_name)
    elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
        if _namespace_alias_sources(stmt.value, aliases):
            aliases.add(stmt.target.id)


def _update_module_dict_aliases(stmt, name, aliases, namespace_aliases):
    rebound_names = _assignment_target_names(stmt) | _delete_target_names(stmt) | _bound_names(stmt)
    for rebound_name in rebound_names:
        aliases.pop(rebound_name, None)

    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
        sources = _module_dict_alias_sources(stmt.value, name, aliases, namespace_aliases)
        if sources:
            aliases[stmt.targets[0].id] = sources
    elif isinstance(stmt, ast.Assign) and len(stmt.targets) > 1:
        sources = _module_dict_alias_sources(stmt.value, name, aliases, namespace_aliases)
        if sources:
            for target in stmt.targets:
                target_name = _alias_target_name(target)
                if target_name is not None:
                    aliases[target_name] = sources
    elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        _update_module_dict_alias_from_unpack(stmt.targets[0], stmt.value, name, aliases, namespace_aliases)
    elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
        sources = _module_dict_alias_sources(stmt.value, name, aliases, namespace_aliases)
        if sources:
            aliases[stmt.target.id] = sources


def _final_module_dict(tree, name, value_converter, value_invalidated_by_names=None, return_state=False):
    value_invalidated_by_names = value_invalidated_by_names or (lambda _value, _names: False)
    value = _MISSING
    sticky_invalid = False
    env = {}
    class_bindings = {}
    class_aliases = {}
    class_attribute_aliases = {}
    module_dict_aliases = {}
    namespace_aliases = set()

    def advance_module_state(stmt):
        _invalidate_class_bindings(
            class_bindings,
            _module_class_attribute_invalidated_names(
                stmt,
                class_aliases,
                class_attribute_aliases,
                namespace_aliases,
            ),
        )
        _apply_module_stmt_to_env(stmt, env, class_bindings)
        _update_class_aliases(stmt, class_aliases, class_bindings, namespace_aliases)
        _update_class_attribute_aliases(
            stmt,
            class_attribute_aliases,
            class_aliases,
            class_bindings,
            namespace_aliases,
        )
        _update_module_dict_aliases(stmt, name, module_dict_aliases, namespace_aliases)
        _update_namespace_aliases(stmt, namespace_aliases)

    for stmt in tree.body:
        class_body_module_mutations = (
            _class_body_module_mutation_names(stmt) if isinstance(stmt, ast.ClassDef) else set()
        )
        class_attr_names = _module_class_attribute_invalidated_names(
            stmt,
            class_aliases,
            class_attribute_aliases,
            namespace_aliases,
        )
        if (
            value not in (_MISSING, _INVALID)
            and class_attr_names
            and value_invalidated_by_names(value, class_attr_names)
        ):
            value = _INVALID
            sticky_invalid = True
        if _name_invalidated_by(name, _mutating_call_target_names(stmt)):
            value = _INVALID
            sticky_invalid = True
        if _name_invalidated_by(name, class_body_module_mutations):
            value = _INVALID
            sticky_invalid = True
        if _name_invalidated_by(name, _arbitrary_call_observed_names(stmt)):
            value = _INVALID
            sticky_invalid = True
        if value not in (_MISSING, _INVALID) and _has_arbitrary_call(stmt):
            value = _INVALID
            sticky_invalid = True
        if _name_invalidated_by(name, _namespace_alias_mutation_target_names(stmt, namespace_aliases)):
            value = _INVALID
            sticky_invalid = True
        if _module_dict_alias_invalidated(stmt, module_dict_aliases):
            value = _INVALID
            sticky_invalid = True
        if isinstance(stmt, ast.Assign):
            if not _name_is_assigned(stmt, name):
                if isinstance(stmt.value, ast.Name) and stmt.value.id == name:
                    value = _INVALID
                    sticky_invalid = True
                advance_module_state(stmt)
                continue
            if sticky_invalid:
                value = _INVALID
            elif len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                try:
                    value = _module_dict_entries(stmt.value, env, class_bindings, value_converter)
                except UnsupportedStaticExpression:
                    value = _INVALID
                    sticky_invalid = True
            else:
                value = _INVALID
                sticky_invalid = True
            advance_module_state(stmt)
            continue
        if isinstance(stmt, ast.AnnAssign):
            if not _name_is_assigned(stmt, name):
                if isinstance(stmt.value, ast.Name) and stmt.value.id == name:
                    value = _INVALID
                    sticky_invalid = True
                advance_module_state(stmt)
                continue
            if sticky_invalid:
                value = _INVALID
            elif isinstance(stmt.target, ast.Name) and stmt.value is not None:
                try:
                    value = _module_dict_entries(stmt.value, env, class_bindings, value_converter)
                except UnsupportedStaticExpression:
                    value = _INVALID
                    sticky_invalid = True
            else:
                value = _INVALID
                sticky_invalid = True
            advance_module_state(stmt)
            continue
        if isinstance(stmt, ast.AugAssign):
            if _name_is_assigned(stmt, name):
                value = _INVALID
                sticky_invalid = True
            advance_module_state(stmt)
            continue
        if isinstance(stmt, ast.Delete):
            if name in _delete_target_names(stmt):
                value = _INVALID
                sticky_invalid = True
            advance_module_state(stmt)
            continue
        if isinstance(stmt, ast.Expr):
            if name in _mutating_call_target_names(stmt):
                value = _INVALID
                sticky_invalid = True
            if name in _bound_names(stmt):
                value = _INVALID
                sticky_invalid = True
            advance_module_state(stmt)
            continue
        if isinstance(stmt, _CONTROL_FLOW_TYPES):
            if name in _assigned_names_in_control_flow(stmt):
                value = _INVALID
                sticky_invalid = True
            if _has_wildcard_import_in_control_flow(stmt):
                value = _INVALID
                sticky_invalid = True
            advance_module_state(stmt)
            continue
        if _has_wildcard_import(stmt):
            value = _INVALID
            sticky_invalid = True
            advance_module_state(stmt)
            continue
        if name in _bound_names(stmt):
            value = _INVALID
            sticky_invalid = True
        advance_module_state(stmt)
    if return_state:
        return value
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
    if not all(isinstance(node_type, str) for node_type in mappings):
        return {}
    return {node_type: binding for node_type, (_class_name, binding) in mappings.items() if node_type}


def _literal_module_dict_string_keys(node, env):
    keys, _ambiguous = _literal_module_dict_string_keys_state(node, env)
    return keys


def _literal_module_dict_string_keys_state(node, env):
    if not isinstance(node, ast.Dict):
        return set(), False
    keys = set()
    ambiguous = False
    for key in node.keys:
        if key is None:
            ambiguous = True
            continue
        try:
            key_value = _literal(key, env)
        except UnsupportedStaticExpression:
            ambiguous = True
            continue
        if isinstance(key_value, str) and key_value:
            keys.add(key_value)
    return keys, ambiguous


def _mapping_subscript_target_key_state(target, mapping_name, env, aliases=None, namespace_aliases=None):
    if not isinstance(target, ast.Subscript):
        return None, False
    if not _module_dict_alias_sources(
        target.value,
        mapping_name,
        aliases or {},
        namespace_aliases or set(),
    ):
        return None, False
    try:
        key_value = _literal(target.slice, env)
    except UnsupportedStaticExpression:
        return None, True
    return (key_value, False) if isinstance(key_value, str) and key_value else (None, False)


def _node_class_mapping_mutation_string_keys(stmt, env, aliases=None, namespace_aliases=None):
    keys = set()
    ambiguous = False
    aliases = aliases or {}
    namespace_aliases = namespace_aliases or set()

    class MappingMutationKeyVisitor(ast.NodeVisitor):
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

        def visit_Assign(self, node):
            nonlocal ambiguous
            for target in node.targets:
                key, key_ambiguous = _mapping_subscript_target_key_state(
                    target,
                    "NODE_CLASS_MAPPINGS",
                    env,
                    aliases,
                    namespace_aliases,
                )
                ambiguous = ambiguous or key_ambiguous
                if key is not None:
                    keys.add(key)
            self.visit(node.value)

        def visit_AnnAssign(self, node):
            nonlocal ambiguous
            key, key_ambiguous = _mapping_subscript_target_key_state(
                node.target,
                "NODE_CLASS_MAPPINGS",
                env,
                aliases,
                namespace_aliases,
            )
            ambiguous = ambiguous or key_ambiguous
            if key is not None:
                keys.add(key)
            if node.value is not None:
                self.visit(node.value)

        def visit_AugAssign(self, node):
            nonlocal ambiguous
            key, key_ambiguous = _mapping_subscript_target_key_state(
                node.target,
                "NODE_CLASS_MAPPINGS",
                env,
                aliases,
                namespace_aliases,
            )
            ambiguous = ambiguous or key_ambiguous
            if key is not None:
                keys.add(key)
            self.visit(node.value)

        def visit_Call(self, node):
            nonlocal ambiguous
            if (
                isinstance(node.func, ast.Attribute)
                and _module_dict_alias_sources(
                    node.func.value,
                    "NODE_CLASS_MAPPINGS",
                    aliases,
                    namespace_aliases,
                )
            ):
                if node.func.attr == "update":
                    for arg in node.args:
                        if isinstance(arg, ast.Dict):
                            arg_keys, arg_ambiguous = _literal_module_dict_string_keys_state(arg, env)
                            keys.update(arg_keys)
                            ambiguous = ambiguous or arg_ambiguous
                        else:
                            ambiguous = True
                    for keyword in node.keywords:
                        if keyword.arg:
                            keys.add(keyword.arg)
                        else:
                            ambiguous = True
                elif node.func.attr == "setdefault" and node.args:
                    try:
                        key_value = _literal(node.args[0], env)
                    except UnsupportedStaticExpression:
                        key_value = None
                        ambiguous = True
                    if isinstance(key_value, str) and key_value:
                        keys.add(key_value)
                elif node.func.attr == "__setitem__" and node.args:
                    try:
                        key_value = _literal(node.args[0], env)
                    except UnsupportedStaticExpression:
                        key_value = None
                        ambiguous = True
                    if isinstance(key_value, str) and key_value:
                        keys.add(key_value)
            self.generic_visit(node)

    MappingMutationKeyVisitor().visit(stmt)
    return _INVALID if ambiguous else keys


def _node_class_mapping_keys(tree):
    if _has_module_wildcard_import(tree):
        return _INVALID
    keys = set()
    env = {}
    class_bindings = {}
    module_dict_aliases = {}
    namespace_aliases = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and _name_is_assigned(stmt, "NODE_CLASS_MAPPINGS"):
            if not isinstance(stmt.value, ast.Dict):
                return _INVALID
            literal_keys, literal_ambiguous = _literal_module_dict_string_keys_state(stmt.value, env)
            keys.update(literal_keys)
            if literal_ambiguous:
                return _INVALID
        elif (
            isinstance(stmt, ast.AnnAssign)
            and _name_is_assigned(stmt, "NODE_CLASS_MAPPINGS")
            and stmt.value is not None
        ):
            if not isinstance(stmt.value, ast.Dict):
                return _INVALID
            literal_keys, literal_ambiguous = _literal_module_dict_string_keys_state(stmt.value, env)
            keys.update(literal_keys)
            if literal_ambiguous:
                return _INVALID
        if _module_dict_alias_invalidated(stmt, module_dict_aliases):
            return _INVALID
        mutation_keys = _node_class_mapping_mutation_string_keys(
            stmt,
            env,
            module_dict_aliases,
            namespace_aliases,
        )
        if mutation_keys is _INVALID:
            return _INVALID
        keys.update(mutation_keys)
        namespace_mutations = _namespace_alias_mutation_target_names(stmt, namespace_aliases)
        if _name_invalidated_by("NODE_CLASS_MAPPINGS", namespace_mutations):
            return _INVALID
        _apply_module_stmt_to_env(stmt, env, class_bindings)
        _update_module_dict_aliases(
            stmt,
            "NODE_CLASS_MAPPINGS",
            module_dict_aliases,
            namespace_aliases,
        )
        _update_namespace_aliases(stmt, namespace_aliases)
    return keys


def _display_mappings(tree):
    displays = _final_module_dict(
        tree,
        "NODE_DISPLAY_NAME_MAPPINGS",
        lambda value, env, _class_bindings: _literal(value, env),
        return_state=True,
    )
    if displays is _MISSING:
        return {}
    if displays is _INVALID:
        return _INVALID
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in displays.items()):
        return _INVALID
    return displays


def _signature_from_class(node_type, cls, display, pack_meta, class_env, input_env):
    input_types = _input_types(cls, input_env, class_env)
    return_types = _class_attr(cls, "RETURN_TYPES", class_env)
    return_names = _class_attr(cls, "RETURN_NAMES", class_env)
    if return_types is _INVALID or return_names is _INVALID:
        return None
    if not isinstance(input_types, dict) or not isinstance(return_types, (list, tuple)):
        return None

    inputs = {}
    required = []
    for section in ("required", "optional"):
        if section in input_types:
            values = input_types[section]
            if not isinstance(values, dict):
                return None
        else:
            values = {}
        for name, spec in values.items():
            if not isinstance(name, str):
                return None
            if name in inputs:
                return None
            input_type = normalise_input_spec(spec)
            if input_type is None:
                return None
            inputs[name] = input_type
            if section == "required":
                required.append(name)

    output_names = []
    if return_names is _MISSING:
        output_names = []
    elif isinstance(return_names, (list, tuple)):
        if not all(isinstance(name, str) for name in return_names):
            return None
        output_names = list(return_names)
    else:
        return None

    if not all(isinstance(value, str) for value in return_types):
        return None

    return {
        "type": node_type,
        "display": display or node_type,
        "pack": pack_meta["id"],
        "repository": pack_meta.get("repository", ""),
        "inputs": inputs,
        "required": required,
        "outputs": list(return_types),
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
    node_sources = {}
    duplicate_node_types = set()
    for path in sorted(_python_files(repo_dir)):
        tree = _parse_python_file(path)
        if tree is None:
            continue
        env = _collect_module_env(tree)
        mappings = _node_class_mappings(tree)
        mapping_node_types = _node_class_mapping_keys(tree)
        if mapping_node_types is _INVALID:
            nodes = {}
            break
        displays = _display_mappings(tree)
        for node_type in sorted(mapping_node_types):
            prior_path = node_sources.get(node_type)
            if prior_path is not None and prior_path != path:
                duplicate_node_types.add(node_type)
                nodes.pop(node_type, None)
                continue
            node_sources.setdefault(node_type, path)
        if displays is _INVALID:
            continue
        for node_type, binding in sorted(mappings.items()):
            prior_path = node_sources.get(node_type)
            if prior_path is not None and prior_path != path:
                duplicate_node_types.add(node_type)
                nodes.pop(node_type, None)
                continue
            node_sources.setdefault(node_type, path)
            if node_type in duplicate_node_types:
                continue
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


def _format_generated_at(generated_at):
    if isinstance(generated_at, datetime):
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=timezone.utc)
        else:
            generated_at = generated_at.astimezone(timezone.utc)
        return generated_at.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return str(generated_at)


def write_artifact(path, sources, packs, nodes, *, generated_at=DEFAULT_GENERATED_AT):
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _format_generated_at(generated_at),
        "sources": _sorted_json_value(sources),
        "packs": _sorted_json_value(packs),
        "nodes": _sorted_json_value(nodes),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
