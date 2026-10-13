"""
Liveness integration tests (Step 9).

NO WEBCAM, NO MODEL AND NO DATASET ARE NEEDED. Every session runs against its
OWN temporary SQLite file; the real data/classvision.db is never opened.

WHAT THIS VERIFIES
    That recognition and liveness stay independent, that a spoofed track can
    never become attendance-eligible, that an uncertain one never quietly
    becomes live, that an unknown person stays unknown whatever their
    liveness, that a liveness failure fails safe, and that the Step 6 rule
    engine is untouched by all of it.

WHAT THIS DOES NOT VERIFY
    Anti-spoofing accuracy. The sequences are synthetic. No real presentation
    attack has been shown to this system, and no accuracy, FAR, FRR, APCER,
    BPCER or ACER is claimed.

Run with:
    python tests/test_liveness_integration.py
"""

import json
import shutil
import sys
import tempfile
import time
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

warnings.filterwarnings("ignore")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "ai"))
sys.path.insert(0, str(PROJECT_ROOT / "database"))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

import backend  # noqa: F401,E402

from fastapi.testclient import TestClient  # noqa: E402

from attendance_evidence import (  # noqa: E402
    BLOCKED_SPOOF,
    BLOCKED_UNCERTAIN,
    ELIGIBLE,
    NOT_CHECKED,
    eligibility_for_state,
    evaluate_evidence,
)
from attendance_rules import PresenceSegment  # noqa: E402
from attendance_session import AttendanceSession  # noqa: E402
from face_recognizer import STATUS_MATCH, RecognitionResult  # noqa: E402
from live_pipeline import LivePipeline  # noqa: E402
from liveness_detector import (  # noqa: E402
    LIVENESS_LIVE,
    LIVENESS_MODE_ENFORCE,
    LIVENESS_MODE_OFF,
    LIVENESS_MODE_REPORT,
    LIVENESS_SPOOF,
    LIVENESS_UNCERTAIN,
    LIVENESS_UNKNOWN,
)
from liveness_test import synthetic_sequence  # noqa: E402
from repositories import (  # noqa: E402
    AttendanceRepository,
    SessionRepository,
    StudentRepository,
)
from schema_baseline import EXPECTED_TABLES  # noqa: E402

from backend.services import live_service, state  # noqa: E402
from paths import enable_safe_console  # noqa: E402

START = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=5)
END = START + timedelta(hours=1)

SESSION_ID = "SES-LIVENESS"
CLASS_ID = "CLASS-LIVENESS"
STUDENTS = {"STU001": "Student One", "STU002": "Student Two"}

# Anything that would be biometric data, media, a path, a secret, or a raw cue
# value presented as if it meant something to a reader.
FORBIDDEN_KEYS = (
    "embedding", "vector", "descriptor", "feature", "face", "image", "photo",
    "crop", "video", "thumbnail", "similarity", "score", "password", "secret",
    "token", "credential", "api_key", "db_path", "filepath", "file_path",
    "model_path", "onnx", "motion_cue", "texture_cue",
)


# -- harness ---------------------------------------------------------------

class SequenceDetector:
    """Replays a synthetic sequence, one detection per frame."""

    def __init__(self, kinds, frames=90):
        # kinds: {track slot -> sequence kind}. Each slot keeps its own x
        # offset so the tracker sees distinct people.
        self.sequences = []
        for slot, kind in enumerate(kinds):
            self.sequences.append(list(synthetic_sequence(
                kind, frames, box=(60 + slot * 260, 170, 120, 120),
                seed=11 + slot * 5)))
        self.calls = 0

    def detect(self, frame):
        index = min(self.calls, len(self.sequences[0]) - 1)
        self.calls += 1
        return [sequence[index][1] for sequence in self.sequences]

    def frame_for(self, index):
        return self.sequences[0][min(index, len(self.sequences[0]) - 1)][0]


class SlotRecognizer:
    """Names a face by which slot it is in. No model, no embedding."""

    def __init__(self, by_slot):
        self.by_slot = by_slot
        self.calls = 0

    def recognize(self, frame, detection):
        self.calls += 1
        slot = 0 if detection.x < 200 else 1
        match = self.by_slot.get(slot)

        if match is None:
            return RecognitionResult(status="UNKNOWN", similarity=0.2,
                                     box=detection.as_tuple())

        return RecognitionResult(identity=match[0], name=match[1],
                                 similarity=0.83, status=STATUS_MATCH,
                                 box=detection.as_tuple())


def build_pipeline(kinds, by_slot, mode=LIVENESS_MODE_REPORT, frames=90,
                   names=None):
    """A real LivePipeline driven by synthetic sequences."""
    detector = SequenceDetector(kinds, frames)
    recognizer = SlotRecognizer(by_slot)

    pipeline = LivePipeline(detector, recognizer, session_start=START,
                            session_end=END,
                            student_names=names or dict(STUDENTS),
                            liveness_mode=mode)
    return pipeline, detector, recognizer


