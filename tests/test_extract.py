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
    assert len(result) <= 3000


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

    excerpt, metadata_only, extraction_status = extract_excerpt(docx_path, category="doc", ext="docx", size=docx_path.stat().st_size)

    assert excerpt is None
    assert metadata_only is True
    assert extraction_status == extract_mod.EXTRACTION_STATUS_NO_TEXT


def test_extract_office_xml_reads_member_under_cap(tmp_path):
    docx_path = tmp_path / "small.docx"
    with zipfile.ZipFile(docx_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"<w:t>hello world</w:t>")

    excerpt, metadata_only, extraction_status = extract_excerpt(docx_path, category="doc", ext="docx", size=docx_path.stat().st_size)

    assert excerpt is not None
    assert "hello world" in excerpt
    assert metadata_only is False
    assert extraction_status == extract_mod.EXTRACTION_STATUS_OK


def test_extract_excerpt_unsupported_category_never_tries_content(tmp_path):
    image_path = tmp_path / "photo.jpg"
    image_path.write_bytes(b"\xff\xd8\xff")

    excerpt, metadata_only, extraction_status = extract_excerpt(image_path, category="image", ext="jpg", size=3)

    assert excerpt is None
    assert metadata_only is True
    assert extraction_status == extract_mod.EXTRACTION_STATUS_UNSUPPORTED


def test_extract_excerpt_missing_file_is_error_not_no_text(tmp_path):
    missing = tmp_path / "gone.txt"

    excerpt, metadata_only, extraction_status = extract_excerpt(missing, category="text", ext="txt", size=0)

    assert excerpt is None
    assert metadata_only is True
    assert extraction_status == extract_mod.EXTRACTION_STATUS_ERROR


def test_extract_excerpt_ok_for_readable_text(tmp_path):
    text_path = tmp_path / "notes.txt"
    text_path.write_text("hello world", encoding="utf-8")

    excerpt, metadata_only, extraction_status = extract_excerpt(text_path, category="text", ext="txt", size=text_path.stat().st_size)

    assert excerpt == "hello world"
    assert metadata_only is False
    assert extraction_status == extract_mod.EXTRACTION_STATUS_OK


def test_extract_excerpt_rtf_strips_control_words(tmp_path):
    rtf_path = tmp_path / "note.rtf"
    rtf_path.write_bytes(rb"{\rtf1\ansi\deff0 {\fonttbl{\f0 Arial;}}\f0\fs24 hello world}")

    excerpt, metadata_only, extraction_status = extract_excerpt(rtf_path, category="text", ext="rtf", size=rtf_path.stat().st_size)

    assert excerpt is not None
    assert "hello world" in excerpt
    assert metadata_only is False
    assert extraction_status == extract_mod.EXTRACTION_STATUS_OK


def test_should_sniff_binary_true_for_small_unknown_extension():
    assert extract_mod.should_sniff_binary("binary", "", 100) is True
    assert extract_mod.should_sniff_binary("binary", "somethingweird", 100) is True


def test_should_sniff_binary_false_for_non_binary_category():
    assert extract_mod.should_sniff_binary("text", "", 100) is False


def test_should_sniff_binary_false_for_known_binary_extensions():
    for ext in ("exe", "dll", "sqlite", "pfx", "kdbx"):
        assert extract_mod.should_sniff_binary("binary", ext, 100) is False


def test_should_sniff_binary_false_when_oversized():
    assert extract_mod.should_sniff_binary("binary", "", extract_mod.SNIFF_MAX_BYTES + 1) is False


def test_should_sniff_binary_true_at_exact_size_boundary():
    assert extract_mod.should_sniff_binary("binary", "", extract_mod.SNIFF_MAX_BYTES) is True


def test_extract_excerpt_sniffs_text_like_unknown_extension_file(tmp_path):
    key_path = tmp_path / "id_rsa"
    key_path.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nabc123\n-----END OPENSSH PRIVATE KEY-----\n", encoding="utf-8")

    excerpt, metadata_only, extraction_status = extract_excerpt(key_path, category="binary", ext="", size=key_path.stat().st_size)

    assert excerpt is not None
    assert "BEGIN OPENSSH PRIVATE KEY" in excerpt
    assert metadata_only is False
    assert extraction_status == extract_mod.EXTRACTION_STATUS_OK


def test_extract_excerpt_sniff_fails_on_genuinely_binary_content(tmp_path):
    blob_path = tmp_path / "myKeyStore"
    blob_path.write_bytes(b"\x00\x01\x02\x03" * 500)

    excerpt, metadata_only, extraction_status = extract_excerpt(blob_path, category="binary", ext="", size=blob_path.stat().st_size)

    assert excerpt is None
    assert metadata_only is True
    assert extraction_status == extract_mod.EXTRACTION_STATUS_UNSUPPORTED


