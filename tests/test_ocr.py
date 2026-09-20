import pytest

from hdd_analyzer import ocr
from hdd_analyzer.ocr import is_ocr_eligible, resolve_tesseract, select_ocr_candidates


# --- resolve_tesseract precedence -------------------------------------------------


def test_resolve_tesseract_prefers_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("TESSERACT_CMD", str(tmp_path / "tesseract.exe"))
    monkeypatch.setattr(ocr.shutil, "which", lambda _name: "/somewhere/else/tesseract")
    assert resolve_tesseract() == str(tmp_path / "tesseract.exe")


def test_resolve_tesseract_falls_back_to_which_when_env_unset(monkeypatch):
    monkeypatch.delenv("TESSERACT_CMD", raising=False)
    monkeypatch.setattr(ocr.shutil, "which", lambda _name: r"C:\tools\tesseract.exe")
    assert resolve_tesseract() == r"C:\tools\tesseract.exe"


def test_resolve_tesseract_falls_back_to_default_path_when_it_exists(monkeypatch, tmp_path):
    fake_default = tmp_path / "Tesseract-OCR" / "tesseract.exe"
    fake_default.parent.mkdir()
    fake_default.write_text("", encoding="utf-8")

    monkeypatch.delenv("TESSERACT_CMD", raising=False)
    monkeypatch.setattr(ocr.shutil, "which", lambda _name: None)
    monkeypatch.setattr(ocr, "_DEFAULT_WINDOWS_TESSERACT", str(fake_default))

    assert resolve_tesseract() == str(fake_default)


def test_resolve_tesseract_raises_clear_error_naming_runbook(monkeypatch, tmp_path):
    missing_default = tmp_path / "does-not-exist" / "tesseract.exe"

    monkeypatch.delenv("TESSERACT_CMD", raising=False)
    monkeypatch.setattr(ocr.shutil, "which", lambda _name: None)
    monkeypatch.setattr(ocr, "_DEFAULT_WINDOWS_TESSERACT", str(missing_default))

    with pytest.raises(RuntimeError, match="RUNBOOK"):
        resolve_tesseract()


# --- row selection -----------------------------------------------------------------


def _row(**overrides):
    base = {
        "dedupe_key": "k",
        "path": "F:\\Users\\ajmee\\Downloads\\drivers_license.jpg",
        "category": "image",
        "extraction_status": "unsupported",
        "value_score": 3.0,
        "error": None,
    }
    base.update(overrides)
    return base


def test_is_ocr_eligible_true_for_name_only_image():
    assert is_ocr_eligible(_row()) is True


def test_is_ocr_eligible_false_for_content_verified_image():
    """An image row is never actually extraction_status "ok" in practice, but if it were, it's not name-only."""
    assert is_ocr_eligible(_row(extraction_status="ok")) is False


def test_is_ocr_eligible_true_for_no_text_pdf():
    row = _row(category="doc", path="C:\\scans\\statement.pdf", extraction_status="no_text")
    assert is_ocr_eligible(row) is True


def test_is_ocr_eligible_false_for_pdf_with_other_non_ok_status():
    for status in ("unsupported", "timeout", "error"):
        row = _row(category="doc", path="C:\\scans\\statement.pdf", extraction_status=status)
        assert is_ocr_eligible(row) is False, status


def test_is_ocr_eligible_false_for_non_image_non_pdf_category():
    row = _row(category="doc", path="C:\\docs\\report.docx", extraction_status="no_text")
    assert is_ocr_eligible(row) is False


def test_is_ocr_eligible_false_for_error_rows():
    assert is_ocr_eligible(_row(error="boom")) is False


def test_select_ocr_candidates_filters_by_min_value_and_sorts_by_value_score():
    rows = [
        _row(dedupe_key="low", value_score=1.0),
        _row(dedupe_key="high", value_score=3.0),
        _row(dedupe_key="mid", value_score=2.5),
    ]
    selected = select_ocr_candidates(rows, top_n=200, take_all=False, min_value=2.0)
    assert [r["dedupe_key"] for r in selected] == ["high", "mid"]


def test_select_ocr_candidates_respects_top_n():
    rows = [_row(dedupe_key=str(i), value_score=float(i)) for i in range(10)]
    selected = select_ocr_candidates(rows, top_n=3, take_all=False, min_value=0.0)
    assert [r["dedupe_key"] for r in selected] == ["9", "8", "7"]


def test_select_ocr_candidates_all_ignores_min_value_and_top_n():
    rows = [
        _row(dedupe_key="a", value_score=0.1),
        _row(dedupe_key="b", value_score=0.2),
    ]
    selected = select_ocr_candidates(rows, top_n=1, take_all=True, min_value=2.0)
    assert {r["dedupe_key"] for r in selected} == {"a", "b"}


def test_select_ocr_candidates_respects_limit_after_take_all():
    rows = [_row(dedupe_key=str(i)) for i in range(5)]
    selected = select_ocr_candidates(rows, take_all=True, limit=2)
    assert len(selected) == 2


def test_select_ocr_candidates_excludes_ineligible_rows():
    rows = [
        _row(dedupe_key="eligible"),
        _row(dedupe_key="verified", extraction_status="ok"),
    ]
    selected = select_ocr_candidates(rows, take_all=True)
    assert [r["dedupe_key"] for r in selected] == ["eligible"]

