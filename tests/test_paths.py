import sys

import pytest

from hdd_analyzer.paths import from_extended_path, pure_path, to_extended_path

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="extended-length paths are a Windows concept")


@windows_only
def test_to_extended_path_converts_drive_path():
    assert to_extended_path("C:\\foo\\bar") == "\\\\?\\C:\\foo\\bar"


@windows_only
def test_to_extended_path_converts_unc_path():
    assert to_extended_path("\\\\server\\share\\foo") == "\\\\?\\UNC\\server\\share\\foo"


def test_to_extended_path_is_idempotent():
    already_extended = "\\\\?\\C:\\foo\\bar"
    assert to_extended_path(already_extended) == already_extended

    already_extended_unc = "\\\\?\\UNC\\server\\share\\foo"
    assert to_extended_path(already_extended_unc) == already_extended_unc


@windows_only
def test_to_extended_path_makes_relative_paths_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert to_extended_path("docs") == "\\\\?\\" + str(tmp_path / "docs")


@windows_only
def test_to_extended_path_normalizes_forward_slashes():
    assert to_extended_path("F:/foo/bar") == "\\\\?\\F:\\foo\\bar"


def test_from_extended_path_strips_drive_prefix():
    assert from_extended_path("\\\\?\\C:\\foo\\bar") == "C:\\foo\\bar"


def test_from_extended_path_strips_unc_prefix():
    assert from_extended_path("\\\\?\\UNC\\server\\share\\foo") == "\\\\server\\share\\foo"


def test_from_extended_path_leaves_plain_path_unchanged():
    assert from_extended_path("C:\\foo\\bar") == "C:\\foo\\bar"


@windows_only
def test_round_trip_drive_path():
    original = "C:\\foo\\bar"
    assert from_extended_path(to_extended_path(original)) == original


@windows_only
def test_round_trip_unc_path():
    original = "\\\\server\\share\\foo"
    assert from_extended_path(to_extended_path(original)) == original


@pytest.mark.skipif(sys.platform == "win32", reason="only meaningful off Windows")
def test_to_extended_path_is_a_no_op_off_windows():
    assert to_extended_path("C:\\foo\\bar") == "C:\\foo\\bar"


def test_pure_path_splits_windows_paths_on_any_host():
    assert str(pure_path("C:\\Users\\me\\Docs\\a.txt").parent) == "C:\\Users\\me\\Docs"
    assert pure_path("\\\\server\\share\\dir\\b.txt").name == "b.txt"


def test_pure_path_splits_posix_paths_on_any_host():
    assert str(pure_path("/mnt/olddrive/home/me/c.txt").parent) == "/mnt/olddrive/home/me"

