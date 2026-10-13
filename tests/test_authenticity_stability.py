"""
Authenticity stability tests (Step 10).

NO WEBCAM, NO MODEL AND NO DATASET ARE NEEDED. The stability layer consumes
verdicts, not images, so these tests hand it verdicts directly.

WHAT THIS VERIFIES
    That a state is only confirmed on repeated agreement, that overturning a
    confirmed state costs more evidence than setting one, that oscillation
    never becomes a state, and that two tracks can never influence each other.

WHAT THIS DOES NOT VERIFY
    Deepfake detection accuracy. Nothing here looks at an image.

Run with:
    python tests/test_authenticity_stability.py
"""

import sys
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

warnings.filterwarnings("ignore")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "ai"))

from authenticity_detector import (  # noqa: E402
    AUTHENTICITY_AUTHENTIC,
    AUTHENTICITY_SUSPICIOUS,
    AUTHENTICITY_UNCERTAIN,
    AUTHENTICITY_UNKNOWN,
    AuthenticityVerdict,
)
from authenticity_stability import (  # noqa: E402
    DEFAULT_CHANGE_CONFIRMATIONS,
    DEFAULT_CONFIRMATIONS_REQUIRED,
    EVENT_AUTHENTICITY_CONFIRMED,
    EVENT_AUTHENTICITY_SUSPICIOUS,
    OUTCOME_CHANGE_REJECTED,
    OUTCOME_CONFIRMED,
    OUTCOME_PENDING,
    OUTCOME_UNCHANGED,
    AuthenticityManager,
)
from paths import enable_safe_console  # noqa: E402

START = datetime(2026, 3, 2, 9, 0, 0, tzinfo=timezone.utc)


def verdict(state, track_id=1):
    return AuthenticityVerdict(track_id, state, "probe", 60, 60)


def feed(manager, track_id, states, start_frame=0):
    outcome = None
    for index, state in enumerate(states):
        _, outcome = manager.update(track_id, verdict(state, track_id),
                                    start_frame + index * 3,
                                    START + timedelta(seconds=index))
    return outcome


class FakeTrack:
    def __init__(self, track_id, visible=True):
        self.track_id = track_id
        self.is_visible = visible


# -- confirmation ----------------------------------------------------------

def test_state_starts_unknown():
    manager = AuthenticityManager()
    assert manager.state_for(7) == AUTHENTICITY_UNKNOWN

    record = manager.get_or_create(7)
    assert record.effective_state == AUTHENTICITY_UNKNOWN
    assert record.confirmed is False
    assert record.observations == 0
    print("PASS  a fresh track is UNKNOWN, unconfirmed, with no observations")


def test_repeated_authentic_confirms_authentic():
    manager = AuthenticityManager()
    outcome = feed(manager, 1, [AUTHENTICITY_AUTHENTIC] * 4)

    record = manager.get(1)
    assert record.effective_state == AUTHENTICITY_AUTHENTIC
    assert record.confirmed is True
    assert outcome == OUTCOME_UNCHANGED
    print("PASS  AUTHENTIC x4 -> confirmed AUTHENTIC")


def test_repeated_suspicious_confirms_suspicious():
    manager = AuthenticityManager()
    feed(manager, 1, [AUTHENTICITY_SUSPICIOUS] * 3)

    record = manager.get(1)
    assert record.is_confirmed_suspicious
    assert manager.confirmed_suspicious_tracks() == [1]
    print("PASS  SUSPICIOUS x3 -> confirmed SUSPICIOUS")


def test_confirmation_needs_the_full_run():
    manager = AuthenticityManager()

    for index in range(DEFAULT_CONFIRMATIONS_REQUIRED - 1):
        _, outcome = manager.update(1, verdict(AUTHENTICITY_AUTHENTIC),
                                    index * 3, START)
        assert outcome == OUTCOME_PENDING
        assert manager.get(1).effective_state == AUTHENTICITY_UNCERTAIN

    _, outcome = manager.update(1, verdict(AUTHENTICITY_AUTHENTIC), 99, START)
    assert outcome == OUTCOME_CONFIRMED
    print("PASS  %d agreeing verdicts are needed; %d are not enough"
          % (DEFAULT_CONFIRMATIONS_REQUIRED,
             DEFAULT_CONFIRMATIONS_REQUIRED - 1))