def test_extract_excerpt_never_sniffs_known_binary_extension(tmp_path):
    exe_path = tmp_path / "app.exe"
    exe_path.write_text("hello world this is actually text", encoding="utf-8")

    excerpt, metadata_only, extraction_status = extract_excerpt(exe_path, category="binary", ext="exe", size=exe_path.stat().st_size)

    assert excerpt is None
    assert metadata_only is True
    assert extraction_status == extract_mod.EXTRACTION_STATUS_UNSUPPORTED


def test_extract_excerpt_does_not_sniff_oversized_unknown_extension_file(tmp_path):
    big_path = tmp_path / "bigblob"
    big_path.write_text("hello world", encoding="utf-8")

    excerpt, metadata_only, extraction_status = extract_excerpt(
        big_path, category="binary", ext="", size=extract_mod.SNIFF_MAX_BYTES + 1
    )

    assert excerpt is None
    assert metadata_only is True
    assert extraction_status == extract_mod.EXTRACTION_STATUS_UNSUPPORTED


def test_sniff_rejects_binary_magic_despite_text_like_payload(tmp_path):
    from hdd_analyzer.extract import extract_excerpt

    webp = tmp_path / "credential_dump.webp"
    webp.write_bytes(b"RIFF" + b"A" * 500)
    excerpt, metadata_only, status = extract_excerpt(webp, "binary", "webp", 504)
    assert excerpt is None
    assert metadata_only is True
    assert status == "unsupported"


def test_sniff_accepts_extensionless_text_key(tmp_path):
    from hdd_analyzer.extract import extract_excerpt

    key = tmp_path / "id_rsa"
    body = b"-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n"
    key.write_bytes(body)
    excerpt, metadata_only, status = extract_excerpt(key, "binary", "", len(body))
    assert status == "ok"
    assert metadata_only is False
    assert "PRIVATE KEY" in excerpt


def _build_emlx(message_bytes: bytes, plist: bytes) -> bytes:
    return f"{len(message_bytes)}\n".encode("ascii") + message_bytes + plist


def test_extract_emlx_parses_headers_and_plain_text_body_excluding_plist(tmp_path):
    message = (
        b"From: alice@example.com\r\n"
        b"To: bob@example.com\r\n"
        b"Date: Mon, 1 Jan 2024 00:00:00 -0000\r\n"
        b"Subject: Hello\r\n"
        b"Content-Type: text/plain\r\n"
        b"\r\n"
        b"This is the body text.\r\n"
    )
    plist = b"<plist><dict><key>flags</key><integer>0</integer></dict></plist>"
    emlx_path = tmp_path / "123.emlx"
    emlx_path.write_bytes(_build_emlx(message, plist))

    excerpt, metadata_only, status = extract_excerpt(emlx_path, category="text", ext="emlx", size=emlx_path.stat().st_size)

    assert status == "ok"
    assert metadata_only is False
    assert "From: alice@example.com" in excerpt
    assert "Subject: Hello" in excerpt
    assert "This is the body text." in excerpt
    assert "<plist>" not in excerpt
    assert "flags" not in excerpt


def test_extract_emlx_missing_length_line_is_no_text(tmp_path):
    emlx_path = tmp_path / "bad.emlx"
    emlx_path.write_bytes(b"not a number here at all with no newline")

    excerpt, metadata_only, status = extract_excerpt(emlx_path, category="text", ext="emlx", size=emlx_path.stat().st_size)

    assert excerpt is None
    assert metadata_only is True
    assert status == "no_text"


def test_extract_eml_parses_headers_and_body(tmp_path):
    message = (
        b"From: alice@example.com\r\n"
        b"To: bob@example.com\r\n"
        b"Subject: Plain eml\r\n"
        b"Content-Type: text/plain\r\n"
        b"\r\n"
        b"Body of the eml.\r\n"
    )
    eml_path = tmp_path / "note.eml"
    eml_path.write_bytes(message)

    excerpt, metadata_only, status = extract_excerpt(eml_path, category="text", ext="eml", size=eml_path.stat().st_size)

    assert status == "ok"
    assert metadata_only is False
    assert "Subject: Plain eml" in excerpt
    assert "Body of the eml." in excerpt


def test_extract_eml_falls_back_to_html_body_when_no_plain_text(tmp_path):
    message = (
        b"From: alice@example.com\r\n"
        b"Subject: Html only\r\n"
        b"Content-Type: text/html\r\n"
        b"\r\n"
        b"<html><body><p>Hello <b>world</b></p></body></html>\r\n"
    )
    eml_path = tmp_path / "html.eml"
    eml_path.write_bytes(message)

    excerpt, metadata_only, status = extract_excerpt(eml_path, category="text", ext="eml", size=eml_path.stat().st_size)

    assert status == "ok"
    assert "Hello" in excerpt
    assert "world" in excerpt
    assert "<p>" not in excerpt
