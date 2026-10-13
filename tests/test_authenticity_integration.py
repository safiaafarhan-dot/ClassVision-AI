"""
Authenticity integration tests (Step 10).

NO WEBCAM, NO MODEL AND NO DATASET ARE NEEDED. Every session runs against its
OWN temporary SQLite file; the real data/classvision.db is never opened.

WHAT THIS VERIFIES
    That identity, liveness and authenticity stay three independent facts,
    that a suspicious track can never become attendance-eligible, that an
    unknown person stays unknown whatever their authenticity, that an
    authenticity failure fails safe, that the Step 6 rule engine and the
    six-table schema are untouched, and that no response can leak a frame, a
    crop, a cue value or a path.

WHAT THIS DOES NOT VERIFY
    Deepfake detection accuracy. The sequences are synthetic. No manipulated
    video has ever been shown to this system.

Run with:
    python tests/test_authenticity_integration.py
"""

import json
import re
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
    BLOCKED_SUSPICIOUS,
    BLOCKED_UNCERTAIN,
    BLOCKED_UNVERIFIED_SOURCE,
    ELIGIBLE,
    NOT_CHECKED,
    authenticity_eligibility,
    combine_eligibility,
    evaluate_evidence,
)
from attendance_rules import PresenceSegment  # noqa: E402
from attendance_session import AttendanceSession  # noqa: E402
from authenticity_detector import (  # noqa: E402
    AUTHENTICITY_AUTHENTIC,
    AUTHENTICITY_MODE_ENFORCE,
    AUTHENTICITY_MODE_OFF,
    AUTHENTICITY_MODE_REPORT,
    AUTHENTICITY_SUSPICIOUS,
    AUTHENTICITY_UNCERTAIN,
    AUTHENTICITY_UNKNOWN,
)
from authenticity_test import synthetic_sequence  # noqa: E402
from face_recognizer import STATUS_MATCH, RecognitionResult  # noqa: E402
from live_pipeline import LivePipeline  # noqa: E402
from liveness_detector import LIVENESS_LIVE, LIVENESS_SPOOF  # noqa: E402
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

SESSION_ID = "SES-AUTHENTICITY"
CLASS_ID = "CLASS-AUTH"
STUDENTS = {"STU001": "Student One", "STU002": "Student Two"}

# Enough frames to clear the 60-sample minimum at a 3-frame interval.
FRAMES = 260

FORBIDDEN_KEYS = (
    "embedding", "vector", "descriptor", "feature", "face", "image", "photo",
    "crop", "video", "thumbnail", "similarity", "score", "password", "secret",
    "token", "credential", "api_key", "db_path", "filepath", "file_path",
    "model_path", "onnx", "coupling", "sharpness", "chroma", "luma", "tensor",
)


# -- harness ---------------------------------------------------------------

class SequenceDetector:
    """Replays synthetic sequences, one detection per slot per frame."""

    def __init__(self, kinds, frames=FRAMES):
        self.sequences = []
        for slot, kind in enumerate(kinds):
            self.sequences.append(list(synthetic_sequence(
                kind, frames, box=(40 + slot * 300, 150, 130, 130),
                seed=17 + slot * 5)))
        self.calls = 0

    def detect(self, frame):
        index = min(self.calls, len(self.sequences[0]) - 1)
        self.calls += 1
        return [sequence[index][1] for sequence in self.sequences]

    def frame_for(self, index):
        """
        ONE composite frame carrying every slot's face.

        Each slot generates its own full frame; returning only the first
        would measure every track against the first slot's background, which
        is exactly the mistake the authenticity cue is designed to notice.
        """
        position = min(index, len(self.sequences[0]) - 1)
        frame = self.sequences[0][position][0].copy()

        for sequence in self.sequences[1:]:
            source, detection, _ = sequence[position]
            x, y = detection.x, detection.y
            width, height = detection.width, detection.height
            frame[y:y + height, x:x + width] = source[y:y + height,
                                                      x:x + width]

        return frame


