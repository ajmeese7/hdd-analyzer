import json

from hdd_analyzer.report import (
    _category_table,
    collapse_siblings,
    load_results,
    render_report_md,
    split_by_verified,
    verified_label,
)


_ROWS = [
    {
        "path": "a.txt",
        "size": 100,
        "value_score": 0.9,
        "value_confidence": 0.8,
        "probabilities": {"personal": 0.7},
        "error": None,
    },
    {
        "path": "b.txt",
        "size": 200,
        "value_score": 0.5,
        "value_confidence": 0.6,
        "probabilities": {"personal": 0.4},
        "error": None,
    },
]


def test_category_table_min_prob_filters_out_lower_probability_rows():
    high_threshold = _category_table(_ROWS, "personal", min_prob=0.6)
    assert "a.txt" in high_threshold
    assert "b.txt" not in high_threshold


def test_category_table_min_prob_includes_more_rows_when_lowered():
    low_threshold = _category_table(_ROWS, "personal", min_prob=0.3)
    assert "a.txt" in low_threshold
    assert "b.txt" in low_threshold


def test_render_report_md_threads_min_prob_into_category_sections():
    report = render_report_md(_ROWS, dedupe_savings=0, spend_usd=0.0, top_n=10, min_prob=0.3)
    assert "probability >= 0.3" in report
    assert "b.txt" in report


def test_verified_label_content_when_extraction_ok():
    assert verified_label({"extraction_status": "ok"}) == "content"


def test_verified_label_name_only_for_no_text_timeout_unsupported_error():
    for status in ("no_text", "timeout", "unsupported", "error"):
        assert verified_label({"extraction_status": status}) == "name-only"


def test_verified_label_unknown_for_legacy_rows_missing_the_field():
    assert verified_label({}) == "unknown"


def test_verified_label_ocr_for_ocr_extraction_status():
    assert verified_label({"extraction_status": "ocr"}) == "ocr"


def test_split_by_verified_separates_content_from_everything_else():
    rows = [
        {"path": "a.txt", "extraction_status": "ok"},
        {"path": "b.jpg", "extraction_status": "unsupported"},
        {"path": "c.txt"},
    ]
    verified, unverified = split_by_verified(rows)
    assert verified == [rows[0]]
    assert unverified == [rows[1], rows[2]]


def test_collapse_siblings_leaves_small_directories_alone():
    rows = [{"path": f"C:/saves/save{i}.dat"} for i in range(3)]
    assert collapse_siblings(rows, max_per_dir=3) == rows


def test_collapse_siblings_collapses_directories_over_the_limit():
    rows = [{"path": f"C:/saves/save{i}.dat"} for i in range(5)]
    collapsed = collapse_siblings(rows, max_per_dir=3)
    assert len(collapsed) == 4
    assert collapsed[:3] == rows[:3]
    assert collapsed[3] == {"collapsed": True, "count": 2, "dir": "C:\\saves"}


def test_collapse_siblings_keeps_highest_ranked_rows_per_directory():
    rows = [{"path": f"C:/saves/save{i}.dat"} for i in range(4)]
    collapsed = collapse_siblings(rows, max_per_dir=1)
    assert collapsed[0] == rows[0]
    assert collapsed[1] == {"collapsed": True, "count": 3, "dir": "C:\\saves"}


def test_collapse_siblings_treats_different_directories_independently():
    rows = [{"path": f"C:/a/x{i}.dat"} for i in range(4)] + [{"path": "C:/b/y.dat"}]
    collapsed = collapse_siblings(rows, max_per_dir=3)
    assert len(collapsed) == 5
    assert collapsed[-1] == rows[-1]


def _write_results(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_load_results_keeps_latest_row_per_key(tmp_path):
    _write_results(tmp_path / "results.jsonl", [
        {"dedupe_key": "a", "value_score": 1.0, "error": None},
        {"dedupe_key": "a", "value_score": 2.0, "error": None},
    ])
    assert [r["value_score"] for r in load_results(tmp_path)] == [2.0]


def test_load_results_never_lets_an_error_row_supersede_a_valid_judgment(tmp_path):
    _write_results(tmp_path / "results.jsonl", [
        {"dedupe_key": "a", "value_score": 1.0, "error": None},
        {"dedupe_key": "a", "error": "429 high demand"},
    ])
    assert load_results(tmp_path) == [{"dedupe_key": "a", "value_score": 1.0, "error": None}]


def test_load_results_keeps_an_error_row_when_nothing_better_exists(tmp_path):
    _write_results(tmp_path / "results.jsonl", [
        {"dedupe_key": "a", "error": "timeout"},
        {"dedupe_key": "a", "error": "429 high demand"},
    ])
    assert load_results(tmp_path) == [{"dedupe_key": "a", "error": "429 high demand"}]


def test_load_results_lets_a_valid_row_replace_an_earlier_error(tmp_path):
    _write_results(tmp_path / "results.jsonl", [
        {"dedupe_key": "a", "error": "timeout"},
        {"dedupe_key": "a", "value_score": 1.5, "error": None},
    ])
    assert [r["value_score"] for r in load_results(tmp_path)] == [1.5]
