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


class LlmBackendPlumbingTests(unittest.TestCase):
    """Summaries can only be generated where an LLM is reachable. The image used
    to ship no `anthropic` SDK, no `claude` CLI and no key, and neither compose
    file passed ANTHROPIC_API_KEY through — so the feature was a guaranteed
    no-op in the only supported install method.
    """

    def test_image_ships_the_anthropic_sdk(self):
        dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text()
        self.assertIn("anthropic>=", dockerfile)

    def test_both_compose_files_pass_the_api_key_through(self):
        compose = (REPO_ROOT / "docker" / "docker-compose.yml").read_text()
        self.assertIn("ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}", compose)

        installer = (REPO_ROOT / "install.sh").read_text()
        self.assertIn("ANTHROPIC_API_KEY=\\${ANTHROPIC_API_KEY:-}", installer)

    def test_generator_script_is_packaged_and_exposed(self):
        script = (
            REPO_ROOT / "plugins" / "jarvis" / "mcp-server" / "bin"
            / "generate_summaries.py"
        )
        self.assertTrue(script.exists())
        pyproject = (
            REPO_ROOT / "plugins" / "jarvis" / "mcp-server" / "pyproject.toml"
        ).read_text()
        self.assertIn("jarvis-generate-summaries", pyproject)

    def test_runtime_contract_verifies_the_sdk_is_importable(self):
        contract = (
            REPO_ROOT / "docker" / "tests" / "verify_runtime_contract.py"
        ).read_text()
        self.assertIn("verify_llm_backend_importable", contract)
