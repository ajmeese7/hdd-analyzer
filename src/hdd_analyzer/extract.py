"""Local, free content extraction for the scan stage."""

from __future__ import annotations

import asyncio
import concurrent.futures
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


def _dispatch_extract(path: Path, category: str, ext: str) -> tuple[str | None, str]:
    if category in _TEXT_CATEGORIES:
        return _extract_text_or_code(path)
    if ext == "pdf":
        return _extract_pdf(path)
    if ext == "docx":
        return _extract_office_xml(path, ("word/document.xml",))
    if ext == "xlsx":
        return _extract_office_xml(path, ("xl/sharedStrings.xml",))
    if ext == "doc":
        return _extract_legacy_doc(path)
    return None, EXTRACTION_STATUS_UNSUPPORTED


def extract_excerpt(path: Path, category: str, ext: str) -> tuple[str | None, bool, str]:
    """Return (excerpt, metadata_only, extraction_status).

    `excerpt` is None if unavailable. `extraction_status` distinguishes a
    category we never try content extraction for (unsupported) from a
    category we tried and got nothing back for (no_text, timeout, error), so
    a metadata-only classification can be told apart from a genuine content
    read downstream. Extraction runs on a shared thread pool with a hard
    per-file timeout, so a pathological PDF/doc cannot hang the whole scan.
    """
    ext = ext.lower()
    if ext not in _SUPPORTED_EXTS and category not in _TEXT_CATEGORIES:
        return None, True, EXTRACTION_STATUS_UNSUPPORTED

    extended_path = Path(to_extended_path(str(path)))
    future = _EXTRACT_EXECUTOR.submit(_dispatch_extract, extended_path, category, ext)
    try:
        text, status = future.result(timeout=EXTRACT_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError:
        _LOGGER.warning("extraction timed out after %ss, falling back to metadata-only: %s", EXTRACT_TIMEOUT_SECONDS, path)
        return None, True, EXTRACTION_STATUS_TIMEOUT

    metadata_only = status != EXTRACTION_STATUS_OK
    return text, metadata_only, status


async def extract_excerpt_async(path: Path, category: str, ext: str) -> tuple[str | None, bool, str]:
    """Async twin of `extract_excerpt`, for pipelining extraction with classification.

    Submits to the same shared thread pool and awaits the future without
    blocking the event loop, so IO overlaps API latency instead of
    serializing ahead of it.
    """
    ext = ext.lower()
    if ext not in _SUPPORTED_EXTS and category not in _TEXT_CATEGORIES:
        return None, True, EXTRACTION_STATUS_UNSUPPORTED

    extended_path = Path(to_extended_path(str(path)))
    future = _EXTRACT_EXECUTOR.submit(_dispatch_extract, extended_path, category, ext)
    try:
        text, status = await asyncio.wait_for(asyncio.wrap_future(future), timeout=EXTRACT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        _LOGGER.warning("extraction timed out after %ss, falling back to metadata-only: %s", EXTRACT_TIMEOUT_SECONDS, path)
        return None, True, EXTRACTION_STATUS_TIMEOUT

    metadata_only = status != EXTRACTION_STATUS_OK
    return text, metadata_only, status
