from hdd_analyzer.jev import NO_CONTENT_NOTE, build_state


def test_build_state_with_readable_content_passes_excerpt_through():
    state = build_state(
        path="C:/notes.txt",
        name="notes.txt",
        ext="txt",
        size=10,
        modified=0.0,
        excerpt="hello world",
        metadata_only=False,
    )
    assert state["excerpt"] == "hello world"
    assert state["content_readable"] is True
    assert state["metadata_only"] is False


def test_build_state_metadata_only_replaces_excerpt_with_warning_note():
    state = build_state(
        path="C:/passport.pdf",
        name="passport.pdf",
        ext="pdf",
        size=100,
        modified=0.0,
        excerpt=None,
        metadata_only=True,
    )
    assert state["excerpt"] == NO_CONTENT_NOTE
    assert state["content_readable"] is False
    assert state["metadata_only"] is True
    assert "weak evidence" in state["excerpt"]


def test_build_state_is_json_serializable():
    import json

    state = build_state(
        path="C:/passport.pdf",
        name="passport.pdf",
        ext="pdf",
        size=100,
        modified=0.0,
        excerpt=None,
        metadata_only=True,
    )
    json.dumps(state)  # must not raise
