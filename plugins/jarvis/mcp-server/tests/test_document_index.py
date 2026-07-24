import math
from unittest.mock import MagicMock

import pytest

from tools import document_index


class _Service:
    def tokenize(self, text, *, with_pieces=False):
        assert with_pieces
        return [{"id": index, "piece": char} for index, char in enumerate(text)]

    def encode(self, text):
        return [1.0, 0.0]

    def encode_batch(self, texts, batch_size=8):
        assert batch_size == 8
        return [[1.0, float(index + 1)] for index, _ in enumerate(texts)]


def test_prepare_document_keeps_all_windows_and_normalizes_parent(monkeypatch):
    monkeypatch.setattr(document_index, "DOCUMENT_WINDOW_TOKENS", 4)
    monkeypatch.setattr(document_index, "DOCUMENT_WINDOW_OVERLAP", 1)
    prepared = document_index.prepare_document("abcdefghij", _Service())

    assert prepared.windows == ["abcd", "defg", "ghij"]
    assert prepared.is_chunked is True
    norm = math.sqrt(sum(value * value for value in prepared.canonical_embedding))
    assert norm == pytest.approx(1.0)


def test_replace_chunks_is_parent_addressed_and_atomic_shape(monkeypatch):
    monkeypatch.setattr(document_index, "DOCUMENT_WINDOW_TOKENS", 4)
    monkeypatch.setattr(document_index, "DOCUMENT_WINDOW_OVERLAP", 1)
    prepared = document_index.prepare_document("abcdefghij", _Service())
    cursor = MagicMock()

    document_index.replace_local_chunks(cursor, "obs::one", prepared)

    cursor.execute.assert_called_once_with(
        "DELETE FROM local.memory_chunks WHERE parent_id = %s", ("obs::one",)
    )
    rows = cursor.executemany.call_args.args[1]
    assert [row[1] for row in rows] == [0, 1, 2]
    assert all(row[0] == "obs::one" and row[2] == 3 for row in rows)
