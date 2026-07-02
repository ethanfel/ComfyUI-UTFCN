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

    def test_dynamic_display_mapping_reassignment_falls_back_to_node_type(self):
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

        self.assertEqual("DisplayInvalidatedNode", result["nodes"]["DisplayInvalidatedNode"]["display"])
        self.assertEqual("ok", result["pack"]["status"])

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