def test_an_unconfirmed_track_reports_uncertain():
    manager = AuthenticityManager()
    manager.update(1, verdict(AUTHENTICITY_UNCERTAIN), 0, START)

    assert manager.get(1).effective_state == AUTHENTICITY_UNCERTAIN
    assert manager.get(1).confirmed is False
    print("PASS  a looked-at but undecided track is UNCERTAIN, never "
          "AUTHENTIC")


# -- mixed and oscillating evidence ----------------------------------------

def test_alternating_verdicts_never_confirm_anything():
    manager = AuthenticityManager()
    feed(manager, 1, [AUTHENTICITY_AUTHENTIC, AUTHENTICITY_SUSPICIOUS] * 6)

    record = manager.get(1)
    assert record.effective_state == AUTHENTICITY_UNCERTAIN, (
        "oscillation produced %s" % record.effective_state)
    assert record.confirmed is False
    assert record.oscillating is True
    print("PASS  12 alternating verdicts -> UNCERTAIN, flagged unstable")


def test_oscillation_after_confirmation_keeps_the_stable_state():
    manager = AuthenticityManager()
    feed(manager, 1, [AUTHENTICITY_AUTHENTIC] * 3)
    assert manager.get(1).is_confirmed_authentic

    states = []
    for index in range(8):
        state = (AUTHENTICITY_SUSPICIOUS if index % 2 == 0
                 else AUTHENTICITY_AUTHENTIC)
        manager.update(1, verdict(state), 100 + index * 3, START)
        states.append(manager.get(1).effective_state)

    assert set(states) == {AUTHENTICITY_AUTHENTIC}, (
        "the reported state flickered: %s" % states)
    print("PASS  flapping after confirmation never moves the reported state")


def test_overturning_a_confirmed_state_costs_more_evidence():
    manager = AuthenticityManager()
    feed(manager, 1, [AUTHENTICITY_AUTHENTIC] * 3)

    for index in range(DEFAULT_CHANGE_CONFIRMATIONS - 1):
        _, outcome = manager.update(1, verdict(AUTHENTICITY_SUSPICIOUS),
                                    100 + index * 3, START)
        assert outcome == OUTCOME_CHANGE_REJECTED
        assert manager.get(1).effective_state == AUTHENTICITY_AUTHENTIC

    manager.update(1, verdict(AUTHENTICITY_SUSPICIOUS), 200, START)
    assert manager.get(1).is_confirmed_suspicious
    print("PASS  %d verdicts set a state, %d are needed to overturn one"
          % (DEFAULT_CONFIRMATIONS_REQUIRED, DEFAULT_CHANGE_CONFIRMATIONS))


def test_a_confirmed_suspicious_is_not_cleared_by_a_glimpse():
    manager = AuthenticityManager()
    feed(manager, 1, [AUTHENTICITY_SUSPICIOUS] * 3)
    assert manager.get(1).is_confirmed_suspicious

    feed(manager, 1, [AUTHENTICITY_AUTHENTIC] * (DEFAULT_CHANGE_CONFIRMATIONS - 1),
         start_frame=100)

    assert manager.get(1).is_confirmed_suspicious, (
        "a confirmed SUSPICIOUS was cleared by %d authentic verdicts"
        % (DEFAULT_CHANGE_CONFIRMATIONS - 1))
    print("PASS  a confirmed SUSPICIOUS survives %d authentic verdicts"
          % (DEFAULT_CHANGE_CONFIRMATIONS - 1))


def test_uncertain_breaks_a_run_but_not_a_confirmed_state():
    manager = AuthenticityManager()

    feed(manager, 1, [AUTHENTICITY_AUTHENTIC, AUTHENTICITY_AUTHENTIC,
                      AUTHENTICITY_UNCERTAIN])
    assert not manager.get(1).confirmed

    feed(manager, 1, [AUTHENTICITY_AUTHENTIC] * 3, start_frame=100)
    assert manager.get(1).is_confirmed_authentic

    feed(manager, 1, [AUTHENTICITY_UNCERTAIN] * 5, start_frame=200)
    assert manager.get(1).is_confirmed_authentic, (
        "five uncertain observations cleared a confirmed AUTHENTIC")

    print("PASS  UNCERTAIN breaks a run but never tears down a confirmed "
          "state")


