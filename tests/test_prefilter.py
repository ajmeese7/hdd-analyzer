import json

from hdd_analyzer.prefilter import (
    PREFILTER_FILE,
    DirectoryListing,
    apply,
    build_listings,
    estimated_cost,
    load_decisions,
    parent_of,
)


def _rec(path, ext="txt", size=100, dup_of=None):
    return {"path": path, "ext": ext, "size": size, "dedupe_key": path, "dup_of": dup_of}


def _decision(path, worth, kind="third_party", error=None):
    return {"path": path, "file_count": 12, "worth_scanning": worth, "kind": kind, "input_tokens": 800, "error": error}


def test_parent_of_splits_windows_and_posix_paths():
    assert parent_of("C:\\a\\b\\c.txt") == "C:\\a\\b"
    assert parent_of("/home/a/c.txt") == "/home/a"


def test_build_listings_skips_directories_under_the_file_floor():
    inventory = [_rec(f"C:\\big\\f{i}.txt") for i in range(10)] + [_rec("C:\\small\\a.txt"), _rec("C:\\small\\b.txt")]
    listings = build_listings(inventory, inventory, min_files=10)
    assert set(listings) == {"C:\\big"}


def test_build_listings_only_covers_directories_holding_a_candidate():
    inventory = [_rec(f"C:\\done\\f{i}.txt") for i in range(10)] + [_rec(f"C:\\todo\\f{i}.txt") for i in range(10)]
    candidates = [r for r in inventory if "todo" in r["path"]]
    assert set(build_listings(inventory, candidates, min_files=10)) == {"C:\\todo"}


def test_build_listings_counts_every_inventory_row_including_duplicates():
    inventory = [_rec(f"C:\\d\\f{i}.txt", dup_of=None if i % 2 else "other") for i in range(10)]
    candidates = [r for r in inventory if r["dup_of"] is None]
    listing = build_listings(inventory, candidates, min_files=10)["C:\\d"]
    assert listing.file_count == 10


def test_build_listings_collects_extensions_bytes_and_child_directories():
    inventory = [_rec(f"C:\\d\\f{i}.py", ext="py", size=10) for i in range(8)] + [
        _rec("C:\\d\\notes.md", ext="md", size=5),
        _rec("C:\\d\\data.csv", ext="csv", size=7),
        _rec("C:\\d\\sub\\deep\\x.txt"),
    ]
    listing = build_listings(inventory, inventory, min_files=10)["C:\\d"]
    assert listing.extensions == {"py": 8, "md": 1, "csv": 1}
    assert listing.total_bytes == 92
    assert listing.subdirectories == ["sub"]
    assert listing.to_state()["subdirectory_count"] == 1


def test_build_listings_samples_names_evenly_when_over_the_cap():
    inventory = [_rec(f"C:\\d\\{i:04}.svg", ext="svg") for i in range(400)]
    listing = build_listings(inventory, inventory, min_files=10)["C:\\d"]
    assert len(listing.names_sample) == 40
    assert listing.names_sample[0] == "0000.svg"
    assert listing.names_sample[-1] == "0390.svg"


def test_apply_drops_files_of_directories_below_the_threshold():
    candidates = [_rec("C:\\junk\\a.txt"), _rec("C:\\junk\\b.txt"), _rec("C:\\keep\\c.txt"), _rec("C:\\tiny\\d.txt")]
    listings = {
        "C:\\junk": DirectoryListing("C:\\junk", 12, 0, {}, [], []),
        "C:\\keep": DirectoryListing("C:\\keep", 12, 0, {}, [], []),
    }
    decisions = {"C:\\junk": _decision("C:\\junk", 0.04), "C:\\keep": _decision("C:\\keep", 0.8, "user_content")}
    outcome = apply(candidates, listings, decisions, skip_below=0.10)
    assert [r["path"] for r in outcome.kept] == ["C:\\keep\\c.txt", "C:\\tiny\\d.txt"]
    assert outcome.asked == 2
    assert outcome.skipped_directories == 1
    assert outcome.skipped_files == 2
    assert outcome.skipped_paths == ["C:\\junk"]


def test_apply_keeps_a_directory_whose_call_failed_or_was_never_made():
    candidates = [_rec("C:\\failed\\a.txt"), _rec("C:\\unasked\\b.txt")]
    listings = {
        "C:\\failed": DirectoryListing("C:\\failed", 12, 0, {}, [], []),
        "C:\\unasked": DirectoryListing("C:\\unasked", 12, 0, {}, [], []),
    }
    outcome = apply(candidates, listings, decisions={}, skip_below=0.10)
    assert len(outcome.kept) == 2
    assert outcome.skipped_files == 0


def test_load_decisions_keeps_latest_successful_row_and_retries_failures(tmp_path):
    rows = [
        _decision("C:\\a", 0.9),
        _decision("C:\\a", 0.1),
        _decision("C:\\b", None, error="429"),
    ]
    (tmp_path / PREFILTER_FILE).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    decisions = load_decisions(tmp_path)
    assert decisions["C:\\a"]["worth_scanning"] == 0.1
    assert "C:\\b" not in decisions


def test_load_decisions_empty_without_file(tmp_path):
    assert load_decisions(tmp_path) == {}


def test_estimated_cost_scales_with_listing_count():
    one = {"C:\\a": DirectoryListing("C:\\a", 12, 0, {"txt": 12}, ["a.txt"], [])}
    two = {**one, "C:\\b": DirectoryListing("C:\\b", 12, 0, {"txt": 12}, ["b.txt"], [])}
    assert estimated_cost(two) > estimated_cost(one) > 0
    assert estimated_cost({}) == 0
