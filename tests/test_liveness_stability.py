"""
Liveness stability tests (Step 9).

NO WEBCAM, NO MODEL AND NO DATASET ARE NEEDED. The stability layer consumes
verdicts, not images, so these tests hand it verdicts directly.

WHAT THIS VERIFIES
    That a state is only confirmed on repeated agreement, that overturning a
    confirmed state costs more evidence than setting one, that oscillation
    never becomes a state, and that two tracks can never influence each other.

WHAT THIS DOES NOT VERIFY
    Anti-spoofing accuracy. Nothing here looks at an image.

Run with:
    python tests/test_liveness_stability.py
"""

import sys
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

warnings.filterwarnings("ignore")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "ai"))

from liveness_detector import (  # noqa: E402
    LIVENESS_LIVE,
    LIVENESS_SPOOF,
    LIVENESS_UNCERTAIN,
    LIVENESS_UNKNOWN,
    LivenessVerdict,
)
from liveness_stability import (  # noqa: E402
    DEFAULT_CHANGE_CONFIRMATIONS,
    DEFAULT_CONFIRMATIONS_REQUIRED,
    EVENT_LIVENESS_CONFIRMED,
    EVENT_LIVENESS_SPOOF_DETECTED,
    OUTCOME_CHANGE_REJECTED,
    OUTCOME_CONFIRMED,
    OUTCOME_PENDING,
    OUTCOME_UNCHANGED,
    LivenessManager,
)
from paths import enable_safe_console  # noqa: E402

START = datetime(2026, 3, 2, 9, 0, 0, tzinfo=timezone.utc)


def verdict(state, track_id=1, reason="synthetic"):
    return LivenessVerdict(track_id, state, reason, observations=12,
                           usable_observations=12)


def feed(manager, track_id, states, start_frame=0):
    """Push a sequence of verdicts at one track. Returns the last outcome."""
    outcome = None
    for index, state in enumerate(states):
        frame = start_frame + index * 3
        _, outcome = manager.update(track_id, verdict(state, track_id), frame,
                                    START + timedelta(seconds=index))
    return outcome


class FakeTrack:
    def __init__(self, track_id, visible=True):
        self.track_id = track_id
        self.is_visible = visible


# -- confirmation ----------------------------------------------------------

def test_repeated_live_confirms_live():
    manager = LivenessManager()
    outcome = feed(manager, 1, [LIVENESS_LIVE] * 4)

    record = manager.get(1)
    assert record.effective_state == LIVENESS_LIVE
    assert record.confirmed is True
    assert outcome == OUTCOME_UNCHANGED, (
        "the fourth agreeing verdict should be 'more of the same', got %s"
        % outcome)
    print("PASS  LIVE x4 -> confirmed LIVE")


def test_repeated_spoof_confirms_spoof():
    manager = LivenessManager()
    feed(manager, 1, [LIVENESS_SPOOF] * 3)

    record = manager.get(1)
    assert record.effective_state == LIVENESS_SPOOF
    assert record.confirmed is True
    assert record.is_confirmed_spoof
    print("PASS  SPOOF x3 -> confirmed SPOOF")


def test_confirmation_needs_the_full_run():
    manager = LivenessManager()

    for index in range(DEFAULT_CONFIRMATIONS_REQUIRED - 1):
        _, outcome = manager.update(1, verdict(LIVENESS_LIVE), index * 3, START)
        assert outcome == OUTCOME_PENDING, (
            "verdict %d already returned %s" % (index + 1, outcome))
        assert manager.get(1).effective_state == LIVENESS_UNCERTAIN, (
            "a state was reported before it was confirmed")

    _, outcome = manager.update(1, verdict(LIVENESS_LIVE), 99, START)
    assert outcome == OUTCOME_CONFIRMED

    print("PASS  %d agreeing verdicts are needed; %d are not enough"
          % (DEFAULT_CONFIRMATIONS_REQUIRED, DEFAULT_CONFIRMATIONS_REQUIRED - 1))


def test_an_unconfirmed_track_is_uncertain_never_unknown_forever():
    manager = LivenessManager()
    manager.update(1, verdict(LIVENESS_UNCERTAIN), 0, START)

    record = manager.get(1)
    assert record.effective_state == LIVENESS_UNCERTAIN
    assert record.confirmed is False
    print("PASS  a looked-at but undecided track is UNCERTAIN, not LIVE")


def test_a_never_checked_track_is_unknown():
    manager = LivenessManager()
    assert manager.state_for(404) == LIVENESS_UNKNOWN
    print("PASS  a track nobody has checked is UNKNOWN - distinct from "
          "'checked and undecided'")


