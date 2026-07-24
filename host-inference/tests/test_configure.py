from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "host_inference_configure", ROOT / "configure.py"
)
configure = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(configure)


class ConfigureTests(unittest.TestCase):
    def test_activate_preserves_unrelated_settings(self):
        payload = {
            "vault_path": "/vault",
            "todoist": {"api_token": "secret"},
            "memory": {"ranking": {"importance_weight": 0.24}},
        }
        configure.activate(payload)
        self.assertEqual(payload["vault_path"], "/vault")
        self.assertEqual(payload["todoist"]["api_token"], "secret")
        self.assertEqual(payload["memory"]["embedding_backend"], "host")
        self.assertEqual(
            payload["memory"]["embedding_model_id"],
            "ibm-granite/granite-embedding-small-english-r2",
        )
        self.assertTrue(payload["memory"]["reranking"]["enabled"])
        self.assertEqual(payload["memory"]["reranking"]["candidate_count"], 20)

    def test_atomic_write_and_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            original = {"memory": {"embedding_backend": "onnx"}}
            path.write_text(json.dumps(original), encoding="utf-8")
            backup = configure.ensure_backup(path)
            payload = configure.load_config(path)
            configure.activate(payload)
            configure.write_config(path, payload)
            self.assertEqual(json.loads(backup.read_text()), original)
            self.assertEqual(
                json.loads(path.read_text())["memory"]["embedding_backend"], "host"
            )
            self.assertFalse(path.with_name("config.json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
