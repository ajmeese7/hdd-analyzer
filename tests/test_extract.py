import zipfile

from hdd_analyzer import extract as extract_mod
from hdd_analyzer.extract import extract_excerpt, sanitize_excerpt


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


def test_extract_office_xml_skips_oversized_member_without_decompressing(tmp_path, monkeypatch):
    """A member declaring a size over the cap must be skipped before reading it.

    The cap is lowered via monkeypatch so the test can assert the check
    against declared uncompressed size (file_size) without allocating a
    real multi-megabyte payload.
    """
    monkeypatch.setattr(extract_mod, "ARCHIVE_MEMBER_SIZE_CAP_BYTES", 10)
    docx_path = tmp_path / "bomb.docx"
    with zipfile.ZipFile(docx_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"<w:t>this is well over ten bytes</w:t>")

    excerpt, metadata_only = extract_excerpt(docx_path, category="doc", ext="docx")

    assert excerpt is None


def test_extract_office_xml_reads_member_under_cap(tmp_path):
    docx_path = tmp_path / "small.docx"
    with zipfile.ZipFile(docx_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"<w:t>hello world</w:t>")

    excerpt, metadata_only = extract_excerpt(docx_path, category="doc", ext="docx")

    assert excerpt is not None
    assert "hello world" in excerpt
    assert metadata_only is False
