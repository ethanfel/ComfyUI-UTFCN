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
    def _normalise_generated_at(self, text):
        parsed = json.loads(text)
        return text.replace(parsed["generated_at"], "<generated-at>")

    def _extract_source(self, source, pack_id="sample-pack"):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "__init__.py").write_text(textwrap.dedent(source), encoding="utf-8")
            return extract_repo_signatures(
                Path(tmp),
                {
                    "id": pack_id,
                    "title": "Sample Pack",
                    "repository": f"https://github.com/example/{pack_id}",
                    "rank": 1,
                },
            )

    def _skip_if_syntax_unsupported(self, source):
        try:
            compile(textwrap.dedent(source), "<test-source>", "exec")
        except SyntaxError as exc:
            self.skipTest(f"syntax unsupported by this Python: {exc.msg}")

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

    def test_skips_unparseable_python_files_and_extracts_static_nodes(self):
        good_source = '''
class GoodNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "GoodNode": GoodNode,
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "bad.py").write_bytes(b"class Bad:\xff\n")
            Path(tmp, "good.py").write_text(textwrap.dedent(good_source), encoding="utf-8")
            result = extract_repo_signatures(
                Path(tmp),
                {
                    "id": "mixed-pack",
                    "title": "Mixed Pack",
                    "repository": "https://github.com/example/mixed-pack",
                    "rank": 1,
                },
            )

        self.assertIn("GoodNode", result["nodes"])
        self.assertEqual("ok", result["pack"]["status"])

    def test_skips_undecodable_python_files_without_modified_parse(self):
        undecodable_source = b'''
# invalid byte follows: \xff
class UndecodableNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "UndecodableNode": UndecodableNode,
}
'''
        good_source = '''
class GoodUtf8Node:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "GoodUtf8Node": GoodUtf8Node,
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "bad.py").write_bytes(undecodable_source)
            Path(tmp, "good.py").write_text(textwrap.dedent(good_source), encoding="utf-8")
            result = extract_repo_signatures(
                Path(tmp),
                {
                    "id": "undecodable-pack",
                    "title": "Undecodable Pack",
                    "repository": "https://github.com/example/undecodable-pack",
                    "rank": 1,
                },
            )

        self.assertNotIn("UndecodableNode", result["nodes"])
        self.assertIn("GoodUtf8Node", result["nodes"])
        self.assertEqual("ok", result["pack"]["status"])

    def test_duplicate_node_ids_across_files_are_skipped(self):
        source_a = '''
class FirstDupNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "DupNode": FirstDupNode,
}
'''
        source_b = '''
class SecondDupNode:
    RETURN_TYPES = ("MASK",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "DupNode": SecondDupNode,
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "a.py").write_text(textwrap.dedent(source_a), encoding="utf-8")
            Path(tmp, "b.py").write_text(textwrap.dedent(source_b), encoding="utf-8")
            result = extract_repo_signatures(
                Path(tmp),
                {
                    "id": "duplicate-node-pack",
                    "title": "Duplicate Node Pack",
                    "repository": "https://github.com/example/duplicate-node-pack",
                    "rank": 1,
                },
            )

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_duplicate_node_id_with_unsupported_mapping_value_skips_static_node(self):
        source_a = '''
class StaticDupNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "DupNode": StaticDupNode,
}
'''
        source_b = '''
def build_node():
    return object()


NODE_CLASS_MAPPINGS = {
    "DupNode": build_node(),
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "a.py").write_text(textwrap.dedent(source_a), encoding="utf-8")
            Path(tmp, "b.py").write_text(textwrap.dedent(source_b), encoding="utf-8")
            result = extract_repo_signatures(
                Path(tmp),
                {
                    "id": "unsupported-duplicate-node-pack",
                    "title": "Unsupported Duplicate Node Pack",
                    "repository": "https://github.com/example/unsupported-duplicate-node-pack",
                    "rank": 1,
                },
            )

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_unsupported_reassignment_invalidates_static_env_value(self):
        source = '''
def build_inputs():
    return {"required": {"image": ("IMAGE",)}}


INPUTS = {
    "required": {
        "image": ("IMAGE",),
    },
}
INPUTS = build_inputs()


class StaleEnvNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return INPUTS


NODE_CLASS_MAPPINGS = {
    "StaleEnvNode": StaleEnvNode,
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "__init__.py").write_text(textwrap.dedent(source), encoding="utf-8")
            result = extract_repo_signatures(
                Path(tmp),
                {
                    "id": "stale-env-pack",
                    "title": "Stale Env Pack",
                    "repository": "https://github.com/example/stale-env-pack",
                    "rank": 1,
                },
            )

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_function_binding_invalidates_static_env_value(self):
        source = '''
INPUTS = {
    "required": {
        "image": ("IMAGE",),
    },
}


def INPUTS():
    return {}


class FunctionRebindNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return INPUTS


NODE_CLASS_MAPPINGS = {
    "FunctionRebindNode": FunctionRebindNode,
}
'''
        result = self._extract_source(source, "function-rebind-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_class_binding_invalidates_static_env_value(self):
        source = '''
INPUTS = {
    "required": {
        "image": ("IMAGE",),
    },
}


class INPUTS:
    pass


class ClassRebindNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return INPUTS


NODE_CLASS_MAPPINGS = {
    "ClassRebindNode": ClassRebindNode,
}
'''
        result = self._extract_source(source, "class-rebind-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_import_binding_invalidates_static_env_value(self):
        source = '''
INPUTS = {
    "required": {
        "image": ("IMAGE",),
    },
}
import something as INPUTS


class ImportRebindNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return INPUTS


NODE_CLASS_MAPPINGS = {
    "ImportRebindNode": ImportRebindNode,
}
'''
        result = self._extract_source(source, "import-rebind-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_alias_mutation_invalidates_static_source_value(self):
        source = '''
INPUTS = {
    "required": {
        "image": ("IMAGE",),
    },
}
ALIAS = INPUTS
ALIAS.clear()


class AliasMutatedInputNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return INPUTS


NODE_CLASS_MAPPINGS = {
    "AliasMutatedInputNode": AliasMutatedInputNode,
}
'''
        result = self._extract_source(source, "alias-mutated-input-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_annotated_alias_mutation_invalidates_static_source_value(self):
        source = '''
INPUTS = {
    "required": {
        "image": ("IMAGE",),
    },
}
ALIAS: dict = INPUTS
ALIAS.clear()


class AnnotatedAliasMutatedInputNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return INPUTS


NODE_CLASS_MAPPINGS = {
    "AnnotatedAliasMutatedInputNode": AnnotatedAliasMutatedInputNode,
}
'''
        result = self._extract_source(source, "annotated-alias-mutated-input-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_wildcard_import_invalidates_static_env_values(self):
        source = '''
INPUTS = {
    "required": {
        "image": ("IMAGE",),
    },
}
from something import *


class WildcardImportInputNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return INPUTS


NODE_CLASS_MAPPINGS = {
    "WildcardImportInputNode": WildcardImportInputNode,
}
'''
        result = self._extract_source(source, "wildcard-import-input-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_nested_wildcard_import_invalidates_static_env_values(self):
        source = '''
INPUTS = {
    "required": {
        "image": ("IMAGE",),
    },
}
if True:
    from something import *


class NestedWildcardImportInputNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return INPUTS


NODE_CLASS_MAPPINGS = {
    "NestedWildcardImportInputNode": NestedWildcardImportInputNode,
}
'''
        result = self._extract_source(source, "nested-wildcard-import-input-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_annotated_reassignment_invalidates_static_env_value(self):
        source = '''
def build_inputs():
    return {"required": {"image": ("IMAGE",)}}


INPUTS = {
    "required": {
        "image": ("IMAGE",),
    },
}
INPUTS: dict = build_inputs()


class AnnotatedRebindNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return INPUTS


NODE_CLASS_MAPPINGS = {
    "AnnotatedRebindNode": AnnotatedRebindNode,
}
'''
        result = self._extract_source(source, "annotated-rebind-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_multi_target_reassignment_invalidates_static_env_value(self):
        source = '''
def build_inputs():
    return {"required": {"image": ("IMAGE",)}}


INPUTS = {
    "required": {
        "image": ("IMAGE",),
    },
}
OTHER = INPUTS = build_inputs()


class MultiTargetRebindNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return INPUTS


NODE_CLASS_MAPPINGS = {
    "MultiTargetRebindNode": MultiTargetRebindNode,
}
'''
        result = self._extract_source(source, "multi-target-rebind-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_augmented_assignment_invalidates_static_env_value(self):
        source = '''
