from hdd_analyzer.extract import sanitize_excerpt


def test_sanitize_excerpt_decodes_plain_text():
    result = sanitize_excerpt(b"hello world")
    assert result == "hello world"


def test_sanitize_excerpt_empty_bytes_returns_empty_string():
    assert sanitize_excerpt(b"") == ""


def test_sanitize_excerpt_rejects_nul_heavy_binary():
    raw = b"\x00" * 1000 + b"text"
    assert sanitize_excerpt(raw) is None


def test_sanitize_excerpt_tolerates_a_few_nul_bytes():
    raw = b"hello\x00world" + b"x" * 1000
    assert sanitize_excerpt(raw) is not None


def test_sanitize_excerpt_caps_length():
    raw = b"a" * 10_000
    result = sanitize_excerpt(raw)
    assert result is not None
    assert len(result) <= 6000
