import json
import tempfile
import unittest
from pathlib import Path

import utfcn_core


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


if __name__ == "__main__":
    unittest.main()