class SlotRecognizer:
    def __init__(self, by_slot):
        self.by_slot = by_slot

    def recognize(self, frame, detection):
        slot = 0 if detection.x < 250 else 1
        match = self.by_slot.get(slot)
        if match is None:
            return RecognitionResult(status="UNKNOWN", similarity=0.2,
                                     box=detection.as_tuple())
        return RecognitionResult(identity=match[0], name=match[1],
                                 similarity=0.83, status=STATUS_MATCH,
                                 box=detection.as_tuple())


def build_pipeline(kinds, by_slot, authenticity_mode=AUTHENTICITY_MODE_ENFORCE,
                   frames=FRAMES, names=None, liveness_mode=None):
    detector = SequenceDetector(kinds, frames)
    recognizer = SlotRecognizer(by_slot)

    pipeline = LivePipeline(detector, recognizer, session_start=START,
                            session_end=END,
                            student_names=names or dict(STUDENTS),
                            liveness_mode=liveness_mode,
                            authenticity_mode=authenticity_mode)
    return pipeline, detector, recognizer


def drive(pipeline, detector, frames=FRAMES):
    for index in range(frames):
        pipeline.process(detector.frame_for(index))
    return pipeline


def seed(database):
    students = StudentRepository(database)
    for student_id, name in STUDENTS.items():
        students.upsert(student_id, name)

    SessionRepository(database).save(AttendanceSession(
        SESSION_ID, CLASS_ID, START, END, policy_version="1.0",
        expected_students=list(STUDENTS)))


def make_client(kinds, by_slot, authenticity_mode=AUTHENTICITY_MODE_REPORT,
                frames=FRAMES):
    directory = Path(tempfile.mkdtemp(prefix="classvision_auth_"))
    state.configure(directory / "auth.db")
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
        pipeline, detector, _ = build_pipeline(
            kinds, by_slot, authenticity_mode, frames, names=student_names)
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


def wait_for(holder, frames, timeout=45.0):
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


# -- three independent facts -----------------------------------------------

def test_identity_is_never_changed_by_authenticity():
    pipeline, detector, _ = build_pipeline(
        ["inverted"], {0: ("STU001", "Student One")})
    drive(pipeline, detector)

    row = pipeline.tracks_view()[0]
    assert row["authenticity_state"] == AUTHENTICITY_SUSPICIOUS, (
        "the probe never reached SUSPICIOUS (%s)" % row["authenticity_state"])
    assert row["identity_state"] == "CONFIRMED", (
        "a suspicious verdict changed the identity to %s"
        % row["identity_state"])
    assert row["student_id"] == "STU001"
    assert pipeline.identities.get(1).student_id == "STU001"

    print("PASS  a SUSPICIOUS track keeps its CONFIRMED identity untouched")


def test_authenticity_is_never_set_by_recognition():
    """A confidently recognised face is not automatically authentic."""
    pipeline, detector, _ = build_pipeline(
        ["decoupled"], {0: ("STU001", "Student One")})
    drive(pipeline, detector)

    row = pipeline.tracks_view()[0]
    assert row["identity_state"] == "CONFIRMED"
    assert row["authenticity_state"] != AUTHENTICITY_AUTHENTIC, (
        "recognition promoted the track to %s" % row["authenticity_state"])

    print("PASS  a CONFIRMED identity with %s authenticity - recognition "
          "vouches for nothing" % row["authenticity_state"])


def test_liveness_and_authenticity_are_independent():
    """
    The combination that proves they are different questions.

    A decoupled sequence deforms and carries detail, so liveness is happy with
    it; its face region does not behave like the rest of the image, so
    authenticity is not.
    """
    pipeline, detector, _ = build_pipeline(
        ["decoupled"], {0: ("STU001", "Student One")}, liveness_mode="report")
    drive(pipeline, detector)

    row = pipeline.tracks_view()[0]
    assert row["liveness_state"] == LIVENESS_LIVE, (
        "the probe needs a LIVE track to make its point, got %s"
        % row["liveness_state"])
    assert row["authenticity_state"] == AUTHENTICITY_UNCERTAIN

    print("PASS  one track is liveness=%s and authenticity=%s at the same "
          "time" % (row["liveness_state"], row["authenticity_state"]))


