"""Local, free content extraction for the scan stage."""

from __future__ import annotations

import asyncio
import concurrent.futures
import email
import email.policy
import logging
import re
import zipfile
from pathlib import Path

import chardet

from hdd_analyzer.config import (
    ARCHIVE_MEMBER_SIZE_CAP_BYTES,
    EXCERPT_CHAR_CAP,
    EXTRACT_READ_BYTES,
    EXTRACT_TIMEOUT_SECONDS,
)
from hdd_analyzer.paths import to_extended_path

_EXTRACT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="extract")
_LOGGER = logging.getLogger(__name__)

# pypdf logs recoverable parse issues for corrupt PDFs at WARNING; old drives are full of them
logging.getLogger("pypdf").setLevel(logging.ERROR)

_TEXT_CATEGORIES = {"text", "code"}
_NUL_RATIO_THRESHOLD = 0.01
_TAG_RE = re.compile(rb"<[^>]+>")
_PRINTABLE_RUN_RE = re.compile(rb"[\x20-\x7e]{4,}")
_RTF_CONTROL_WORD_RE = re.compile(rb"\\[a-zA-Z]+-?\d*[ ]?")
_RTF_BRACE_RE = re.compile(rb"[{}]")

# Content sniffing: files with no extension or an extension we don't recognize
# fall into category "binary" by default. Most are genuinely binary, but some
# (id_rsa, "Login Data", myKeyStore) are plain text with no clue in the name.
# Sniffing the first few KB and reusing the existing NUL-heavy binary
# detection (inverted: proceed only if it does NOT look binary) catches these
# without hashing or fully reading every binary file on the drive. Extensions
# that are unambiguously binary formats are excluded from sniffing even
# though they fall into the default "binary" category, since a signature
# match beats a text/NUL heuristic for those.
SNIFF_MAX_BYTES = 1 * 1024 * 1024
SNIFF_READ_BYTES = 4096
KNOWN_BINARY_EXTS = frozenset(
    {
        "exe", "dll", "so", "dylib", "sys", "msi", "bin", "dat", "iso", "img", "vhd", "vmdk",
        "sqlite", "sqlite3", "db", "pfx", "p12", "kdbx", "class", "pyc", "o", "a", "lib", "node", "wasm",
    }
)


def sanitize_excerpt(raw: bytes) -> str | None:
    """Decode raw bytes to text, rejecting NUL-heavy binary masquerading as text.

    Returns None if the sample looks binary (too many NUL bytes).
    """
    if not raw:
        return ""
    nul_ratio = raw.count(b"\x00") / len(raw)
    if nul_ratio > _NUL_RATIO_THRESHOLD:
        return None

    detected = chardet.detect(raw)
    encoding = detected.get("encoding") or "utf-8"
    try:
        text = raw.decode(encoding, errors="replace")
    except (LookupError, UnicodeDecodeError):
        text = raw.decode("utf-8", errors="replace")
    return text[:EXCERPT_CHAR_CAP]


# Extraction status values persisted alongside every result row so a
# high-scoring row can be told apart as "we actually read content" (ok) from
# "we tried and got nothing" (no_text, timeout, error) or "we never try
# content for this category" (unsupported: images/av/archives/binaries).
EXTRACTION_STATUS_OK = "ok"
EXTRACTION_STATUS_NO_TEXT = "no_text"
EXTRACTION_STATUS_TIMEOUT = "timeout"
EXTRACTION_STATUS_UNSUPPORTED = "unsupported"
EXTRACTION_STATUS_ERROR = "error"

_SUPPORTED_EXTS = {"pdf", "docx", "xlsx", "doc"}


def _extract_text_or_code(path: Path) -> tuple[str | None, str]:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(EXTRACT_READ_BYTES)
    except OSError:
        return None, EXTRACTION_STATUS_ERROR
    text = sanitize_excerpt(raw)
    if not text:
        return text, EXTRACTION_STATUS_NO_TEXT
    return text, EXTRACTION_STATUS_OK


def _extract_pdf(path: Path) -> tuple[str | None, str]:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        pages = reader.pages[:3]
        text = "\n".join(page.extract_text() or "" for page in pages)
    except Exception:
        return None, EXTRACTION_STATUS_ERROR
    if not text.strip():
        return None, EXTRACTION_STATUS_NO_TEXT
    return text[:EXCERPT_CHAR_CAP], EXTRACTION_STATUS_OK


def _strip_tags(data: bytes) -> str:
    return _TAG_RE.sub(b" ", data).decode("utf-8", errors="replace")