INPUTS = {
    "required": {
        "image": ("IMAGE",),
    },
}
INPUTS += ({},)


class AugmentedRebindNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return INPUTS


NODE_CLASS_MAPPINGS = {
    "AugmentedRebindNode": AugmentedRebindNode,
}
'''
        result = self._extract_source(source, "augmented-rebind-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_control_flow_assignment_invalidates_static_env_value(self):
        source = '''
def build_inputs():
    return {"required": {"image": ("IMAGE",)}}


INPUTS = {
    "required": {
        "image": ("IMAGE",),
    },
}
if True:
    INPUTS = build_inputs()


class ControlFlowRebindNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return INPUTS


NODE_CLASS_MAPPINGS = {
    "ControlFlowRebindNode": ControlFlowRebindNode,
}
'''
        result = self._extract_source(source, "control-flow-rebind-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_except_handler_binding_invalidates_static_env_value(self):
        source = '''
INPUTS = {
    "required": {
        "image": ("IMAGE",),
    },
}
try:
    pass
except Exception as INPUTS:
    pass


class ExceptHandlerBoundInputEnvNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return INPUTS


NODE_CLASS_MAPPINGS = {
    "ExceptHandlerBoundInputEnvNode": ExceptHandlerBoundInputEnvNode,
}
'''
        result = self._extract_source(source, "except-handler-bound-input-env-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_trystar_assignment_invalidates_static_env_value(self):
        source = '''
def build_inputs():
    return {"required": {"mask": ("MASK",)}}


INPUTS = {
    "required": {
        "image": ("IMAGE",),
    },
}
try:
    pass
except* RuntimeError:
    INPUTS = build_inputs()


class TryStarRebindInputEnvNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return INPUTS


NODE_CLASS_MAPPINGS = {
    "TryStarRebindInputEnvNode": TryStarRebindInputEnvNode,
}
'''
        self._skip_if_syntax_unsupported(source)
        result = self._extract_source(source, "trystar-rebind-input-env-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_type_alias_binding_invalidates_static_env_value(self):
        source = '''
INPUTS = {
    "required": {
        "image": ("IMAGE",),
    },
}
type INPUTS = int


class TypeAliasBoundInputEnvNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return INPUTS


NODE_CLASS_MAPPINGS = {
    "TypeAliasBoundInputEnvNode": TypeAliasBoundInputEnvNode,
}
'''
        self._skip_if_syntax_unsupported(source)
        result = self._extract_source(source, "type-alias-bound-input-env-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_delete_invalidates_static_env_value(self):
        source = '''
INPUTS = {
    "required": {
        "image": ("IMAGE",),
    },
}
del INPUTS


class DeletedInputEnvNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return INPUTS


NODE_CLASS_MAPPINGS = {
    "DeletedInputEnvNode": DeletedInputEnvNode,
}
'''
        result = self._extract_source(source, "deleted-input-env-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_rhs_mutating_call_invalidates_static_env_value(self):
        source = '''
INPUTS = {
    "required": {
        "image": ("IMAGE",),
    },
}
X = INPUTS.clear()


class RhsMutatedInputEnvNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return INPUTS


NODE_CLASS_MAPPINGS = {
    "RhsMutatedInputEnvNode": RhsMutatedInputEnvNode,
}
'''
        result = self._extract_source(source, "rhs-mutated-input-env-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_arbitrary_call_observing_mutable_env_value_invalidates_static_env_value(self):
        source = '''
INPUTS = {
    "required": {
        "image": ("IMAGE",),
    },
}
observe(INPUTS)


class ObservedInputEnvNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return INPUTS


NODE_CLASS_MAPPINGS = {
    "ObservedInputEnvNode": ObservedInputEnvNode,
}
'''
        result = self._extract_source(source, "observed-input-env-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_function_default_mutation_invalidates_static_env_value(self):
        source = '''
INPUTS = {
    "required": {
        "image": ("IMAGE",),
    },
}


def helper(x=INPUTS.clear()):
    pass


class DefaultMutatedInputEnvNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return INPUTS


NODE_CLASS_MAPPINGS = {
    "DefaultMutatedInputEnvNode": DefaultMutatedInputEnvNode,
}
'''
        result = self._extract_source(source, "default-mutated-input-env-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_function_decorator_mutation_invalidates_static_env_value(self):
        source = '''
def decorator(value):
    def wrap(fn):
        return fn
    return wrap


INPUTS = {
    "required": {
        "image": ("IMAGE",),
    },
}


@decorator(INPUTS.clear())
def helper():
    pass


class DecoratorMutatedInputEnvNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return INPUTS


NODE_CLASS_MAPPINGS = {
    "DecoratorMutatedInputEnvNode": DecoratorMutatedInputEnvNode,
}
'''
        result = self._extract_source(source, "decorator-mutated-input-env-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_class_body_function_default_mutation_invalidates_static_input_env(self):
        source = '''
INPUTS = {
    "required": {
        "image": ("IMAGE",),
    },
}


class ClassDefaultMutatedInputEnvNode:
    RETURN_TYPES = ("IMAGE",)

    def helper(x=INPUTS.clear()):
        pass

    @classmethod
    def INPUT_TYPES(cls):
        return INPUTS


NODE_CLASS_MAPPINGS = {
    "ClassDefaultMutatedInputEnvNode": ClassDefaultMutatedInputEnvNode,
}
'''
        result = self._extract_source(source, "class-default-mutated-input-env-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_class_body_function_decorator_mutation_invalidates_static_input_env(self):
        source = '''
def decorator(value):
    def wrap(fn):
        return fn
    return wrap


INPUTS = {
    "required": {
        "image": ("IMAGE",),
    },
}


class ClassDecoratorMutatedInputEnvNode:
    RETURN_TYPES = ("IMAGE",)

    @decorator(INPUTS.clear())
    def helper(self):
        pass

    @classmethod
    def INPUT_TYPES(cls):
        return INPUTS


NODE_CLASS_MAPPINGS = {
    "ClassDecoratorMutatedInputEnvNode": ClassDecoratorMutatedInputEnvNode,
}
'''
        result = self._extract_source(source, "class-decorator-mutated-input-env-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_class_body_function_body_mutation_does_not_invalidate_static_input_env(self):
        source = '''
INPUTS = {
    "required": {
        "image": ("IMAGE",),
    },
}


class RuntimeBodyMutatedInputEnvNode:
    RETURN_TYPES = ("IMAGE",)

    def helper(self):
        INPUTS.clear()

    @classmethod
    def INPUT_TYPES(cls):
        return INPUTS


NODE_CLASS_MAPPINGS = {
    "RuntimeBodyMutatedInputEnvNode": RuntimeBodyMutatedInputEnvNode,
}
'''
        result = self._extract_source(source, "runtime-body-mutated-input-env-pack")

        self.assertIn("RuntimeBodyMutatedInputEnvNode", result["nodes"])
        self.assertEqual({"image": "IMAGE"}, result["nodes"]["RuntimeBodyMutatedInputEnvNode"]["inputs"])
        self.assertEqual("ok", result["pack"]["status"])

    def test_class_body_global_assignment_invalidates_static_input_env(self):
        source = '''
INPUTS = {
    "required": {
        "image": ("IMAGE",),
    },
}


def build_inputs():
    return {
        "required": {
            "mask": ("MASK",),
        },
    }


class MutatesModuleAtDefinition:
    global INPUTS
    INPUTS = build_inputs()


class ClassGlobalMutatedInputEnvNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return INPUTS


NODE_CLASS_MAPPINGS = {
    "ClassGlobalMutatedInputEnvNode": ClassGlobalMutatedInputEnvNode,
}
'''
        result = self._extract_source(source, "class-global-mutated-input-env-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_nested_mutable_env_literal_skips_static_node(self):
        source = '''
REQ = {
    "image": ("IMAGE",),
}
INPUTS = {
    "required": REQ,
}
REQ.clear()


class NestedMutableEnvLiteralNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return INPUTS


NODE_CLASS_MAPPINGS = {
    "NestedMutableEnvLiteralNode": NestedMutableEnvLiteralNode,
}
'''
        result = self._extract_source(source, "nested-mutable-env-literal-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_nested_mutable_env_subscript_alias_skips_static_node(self):
        source = '''
INPUTS = {
    "required": {
        "image": ("IMAGE",),
    },
}
REQ = INPUTS["required"]
REQ.clear()


class NestedMutableEnvSubscriptAliasNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return INPUTS