def test_two_tracks_hold_different_authenticity_in_one_frame():
    pipeline, detector, _ = build_pipeline(
        ["coupled", "inverted"],
        {0: ("STU001", "Student One"), 1: ("STU002", "Student Two")})
    drive(pipeline, detector)

    rows = {row["student_id"]: row for row in pipeline.tracks_view()}

    assert rows["STU001"]["authenticity_state"] == AUTHENTICITY_AUTHENTIC
    assert rows["STU002"]["authenticity_state"] == AUTHENTICITY_SUSPICIOUS
    assert rows["STU001"]["attendance_eligibility"] == ELIGIBLE
    assert rows["STU002"]["attendance_eligibility"] == BLOCKED_SUSPICIOUS

    print("PASS  in one frame, one student is %s/%s and the other %s/%s"
          % (rows["STU001"]["authenticity_state"],
             rows["STU001"]["attendance_eligibility"],
             rows["STU002"]["authenticity_state"],
             rows["STU002"]["attendance_eligibility"]))


# -- the eligibility gate --------------------------------------------------

def test_eligibility_requires_confirmed_authentic():
    cases = [
        (AUTHENTICITY_AUTHENTIC, True, ELIGIBLE),
        (AUTHENTICITY_AUTHENTIC, False, BLOCKED_UNVERIFIED_SOURCE),
        (AUTHENTICITY_SUSPICIOUS, True, BLOCKED_SUSPICIOUS),
        (AUTHENTICITY_SUSPICIOUS, False, BLOCKED_SUSPICIOUS),
        (AUTHENTICITY_UNCERTAIN, False, BLOCKED_UNVERIFIED_SOURCE),
        (AUTHENTICITY_UNKNOWN, False, BLOCKED_UNVERIFIED_SOURCE),
        (None, False, BLOCKED_UNVERIFIED_SOURCE),
    ]

    for source_state, confirmed, expected in cases:
        got = authenticity_eligibility(source_state, confirmed,
                                       AUTHENTICITY_MODE_ENFORCE)
        assert got == expected, (
            "%s (confirmed=%s) gave %s, expected %s"
            % (source_state, confirmed, got, expected))

    assert authenticity_eligibility(AUTHENTICITY_SUSPICIOUS, True,
                                    AUTHENTICITY_MODE_OFF) == NOT_CHECKED

    print("PASS  %d eligibility cases - only CONFIRMED AUTHENTIC is eligible"
          % len(cases))


def test_either_gate_can_block_and_neither_can_unblock():
    assert combine_eligibility(ELIGIBLE, BLOCKED_SUSPICIOUS) == BLOCKED_SUSPICIOUS
    assert combine_eligibility(BLOCKED_SPOOF, ELIGIBLE) == BLOCKED_SPOOF
    assert combine_eligibility(ELIGIBLE, ELIGIBLE) == ELIGIBLE
    assert combine_eligibility(ELIGIBLE, NOT_CHECKED) == ELIGIBLE
    assert combine_eligibility(NOT_CHECKED, ELIGIBLE) == ELIGIBLE
    assert combine_eligibility(NOT_CHECKED, NOT_CHECKED) == NOT_CHECKED

    # The narrower finding wins when both gates fire.
    assert combine_eligibility(BLOCKED_SPOOF,
                               BLOCKED_SUSPICIOUS) == BLOCKED_SPOOF

    print("PASS  either gate blocks, neither unblocks, and a switched-off "
          "gate has no opinion")


def test_suspicious_evidence_never_reaches_the_rule_engine():
    report = evaluate_evidence(
        [segment("STU001", 1)],
        liveness_states={1: (LIVENESS_LIVE, True)},
        mode="enforce",
        authenticity_states={1: (AUTHENTICITY_SUSPICIOUS, True)},
        authenticity_mode=AUTHENTICITY_MODE_ENFORCE)

    assert report.eligible_segments == []
    assert report.counts[BLOCKED_SUSPICIOUS] == 1
    assert report.withheld_students == ["STU001"]

    print("PASS  a live, recognised, suspicious track contributes 0 segments")


