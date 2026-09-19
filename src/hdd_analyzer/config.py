"""Shared constants and pure classification logic (skip rules, categorization)."""

from __future__ import annotations

PRICE_PER_MTOK = 0.042
DEFAULT_CAP_USD = 5.0
DEFAULT_CONCURRENCY = 8
EXCERPT_CHAR_CAP = 6000
EXTRACT_READ_BYTES = 16 * 1024
DEDUPE_SAMPLE_BYTES = 256 * 1024
SIZE_FLOOR_BYTES = 32

# Directory names skipped regardless of platform (case-insensitive match on
# the final path component).
_COMMON_SKIP_DIRS = {
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "$recycle.bin",
    "system volume information",
    "$winreagent",
    "recovery",
    "perflogs",
    "intel",
    "xboxgames",
    "node_modules",
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    ".cache",
}

# Directory names skipped only when they look like Local/Temp style caches
# under an AppData tree. Matched by full lowercase relative suffix later;
# here we just track the leaf names that trigger the "cache-like" heuristic.
_APPDATA_CACHE_LEAVES = {"temp", "cache", "code cache", "gpucache", "temporary internet files"}

# Linux rootfs system directories to skip. `etc`, `opt`, and `srv` are kept
# per the design doc (small, may hold hand-edited configs / user data).
_LINUX_SKIP_DIRS = {
    "proc",
    "sys",
    "dev",
    "run",
    "usr",
    "lib",
    "lib64",
    "bin",
    "sbin",
    "boot",
    "snap",
    "lost+found",
    "cdrom",
    "media",
    "mnt",
}


def is_skip_dir(name: str, parent_parts: tuple[str, ...]) -> bool:
    """Return True if a directory entry named `name` should be pruned.

    `parent_parts` are the lowercased path components above `name`, used to
    detect AppData\\Local\\Temp-like cache directories and the `var/mail`
    carve-out.
    """
    lowered = name.lower()
    if lowered in _COMMON_SKIP_DIRS or lowered in _LINUX_SKIP_DIRS:
        return True

    if lowered == "var":
        # Linux: skip var/ entirely except var/mail, which we can't know
        # about yet at this point (mail is a child, not this dir itself), so
        # we keep walking into var and let the child-level check exclude
        # siblings of mail. Handled by caller via is_var_child_skip.
        return False

    if lowered in _APPDATA_CACHE_LEAVES and "appdata" in parent_parts:
        return True
    if lowered.startswith("temp") and "local" in parent_parts and "appdata" in parent_parts:
        return True

    return False


def is_var_child_skip(name: str, parent_parts: tuple[str, ...]) -> bool:
    """Skip children of a top-level Linux `var` dir except `var/mail`."""
    if not parent_parts or parent_parts[-1] != "var":
        return False
    return name.lower() != "mail"


_EXTENSION_CATEGORIES: dict[str, str] = {}


def _register(category: str, extensions: tuple[str, ...]) -> None:
    for ext in extensions:
        _EXTENSION_CATEGORIES[ext] = category


_register(
    "text",
    ("txt", "md", "csv", "log", "json", "xml", "yaml", "yml", "ini", "cfg", "conf", "eml", "htm", "html"),
)
_register(
    "code",
    ("py", "js", "ts", "c", "cpp", "h", "java", "rs", "go", "sh", "ps1", "bat", "sql", "rb", "php", "pl"),
)
_register("doc", ("docx", "doc", "rtf", "odt", "xlsx", "pdf"))
_register("image", ("jpg", "jpeg", "png", "heic", "gif", "raw", "cr2", "tiff", "tif"))
_register("archive", ("zip", "7z", "rar", "tar", "gz"))
_register("av", ("mp3", "mp4", "mov", "avi", "mkv", "wav"))


def categorize(extension: str) -> str:
    """Map a file extension (no leading dot, any case) to a category."""
    return _EXTENSION_CATEGORIES.get(extension.lower(), "binary")
