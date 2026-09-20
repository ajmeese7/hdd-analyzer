"""OCR stage: tesseract text recognition for name-only image and PDF rows.

This is a separate, free (no API spend) enrichment pass over an existing
run's results.jsonl, not part of `scan`. It targets exactly the rows a scan
could never read: images (always metadata_only, see extract.py) and PDFs
whose text layer was empty (`extraction_status == "no_text"`, e.g. a
scanned document with no OCR text layer of its own). A high value_score on
one of these rows means Jev guessed from the filename alone; OCR gives that
guess a chance to be confirmed or refuted by actual content.

`ocr.jsonl` (written to `runs/NAME/ocr.jsonl`) holds per-file OCR excerpts,
which may include credentials or other PII pulled straight from file
content. It is never printed verbatim by this module or by `scan --from-ocr`
and, like the rest of `runs/`, is gitignored.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hdd_analyzer.config import EXCERPT_CHAR_CAP
from hdd_analyzer.paths import to_extended_path
from hdd_analyzer.scan import was_name_only

_LOGGER = logging.getLogger(__name__)

# Tesseract is not on PATH in the verified environment; TESSERACT_CMD (.env)
# or this default winget install location are the fallbacks. See
# docs/RUNBOOK.md's "OCR setup" section for the one-time install step.
_DEFAULT_WINDOWS_TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# Extensions tesseract can actually read. HEIC is a valid "image" category
# extension (see config.py) but tesseract has no HEIC decoder, so it is
# deliberately excluded and reported as "unsupported" rather than attempted.
OCR_IMAGE_EXTS = frozenset({"jpg", "jpeg", "png", "tif", "tiff", "bmp", "webp", "gif"})

OCR_STATUS_OK = "ok"
OCR_STATUS_NO_TEXT = "no_text"
OCR_STATUS_TIMEOUT = "timeout"
OCR_STATUS_ERROR = "error"
OCR_STATUS_UNSUPPORTED = "unsupported"

OCR_IMAGE_TIMEOUT_SECONDS = 30
OCR_PDF_TIMEOUT_SECONDS = 60
OCR_PDF_MAX_PAGES = 3
OCR_PDF_RENDER_DPI = 150
OCR_WORKERS = 4
OCR_PROGRESS_INTERVAL = 25

OCR_DEFAULT_TOP_N = 200
OCR_DEFAULT_MIN_VALUE = 2.0


def resolve_tesseract() -> str:
    """Locate the tesseract binary.

    Precedence: `TESSERACT_CMD` env var, then `tesseract` on PATH, then the
    default Windows winget install location if it exists on disk. Raises a
    clear, actionable error (pointing at the RUNBOOK) if none resolve,
    rather than letting a bare FileNotFoundError surface from subprocess.
    """
    env_path = os.environ.get("TESSERACT_CMD")
    if env_path:
        return env_path

    which_path = shutil.which("tesseract")
    if which_path:
        return which_path

    if Path(_DEFAULT_WINDOWS_TESSERACT).exists():
        return _DEFAULT_WINDOWS_TESSERACT

    raise RuntimeError(
        "tesseract not found: not on PATH, TESSERACT_CMD is unset, and the default install path "
        f"({_DEFAULT_WINDOWS_TESSERACT}) does not exist. See docs/RUNBOOK.md, 'OCR setup' section, "
        "to install it."
    )


def _tesseract_creationflags() -> int:
    # CREATE_NO_WINDOW only exists on win32; guard so this module still
    # imports (though OCR is Windows-only in practice) on other platforms.
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _invoke_tesseract(tesseract_cmd: str, path: Path, timeout: int) -> tuple[str | None, str]:
    """Run tesseract on one image file, returning (text, ocr_status)."""
    extended_path = to_extended_path(str(path))
    try:
        completed = subprocess.run(
            [tesseract_cmd, extended_path, "stdout", "-l", "eng", "--psm", "3"],
            capture_output=True,
            timeout=timeout,
            creationflags=_tesseract_creationflags(),
        )
    except subprocess.TimeoutExpired:
        return None, OCR_STATUS_TIMEOUT
    except OSError as exc:
        _LOGGER.warning("tesseract invocation failed for %s: %s", path, exc)
        return None, OCR_STATUS_ERROR

    if completed.returncode != 0:
        return None, OCR_STATUS_ERROR

    text = completed.stdout.decode("utf-8", errors="replace").strip()
    return (text, OCR_STATUS_OK) if text else (None, OCR_STATUS_NO_TEXT)


def ocr_image(path: Path, timeout: int = 30) -> str | None:
    """OCR a single image file with tesseract. None on empty output, timeout, or error."""
    tesseract_cmd = resolve_tesseract()
    text, _status = _invoke_tesseract(tesseract_cmd, path, timeout)
    return text


def _ocr_pdf_with_status(path: Path, max_pages: int, timeout: int) -> tuple[str | None, str]:
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return None, OCR_STATUS_ERROR

    tesseract_cmd = resolve_tesseract()

    try:
        pdf = pdfium.PdfDocument(to_extended_path(str(path)))
    except Exception:  # noqa: BLE001 - corrupt/unreadable PDFs must not crash the run
        return None, OCR_STATUS_ERROR

    tmp_paths: list[Path] = []
    try:
        page_count = min(len(pdf), max_pages)
        if page_count == 0:
            return None, OCR_STATUS_NO_TEXT

        per_page_timeout = max(1, timeout // page_count)
        texts: list[str] = []
        attempted_pages = 0
        saw_timeout = False
        saw_error = False

        for index in range(page_count):
            try:
                page = pdf[index]
                try:
                    bitmap = page.render(scale=OCR_PDF_RENDER_DPI / 72)
                    try:
                        pil_image = bitmap.to_pil()
                    finally:
                        bitmap.close()
                finally:
                    page.close()
            except Exception:  # noqa: BLE001 - a single bad page must not abort the rest
                continue

            attempted_pages += 1
            fd, tmp_name = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            tmp_path = Path(tmp_name)
            tmp_paths.append(tmp_path)
            pil_image.save(tmp_path)

            text, status = _invoke_tesseract(tesseract_cmd, tmp_path, per_page_timeout)
            if status == OCR_STATUS_TIMEOUT:
                saw_timeout = True
            elif status == OCR_STATUS_ERROR:
                saw_error = True
            if text:
                texts.append(text)

        joined = "\n".join(texts).strip()
        if joined:
            return joined[:EXCERPT_CHAR_CAP], OCR_STATUS_OK
        if attempted_pages == 0:
            # Every page failed to even rasterize (corrupt/encrypted content stream).
            return None, OCR_STATUS_NO_TEXT
        if saw_timeout:
            return None, OCR_STATUS_TIMEOUT
        if saw_error:
            # Every rasterized page failed the actual tesseract invocation (a real
            # error), as opposed to tesseract running cleanly and finding nothing.
            return None, OCR_STATUS_ERROR
        return None, OCR_STATUS_NO_TEXT
    except Exception:  # noqa: BLE001 - guard against any other pypdfium2 failure on corrupt PDFs
        return None, OCR_STATUS_ERROR
    finally:
        try:
            pdf.close()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass
        for tmp_path in tmp_paths:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def ocr_pdf(path: Path, max_pages: int = 3, timeout: int = 60) -> str | None:
    """Rasterize the first `max_pages` pages at ~150dpi and OCR each. None on failure."""
    text, _status = _ocr_pdf_with_status(path, max_pages, timeout)
    return text


def _row_ext(row: dict[str, Any]) -> str:
    return Path(row.get("path", "")).suffix.lstrip(".").lower()


def is_ocr_eligible(row: dict[str, Any]) -> bool:
    """True if `row` is a name-only image, or a name-only PDF with an empty text layer.

    Both cases are files Jev judged from filename and metadata alone, where
    OCR has a real chance of recovering actual content that changes the
    picture.
    """
    if row.get("error"):
        return False
    if not was_name_only(row):
        return False
    if row.get("category") == "image":
        return True
    return _row_ext(row) == "pdf" and row.get("extraction_status") == OCR_STATUS_NO_TEXT


def select_ocr_candidates(
    results: list[dict[str, Any]],
    top_n: int = OCR_DEFAULT_TOP_N,
    take_all: bool = False,
    min_value: float = OCR_DEFAULT_MIN_VALUE,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Pick rows worth OCRing out of `results` (already latest-per-dedupe-key).

    Default: the top `top_n` eligible rows by value_score, restricted to
    value_score >= min_value (the high-ranked filename guesses worth
    verifying). `take_all` ignores value_score/top_n entirely and OCRs every
    eligible row. `limit`, when given, caps the final list either way.
    """
    eligible = [row for row in results if is_ocr_eligible(row)]

    if take_all:
        selected = eligible
    else:
        above_min = [row for row in eligible if (row.get("value_score") or 0) >= min_value]
        above_min.sort(key=lambda row: row["value_score"], reverse=True)
        selected = above_min[:top_n]

    if limit is not None:
        selected = selected[:limit]
    return selected


