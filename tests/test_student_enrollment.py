"""
Dashboard student registration and face enrollment tests.

NO WEBCAM AND NO MODEL ARE NEEDED. The enrollment manager's camera and
detector/embedder factories are replaced with fakes, so the whole lifecycle -
start, progress, save, cancel, fail - runs on a machine with no camera.

Every test uses its OWN temporary SQLite file AND its own temporary embedding
store. Neither data/classvision.db nor data/embeddings is ever opened.

WHAT THIS VERIFIES
    That the student registry and the face-enrollment store are finally joined
    on student_id, that a dashboard enrollment produces the same records the
    command-line tool produces, that an enrolled student is immediately
    selectable when scheduling a session, and that no frame, crop or embedding
    can cross the API boundary.

WHAT THIS DOES NOT VERIFY
    Recognition accuracy. Enrollment stores vectors; whether they identify
    anybody is Step 4's question and is unchanged.

Run with:
    python tests/test_student_enrollment.py
"""

import json
import shutil
import sys
import tempfile
import threading
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "ai"))
sys.path.insert(0, str(PROJECT_ROOT / "database"))

import backend  # noqa: F401,E402

import numpy as np  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from embedding_store import EmbeddingStore  # noqa: E402
from enrollment_rules import DEFAULT_SAMPLES  # noqa: E402
from enrollment_session import (  # noqa: E402
    ENROLL_CANCELLED,
    ENROLL_COMPLETED,
    ENROLL_FAILED,
    ENROLL_IDLE,
    SampleCollector,
)
from face_detector import Detection  # noqa: E402
from live_camera import CameraError  # noqa: E402
from repositories import StudentRepository  # noqa: E402

from backend.services import enrollment_service, live_service, state  # noqa: E402
from paths import enable_safe_console  # noqa: E402

EMBEDDING_DIM = 128

# Anything resembling biometric data, media, a path or a secret.
FORBIDDEN_KEYS = (
    "embedding", "embeddings", "vector", "vectors", "descriptor", "feature",
    "face", "image", "photo", "crop", "frame", "video", "thumbnail",
    "password", "secret", "token", "credential", "api_key", "db_path",
    "filepath", "file_path", "connection_string", "npz",
)


# -- fakes ------------------------------------------------------------------

class FakeCamera:
    """Hands out frames. Never opens anything."""

    def __init__(self, frames=None, fail_after=None, never_opens=False):
        self.frames = frames
        self.fail_after = fail_after
        self.never_opens = never_opens
        self.reads = 0
        self.released = 0

    def read(self):
        self.reads += 1
        time.sleep(0.001)
        if self.fail_after is not None and self.reads > self.fail_after:
            return None
        return np.full((480, 640, 3), 120, dtype=np.uint8)

    def release(self):
        self.released += 1
        return True


class FakeDetector:
    """One well-placed face, every frame."""

    def __init__(self, faces=1):
        self.faces = faces

    def detect(self, frame):
        points = np.array([[0.31, 0.36], [0.69, 0.36], [0.50, 0.57],
                           [0.35, 0.75], [0.65, 0.75]], dtype=np.float32)
        detections = []
        for index in range(self.faces):
            x, y, size = 220 + index * 40, 150, 180
            landmarks = np.column_stack([x + points[:, 0] * size,
                                         y + points[:, 1] * size]
                                        ).astype(np.float32)
            detections.append(Detection(x, y, size, size, 0.95,
                                        landmarks=landmarks))
        return detections


class FakeEmbedder:
    """Returns unit vectors. No model, no ONNX file."""

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def embed_detection(self, frame, detection):
        self.calls += 1
        if self.fail:
            raise ValueError("synthetic embedding failure")
        vector = np.zeros(EMBEDDING_DIM, dtype=np.float32)
        vector[self.calls % EMBEDDING_DIM] = 1.0
        return vector

    def model_info(self):
        return {
            "model_name": "sface", "model_version": "2021dec",
            "model_file": "face_recognition_sface_2021dec.onnx",
            "model_sha256": "0" * 64, "embedding_dim": EMBEDDING_DIM,
            "normalization": "l2", "similarity": "cosine",
        }


