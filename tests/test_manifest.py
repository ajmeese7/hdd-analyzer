from hdd_analyzer.manifest import (
    directory_rollup,
    is_manifest_included,
    select_manifest_rows,
    split_verified_rows,
)


def _row(**overrides):
    base = {
        "dedupe_key": "k",
        "path": "C:\\Users\\me\\file.txt",
        "size": 100,
        "category": "text",
        "probabilities": {},
        "value_score": 0.0,
        "extraction_status": "ok",
        "error": None,
    }
    base.update(overrides)
    return base


def test_verified_row_included_via_value_score_threshold():
    row = _row(extraction_status="ok", value_score=2.5)
    assert is_manifest_included(row, min_value=2.0, min_prob=0.7) is True


def test_verified_row_included_via_any_category_probability():
    row = _row(extraction_status="ok", value_score=0.0, probabilities={"original_work": 0.9})
    assert is_manifest_included(row, min_value=2.0, min_prob=0.7) is True


def test_verified_row_excluded_below_both_thresholds():
    row = _row(extraction_status="ok", value_score=1.0, probabilities={"personal": 0.2})
    assert is_manifest_included(row, min_value=2.0, min_prob=0.7) is False


def test_ocr_verified_row_treated_like_content_verified():
    row = _row(extraction_status="ocr", value_score=3.0)
    assert is_manifest_included(row, min_value=2.0, min_prob=0.7) is True


def test_name_only_row_included_when_credentials_probability_meets_threshold():
    row = _row(extraction_status="unsupported", value_score=0.0, probabilities={"credentials": 0.8})
    assert is_manifest_included(row, min_value=2.0, min_prob=0.7) is True


def test_name_only_row_included_for_financial_legal_and_personal_too():
    for category in ("financial_legal", "personal"):
        row = _row(extraction_status="unsupported", probabilities={category: 0.75})
        assert is_manifest_included(row, min_value=2.0, min_prob=0.7) is True, category


def test_name_only_row_excluded_for_irreplaceable_even_at_high_probability():
    """A name-only 'irreplaceable' hit (e.g. a game save) is not trustworthy from the name alone."""
    row = _row(extraction_status="unsupported", probabilities={"irreplaceable": 0.99}, value_score=3.0)
    assert is_manifest_included(row, min_value=2.0, min_prob=0.7) is False


def test_name_only_row_excluded_below_min_prob():
    row = _row(extraction_status="unsupported", probabilities={"credentials": 0.5})
    assert is_manifest_included(row, min_value=2.0, min_prob=0.7) is False


def test_name_only_row_ignores_value_score_entirely():
    """Name-only rows never qualify through value_score, only the gated categories."""
    row = _row(extraction_status="unsupported", value_score=3.0, probabilities={})
    assert is_manifest_included(row, min_value=2.0, min_prob=0.7) is False


def test_unknown_verification_treated_as_name_only():
    row = _row(extraction_status=None, probabilities={"personal": 0.8})
    del row["extraction_status"]
    assert is_manifest_included(row, min_value=2.0, min_prob=0.7) is True
    row_low = _row()
    del row_low["extraction_status"]
    row_low["probabilities"] = {"original_work": 0.99}
    row_low["value_score"] = 3.0
    assert is_manifest_included(row_low, min_value=2.0, min_prob=0.7) is False


def test_generated_category_excluded_even_if_thresholds_met():
    row = _row(category="generated", value_score=3.0, probabilities={"irreplaceable": 0.9})
    assert is_manifest_included(row, min_value=2.0, min_prob=0.7) is False


def test_error_row_excluded_even_if_thresholds_met():
    row = _row(error="boom", value_score=3.0, probabilities={"credentials": 0.9})
    assert is_manifest_included(row, min_value=2.0, min_prob=0.7) is False


def test_select_manifest_rows_filters_the_full_list():
    rows = [
        _row(dedupe_key="a", value_score=3.0),
        _row(dedupe_key="b", value_score=0.0, probabilities={}),
        _row(dedupe_key="c", category="generated", value_score=5.0),
    ]
    selected = select_manifest_rows(rows, min_value=2.0, min_prob=0.7)
    assert [r["dedupe_key"] for r in selected] == ["a"]


def test_split_verified_rows_separates_content_ocr_from_name_only():
    rows = [
        _row(dedupe_key="a", extraction_status="ok"),
        _row(dedupe_key="b", extraction_status="ocr"),
        _row(dedupe_key="c", extraction_status="unsupported"),
        _row(dedupe_key="d", extraction_status="no_text"),
    ]
    verified, name_only = split_verified_rows(rows)
    assert [r["dedupe_key"] for r in verified] == ["a", "b"]
    assert [r["dedupe_key"] for r in name_only] == ["c", "d"]


def test_directory_rollup_counts_files_per_parent_directory():
    rows = [
        _row(path="C:\\Users\\me\\Docs\\a.txt"),
        _row(path="C:\\Users\\me\\Docs\\b.txt"),
        _row(path="C:\\Users\\me\\Photos\\c.jpg"),
    ]
    rollup = directory_rollup(rows)
    assert rollup[0] == ("C:\\Users\\me\\Docs", 2)
    assert ("C:\\Users\\me\\Photos", 1) in rollup


def test_directory_rollup_respects_top_n():
    rows = [_row(path=f"C:\\Users\\me\\Dir{i}\\file.txt") for i in range(5)]
    rollup = directory_rollup(rows, top_n=2)
    assert len(rollup) == 2


def test_directory_rollup_empty_for_no_rows():
    assert directory_rollup([]) == []


def test_manifest_total_bytes_sums_included_rows_sizes():
    rows = [
        _row(dedupe_key="a", size=1000, value_score=3.0),
        _row(dedupe_key="b", size=2000, value_score=3.0),
        _row(dedupe_key="c", size=999999, category="generated", value_score=3.0),
    ]
    selected = select_manifest_rows(rows, min_value=2.0, min_prob=0.7)
    assert sum(r["size"] for r in selected) == 3000
