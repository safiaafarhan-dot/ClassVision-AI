"""
ClassVision AI - Dashboard Enrollment Service
---------------------------------------------
Runs one face enrollment on behalf of the Students page.

    This adds NO recognition code. Detection is YuNet, embedding is SFace,
    the quality gate is ai/face_quality.py, the capture rules are
    ai/enrollment_rules.py and the store is ai/embedding_store.py - every one
    of them the same object the command-line tool has always used. What is
    here is the lifecycle a web request needs and a terminal does not.

The problem this closes
    The project has always had two stores keyed on the same student_id and no
    link between them:

        data/embeddings/      vectors      -> read by RECOGNITION
        data/classvision.db   student rows -> read by the DASHBOARD

    Enrolling from the command line wrote vectors and no row, so a student the
    camera could recognise perfectly well was invisible on the Students page
    and unselectable when scheduling a session. Enrollment now writes both.

One camera, one holder
    Step 8's live classroom and this share a webcam, and a webcam cannot be
    opened twice. Each refuses while the other holds it, with a message that
    says which one to stop - rather than failing at the driver with something
    a teacher cannot act on.

Privacy
    The browser sends student details and nothing else. It never uploads a
    frame, a crop or a vector, and it never receives one: the camera is read
    server-side, each frame becomes an embedding and is dropped, and the only
    thing this service publishes is a state and two counters.
"""

import threading

from embedding_store import EmbeddingStore, check_student_id
from enrollment_rules import DEFAULT_SAMPLES
from enrollment_session import (
    ENROLL_COMPLETED,
    ENROLL_IDLE,
    EnrollmentRuntime,
    EnrollmentWorker,
    SampleCollector,
)
from live_camera import CameraConfig, CameraError
from database import Database
from repositories import StudentRepository

# How long /stop or a shutdown waits for the capture thread to finish its
# current frame before giving up on a clean join.
WORKER_JOIN_TIMEOUT_SECONDS = 10.0


class EnrollmentServiceError(Exception):
    """Base class. Every message is safe to show a teacher."""


class EnrollmentConflict(EnrollmentServiceError):
    """Well-formed request, wrong state - a camera or an enrollment is busy."""


class EnrollmentInvalid(EnrollmentServiceError):
    """The request cannot describe a usable enrollment."""


class EnrollmentUnavailable(EnrollmentServiceError):
    """A resource the enrollment needs - camera or model - is not available."""


def _default_camera_factory(config):
    from live_camera import open_camera_source
    return open_camera_source(config)


def _default_components():
    """The real detector and embedder. Imported late so tests need neither."""
    from face_detector import YuNetFaceDetector
    from face_embedding import SFaceEmbedder
    return YuNetFaceDetector(), SFaceEmbedder()


def enrollment_metadata(store=None):
    """
    Enrollment facts per student, keyed by student_id.

    Reads the embedding store's METADATA only. The vectors live in a separate
    file per student and are never opened here - there is no code path from a
    dashboard request to a .npz.
    """
    try:
        store = store or EmbeddingStore()
        records = store.list_students()
    except Exception:
        # A missing or unreadable store is not an error for the Students page:
        # it means nobody is enrolled yet, which is a normal starting state.
        return {}

    metadata = {}
    for record in records:
        metadata[record.get("student_id")] = {
            "enrolled": True,
            "roll_number": record.get("roll_number"),
            "class_name": record.get("class_name"),
            "section": record.get("section"),
            "sample_count": int(record.get("sample_count") or 0),
            "model_name": record.get("model_name"),
            "model_version": record.get("model_version"),
            "enrolled_at": record.get("created_at"),
        }

    return metadata