# -- harness ----------------------------------------------------------------

def make_client(camera=None, detector=None, embedder=None, camera_error=None):
    directory = Path(tempfile.mkdtemp(prefix="classvision_enroll_"))
    state.configure(directory / "enroll.db")

    store = EmbeddingStore(directory / "embeddings")
    holder = {"camera": camera or FakeCamera(), "store": store,
              "embedder": embedder or FakeEmbedder()}

    def camera_factory(config):
        if camera_error:
            raise CameraError(camera_error)
        return holder["camera"]

    def component_factory():
        return detector or FakeDetector(), holder["embedder"]

    manager = enrollment_service.EnrollmentManager(
        camera_factory=camera_factory, component_factory=component_factory)

    # Point the manager and the Students page at the temporary store, so the
    # real data/embeddings directory is never touched.
    original_start = manager.start

    def start(database, **kwargs):
        kwargs.setdefault("store", store)
        return original_start(database, **kwargs)

    manager.start = start
    enrollment_service.reset_enrollment_manager(manager)

    original_metadata = enrollment_service.enrollment_metadata

    def scoped_metadata(store_argument=None):
        return original_metadata(store_argument or store)

    enrollment_service.enrollment_metadata = scoped_metadata
    holder["original_metadata"] = original_metadata

    from backend.app import app
    return TestClient(app), directory, holder


def teardown(client, directory, holder=None):
    if holder and holder.get("original_metadata"):
        enrollment_service.enrollment_metadata = holder["original_metadata"]
    enrollment_service.reset_enrollment_manager(None)
    live_service.reset_live_manager(None)
    client.close()
    state.close_database()
    shutil.rmtree(directory, ignore_errors=True)


