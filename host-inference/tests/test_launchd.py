from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location("jarvis_launchd", ROOT / "launchd.py")
launchd = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(launchd)


class LaunchdPlistTests(unittest.TestCase):
    def test_embedding_plist_is_loopback_and_restartable(self):
        models = launchd.host_models.load_manifest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = launchd.build_plist(
                "granite_embedding",
                models["granite_embedding"],
                root / "models",
                "/opt/homebrew/bin/llama-server",
                root / "logs",
            )
        command = payload["ProgramArguments"]
        self.assertEqual(payload["Label"], "com.jarvis.model-host.embedding")
        self.assertTrue(payload["RunAtLoad"])
        self.assertEqual(payload["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(command[command.index("--host") + 1], "127.0.0.1")
        self.assertEqual(command[command.index("--port") + 1], "8751")
        self.assertEqual(command[command.index("--pooling") + 1], "cls")
        self.assertEqual(command[command.index("--batch-size") + 1], "8192")
        self.assertEqual(command[command.index("--ubatch-size") + 1], "8192")

    def test_reranker_plist_uses_rank_pooling(self):
        models = launchd.host_models.load_manifest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = launchd.build_plist(
                "bge_reranker",
                models["bge_reranker"],
                root / "models",
                "/opt/homebrew/bin/llama-server",
                root / "logs",
            )
        command = payload["ProgramArguments"]
        self.assertEqual(payload["Label"], "com.jarvis.model-host.reranker")
        self.assertEqual(command[command.index("--port") + 1], "8752")
        self.assertEqual(command[command.index("--pooling") + 1], "rank")
        self.assertIn("--reranking", command)

    def test_bootstrap_retries_transient_launchd_failure(self):
        failed = mock.Mock(returncode=5, stderr="Input/output error", stdout="")
        succeeded = mock.Mock(returncode=0, stderr="", stdout="")
        with mock.patch.object(
            launchd, "run_launchctl", side_effect=[failed, succeeded]
        ) as run, mock.patch.object(launchd.time, "sleep"):
            launchd.bootstrap_with_retry("gui/501", Path("/tmp/test.plist"))
        self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