NODE_CLASS_MAPPINGS = {
    "NestedMutableEnvSubscriptAliasNode": NestedMutableEnvSubscriptAliasNode,
}
'''
        result = self._extract_source(source, "nested-mutable-env-subscript-alias-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_unhashable_literal_input_key_skips_repo_without_raising(self):
        source = '''
INPUTS = {
    ["bad"]: ("IMAGE",),
}


class UnhashableLiteralInputKeyNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return INPUTS


NODE_CLASS_MAPPINGS = {
    "UnhashableLiteralInputKeyNode": UnhashableLiteralInputKeyNode,
}
'''
        result = self._extract_source(source, "unhashable-literal-input-key-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_post_class_input_reassignment_skips_static_node(self):
        source = '''
def build_inputs():
    return {"required": {"mask": ("MASK",)}}


INPUTS = {
    "required": {
        "image": ("IMAGE",),
    },
}


class PostClassInputRebindNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return INPUTS


INPUTS = build_inputs()

NODE_CLASS_MAPPINGS = {
    "PostClassInputRebindNode": PostClassInputRebindNode,
}
'''
        result = self._extract_source(source, "post-class-input-rebind-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_direct_literal_input_types_survives_post_class_env_changes(self):
        source = '''
INPUTS = build_inputs()


class LiteralInputTypesAfterEnvChangeNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


INPUTS = build_inputs()

NODE_CLASS_MAPPINGS = {
    "LiteralInputTypesAfterEnvChangeNode": LiteralInputTypesAfterEnvChangeNode,
}
'''
        result = self._extract_source(source, "literal-input-after-env-change-pack")

        self.assertIn("LiteralInputTypesAfterEnvChangeNode", result["nodes"])
        self.assertEqual("ok", result["pack"]["status"])

    def test_later_dynamic_input_types_binding_skips_node(self):
        source = '''
def build_inputs():
    return {"required": {"mask": ("MASK",)}}


class LaterDynamicInputTypesNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }

    def INPUT_TYPES(cls):
        return build_inputs()


NODE_CLASS_MAPPINGS = {
    "LaterDynamicInputTypesNode": LaterDynamicInputTypesNode,
}
'''
        result = self._extract_source(source, "later-dynamic-input-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_input_types_default_referencing_return_types_skips_node(self):
        source = '''
class DefaultReferencesReturnTypesInputNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls, value=RETURN_TYPES):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "DefaultReferencesReturnTypesInputNode": DefaultReferencesReturnTypesInputNode,
}
'''
        result = self._extract_source(source, "default-references-return-types-input-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_input_types_return_annotation_referencing_input_types_skips_node(self):
        source = '''
class AnnotationReferencesInputTypesNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls) -> INPUT_TYPES:
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "AnnotationReferencesInputTypesNode": AnnotationReferencesInputTypesNode,
}
'''
        result = self._extract_source(source, "annotation-references-input-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_decorated_input_types_skips_node(self):
        source = '''
def replace(fn):
    def replacement(cls):
        return {
            "required": {
                "mask": ("MASK",),
            },
        }
    return replacement


class DecoratedInputTypesNode:
    RETURN_TYPES = ("IMAGE",)

    @replace
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "DecoratedInputTypesNode": DecoratedInputTypesNode,
}
'''
        result = self._extract_source(source, "decorated-input-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_shadowed_classmethod_decorator_skips_node(self):
        source = '''
def classmethod(fn):
    def replacement(cls):
        return {
            "required": {
                "mask": ("MASK",),
            },
        }
    return replacement


class ShadowedClassmethodInputTypesNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "ShadowedClassmethodInputTypesNode": ShadowedClassmethodInputTypesNode,
}
'''
        result = self._extract_source(source, "shadowed-classmethod-input-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_input_types_with_present_non_dict_sections_skips_node(self):
        source = '''
class InvalidInputSectionsNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": [],
            "optional": None,
        }


NODE_CLASS_MAPPINGS = {
    "InvalidInputSectionsNode": InvalidInputSectionsNode,
}
'''
        result = self._extract_source(source, "invalid-input-sections-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_input_types_with_duplicate_required_optional_name_skips_node(self):
        source = '''
class DuplicateInputNameNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "x": ("IMAGE",),
            },
            "optional": {
                "x": ("MASK",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "DuplicateInputNameNode": DuplicateInputNameNode,
}
'''
        result = self._extract_source(source, "duplicate-input-name-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_input_types_with_non_string_input_name_skips_node(self):
        source = '''
class NonStringInputNameNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                1: ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "NonStringInputNameNode": NonStringInputNameNode,
}
'''
        result = self._extract_source(source, "non-string-input-name-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_input_types_with_non_string_input_type_skips_node(self):
        source = '''
class NonStringInputTypeNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": (2,),
            },
        }


NODE_CLASS_MAPPINGS = {
    "NonStringInputTypeNode": NonStringInputTypeNode,
}
'''
        result = self._extract_source(source, "non-string-input-type-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_dynamic_return_types_reassignment_skips_node(self):
        source = '''
def build_outputs():
    return ("MASK",)


class DynamicReturnTypesNode:
    RETURN_TYPES = ("IMAGE",)
    RETURN_TYPES = build_outputs()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "DynamicReturnTypesNode": DynamicReturnTypesNode,
}
'''
        result = self._extract_source(source, "dynamic-return-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_delete_return_types_skips_node(self):
        source = '''
class DeletedReturnTypesNode:
    RETURN_TYPES = ("IMAGE",)
    del RETURN_TYPES

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "DeletedReturnTypesNode": DeletedReturnTypesNode,
}
'''
        result = self._extract_source(source, "deleted-return-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_mutated_return_types_skips_node(self):
        source = '''
class MutatedReturnTypesNode:
    RETURN_TYPES = ["IMAGE"]
    RETURN_TYPES.clear()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "MutatedReturnTypesNode": MutatedReturnTypesNode,
}
'''
        result = self._extract_source(source, "mutated-return-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_rhs_mutating_call_to_return_types_skips_node(self):
        source = '''
class RhsMutatedReturnTypesNode:
    RETURN_TYPES = ["IMAGE"]
    X = RETURN_TYPES.pop()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "RhsMutatedReturnTypesNode": RhsMutatedReturnTypesNode,
}
'''
        result = self._extract_source(source, "rhs-mutated-return-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_function_default_walrus_to_return_types_skips_node(self):
        source = '''
class DefaultWalrusReturnTypesNode:
    RETURN_TYPES = ("IMAGE",)

    def helper(self, x=(RETURN_TYPES := ("MASK",))):
        pass

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "DefaultWalrusReturnTypesNode": DefaultWalrusReturnTypesNode,
}
'''
        result = self._extract_source(source, "default-walrus-return-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_function_default_mutation_to_return_types_skips_node(self):
        source = '''
class DefaultMutatedReturnTypesNode:
    RETURN_TYPES = ["IMAGE"]

    def helper(self, x=RETURN_TYPES.clear()):
        pass

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "DefaultMutatedReturnTypesNode": DefaultMutatedReturnTypesNode,
}
'''
        result = self._extract_source(source, "default-mutated-return-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_getattr_method_mutation_to_return_types_skips_node(self):
        source = '''
class GetattrMethodMutatedReturnTypesNode:
    RETURN_TYPES = ["IMAGE"]
    getattr(RETURN_TYPES, "clear")()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "GetattrMethodMutatedReturnTypesNode": GetattrMethodMutatedReturnTypesNode,
}
'''
        result = self._extract_source(source, "getattr-method-mutated-return-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_getattr_method_mutation_to_return_names_skips_node(self):
        source = '''
class GetattrMethodMutatedReturnNamesNode:
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ["image"]
    getattr(RETURN_NAMES, "clear")()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "GetattrMethodMutatedReturnNamesNode": GetattrMethodMutatedReturnNamesNode,
}
'''
        result = self._extract_source(source, "getattr-method-mutated-return-names-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_except_handler_binding_to_return_types_skips_node(self):
        source = '''
class ExceptHandlerBoundReturnTypesNode:
    RETURN_TYPES = ("IMAGE",)
    try:
        pass
    except Exception as RETURN_TYPES:
        pass

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "ExceptHandlerBoundReturnTypesNode": ExceptHandlerBoundReturnTypesNode,
}
'''
        result = self._extract_source(source, "except-handler-bound-return-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_return_types_alias_mutation_skips_node(self):
        source = '''
class AliasMutatedReturnTypesNode:
    RETURN_TYPES = ["IMAGE"]
    ALIAS = RETURN_TYPES
    ALIAS.clear()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "AliasMutatedReturnTypesNode": AliasMutatedReturnTypesNode,
}
'''
        result = self._extract_source(source, "alias-mutated-return-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_return_types_chained_alias_mutation_skips_node(self):
        source = '''