def wait_for_state(client, states, timeout=30.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get("/api/students/enrollment/status").json()
        if last["state"] in states:
            return last
        time.sleep(0.02)
    return last


def details(**overrides):
    body = {"student_id": "STU002", "name": "Test Student",
            "roll_number": "002", "class_name": "CSE(AI)", "section": "C"}
    body.update(overrides)
    return body


def walk(value, visit, trail="$"):
    if isinstance(value, dict):
        for key, item in value.items():
            visit(key, "%s.%s" % (trail, key))
            walk(item, visit, "%s.%s" % (trail, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            walk(item, visit, "%s[%d]" % (trail, index))


# -- the sample collector (shared with the CLI) ----------------------------

def test_the_collector_needs_stability_before_taking_a_sample():
    """
    One good frame is not a sample.

    This is the rule the CLI has always applied; it now lives in one place
    and both paths use it, which is the point of the refactor.
    """
    from enrollment_rules import STABLE_FRAMES_REQUIRED

    collector = SampleCollector(FakeDetector(), FakeEmbedder(), 3)
    frame = np.full((480, 640, 3), 120, dtype=np.uint8)

    for index in range(STABLE_FRAMES_REQUIRED - 1):
        captured, _, _ = collector.offer(frame)
        assert not captured, "a sample was taken after %d frames" % (index + 1)

    captured, reason, _ = collector.offer(frame)
    assert captured, "no sample after %d stable frames" % STABLE_FRAMES_REQUIRED
    assert collector.captured == 1
    print("PASS  %d stable frames are needed before a sample is taken"
          % STABLE_FRAMES_REQUIRED)


def test_the_collector_cools_down_between_samples():
    """Two samples must not come from nearly the same moment."""
    clock = {"now": 1000.0}
    collector = SampleCollector(FakeDetector(), FakeEmbedder(), 5,
                                clock=lambda: clock["now"])
    frame = np.full((480, 640, 3), 120, dtype=np.uint8)

    while collector.captured < 1:
        collector.offer(frame)

    for _ in range(20):
        captured, _, _ = collector.offer(frame)
        assert not captured, "a second sample was taken during the cooldown"

    clock["now"] += collector.cooldown + 0.01
    for _ in range(20):
        if collector.offer(frame)[0]:
            break

    assert collector.captured == 2
    print("PASS  a cooldown separates samples; the clock is injectable")


def test_a_rejected_sample_is_counted_and_not_stored():
    collector = SampleCollector(FakeDetector(), FakeEmbedder(fail=True), 3)
    frame = np.full((480, 640, 3), 120, dtype=np.uint8)

    for _ in range(40):
        collector.offer(frame)

    assert collector.captured == 0, "a failing embedder still stored samples"
    assert collector.rejected > 0, "rejections were not counted"
    print("PASS  %d samples rejected, 0 stored" % collector.rejected)


def test_the_cli_and_the_dashboard_share_the_capture_rules():
    """If these ever diverge, two students are held to different standards."""
    import enroll_student
    import enrollment_rules

    assert enroll_student.DEFAULT_SAMPLES is enrollment_rules.DEFAULT_SAMPLES
    assert enroll_student.STABLE_FRAMES_REQUIRED is \
        enrollment_rules.STABLE_FRAMES_REQUIRED
    assert enroll_student.COOLDOWN_SECONDS is enrollment_rules.COOLDOWN_SECONDS
    assert "SampleCollector" in (PROJECT_ROOT / "ai" / "enroll_student.py"
                                 ).read_text(encoding="utf-8")

    print("PASS  the CLI imports the shared rules and the shared collector")


# -- registration ----------------------------------------------------------

def test_a_student_can_be_registered_without_a_camera():
    client, directory, holder = make_client()
    try:
        response = client.post("/api/students", json={
            "student_id": "STU010", "name": "Registered Only"})
        assert response.status_code == 201, response.text

        body = response.json()
        assert body["student_id"] == "STU010"
        assert body["enrolled"] is False, (
            "a student with no face samples was reported as enrolled")
        assert body["sample_count"] == 0

        print("PASS  a student registers without a camera and is reported "
              "Not enrolled")
    finally:
        teardown(client, directory, holder)


def test_a_duplicate_student_id_is_refused():
    client, directory, holder = make_client()
    try:
        first = client.post("/api/students", json={
            "student_id": "STU010", "name": "First"})
        assert first.status_code == 201

        second = client.post("/api/students", json={
            "student_id": "STU010", "name": "Second"})
        assert second.status_code == 409, (
            "a duplicate returned %d" % second.status_code)

        stored = client.get("/api/students/STU010").json()
        assert stored["name"] == "First", (
            "the duplicate overwrote the original name")

        print("PASS  a duplicate student id is 409 and changes nothing")
    finally:
        teardown(client, directory, holder)


def test_registration_rejects_missing_fields():
    client, directory, holder = make_client()
    try:
        for payload, description in (
                ({}, "nothing"),
                ({"student_id": "STU010"}, "no name"),
                ({"name": "No Id"}, "no id"),
                ({"student_id": "  ", "name": "Blank id"}, "blank id"),
                ({"student_id": "STU010", "name": "   "}, "blank name"),
                ({"student_id": "x" * 200, "name": "Long"}, "over-long id")):
            response = client.post("/api/students", json=payload)
            assert response.status_code in (400, 422), (
                "%s returned %d" % (description, response.status_code))

        assert client.get("/api/students").json()["total"] == 0
        print("PASS  6 malformed registrations refused, nothing created")
    finally:
        teardown(client, directory, holder)


# -- enrollment lifecycle --------------------------------------------------

def test_enrollment_status_answers_before_anything_starts():
    client, directory, holder = make_client()
    try:
        body = client.get("/api/students/enrollment/status").json()
        assert body["state"] == ENROLL_IDLE
        assert body["captured"] == 0
        assert body["student_id"] is None
        print("PASS  enrollment status answers IDLE - not an error")
    finally:
        teardown(client, directory, holder)


def test_a_full_enrollment_writes_both_stores():
    """
    The whole point of the feature.

    Vectors for recognition, and a row for the dashboard - the two stores that
    have always shared a key and never been linked.
    """
    client, directory, holder = make_client()
    try:
        started = client.post("/api/students/enrollment/start",
                              json=details())
        assert started.status_code == 200, started.text
        assert started.json()["required"] == DEFAULT_SAMPLES

        final = wait_for_state(client, {ENROLL_COMPLETED, ENROLL_FAILED})
        assert final["state"] == ENROLL_COMPLETED, (
            "enrollment ended %s: %s" % (final["state"], final.get("error")))
        assert final["captured"] == DEFAULT_SAMPLES
        assert final["samples_stored"] == DEFAULT_SAMPLES
        assert final["registry_synced"] is True

        # 1. The embedding store has the vectors and the metadata.
        record = holder["store"].get_student("STU002")
        assert record is not None
        assert record["class_name"] == "CSE(AI)"
        assert record["roll_number"] == "002"
        vectors, _ = holder["store"].load_embeddings("STU002")
        assert vectors.shape == (DEFAULT_SAMPLES, EMBEDDING_DIM)

        # 2. The attendance database has the student row.
        registry = StudentRepository(state.get_database()).get("STU002")
        assert registry is not None, (
            "the student was enrolled but never registered - the exact bug "
            "this feature exists to fix")
        assert registry["name"] == "Test Student"

        # 3. The Students page sees one student, enrolled.
        listing = client.get("/api/students").json()
        assert listing["total"] == 1
        item = listing["items"][0]
        assert item["enrolled"] is True
        assert item["sample_count"] == DEFAULT_SAMPLES
        assert item["class_name"] == "CSE(AI)"
        assert item["section"] == "C"
        assert item["roll_number"] == "002"

        print("PASS  one enrollment wrote %d vectors, a registry row, and "
              "shows as enrolled on the Students page" % DEFAULT_SAMPLES)
    finally:
        teardown(client, directory, holder)


def test_enrollment_reports_progress_while_it_runs():
    client, directory, holder = make_client()
    try:
        client.post("/api/students/enrollment/start", json=details())

        seen = []
        deadline = time.time() + 30
        while time.time() < deadline:
            status = client.get("/api/students/enrollment/status").json()
            seen.append((status["state"], status["captured"]))
            if status["state"] in (ENROLL_COMPLETED, ENROLL_FAILED):
                break
            time.sleep(0.02)

        counts = sorted({captured for _, captured in seen})
        assert counts[0] == 0 and counts[-1] == DEFAULT_SAMPLES, (
            "progress never ran from 0 to %d: %s" % (DEFAULT_SAMPLES, counts))
        assert len(counts) > 2, (
            "progress jumped straight to the end: %s" % counts)

        print("PASS  progress was observed at sample counts %s" % counts)
    finally:
        teardown(client, directory, holder)


def test_a_second_enrollment_is_refused_while_one_runs():
    client, directory, holder = make_client()
    try:
        assert client.post("/api/students/enrollment/start",
                           json=details()).status_code == 200

        second = client.post("/api/students/enrollment/start",
                             json=details(student_id="STU003", name="Other"))
        assert second.status_code == 409, (
            "a second enrollment returned %d" % second.status_code)

        client.post("/api/students/enrollment/cancel")
        print("PASS  a second enrollment is refused with 409")
    finally:
        teardown(client, directory, holder)


def test_enrollment_can_be_cancelled_and_stores_nothing():
    slow = FakeCamera()
    client, directory, holder = make_client(camera=slow)
    try:
        client.post("/api/students/enrollment/start",
                    json=details(samples=20))

        cancelled = client.post("/api/students/enrollment/cancel").json()
        assert cancelled["state"] in (ENROLL_CANCELLED, ENROLL_COMPLETED)

        if cancelled["state"] == ENROLL_CANCELLED:
            assert not holder["store"].has_student("STU002"), (
                "a cancelled enrollment stored vectors")
            assert StudentRepository(state.get_database()).get("STU002") is None, (
                "a cancelled enrollment created a student row")
            assert slow.released >= 1, "the camera was not released"

        print("PASS  a cancelled enrollment stores nothing and releases the "
              "camera")
    finally:
        teardown(client, directory, holder)


def test_an_unavailable_camera_fails_cleanly():
    client, directory, holder = make_client(
        camera_error="Camera 3 could not be opened. It may be in use by "
                     "another application.")
    try:
        response = client.post("/api/students/enrollment/start",
                               json=details(camera_index=3))
        assert response.status_code == 503, (
            "a missing camera returned %d" % response.status_code)

        detail = response.json()["detail"]
        assert "Camera 3" in detail
        for leak in ("Traceback", "File \"", "sqlite", "C:\\"):
            assert leak not in detail, "the error leaked %r" % leak

        assert client.get("/api/students/enrollment/status").json()["state"] \
            == ENROLL_FAILED
        assert client.get("/api/students").json()["total"] == 0, (
            "a failed enrollment created a student")

        print("PASS  an unavailable camera is 503, FAILED, and creates nothing")
    finally:
        teardown(client, directory, holder)


def test_a_camera_that_dies_mid_enrollment_fails_safely():
    dying = FakeCamera(fail_after=3)
    client, directory, holder = make_client(camera=dying)
    try:
        client.post("/api/students/enrollment/start", json=details())
        final = wait_for_state(client, {ENROLL_FAILED, ENROLL_COMPLETED})

        assert final["state"] == ENROLL_FAILED, (
            "a dead camera ended %s" % final["state"])
        assert "camera" in (final["error"] or "").lower()
        assert "Traceback" not in (final["error"] or "")
        assert dying.released >= 1, "the camera was not released"
        assert not holder["store"].has_student("STU002"), (
            "a failed enrollment stored vectors")

        print("PASS  a camera that dies mid-enrollment fails safely and "
              "stores nothing")
    finally:
        teardown(client, directory, holder)


def test_enrollment_refuses_an_already_enrolled_student():
    client, directory, holder = make_client()
    try:
        client.post("/api/students/enrollment/start", json=details())
        wait_for_state(client, {ENROLL_COMPLETED, ENROLL_FAILED})

        again = client.post("/api/students/enrollment/start", json=details())
        assert again.status_code == 409, (
            "re-enrolling returned %d" % again.status_code)
        assert "already enrolled" in again.json()["detail"].lower()

        replaced = client.post("/api/students/enrollment/start",
                               json=details(replace=True))
        assert replaced.status_code == 200, (
            "an explicit replace was refused: %s" % replaced.text)
        client.post("/api/students/enrollment/cancel")

        print("PASS  re-enrolling needs an explicit replace")
    finally:
        teardown(client, directory, holder)


def test_enrollment_refuses_while_a_live_session_holds_the_camera():
    """One webcam cannot serve a lesson and an enrollment at once."""
    client, directory, holder = make_client()
    try:
        live = live_service.get_live_manager()
        live.runtime.begin("SES-BUSY", class_id="10A", camera_index=0)
        live.runtime.mark_running()

        response = client.post("/api/students/enrollment/start",
                               json=details())
        assert response.status_code == 409, (
            "enrollment started while a lesson held the camera (%d)"
            % response.status_code)
        assert "live classroom" in response.json()["detail"].lower(), (
            "the refusal does not say what to stop: %s"
            % response.json()["detail"])

        print("PASS  enrollment is refused while a live session holds the "
              "camera, and says which one to stop")
    finally:
        live_service.get_live_manager().runtime.reset()
        teardown(client, directory, holder)


def test_enrollment_rejects_a_dangerous_student_id():
    client, directory, holder = make_client()
    try:
        for bad in ("../escape", "a/b", "with space", ""):
            response = client.post("/api/students/enrollment/start",
                                   json=details(student_id=bad))
            assert response.status_code in (400, 422), (
                "%r returned %d" % (bad, response.status_code))

        print("PASS  path-unsafe and empty student ids are refused")
    finally:
        teardown(client, directory, holder)


# -- the rest of the system sees the student -------------------------------

def test_an_enrolled_student_can_be_expected_in_a_session():
    """The symptom that started this: an enrolled student was unselectable."""
    client, directory, holder = make_client()
    try:
        client.post("/api/students/enrollment/start", json=details())
        assert wait_for_state(client,
                              {ENROLL_COMPLETED})["state"] == ENROLL_COMPLETED

        roster = client.get("/api/students?status=ACTIVE").json()
        assert any(item["student_id"] == "STU002" for item in roster["items"]), (
            "the enrolled student is not offered as an expected student")

        from datetime import datetime, timedelta, timezone
        start = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0)

        created = client.post("/api/sessions", json={
            "class_id": "CSE(AI)-C",
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(hours=1)).isoformat(),
            "expected_students": ["STU002"]})

        assert created.status_code == 201, created.text
        assert created.json()["expected_students"] == ["STU002"]

        print("PASS  an enrolled student is selectable and a session can be "
              "scheduled for them")
    finally:
        teardown(client, directory, holder)


def test_the_recognition_gallery_can_load_the_enrollment():
    """
    Recognition reads the embedding store, not the database.

    A dashboard enrollment has to produce a record Step 4's gallery accepts,
    or the student is registered and still unrecognisable.
    """
    client, directory, holder = make_client()
    try:
        client.post("/api/students/enrollment/start", json=details())
        wait_for_state(client, {ENROLL_COMPLETED})

        from face_recognizer import EnrolledGallery

        gallery = EnrolledGallery(holder["store"])
        assert gallery.student_count == 1, (
            "the gallery loaded %d students" % gallery.student_count)
        assert gallery.student_ids == ["STU002"]
        assert gallery.embedding_count == DEFAULT_SAMPLES
        assert not gallery.is_empty

        print("PASS  Step 4's gallery loads the dashboard enrollment: %d "
              "student, %d vectors"
              % (gallery.student_count, gallery.embedding_count))
    finally:
        teardown(client, directory, holder)


def test_a_student_row_without_an_enrollment_is_reported_honestly():
    """State B: registered, never sat in front of a camera."""
    client, directory, holder = make_client()
    try:
        client.post("/api/students", json={"student_id": "STU777",
                                           "name": "Never Enrolled",
                                           "roll_number": "777",
                                           "class_name": "CSE(AI)",
                                           "section": "B"})

        item = client.get("/api/students/STU777").json()
        assert item["enrolled"] is False
        assert item["sample_count"] == 0

        # The registry metadata is stored whether or not a camera was ever
        # involved - since schema version 2 these are SQLite columns, so
        # registration alone produces a complete student record.
        assert item["roll_number"] == "777"
        assert item["class_name"] == "CSE(AI)"
        assert item["section"] == "B"

        print("PASS  a registered-but-unenrolled student is reported as such, "
              "with their metadata, not invented")
    finally:
        teardown(client, directory, holder)


def test_an_enrollment_without_a_student_row_is_not_invented():
    """
    State C: vectors exist, no registry row.

    The Students page lists the DATABASE. An orphan enrollment must not
    conjure a student row, because nothing knows that person's status or when
    they were added.
    """
    client, directory, holder = make_client()
    try:
        holder["store"].save_student(
            student_id="STU888", name="Orphan", roll_number="888",
            class_name="CSE(AI)", section="C",
            model_name="sface", model_version="2021dec")

        listing = client.get("/api/students").json()
        assert listing["total"] == 0, (
            "an orphan enrollment invented a student row")

        print("PASS  an enrollment with no registry row invents nothing")
    finally:
        teardown(client, directory, holder)


# -- privacy ---------------------------------------------------------------

def test_no_student_or_enrollment_response_exposes_biometric_data():
    client, directory, holder = make_client()
    try:
        client.post("/api/students/enrollment/start", json=details())
        wait_for_state(client, {ENROLL_COMPLETED})

        bodies = {
            "/api/students": client.get("/api/students").json(),
            "/api/students/STU002": client.get("/api/students/STU002").json(),
            "/api/students/enrollment/status":
                client.get("/api/students/enrollment/status").json(),
        }

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
                assert magic not in text, "%s returned embedded media" % url

        assert not offenders, (
            "student responses exposed:\n  " + "\n  ".join(offenders))

        # And no long numeric array smuggled through under an innocent name.
        text = json.dumps(bodies)
        assert text.count("0.") < 20, (
            "a response carries something that looks like a vector")

        print("PASS  %d student responses checked against %d forbidden "
              "patterns - nothing leaked" % (len(bodies), len(FORBIDDEN_KEYS)))
    finally:
        teardown(client, directory, holder)


def test_the_browser_cannot_submit_an_embedding():
    """Vectors are produced server-side from the camera, or not at all."""
    from backend.schemas.models import (
        EnrollmentStartRequest,
        StudentCreateRequest,
    )

    for model in (StudentCreateRequest, EnrollmentStartRequest):
        for field in model.model_fields:
            for banned in FORBIDDEN_KEYS:
                assert banned not in field.lower(), (
                    "%s declares %r" % (model.__name__, field))

    client, directory, holder = make_client()
    try:
        # An extra field is ignored, not stored.
        response = client.post("/api/students", json={
            "student_id": "STU010", "name": "Smuggler",
            "embedding": [0.1] * 128, "face_image": "AAAA"})
        assert response.status_code == 201
        assert "embedding" not in json.dumps(response.json())

        print("PASS  neither request model accepts a vector, and extra "
              "fields are ignored")
    finally:
        teardown(client, directory, holder)


def test_enrollment_writes_no_image_to_disk():
    client, directory, holder = make_client()
    try:
        client.post("/api/students/enrollment/start", json=details())
        wait_for_state(client, {ENROLL_COMPLETED})

        found = []
        for pattern in ("*.jpg", "*.jpeg", "*.png", "*.bmp", "*.mp4", "*.avi"):
            found.extend(directory.rglob(pattern))

        assert not found, "enrollment wrote %s" % found

        written = sorted(p.name for p in (directory / "embeddings").glob("*"))
        assert written == ["STU002.npz", "students.json"], (
            "unexpected files in the store: %s" % written)

        print("PASS  a full enrollment wrote exactly students.json and one "
              ".npz - no image of any kind")
    finally:
        teardown(client, directory, holder)


def test_tests_never_touch_the_real_stores():
    from database import DEFAULT_DATABASE_PATH
    from embedding_store import EMBEDDINGS_DIR

    client, directory, holder = make_client()
    try:
        assert state.get_database().path != DEFAULT_DATABASE_PATH
        assert holder["store"].directory != EMBEDDINGS_DIR
        assert str(directory) in str(holder["store"].directory)
        print("PASS  enrollment tests use a temporary database AND a "
              "temporary embedding store")
    finally:
        teardown(client, directory, holder)


# -- the dashboard ---------------------------------------------------------

STATIC = PROJECT_ROOT / "backend" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "styles.css").read_text(encoding="utf-8")


