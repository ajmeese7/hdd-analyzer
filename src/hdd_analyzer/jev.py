"""Jev (typesafe-sdk) question set and per-file classification call."""

from __future__ import annotations

from typing import Any

from typesafe_sdk import AsyncTypeSafeClient, JSONContent, Noul, Score, SystemOneResponse

NOULS: dict[str, Noul] = {
    "credentials": Noul(
        instructions="Does this file contain credentials (passwords, API keys, private keys, seed phrases, wallets, or tokens)?",
        criteria={
            "true": "Contains a password, API key, private key, seed phrase, wallet file, or auth token.",
            "false": "No credential material present.",
        },
    ),
    "personal": Noul(
        instructions="Is this personal (correspondence, journals, original writing, or sentimental content)?",
        criteria={
            "true": "Personal correspondence, a journal/diary entry, original personal writing, or sentimental content (photos of family, letters, etc).",
            "false": "Not personal; generic, work, or third-party content.",
        },
    ),
    "financial_legal": Noul(
        instructions="Is this financial or legal (tax, banking, contracts, insurance, medical, or identity documents)?",
        criteria={
            "true": "Tax records, bank statements, contracts, insurance documents, medical records, or identity documents.",
            "false": "No financial or legal content.",
        },
    ),
    "original_work": Noul(
        instructions="Is this authored source code or creative work, as opposed to downloaded, installed, or third-party content?",
        criteria={
            "true": "Appears to be code or creative work the drive owner authored themselves.",
            "false": "Downloaded, installed, generated, or third-party content (libraries, installers, vendor files).",
        },
    ),
    "irreplaceable": Noul(
        instructions="Is this genuinely unique, human-created or human-received content that could not be recreated?",
        criteria={
            "true": (
                "Genuinely unique content the owner created or received: original writing, personal photos, "
                "correspondence, or hand-authored work with no other copy or source."
            ),
            "false": (
                "Could be re-downloaded, reinstalled, or regenerated, including application-regenerable state such "
                "as game saves, application caches, or a mail client's local database file that merely indexes mail "
                "stored elsewhere."
            ),
        },
    ),
}

VALUE_SCORE: dict[str, Score] = {
    "value": Score(
        instructions="Rate the overall value of this file to the drive's owner.",
        criteria=[
            "Worthless system noise (cache, temp file, installer, log with no useful content).",
            "Routine (ordinary file, replaceable, low individual significance).",
            "Notable (meaningfully useful or interesting to the owner).",
            "High-value and irreplaceable (credentials, personal history, original work, or documents the owner would be devastated to lose).",
        ],
    ),
}

QUESTIONS: dict[str, Any] = {**NOULS, **VALUE_SCORE}

# Bumped whenever a question's instructions/criteria change meaning. Scores
# from different rubric versions are not comparable; each result row records
# the version it was scored under.
RUBRIC_VERSION = 2

NO_CONTENT_NOTE = (
    "file content could not be read; only the file name and metadata are available; "
    "treat the name as weak evidence"
)


def build_state(
    path: str,
    name: str,
    ext: str,
    size: int,
    modified: float,
    excerpt: str | None,
    metadata_only: bool,
) -> JSONContent:
    """Build the JSON-serializable state dict sent to Jev for one file.

    When `metadata_only` is true, Jev is explicitly warned that it is
    guessing from the file name alone: `content_readable` is set to false
    and `excerpt` is replaced with a note saying so, rather than silently
    sending `excerpt: null` and leaving Jev to infer that on its own.
    """
    return {
        "path": path,
        "name": name,
        "ext": ext,
        "size": size,
        "modified": modified,
        "excerpt": NO_CONTENT_NOTE if metadata_only else excerpt,
        "metadata_only": metadata_only,
        "content_readable": not metadata_only,
    }


async def classify_file(client: AsyncTypeSafeClient, state: JSONContent) -> SystemOneResponse:
    """Run the Jev system_one call for a single file's state."""
    return await client.system_one(state=state, questions=QUESTIONS)