def _extract_office_xml(path: Path, member_candidates: tuple[str, ...]) -> tuple[str | None, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            for member in member_candidates:
                if member not in names:
                    continue
                if archive.getinfo(member).file_size > ARCHIVE_MEMBER_SIZE_CAP_BYTES:
                    continue
                with archive.open(member) as handle:
                    data = handle.read(EXTRACT_READ_BYTES)
                text = _strip_tags(data)
                if not text.strip():
                    return None, EXTRACTION_STATUS_NO_TEXT
                return text[:EXCERPT_CHAR_CAP], EXTRACTION_STATUS_OK
    except (OSError, zipfile.BadZipFile):
        return None, EXTRACTION_STATUS_ERROR
    return None, EXTRACTION_STATUS_NO_TEXT


_EMAIL_HEADERS = ("From", "To", "Date", "Subject")


def _email_body_text(message: email.message.Message) -> str:
    """First text/plain part if present, else the first text/html part with tags stripped."""
    if message.is_multipart():
        parts = list(message.walk())
    else:
        parts = [message]

    for part in parts:
        if part.get_content_type() == "text/plain":
            try:
                return str(part.get_content())
            except Exception:
                continue

    for part in parts:
        if part.get_content_type() == "text/html":
            try:
                html = str(part.get_content())
            except Exception:
                continue
            return _strip_tags(html.encode("utf-8", errors="replace"))

    return ""


def _parse_email_message(message_bytes: bytes) -> tuple[str | None, str]:
    """Build an excerpt from headers (From/To/Date/Subject) plus the plain-text body."""
    try:
        message = email.message_from_bytes(message_bytes, policy=email.policy.default)
    except Exception:
        return None, EXTRACTION_STATUS_ERROR

    header_lines = [f"{name}: {message[name]}" for name in _EMAIL_HEADERS if message[name]]
    body = _email_body_text(message)

    text = "\n".join(header_lines)
    if body:
        text = f"{text}\n\n{body}" if text else body
    text = text.strip()
    if not text:
        return None, EXTRACTION_STATUS_NO_TEXT
    return text[:EXCERPT_CHAR_CAP], EXTRACTION_STATUS_OK


def _extract_eml(path: Path) -> tuple[str | None, str]:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(EXTRACT_READ_BYTES)
    except OSError:
        return None, EXTRACTION_STATUS_ERROR
    return _parse_email_message(raw)


def _extract_emlx(path: Path) -> tuple[str | None, str]:
    """Apple Mail .emlx: a byte-count line, then that many bytes of RFC822 message, then a plist.

    Only the byte-count line and the RFC822 message are used; the trailing
    plist (message flags/metadata, not content) is never read or included.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read(EXTRACT_READ_BYTES + 64)
    except OSError:
        return None, EXTRACTION_STATUS_ERROR

    newline_index = raw.find(b"\n")
    if newline_index == -1:
        return None, EXTRACTION_STATUS_NO_TEXT
    try:
        message_length = int(raw[:newline_index].strip())
    except ValueError:
        return None, EXTRACTION_STATUS_NO_TEXT

    message_start = newline_index + 1
    message_bytes = raw[message_start : message_start + message_length]
    if not message_bytes:
        return None, EXTRACTION_STATUS_NO_TEXT
    return _parse_email_message(message_bytes)


def _extract_rtf(path: Path) -> tuple[str | None, str]:
    """Salvage plain text from RTF by stripping its markup (control words and braces).

    RTF is markup over plain text, similar in spirit to the legacy .doc
    printable-run salvage, but RTF's escaping conventions are regular enough
    to strip directly rather than relying on printable-run detection.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read(EXTRACT_READ_BYTES)
    except OSError:
        return None, EXTRACTION_STATUS_ERROR
    stripped = _RTF_BRACE_RE.sub(b"", _RTF_CONTROL_WORD_RE.sub(b" ", raw))
    text = stripped.decode("latin-1", errors="replace").strip()
    if not text:
        return None, EXTRACTION_STATUS_NO_TEXT
    return text[:EXCERPT_CHAR_CAP], EXTRACTION_STATUS_OK


def should_sniff_binary(category: str, ext: str, size: int) -> bool:
    """Decide whether an unrecognized-extension "binary" file is worth a content peek.

    Only files that fell into the default "binary" category (no extension,
    or an extension we don't recognize) are eligible, and only if they are
    small enough that a 4 KB peek is cheap relative to the file. Extensions
    that are unambiguously binary formats (KNOWN_BINARY_EXTS) are excluded
    even though they default to "binary", since sniffing them would waste an
    IO read on a file we already know is not text.
    """
    if category != "binary":
        return False
    if ext in KNOWN_BINARY_EXTS:
        return False
    return size <= SNIFF_MAX_BYTES


# Compressed payloads can carry few NUL bytes and fool the text heuristic, so
# reject well-known binary signatures before it runs.
_BINARY_MAGIC_PREFIXES = (
    b"MZ", b"\x7fELF", b"PK\x03\x04", b"PK\x05\x06", b"Rar!", b"7z\xbc\xaf\x27\x1c",
    b"\x1f\x8b", b"RIFF", b"\x89PNG", b"\xff\xd8\xff", b"GIF87a", b"GIF89a",
    b"OggS", b"fLaC", b"ID3", b"BM", b"\xca\xfe\xba\xbe", b"SQLite format 3\x00",
)


def _has_binary_magic(raw: bytes) -> bool:
    return raw.startswith(_BINARY_MAGIC_PREFIXES) or raw[4:8] == b"ftyp"


def _extract_sniffed_binary(path: Path) -> tuple[str | None, str]:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(SNIFF_READ_BYTES)
    except OSError:
        return None, EXTRACTION_STATUS_ERROR
    if _has_binary_magic(raw):
        return None, EXTRACTION_STATUS_UNSUPPORTED
    text = sanitize_excerpt(raw)
    if text is None:
        # Sniff failed: this really is binary content, leave it unsupported
        # rather than claiming a failed extraction attempt.
        return None, EXTRACTION_STATUS_UNSUPPORTED
    if not text:
        return text, EXTRACTION_STATUS_NO_TEXT
    return text, EXTRACTION_STATUS_OK


def _extract_legacy_doc(path: Path) -> tuple[str | None, str]:
    """Salvage printable ASCII runs from a binary .doc file."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(EXTRACT_READ_BYTES)
    except OSError:
        return None, EXTRACTION_STATUS_ERROR
    runs = _PRINTABLE_RUN_RE.findall(raw)
    if not runs:
        return None, EXTRACTION_STATUS_NO_TEXT
    text = " ".join(run.decode("latin-1") for run in runs)
    return text[:EXCERPT_CHAR_CAP], EXTRACTION_STATUS_OK


def _dispatch_extract(path: Path, category: str, ext: str, sniff: bool) -> tuple[str | None, str]:
    if ext == "emlx":
        return _extract_emlx(path)
    if ext == "eml":
        return _extract_eml(path)
    if ext == "rtf":
        return _extract_rtf(path)
    if category in _TEXT_CATEGORIES:
        return _extract_text_or_code(path)
    if sniff:
        return _extract_sniffed_binary(path)
    if ext == "pdf":
        return _extract_pdf(path)
    if ext == "docx":
        return _extract_office_xml(path, ("word/document.xml",))
    if ext == "xlsx":
        return _extract_office_xml(path, ("xl/sharedStrings.xml",))
    if ext == "doc":
        return _extract_legacy_doc(path)
    return None, EXTRACTION_STATUS_UNSUPPORTED


def _extraction_eligible(category: str, ext: str, size: int) -> tuple[bool, bool]:
    """Return (eligible, sniff): whether extraction should even be attempted.

    `sniff` is True when eligibility comes from the binary content-sniffing
    fallback rather than a recognized text/document category, so the caller
    can pick the cheaper 4 KB peek instead of the normal dispatch path.
    """
    sniff = should_sniff_binary(category, ext, size)
    eligible = sniff or ext in _SUPPORTED_EXTS or category in _TEXT_CATEGORIES
    return eligible, sniff


def extract_excerpt(path: Path, category: str, ext: str, size: int) -> tuple[str | None, bool, str]:
    """Return (excerpt, metadata_only, extraction_status).

    `excerpt` is None if unavailable. `extraction_status` distinguishes a
    category we never try content extraction for (unsupported) from a
    category we tried and got nothing back for (no_text, timeout, error), so
    a metadata-only classification can be told apart from a genuine content
    read downstream. Extraction runs on a shared thread pool with a hard
    per-file timeout, so a pathological PDF/doc cannot hang the whole scan.
    """
    ext = ext.lower()
    eligible, sniff = _extraction_eligible(category, ext, size)
    if not eligible:
        return None, True, EXTRACTION_STATUS_UNSUPPORTED

    extended_path = Path(to_extended_path(str(path)))
    future = _EXTRACT_EXECUTOR.submit(_dispatch_extract, extended_path, category, ext, sniff)
    try:
        text, status = future.result(timeout=EXTRACT_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError:
        _LOGGER.warning("extraction timed out after %ss, falling back to metadata-only: %s", EXTRACT_TIMEOUT_SECONDS, path)
        return None, True, EXTRACTION_STATUS_TIMEOUT

    metadata_only = status != EXTRACTION_STATUS_OK
    return text, metadata_only, status


async def extract_excerpt_async(path: Path, category: str, ext: str, size: int) -> tuple[str | None, bool, str]:
    """Async twin of `extract_excerpt`, for pipelining extraction with classification.

    Submits to the same shared thread pool and awaits the future without
    blocking the event loop, so IO overlaps API latency instead of
    serializing ahead of it.
    """
    ext = ext.lower()
    eligible, sniff = _extraction_eligible(category, ext, size)
    if not eligible:
        return None, True, EXTRACTION_STATUS_UNSUPPORTED

    extended_path = Path(to_extended_path(str(path)))
    future = _EXTRACT_EXECUTOR.submit(_dispatch_extract, extended_path, category, ext, sniff)
    try:
        text, status = await asyncio.wait_for(asyncio.wrap_future(future), timeout=EXTRACT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        _LOGGER.warning("extraction timed out after %ss, falling back to metadata-only: %s", EXTRACT_TIMEOUT_SECONDS, path)
        return None, True, EXTRACTION_STATUS_TIMEOUT

    metadata_only = status != EXTRACTION_STATUS_OK
    return text, metadata_only, status