def test_authenticity_report_mode_changes_nothing():
    segments = [segment("STU001", 1)]
    states = {1: (AUTHENTICITY_SUSPICIOUS, True)}

    reported = evaluate_evidence(segments, authenticity_states=states,
                                 authenticity_mode=AUTHENTICITY_MODE_REPORT)
    assert len(reported.segments_for_rules(segments)) == 1, (
        "report mode filtered the evidence")
    assert len(reported.blocked_segments) == 1, (
        "report mode should still say what it would have blocked")

    print("PASS  report mode blocks nothing and reports 1 segment it would "
          "have held")


def test_authenticity_off_restores_step_9_exactly():
    segments = [segment("STU001", 1)]

    report = evaluate_evidence(
        segments,
        liveness_states={1: (LIVENESS_LIVE, True)},
        mode="enforce",
        authenticity_states={1: (AUTHENTICITY_SUSPICIOUS, True)},
        authenticity_mode=AUTHENTICITY_MODE_OFF)

    assert len(report.eligible_segments) == 1, (
        "a switched-off authenticity gate still blocked evidence")

    print("PASS  with authenticity off, a suspicious track is judged on "
          "liveness alone - Step 9 behaviour exactly")


def test_a_students_two_tracks_are_judged_separately():
    report = evaluate_evidence(
        [segment("STU001", 1), segment("STU001", 2)],
        liveness_states={1: (LIVENESS_LIVE, True), 2: (LIVENESS_LIVE, True)},
        mode="enforce",
        authenticity_states={1: (AUTHENTICITY_AUTHENTIC, True),
                             2: (AUTHENTICITY_SUSPICIOUS, True)},
        authenticity_mode=AUTHENTICITY_MODE_ENFORCE)

    assert len(report.eligible_segments) == 1
    assert report.eligible_segments[0].track_id == 1
    assert report.withheld_students == [], (
        "a student with trustworthy evidence was withheld anyway")

    print("PASS  one student, two tracks - the authentic one counts and the "
          "suspicious one does not")


def test_suspicious_liveness_never_becomes_absent():
    """
    Withheld is not absent. Step 9 established this; Step 10 must not undo it.
    """
    report = evaluate_evidence(
        [segment("STU001", 1)],
        liveness_states={1: (LIVENESS_LIVE, True)},
        mode="enforce",
        authenticity_states={1: (AUTHENTICITY_SUSPICIOUS, True)},
        authenticity_mode=AUTHENTICITY_MODE_ENFORCE)

    assert report.withheld_students == ["STU001"]
    assert report.students_for_rules(list(STUDENTS)) == ["STU002"], (
        "the suspicious student was still handed to the rule engine")

    print("PASS  a suspicious student is withheld from evaluation, never "
          "marked absent")


# -- unknown people --------------------------------------------------------

def test_a_reporting_gate_cannot_remove_evidence_when_the_other_enforces():
    """
    Found while wiring the second gate, and the reason this test exists.

    A gate in report mode has an opinion and no power. Before the fix,
    switching authenticity to enforce made the report-only LIVENESS opinion
    start filtering evidence too - so turning on one gate silently enforced
    the other, which is neither what an operator asked for nor what either
    mode is documented to do.
    """
    segments = [segment("STU001", 1)]

    report = evaluate_evidence(
        segments,
        # Liveness has no state for this track, so in report mode it thinks
        # the evidence is unverified - an opinion it must not act on.
        liveness_states={},
        mode="report",
        authenticity_states={1: (AUTHENTICITY_AUTHENTIC, True)},
        authenticity_mode=AUTHENTICITY_MODE_ENFORCE)

    assert len(report.segments_for_rules(segments)) == 1, (
        "a report-mode gate removed evidence while the other gate enforced")
    assert report.withheld_students == [], (
        "a report-mode gate withheld a student from evaluation")

    # And the reverse pairing behaves the same way.
    reverse = evaluate_evidence(
        segments,
        liveness_states={1: (LIVENESS_LIVE, True)},
        mode="enforce",
        authenticity_states={},
        authenticity_mode=AUTHENTICITY_MODE_REPORT)

    assert len(reverse.segments_for_rules(segments)) == 1, (
        "a report-mode authenticity gate removed evidence")

    print("PASS  a gate in report mode never removes evidence, whatever the "
          "other gate is doing")


