"""Local, free content extraction for the scan stage."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import chardet

from hdd_analyzer.config import EXCERPT_CHAR_CAP, EXTRACT_READ_BYTES

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


def _extract_text_or_code(path: Path) -> str | None:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(EXTRACT_READ_BYTES)
    except OSError:
        return None
    return sanitize_excerpt(raw)


def _extract_pdf(path: Path) -> str | None:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        pages = reader.pages[:3]
        text = "\n".join(page.extract_text() or "" for page in pages)
        return text[:EXCERPT_CHAR_CAP] if text.strip() else None
    except Exception:
        return None


def _strip_tags(data: bytes) -> str:
    return _TAG_RE.sub(b" ", data).decode("utf-8", errors="replace")


def _extract_office_xml(path: Path, member_candidates: tuple[str, ...]) -> str | None:
    try:
        with zipfile.ZipFile(path) as archive:
            for member in member_candidates:
                if member in archive.namelist():
                    data = archive.read(member)[:EXTRACT_READ_BYTES]
                    text = _strip_tags(data)
                    return text[:EXCERPT_CHAR_CAP] if text.strip() else None
    except (OSError, zipfile.BadZipFile):
        return None
    return None


def _extract_legacy_doc(path: Path) -> str | None:
    """Salvage printable ASCII runs from a binary .doc file."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(EXTRACT_READ_BYTES)
    except OSError:
        return None
    runs = _PRINTABLE_RUN_RE.findall(raw)
    if not runs:
        return None
    text = " ".join(run.decode("latin-1") for run in runs)
    return text[:EXCERPT_CHAR_CAP]


def extract_excerpt(path: Path, category: str, ext: str) -> tuple[str | None, bool]:
    """Return (excerpt, metadata_only). excerpt is None if unavailable."""
    ext = ext.lower()

    if category in _TEXT_CATEGORIES:
        return _extract_text_or_code(path), False

    if ext == "pdf":
        return _extract_pdf(path), False

    if ext == "docx":
        return _extract_office_xml(path, ("word/document.xml",)), False

    if ext == "xlsx":
        return _extract_office_xml(path, ("xl/sharedStrings.xml",)), False

    if ext == "doc":
        return _extract_legacy_doc(path), False

    return None, True