def test_the_students_page_has_an_add_student_control():
    assert 'id="add-student"' in HTML, "there is no Add Student button"
    assert "+ Add Student" in HTML
    assert 'id="student-dialog"' in HTML, "there is no enrollment dialog"

    for control in ("new-student-id", "new-student-name", "new-student-roll",
                    "new-student-class", "new-student-section",
                    "new-student-camera"):
        assert 'id="%s"' % control in HTML, "%s is missing" % control
        assert 'for="%s"' % control in HTML, "%s has no label" % control

    print("PASS  the Students page has a labelled Add Student dialog")


def test_the_dialog_explains_what_is_and_is_not_stored():
    dialog = HTML[HTML.index('id="student-dialog"'):
                  HTML.index('id="session-dialog"')]

    assert "No photograph" in dialog, (
        "the dialog does not say that no photograph is stored")
    assert "embedding" in dialog.lower(), (
        "the dialog does not say what IS stored")
    assert "recognise" in dialog.lower() or "recognize" in dialog.lower(), (
        "the dialog does not explain why enrollment matters")

    print("PASS  the dialog states that no photograph is stored and what is")


def test_the_dashboard_never_renders_or_uploads_an_image():
    """
    A STUDENT's face never reaches the browser, in either direction.

    The slice is bounded to the student-registry section rather than running
    to the end of the file. That is the code this ban was written about and
    it is enforced over all of it, unchanged.

    What is deliberately outside the slice is the staff personnel dialog,
    which does open the browser's camera. The two are not the same thing and
    a file-wide string search cannot tell them apart:

        a student's face is biometric data the recognition pipeline acts on,
        captured server-side, never sent to a browser, and the whole privacy
        argument of this project rests on it staying that way;

        a teacher's ID photograph is a picture somebody is choosing to have
        taken of themselves, nothing computes an embedding from it, and
        backend/services/staff_files_service.py imports nothing from ai/ so
        that it cannot quietly start.
    """
    enrollment_js = JS[JS.index("add, edit and remove a student"):
                       JS.index("cancel or delete a session")]

    for streaming in ("<img", "<video", "<canvas", "getUserMedia",
                      "MediaRecorder", "FileReader", "FormData"):
        assert streaming not in enrollment_js, (
            "the enrollment UI uses %s - the camera is read server-side and "
            "the browser must never touch a frame" % streaming)

    assert "/api/students/enrollment/start" in JS
    assert "/api/students/enrollment/status" in JS
    print("PASS  the enrollment UI polls JSON and touches no image")


