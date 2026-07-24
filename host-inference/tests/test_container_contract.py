from __future__ import annotations

import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


class HostOnlyContainerContractTests(unittest.TestCase):
    def test_image_contains_no_local_inference_runtime_or_model_assets(self):
        dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text()

        for forbidden in (
            "onnxruntime",
            "sentence-transformers",
            'torch --index-url',
            "model-fetch",
            "embedding-fetch",
            "/app/models",
            "JARVIS_MODEL_DIR",
            "EMBEDDING_MODEL=/",
        ):
            self.assertNotIn(forbidden, dockerfile)

    def test_image_and_compose_default_to_host_inference(self):
        dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text()
        compose = (REPO_ROOT / "docker" / "docker-compose.yml").read_text()

        self.assertIn("ENV EMBEDDING_BACKEND=host", dockerfile)
        self.assertIn("ENV RERANKING_BACKEND=host", dockerfile)
        self.assertIn("ENV RERANKING_ENABLED=true", dockerfile)
        self.assertIn("EMBEDDING_BACKEND=${EMBEDDING_BACKEND:-host}", compose)
        self.assertIn("RERANKING_BACKEND=${RERANKING_BACKEND:-host}", compose)
        self.assertIn("RERANKING_ENABLED=${RERANKING_ENABLED:-true}", compose)

        canary = json.loads(
            (REPO_ROOT / "host-inference" / "canary-config.json").read_text()
        )
        memory = canary["memory"]
        self.assertEqual(memory["embedding_backend"], "host")
        self.assertTrue(memory["reranking"]["enabled"])
        self.assertEqual(memory["reranking"]["backend"], "host")


if __name__ == "__main__":
    unittest.main()