def test_an_unknown_person_stays_unknown_whatever_their_authenticity():
    for kind in ("coupled", "inverted"):
        pipeline, detector, _ = build_pipeline([kind], {})
        drive(pipeline, detector)

        row = pipeline.tracks_view()[0]
        assert row["identity_state"] == "UNKNOWN"
        assert row["student_id"] is None, (
            "an unrecognised %s person was named %r" % (kind, row["student_id"]))
        assert pipeline.presence_view() == [], (
            "an unrecognised person produced presence")
        assert pipeline.segments(session_end=END) == [], (
            "an unrecognised person produced a segment")

    print("PASS  an unknown person stays UNKNOWN whether AUTHENTIC or "
          "SUSPICIOUS, and produces no segment either way")


# -- failure safety --------------------------------------------------------

def test_an_authenticity_failure_fails_safe_and_keeps_the_pipeline_running():
    pipeline, detector, _ = build_pipeline(
        ["coupled"], {0: ("STU001", "Student One")})

    for index in range(FRAMES):
        pipeline.process(detector.frame_for(index))

    before = pipeline.tracks_view()[0]
    assert before["authenticity_state"] == AUTHENTICITY_AUTHENTIC, (
        "the probe never confirmed AUTHENTIC")
    assert before["attendance_eligibility"] == ELIGIBLE

    class BrokenDetector:
        def observe(self, *args, **kwargs):
            raise RuntimeError("model gone: C:\\\\secret\\\\weights.onnx")

        def assess(self, *args, **kwargs):
            raise RuntimeError("model gone")

        def forget(self, *args, **kwargs):
            pass

    pipeline.authenticity = BrokenDetector()
    for index in range(FRAMES, FRAMES + 120):
        pipeline.process(detector.frame_for(index))

    after = pipeline.tracks_view()[0]
    summary = pipeline.authenticity_summary()

    assert after["authenticity_state"] == AUTHENTICITY_UNCERTAIN, (
        "a confirmed AUTHENTIC verdict survived the death of the checker (%s)"
        % after["authenticity_state"])
    assert after["attendance_eligibility"] != ELIGIBLE
    assert summary["authentic"] == 0
    assert summary["authenticity_available"] is False

    states = pipeline.authenticity_states()
    assert all(s == AUTHENTICITY_UNCERTAIN and not c
               for s, c in states.values()), (
        "finalisation would still have counted a pre-failure verdict")

    # Identity and liveness are unaffected: only the failed layer goes quiet.
    assert after["identity_state"] == "CONFIRMED"
    assert pipeline.frame_index + 1 == FRAMES + 120, (
        "the pipeline stopped processing frames")

    print("PASS  a dead authenticity checker withdraws its verdict, keeps the "
          "pipeline running, and leaves identity alone")


def test_the_authenticity_failure_latch_clears_only_on_a_real_check():
    pipeline, detector, _ = build_pipeline(
        ["coupled"], {0: ("STU001", "Student One")}, frames=FRAMES + 200)

    for index in range(80):
        pipeline.process(detector.frame_for(index))

    class BrokenDetector:
        def observe(self, *args, **kwargs):
            raise RuntimeError("down")

        def assess(self, *args, **kwargs):
            raise RuntimeError("down")

        def forget(self, *args, **kwargs):
            pass

    pipeline.authenticity = BrokenDetector()

    discovered = None
    for index in range(80, 90):
        pipeline.process(detector.frame_for(index))
        if pipeline.authenticity_summary()["authenticity_available"] is False:
            discovered = index
            break

    assert discovered is not None, "the broken detector was never noticed"

    for index in range(discovered + 1, 150):
        pipeline.process(detector.frame_for(index))
        assert pipeline.authenticity_summary()["authenticity_available"] is False, (
            "the latch cleared at frame %d, where no check ran" % index)

    from authenticity_detector import AuthenticityDetector
    pipeline.authenticity = AuthenticityDetector()
    for index in range(150, 200):
        pipeline.process(detector.frame_for(index))

    assert pipeline.authenticity_summary()["authenticity_available"] is True

    print("PASS  the latch holds for %d frames of failure and clears only on "
          "a real successful check" % (149 - discovered))


