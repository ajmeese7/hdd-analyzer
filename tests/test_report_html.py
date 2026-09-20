import json
import re

from hdd_analyzer.report_html import build_payload, build_tree, render_report_html


def _row(path, value=2.5, probs=None, status="ok", size=100, key=None):
    return {
        "dedupe_key": key or path,
        "path": path,
        "size": size,
        "value_score": value,
        "value_confidence": 0.8,
        "probabilities": probs or {"personal": 0.1},
        "extraction_status": status,
        "input_tokens": 900,
        "error": None,
    }


def _by_name(nodes, name):
    return next(n for n in nodes if n["name"] == name)


def test_build_tree_rolls_up_scanned_and_notable_counts_per_subtree():
    rows = [
        _row("C:\\Users\\a\\docs\\tax.pdf"),
        _row("C:\\Users\\a\\docs\\notes.txt"),
        _row("C:\\Users\\a\\cache\\junk.tmp", value=0.1),
        _row("C:\\Users\\a\\cache\\more.tmp", value=0.1),
    ]
    tree = build_tree(rows, notable=rows[:2], min_prob=0.6)

    assert len(tree.roots) == 1
    root = tree.roots[0]
    assert root["scanned_subtree"] == 4
    assert root["notable_subtree"] == 2


def test_build_tree_prunes_directories_with_no_notable_files():
    rows = [_row("C:\\Users\\a\\docs\\tax.pdf"), _row("C:\\Users\\a\\cache\\junk.tmp", value=0.1)]
    tree = build_tree(rows, notable=rows[:1], min_prob=0.6)

    names = [c["name"] for c in tree.roots[0]["children"]]
    assert "cache" not in names


def test_build_tree_collapses_single_child_chains_with_the_native_separator():
    rows = [_row("C:\\Users\\a\\docs\\tax.pdf"), _row("C:\\Users\\a\\pics\\me.jpg")]
    tree = build_tree(rows, notable=rows, min_prob=0.6)

    root = tree.roots[0]
    assert root["name"] == "C:\\Users\\a"
    assert root["path"] == "C:\\Users\\a"
    assert {c["name"] for c in root["children"]} == {"docs", "pics"}


def test_build_tree_collapses_posix_chains_with_forward_slash():
    rows = [_row("/home/a/docs/tax.pdf"), _row("/home/a/pics/me.jpg")]
    tree = build_tree(rows, notable=rows, min_prob=0.6)

    assert tree.roots[0]["name"] == "/home/a"


def test_build_tree_dirs_carry_a_trailing_separator_so_the_page_can_join_names():
    rows = [_row("C:\\Users\\a\\docs\\tax.pdf"), _row("/home/b/notes.txt")]
    tree = build_tree(rows, notable=rows, min_prob=0.6)

    joined = {tree.dirs[f["dir"]] + f["name"] for f in tree.files}
    assert joined == {"C:\\Users\\a\\docs\\tax.pdf", "/home/b/notes.txt"}


def test_build_tree_counts_categories_at_or_above_min_prob_across_subtree():
    rows = [
        _row("C:\\a\\x\\secrets.env", probs={"credentials": 0.95}),
        _row("C:\\a\\y\\letter.txt", probs={"personal": 0.7, "credentials": 0.2}),
    ]
    tree = build_tree(rows, notable=rows, min_prob=0.6)

    root = tree.roots[0]
    assert root["cats"]["credentials"] == 1
    assert root["cats"]["personal"] == 1
    assert _by_name(root["children"], "x")["cats"] == {**{c: 0 for c in root["cats"]}, "credentials": 1}


def test_build_tree_sorts_children_by_notable_subtree_then_name():
    rows = [
        _row("C:\\a\\zed\\1.txt"),
        _row("C:\\a\\zed\\2.txt"),
        _row("C:\\a\\beta\\1.txt"),
        _row("C:\\a\\alpha\\1.txt"),
    ]
    tree = build_tree(rows, notable=rows, min_prob=0.6)

    assert [c["name"] for c in tree.roots[0]["children"]] == ["zed", "alpha", "beta"]


def test_build_tree_orders_direct_files_by_value_descending():
    rows = [_row("C:\\a\\low.txt", value=2.1), _row("C:\\a\\high.txt", value=2.9)]
    tree = build_tree(rows, notable=rows, min_prob=0.6)

    ordered = [tree.files[i]["name"] for i in tree.roots[0]["files"]]
    assert ordered == ["high.txt", "low.txt"]


def test_build_payload_embeds_only_manifest_included_rows_but_counts_all_scanned():
    results = [
        _row("C:\\a\\keep.txt", value=2.5),
        _row("C:\\a\\meh.txt", value=1.0),
        _row("C:\\a\\name-only-secret.txt", value=1.0, probs={"credentials": 0.9}, status="unsupported"),
        {**_row("C:\\a\\broken.txt"), "error": "boom", "value_score": None},
    ]
    payload = build_payload(results, run_name="r", spend_usd=0.5, dedupe_savings=3, min_value=2.0, min_prob=0.6)

    assert payload["summary"]["scanned"] == 4
    assert payload["summary"]["errors"] == 1
    assert payload["summary"]["notable"] == 2
    assert payload["summary"]["verified"] == {"content": 1, "ocr": 0, "name-only": 1, "unknown": 0}
    assert payload["summary"]["credential_hits"] == 1
    assert {f["name"] for f in payload["files"]} == {"keep.txt", "name-only-secret.txt"}


def test_build_payload_never_embeds_excerpts():
    results = [{**_row("C:\\a\\keep.txt"), "excerpt": "super secret contents"}]
    payload = build_payload(results, run_name="r", spend_usd=0, dedupe_savings=0, min_value=2.0, min_prob=0.6)

    assert "super secret contents" not in json.dumps(payload)


def test_render_report_html_is_self_contained_and_escapes_script_terminators():
    results = [_row("C:\\a\\</script><b>oops.txt")]
    payload = build_payload(results, run_name="r", spend_usd=0, dedupe_savings=0, min_value=2.0, min_prob=0.6)
    html = render_report_html(payload)

    data_block = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S).group(1)
    assert "</script>" not in data_block
    assert json.loads(data_block)["run"] == "r"
    assert "<link" not in html
    assert 'src="http' not in html
