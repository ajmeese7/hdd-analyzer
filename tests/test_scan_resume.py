import json

from hdd_analyzer.budget import BudgetTracker
from hdd_analyzer.jev import RUBRIC_VERSION
from hdd_analyzer.scan import load_outdated_rubric_keys, bill_result, build_candidates, eligible_records, load_name_only_keys, load_resumed_keys


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


def test_load_name_only_keys_empty_when_no_results_file(tmp_path):
    assert load_name_only_keys(tmp_path) == set()


def _write_inventory(run_dir, records):
    (run_dir / "inventory.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )


def _write_results(run_dir, rows):
    (run_dir / "results.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )


def test_load_name_only_keys_selects_non_ok_extraction_status(tmp_path):
    _write_inventory(
        tmp_path,
        [
            {"dedupe_key": "verified", "ext": "txt", "size": 10},
            {"dedupe_key": "name-only", "ext": "pem", "size": 10},
            {"dedupe_key": "legacy", "ext": "txt", "size": 10},
        ],
    )
    _write_results(
        tmp_path,
        [
            {"dedupe_key": "verified", "extraction_status": "ok"},
            {"dedupe_key": "name-only", "extraction_status": "unsupported"},
            {"dedupe_key": "legacy"},  # missing field entirely
        ],
    )
    assert load_name_only_keys(tmp_path) == {"name-only", "legacy"}


def test_load_name_only_keys_uses_latest_row_per_dedupe_key(tmp_path):
    _write_inventory(tmp_path, [{"dedupe_key": "k", "ext": "txt", "size": 10}])
    _write_results(
        tmp_path,
        [
            {"dedupe_key": "k", "extraction_status": "unsupported"},
            {"dedupe_key": "k", "extraction_status": "ok"},
        ],
    )
    assert load_name_only_keys(tmp_path) == set()


def test_load_name_only_keys_excludes_rows_ineligible_for_extraction_today(tmp_path):
    """A name-only row must not be selected if today's extraction would never even try it.

    "generated" is never extraction-eligible (Unity .meta etc.), and a known
    binary extension like sqlite is excluded from the sniffing fallback, so
    both should be dropped even though they were judged from filename alone.
    """
    _write_inventory(
        tmp_path,
        [
            {"dedupe_key": "eligible", "ext": "pem", "size": 10},
            {"dedupe_key": "generated", "ext": "meta", "size": 10},
            {"dedupe_key": "known-binary", "ext": "sqlite", "size": 10},
        ],
    )
    _write_results(
        tmp_path,
        [
            {"dedupe_key": "eligible", "extraction_status": "unsupported"},
            {"dedupe_key": "generated", "extraction_status": "unsupported"},
            {"dedupe_key": "known-binary", "extraction_status": "unsupported"},
        ],
    )
    assert load_name_only_keys(tmp_path) == {"eligible"}


def test_eligible_records_only_keys_bypasses_resumed_check_for_included_keys():
    """--only-name-only must re-include a previously-successful row it targets."""
    records = [{"dedupe_key": "name-only-key", "dup_of": None}]
    eligible = eligible_records(records, resumed_keys={"name-only-key"}, only_keys={"name-only-key"})
    assert eligible == records


def test_eligible_records_only_keys_excludes_rows_not_in_the_set():
    records = [
        {"dedupe_key": "verified", "dup_of": None},
        {"dedupe_key": "name-only", "dup_of": None},
    ]
    eligible = eligible_records(records, resumed_keys=set(), only_keys={"name-only"})
    assert eligible == [records[1]]


def test_eligible_records_only_keys_still_skips_duplicates():
    records = [{"dedupe_key": "name-only", "dup_of": "some/other/path"}]
    eligible = eligible_records(records, resumed_keys=set(), only_keys={"name-only"})
    assert eligible == []


def test_was_name_only_uses_stored_status_when_present():
    from hdd_analyzer.scan import was_name_only

    assert was_name_only({"extraction_status": "unsupported", "category": "text"}) is True
    assert was_name_only({"extraction_status": "ok", "category": "image"}) is False


def test_was_name_only_infers_legacy_rows_from_category():
    from hdd_analyzer.scan import was_name_only

    assert was_name_only({"category": "image"}) is True
    assert was_name_only({"category": "binary"}) is True
    assert was_name_only({"category": "text"}) is False
    assert was_name_only({"category": "code"}) is False


def test_eligible_records_excludes_generated_category_even_without_dup_of():
    records = [
        {"dedupe_key": "meta1", "dup_of": None, "category": "generated", "ext": "meta"},
        {"dedupe_key": "real1", "dup_of": None, "category": "text", "ext": "txt"},
    ]
    eligible = eligible_records(records, resumed_keys=set())
    assert eligible == [records[1]]


def test_build_candidates_excludes_generated_category(tmp_path):
    sample = tmp_path / "sample.meta"
    sample.write_text("guid: abc123", encoding="utf-8")
    records = [
        {
            "path": str(sample),
            "size": sample.stat().st_size,
            "mtime": 0.0,
            "ext": "meta",
            "category": "generated",
            "dedupe_key": "meta1",
            "dup_of": None,
        }
    ]
    candidates = build_candidates(records, resumed_keys=set())
    assert candidates == []


def test_eligible_records_exclude_exts_drops_matching_extensions():
    records = [
        {"dedupe_key": "a", "dup_of": None, "category": "image", "ext": "jpg"},
        {"dedupe_key": "b", "dup_of": None, "category": "text", "ext": "txt"},
    ]
    eligible = eligible_records(records, resumed_keys=set(), exclude_exts={"jpg"})
    assert eligible == [records[1]]


def test_eligible_records_exclude_exts_is_case_insensitive_on_stored_ext():
    records = [{"dedupe_key": "a", "dup_of": None, "category": "text", "ext": "TXT"}]
    eligible = eligible_records(records, resumed_keys=set(), exclude_exts={"txt"})
    assert eligible == []


def test_build_candidates_excerpt_override_short_circuits_extraction(tmp_path):
    """A dedupe key present in excerpt_override must skip extraction entirely.

    The record's path deliberately does not exist on disk: if extraction were
    attempted anyway, it would hit an OSError and produce extraction_status
    "error" instead of the override's "ocr" status, so this also proves the
    override wins over a real (failing) extraction attempt.
    """
    from hdd_analyzer.scan import EXTRACTION_STATUS_OCR

    records = [
        {
            "path": str(tmp_path / "does-not-exist.txt"),
            "size": 123,
            "mtime": 0.0,
            "ext": "txt",
            "category": "text",
            "dedupe_key": "ocr-key",
            "dup_of": None,
        }
    ]

    candidates = build_candidates(records, resumed_keys=set(), excerpt_override={"ocr-key": "recovered text"})

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.excerpt == "recovered text"
    assert candidate.metadata_only is False
    assert candidate.extraction_status == EXTRACTION_STATUS_OCR


def test_build_candidates_excerpt_override_ignores_keys_not_present(tmp_path):
    """A record whose dedupe key is absent from excerpt_override still extracts normally."""
    sample = tmp_path / "sample.txt"
    sample.write_text("hello world", encoding="utf-8")
    records = [
        {
            "path": str(sample),
            "size": 11,
            "mtime": 0.0,
            "ext": "txt",
            "category": "text",
            "dedupe_key": "other-key",
            "dup_of": None,
        }
    ]

    candidates = build_candidates(records, resumed_keys=set(), excerpt_override={"ocr-key": "recovered text"})

    assert len(candidates) == 1
    assert candidates[0].excerpt == "hello world"
    assert candidates[0].extraction_status == "ok"


def test_load_outdated_rubric_keys_selects_rows_scored_under_an_older_rubric(tmp_path):
    rows = [
        {"dedupe_key": "legacy", "value_score": 1.0, "error": None},
        {"dedupe_key": "old", "value_score": 1.0, "rubric_version": RUBRIC_VERSION - 1, "error": None},
        {"dedupe_key": "current", "value_score": 1.0, "rubric_version": RUBRIC_VERSION, "error": None},
        {"dedupe_key": "failed", "error": "boom"},
    ]
    (tmp_path / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert load_outdated_rubric_keys(tmp_path) == {"legacy", "old"}


def test_load_outdated_rubric_keys_uses_the_latest_valid_row(tmp_path):
    rows = [
        {"dedupe_key": "a", "value_score": 1.0, "error": None},
        {"dedupe_key": "a", "value_score": 1.0, "rubric_version": RUBRIC_VERSION, "error": None},
        {"dedupe_key": "a", "error": "429"},
    ]
    (tmp_path / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert load_outdated_rubric_keys(tmp_path) == set()