# -- oscillation -----------------------------------------------------------

def test_alternating_verdicts_never_confirm_anything():
    """
    LIVE, SPOOF, LIVE, SPOOF is not weak evidence of either.

    It is evidence that something is wrong, and the honest report is
    UNCERTAIN plus a flag saying the track is unstable.
    """
    manager = LivenessManager()
    feed(manager, 1, [LIVENESS_LIVE, LIVENESS_SPOOF] * 6)

    record = manager.get(1)
    assert record.effective_state == LIVENESS_UNCERTAIN, (
        "oscillation produced %s" % record.effective_state)
    assert record.confirmed is False
    assert record.oscillating is True, "the flapping was not recorded"

    print("PASS  12 alternating verdicts -> UNCERTAIN, flagged unstable, "
          "never confirmed")


def test_oscillation_after_a_confirmed_state_keeps_the_stable_state():
    """
    Eligibility must not flicker.

    Once LIVE is confirmed, flapping does not tear it down - the state only
    moves on sustained, repeated disagreement. That is what stops a student's
    eligibility changing several times a second.
    """
    manager = LivenessManager()
    feed(manager, 1, [LIVENESS_LIVE] * 3)
    assert manager.get(1).is_confirmed_live

    states = []
    for index in range(8):
        state = LIVENESS_SPOOF if index % 2 == 0 else LIVENESS_LIVE
        manager.update(1, verdict(state), 100 + index * 3, START)
        states.append(manager.get(1).effective_state)

    assert set(states) == {LIVENESS_LIVE}, (
        "the reported state flickered: %s" % states)
    print("PASS  flapping after confirmation never moves the reported state")


# -- changing an established state -----------------------------------------

def test_overturning_a_confirmed_state_costs_more_evidence():
    manager = LivenessManager()
    feed(manager, 1, [LIVENESS_LIVE] * 3)
    assert manager.get(1).is_confirmed_live

    for index in range(DEFAULT_CHANGE_CONFIRMATIONS - 1):
        _, outcome = manager.update(1, verdict(LIVENESS_SPOOF),
                                    100 + index * 3, START)
        assert outcome == OUTCOME_CHANGE_REJECTED, (
            "spoof verdict %d already changed the state (%s)"
            % (index + 1, outcome))
        assert manager.get(1).effective_state == LIVENESS_LIVE

    manager.update(1, verdict(LIVENESS_SPOOF), 200, START)
    assert manager.get(1).is_confirmed_spoof

    print("PASS  %d agreeing verdicts set a state, %d are needed to overturn "
          "one" % (DEFAULT_CONFIRMATIONS_REQUIRED, DEFAULT_CHANGE_CONFIRMATIONS))


def test_a_confirmed_spoof_is_not_cleared_by_a_glimpse_of_a_real_face():
    """
    The attack this asymmetry exists to resist.

    Hold up a photograph until it is flagged, then show a real face for a
    moment. If two good verdicts cleared the flag, the control would be
    worthless.
    """
    manager = LivenessManager()
    feed(manager, 1, [LIVENESS_SPOOF] * 3)
    assert manager.get(1).is_confirmed_spoof

    feed(manager, 1, [LIVENESS_LIVE] * (DEFAULT_CHANGE_CONFIRMATIONS - 1),
         start_frame=100)

    assert manager.get(1).is_confirmed_spoof, (
        "a confirmed spoof was cleared by %d live verdicts"
        % (DEFAULT_CHANGE_CONFIRMATIONS - 1))
    print("PASS  a confirmed SPOOF survives %d live verdicts"
          % (DEFAULT_CHANGE_CONFIRMATIONS - 1))


def test_an_uncertain_verdict_breaks_a_run_but_not_a_confirmed_state():
    manager = LivenessManager()

    # Two LIVE, then UNCERTAIN: the run is broken, nothing is confirmed.
    feed(manager, 1, [LIVENESS_LIVE, LIVENESS_LIVE, LIVENESS_UNCERTAIN])
    assert not manager.get(1).confirmed, (
        "an interrupted run still confirmed a state")

    # Confirm properly, then an UNCERTAIN observation must not undo it. A
    # student who turned away has not become a photograph.
    feed(manager, 1, [LIVENESS_LIVE] * 3, start_frame=100)
    assert manager.get(1).is_confirmed_live

    feed(manager, 1, [LIVENESS_UNCERTAIN] * 5, start_frame=200)
    assert manager.get(1).is_confirmed_live, (
        "five uncertain observations cleared a confirmed LIVE")

    print("PASS  UNCERTAIN breaks a run but never tears down a confirmed state")