def test_the_student_list_refreshes_after_enrollment():
    assert "refreshAfterEnrollment" in JS
    assert "loadStudents" in JS and "loadRoster" in JS, (
        "the session roster is not refreshed, so a new student would not be "
        "selectable until a reload")
    print("PASS  a completed enrollment refreshes the student list and the "
          "session roster")


def test_the_enrollment_ui_adds_no_animation_or_external_request():
    """
    The enrollment UI stays still, and nothing is fetched from anywhere.

    The dashboard-wide animation ban that used to be asserted here was
    replaced by a reduced-motion contract in
    tests/test_dashboard_frontend.py once the project asked for a designed
    interface. This keeps the narrow half: the capture progress display must
    not animate, because somebody is reading it to decide whether to stand
    still.
    """
    enrollment_js = JS[JS.index("add, edit and remove a student"):
                       JS.index("cancel or delete a session")]
    assert "requestAnimationFrame" not in enrollment_js, (
        "the enrollment UI animates while somebody is being asked to hold "
        "still and read it")

    combined = (HTML + CSS + JS).replace("http://www.w3.org/2000/svg", "")
    for external in ("http://", "https://", "//cdn", "unpkg", "jsdelivr"):
        assert external not in combined

    # A ceiling, not a target: it exists to catch a framework or a bundled
    # library arriving by the back door, and the checks above are what
    # actually enforce that. Raised from 120 KB when the dashboard gained a
    # sign-in screen, an administration view and the full registry table.
    total = len(HTML) + len(CSS) + len(JS)
    # See test_dashboard_frontend.test_frontend_is_lightweight for why
    # this ceiling moves and what it is actually guarding against.
    assert total < 620_000, "the frontend has grown to %d bytes" % total

    print("PASS  the enrollment UI adds no animation and no external request "
          "(%.1f KB total)" % (total / 1024.0))