def test_a_liveness_failure_does_not_disturb_authenticity():
    """The two layers fail independently, as they decide independently."""
    pipeline, detector, _ = build_pipeline(
        ["coupled"], {0: ("STU001", "Student One")}, liveness_mode="report")
    drive(pipeline, detector)

    class BrokenLiveness:
        def observe(self, *args, **kwargs):
            raise RuntimeError("liveness down")

        def assess(self, *args, **kwargs):
            raise RuntimeError("liveness down")

        def forget(self, *args, **kwargs):
            pass

    pipeline.liveness = BrokenLiveness()
    for index in range(FRAMES, FRAMES + 60):
        pipeline.process(detector.frame_for(index))

    assert pipeline.liveness_summary()["liveness_available"] is False
    assert pipeline.authenticity_summary()["authenticity_available"] is True, (
        "a liveness failure took the authenticity layer down with it")
    assert pipeline.tracks_view()[0]["authenticity_state"] == \
        AUTHENTICITY_AUTHENTIC

    print("PASS  liveness failed and authenticity carried on unaffected")


# -- through the API -------------------------------------------------------

def test_the_first_status_already_reports_the_authenticity_mode():
    """
    A status that is wrong about a safety layer is worse than none.

    The worker republishes every 15 frames, so the mode has to be published
    before the worker starts. Step 9 needed exactly this fix; reintroducing
    the same gap in Step 10 would have been careless.
    """
    client, directory, holder = make_client(
        ["coupled"], {0: ("STU001", "Student One")})
    try:
        started = client.post("/api/live/start",
                              json={"session_id": SESSION_ID}).json()

        assert started["authenticity_mode"] == AUTHENTICITY_MODE_REPORT, (
            "the start response reported authenticity as %r"
            % started["authenticity_mode"])
        assert started["authenticity_baseline_version"], (
            "the start response named no baseline version")
        assert started["authentic_tracks"] == 0, (
            "authenticity counted %d authentic tracks before a frame was "
            "processed" % started["authentic_tracks"])

        client.post("/api/live/stop")
        print("PASS  the very first status already reports mode %r and "
              "baseline %s" % (started["authenticity_mode"],
                               started["authenticity_baseline_version"]))
    finally:
        teardown(client, directory)


def test_the_live_api_reports_authenticity():
    client, directory, holder = make_client(
        ["coupled"], {0: ("STU001", "Student One")})
    try:
        client.post("/api/live/start", json={"session_id": SESSION_ID})
        assert wait_for(holder, 200), (
            "the worker reached only %d frames" % reached(holder))

        status = client.get("/api/live/status").json()
        assert status["authenticity_mode"] == AUTHENTICITY_MODE_REPORT
        assert status["authenticity_available"] is True
        assert status["authenticity_baseline_version"]
        assert status["authentic_tracks"] >= 1, (
            "no track was confirmed authentic: %s" % status)

        presence = client.get("/api/live/presence").json()
        track = presence["tracks"][0]
        assert track["authenticity_state"] == AUTHENTICITY_AUTHENTIC
        assert track["identity_state"] == "CONFIRMED"
        assert track["liveness_state"], "the liveness column vanished"

        assert "research baseline" in presence["authenticity_note"].lower()
        assert "deepfake" in presence["authenticity_note"].lower()

        client.post("/api/live/stop")
        print("PASS  the live API reports identity, liveness and authenticity "
              "as three separate columns")
    finally:
        teardown(client, directory)


