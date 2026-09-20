import asyncio
import time

from hdd_analyzer.scan import CONSECUTIVE_SYSTEMIC_LIMIT, Pacer, SystemicErrorTracker, is_systemic_error


class _FakeAPIError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class _NoStatusError(Exception):
    pass


def test_is_systemic_error_true_for_401_402_403():
    for status in (401, 402, 403):
        assert is_systemic_error(_FakeAPIError(status, "nope"))


def test_is_systemic_error_false_for_other_status():
    for status in (400, 404, 422, 429, 500, 503):
        assert not is_systemic_error(_FakeAPIError(status, "nope"))


def test_is_systemic_error_false_without_status_attribute():
    assert not is_systemic_error(_NoStatusError("boom"))
    assert not is_systemic_error(ValueError("boom"))


def test_tracker_aborts_after_limit_consecutive_systemic_errors():
    tracker = SystemicErrorTracker(limit=5)
    err = _FakeAPIError(402, "Insufficient credits")
    aborted_flags = [tracker.record_error(err) for _ in range(5)]
    assert aborted_flags == [False, False, False, False, True]


def test_tracker_default_limit_matches_constant():
    tracker = SystemicErrorTracker()
    for _ in range(CONSECUTIVE_SYSTEMIC_LIMIT - 1):
        assert tracker.record_error(_FakeAPIError(402, "nope")) is False
    assert tracker.record_error(_FakeAPIError(402, "nope")) is True


def test_success_resets_consecutive_count():
    tracker = SystemicErrorTracker(limit=3)
    err = _FakeAPIError(401, "bad key")
    assert tracker.record_error(err) is False
    assert tracker.record_error(err) is False
    tracker.record_success()
    assert tracker.record_error(err) is False
    assert tracker.record_error(err) is False
    assert tracker.record_error(err) is True


def test_per_file_errors_never_trip_the_breaker():
    tracker = SystemicErrorTracker(limit=2)
    err = _FakeAPIError(500, "server hiccup")
    assert tracker.record_error(err) is False
    assert tracker.record_error(err) is False
    assert tracker.record_error(err) is False


def test_abort_reason_reports_first_error_and_status():
    tracker = SystemicErrorTracker(limit=1)
    err = _FakeAPIError(402, "Insufficient credits")
    tracker.record_error(err)
    reason = tracker.abort_reason()
    assert "402" in reason
    assert "Insufficient credits" in reason
    assert "1 consecutive" in reason


def test_canary_abort_reason_does_not_claim_a_consecutive_streak():
    tracker = SystemicErrorTracker(limit=5)
    reason = tracker.canary_abort_reason(_FakeAPIError(402, "Insufficient credits"))
    assert "402" in reason
    assert "Insufficient credits" in reason
    assert "preflight canary" in reason
    assert "consecutive" not in reason


def test_abort_reason_keeps_first_message_across_the_streak():
    tracker = SystemicErrorTracker(limit=3)
    tracker.record_error(_FakeAPIError(402, "first message"))
    tracker.record_error(_FakeAPIError(402, "second message"))
    tracker.record_error(_FakeAPIError(402, "third message"))
    reason = tracker.abort_reason()
    assert "first message" in reason
    assert "second message" not in reason


def test_pacer_spaces_starts_at_the_configured_rate():
    async def run():
        pacer = Pacer(rpm=1200)  # one start per 50 ms
        started = time.monotonic()
        await asyncio.gather(*(pacer.wait() for _ in range(5)))
        return time.monotonic() - started

    assert asyncio.run(run()) >= 0.2


def test_pacer_disabled_when_rpm_is_none():
    async def run():
        pacer = Pacer(rpm=None)
        started = time.monotonic()
        await asyncio.gather(*(pacer.wait() for _ in range(50)))
        return time.monotonic() - started

    assert asyncio.run(run()) < 0.05