def main():
    enable_safe_console()

    tests = [
        test_the_collector_needs_stability_before_taking_a_sample,
        test_the_collector_cools_down_between_samples,
        test_a_rejected_sample_is_counted_and_not_stored,
        test_the_cli_and_the_dashboard_share_the_capture_rules,
        test_a_student_can_be_registered_without_a_camera,
        test_a_duplicate_student_id_is_refused,
        test_registration_rejects_missing_fields,
        test_enrollment_status_answers_before_anything_starts,
        test_a_full_enrollment_writes_both_stores,
        test_enrollment_reports_progress_while_it_runs,
        test_a_second_enrollment_is_refused_while_one_runs,
        test_enrollment_can_be_cancelled_and_stores_nothing,
        test_an_unavailable_camera_fails_cleanly,
        test_a_camera_that_dies_mid_enrollment_fails_safely,
        test_enrollment_refuses_an_already_enrolled_student,
        test_enrollment_refuses_while_a_live_session_holds_the_camera,
        test_enrollment_rejects_a_dangerous_student_id,
        test_an_enrolled_student_can_be_expected_in_a_session,
        test_the_recognition_gallery_can_load_the_enrollment,
        test_a_student_row_without_an_enrollment_is_reported_honestly,
        test_an_enrollment_without_a_student_row_is_not_invented,
        test_no_student_or_enrollment_response_exposes_biometric_data,
        test_the_browser_cannot_submit_an_embedding,
        test_enrollment_writes_no_image_to_disk,
        test_tests_never_touch_the_real_stores,
        test_the_students_page_has_an_add_student_control,
        test_the_dialog_explains_what_is_and_is_not_stored,
        test_the_dashboard_never_renders_or_uploads_an_image,
        test_the_student_list_refreshes_after_enrollment,
        test_the_enrollment_ui_adds_no_animation_or_external_request,
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
