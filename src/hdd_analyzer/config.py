"""Shared constants and pure classification logic (skip rules, categorization)."""

from __future__ import annotations

PRICE_PER_MTOK = 0.042
# Measured from live billing: a metadata-only call bills ~830 tokens, so the
# question definitions and schema dominate the per-call cost.
PER_CALL_OVERHEAD_TOKENS = 850
DEFAULT_CAP_USD = 5.0
DEFAULT_CONCURRENCY = 8
EXCERPT_CHAR_CAP = 6000
EXTRACT_READ_BYTES = 16 * 1024
DEDUPE_SAMPLE_BYTES = 256 * 1024
SIZE_FLOOR_BYTES = 32
EXTRACT_TIMEOUT_SECONDS = 20
ARCHIVE_MEMBER_SIZE_CAP_BYTES = 64 * 1024 * 1024
MAX_HASH_BYTES = 50 * 1024 * 1024

# Categories eligible for content hashing (see should_hash_content). Every
# other category is deduped on (size, lowercased filename) only, since the
# real payoff of a content hash is dedicated to small text-ish files where
# a false-negative dedup is cheap and a full read is cheap too.
_HASH_ELIGIBLE_CATEGORIES = {"text", "code", "doc"}

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
    ".nuget",
    ".gradle",
    ".m2",
    ".cargo",
    ".rustup",
    ".npm",
    ".pnpm-store",
    ".yarn",
    "site-packages",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "cache",
    "caches",
    "cacheddata",
    "code cache",
    "gpucache",
    "shadercache",
    ".vs",
    ".idea",
    ".vscode-server",
    "temp",
    "tmp",
    ".trash-1000",
    ".thumbnails",
}

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
    detect the AppData\\Local and AppData\\LocalLow subtrees and the
    `var/mail` carve-out.
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

    if is_appdata_private_subtree(name, parent_parts):
        return True

    return False


def is_appdata_private_subtree(name: str, parent_parts: tuple[str, ...]) -> bool:
    """Skip AppData\\Local and AppData\\LocalLow entirely.

    These are machine-local install/cache trees. AppData\\Roaming is left
    walkable (real user configs and credentials live there, e.g. FileZilla,
    Thunderbird), though the unconditional cache-name skips still apply
    inside it.
    """
    lowered = name.lower()
    return lowered in ("local", "locallow") and bool(parent_parts) and parent_parts[-1] == "appdata"


def is_var_child_skip(name: str, parent_parts: tuple[str, ...]) -> bool:
    """Skip children of a top-level Linux `var` dir except `var/mail`."""
    if not parent_parts or parent_parts[-1] != "var":
        return False
    return name.lower() != "mail"


def _sibling_has_any(siblings: frozenset[str], *names: str) -> bool:
    return any(candidate in siblings for candidate in names)


def _sibling_has_ext(siblings: frozenset[str], suffix: str) -> bool:
    return any(sibling.endswith(suffix) for sibling in siblings)


def is_marker_skip(name: str, siblings: frozenset[str]) -> bool:
    """Return True if `name` should be pruned because of a sibling marker.

    `siblings` are the lowercased names of every entry in the same parent
    directory (files and dirs alike), as seen in a single scandir listing.
    Each rule targets a project-local build/dependency cache that is only
    junk in the presence of the marker file that identifies the project
    type; without the marker the same directory name could be real content.
    """
    lowered = name.lower()

    if lowered in ("library", "temp", "logs", "obj") and _sibling_has_any(siblings, "projectsettings", "assets"):
        return True
    if lowered == "target" and _sibling_has_any(siblings, "cargo.toml", "pom.xml"):
        return True
    if lowered in ("bin", "obj") and (_sibling_has_ext(siblings, ".csproj") or _sibling_has_ext(siblings, ".sln")):
        return True
    if lowered == "build" and _sibling_has_any(siblings, "gradlew", "cmakelists.txt", "package.json"):
        return True
    if lowered in ("dist", ".next", ".nuxt", "coverage") and _sibling_has_any(siblings, "package.json"):
        return True
    if lowered == "vendor" and _sibling_has_any(siblings, "composer.json", "go.mod"):
        return True

    return False


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


def should_hash_content(category: str, size: int) -> bool:
    """Decide whether a file is worth a content hash for dedupe.

    Content hashing costs an IO read of the first DEDUPE_SAMPLE_BYTES, which
    dominates runtime on spinning disks. It only pays for itself on the
    categories where duplicate detection is actually valuable (text, code,
    doc/pdf), and only up to MAX_HASH_BYTES; anything else, or an oversized
    file in one of those categories, falls back to a cheap metadata key.
    """
    return category in _HASH_ELIGIBLE_CATEGORIES and size <= MAX_HASH_BYTES
