from hdd_analyzer.budget import (
    WORST_CASE_MULTIPLIER,
    BudgetTracker,
    estimate_tokens,
    estimate_worst_case_tokens,
    tokens_to_cost,
)


def test_estimate_tokens_uses_chars_per_four():
    assert estimate_tokens(400) == 100


def test_estimate_tokens_minimum_is_one():
    assert estimate_tokens(0) == 1


def test_tokens_to_cost_matches_price_per_mtok():
    assert tokens_to_cost(1_000_000) == 0.042


def test_budget_tracker_records_exact_tokens_when_available():
    tracker = BudgetTracker(cap_usd=1.0)
    cost = tracker.record_tokens(input_tokens=1_000_000, fallback_char_count=999_999)
    assert cost == 0.042
    assert tracker.spent_usd == 0.042


def test_budget_tracker_falls_back_to_char_estimate():
    tracker = BudgetTracker(cap_usd=1.0)
    tracker.record_tokens(input_tokens=None, fallback_char_count=400)
    assert tracker.spent_usd == tokens_to_cost(100)


def test_would_exceed_cap_true_when_over_budget():
    tracker = BudgetTracker(cap_usd=0.01, spent_usd=0.009)
    assert tracker.would_exceed_cap(0.002)


def test_would_exceed_cap_false_when_within_budget():
    tracker = BudgetTracker(cap_usd=1.0, spent_usd=0.1)
    assert not tracker.would_exceed_cap(0.5)


def test_remaining_usd_never_negative():
    tracker = BudgetTracker(cap_usd=1.0, spent_usd=5.0)
    assert tracker.remaining_usd() == 0.0


def test_worst_case_tokens_scales_average_estimate():
    average = estimate_tokens(400)
    worst_case = estimate_worst_case_tokens(400)
    assert worst_case == int(average * WORST_CASE_MULTIPLIER)


def test_estimate_tokens_default_is_average_not_worst_case():
    assert estimate_tokens(400) != estimate_worst_case_tokens(400)


def test_worst_case_tokens_minimum_is_at_least_one():
    assert estimate_worst_case_tokens(0) >= 1