def _ocr_dispatch(tesseract_cmd: str, path: Path, ext: str) -> tuple[str | None, str]:
    if ext == "pdf":
        return _ocr_pdf_with_status(path, OCR_PDF_MAX_PAGES, OCR_PDF_TIMEOUT_SECONDS)
    if ext in OCR_IMAGE_EXTS:
        return _invoke_tesseract(tesseract_cmd, path, OCR_IMAGE_TIMEOUT_SECONDS)
    return None, OCR_STATUS_UNSUPPORTED


def _ocr_one(tesseract_cmd: str, row: dict[str, Any]) -> dict[str, Any]:
    path = Path(row["path"])
    ext = _row_ext(row)
    text, status = _ocr_dispatch(tesseract_cmd, path, ext)
    excerpt = text[:EXCERPT_CHAR_CAP] if text else None
    return {
        "dedupe_key": row["dedupe_key"],
        "path": row["path"],
        "ocr_status": status,
        "excerpt_chars": len(excerpt) if excerpt else 0,
        "excerpt": excerpt,
    }


@dataclass(frozen=True)
class OcrOutcome:
    ok: int
    no_text: int
    timeout: int
    error: int
    unsupported: int
    elapsed_seconds: float


def run_ocr(run_dir: Path, candidates: list[dict[str, Any]], workers: int = OCR_WORKERS) -> OcrOutcome:
    """OCR every row in `candidates`, appending each result to runs/NAME/ocr.jsonl.

    Runs on a thread pool since tesseract invocation is IO/subprocess bound.
    Prints progress every OCR_PROGRESS_INTERVAL files to stderr.
    """
    tesseract_cmd = resolve_tesseract()
    ocr_path = run_dir / "ocr.jsonl"
    counts = {
        OCR_STATUS_OK: 0,
        OCR_STATUS_NO_TEXT: 0,
        OCR_STATUS_TIMEOUT: 0,
        OCR_STATUS_ERROR: 0,
        OCR_STATUS_UNSUPPORTED: 0,
    }
    start = time.monotonic()
    processed = 0
    total = len(candidates)

    with open(ocr_path, "a", encoding="utf-8") as out:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_ocr_one, tesseract_cmd, row) for row in candidates]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                counts[result["ocr_status"]] += 1
                out.write(json.dumps(result) + "\n")
                out.flush()
                processed += 1
                if processed % OCR_PROGRESS_INTERVAL == 0:
                    print(f"  ocr progress: {processed}/{total}", file=sys.stderr)

    return OcrOutcome(
        ok=counts[OCR_STATUS_OK],
        no_text=counts[OCR_STATUS_NO_TEXT],
        timeout=counts[OCR_STATUS_TIMEOUT],
        error=counts[OCR_STATUS_ERROR],
        unsupported=counts[OCR_STATUS_UNSUPPORTED],
        elapsed_seconds=time.monotonic() - start,
    )


def load_ocr_excerpts(run_dir: Path) -> dict[str, str]:
    """Latest-per-dedupe-key OCR excerpts with ocr_status == "ok", for `scan --from-ocr`."""
    ocr_path = run_dir / "ocr.jsonl"
    if not ocr_path.exists():
        return {}

    latest: dict[str, dict[str, Any]] = {}
    with open(ocr_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = record.get("dedupe_key")
            if key:
                latest[key] = record

    return {
        key: record["excerpt"]
        for key, record in latest.items()
        if record.get("ocr_status") == OCR_STATUS_OK and record.get("excerpt")
    }
