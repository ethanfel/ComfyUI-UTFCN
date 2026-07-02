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
