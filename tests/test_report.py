from hdd_analyzer.report import _category_table, render_report_md


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
