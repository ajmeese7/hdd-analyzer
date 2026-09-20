"""Windows extended-length path helpers.

Windows caps normal paths at 260 characters. The `\\\\?\\` extended-length
prefix lifts that limit and is required for os.stat/os.scandir to succeed on
long paths reached through the WSL UNC redirector (\\\\wsl.localhost\\...).
"""

from __future__ import annotations

import re
import sys
from pathlib import PurePath, PurePosixPath, PureWindowsPath

_WINDOWS_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def pure_path(path_str: str) -> PurePath:
    """Parse a recorded path with the flavor it was written in, on any host.

    Inventory and result files carry the walked drive's native paths, and a
    report generated on Linux from a Windows walk must still split on
    backslashes.
    """
    return PureWindowsPath(path_str) if _WINDOWS_PATH.match(path_str) else PurePosixPath(path_str)


_DRIVE_PREFIX = "\\\\?\\"
_UNC_PREFIX = "\\\\?\\UNC\\"


def to_extended_path(path_str: str) -> str:
    """Convert `path_str` to its Windows extended-length form.

    Returns `path_str` unchanged on non-Windows platforms, and unchanged if
    it is already in extended form. Forward slashes are normalized to
    backslashes before conversion, since callers may accept `F:/` style
    input.
    """
    if sys.platform != "win32":
        return path_str

    normalized = path_str.replace("/", "\\")

    if normalized.startswith(_DRIVE_PREFIX):
        return normalized

    if normalized.startswith("\\\\"):
        return _UNC_PREFIX + normalized.lstrip("\\")

    return _DRIVE_PREFIX + normalized


def from_extended_path(path_str: str) -> str:
    """Strip a Windows extended-length prefix from `path_str`, if present.

    Inverse of `to_extended_path`. Returns `path_str` unchanged if it carries
    no extended prefix.
    """
    if path_str.startswith(_UNC_PREFIX):
        return "\\\\" + path_str[len(_UNC_PREFIX):]
    if path_str.startswith(_DRIVE_PREFIX):
        return path_str[len(_DRIVE_PREFIX):]
    return path_str