def test_the_stop_summary_reports_the_authenticity_gate():
    client, directory, holder = make_client(
        ["inverted"], {0: ("STU001", "Student One")},
        authenticity_mode=AUTHENTICITY_MODE_ENFORCE)
    try:
        client.post("/api/live/start", json={"session_id": SESSION_ID})
        assert wait_for(holder, 200), (
            "the worker reached only %d frames" % reached(holder))

        body = client.post("/api/live/stop").json()
        gate = body["liveness"]

        assert gate["authenticity_mode"] == AUTHENTICITY_MODE_ENFORCE
        assert gate["authenticity_baseline_version"]
        assert gate["enforced"] is True

        attendance = AttendanceRepository(state.get_database())
        record = attendance.get(SESSION_ID, "STU001")
        assert record is None or record["present_seconds"] == 0, (
            "a suspicious track credited presence")

        print("PASS  the stop summary reports the authenticity gate and "
              "credits no presence to a suspicious track")
    finally:
        teardown(client, directory)


def test_the_api_leaks_no_cue_value_frame_or_path():
    client, directory, holder = make_client(
        ["coupled", "inverted"],
        {0: ("STU001", "Student One"), 1: ("STU002", "Student Two")})
    try:
        client.post("/api/live/start", json={"session_id": SESSION_ID})
        assert wait_for(holder, 200), (
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
            for magic in ("data:image", "base64,", "\\x89PNG"):
                assert magic not in text

        assert not offenders, (
            "authenticity responses exposed:\n  " + "\n  ".join(offenders))

        print("PASS  %d responses checked against %d forbidden patterns - "
              "nothing leaked" % (len(bodies), len(FORBIDDEN_KEYS)))
    finally:
        teardown(client, directory)


def test_an_authenticity_error_leaks_no_model_path():
    client, directory, holder = make_client(
        ["coupled"], {0: ("STU001", "Student One")})
    try:
        client.post("/api/live/start", json={"session_id": SESSION_ID})
        assert wait_for(holder, 60), (
            "the worker reached only %d frames" % reached(holder))

        class LeakyDetector:
            def observe(self, *args, **kwargs):
                raise RuntimeError(
                    "failed to load C:\\\\models\\\\secret_deepfake.onnx "
                    "with token xyz789")

            def assess(self, *args, **kwargs):
                raise RuntimeError("same")

            def forget(self, *args, **kwargs):
                pass

        holder["pipeline"].authenticity = LeakyDetector()
        assert wait_for(holder, 160), (
            "the worker reached only %d frames" % reached(holder))

        bodies = [client.get("/api/live/status").json(),
                  client.get("/api/live/presence").json(),
                  client.post("/api/live/stop").json()]

        text = json.dumps(bodies).lower()
        for leak in ("secret_deepfake", ".onnx", "token xyz789", "traceback",
                     "c:\\\\", "/users/"):
            assert leak not in text, "an authenticity error leaked %r" % leak

        assert "runtimeerror" in text, (
            "the failure should still be reported by type")

        print("PASS  an error naming a model path and a token leaks neither - "
              "only the exception type")
    finally:
        teardown(client, directory)


def test_an_unknown_session_is_still_a_404():
    client, directory, _ = make_client(["coupled"], {})
    try:
        response = client.post("/api/live/start",
                               json={"session_id": "NO-SUCH-SESSION"})
        assert response.status_code == 404
        assert client.get("/api/live/status").json()["state"] == "IDLE"
        print("PASS  an unknown session is still 404, and nothing started")
    finally:
        teardown(client, directory)


def test_the_camera_is_released_on_a_clean_stop():
    client, directory, holder = make_client(
        ["coupled"], {0: ("STU001", "Student One")})
    try:
        client.post("/api/live/start", json={"session_id": SESSION_ID})
        assert wait_for(holder, 100), (
            "the worker reached only %d frames" % reached(holder))

        client.post("/api/live/stop")

        assert holder["camera"].released >= 1, "the camera was not released"
        status = client.get("/api/live/status").json()
        assert status["state"] == "COMPLETED"
        assert status["camera"] == "released"

        print("PASS  Step 8 camera lifecycle intact with Step 10 running")
    finally:
        teardown(client, directory)


# -- nothing else moved ----------------------------------------------------

def test_the_attendance_rule_engine_is_untouched_by_step_10():
    source = (PROJECT_ROOT / "ai" / "attendance_rules.py").read_text(
        encoding="utf-8")

    for word in ("authenticity", "deepfake", "suspicious", "manipulat"):
        assert word not in source.lower(), (
            "ai/attendance_rules.py mentions %r - Step 10 has leaked into "
            "the rule engine" % word)

    print("PASS  the Step 6 rule engine contains no authenticity logic at all")


def test_step_10_adds_no_database_table_or_column():
    from database import SCHEMA

    # Every table the project is allowed to have, listed by hand in
    # tests/schema_baseline.py. Step 10 contributes none of them.
    expected = EXPECTED_TABLES
    created = sorted(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA))
    assert created == expected, "the schema declares %s" % created

    for word in ("authenticity", "deepfake", "manipulat"):
        assert word not in SCHEMA.lower(), (
            "the schema gained a %r column" % word)

    directory = Path(tempfile.mkdtemp(prefix="classvision_auth_schema_"))
    try:
        state.configure(directory / "schema.db")
        tables = sorted(state.get_database().table_names())
        assert tables == expected, "the live schema has %s" % tables
        print("PASS  no authenticity table and no authenticity column - the "
              "same %d tables" % len(tables))
    finally:
        state.close_database()
        shutil.rmtree(directory, ignore_errors=True)