def test_uncertain_alone_never_becomes_authentic():
    manager = AuthenticityManager()
    feed(manager, 1, [AUTHENTICITY_UNCERTAIN] * 50)

    assert manager.get(1).effective_state == AUTHENTICITY_UNCERTAIN
    assert manager.get(1).confirmed is False
    print("PASS  50 uncertain observations are still UNCERTAIN")


# -- track isolation -------------------------------------------------------

def test_tracks_never_share_state():
    manager = AuthenticityManager()

    feed(manager, 1, [AUTHENTICITY_AUTHENTIC] * 4)
    feed(manager, 2, [AUTHENTICITY_SUSPICIOUS] * 4)
    feed(manager, 3, [AUTHENTICITY_UNCERTAIN] * 4)

    assert manager.get(1).effective_state == AUTHENTICITY_AUTHENTIC
    assert manager.get(2).effective_state == AUTHENTICITY_SUSPICIOUS
    assert manager.get(3).effective_state == AUTHENTICITY_UNCERTAIN

    summary = manager.summary()
    assert (summary["authentic"], summary["suspicious"],
            summary["uncertain"]) == (1, 1, 1)

    print("PASS  three tracks hold three different states at once")


def test_one_tracks_confirmation_cannot_confirm_another():
    manager = AuthenticityManager()

    for _ in range(10):
        manager.update(1, verdict(AUTHENTICITY_AUTHENTIC), 0, START)

    assert manager.get(1).is_confirmed_authentic
    assert manager.get(2) is None, "track 2 was created by track 1's evidence"

    manager.update(2, verdict(AUTHENTICITY_AUTHENTIC), 0, START)
    assert not manager.get(2).confirmed
    print("PASS  ten confirmations on one track confirm nothing on another")


def test_forgetting_releases_runtime_but_keeps_the_verdict():
    manager = AuthenticityManager()
    feed(manager, 1, [AUTHENTICITY_AUTHENTIC] * 4)

    manager.forget(1)

    assert manager.get(1) is None, "the runtime record was not released"
    assert manager.all_states()[1] == (AUTHENTICITY_AUTHENTIC, True), (
        "the confirmed verdict was lost when the track exited")
    print("PASS  a retired track releases its buffers and keeps its verdict")


def test_a_reacquired_track_starts_fresh():
    manager = AuthenticityManager()
    feed(manager, 1, [AUTHENTICITY_AUTHENTIC] * 4)
    manager.forget(1)

    manager.update(2, verdict(AUTHENTICITY_AUTHENTIC, 2), 0, START)
    assert not manager.get(2).confirmed, (
        "a new track inherited the previous track's confirmation")
    print("PASS  a returning person's new track proves authenticity again")


def test_pruning_retires_every_dropped_track():
    manager = AuthenticityManager()
    for track_id in (1, 2, 3):
        feed(manager, track_id, [AUTHENTICITY_AUTHENTIC] * 3)

    manager.prune([2])

    assert sorted(manager.records) == [2]
    assert sorted(manager.all_states()) == [1, 2, 3]
    print("PASS  pruning releases runtime state and keeps the verdicts")


# -- scheduling ------------------------------------------------------------

def test_only_visible_tracks_are_checked():
    manager = AuthenticityManager(sample_interval=3)

    assert manager.should_check(FakeTrack(1, visible=True), 0) is True
    assert manager.should_check(FakeTrack(2, visible=False), 0) is False
    print("PASS  a track that is not visible is never measured")


def test_checks_are_spaced_out():
    manager = AuthenticityManager(sample_interval=3)
    track = FakeTrack(1)

    assert manager.should_check(track, 0)
    manager.update(1, verdict(AUTHENTICITY_UNCERTAIN), 0, START)

    assert manager.should_check(track, 1) is False
    assert manager.should_check(track, 2) is False
    assert manager.should_check(track, 3) is True
    print("PASS  a track is measured every 3rd frame, not every frame")


# -- events and unavailability ---------------------------------------------

def test_events_are_transitions_not_a_stream():
    manager = AuthenticityManager()
    feed(manager, 1, [AUTHENTICITY_AUTHENTIC] * 30)

    types = [event.event_type for event in manager.events]
    assert types.count(EVENT_AUTHENTICITY_CONFIRMED) == 1
    assert len(manager.events) <= 3
    print("PASS  30 observations produced %d events, all transitions"
          % len(manager.events))


