from hdd_analyzer.paths import from_extended_path, to_extended_path


def test_to_extended_path_converts_drive_path():
    assert to_extended_path("C:\\foo\\bar") == "\\\\?\\C:\\foo\\bar"


def test_to_extended_path_converts_unc_path():
    assert to_extended_path("\\\\server\\share\\foo") == "\\\\?\\UNC\\server\\share\\foo"


def test_to_extended_path_is_idempotent():
    already_extended = "\\\\?\\C:\\foo\\bar"
    assert to_extended_path(already_extended) == already_extended

    already_extended_unc = "\\\\?\\UNC\\server\\share\\foo"
    assert to_extended_path(already_extended_unc) == already_extended_unc


def test_to_extended_path_normalizes_forward_slashes():
    assert to_extended_path("F:/foo/bar") == "\\\\?\\F:\\foo\\bar"


def test_from_extended_path_strips_drive_prefix():
    assert from_extended_path("\\\\?\\C:\\foo\\bar") == "C:\\foo\\bar"


def test_from_extended_path_strips_unc_prefix():
    assert from_extended_path("\\\\?\\UNC\\server\\share\\foo") == "\\\\server\\share\\foo"


def test_from_extended_path_leaves_plain_path_unchanged():
    assert from_extended_path("C:\\foo\\bar") == "C:\\foo\\bar"


def test_round_trip_drive_path():
    original = "C:\\foo\\bar"
    assert from_extended_path(to_extended_path(original)) == original


def test_round_trip_unc_path():
    original = "\\\\server\\share\\foo"
    assert from_extended_path(to_extended_path(original)) == original