def test_step_10_writes_nothing_to_disk():
    """A research signal is not worth a single byte of biometric media."""
    before = {p for p in (PROJECT_ROOT / "data").glob("*")}

    pipeline, detector, _ = build_pipeline(
        ["coupled"], {0: ("STU001", "Student One")})
    drive(pipeline, detector)

    after = {p for p in (PROJECT_ROOT / "data").glob("*")}
    assert before == after, "authenticity analysis created %s" % (after - before)

    for pattern in ("*.jpg", "*.png", "*.npy", "*.npz", "*.mp4"):
        found = list(PROJECT_ROOT.glob(pattern)) + \
            list((PROJECT_ROOT / "ai").glob(pattern))
        assert not found, "authenticity analysis wrote %s" % found

    print("PASS  a full authenticity run wrote no image, vector or video")


def test_recognition_threshold_is_untouched():
    from face_recognizer import DEFAULT_RECOGNITION_THRESHOLD

    assert DEFAULT_RECOGNITION_THRESHOLD == 0.45, (
        "Step 10 changed the recognition threshold to %r"
        % DEFAULT_RECOGNITION_THRESHOLD)
    print("PASS  the recognition threshold is still exactly 0.45")


def main():
    enable_safe_console()

    tests = [
        test_identity_is_never_changed_by_authenticity,
        test_authenticity_is_never_set_by_recognition,
        test_liveness_and_authenticity_are_independent,
        test_two_tracks_hold_different_authenticity_in_one_frame,
        test_eligibility_requires_confirmed_authentic,
        test_either_gate_can_block_and_neither_can_unblock,
        test_suspicious_evidence_never_reaches_the_rule_engine,
        test_authenticity_report_mode_changes_nothing,
        test_authenticity_off_restores_step_9_exactly,
        test_a_students_two_tracks_are_judged_separately,
        test_suspicious_liveness_never_becomes_absent,
        test_a_reporting_gate_cannot_remove_evidence_when_the_other_enforces,
        test_an_unknown_person_stays_unknown_whatever_their_authenticity,
        test_an_authenticity_failure_fails_safe_and_keeps_the_pipeline_running,
        test_the_authenticity_failure_latch_clears_only_on_a_real_check,
        test_a_liveness_failure_does_not_disturb_authenticity,
        test_the_first_status_already_reports_the_authenticity_mode,
        test_the_live_api_reports_authenticity,
        test_the_stop_summary_reports_the_authenticity_gate,
        test_the_api_leaks_no_cue_value_frame_or_path,
        test_an_authenticity_error_leaks_no_model_path,
        test_an_unknown_session_is_still_a_404,
        test_the_camera_is_released_on_a_clean_stop,
        test_the_attendance_rule_engine_is_untouched_by_step_10,
        test_step_10_adds_no_database_table_or_column,
        test_step_10_writes_nothing_to_disk,
        test_recognition_threshold_is_untouched,
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
