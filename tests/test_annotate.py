import json

from hdd_analyzer.annotate import annotate_run
from hdd_analyzer.report import load_results


def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def test_annotate_preserves_stored_scan_time_status_over_current_extraction(tmp_path):
    """A row with a known scan-time judgment must not be relabeled just because
    a categorization/extraction fix makes today's re-extraction succeed."""
    key_path = tmp_path / "private.pem"
    key_path.write_text("-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n", encoding="utf-8")

    inventory_row = {
        "dedupe_key": "k1",
        "path": str(key_path),
        "size": key_path.stat().st_size,
        "category": "text",
        "ext": "pem",
        "mtime": 0.0,
        "dup_of": None,
    }
    result_row = {
        "dedupe_key": "k1",
        "path": str(key_path),
        "size": inventory_row["size"],
        "category": "binary",
        "probabilities": {"credentials": 0.95},
        "value_score": 2.9,
        "value_confidence": 0.9,
        "input_tokens": 800,
        "metadata_only": True,
        "extraction_status": "unsupported",
        "rubric_version": 1,
        "error": None,
    }

    _write_jsonl(tmp_path / "inventory.jsonl", [inventory_row])
    _write_jsonl(tmp_path / "results.jsonl", [result_row])

    outcome = annotate_run(tmp_path, all_rows=True)

    assert outcome.updated == 0
    assert outcome.became_name_only == []
    assert outcome.rescan_candidates == [str(key_path)]

    results = load_results(tmp_path)
    assert len(results) == 1
    assert results[0]["extraction_status"] == "unsupported"
    assert results[0]["metadata_only"] is True


def test_annotate_backfills_legacy_rows_missing_extraction_status(tmp_path):
    text_path = tmp_path / "notes.txt"
    text_path.write_text("hello world", encoding="utf-8")

    inventory_row = {
        "dedupe_key": "k2",
        "path": str(text_path),
        "size": text_path.stat().st_size,
        "category": "text",
        "ext": "txt",
        "mtime": 0.0,
        "dup_of": None,
    }
    legacy_result_row = {
        "dedupe_key": "k2",
        "path": str(text_path),
        "size": inventory_row["size"],
        "category": "text",
        "probabilities": {"personal": 0.4},
        "value_score": 1.0,
        "value_confidence": 0.5,
        "input_tokens": 800,
        "error": None,
    }

    _write_jsonl(tmp_path / "inventory.jsonl", [inventory_row])
    _write_jsonl(tmp_path / "results.jsonl", [legacy_result_row])

    outcome = annotate_run(tmp_path, all_rows=True)

    assert outcome.updated == 1
    assert outcome.became_name_only == []
    assert outcome.rescan_candidates == []

    results = load_results(tmp_path)
    assert len(results) == 1
    assert results[0]["extraction_status"] == "ok"
    assert results[0]["metadata_only"] is False


def test_annotate_reports_became_name_only_for_backfilled_legacy_rows(tmp_path):
    image_path = tmp_path / "photo.jpg"
    image_path.write_bytes(b"\xff\xd8\xff")

    inventory_row = {
        "dedupe_key": "k3",
        "path": str(image_path),
        "size": image_path.stat().st_size,
        "category": "image",
        "ext": "jpg",
        "mtime": 0.0,
        "dup_of": None,
    }
    legacy_result_row = {
        "dedupe_key": "k3",
        "path": str(image_path),
        "size": inventory_row["size"],
        "category": "image",
        "probabilities": {"financial_legal": 0.9},
        "value_score": 2.5,
        "value_confidence": 0.9,
        "input_tokens": 800,
        "error": None,
    }

    _write_jsonl(tmp_path / "inventory.jsonl", [inventory_row])
    _write_jsonl(tmp_path / "results.jsonl", [legacy_result_row])

    outcome = annotate_run(tmp_path, all_rows=True)

    assert outcome.updated == 1
    assert outcome.became_name_only == [str(image_path)]