class ChainedAliasMutatedReturnTypesNode:
    RETURN_TYPES = ["IMAGE"]
    A = B = RETURN_TYPES
    A.append("MASK")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "ChainedAliasMutatedReturnTypesNode": ChainedAliasMutatedReturnTypesNode,
}
'''
        result = self._extract_source(source, "chained-alias-mutated-return-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_return_types_unpacked_alias_mutation_skips_node(self):
        source = '''
class UnpackedAliasMutatedReturnTypesNode:
    RETURN_TYPES = ["IMAGE"]
    ALIAS, = (RETURN_TYPES,)
    ALIAS.clear()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "UnpackedAliasMutatedReturnTypesNode": UnpackedAliasMutatedReturnTypesNode,
}
'''
        result = self._extract_source(source, "unpacked-alias-mutated-return-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_return_types_starred_unpacked_alias_mutation_skips_node(self):
        source = '''
class StarredUnpackedAliasMutatedReturnTypesNode:
    RETURN_TYPES = ["IMAGE"]
    ALIAS, *REST = (RETURN_TYPES, [], [])
    ALIAS.clear()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "StarredUnpackedAliasMutatedReturnTypesNode": StarredUnpackedAliasMutatedReturnTypesNode,
}
'''
        result = self._extract_source(source, "starred-unpacked-alias-mutated-return-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_return_types_starred_collection_alias_mutation_skips_node(self):
        source = '''
class StarredCollectionAliasMutatedReturnTypesNode:
    RETURN_TYPES = ["IMAGE"]
    *ALIASES, = (RETURN_TYPES,)
    ALIASES[0].clear()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "StarredCollectionAliasMutatedReturnTypesNode": StarredCollectionAliasMutatedReturnTypesNode,
}
'''
        result = self._extract_source(source, "starred-collection-alias-mutated-return-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_return_types_alias_subscript_assignment_skips_node(self):
        source = '''
class AliasSubscriptMutatedReturnTypesNode:
    RETURN_TYPES = ["IMAGE"]
    ALIAS = RETURN_TYPES
    ALIAS[0] = "MASK"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "AliasSubscriptMutatedReturnTypesNode": AliasSubscriptMutatedReturnTypesNode,
}
'''
        result = self._extract_source(source, "alias-subscript-mutated-return-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_return_types_alias_augmented_assignment_skips_node(self):
        source = '''
class AliasAugmentedMutatedReturnTypesNode:
    RETURN_TYPES = ["IMAGE"]
    ALIAS = RETURN_TYPES
    ALIAS += ["MASK"]

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "AliasAugmentedMutatedReturnTypesNode": AliasAugmentedMutatedReturnTypesNode,
}
'''
        result = self._extract_source(source, "alias-augmented-mutated-return-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_return_types_arbitrary_call_skips_node(self):
        source = '''
class ArbitraryCallReturnTypesNode:
    RETURN_TYPES = ["IMAGE"]
    mutate(RETURN_TYPES)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "ArbitraryCallReturnTypesNode": ArbitraryCallReturnTypesNode,
}
'''
        result = self._extract_source(source, "arbitrary-call-return-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_return_types_alias_arbitrary_call_skips_node(self):
        source = '''
class AliasArbitraryCallReturnTypesNode:
    RETURN_TYPES = ["IMAGE"]
    ALIAS = RETURN_TYPES
    mutate(ALIAS)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "AliasArbitraryCallReturnTypesNode": AliasArbitraryCallReturnTypesNode,
}
'''
        result = self._extract_source(source, "alias-arbitrary-call-return-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_return_types_used_as_callee_skips_node(self):
        source = '''
class CalleeObservedReturnTypesNode:
    RETURN_TYPES = ["IMAGE"]
    RETURN_TYPES()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "CalleeObservedReturnTypesNode": CalleeObservedReturnTypesNode,
}
'''
        result = self._extract_source(source, "callee-observed-return-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_return_types_function_default_arbitrary_call_skips_node(self):
        source = '''
class DefaultArbitraryCallReturnTypesNode:
    RETURN_TYPES = ["IMAGE"]

    def helper(value=mutate(RETURN_TYPES)):
        pass

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "DefaultArbitraryCallReturnTypesNode": DefaultArbitraryCallReturnTypesNode,
}
'''
        result = self._extract_source(source, "default-arbitrary-call-return-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_return_names_alias_subscript_assignment_skips_node(self):
        source = '''
class AliasSubscriptMutatedReturnNamesNode:
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ["image"]
    ALIAS = RETURN_NAMES
    ALIAS[0] = "mask"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "AliasSubscriptMutatedReturnNamesNode": AliasSubscriptMutatedReturnNamesNode,
}
'''
        result = self._extract_source(source, "alias-subscript-mutated-return-names-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_return_names_starred_unpacked_alias_mutation_skips_node(self):
        source = '''
class StarredUnpackedAliasMutatedReturnNamesNode:
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ["image"]
    ALIAS, *REST = (RETURN_NAMES, [], [])
    ALIAS.clear()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "StarredUnpackedAliasMutatedReturnNamesNode": StarredUnpackedAliasMutatedReturnNamesNode,
}
'''
        result = self._extract_source(source, "starred-unpacked-alias-mutated-return-names-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_return_types_transitive_alias_mutation_skips_node(self):
        source = '''
class TransitiveAliasMutatedReturnTypesNode:
    RETURN_TYPES = ["IMAGE"]
    A = RETURN_TYPES
    B = A
    B.clear()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "TransitiveAliasMutatedReturnTypesNode": TransitiveAliasMutatedReturnTypesNode,
}
'''
        result = self._extract_source(source, "transitive-alias-mutated-return-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_class_return_types_uses_definition_time_module_env(self):
        source = '''
RETURNS = ("IMAGE",)


class DefinitionTimeReturnTypesNode:
    RETURN_TYPES = RETURNS

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


RETURNS = ("MASK",)

NODE_CLASS_MAPPINGS = {
    "DefinitionTimeReturnTypesNode": DefinitionTimeReturnTypesNode,
}
'''
        result = self._extract_source(source, "definition-time-return-pack")

        self.assertEqual(["IMAGE"], result["nodes"]["DefinitionTimeReturnTypesNode"]["outputs"])
        self.assertEqual("ok", result["pack"]["status"])

    def test_mutable_module_return_types_capture_skips_node(self):
        source = '''
RETURNS = ["IMAGE"]


class MutableModuleReturnTypesNode:
    RETURN_TYPES = RETURNS

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


RETURNS.clear()

NODE_CLASS_MAPPINGS = {
    "MutableModuleReturnTypesNode": MutableModuleReturnTypesNode,
}
'''
        result = self._extract_source(source, "mutable-module-return-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_mutable_module_return_names_capture_skips_node(self):
        source = '''
NAMES = ["image"]


class MutableModuleReturnNamesNode:
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = NAMES

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NAMES.clear()

NODE_CLASS_MAPPINGS = {
    "MutableModuleReturnNamesNode": MutableModuleReturnNamesNode,
}
'''
        result = self._extract_source(source, "mutable-module-return-names-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_subscript_assignment_to_return_types_skips_node(self):
        source = '''
class SubscriptMutatedReturnTypesNode:
    RETURN_TYPES = ["IMAGE"]
    RETURN_TYPES[0] = "MASK"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "SubscriptMutatedReturnTypesNode": SubscriptMutatedReturnTypesNode,
}
'''
        result = self._extract_source(source, "subscript-mutated-return-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_subscript_assignment_to_return_names_skips_node(self):
        source = '''
class SubscriptMutatedReturnNamesNode:
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ["image"]
    RETURN_NAMES[0] = "mask"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "SubscriptMutatedReturnNamesNode": SubscriptMutatedReturnNamesNode,
}
'''
        result = self._extract_source(source, "subscript-mutated-return-names-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_string_return_names_declaration_skips_node(self):
        source = '''
class StringReturnNamesNode:
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = "image"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "StringReturnNamesNode": StringReturnNamesNode,
}
'''
        result = self._extract_source(source, "string-return-names-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_non_string_return_type_entry_skips_node(self):
        source = '''
class NonStringReturnTypeNode:
    RETURN_TYPES = (123,)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "NonStringReturnTypeNode": NonStringReturnTypeNode,
}
'''
        result = self._extract_source(source, "non-string-return-type-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_non_string_return_name_entry_skips_node(self):
        source = '''