def drive(pipeline, detector, frames=90):
    for index in range(frames):
        pipeline.process(detector.frame_for(index))
    return pipeline


def seed(database):
    students = StudentRepository(database)
    for student_id, name in STUDENTS.items():
        students.upsert(student_id, name)

    session = AttendanceSession(SESSION_ID, CLASS_ID, START, END,
                                policy_version="1.0",
                                expected_students=list(STUDENTS))
    SessionRepository(database).save(session)
    return session


def make_client(kinds, by_slot, mode=LIVENESS_MODE_REPORT, frames=90):
    """A fresh app and manager, wired to a real pipeline over synthetic input."""
    directory = Path(tempfile.mkdtemp(prefix="classvision_pad_"))
    state.configure(directory / "pad.db")
    seed(state.get_database())

    holder = {"pipeline": None, "detector": None, "camera": None}

    class SequenceCamera:
        def __init__(self):
            self.index = -1
            self.released = 0

        def read(self):
            self.index += 1
            time.sleep(0.001)
            detector = holder["detector"]
            return detector.frame_for(self.index) if detector else None

        def release(self):
            self.released += 1

    def camera_factory(config):
        holder["camera"] = SequenceCamera()
        return holder["camera"]

    def pipeline_factory(session_start, session_end, student_names):
        pipeline, detector, _ = build_pipeline(kinds, by_slot, mode, frames,
                                               names=student_names)
        holder["pipeline"] = pipeline
        holder["detector"] = detector
        return pipeline

    manager = live_service.LiveSessionManager(camera_factory=camera_factory,
                                              pipeline_factory=pipeline_factory)
    live_service.reset_live_manager(manager)

    from backend.app import app
    return TestClient(app), directory, holder


def teardown(client, directory):
    live_service.reset_live_manager(None)
    client.close()
    state.close_database()
    shutil.rmtree(directory, ignore_errors=True)