def test_uncertain_alone_never_becomes_live():
    manager = LivenessManager()
    feed(manager, 1, [LIVENESS_UNCERTAIN] * 40)

    record = manager.get(1)
    assert record.effective_state == LIVENESS_UNCERTAIN
    assert record.confirmed is False
    print("PASS  40 uncertain observations are still UNCERTAIN, never LIVE")


# -- track independence ----------------------------------------------------

def test_tracks_never_share_state():
    """
    The most dangerous bug this layer could have.

    If two tracks shared a buffer, one person moving could confirm the person
    beside them as live.
    """
    manager = LivenessManager()

    feed(manager, 1, [LIVENESS_LIVE] * 4)
    feed(manager, 2, [LIVENESS_SPOOF] * 4)
    feed(manager, 3, [LIVENESS_UNCERTAIN] * 4)

    assert manager.get(1).effective_state == LIVENESS_LIVE
    assert manager.get(2).effective_state == LIVENESS_SPOOF
    assert manager.get(3).effective_state == LIVENESS_UNCERTAIN

    assert manager.confirmed_live_tracks() == [1]
    assert manager.confirmed_spoof_tracks() == [2]

    summary = manager.summary()
    assert (summary["live"], summary["spoof"], summary["uncertain"]) == (1, 1, 1)

    print("PASS  three tracks hold three different states at once")


def test_one_tracks_confirmation_cannot_confirm_another():
    manager = LivenessManager()

    for _ in range(10):
        manager.update(1, verdict(LIVENESS_LIVE), 0, START)

    assert manager.get(1).is_confirmed_live
    assert manager.get(2) is None, "track 2 was created by track 1's evidence"

    manager.update(2, verdict(LIVENESS_LIVE), 0, START)
    assert not manager.get(2).confirmed, (
        "track 2 was confirmed by one verdict because track 1 had ten")

    print("PASS  ten confirmations on one track confirm nothing on another")


def test_forgetting_a_track_releases_runtime_but_keeps_the_verdict():
    """
    The verdict has to outlive the track.

    Finalisation runs after every track has exited. If forgetting a track
    erased what it concluded, evidence that WAS confirmed live would be held
    back at the end of the lesson.
    """
    manager = LivenessManager()
    feed(manager, 1, [LIVENESS_LIVE] * 4)

    manager.forget(1)

    assert manager.get(1) is None, "the runtime record was not released"
    assert manager.all_states()[1] == (LIVENESS_LIVE, True), (
        "the confirmed verdict was lost when the track exited")

    print("PASS  a retired track releases its buffers and keeps its verdict")


def test_pruning_retires_every_track_the_tracker_dropped():
    manager = LivenessManager()
    for track_id in (1, 2, 3):
        feed(manager, track_id, [LIVENESS_LIVE] * 3)

    manager.prune([2])

    assert sorted(manager.records) == [2]
    assert sorted(manager.all_states()) == [1, 2, 3], (
        "pruning lost the verdicts of the tracks it retired")
    print("PASS  pruning releases runtime state for 2 tracks and keeps 3 "
          "verdicts")


def test_a_reacquired_track_starts_fresh():
    """
    Track ids are not identities - Step 5 is explicit about that.

    A student who leaves and returns gets a NEW track id, and that new track
    must prove its own liveness rather than inheriting the old one's.
    """
    manager = LivenessManager()
    feed(manager, 1, [LIVENESS_LIVE] * 4)
    manager.forget(1)

    manager.update(2, verdict(LIVENESS_LIVE, 2), 0, START)
    assert not manager.get(2).confirmed, (
        "a new track inherited the previous track's confirmation")

    print("PASS  a returning person's new track proves liveness again")


# -- scheduling ------------------------------------------------------------

def test_only_visible_tracks_are_checked():
    manager = LivenessManager(sample_interval=3)

    assert manager.should_check(FakeTrack(1, visible=True), 0) is True
    assert manager.should_check(FakeTrack(2, visible=False), 0) is False, (
        "a track with no fresh face to look at was scheduled for a check")

    print("PASS  a track that is not visible is never measured")


def test_checks_are_spaced_out():
    manager = LivenessManager(sample_interval=3)
    track = FakeTrack(1)

    assert manager.should_check(track, 0)
    manager.update(1, verdict(LIVENESS_UNCERTAIN), 0, START)

    assert manager.should_check(track, 1) is False
    assert manager.should_check(track, 2) is False
    assert manager.should_check(track, 3) is True

    print("PASS  a track is measured every 3rd frame, not every frame")