class NonStringReturnNameNode:
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = (456,)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "NonStringReturnNameNode": NonStringReturnNameNode,
}
'''
        result = self._extract_source(source, "non-string-return-name-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_missing_return_names_is_allowed(self):
        source = '''
class MissingReturnNamesNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "MissingReturnNamesNode": MissingReturnNamesNode,
}
'''
        result = self._extract_source(source, "missing-return-names-pack")

        self.assertEqual([], result["nodes"]["MissingReturnNamesNode"]["output_names"])
        self.assertEqual("ok", result["pack"]["status"])

    def test_final_static_return_types_assignment_wins(self):
        source = '''
class FinalReturnTypesNode:
    RETURN_TYPES = ("IMAGE",)
    RETURN_TYPES = ("MASK",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "mask": ("MASK",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "FinalReturnTypesNode": FinalReturnTypesNode,
}
'''
        result = self._extract_source(source, "final-return-pack")

        self.assertEqual(["MASK"], result["nodes"]["FinalReturnTypesNode"]["outputs"])
        self.assertEqual("ok", result["pack"]["status"])

    def test_dynamic_node_class_mapping_reassignment_skips_node(self):
        source = '''
def build_mappings():
    return {"DynamicMappingNode": DynamicMappingNode}


class DynamicMappingNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "DynamicMappingNode": DynamicMappingNode,
}
NODE_CLASS_MAPPINGS = build_mappings()
'''
        result = self._extract_source(source, "dynamic-mapping-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_dynamic_node_class_mapping_assignment_stays_invalid_after_static_reassignment(self):
        source = '''
def build_mappings():
    return {"StickyDynamicMappingNode": StickyDynamicMappingNode}


class StickyDynamicMappingNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "StickyDynamicMappingNode": StickyDynamicMappingNode,
}
NODE_CLASS_MAPPINGS = build_mappings()
NODE_CLASS_MAPPINGS = {
    "StickyDynamicMappingNode": StickyDynamicMappingNode,
}
'''
        result = self._extract_source(source, "sticky-dynamic-mapping-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_rebound_node_class_name_skips_static_mapping(self):
        source = '''
def build_node():
    return object()


class ReboundNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


ReboundNode = build_node()

NODE_CLASS_MAPPINGS = {
    "ReboundNode": ReboundNode,
}
'''
        result = self._extract_source(source, "rebound-node-class-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_node_mapping_uses_assignment_time_class_binding(self):
        source = '''
class Node:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "Node": Node,
}


class Node:
    RETURN_TYPES = ("MASK",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "mask": ("MASK",),
            },
        }
'''
        result = self._extract_source(source, "assignment-time-class-binding-pack")

        self.assertEqual(["IMAGE"], result["nodes"]["Node"]["outputs"])
        self.assertEqual({"image": "IMAGE"}, result["nodes"]["Node"]["inputs"])
        self.assertEqual("ok", result["pack"]["status"])

    def test_conditional_class_mapping_skips_node(self):
        source = '''
if True:
    class ConditionalNode:
        RETURN_TYPES = ("IMAGE",)

        @classmethod
        def INPUT_TYPES(cls):
            return {
                "required": {
                    "image": ("IMAGE",),
                },
            }


NODE_CLASS_MAPPINGS = {
    "ConditionalNode": ConditionalNode,
}
'''
        result = self._extract_source(source, "conditional-node-class-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_top_level_class_mapping_still_extracts_node(self):
        source = '''
class TopLevelMappedNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "TopLevelMappedNode": TopLevelMappedNode,
}
'''
        result = self._extract_source(source, "top-level-node-class-pack")

        self.assertIn("TopLevelMappedNode", result["nodes"])
        self.assertEqual("ok", result["pack"]["status"])

    def test_decorated_class_mapping_skips_node(self):
        source = '''
def decorator(cls):
    return cls


@decorator
class DecoratedMappedNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "DecoratedMappedNode": DecoratedMappedNode,
}
'''
        result = self._extract_source(source, "decorated-mapped-class-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_class_with_base_mapping_skips_node(self):
        source = '''
class Base:
    def __init_subclass__(cls):
        cls.RETURN_TYPES = ("MASK",)


class HookedNode(Base):
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "HookedNode": HookedNode,
}
'''
        result = self._extract_source(source, "hooked-base-class-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_class_with_metaclass_mapping_skips_node(self):
        source = '''
class Meta(type):
    def __new__(mcls, name, bases, attrs):
        attrs["RETURN_TYPES"] = ("MASK",)
        return super().__new__(mcls, name, bases, attrs)


class MetaNode(metaclass=Meta):
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "MetaNode": MetaNode,
}
'''
        result = self._extract_source(source, "metaclass-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_class_with_type_params_mapping_skips_node(self):
        source = '''
class TypeParamNode[T]:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "TypeParamNode": TypeParamNode,
}
'''
        self._skip_if_syntax_unsupported(source)
        result = self._extract_source(source, "type-param-class-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_node_mapping_key_uses_assignment_time_env(self):
        source = '''
KEY = "Original"


class Node:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    KEY: Node,
}
KEY = "Wrong"
'''
        result = self._extract_source(source, "assignment-time-key-pack")

        self.assertIn("Original", result["nodes"])
        self.assertNotIn("Wrong", result["nodes"])
        self.assertEqual(["IMAGE"], result["nodes"]["Original"]["outputs"])
        self.assertEqual("ok", result["pack"]["status"])

    def test_non_string_node_mapping_key_skips_node(self):
        source = '''
class NonStringMappingKeyNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    123: NonStringMappingKeyNode,
}
'''
        result = self._extract_source(source, "non-string-mapping-key-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_module_class_return_types_patch_after_mapping_skips_node(self):
        source = '''
class PatchedReturnTypesNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "PatchedReturnTypesNode": PatchedReturnTypesNode,
}
PatchedReturnTypesNode.RETURN_TYPES = ("MASK",)
'''
        result = self._extract_source(source, "patched-return-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_module_class_setattr_patch_after_mapping_skips_node(self):
        source = '''
class SetattrPatchedNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "SetattrPatchedNode": SetattrPatchedNode,
}
setattr(SetattrPatchedNode, "RETURN_TYPES", ("MASK",))
'''
        result = self._extract_source(source, "setattr-patched-node-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_module_class_input_types_patch_after_mapping_skips_node(self):
        source = '''
def build_inputs():
    return {"required": {"mask": ("MASK",)}}


class PatchedInputTypesNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "PatchedInputTypesNode": PatchedInputTypesNode,
}
PatchedInputTypesNode.INPUT_TYPES = build_inputs
'''
        result = self._extract_source(source, "patched-input-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_duplicate_node_mapping_key_with_dynamic_value_skips_node(self):
        source = '''
def build_node():
    return object()


class DuplicateMappingKeyNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "DuplicateMappingKeyNode": DuplicateMappingKeyNode,
    "DuplicateMappingKeyNode": build_node(),
}
'''
        result = self._extract_source(source, "duplicate-mapping-key-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_module_class_alias_patch_after_mapping_skips_node(self):
        source = '''
class AliasPatchedNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "AliasPatchedNode": AliasPatchedNode,
}
Alias = AliasPatchedNode
Alias.RETURN_TYPES = ("MASK",)
'''
        result = self._extract_source(source, "alias-patched-node-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_module_class_chained_alias_patch_after_mapping_skips_node(self):
        source = '''
class ChainedAliasPatchedNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "ChainedAliasPatchedNode": ChainedAliasPatchedNode,
}
A = B = ChainedAliasPatchedNode
A.RETURN_TYPES = ("MASK",)
'''
        result = self._extract_source(source, "chained-alias-patched-node-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_module_class_starred_alias_patch_after_mapping_skips_node(self):
        source = '''
class StarredAliasPatchedNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "StarredAliasPatchedNode": StarredAliasPatchedNode,
}
Alias, *REST = (StarredAliasPatchedNode, object(), object())
Alias.RETURN_TYPES = ("MASK",)
'''
        result = self._extract_source(source, "starred-alias-patched-node-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_module_class_starred_collection_alias_patch_after_mapping_skips_node(self):
        source = '''
class StarredCollectionAliasPatchedNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "StarredCollectionAliasPatchedNode": StarredCollectionAliasPatchedNode,
}
*ALIASES, = (StarredCollectionAliasPatchedNode,)
ALIASES[0].RETURN_TYPES = ("MASK",)
'''
        result = self._extract_source(source, "starred-collection-alias-patched-node-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_module_class_globals_subscript_alias_patch_after_mapping_skips_node(self):
        source = '''
class GlobalsSubscriptAliasPatchedNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "GlobalsSubscriptAliasPatchedNode": GlobalsSubscriptAliasPatchedNode,
}
Alias = globals()["GlobalsSubscriptAliasPatchedNode"]
Alias.RETURN_TYPES = ("MASK",)
'''
        result = self._extract_source(source, "globals-subscript-alias-patched-node-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_module_class_globals_get_alias_patch_after_mapping_skips_node(self):
        source = '''
class GlobalsGetAliasPatchedNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "GlobalsGetAliasPatchedNode": GlobalsGetAliasPatchedNode,
}
Alias = globals().get("GlobalsGetAliasPatchedNode")
Alias.RETURN_TYPES = ("MASK",)
'''
        result = self._extract_source(source, "globals-get-alias-patched-node-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_module_class_locals_subscript_alias_patch_after_mapping_skips_node(self):
        source = '''
class LocalsSubscriptAliasPatchedNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "LocalsSubscriptAliasPatchedNode": LocalsSubscriptAliasPatchedNode,
}
Alias = locals()["LocalsSubscriptAliasPatchedNode"]
Alias.RETURN_TYPES = ("MASK",)
'''
        result = self._extract_source(source, "locals-subscript-alias-patched-node-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_module_class_vars_get_alias_patch_after_mapping_skips_node(self):
        source = '''
class VarsGetAliasPatchedNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "VarsGetAliasPatchedNode": VarsGetAliasPatchedNode,
}
Alias = vars().get("VarsGetAliasPatchedNode")
Alias.RETURN_TYPES = ("MASK",)
'''
        result = self._extract_source(source, "vars-get-alias-patched-node-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_module_class_attribute_alias_mutation_before_mapping_skips_node(self):
        source = '''
class PreMappingAttributeAliasNode:
    RETURN_TYPES = ["IMAGE"]

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


RET = PreMappingAttributeAliasNode.RETURN_TYPES
RET.clear()

NODE_CLASS_MAPPINGS = {
    "PreMappingAttributeAliasNode": PreMappingAttributeAliasNode,
}
'''
        result = self._extract_source(source, "pre-mapping-attribute-alias-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_module_class_attribute_chained_alias_mutation_skips_node(self):
        source = '''
class ChainedAttributeAliasNode:
    RETURN_TYPES = ["IMAGE"]

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


A = B = ChainedAttributeAliasNode.RETURN_TYPES
A.append("MASK")

NODE_CLASS_MAPPINGS = {
    "ChainedAttributeAliasNode": ChainedAttributeAliasNode,
}
'''
        result = self._extract_source(source, "chained-attribute-alias-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_module_class_attribute_tuple_alias_mutation_skips_node(self):
        source = '''
class TupleAttributeAliasNode:
    RETURN_TYPES = ["IMAGE"]

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


RET, = (TupleAttributeAliasNode.RETURN_TYPES,)
RET.clear()

NODE_CLASS_MAPPINGS = {
    "TupleAttributeAliasNode": TupleAttributeAliasNode,
}
'''
        result = self._extract_source(source, "tuple-attribute-alias-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_module_class_attribute_starred_alias_mutation_skips_node(self):
        source = '''
class StarredAttributeAliasNode:
    RETURN_TYPES = ["IMAGE"]

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


RET, *REST = (StarredAttributeAliasNode.RETURN_TYPES, [], [])
RET.clear()

NODE_CLASS_MAPPINGS = {
    "StarredAttributeAliasNode": StarredAttributeAliasNode,
}
'''
        result = self._extract_source(source, "starred-attribute-alias-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_module_class_attribute_starred_collection_alias_mutation_skips_node(self):
        source = '''
class StarredCollectionAttributeAliasNode:
    RETURN_TYPES = ["IMAGE"]

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


*ALIASES, = (StarredCollectionAttributeAliasNode.RETURN_TYPES,)
ALIASES[0].clear()

NODE_CLASS_MAPPINGS = {
    "StarredCollectionAttributeAliasNode": StarredCollectionAttributeAliasNode,
}
'''
        result = self._extract_source(source, "starred-collection-attribute-alias-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_module_class_attribute_transitive_alias_mutation_skips_node(self):
        source = '''
class TransitiveAttributeAliasNode:
    RETURN_TYPES = ["IMAGE"]

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


RET = TransitiveAttributeAliasNode.RETURN_TYPES
ALIAS = RET
ALIAS.clear()

NODE_CLASS_MAPPINGS = {
    "TransitiveAttributeAliasNode": TransitiveAttributeAliasNode,
}
'''
        result = self._extract_source(source, "transitive-attribute-alias-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_module_class_attribute_alias_mutation_after_mapping_skips_node(self):
        source = '''
class PostMappingAttributeAliasNode:
    RETURN_TYPES = ["IMAGE"]

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "PostMappingAttributeAliasNode": PostMappingAttributeAliasNode,
}
RET = PostMappingAttributeAliasNode.RETURN_TYPES
RET.clear()
'''
        result = self._extract_source(source, "post-mapping-attribute-alias-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_module_class_attribute_arbitrary_call_after_mapping_skips_node(self):
        source = '''
class ObserveAttributeCallNode:
    RETURN_TYPES = ["IMAGE"]

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "ObserveAttributeCallNode": ObserveAttributeCallNode,
}
mutate(ObserveAttributeCallNode.RETURN_TYPES)
'''
        result = self._extract_source(source, "observe-attribute-call-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_module_class_attribute_alias_arbitrary_call_after_mapping_skips_node(self):
        source = '''
class ObserveAttributeAliasCallNode:
    RETURN_TYPES = ["IMAGE"]

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "ObserveAttributeAliasCallNode": ObserveAttributeAliasCallNode,
}
RET = ObserveAttributeAliasCallNode.RETURN_TYPES
mutate(RET)
'''
        result = self._extract_source(source, "observe-attribute-alias-call-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_getattr_return_types_mutation_after_mapping_skips_node(self):
        source = '''
class GetattrReturnTypesNode:
    RETURN_TYPES = ["IMAGE"]

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "GetattrReturnTypesNode": GetattrReturnTypesNode,
}
getattr(GetattrReturnTypesNode, "RETURN_TYPES").clear()
'''
        result = self._extract_source(source, "getattr-return-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_globals_class_return_types_mutation_after_mapping_skips_node(self):
        source = '''
class GlobalsClassReturnTypesNode:
    RETURN_TYPES = ["IMAGE"]

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "GlobalsClassReturnTypesNode": GlobalsClassReturnTypesNode,
}
globals()["GlobalsClassReturnTypesNode"].RETURN_TYPES.clear()
'''
        result = self._extract_source(source, "globals-class-return-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_globals_get_class_return_types_mutation_after_mapping_skips_node(self):
        source = '''
class GlobalsGetReturnTypesNode:
    RETURN_TYPES = ["IMAGE"]

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "GlobalsGetReturnTypesNode": GlobalsGetReturnTypesNode,
}
globals().get("GlobalsGetReturnTypesNode").RETURN_TYPES.clear()
'''
        result = self._extract_source(source, "globals-get-return-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_getattr_class_attribute_alias_mutation_after_mapping_skips_node(self):
        source = '''
class GetattrAttributeAliasNode:
    RETURN_TYPES = ["IMAGE"]

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "GetattrAttributeAliasNode": GetattrAttributeAliasNode,
}
RET = getattr(GetattrAttributeAliasNode, "RETURN_TYPES")
RET.clear()
'''
        result = self._extract_source(source, "getattr-attribute-alias-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_getattr_namespace_class_attribute_alias_mutation_after_mapping_skips_node(self):
        source = '''
class GetattrNamespaceAttributeAliasNode:
    RETURN_TYPES = ["IMAGE"]

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "GetattrNamespaceAttributeAliasNode": GetattrNamespaceAttributeAliasNode,
}
RET = getattr(globals()["GetattrNamespaceAttributeAliasNode"], "RETURN_TYPES")
RET.clear()
'''
        result = self._extract_source(source, "getattr-namespace-attribute-alias-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_module_class_tuple_alias_patch_after_mapping_skips_node(self):
        source = '''
class TupleAliasPatchedNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "TupleAliasPatchedNode": TupleAliasPatchedNode,
}
Alias, = (TupleAliasPatchedNode,)
Alias.RETURN_TYPES = ("MASK",)
'''
        result = self._extract_source(source, "tuple-alias-patched-node-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_definition_time_class_attribute_mutation_after_mapping_skips_node(self):
        source = '''
class DefinitionTimeMutatedMappedNode:
    RETURN_TYPES = ["IMAGE"]

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "DefinitionTimeMutatedMappedNode": DefinitionTimeMutatedMappedNode,
}
def helper(x=DefinitionTimeMutatedMappedNode.RETURN_TYPES.clear()):
    pass
