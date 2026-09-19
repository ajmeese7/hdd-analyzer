import json

from hdd_analyzer.scan import build_candidates, load_resumed_keys


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