def wait_for(holder, frames, timeout=30.0):
    """
    Wait for the worker to reach a frame count.

    The timeout is generous on purpose. These suites run alongside twenty
    others, and a worker thread that is merely slow to be scheduled is not a
    failure of anything Step 9 claims - treating it as one would make the
    suite flaky rather than strict.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        pipeline = holder.get("pipeline")
        if pipeline is not None and pipeline.frame_index + 1 >= frames:
            return True
        time.sleep(0.01)
    return False


def reached(holder):
    pipeline = holder.get("pipeline")
    return (pipeline.frame_index + 1) if pipeline else 0


def walk(value, visit, trail="$"):
    if isinstance(value, dict):
        for key, item in value.items():
            visit(key, "%s.%s" % (trail, key))
            walk(item, visit, "%s.%s" % (trail, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            walk(item, visit, "%s[%d]" % (trail, index))


def segment(student_id, track_id, minutes=10):
    return PresenceSegment(student_id, START + timedelta(minutes=1),
                           START + timedelta(minutes=1 + minutes), track_id)


# -- recognition and liveness are independent ------------------------------

def test_a_recognised_student_is_not_automatically_live():
    """
    The rule that the whole step rests on.

    A photograph of a student is recognised as that student - that is what
    recognition is for. It must not follow that they are present.
    """
    pipeline, detector, recognizer = build_pipeline(
        ["spoof"], {0: ("STU001", "Student One")})
    drive(pipeline, detector)

    rows = pipeline.tracks_view()
    assert len(rows) == 1, "expected one track, got %d" % len(rows)

    row = rows[0]
    assert row["identity_state"] == "CONFIRMED", (
        "recognition should still have named the face")
    assert row["student_id"] == "STU001"
    assert row["liveness_state"] == LIVENESS_SPOOF, (
        "a presented still image was reported %s" % row["liveness_state"])
    assert row["attendance_eligibility"] == BLOCKED_SPOOF

    print("PASS  a recognised face on a presented image is CONFIRMED + SPOOF "
          "+ %s" % row["attendance_eligibility"])


def test_a_live_face_is_not_automatically_anybody():
    """The other direction: liveness names nobody."""
    pipeline, detector, _ = build_pipeline(["live"], {})   # nobody matches
    drive(pipeline, detector)

    rows = pipeline.tracks_view()
    row = rows[0]

    assert row["liveness_state"] == LIVENESS_LIVE
    assert row["identity_state"] == "UNKNOWN"
    assert row["student_id"] is None, (
        "a live but unrecognised person was named %r" % row["student_id"])
    assert pipeline.presence_view() == [], (
        "an unrecognised person produced presence")

    print("PASS  a confirmed-live face with no identity stays UNKNOWN and "
          "produces nothing")


def test_two_people_get_independent_verdicts_in_one_frame():
    pipeline, detector, _ = build_pipeline(
        ["live", "spoof"],
        {0: ("STU001", "Student One"), 1: ("STU002", "Student Two")})
    drive(pipeline, detector)

    rows = {row["student_id"]: row for row in pipeline.tracks_view()}

    assert rows["STU001"]["liveness_state"] == LIVENESS_LIVE
    assert rows["STU002"]["liveness_state"] == LIVENESS_SPOOF
    assert rows["STU001"]["attendance_eligibility"] == ELIGIBLE
    assert rows["STU002"]["attendance_eligibility"] == BLOCKED_SPOOF

    print("PASS  in one frame, one student is LIVE/%s and the other SPOOF/%s"
          % (rows["STU001"]["attendance_eligibility"],
             rows["STU002"]["attendance_eligibility"]))


def test_liveness_never_changes_an_identity():
    pipeline, detector, _ = build_pipeline(
        ["spoof"], {0: ("STU001", "Student One")})
    drive(pipeline, detector)

    identity = pipeline.identities.get(1)
    assert identity.is_confirmed
    assert identity.student_id == "STU001", (
        "a spoof verdict altered the identity to %r" % identity.student_id)

    print("PASS  a SPOOF verdict leaves the confirmed identity untouched")


# -- the eligibility layer -------------------------------------------------

def test_eligibility_requires_confirmed_live():
    cases = [
        (LIVENESS_LIVE, True, ELIGIBLE),
        (LIVENESS_LIVE, False, BLOCKED_UNCERTAIN),
        (LIVENESS_SPOOF, True, BLOCKED_SPOOF),
        (LIVENESS_SPOOF, False, BLOCKED_SPOOF),
        (LIVENESS_UNCERTAIN, False, BLOCKED_UNCERTAIN),
        (LIVENESS_UNKNOWN, False, BLOCKED_UNCERTAIN),
        (None, False, BLOCKED_UNCERTAIN),
    ]

    for liveness_state, confirmed, expected in cases:
        got = eligibility_for_state(liveness_state, confirmed,
                                    LIVENESS_MODE_ENFORCE)
        assert got == expected, (
            "%s (confirmed=%s) gave %s, expected %s"
            % (liveness_state, confirmed, got, expected))

    assert eligibility_for_state(LIVENESS_SPOOF, True, LIVENESS_MODE_OFF) == \
        NOT_CHECKED, "a switched-off layer should report NOT_CHECKED"

    print("PASS  %d eligibility cases - only CONFIRMED LIVE is eligible"
          % len(cases))


def test_uncertain_never_silently_becomes_live():
    report = evaluate_evidence(
        [segment("STU001", 1)],
        {1: (LIVENESS_UNCERTAIN, False)},
        mode=LIVENESS_MODE_ENFORCE)

    assert report.eligible_segments == []
    assert len(report.blocked_segments) == 1
    assert report.counts[BLOCKED_UNCERTAIN] == 1
    print("PASS  uncertain evidence is held back, never promoted to eligible")


def test_report_mode_changes_nothing_but_says_what_it_would_have_done():
    segments = [segment("STU001", 1), segment("STU002", 2)]
    states = {1: (LIVENESS_LIVE, True), 2: (LIVENESS_SPOOF, True)}

    reported = evaluate_evidence(segments, states, mode=LIVENESS_MODE_REPORT)
    enforced = evaluate_evidence(segments, states, mode=LIVENESS_MODE_ENFORCE)

    assert len(reported.segments_for_rules(segments)) == 2, (
        "report mode filtered the evidence")
    assert len(enforced.segments_for_rules(segments)) == 1, (
        "enforce mode did not filter the evidence")
    assert len(reported.blocked_segments) == 1, (
        "report mode should still say what it would have blocked")

    print("PASS  report mode blocks nothing and reports 1 segment it would "
          "have held; enforce mode holds it")


def test_blocked_evidence_is_never_edited():
    original = segment("STU001", 1, minutes=10)
    report = evaluate_evidence([original], {1: (LIVENESS_SPOOF, True)},
                               mode=LIVENESS_MODE_ENFORCE)

    held = report.blocked_segments[0]
    assert held is original, "the blocked segment was replaced"
    assert held.duration_seconds == 600.0, (
        "the blocked segment was shortened to %.0fs" % held.duration_seconds)

    print("PASS  held-back evidence is held whole, never trimmed or edited")


def test_a_students_two_tracks_are_judged_separately():
    """
    Liveness belongs to a track, not to a person.

    A student seen genuinely and then presented as a photograph must keep the
    genuine evidence and lose only the presented part.
    """
    segments = [segment("STU001", 1, minutes=10),
                segment("STU001", 2, minutes=10)]

    report = evaluate_evidence(
        segments, {1: (LIVENESS_LIVE, True), 2: (LIVENESS_SPOOF, True)},
        mode=LIVENESS_MODE_ENFORCE)

    assert len(report.eligible_segments) == 1
    assert report.eligible_segments[0].track_id == 1
    assert report.by_student["STU001"]["eligible_segments"] == 1
    assert report.by_student["STU001"]["blocked_segments"] == 1

    print("PASS  one student, two tracks - the genuine one counts and the "
          "presented one does not")


# -- through the API -------------------------------------------------------

def test_the_first_status_already_reports_the_liveness_mode():
    """
    A status that is wrong about a safety control is worse than none.

    The worker republishes liveness every 15 frames, so the mode has to be
    published before the worker starts - otherwise the first second of polling
    would say liveness was off while it was running.
    """
    client, directory, holder = make_client(
        ["live"], {0: ("STU001", "Student One")})
    try:
        started = client.post("/api/live/start",
                              json={"session_id": SESSION_ID}).json()

        assert started["liveness_mode"] == LIVENESS_MODE_REPORT, (
            "the start response reported liveness as %r"
            % started["liveness_mode"])
        assert started["baseline_version"], (
            "the start response named no baseline version")
        assert started["live_tracks"] == 0, (
            "liveness counted %d live tracks before a frame was processed"
            % started["live_tracks"])

        client.post("/api/live/stop")
        print("PASS  the very first status already reports mode %r and "
              "baseline %s" % (started["liveness_mode"],
                               started["baseline_version"]))
    finally:
        teardown(client, directory)


def test_a_live_session_reports_liveness_through_the_api():
    client, directory, holder = make_client(
        ["live"], {0: ("STU001", "Student One")})
    try:
        client.post("/api/live/start", json={"session_id": SESSION_ID})
        assert wait_for(holder, 60), (
            "the worker reached only %d frames" % reached(holder))

        status = client.get("/api/live/status").json()
        assert status["liveness_mode"] == LIVENESS_MODE_REPORT
        assert status["liveness_available"] is True
        assert status["baseline_version"]
        assert status["live_tracks"] >= 1, (
            "no track was confirmed live: %s" % status)

        presence = client.get("/api/live/presence").json()
        student = presence["students"][0]
        assert student["liveness_state"] == LIVENESS_LIVE
        assert student["liveness_confirmed"] is True
        assert student["attendance_eligibility"] == ELIGIBLE

        track = presence["tracks"][0]
        assert track["identity_state"] == "CONFIRMED"
        assert track["liveness_state"] == LIVENESS_LIVE

        client.post("/api/live/stop")
        print("PASS  the live API reports identity and liveness as separate "
              "columns")
    finally:
        teardown(client, directory)


def test_a_spoofed_student_is_blocked_at_finalisation_under_enforcement():
    """
    The protection that matters, end to end.

    A photograph of an enrolled student, recognised as that student, must not
    become attendance when enforcement is on.
    """
    client, directory, holder = make_client(
        ["spoof"], {0: ("STU001", "Student One")}, mode=LIVENESS_MODE_ENFORCE)
    try:
        client.post("/api/live/start", json={"session_id": SESSION_ID})
        assert wait_for(holder, 60), (
            "the worker reached only %d frames" % reached(holder))

        body = client.post("/api/live/stop").json()

        liveness = body["liveness"]
        assert liveness["enforced"] is True
        assert liveness["blocked_segments"] >= 1, (
            "the spoofed evidence was not held back: %s" % liveness)
        assert "STU001" in liveness["blocked_students"]

        attendance = AttendanceRepository(state.get_database())
        record = attendance.get(SESSION_ID, "STU001")

        # No record at all, rather than a confident one. "The system cannot
        # say" is the honest outcome, and it is NOT the same claim as ABSENT.
        assert record is None, (
            "a student whose evidence was withheld was still given a %s "
            "record" % (record or {}).get("status"))
        assert "STU001" in liveness["withheld_students"]

        # A student who genuinely never appeared is still evaluated normally -
        # the withholding is narrow, not a blanket refusal to decide.
        other = attendance.get(SESSION_ID, "STU002")
        assert other is not None and other["status"] == "ABSENT", (
            "an ordinary absence stopped being evaluated")

        # The evidence itself is still stored, so a teacher can review it.
        segments = attendance.get_segments(SESSION_ID)
        assert segments, (
            "the held-back evidence was deleted, leaving nothing to review")

        print("PASS  a spoofed student gets NO record (never ABSENT), a truly "
              "absent student still gets ABSENT, %d segment(s) held but stored"
              % liveness["blocked_segments"])
    finally:
        teardown(client, directory)


def test_uncertain_liveness_never_marks_a_student_absent():
    """
    The constraint that forced a design change.

    Under enforcement, a student whose liveness is merely UNDECIDED must not
    fall through to ABSENT. "We could not verify this" and "they were not
    here" are different claims, and only one of them is supported.
    """
    segments = [segment("STU001", 1)]
    report = evaluate_evidence(segments, {1: (LIVENESS_UNCERTAIN, False)},
                               mode=LIVENESS_MODE_ENFORCE)

    assert report.withheld_students == ["STU001"]
    assert report.students_for_rules(list(STUDENTS)) == ["STU002"], (
        "the uncertain student was still handed to the rule engine")

    # A student with SOME trusted evidence is judged on it, not withheld.
    mixed = evaluate_evidence(
        [segment("STU001", 1), segment("STU001", 2)],
        {1: (LIVENESS_LIVE, True), 2: (LIVENESS_UNCERTAIN, False)},
        mode=LIVENESS_MODE_ENFORCE)
    assert mixed.withheld_students == [], (
        "a student with trustworthy evidence was withheld anyway")
    assert sorted(mixed.students_for_rules(list(STUDENTS))) == sorted(STUDENTS)

    # And in report mode nothing is withheld at all.
    reported = evaluate_evidence(segments, {1: (LIVENESS_UNCERTAIN, False)},
                                 mode=LIVENESS_MODE_REPORT)
    assert reported.withheld_students == []

    print("PASS  uncertain liveness withholds a verdict; it never produces "
          "one")


def test_report_mode_leaves_step_8_attendance_exactly_as_it_was():
    """
    The default must not change anybody's attendance.

    This baseline has never been evaluated against a real attack, so the
    default mode reports and does not act.
    """
    client, directory, holder = make_client(
        ["spoof"], {0: ("STU001", "Student One")}, mode=LIVENESS_MODE_REPORT)
    try:
        client.post("/api/live/start", json={"session_id": SESSION_ID})
        assert wait_for(holder, 60), (
            "the worker reached only %d frames" % reached(holder))

        body = client.post("/api/live/stop").json()

        assert body["liveness"]["enforced"] is False
        assert body["liveness"]["blocked_segments"] >= 1, (
            "report mode should still say what it would have blocked")
        assert "would have held" in body["note"]

        record = AttendanceRepository(state.get_database()).get(
            SESSION_ID, "STU001")
        assert record["present_seconds"] > 0, (
            "report mode changed the attendance it was supposed to leave "
            "alone")

        print("PASS  report mode credits %.0fs as Step 8 did, and reports %d "
              "segment(s) it would have held"
              % (record["present_seconds"],
                 body["liveness"]["blocked_segments"]))
    finally:
        teardown(client, directory)


def test_an_unknown_person_stays_unknown_whatever_their_liveness():
    for kind in ("live", "spoof"):
        client, directory, holder = make_client(
            [kind], {}, mode=LIVENESS_MODE_ENFORCE)
        try:
            client.post("/api/live/start", json={"session_id": SESSION_ID})
            assert wait_for(holder, 60), (
            "the worker reached only %d frames" % reached(holder))

            presence = client.get("/api/live/presence").json()
            assert presence["students"] == [], (
                "an unrecognised %s person appeared as a student" % kind)

            track = presence["tracks"][0]
            assert track["identity_state"] == "UNKNOWN"
            assert track["student_id"] is None

            body = client.post("/api/live/stop").json()
            assert body["segments_saved"] == 0

            records = AttendanceRepository(
                state.get_database()).list_for_session(SESSION_ID)
            assert {row["student_id"] for row in records} == set(STUDENTS)
            assert all(row["present_seconds"] == 0 for row in records), (
                "an unrecognised person credited presence to somebody")
        finally:
            teardown(client, directory)

    print("PASS  an unknown person stays UNKNOWN whether LIVE or SPOOF, and "
          "produces no attendance either way")


def test_a_liveness_failure_fails_safe_and_keeps_the_session_running():
    """
    'Liveness unavailable' must never mean 'everybody is live'.

    The detector is replaced with one that raises on every call - the harshest
    failure available - and the session must keep running while every track
    reports UNCERTAIN.
    """
    client, directory, holder = make_client(
        ["live"], {0: ("STU001", "Student One")}, mode=LIVENESS_MODE_ENFORCE)
    try:
        client.post("/api/live/start", json={"session_id": SESSION_ID})
        assert wait_for(holder, 20), (
            "the worker reached only %d frames" % reached(holder))

        class BrokenDetector:
            def observe(self, *args, **kwargs):
                raise RuntimeError("the liveness model fell over")

            def assess(self, *args, **kwargs):
                raise RuntimeError("the liveness model fell over")

            def forget(self, *args, **kwargs):
                pass

        holder["pipeline"].liveness = BrokenDetector()
        assert wait_for(holder, 120), (
            "the worker reached only %d frames" % reached(holder))

        status = client.get("/api/live/status").json()
        assert status["state"] == "RUNNING", (
            "a liveness failure stopped the session (%s)" % status["state"])
        assert status["liveness_available"] is False
        assert status["live_tracks"] == 0, (
            "a broken liveness layer reported %d live tracks"
            % status["live_tracks"])

        presence = client.get("/api/live/presence").json()
        for track in presence["tracks"]:
            assert track["liveness_state"] != LIVENESS_LIVE, (
                "a broken liveness layer reported a track LIVE")
            assert track["attendance_eligibility"] != ELIGIBLE

        body = client.post("/api/live/stop").json()
        assert body["finalized"] is True, (
            "the session could not be finalised after a liveness failure")

        print("PASS  a liveness layer that raises every call keeps the session "
              "RUNNING and reports 0 live tracks")
    finally:
        teardown(client, directory)


def test_a_failure_after_confirmation_withdraws_the_confirmed_state():
    """
    Found by the Step 9 audit, and the reason this test exists.

    The stability layer deliberately refuses to tear down a confirmed state on
    one bad observation - correct for a student who briefly turned away. It is
    WRONG when the liveness layer itself has died: a checker that cannot run
    cannot vouch for anybody, including somebody it vouched for a second
    earlier.

    Before the fix, a track confirmed LIVE and then orphaned by a broken
    detector kept reporting LIVE and ELIGIBLE, which is precisely the
    "liveness unavailable must not mean everybody is live" failure.
    """
    pipeline, detector, _ = build_pipeline(
        ["live"], {0: ("STU001", "Student One")}, mode=LIVENESS_MODE_ENFORCE,
        frames=200)

    # 1. Get a genuinely confirmed LIVE verdict first.
    for index in range(40):
        pipeline.process(detector.frame_for(index))

    before = pipeline.tracks_view()[0]
    assert before["liveness_state"] == LIVENESS_LIVE, (
        "the probe never confirmed LIVE, so it cannot test withdrawal")
    assert before["attendance_eligibility"] == ELIGIBLE

    # 2. Now break the liveness layer under it.
    class BrokenDetector:
        def observe(self, *args, **kwargs):
            raise RuntimeError("the liveness model fell over")

        def assess(self, *args, **kwargs):
            raise RuntimeError("the liveness model fell over")

        def forget(self, *args, **kwargs):
            pass

    pipeline.liveness = BrokenDetector()
    for index in range(40, 120):
        pipeline.process(detector.frame_for(index))

    after = pipeline.tracks_view()[0]
    summary = pipeline.liveness_summary()

    assert after["liveness_state"] == LIVENESS_UNCERTAIN, (
        "a confirmed LIVE verdict survived the death of the checker (%s)"
        % after["liveness_state"])
    assert after["liveness_confirmed"] is False
    assert after["attendance_eligibility"] != ELIGIBLE
    assert summary["live"] == 0, (
        "a dead checker still reported %d live track(s)" % summary["live"])
    assert summary["liveness_available"] is False

    # 3. And the finalisation view - the one that decides attendance - agrees.
    states = pipeline.liveness_states()
    assert states, "the probe lost its tracks entirely"
    assert all(state == LIVENESS_UNCERTAIN and not confirmed
               for state, confirmed in states.values()), (
        "finalisation would still have counted a pre-failure verdict: %s"
        % states)

    report = evaluate_evidence(pipeline.segments(session_end=END), states,
                               mode=LIVENESS_MODE_ENFORCE)
    assert report.eligible_segments == [], (
        "%d segment(s) reached the rule engine on a dead checker's word"
        % len(report.eligible_segments))

    print("PASS  a confirmed LIVE verdict is withdrawn when the checker dies - "
          "0 live tracks, 0 eligible segments")


def test_the_failure_latch_clears_only_on_a_real_successful_check():
    """
    A frame on which no track was due a check proves nothing.

    Liveness samples every 3rd frame, so two frames in three run the check
    pass without measuring anything. Treating those as recovery would clear
    the failure almost immediately and hand back the LIVE verdict the test
    above exists to withdraw.
    """
    pipeline, detector, _ = build_pipeline(
        ["live"], {0: ("STU001", "Student One")}, frames=200)

    for index in range(40):
        pipeline.process(detector.frame_for(index))

    class BrokenDetector:
        def observe(self, *args, **kwargs):
            raise RuntimeError("down")

        def assess(self, *args, **kwargs):
            raise RuntimeError("down")

        def forget(self, *args, **kwargs):
            pass

    pipeline.liveness = BrokenDetector()

    # The layer cannot know it is broken until a check is actually attempted,
    # and checks happen every 3rd frame - so allow one sampling cycle for the
    # failure to be discovered.
    discovered = None
    for index in range(40, 46):
        pipeline.process(detector.frame_for(index))
        if pipeline.liveness_summary()["liveness_available"] is False:
            discovered = index
            break

    assert discovered is not None, (
        "the broken detector was never noticed within one sampling cycle")

    # From then on it must STAY latched, including on the two frames in three
    # where no check runs. Those frames prove nothing and must not be mistaken
    # for recovery.
    for index in range(discovered + 1, 100):
        pipeline.process(detector.frame_for(index))
        assert pipeline.liveness_summary()["liveness_available"] is False, (
            "the failure latch cleared at frame %d, where no check ran" % index)

    # A working detector must clear it again - a transient failure should not
    # poison the rest of the lesson.
    from liveness_detector import LivenessDetector
    pipeline.liveness = LivenessDetector()
    for index in range(100, 160):
        pipeline.process(detector.frame_for(index))

    assert pipeline.liveness_summary()["liveness_available"] is True, (
        "the layer recovered but never said so")

    print("PASS  the failure is noticed within one sampling cycle, holds for "
          "%d frames, and clears on a real successful check"
          % (99 - discovered))


def test_liveness_errors_leak_no_path_or_internals():
    client, directory, holder = make_client(
        ["live"], {0: ("STU001", "Student One")})
    try:
        client.post("/api/live/start", json={"session_id": SESSION_ID})
        assert wait_for(holder, 20), (
            "the worker reached only %d frames" % reached(holder))

        class LeakyDetector:
            def observe(self, *args, **kwargs):
                raise RuntimeError(
                    "failed to load C:\\\\models\\\\secret_pad_model.onnx "
                    "with token abc123")

            def assess(self, *args, **kwargs):
                raise RuntimeError("same")

            def forget(self, *args, **kwargs):
                pass

        holder["pipeline"].liveness = LeakyDetector()
        assert wait_for(holder, 100), (
            "the worker reached only %d frames" % reached(holder))

        bodies = [client.get("/api/live/status").json(),
                  client.get("/api/live/presence").json()]
        bodies.append(client.post("/api/live/stop").json())

        text = json.dumps(bodies).lower()
        for leak in ("secret_pad_model", ".onnx", "c:\\\\", "token abc123",
                     "traceback", "/users/"):
            assert leak not in text, "a liveness error leaked %r" % leak

        assert "runtimeerror" in text, (
            "the failure should still be reported by type")

        print("PASS  a liveness error naming a model path and a token leaks "
              "neither - only the exception type")
    finally:
        teardown(client, directory)


def test_the_liveness_api_carries_no_biometric_media_or_cue_value():
    client, directory, holder = make_client(
        ["live", "spoof"],
        {0: ("STU001", "Student One"), 1: ("STU002", "Student Two")})
    try:
        client.post("/api/live/start", json={"session_id": SESSION_ID})
        assert wait_for(holder, 60), (
            "the worker reached only %d frames" % reached(holder))

        bodies = {
            "/api/live/status": client.get("/api/live/status").json(),
            "/api/live/presence": client.get("/api/live/presence").json(),
        }
        bodies["/api/live/stop"] = client.post("/api/live/stop").json()

        offenders = []
        for url, body in bodies.items():
            def inspect(key, trail, url=url):
                lowered = key.lower()
                if lowered in ("first_seen", "last_seen"):
                    return
                for banned in FORBIDDEN_KEYS:
                    if banned in lowered:
                        offenders.append("%s -> %s" % (url, trail))

            walk(body, inspect)

            text = json.dumps(body)
            for magic in ("data:image", "base64,", "\\x89PNG", "\\xff\\xd8"):
                assert magic not in text, "%s returned embedded media" % url

        assert not offenders, (
            "liveness responses exposed forbidden fields:\n  "
            + "\n  ".join(offenders))

        # And no raw cue value under any name: the detector's own numbers must
        # not have travelled.
        presence_text = json.dumps(bodies["/api/live/presence"])
        for cue in ("0.0639", "0.5009", "motion", "variation"):
            assert cue not in presence_text, (
                "a raw cue value or name reached the API: %r" % cue)

        print("PASS  %d liveness responses checked - no biometric, media, "
              "path, secret or cue value" % len(bodies))
    finally:
        teardown(client, directory)


def test_stopping_releases_the_liveness_runtime():
    client, directory, holder = make_client(
        ["live"], {0: ("STU001", "Student One")})
    try:
        client.post("/api/live/start", json={"session_id": SESSION_ID})
        assert wait_for(holder, 60), (
            "the worker reached only %d frames" % reached(holder))

        pipeline = holder["pipeline"]
        assert pipeline.liveness_manager.records or \
            pipeline.liveness_manager.retired

        client.post("/api/live/stop")

        assert holder["camera"].released >= 1, "the camera was not released"

        live_service.get_live_manager().reset()
        assert client.get("/api/live/status").json()["live_tracks"] == 0, (
            "liveness counts survived a reset")

        print("PASS  stopping releases the camera and resetting clears the "
              "liveness runtime")
    finally:
        teardown(client, directory)


# -- the attendance rules are untouched ------------------------------------

def test_the_attendance_rule_engine_is_not_modified_by_step_9():
    """
    A guard, not a formality.

    Step 9 must filter what the engine SEES, never what the engine DOES. If
    the engine ever learns the word "liveness", the layering has collapsed.
    """
    source = (PROJECT_ROOT / "ai" / "attendance_rules.py").read_text(
        encoding="utf-8")

    for word in ("liveness", "spoof", "LIVENESS", "SPOOF", "presentation "
                 "attack"):
        assert word not in source, (
            "ai/attendance_rules.py mentions %r - liveness has leaked into "
            "the rule engine" % word)

    session_source = (PROJECT_ROOT / "ai" / "attendance_session.py").read_text(
        encoding="utf-8")
    for word in ("liveness", "spoof"):
        assert word not in session_source.lower(), (
            "ai/attendance_session.py mentions %r" % word)

    print("PASS  the Step 6 rule engine and session model contain no liveness "
          "logic at all")


def test_liveness_adds_no_database_table_or_column():
    from database import SCHEMA

    lowered = SCHEMA.lower()
    for word in ("liveness", "spoof", "pad_", "presentation"):
        assert word not in lowered, (
            "the schema gained %r - Step 9 was supposed to stay in memory"
            % word)

    directory = Path(tempfile.mkdtemp(prefix="classvision_schema_"))
    try:
        state.configure(directory / "schema.db")
        database = state.get_database()
        tables = database.table_names()

        assert not any("liveness" in name.lower() for name in tables), (
            "a liveness table was created: %s" % tables)

        # Every table the project is allowed to have, listed by hand in
        # tests/schema_baseline.py. Liveness contributes none of them, which
        # is what this test exists to prove; exact equality means an
        # unexpected extra table fails here too.
        expected = EXPECTED_TABLES
        assert sorted(tables) == expected, (
            "the schema has %s, expected %s"
            % (sorted(tables), expected))

        print("PASS  no liveness table and no liveness column - %d tables, "
              "none of them from Step 9" % len(tables))
    finally:
        state.close_database()
        shutil.rmtree(directory, ignore_errors=True)


def test_switching_liveness_off_restores_step_8_exactly():
    pipeline, detector, _ = build_pipeline(
        ["spoof"], {0: ("STU001", "Student One")}, mode=LIVENESS_MODE_OFF)
    drive(pipeline, detector)

    assert pipeline.liveness is None
    assert pipeline.liveness_manager is None
    assert pipeline.liveness_states() == {}

    row = pipeline.tracks_view()[0]
    assert row["liveness_state"] == LIVENESS_UNKNOWN
    assert row["attendance_eligibility"] == NOT_CHECKED

    report = evaluate_evidence([segment("STU001", 1)], {},
                               mode=LIVENESS_MODE_OFF)
    assert len(report.eligible_segments) == 1, (
        "switching liveness off still blocked evidence")

    print("PASS  with liveness off nothing is measured and nothing is blocked")


def main():
    enable_safe_console()

    tests = [
        test_a_recognised_student_is_not_automatically_live,
        test_a_live_face_is_not_automatically_anybody,
        test_two_people_get_independent_verdicts_in_one_frame,
        test_liveness_never_changes_an_identity,
        test_eligibility_requires_confirmed_live,
        test_uncertain_never_silently_becomes_live,
        test_report_mode_changes_nothing_but_says_what_it_would_have_done,
        test_blocked_evidence_is_never_edited,
        test_a_students_two_tracks_are_judged_separately,
        test_the_first_status_already_reports_the_liveness_mode,
        test_a_live_session_reports_liveness_through_the_api,
        test_a_spoofed_student_is_blocked_at_finalisation_under_enforcement,
        test_uncertain_liveness_never_marks_a_student_absent,
        test_report_mode_leaves_step_8_attendance_exactly_as_it_was,
        test_an_unknown_person_stays_unknown_whatever_their_liveness,
        test_a_liveness_failure_fails_safe_and_keeps_the_session_running,
        test_a_failure_after_confirmation_withdraws_the_confirmed_state,
        test_the_failure_latch_clears_only_on_a_real_successful_check,
        test_liveness_errors_leak_no_path_or_internals,
        test_the_liveness_api_carries_no_biometric_media_or_cue_value,
        test_stopping_releases_the_liveness_runtime,
        test_the_attendance_rule_engine_is_not_modified_by_step_9,
        test_liveness_adds_no_database_table_or_column,
        test_switching_liveness_off_restores_step_8_exactly,
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