'''
        result = self._extract_source(source, "definition-time-mutated-mapped-node-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_unhashable_node_mapping_key_skips_repo_without_raising(self):
        source = '''
KEY = ["UnhashableMappingKeyNode"]


class UnhashableMappingKeyNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    KEY: UnhashableMappingKeyNode,
}
'''
        result = self._extract_source(source, "unhashable-mapping-key-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_mutated_node_class_mapping_skips_node(self):
        source = '''
class MutatedMappingNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "MutatedMappingNode": MutatedMappingNode,
}
NODE_CLASS_MAPPINGS.clear()
'''
        result = self._extract_source(source, "mutated-mapping-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_globals_mutation_invalidates_static_node_mapping(self):
        source = '''
class GlobalMutatedMappingNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "GlobalMutatedMappingNode": GlobalMutatedMappingNode,
}
globals()["NODE_CLASS_MAPPINGS"].clear()
'''
        result = self._extract_source(source, "global-mutated-mapping-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_globals_update_invalidates_static_node_mapping(self):
        source = '''
class GlobalUpdateNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "GlobalUpdateNode": GlobalUpdateNode,
}
globals().update(NODE_CLASS_MAPPINGS={})
'''
        result = self._extract_source(source, "global-update-mapping-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_globals_alias_subscript_assignment_invalidates_static_node_mapping(self):
        source = '''
class GlobalAliasSubscriptAssignmentNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "GlobalAliasSubscriptAssignmentNode": GlobalAliasSubscriptAssignmentNode,
}
G = globals()
G["NODE_CLASS_MAPPINGS"] = {}
'''
        result = self._extract_source(source, "global-alias-subscript-assignment-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_globals_chained_alias_subscript_assignment_invalidates_static_node_mapping(self):
        source = '''
class GlobalChainedAliasSubscriptAssignmentNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "GlobalChainedAliasSubscriptAssignmentNode": GlobalChainedAliasSubscriptAssignmentNode,
}
G = H = globals()
H["NODE_CLASS_MAPPINGS"] = {}
'''
        result = self._extract_source(source, "global-chained-alias-subscript-assignment-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_globals_alias_update_invalidates_static_node_mapping(self):
        source = '''
class GlobalAliasUpdateNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "GlobalAliasUpdateNode": GlobalAliasUpdateNode,
}
G = globals()
G.update(NODE_CLASS_MAPPINGS={})
'''
        result = self._extract_source(source, "global-alias-update-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_globals_alias_get_mutation_invalidates_static_node_mapping(self):
        source = '''
class GlobalAliasGetMutationNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "GlobalAliasGetMutationNode": GlobalAliasGetMutationNode,
}
G = globals()
G.get("NODE_CLASS_MAPPINGS").clear()
'''
        result = self._extract_source(source, "global-alias-get-mutation-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_globals_dunder_setitem_invalidates_static_node_mapping(self):
        source = '''
class GlobalDunderSetitemNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "GlobalDunderSetitemNode": GlobalDunderSetitemNode,
}
globals().__setitem__("NODE_CLASS_MAPPINGS", {})
'''
        result = self._extract_source(source, "global-dunder-setitem-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_globals_alias_dunder_setitem_invalidates_static_node_mapping(self):
        source = '''
class GlobalAliasDunderSetitemNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "GlobalAliasDunderSetitemNode": GlobalAliasDunderSetitemNode,
}
ns = globals()
ns.__setitem__("NODE_CLASS_MAPPINGS", {})
'''
        result = self._extract_source(source, "global-alias-dunder-setitem-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_arbitrary_call_invalidates_static_node_mapping(self):
        source = '''
class ArbitraryCallMappingNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "ArbitraryCallMappingNode": ArbitraryCallMappingNode,
}
mutate(NODE_CLASS_MAPPINGS)
'''
        result = self._extract_source(source, "arbitrary-call-mapping-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_alias_arbitrary_call_invalidates_static_node_mapping(self):
        source = '''
class AliasArbitraryCallMappingNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "AliasArbitraryCallMappingNode": AliasArbitraryCallMappingNode,
}
ALIAS = globals()["NODE_CLASS_MAPPINGS"]
mutate(ALIAS)
'''
        result = self._extract_source(source, "alias-arbitrary-call-mapping-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_globals_subscript_alias_mutation_invalidates_static_node_mapping(self):
        source = '''
class GlobalAliasMutatedMappingNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "GlobalAliasMutatedMappingNode": GlobalAliasMutatedMappingNode,
}
ALIAS = globals()["NODE_CLASS_MAPPINGS"]
ALIAS.clear()
'''
        result = self._extract_source(source, "global-alias-mutated-mapping-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_globals_get_alias_mutation_invalidates_static_node_mapping(self):
        source = '''
class GlobalGetAliasMutatedMappingNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "GlobalGetAliasMutatedMappingNode": GlobalGetAliasMutatedMappingNode,
}
ALIAS = globals().get("NODE_CLASS_MAPPINGS")
ALIAS.clear()
'''
        result = self._extract_source(source, "global-get-alias-mutated-mapping-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_globals_alias_get_alias_mutation_invalidates_static_node_mapping(self):
        source = '''
class GlobalAliasGetAliasMutatedMappingNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "GlobalAliasGetAliasMutatedMappingNode": GlobalAliasGetAliasMutatedMappingNode,
}
G = globals()
ALIAS = G.get("NODE_CLASS_MAPPINGS")
ALIAS.clear()
'''
        result = self._extract_source(source, "global-alias-get-alias-mutated-mapping-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_unpacked_alias_mutation_invalidates_static_node_mapping(self):
        source = '''
class UnpackedAliasMutatedMappingNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "UnpackedAliasMutatedMappingNode": UnpackedAliasMutatedMappingNode,
}
ALIAS, = (NODE_CLASS_MAPPINGS,)
ALIAS.clear()
'''
        result = self._extract_source(source, "unpacked-alias-mutated-mapping-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_starred_unpacked_alias_mutation_invalidates_static_node_mapping(self):
        source = '''
class StarredUnpackedAliasMutatedMappingNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "StarredUnpackedAliasMutatedMappingNode": StarredUnpackedAliasMutatedMappingNode,
}
ALIAS, *REST = (NODE_CLASS_MAPPINGS, {}, {})
ALIAS.clear()
'''
        result = self._extract_source(source, "starred-unpacked-alias-mutated-mapping-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_starred_collection_alias_mutation_invalidates_static_node_mapping(self):
        source = '''
class StarredCollectionAliasMutatedMappingNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "StarredCollectionAliasMutatedMappingNode": StarredCollectionAliasMutatedMappingNode,
}
*ALIASES, = (NODE_CLASS_MAPPINGS,)
ALIASES[0].clear()
'''
        result = self._extract_source(source, "starred-collection-alias-mutated-mapping-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_rhs_mutating_call_to_node_mapping_skips_node(self):
        source = '''
class RhsMutatedMappingNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "RhsMutatedMappingNode": RhsMutatedMappingNode,
}
X = NODE_CLASS_MAPPINGS.clear()
'''
        result = self._extract_source(source, "rhs-mutated-mapping-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_annotated_alias_mutation_invalidates_static_node_mapping(self):
        source = '''
class AnnotatedAliasMutatedMappingNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "AnnotatedAliasMutatedMappingNode": AnnotatedAliasMutatedMappingNode,
}
ALIAS: dict = NODE_CLASS_MAPPINGS
ALIAS.clear()
'''
        result = self._extract_source(source, "annotated-alias-mutated-mapping-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_type_alias_binding_invalidates_static_node_mapping(self):
        source = '''
class TypeAliasBoundMappingNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "TypeAliasBoundMappingNode": TypeAliasBoundMappingNode,
}
type NODE_CLASS_MAPPINGS = dict
'''
        self._skip_if_syntax_unsupported(source)
        result = self._extract_source(source, "type-alias-bound-mapping-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_wildcard_import_invalidates_static_node_mapping(self):
        source = '''
class WildcardImportMappingNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "WildcardImportMappingNode": WildcardImportMappingNode,
}
from something import *
'''
        result = self._extract_source(source, "wildcard-import-mapping-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_nested_wildcard_import_invalidates_static_node_mapping(self):
        source = '''
class NestedWildcardImportMappingNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "NestedWildcardImportMappingNode": NestedWildcardImportMappingNode,
}
if True:
    from something import *