# -- events ----------------------------------------------------------------

def test_events_are_transitions_not_a_stream():
    manager = LivenessManager()
    feed(manager, 1, [LIVENESS_LIVE] * 30)

    types = [event.event_type for event in manager.events]
    assert types.count(EVENT_LIVENESS_CONFIRMED) == 1, (
        "confirmation was announced %d times"
        % types.count(EVENT_LIVENESS_CONFIRMED))
    assert len(manager.events) <= 3, (
        "30 observations produced %d events" % len(manager.events))

    print("PASS  30 observations produced %d events, all transitions"
          % len(manager.events))


def test_a_spoof_is_announced_once():
    manager = LivenessManager()
    feed(manager, 1, [LIVENESS_SPOOF] * 20)

    spoof_events = [event for event in manager.events
                    if event.event_type == EVENT_LIVENESS_SPOOF_DETECTED]
    assert len(spoof_events) == 1
    assert spoof_events[0].track_id == 1
    print("PASS  a confirmed spoof is announced once, with its track id")


def test_the_event_log_is_bounded():
    manager = LivenessManager(event_limit=10)

    for track_id in range(60):
        feed(manager, track_id, [LIVENESS_LIVE] * 3)

    assert len(manager.events) == 10, (
        "the event log grew to %d" % len(manager.events))
    print("PASS  the event log is bounded at 10, not %d" % (60 * 2))


def test_events_and_records_carry_no_identity_or_image():
    manager = LivenessManager()
    feed(manager, 1, [LIVENESS_SPOOF] * 4)

    banned = ("embedding", "vector", "crop", "image", "frame_data", "student",
              "name", "similarity", "motion", "detail")

    for exported in [manager.get(1).to_dict()] + manager.recent_events():
        for key in exported:
            for word in banned:
                assert word not in key.lower() or key == "frame_index", (
                    "an exported field is called %r" % key)
        for value in exported.values():
            assert isinstance(value, (str, int, bool, type(None))), (
                "an exported value is a %s" % type(value).__name__)

    print("PASS  records and events export scalars only - no identity, no "
          "image, no cue value")


# -- unavailability --------------------------------------------------------

def test_an_unavailable_check_is_uncertain_not_live():
    manager = LivenessManager()
    manager.note_unavailable(1, 0, "the liveness check is unavailable", START)

    record = manager.get(1)
    assert record.effective_state == LIVENESS_UNCERTAIN
    assert record.confirmed is False
    print("PASS  'the check did not run' is UNCERTAIN - never LIVE")


def test_unavailability_breaks_a_run_in_progress():
    manager = LivenessManager()

    manager.update(1, verdict(LIVENESS_LIVE), 0, START)
    manager.update(1, verdict(LIVENESS_LIVE), 3, START)
    manager.note_unavailable(1, 6, "unavailable", START)
    manager.update(1, verdict(LIVENESS_LIVE), 9, START)

    assert not manager.get(1).confirmed, (
        "a run interrupted by an unavailable check still confirmed LIVE")
    print("PASS  an unavailable check breaks a confirmation run")


def test_reset_clears_everything():
    manager = LivenessManager()
    feed(manager, 1, [LIVENESS_LIVE] * 4)
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
        test_repeated_live_confirms_live,
        test_repeated_spoof_confirms_spoof,
        test_confirmation_needs_the_full_run,
        test_an_unconfirmed_track_is_uncertain_never_unknown_forever,
        test_a_never_checked_track_is_unknown,
        test_alternating_verdicts_never_confirm_anything,
        test_oscillation_after_a_confirmed_state_keeps_the_stable_state,
        test_overturning_a_confirmed_state_costs_more_evidence,
        test_a_confirmed_spoof_is_not_cleared_by_a_glimpse_of_a_real_face,
        test_an_uncertain_verdict_breaks_a_run_but_not_a_confirmed_state,
        test_uncertain_alone_never_becomes_live,
        test_tracks_never_share_state,
        test_one_tracks_confirmation_cannot_confirm_another,
        test_forgetting_a_track_releases_runtime_but_keeps_the_verdict,
        test_pruning_retires_every_track_the_tracker_dropped,
        test_a_reacquired_track_starts_fresh,
        test_only_visible_tracks_are_checked,
        test_checks_are_spaced_out,
        test_events_are_transitions_not_a_stream,
        test_a_spoof_is_announced_once,
        test_the_event_log_is_bounded,
        test_events_and_records_carry_no_identity_or_image,
        test_an_unavailable_check_is_uncertain_not_live,
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
