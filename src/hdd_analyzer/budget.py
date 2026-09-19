"""Pure budget accounting: token estimation, spend tracking, cap checks."""

from __future__ import annotations

from dataclasses import dataclass

from hdd_analyzer.config import PRICE_PER_MTOK

CHARS_PER_TOKEN = 4
WORST_CASE_MULTIPLIER = 2.0


def estimate_tokens(char_count: int, worst_case: bool = False) -> int:
    """Estimate token count from character count (fallback heuristic).

    `worst_case=True` scales the average estimate by WORST_CASE_MULTIPLIER,
    for use in pre-dispatch cap checks where actual spend must not exceed
    the cap. The unbiased average estimate (the default) is used everywhere
    else, including the `estimate` CLI command.
    """
    tokens = max(1, char_count // CHARS_PER_TOKEN)
    if worst_case:
        tokens = max(1, int(tokens * WORST_CASE_MULTIPLIER))
    return tokens


def estimate_worst_case_tokens(char_count: int) -> int:
    """Worst-case token estimate for pre-dispatch cap checks."""
    return estimate_tokens(char_count, worst_case=True)


def tokens_to_cost(tokens: int) -> float:
    return tokens * PRICE_PER_MTOK / 1_000_000


@dataclass
class BudgetTracker:
    """Tracks exact-first, estimate-fallback spend against a hard cap."""

    cap_usd: float
    spent_usd: float = 0.0

    def record_tokens(self, input_tokens: int | None, fallback_char_count: int) -> float:
        """Record a completed call's cost, returning the cost added."""
        tokens = input_tokens if input_tokens is not None else estimate_tokens(fallback_char_count)
        cost = tokens_to_cost(tokens)
        self.spent_usd += cost
        return cost

    def would_exceed_cap(self, worst_case_batch_cost: float) -> bool:
        """True if spending worst_case_batch_cost on top of current spend breaks the cap."""
        return (self.spent_usd + worst_case_batch_cost) > self.cap_usd

    def remaining_usd(self) -> float:
        return max(0.0, self.cap_usd - self.spent_usd)
