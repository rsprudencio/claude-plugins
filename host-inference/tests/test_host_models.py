from __future__ import annotations

import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "host_models.py"
SPEC = importlib.util.spec_from_file_location("host_models", MODULE_PATH)
host_models = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(host_models)


class ManifestTests(unittest.TestCase):
    def test_manifest_is_pinned_and_complete(self):
        models = host_models.load_manifest()
        self.assertEqual(set(models), {"granite_embedding", "bge_reranker"})
        self.assertEqual(models["granite_embedding"]["dimensions"], 384)
        for model in models.values():
            self.assertEqual(len(model["source_revision"]), 40)
            self.assertEqual(len(model["sha256"]), 64)
            self.assertGreater(model["size"], 0)
            self.assertIn(model["source_revision"], host_models.model_url(model))


class ModelAssetTests(unittest.TestCase):
    def test_verify_model_checks_size_and_hash(self):
        content = b"model bytes"
        model = {
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.gguf"
            self.assertEqual(host_models.verify_model(path, model), (False, "missing"))
            path.write_bytes(content)
            self.assertEqual(host_models.verify_model(path, model), (True, "verified"))
            path.write_bytes(content + b"!")
            self.assertFalse(host_models.verify_model(path, model)[0])

    def test_fetch_model_is_atomic_and_verified(self):
        content = b"verified model"
        model = {
            "filename": "model.gguf",
            "source_repo": "owner/repo",
            "source_revision": "a" * 40,
            "quantization": "F16",
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
        }

        class Response:
            def __init__(self):
                self.content = content

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self, size):
                result, self.content = self.content, b""
                return result

        with tempfile.TemporaryDirectory() as directory:
            model_dir = Path(directory)
            with mock.patch.object(
                host_models.urllib.request, "urlopen", return_value=Response()
            ):
                destination = host_models.fetch_model(model_dir, "test", model)
            self.assertEqual(destination.read_bytes(), content)
            self.assertFalse((model_dir / "model.gguf.part").exists())


class ServerCommandTests(unittest.TestCase):
    def test_server_commands_keep_models_on_loopback(self):
        models = host_models.load_manifest()
        with tempfile.TemporaryDirectory() as directory:
            model_dir = Path(directory)
            embedding = host_models.build_server_command(
                "granite_embedding", models["granite_embedding"], model_dir
            )
            reranker = host_models.build_server_command(
                "bge_reranker", models["bge_reranker"], model_dir
            )
        self.assertEqual(embedding[embedding.index("--host") + 1], "127.0.0.1")
        self.assertEqual(embedding[embedding.index("--port") + 1], "8751")
        self.assertEqual(embedding[embedding.index("--pooling") + 1], "cls")
        self.assertNotIn("--reranking", embedding)
        self.assertEqual(embedding[embedding.index("--parallel") + 1], "1")
        self.assertEqual(embedding[embedding.index("--batch-size") + 1], "8192")
        self.assertEqual(embedding[embedding.index("--ubatch-size") + 1], "8192")
        self.assertEqual(
            embedding[embedding.index("--cors-origins") + 1], "localhost"
        )
        self.assertEqual(reranker[reranker.index("--port") + 1], "8752")
        self.assertEqual(reranker[reranker.index("--pooling") + 1], "rank")
        self.assertIn("--reranking", reranker)


class SmokeTests(unittest.TestCase):
    def test_embedding_smoke_validates_openai_shape(self):
        model = {"model_id": "granite", "dimensions": 3}
        with mock.patch.object(
            host_models,
            "json_request",
            return_value={"data": [{"embedding": [1.0, 0.0, 0.0]}]},
        ):
            result = host_models.smoke_embedding(model, "http://localhost:8751")
        self.assertEqual(result["dimensions"], 3)
        self.assertEqual(result["norm"], 1.0)

    def test_reranker_smoke_requires_semantically_correct_top_result(self):
        model = {"model_id": "bge"}
        payload = {
            "results": [
                {"index": 1, "relevance_score": 0.99},
                {"index": 0, "relevance_score": 0.02},
                {"index": 2, "relevance_score": 0.01},
            ]
        }
        with mock.patch.object(host_models, "json_request", return_value=payload):
            result = host_models.smoke_reranker(model, "http://localhost:8752")
        self.assertEqual(result["top_index"], 1)
        self.assertEqual(result["top_score"], 0.99)


if __name__ == "__main__":
    unittest.main()
