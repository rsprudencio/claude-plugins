from tools.text_windows import split_text_windows


def _character_tokens(text, *, with_pieces=False):
    assert with_pieces is True
    return [{"id": index, "piece": char} for index, char in enumerate(text)]


def test_token_windows_cover_unbounded_input_with_overlap():
    windows = split_text_windows(
        "abcdefghij",
        max_tokens=4,
        overlap_tokens=1,
        tokenize=_character_tokens,
    )
    assert windows == ["abcd", "defg", "ghij"]
    assert all(len(window) <= 4 for window in windows)


def test_unicode_byte_pieces_do_not_corrupt_canonical_caller_text():
    def tokenize(_text, *, with_pieces=False):
        assert with_pieces
        return [
            {"id": 1, "piece": "a"},
            {"id": 2, "piece": [240, 159]},
            {"id": 3, "piece": [154]},
            {"id": 4, "piece": [128]},
            {"id": 5, "piece": "b"},
        ]

    windows = split_text_windows(
        "a🚀b", max_tokens=4, overlap_tokens=2, tokenize=tokenize
    )
    assert windows
    assert any("🚀" in window for window in windows)


def test_fallback_is_bounded_by_utf8_bytes():
    windows = split_text_windows(
        "🚀" * 20, max_tokens=16, overlap_tokens=4, tokenize=None
    )
    assert len(windows) > 1
    assert all(len(window.encode("utf-8")) <= 16 for window in windows)
