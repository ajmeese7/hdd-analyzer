import json

from hdd_analyzer.budget import BudgetTracker
from hdd_analyzer.scan import bill_result, build_candidates, load_resumed_keys


def test_load_resumed_keys_empty_when_no_results_file(tmp_path):
    assert load_resumed_keys(tmp_path) == set()


def test_load_resumed_keys_parses_dedupe_keys(tmp_path):
    results = tmp_path / "results.jsonl"
    results.write_text(
        json.dumps({"dedupe_key": "abc", "path": "x"}) + "\n" + json.dumps({"dedupe_key": "def", "path": "y"}) + "\n",
        encoding="utf-8",
    )
    assert load_resumed_keys(tmp_path) == {"abc", "def"}


def test_load_resumed_keys_ignores_blank_and_malformed_lines(tmp_path):
    results = tmp_path / "results.jsonl"
    results.write_text(json.dumps({"dedupe_key": "abc"}) + "\n\nnot json\n", encoding="utf-8")
    assert load_resumed_keys(tmp_path) == {"abc"}


def test_load_resumed_keys_excludes_error_rows(tmp_path):
    """A row with a non-null error must not count as resolved, so it retries."""
    results = tmp_path / "results.jsonl"
    results.write_text(
        json.dumps({"dedupe_key": "ok", "error": None}) + "\n" + json.dumps({"dedupe_key": "failed", "error": "boom"}) + "\n",
        encoding="utf-8",
    )
    assert load_resumed_keys(tmp_path) == {"ok"}


def test_load_resumed_keys_treats_missing_error_field_as_resolved(tmp_path):
    results = tmp_path / "results.jsonl"
    results.write_text(json.dumps({"dedupe_key": "abc"}) + "\n", encoding="utf-8")
    assert load_resumed_keys(tmp_path) == {"abc"}


def test_build_candidates_skips_records_with_resumed_key(tmp_path):
    sample = tmp_path / "sample.txt"
    sample.write_text("hello", encoding="utf-8")
    records = [
        {
            "path": str(sample),
            "size": 5,
            "mtime": 0.0,
            "ext": "txt",
            "category": "text",
            "dedupe_key": "already-done",
            "dup_of": None,
        }
    ]
    candidates = build_candidates(records, resumed_keys={"already-done"})
    assert candidates == []


def test_build_candidates_skips_duplicate_records(tmp_path):
    sample = tmp_path / "sample.txt"
    sample.write_text("hello", encoding="utf-8")
    records = [
        {
            "path": str(sample),
            "size": 5,
            "mtime": 0.0,
            "ext": "txt",
            "category": "text",
            "dedupe_key": "key1",
            "dup_of": "some/other/path.txt",
        }
    ]
    candidates = build_candidates(records, resumed_keys=set())
    assert candidates == []


def test_build_candidates_includes_new_unique_records(tmp_path):
    sample = tmp_path / "sample.txt"
    sample.write_text("hello world", encoding="utf-8")
    records = [
        {
            "path": str(sample),
            "size": 11,
            "mtime": 0.0,
            "ext": "txt",
            "category": "text",
            "dedupe_key": "key1",
            "dup_of": None,
        }
    ]
    candidates = build_candidates(records, resumed_keys=set())
    assert len(candidates) == 1
    assert candidates[0].dedupe_key == "key1"
    assert candidates[0].excerpt == "hello world"


def test_bill_result_never_bills_error_rows():
    tracker = BudgetTracker(cap_usd=1.0)
    result = {"dedupe_key": "a", "error": "timeout", "input_tokens": None}

    billed = bill_result(tracker, result, fallback_chars=10_000)

    assert billed["input_tokens"] == 0
    assert tracker.spent_usd == 0.0


def test_bill_result_uses_real_usage_when_present():
    tracker = BudgetTracker(cap_usd=1.0)
    result = {"dedupe_key": "a", "error": None, "input_tokens": 1_000_000}

    bill_result(tracker, result, fallback_chars=4)

    assert tracker.spent_usd == 0.042


def test_bill_result_falls_back_to_estimate_only_for_successful_rows():
    tracker = BudgetTracker(cap_usd=1.0)
    result = {"dedupe_key": "a", "error": None, "input_tokens": None}

    bill_result(tracker, result, fallback_chars=400)

    assert tracker.spent_usd > 0.0