def test_a_suspicious_finding_is_announced_once():
    manager = AuthenticityManager()
    feed(manager, 1, [AUTHENTICITY_SUSPICIOUS] * 20)

    found = [e for e in manager.events
             if e.event_type == EVENT_AUTHENTICITY_SUSPICIOUS]
    assert len(found) == 1 and found[0].track_id == 1
    print("PASS  a confirmed suspicious finding is announced once")


def test_the_event_log_is_bounded():
    manager = AuthenticityManager(event_limit=10)
    for track_id in range(60):
        feed(manager, track_id, [AUTHENTICITY_AUTHENTIC] * 3)

    assert len(manager.events) == 10
    print("PASS  the event log is bounded at 10")


def test_records_and_events_carry_no_identity_or_cue():
    manager = AuthenticityManager()
    feed(manager, 1, [AUTHENTICITY_SUSPICIOUS] * 4)

    banned = ("embedding", "vector", "crop", "image", "student", "name",
              "coupling", "sharpness", "chroma", "luma")

    for exported in [manager.get(1).to_dict()] + manager.recent_events():
        for key in exported:
            for word in banned:
                assert word not in key.lower(), (
                    "an exported field is called %r" % key)
        for value in exported.values():
            assert isinstance(value, (str, int, bool, type(None))), (
                "an exported value is a %s" % type(value).__name__)

    print("PASS  records and events export scalars only")


def test_an_unavailable_check_is_uncertain_not_authentic():
    manager = AuthenticityManager()
    manager.note_unavailable(1, 0, "analysis unavailable", START)

    assert manager.get(1).effective_state == AUTHENTICITY_UNCERTAIN
    assert manager.get(1).confirmed is False
    print("PASS  'the check did not run' is UNCERTAIN - never AUTHENTIC")


def test_unavailability_breaks_a_run_in_progress():
    manager = AuthenticityManager()

    manager.update(1, verdict(AUTHENTICITY_AUTHENTIC), 0, START)
    manager.update(1, verdict(AUTHENTICITY_AUTHENTIC), 3, START)
    manager.note_unavailable(1, 6, "unavailable", START)
    manager.update(1, verdict(AUTHENTICITY_AUTHENTIC), 9, START)

    assert not manager.get(1).confirmed
    print("PASS  an unavailable check breaks a confirmation run")


def test_reset_clears_everything():
    manager = AuthenticityManager()
    feed(manager, 1, [AUTHENTICITY_AUTHENTIC] * 4)
    manager.forget(1)

    manager.reset()

    assert manager.records == {}
    assert manager.retired == {}
    assert manager.events == []
    assert manager.checks == 0
    print("PASS  reset clears records, retired verdicts, events and counters")


def main():
    enable_safe_console()

    tests = [
        test_state_starts_unknown,
        test_repeated_authentic_confirms_authentic,
        test_repeated_suspicious_confirms_suspicious,
        test_confirmation_needs_the_full_run,
        test_an_unconfirmed_track_reports_uncertain,
        test_alternating_verdicts_never_confirm_anything,
        test_oscillation_after_confirmation_keeps_the_stable_state,
        test_overturning_a_confirmed_state_costs_more_evidence,
        test_a_confirmed_suspicious_is_not_cleared_by_a_glimpse,
        test_uncertain_breaks_a_run_but_not_a_confirmed_state,
        test_uncertain_alone_never_becomes_authentic,
        test_tracks_never_share_state,
        test_one_tracks_confirmation_cannot_confirm_another,
        test_forgetting_releases_runtime_but_keeps_the_verdict,
        test_a_reacquired_track_starts_fresh,
        test_pruning_retires_every_dropped_track,
        test_only_visible_tracks_are_checked,
        test_checks_are_spaced_out,
        test_events_are_transitions_not_a_stream,
        test_a_suspicious_finding_is_announced_once,
        test_the_event_log_is_bounded,
        test_records_and_events_carry_no_identity_or_cue,
        test_an_unavailable_check_is_uncertain_not_authentic,
        test_unavailability_breaks_a_run_in_progress,
        test_reset_clears_everything,
    ]

    failures = 0
    for test in tests:
        try:
            test()
        except AssertionError as error:
            failures += 1
            print("FAIL  %s: %s" % (test.__name__, error))
        except Exception as error:
            failures += 1
            print("ERROR %s: %s: %s" % (test.__name__, type(error).__name__, error))

    print("\n%d/%d tests passed." % (len(tests) - failures, len(tests)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