class EnrollmentManager:
    """
    The one enrollment this process may run.

    Shaped like Step 8's LiveSessionManager on purpose - same lock discipline,
    same injectable factories, same "the worker owns the camera" rule - so
    there is one way camera work behaves in this project rather than two.
    """

    def __init__(self, camera_factory=None, component_factory=None):
        self._lock = threading.RLock()
        self.runtime = EnrollmentRuntime()

        self.camera_factory = camera_factory or _default_camera_factory
        self.component_factory = component_factory or _default_components

        self._worker = None
        self._camera = None
        self._stop_event = None
        self._summary = None

    # -- start -------------------------------------------------------------

    def start(self, database, student_id, name, roll_number="", class_name="",
              section="", samples=DEFAULT_SAMPLES, camera_index=None,
              replace=False, store=None):
        """
        Begin a camera enrollment, or refuse with a reason.

        The camera is opened on THIS thread, before the worker starts, so a
        missing or busy camera is an immediate, clear error rather than an
        enrollment that appears to begin and dies a moment later.
        """
        with self._lock:
            if self.runtime.is_busy:
                raise EnrollmentConflict(
                    "An enrollment is already running for %s. Wait for it to "
                    "finish, or cancel it."
                    % (self.runtime.student_id or "another student"))

            self._refuse_if_camera_is_in_a_lesson()

            details = self._validated_details(student_id, name, roll_number,
                                              class_name, section)

            store = store or EmbeddingStore()
            if store.has_student(details["student_id"]) and not replace:
                raise EnrollmentConflict(
                    "%s is already enrolled. Choose a different student id, "
                    "or re-enrol to replace the existing face samples."
                    % details["student_id"])

            try:
                samples = int(samples)
            except (TypeError, ValueError):
                raise EnrollmentInvalid("The sample count must be a number.")

            if not 1 <= samples <= 20:
                raise EnrollmentInvalid(
                    "The sample count must be between 1 and 20.")

            try:
                config = CameraConfig.from_environment(index=camera_index)
            except ValueError as error:
                raise EnrollmentInvalid(str(error))

            self.runtime.begin(details["student_id"], details["name"], samples,
                               config.index)
            self._summary = None

            try:
                detector, embedder = self.component_factory()
            except Exception as error:                   # noqa: BLE001
                self.runtime.fail(
                    "The face models could not be loaded (%s). Run "
                    "python ai/download_models.py and try again."
                    % type(error).__name__)
                raise EnrollmentUnavailable(self.runtime.error)

            try:
                self._camera = self.camera_factory(config)
            except CameraError as error:
                self.runtime.fail(str(error))
                raise EnrollmentUnavailable(str(error))
            except Exception as error:                   # noqa: BLE001
                self.runtime.fail(
                    "The camera could not be opened (%s)."
                    % type(error).__name__)
                raise EnrollmentUnavailable(self.runtime.error)

            collector = SampleCollector(detector, embedder, samples)
            self._stop_event = threading.Event()
            self._worker = EnrollmentWorker(
                self.runtime, self._camera, collector, details, embedder,
                store=store,
                registry_sync=self._registry_sync(database),
                stop_event=self._stop_event)

            self._worker.start()
            return self.status()

    def _refuse_if_camera_is_in_a_lesson(self):
        """
        One webcam cannot serve two masters.

        Checked here rather than left to the driver, so the teacher is told
        which thing to stop instead of seeing an open failure they cannot act
        on.
        """
        try:
            from backend.services.live_service import get_live_manager
            live = get_live_manager()
        except Exception:                                # noqa: BLE001
            return

        if live.runtime.is_busy:
            raise EnrollmentConflict(
                "A live classroom session is using the camera. Stop it from "
                "the Live classroom tab before enrolling a student.")

    @staticmethod
    def _validated_details(student_id, name, roll_number, class_name, section):
        student_id = (student_id or "").strip()
        name = (name or "").strip()

        if not student_id:
            raise EnrollmentInvalid("A student id is required.")
        if not name:
            raise EnrollmentInvalid("A student name is required.")

        # The store's own rule, reused rather than restated: it refuses path
        # separators and anything else that could escape the embeddings
        # directory, and that check must not have two versions.
        try:
            check_student_id(student_id)
        except Exception as error:                       # noqa: BLE001
            raise EnrollmentInvalid(
                "That student id cannot be used: %s" % error)

        if len(name) > 120:
            raise EnrollmentInvalid("The name is too long (limit 120).")

        return {
            "student_id": student_id,
            "name": name,
            "roll_number": (roll_number or "").strip(),
            "class_name": (class_name or "").strip(),
            "section": (section or "").strip(),
        }

    @staticmethod
    def _registry_sync(database):
        """
        Write the FULL student row once the vectors are safely stored.

        All five fields, not two. The registry is the source of truth for who
        a student is, so an enrollment that knows their roll number, class and
        section must put them where a plain SELECT can find them - otherwise
        those three facts exist only in the embedding store, which is the
        split this whole piece of work exists to close.

        Status is deliberately not passed, so upsert leaves it alone: a
        student deactivated last term must not come back ACTIVE because
        somebody re-ran the camera.

        This runs on the CAPTURE THREAD, and SQLite connections are bound to
        the thread that created them - handing the request thread's connection
        across would raise ProgrammingError on every enrollment, silently, and
        leave a student enrolled for recognition but missing from the
        dashboard. Which is the precise bug this feature exists to fix, so it
        would have been an unfortunate way to reintroduce it.

        The worker therefore opens its OWN connection to the same file, which
        is the one-connection-per-thread rule Step 7 already applies to the
        request threadpool.
        """
        path = getattr(database, "path", None)

        def sync(details):
            connection = None
            try:
                connection = Database(path)
                connection.connect()
                with connection.transaction():
                    StudentRepository(connection).upsert(
                        details["student_id"], details["name"],
                        roll_number=details.get("roll_number", ""),
                        class_name=details.get("class_name", ""),
                        section=details.get("section", ""))
                return True
            except Exception:                            # noqa: BLE001
                # Best effort: the vectors are already saved and recognition
                # works. The caller reports that the row did not land rather
                # than discarding a capture the student just sat through.
                return False
            finally:
                if connection is not None:
                    connection.close()

        return sync

    # -- status and cancel -------------------------------------------------

    def status(self):
        """Safe progress. Never a frame, a crop or a vector."""
        snapshot = self.runtime.snapshot()
        snapshot["worker_alive"] = bool(self._worker and self._worker.is_alive())

        if self._worker is not None and self._worker.result:
            snapshot["samples_stored"] = self._worker.result["samples"]
            snapshot["registry_synced"] = self._worker.result["registry_synced"]
        else:
            snapshot["samples_stored"] = 0
            snapshot["registry_synced"] = False

        return snapshot

    def cancel(self):
        """
        Stop a running enrollment. Safe to call when nothing is running.

        A cancelled capture stores nothing: the vectors are only written once
        every sample has been taken.
        """
        with self._lock:
            if not self.runtime.is_busy:
                return self.status()

            if self._stop_event is not None:
                self._stop_event.set()

            worker = self._worker
            if worker is not None and worker.is_alive():
                worker.join(timeout=WORKER_JOIN_TIMEOUT_SECONDS)

            self._release_camera()

            if self.runtime.is_busy:
                self.runtime.cancel()

            return self.status()

    def _release_camera(self):
        if self._camera is not None:
            self._camera.release()
            self._camera = None

    def shutdown(self):
        """Give the camera back on application shutdown."""
        with self._lock:
            if self._stop_event is not None:
                self._stop_event.set()

            worker = self._worker
            if worker is not None and worker.is_alive():
                worker.join(timeout=WORKER_JOIN_TIMEOUT_SECONDS)

            self._release_camera()
            self._worker = None
            self._stop_event = None

    def reset(self):
        """Return to IDLE, releasing anything held. Used between tests."""
        with self._lock:
            self.shutdown()
            self.runtime.reset()
            self._summary = None

    @property
    def is_busy(self):
        return self.runtime.is_busy


# -- the single instance the API uses --------------------------------------

_manager = None
_manager_lock = threading.Lock()


def get_enrollment_manager():
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = EnrollmentManager()
        return _manager


def reset_enrollment_manager(manager=None):
    """Replace the process-wide manager. For tests and shutdown."""
    global _manager
    with _manager_lock:
        if _manager is not None:
            _manager.reset()
        _manager = manager
        return _manager
