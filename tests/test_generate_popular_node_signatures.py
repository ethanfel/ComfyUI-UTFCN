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