'''
        result = self._extract_source(source, "nested-wildcard-import-mapping-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_wildcard_import_before_mapping_skips_static_node_mapping(self):
        source = '''
class WildcardBeforeMappingNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


from something import *

NODE_CLASS_MAPPINGS = {
    "WildcardBeforeMappingNode": WildcardBeforeMappingNode,
}
'''
        result = self._extract_source(source, "wildcard-before-mapping-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_nested_wildcard_import_before_mapping_skips_static_node_mapping(self):
        source = '''
class NestedWildcardBeforeMappingNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


if True:
    from something import *

NODE_CLASS_MAPPINGS = {
    "NestedWildcardBeforeMappingNode": NestedWildcardBeforeMappingNode,
}
'''
        result = self._extract_source(source, "nested-wildcard-before-mapping-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_dynamic_display_mapping_reassignment_skips_node(self):
        source = '''
def build_displays():
    return {"DisplayInvalidatedNode": "Dynamic Display"}


class DisplayInvalidatedNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "DisplayInvalidatedNode": DisplayInvalidatedNode,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "DisplayInvalidatedNode": "Stale Display",
}
NODE_DISPLAY_NAME_MAPPINGS = build_displays()
'''
        result = self._extract_source(source, "dynamic-display-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_dynamic_display_mapping_assignment_stays_invalid_after_static_reassignment(self):
        source = '''
def build_displays():
    return {"StickyDisplayInvalidatedNode": "Dynamic Display"}


class StickyDisplayInvalidatedNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "StickyDisplayInvalidatedNode": StickyDisplayInvalidatedNode,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "StickyDisplayInvalidatedNode": "Stale Display",
}
NODE_DISPLAY_NAME_MAPPINGS = build_displays()
NODE_DISPLAY_NAME_MAPPINGS = {
    "StickyDisplayInvalidatedNode": "Recovered Display",
}
'''
        result = self._extract_source(source, "sticky-display-invalidated-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_non_string_display_mapping_value_skips_node(self):
        source = '''
class NonStringDisplayValueNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "NonStringDisplayValueNode": NonStringDisplayValueNode,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "NonStringDisplayValueNode": 123,
}
'''
        result = self._extract_source(source, "non-string-display-value-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_non_string_display_mapping_key_skips_node(self):
        source = '''
class NonStringDisplayKeyNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "NonStringDisplayKeyNode": NonStringDisplayKeyNode,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    123: "Non String Display Key",
}
'''
        result = self._extract_source(source, "non-string-display-key-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_input_types_with_dynamic_control_flow_is_skipped(self):
        source = '''
def something():
    return True


def dynamic_inputs():
    return {"required": {"image": ("IMAGE",)}}


class DynamicBranchInputNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        if something():
            return dynamic_inputs()
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "DynamicBranchInputNode": DynamicBranchInputNode,
}
'''
        result = self._extract_source(source, "dynamic-branch-input-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_input_types_observed_by_arbitrary_call_skips_node(self):
        source = '''
class ObservedInputTypesNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }

    observe(INPUT_TYPES)


NODE_CLASS_MAPPINGS = {
    "ObservedInputTypesNode": ObservedInputTypesNode,
}
'''
        result = self._extract_source(source, "observed-input-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_input_types_used_as_callee_skips_node(self):
        source = '''
class CalleeObservedInputTypesNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }

    INPUT_TYPES()


NODE_CLASS_MAPPINGS = {
    "CalleeObservedInputTypesNode": CalleeObservedInputTypesNode,
}
'''
        result = self._extract_source(source, "callee-observed-input-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_input_types_alias_observed_by_arbitrary_call_skips_node(self):
        source = '''
class AliasObservedInputTypesNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }

    ALIAS = INPUT_TYPES
    observe(ALIAS)


NODE_CLASS_MAPPINGS = {
    "AliasObservedInputTypesNode": AliasObservedInputTypesNode,
}
'''
        result = self._extract_source(source, "alias-observed-input-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_input_types_chained_alias_observed_by_arbitrary_call_skips_node(self):
        source = '''
class ChainedAliasObservedInputTypesNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }

    A = B = INPUT_TYPES
    observe(A)


NODE_CLASS_MAPPINGS = {
    "ChainedAliasObservedInputTypesNode": ChainedAliasObservedInputTypesNode,
}
'''
        result = self._extract_source(source, "chained-alias-observed-input-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_input_types_chained_alias_used_as_callee_skips_node(self):
        source = '''
class ChainedAliasCalleeInputTypesNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }

    A = B = INPUT_TYPES
    A()


NODE_CLASS_MAPPINGS = {
    "ChainedAliasCalleeInputTypesNode": ChainedAliasCalleeInputTypesNode,
}
'''
        result = self._extract_source(source, "chained-alias-callee-input-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_input_types_default_observed_by_arbitrary_call_skips_node(self):
        source = '''
class DefaultObservedInputTypesNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls, value=observe(INPUT_TYPES)):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "DefaultObservedInputTypesNode": DefaultObservedInputTypesNode,
}
'''
        result = self._extract_source(source, "default-observed-input-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_input_types_return_annotation_observed_by_arbitrary_call_skips_node(self):
        source = '''
class AnnotationObservedInputTypesNode:
    RETURN_TYPES = ("IMAGE",)

    @classmethod
    def INPUT_TYPES(cls) -> observe(INPUT_TYPES):
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }


NODE_CLASS_MAPPINGS = {
    "AnnotationObservedInputTypesNode": AnnotationObservedInputTypesNode,
}
'''
        result = self._extract_source(source, "annotation-observed-input-types-pack")

        self.assertEqual({}, result["nodes"])
        self.assertEqual("no_static_nodes", result["pack"]["status"])

    def test_write_artifact_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_one = Path(tmp, "one.json")
            out_two = Path(tmp, "two.json")
            write_artifact(
                out_one,
                sources={
                    "manager_url": "https://example.invalid/manager.json",
                    "limit": 1,
                    "registry": {"z": "last", "a": "first"},
                },
                packs={
                    "b-pack": {
                        "id": "b-pack",
                        "title": "B Pack",
                        "status": "ok",
                        "metadata": {"z": 2, "a": 1},
                    },
                    "a-pack": {
                        "id": "a-pack",
                        "title": "A Pack",
                        "status": "ok",
                        "metadata": {"z": 4, "a": 3},
                    },
                },
                nodes={
                    "BNode": {
                        "type": "BNode",
                        "display": "B Node",
                        "pack": "b-pack",
                        "repository": "https://github.com/example/b-pack",
                        "inputs": {"zeta": "FLOAT", "alpha": "IMAGE"},
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
                        "inputs": {"zeta": "FLOAT", "alpha": "IMAGE"},
                        "required": [],
                        "outputs": ["IMAGE"],
                        "output_names": ["image"],
                        "confidence": "static_exact",
                    },
                },
            )
            write_artifact(
                out_two,
                sources={
                    "registry": {"a": "first", "z": "last"},
                    "limit": 1,
                    "manager_url": "https://example.invalid/manager.json",
                },
                packs={
                    "a-pack": {
                        "metadata": {"a": 3, "z": 4},
                        "status": "ok",
                        "title": "A Pack",
                        "id": "a-pack",
                    },
                    "b-pack": {
                        "metadata": {"a": 1, "z": 2},
                        "status": "ok",
                        "title": "B Pack",
                        "id": "b-pack",
                    },
                },
                nodes={
                    "ANode": {
                        "confidence": "static_exact",
                        "output_names": ["image"],
                        "outputs": ["IMAGE"],
                        "required": [],
                        "inputs": {"alpha": "IMAGE", "zeta": "FLOAT"},
                        "repository": "https://github.com/example/a-pack",
                        "pack": "a-pack",
                        "display": "A Node",
                        "type": "ANode",
                    },
                    "BNode": {
                        "confidence": "static_exact",
                        "output_names": ["image"],
                        "outputs": ["IMAGE"],
                        "required": [],
                        "inputs": {"alpha": "IMAGE", "zeta": "FLOAT"},
                        "repository": "https://github.com/example/b-pack",
                        "pack": "b-pack",
                        "display": "B Node",
                        "type": "BNode",
                    },
                },
            )
            text_one = out_one.read_text(encoding="utf-8")
            text_two = out_two.read_text(encoding="utf-8")
            parsed = json.loads(text_one)

        self.assertEqual(["a-pack", "b-pack"], list(parsed["packs"]))
        self.assertEqual(["ANode", "BNode"], list(parsed["nodes"]))
        self.assertEqual(self._normalise_generated_at(text_one), self._normalise_generated_at(text_two))


if __name__ == "__main__":
    unittest.main()
