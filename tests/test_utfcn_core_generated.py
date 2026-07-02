import json
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path

import utfcn_core


def _empty_generated():
    return {"sigs": {}, "meta": {}, "by_out": defaultdict(list)}


class GeneratedSignatureLoaderTests(unittest.TestCase):
    def test_missing_generated_file_returns_empty_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated = utfcn_core.load_generated_signatures(tmp)

        self.assertEqual({}, generated["sigs"])
        self.assertEqual({}, generated["meta"])
        self.assertEqual({}, dict(generated["by_out"]))

    def test_loads_usable_static_signature(self):
        payload = {
            "schema_version": 1,
            "generated_at": "2026-07-02T00:00:00Z",
            "sources": {"limit": 1},
            "packs": {
                "sample-pack": {
                    "title": "Sample Pack",
                    "repository": "https://github.com/example/sample-pack",
                }
            },
            "nodes": {
                "SampleImageSize": {
                    "type": "SampleImageSize",
                    "display": "Sample Image Size",
                    "pack": "sample-pack",
                    "repository": "https://github.com/example/sample-pack",
                    "inputs": {"image": "IMAGE"},
                    "required": ["image"],
                    "outputs": ["INT", "INT"],
                    "output_names": ["width", "height"],
                    "confidence": "static_exact",
                }
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "popular_node_signatures.json").write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            generated = utfcn_core.load_generated_signatures(tmp)

        self.assertEqual({"image": "IMAGE"}, generated["sigs"]["SampleImageSize"]["inputs"])
        self.assertEqual({"image"}, generated["sigs"]["SampleImageSize"]["required"])
        self.assertEqual(["INT", "INT"], generated["sigs"]["SampleImageSize"]["outputs"])
        self.assertEqual(["width", "height"], generated["sigs"]["SampleImageSize"]["output_names"])
        self.assertEqual("sample-pack", generated["meta"]["SampleImageSize"]["pack"])
        self.assertEqual("Sample Image Size", generated["meta"]["SampleImageSize"]["display"])
        self.assertEqual(["SampleImageSize"], generated["by_out"]["INT"])

    def test_rejects_metadata_only_entries_for_matching(self):
        payload = {
            "schema_version": 1,
            "generated_at": "2026-07-02T00:00:00Z",
            "sources": {},
            "packs": {},
            "nodes": {
                "NameOnlyNode": {
                    "type": "NameOnlyNode",
                    "display": "Name Only",
                    "pack": "name-only",
                    "repository": "https://github.com/example/name-only",
                    "inputs": {},
                    "required": [],
                    "outputs": [],
                    "output_names": [],
                    "confidence": "metadata_only",
                }
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "popular_node_signatures.json").write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            generated = utfcn_core.load_generated_signatures(tmp)

        self.assertNotIn("NameOnlyNode", generated["sigs"])
        self.assertNotIn("NameOnlyNode", generated["meta"])
        self.assertEqual({}, dict(generated["by_out"]))

    def test_malformed_generated_file_returns_empty_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "popular_node_signatures.json").write_text("{broken", encoding="utf-8")
            generated = utfcn_core.load_generated_signatures(tmp)

        self.assertEqual({}, generated["sigs"])
        self.assertEqual({}, generated["meta"])
        self.assertEqual({}, dict(generated["by_out"]))

    def test_unsupported_schema_returns_empty_indexes(self):
        payload = {
            "schema_version": 99,
            "generated_at": "2026-07-02T00:00:00Z",
            "sources": {},
            "packs": {},
            "nodes": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "popular_node_signatures.json").write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            generated = utfcn_core.load_generated_signatures(tmp)

        self.assertEqual({}, generated["sigs"])
        self.assertEqual({}, generated["meta"])
        self.assertEqual({}, dict(generated["by_out"]))

    def test_repository_artifact_loads_when_present(self):
        repo_dir = Path(__file__).resolve().parents[1]
        generated = utfcn_core.load_generated_signatures(str(repo_dir))

        node_type = "RGB_HexToHSV //Inspire"
        self.assertEqual({"rgb_hex": "STRING"}, generated["sigs"][node_type]["inputs"])
        self.assertEqual({"rgb_hex"}, generated["sigs"][node_type]["required"])
        self.assertEqual(["FLOAT", "FLOAT", "FLOAT"], generated["sigs"][node_type]["outputs"])
        self.assertEqual(["hue", "saturation", "value"], generated["sigs"][node_type]["output_names"])
        self.assertEqual("inspire", generated["meta"][node_type]["pack"])
        self.assertEqual("RGB Hex To HSV (Inspire)", generated["meta"][node_type]["display"])
        self.assertEqual(
            "https://github.com/ltdrdata/ComfyUI-Inspire-Pack",
            generated["meta"][node_type]["repository"],
        )
        self.assertIn(node_type, generated["by_out"]["FLOAT"])


class GeneratedSignatureMatchingTests(unittest.TestCase):
    def _ctx(self, rules=None, generated=None):
        live_sigs = {
            "CoreImageSize": {
                "inputs": {"image": "IMAGE"},
                "required": {"image"},
                "outputs": ["INT", "INT"],
                "output_names": ["width", "height"],
            },
            "CoreMaskInvert": {
                "inputs": {"mask": "MASK"},
                "required": {"mask"},
                "outputs": ["MASK"],
                "output_names": ["mask"],
            },
            "CoreImagePassthrough": {
                "inputs": {"image": "IMAGE"},
                "required": {"image"},
                "outputs": ["IMAGE"],
                "output_names": ["image"],
            },
            "CuratedTarget": {
                "inputs": {"image": "IMAGE"},
                "required": {"image"},
                "outputs": ["INT", "INT"],
                "output_names": ["width", "height"],
            },
        }
        sources = {
            "CoreImageSize": {"source": "core", "pack": "nodes", "display": "Core Image Size"},
            "CoreMaskInvert": {"source": "core", "pack": "nodes", "display": "Core Mask Invert"},
            "CoreImagePassthrough": {"source": "core", "pack": "nodes", "display": "Core Image Passthrough"},
            "CuratedTarget": {"source": "core", "pack": "nodes", "display": "Curated Target"},
        }
        by_out = defaultdict(list)
        for name, sig in live_sigs.items():
            by_out[sig["outputs"][0]].append(name)
        return {
            "sources": sources,
            "sigs": live_sigs,
            "by_out": by_out,
            "rules": rules or {},
            "generated": generated or _empty_generated(),
        }

    def test_generated_exact_signature_matches_missing_node_as_verified(self):
        generated = _empty_generated()
        generated["sigs"]["SampleImageSize"] = {
            "inputs": {"image": "IMAGE"},
            "required": {"image"},
            "outputs": ["INT", "INT"],
            "output_names": ["width", "height"],
        }
        generated["meta"]["SampleImageSize"] = {
            "source": "generated",
            "pack": "sample-pack",
            "display": "Sample Image Size",
            "repository": "https://github.com/example/sample-pack",
            "confidence": "static_exact",
        }
        generated["by_out"]["INT"].append("SampleImageSize")

        result = utfcn_core.match(self._ctx(generated=generated), [{"type": "SampleImageSize"}])

        self.assertEqual("CoreImageSize", result["SampleImageSize"][0]["to"])
        self.assertEqual("exact", result["SampleImageSize"][0]["tier"])
        self.assertTrue(result["SampleImageSize"][0]["verified"])

    def test_curated_rule_stays_first_before_generated_exact_match(self):
        generated = _empty_generated()
        generated["sigs"]["SampleImageSize"] = {
            "inputs": {"image": "IMAGE"},
            "required": {"image"},
            "outputs": ["INT", "INT"],
            "output_names": ["width", "height"],
        }
        generated["meta"]["SampleImageSize"] = {
            "source": "generated",
            "pack": "sample-pack",
            "display": "Sample Image Size",
            "repository": "https://github.com/example/sample-pack",
            "confidence": "static_exact",
        }
        generated["by_out"]["INT"].append("SampleImageSize")
        rules = {
            "SampleImageSize": [
                {
                    "to": "CuratedTarget",
                    "note": "Curated replacement wins over generated exact signature.",
                }
            ]
        }

        result = utfcn_core.match(self._ctx(rules=rules, generated=generated), [{"type": "SampleImageSize"}])

        self.assertEqual("CuratedTarget", result["SampleImageSize"][0]["to"])
        self.assertEqual("curated", result["SampleImageSize"][0]["tier"])
        self.assertTrue(result["SampleImageSize"][0]["verified"])

    def test_generated_partial_signature_matches_but_is_not_verified(self):
        generated = _empty_generated()
        generated["sigs"]["SampleMaskInvert"] = {
            "inputs": {"masks": "MASK"},
            "required": {"masks"},
            "outputs": ["MASK"],
            "output_names": ["mask"],
        }
        generated["meta"]["SampleMaskInvert"] = {
            "source": "generated",
            "pack": "sample-pack",
            "display": "Sample Mask Invert",
            "repository": "https://github.com/example/sample-pack",
            "confidence": "static_exact",
        }
        generated["by_out"]["MASK"].append("SampleMaskInvert")

        result = utfcn_core.match(self._ctx(generated=generated), [{"type": "SampleMaskInvert"}])

        self.assertEqual("CoreMaskInvert", result["SampleMaskInvert"][0]["to"])
        self.assertEqual("partial", result["SampleMaskInvert"][0]["tier"])
        self.assertFalse(result["SampleMaskInvert"][0]["verified"])

    def test_contradictory_generated_signature_falls_back_to_serialized_signature(self):
        generated = _empty_generated()
        generated["sigs"]["SampleMaskInvert"] = {
            "inputs": {"image": "IMAGE"},
            "required": {"image"},
            "outputs": ["IMAGE"],
            "output_names": ["image"],
        }
        generated["meta"]["SampleMaskInvert"] = {
            "source": "generated",
            "pack": "sample-pack",
            "display": "Sample Mask Invert",
            "repository": "https://github.com/example/sample-pack",
            "confidence": "static_exact",
        }
        generated["by_out"]["IMAGE"].append("SampleMaskInvert")

        result = utfcn_core.match(
            self._ctx(generated=generated),
            [
                {
                    "type": "SampleMaskInvert",
                    "inputs": {"masks": "MASK"},
                    "outputs": ["MASK"],
                    "output_names": ["mask"],
                }
            ],
        )

        self.assertEqual("CoreMaskInvert", result["SampleMaskInvert"][0]["to"])
        self.assertEqual("partial", result["SampleMaskInvert"][0]["tier"])
        self.assertFalse(result["SampleMaskInvert"][0]["verified"])

    def test_generated_signature_with_different_input_name_falls_back_to_serialized_signature(self):
        generated = _empty_generated()
        generated["sigs"]["SampleMaskInvert"] = {
            "inputs": {"mask": "MASK"},
            "required": {"mask"},
            "outputs": ["MASK"],
            "output_names": ["mask"],
        }
        generated["meta"]["SampleMaskInvert"] = {
            "source": "generated",
            "pack": "sample-pack",
            "display": "Sample Mask Invert",
            "repository": "https://github.com/example/sample-pack",
            "confidence": "static_exact",
        }
        generated["by_out"]["MASK"].append("SampleMaskInvert")

        result = utfcn_core.match(
            self._ctx(generated=generated),
            [
                {
                    "type": "SampleMaskInvert",
                    "inputs": {"masks": "MASK"},
                    "outputs": ["MASK"],
                    "output_names": ["mask"],
                }
            ],
        )

        self.assertEqual("CoreMaskInvert", result["SampleMaskInvert"][0]["to"])
        self.assertEqual("partial", result["SampleMaskInvert"][0]["tier"])
        self.assertFalse(result["SampleMaskInvert"][0]["verified"])

    def test_malformed_generated_context_falls_back_without_raising(self):
        result = utfcn_core.match(
            self._ctx(generated={"sigs": "bad", "meta": "bad"}),
            [
                {
                    "type": "SerializedMaskInvert",
                    "inputs": {"masks": "MASK"},
                    "outputs": ["MASK"],
                    "output_names": ["mask"],
                }
            ],
        )

        self.assertEqual("CoreMaskInvert", result["SerializedMaskInvert"][0]["to"])
        self.assertEqual("partial", result["SerializedMaskInvert"][0]["tier"])
        self.assertFalse(result["SerializedMaskInvert"][0]["verified"])

    def test_serialized_signature_fallback_still_handles_unknown_generated_node(self):
        result = utfcn_core.match(
            self._ctx(),
            [
                {
                    "type": "SerializedMaskInvert",
                    "inputs": {"masks": "MASK"},
                    "outputs": ["MASK"],
                    "output_names": ["mask"],
                }
            ],
        )

        self.assertEqual("CoreMaskInvert", result["SerializedMaskInvert"][0]["to"])
        self.assertEqual("partial", result["SerializedMaskInvert"][0]["tier"])
        self.assertFalse(result["SerializedMaskInvert"][0]["verified"])


if __name__ == "__main__":
    unittest.main()
