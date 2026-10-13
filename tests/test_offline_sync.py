"""
Step 13: the offline queue, and what happens when the link comes back.

NO WEBCAM AND NO NETWORK ARE NEEDED - and a test asserts that no network
client can be reached from this module at all. Every test uses its own
temporary SQLite file; data/classvision.db is never opened.

WHAT THIS VERIFIES

    NOT LOST. Queueing is a write to the same database the attendance is in,
    so a queue entry survives everything the attendance survives. The queue is
    read back through a freshly opened connection to prove it reached the file
    rather than a buffer.

    NOT TWICE. The idempotency key is derived from what an item IS, so running
    the exporter again after a crash produces the same rows rather than a
    second delivery of a term of attendance. This is checked by exporting the
    same session twice and asserting the row count did not move, and by
    draining twice and asserting the target was handed each item once.

    NOTHING IS MARKED SENT THAT WAS NOT ACKNOWLEDGED. A target that raises
    leaves every item still queued with its attempt counted and the reason in
    words; a target that acknowledges SOME of a batch leaves the rest queued.
    A partial delivery is the normal case over a bad link, and the one that
    silently loses records if it is handled as all-or-nothing.

    NOTHING IS EVER DELETED BY FAILING. An item that has failed too many times
    becomes ABANDONED and stays in the table, readable, with its last error.
    purge_delivered() is asserted to touch nothing that is not DELIVERED.

    ORDER. Sessions are presented before the attendance that references them,
    because a receiver given a decision for a session it has never heard of
    must either reject it or invent the session.

    NO BIOMETRIC CAN BE QUEUED. The payload guard is asserted by trying to
    queue an embedding, a crop, a token and a file path, at the top level and
    nested, and checking each is refused BY NAME rather than silently stripped.

    THE DEFAULT SENDS NOTHING, AND SAYS SO. With no upstream configured the
    drain reports that nothing went and why. Nothing in this project has ever
    synchronised to a real server and the status endpoint says that in words.

WHAT THIS DOES NOT VERIFY

    That any real upstream server accepts anything. There is no real upstream.
    The three targets here are a no-upstream default and two test doubles, in
    the same shape as the notification providers, and the honest claim is that
    the queue has never delivered to a real server.

    Any attendance rule. Every record here was written through the Step 6
    repository; the queue only copies them.

Run with:
    python tests/test_offline_sync.py
"""

import ast
import json
import shutil
import sys
import tempfile
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

from backend.services import state  # noqa: E402
from backend.services.sync_service import (  # noqa: E402
    KIND_ATTENDANCE,
    KIND_PRESENCE,
    KIND_SESSION,
    MAX_ATTEMPTS,
    STATUS_ABANDONED,
    STATUS_DELIVERED,
    STATUS_FAILED,
    STATUS_PENDING,
    Accepts,
    AlwaysFails,
    NoUpstream,
    SessionExporter,
    SyncError,
    SyncService,
)
from paths import enable_safe_console  # noqa: E402

SERVICE = (PROJECT_ROOT / "backend" / "services"
           / "sync_service.py").read_text(encoding="utf-8")

BASE = datetime(2026, 3, 2, 9, 0, 0, tzinfo=timezone.utc)

ADMIN = {"username": "office", "display_name": "The Office",
         "password": "a-long-enough-passphrase"}
TEACHER_PASSWORD = "another-long-passphrase"

STUDENTS = [("Q-1", "Asha Rao", "1"), ("Q-2", "Vikram Nair", "2")]


# -- harness ----------------------------------------------------------------

def make_client():
    directory = Path(tempfile.mkdtemp(prefix="classvision_sync_"))
    state.configure(directory / "sync.db")

    from backend.app import app
    return TestClient(app), directory


def teardown(client, directory):
    try:
        if client is not None:
            client.close()
    except Exception:                                    # noqa: BLE001
        pass
    state.close_database()
    shutil.rmtree(directory, ignore_errors=True)


def make_database():
    """A database with no HTTP app around it, for the service-level tests."""
    directory = Path(tempfile.mkdtemp(prefix="classvision_syncdb_"))
    state.configure(directory / "sync.db")
    return state.get_database(), directory


def setup_school(client):
    assert client.post("/api/auth/setup", json=ADMIN).status_code == 201

    created = client.post("/api/users", json={
        "username": "tsmith", "display_name": "T Smith",
        "password": TEACHER_PASSWORD, "role": "TEACHER",
        "assignments": [{"class_name": "MECH", "section": "A"}]})
    assert created.status_code == 201, created.text

    for student_id, name, roll in STUDENTS:
        response = client.post("/api/students", json={
            "student_id": student_id, "name": name, "roll_number": roll,
            "class_name": "MECH", "section": "A"})
        assert response.status_code == 201, response.text


def register_students():
    from repositories import StudentRepository

    students = StudentRepository(state.get_database())
    for student_id, name, roll in STUDENTS:
        students.upsert(student_id=student_id, name=name, roll_number=roll,
                        class_name="MECH", section="A")


def a_session(session_id="Q-S1", day=0, with_presence=True):
    """One completed session with a decision each and some presence."""
    from attendance_rules import AttendanceDecision, PresenceSegment
    from attendance_session import AttendanceSession
    from repositories import AttendanceRepository, SessionRepository

    database = state.get_database()
    start = BASE + timedelta(days=day)

    session = AttendanceSession(session_id, "MECH-A", start,
                                start + timedelta(hours=1),
                                policy_version="1.0",
                                expected_students=[sid for sid, _, _
                                                   in STUDENTS])
    session.start()
    session.complete(completed_at=start + timedelta(hours=1))
    SessionRepository(database).save(session)

    attendance = AttendanceRepository(database)
    for student_id, status, ratio in (("Q-1", "PRESENT", 0.9),
                                      ("Q-2", "ABSENT", 0.0)):
        decision = AttendanceDecision(student_id, session_id, status,
                                      "synthetic test record", "1.0")
        decision.present_seconds = 3240.0 * ratio
        decision.session_seconds = 3600.0
        decision.presence_ratio = ratio
        attendance.save_decision(decision)

    if with_presence:
        attendance.save_segments(session_id, [
            PresenceSegment("Q-1", start, start + timedelta(minutes=50), 1)])

    return session_id


def rows(database, status=None):
    if status:
        return database.query(
            "SELECT * FROM sync_queue WHERE status = ? ORDER BY queue_id",
            (status,))
    return database.query("SELECT * FROM sync_queue ORDER BY queue_id")


# -- the queue is durable ---------------------------------------------------

def test_a_queued_item_is_in_the_file_not_in_a_buffer():
    """
    Read back through a SECOND connection to the same file.

    A queue that lived in memory would pass every other test in this suite and
    lose a lesson to a power cut, which is the exact event a classroom
    appliance has.
    """
    database, directory = make_database()
    try:
        path = Path(database.path)
        SyncService(database).enqueue(
            KIND_ATTENDANCE, {"session_id": "S", "student_id": "Q-1"},
            "S", "Q-1")

        from database import Database

        second = Database(path)
        second.connect()
        try:
            found = second.query("SELECT * FROM sync_queue")
            assert len(found) == 1, (
                "the item never reached the file - a second connection cannot "
                "see it")
            assert found[0]["status"] == STATUS_PENDING
        finally:
            second.close()

        print("PASS  a queued item is in the database file, not in a buffer")
    finally:
        teardown(None, directory)


def test_queueing_the_same_item_twice_is_one_row():
    database, directory = make_database()
    try:
        sync = SyncService(database)

        first = sync.enqueue(KIND_ATTENDANCE,
                             {"session_id": "S", "student_id": "Q-1",
                              "status": "PRESENT"}, "S", "Q-1")
        second = sync.enqueue(KIND_ATTENDANCE,
                              {"session_id": "S", "student_id": "Q-1",
                               "status": "LATE"}, "S", "Q-1")

        assert first["item_key"] == second["item_key"]
        assert len(rows(database)) == 1, (
            "the same fact was queued as two deliveries")

        # The later payload wins: a correction is a new fact about the same
        # thing, and the receiver needs it under the same key so it can
        # recognise a replacement.
        assert second["payload"]["status"] == "LATE"

        print("PASS  queueing the same item twice is one row, with the later "
              "payload")
    finally:
        teardown(None, directory)


def test_the_item_key_is_what_the_item_is_not_when_it_was_queued():
    assert SyncService.item_key(KIND_ATTENDANCE, "S", "Q-1") == \
        SyncService.item_key(KIND_ATTENDANCE, "S", "Q-1"), (
        "the key is not stable, so a retry would deliver a duplicate")

    assert SyncService.item_key(KIND_ATTENDANCE, "S", "Q-1") != \
        SyncService.item_key(KIND_ATTENDANCE, "S", "Q-2")
    assert SyncService.item_key(KIND_SESSION, "S") != \
        SyncService.item_key(KIND_ATTENDANCE, "S")

    # No timestamp anywhere in it.
    key = SyncService.item_key(KIND_SESSION, "S")
    assert "2026" not in key and ":" in key, key

    try:
        SyncService.item_key(KIND_SESSION)
        raise AssertionError("an item with no identity was given a key")
    except SyncError:
        pass

    print("PASS  the idempotency key is derived from what the item is")


def test_exporting_the_same_session_twice_queues_nothing_new():
    database, directory = make_database()
    try:
        register_students()
        session_id = a_session()

        exporter = SessionExporter(database)
        first = exporter.export_session(session_id)
        before = len(rows(database))

        second = exporter.export_session(session_id)
        after = len(rows(database))

        assert before == after, (
            "re-running the export queued %d extra item(s) - a crash recovery "
            "would double-send a term of attendance" % (after - before))
        assert first["total"] == second["total"]

        print("PASS  re-exporting a session queues nothing new")
    finally:
        teardown(None, directory)


def test_an_export_queues_a_copy_of_what_is_already_recorded():
    database, directory = make_database()
    try:
        register_students()
        session_id = a_session()

        report = SessionExporter(database).export_session(session_id)

        assert report["queued"][KIND_SESSION] == 1
        assert report["queued"][KIND_ATTENDANCE] == 2, (
            "the two decisions were not queued")
        assert report["queued"][KIND_PRESENCE] == 1, (
            "the presence summary was not queued")

        session_item = SyncService(database).get(
            SyncService.item_key(KIND_SESSION, session_id))
        payload = session_item["payload"]

        # Who was EXPECTED travels with the session: a receiver cannot tell
        # who was absent from a count.
        assert payload["expected_students"] == ["Q-1", "Q-2"], payload
        assert payload["class_id"] == "MECH-A"
        assert payload["status"] == "COMPLETED"

        print("PASS  an export queues a copy of the session, its decisions "
              "and its presence")
    finally:
        teardown(None, directory)


def test_a_payload_is_a_copy_rather_than_a_pointer():
    """
    A correction after queueing does not rewrite what is in flight.

    The queued item still says what was true when it was queued; the
    correction queues its own item under the same key when the exporter runs
    again. Silently mutating a payload in the queue would mean the record that
    left was never the record anybody read.
    """
    database, directory = make_database()
    try:
        register_students()
        session_id = a_session()

        SessionExporter(database).export_session(session_id)
        key = SyncService.item_key(KIND_ATTENDANCE, session_id, "Q-1", "1.0")
        queued_before = SyncService(database).get(key)["payload"]["status"]

        # Correct the record in the database, and do NOT re-export.
        from repositories import AttendanceRepository
        AttendanceRepository(database).apply_override(
            session_id, "Q-1", "LATE", "arrived late", actor="office",
            policy_version="1.0")

        queued_after = SyncService(database).get(key)["payload"]["status"]

        assert queued_before == queued_after == "PRESENT", (
            "the queued payload changed under the correction - it is a "
            "pointer, not a copy")

        print("PASS  a payload is a copy taken at queue time, not a pointer")
    finally:
        teardown(None, directory)


# -- nothing is marked sent that was not acknowledged -----------------------

def test_the_default_target_sends_nothing_and_says_why():
    database, directory = make_database()
    try:
        register_students()
        session_id = a_session()
        SessionExporter(database).export_session(session_id)

        sync = SyncService(database)
        assert isinstance(sync.target, NoUpstream)

        result = sync.drain()

        assert result["delivered"] == 0, (
            "something was delivered with no upstream configured")
        assert result["configured"] is False
        assert "No upstream" in result["detail"], result

        # Everything is still here.
        assert not rows(database, STATUS_DELIVERED)
        assert len(sync.pending()) == 4

        upstream = sync.status()["upstream"]
        assert upstream["configured"] is False
        assert "ever synchronised to a real server" in upstream["detail"], (
            "the status does not say that nothing has ever been synchronised")

        print("PASS  the default target sends nothing and says why")
    finally:
        teardown(None, directory)


def test_a_failing_target_leaves_every_item_queued_with_the_reason():
    database, directory = make_database()
    try:
        register_students()
        SessionExporter(database).export_session(a_session())

        sync = SyncService(database, target=AlwaysFails("the link is down"))
        result = sync.drain()

        assert result["delivered"] == 0
        assert result["failed"] == result["attempted"]

        for row in rows(database):
            assert row["status"] == STATUS_FAILED
            assert row["attempts"] == 1
            assert row["last_error"] == "the link is down", (
                "the reason was not recorded in words")
            assert row["delivered_at"] is None

        # FAILED is "not yet", not "finished": it comes back next drain.
        assert len(sync.pending()) == 4

        print("PASS  a failing target leaves every item queued, with the "
              "reason in words")
    finally:
        teardown(None, directory)


def test_a_target_that_raises_an_unexpected_error_does_not_lose_the_queue():
    class Explodes(AlwaysFails):
        def deliver(self, items):
            raise RuntimeError("something nobody anticipated")

    database, directory = make_database()
    try:
        register_students()
        SessionExporter(database).export_session(a_session())

        result = SyncService(database, target=Explodes()).drain()

        assert result["delivered"] == 0
        assert "RuntimeError" in result["detail"], result
        assert len(rows(database)) == 4, "items disappeared"
        for row in rows(database):
            assert row["status"] == STATUS_FAILED

        print("PASS  an unexpected error in a target does not lose the queue")
    finally:
        teardown(None, directory)


def test_a_partial_acknowledgement_only_marks_what_was_acknowledged():
    """
    The case that silently loses records if it is handled as all-or-nothing.
    """
    class AcceptsOne(Accepts):
        def deliver(self, items):
            self.received.extend(items[:1])
            return [items[0]["item_key"]]

    database, directory = make_database()
    try:
        register_students()
        SessionExporter(database).export_session(a_session())

        sync = SyncService(database, target=AcceptsOne())
        result = sync.drain()

        assert result["delivered"] == 1, result
        assert result["failed"] == 3, result

        delivered = rows(database, STATUS_DELIVERED)
        assert len(delivered) == 1
        assert delivered[0]["delivered_at"] is not None

        # The other three are still waiting, not lost and not "probably sent".
        assert len(sync.pending()) == 3

        print("PASS  a partial acknowledgement marks only what was "
              "acknowledged")
    finally:
        teardown(None, directory)


def test_draining_twice_does_not_present_the_same_item_twice():
    database, directory = make_database()
    try:
        register_students()
        SessionExporter(database).export_session(a_session())

        target = Accepts()
        sync = SyncService(database, target=target)

        sync.drain()
        first = len(target.received)
        sync.drain()

        assert len(target.received) == first, (
            "a second drain handed the target %d item(s) it had already "
            "accepted" % (len(target.received) - first))
        assert not sync.pending()

        keys = [item["item_key"] for item in target.received]
        assert len(keys) == len(set(keys)), "an item was delivered twice"

        print("PASS  draining twice does not present the same item twice")
    finally:
        teardown(None, directory)


def test_an_item_that_keeps_failing_is_abandoned_and_kept():
    database, directory = make_database()
    try:
        register_students()
        SessionExporter(database).export_session(a_session())

        sync = SyncService(database, target=AlwaysFails())

        for _ in range(MAX_ATTEMPTS):
            sync.drain()

        abandoned = rows(database, STATUS_ABANDONED)
        assert len(abandoned) == 4, (
            "items were not abandoned after %d attempts" % MAX_ATTEMPTS)

        for row in abandoned:
            assert row["last_error"], "an abandoned item says nothing about why"
            assert row["payload"], "an abandoned item lost its payload"

        # Nothing was deleted, and nothing is retried forever.
        assert len(rows(database)) == 4
        assert not sync.pending()

        # And it can be put back, deliberately.
        sync.retry_abandoned()
        assert len(sync.pending()) == 4
        assert not rows(database, STATUS_ABANDONED)

        print("PASS  an item that keeps failing is abandoned, kept, and can "
              "be retried deliberately")
    finally:
        teardown(None, directory)


def test_purging_touches_only_delivered_items():
    database, directory = make_database()
    try:
        register_students()
        SessionExporter(database).export_session(a_session())
        SessionExporter(database).export_session(a_session("Q-S2", day=1))

        sync = SyncService(database, target=Accepts())
        sync.drain()
        delivered = len(rows(database, STATUS_DELIVERED))
        assert delivered == 8, delivered

        # Queue one more that is NOT delivered.
        sync.enqueue(KIND_ATTENDANCE, {"session_id": "Q-S9",
                                       "student_id": "Q-1"}, "Q-S9", "Q-1")

        removed = sync.purge_delivered(keep=2)

        assert removed == 6, removed
        assert len(rows(database, STATUS_DELIVERED)) == 2
        assert len(rows(database, STATUS_PENDING)) == 1, (
            "purging removed something that had not been delivered")

        print("PASS  purging removes only delivered receipts, and only the "
              "oldest")
    finally:
        teardown(None, directory)


def test_sessions_are_presented_before_the_attendance_that_needs_them():
    database, directory = make_database()
    try:
        register_students()
        session_id = a_session()

        # Queue the attendance FIRST, so insertion order is wrong on purpose.
        sync = SyncService(database)
        sync.enqueue(KIND_ATTENDANCE, {"session_id": session_id,
                                       "student_id": "Q-1"},
                     session_id, "Q-1")
        sync.enqueue(KIND_PRESENCE, {"session_id": session_id,
                                     "student_id": "Q-1"},
                     session_id, "Q-1")
        sync.enqueue(KIND_SESSION, {"session_id": session_id},
                     session_id)

        kinds = [item["kind"] for item in sync.pending()]

        assert kinds[0] == KIND_SESSION, (
            "attendance would be sent before the session it belongs to: %s"
            % kinds)
        assert kinds.index(KIND_ATTENDANCE) < kinds.index(KIND_PRESENCE)

        print("PASS  a session is presented before the attendance that "
              "references it")
    finally:
        teardown(None, directory)


# -- nothing biometric, nothing secret --------------------------------------

def test_a_payload_carrying_a_biometric_is_refused_by_name():
    database, directory = make_database()
    try:
        sync = SyncService(database)

        for payload, expected in (
                ({"student_id": "Q-1", "embedding": [0.1, 0.2]}, "embedding"),
                ({"student_id": "Q-1", "face_crop": "..."}, "face_crop"),
                ({"student_id": "Q-1", "token_hash": "abc"}, "token_hash"),
                ({"student_id": "Q-1", "file_path": "data/x.npy"},
                 "file_path"),
                ({"student_id": "Q-1", "password": "hunter2"}, "password"),
                # Nested, and inside a list - the depth a guard usually misses.
                ({"records": [{"student_id": "Q-1",
                               "descriptor": [1, 2]}]}, "descriptor"),
                ({"outer": {"inner": {"frame": "jpeg"}}}, "frame")):
            try:
                sync.enqueue(KIND_ATTENDANCE, payload, "S", "Q-1")
                raise AssertionError(
                    "a payload carrying %s was accepted" % expected)
            except SyncError as error:
                assert expected in str(error), (
                    "the refusal does not name %s: %s" % (expected, error))

        assert not rows(database), "a refused payload still created a row"

        print("PASS  a payload carrying a biometric, a token or a path is "
              "refused by name")
    finally:
        teardown(None, directory)


def test_what_an_export_actually_queues_carries_nothing_biometric():
    database, directory = make_database()
    try:
        register_students()
        SessionExporter(database).export_session(a_session())

        for row in rows(database):
            body = row["payload"].lower()
            for word in ("embedding", "descriptor", "crop", ".npy",
                         "password", "token", "vector"):
                assert word not in body, (
                    "a queued payload contains %s: %s" % (word, row["payload"]))

        print("PASS  nothing an export queues is biometric or secret")
    finally:
        teardown(None, directory)


def test_an_unknown_kind_is_refused_rather_than_queued():
    database, directory = make_database()
    try:
        sync = SyncService(database)

        try:
            sync.enqueue("photographs", {"student_id": "Q-1"}, "Q-1")
            raise AssertionError("an unknown kind was queued")
        except SyncError as error:
            assert "carries" in str(error), error

        assert not rows(database)

        print("PASS  an unknown kind is refused rather than queued and "
              "discovered later")
    finally:
        teardown(None, directory)


def test_an_oversized_payload_is_refused_with_its_size():
    database, directory = make_database()
    try:
        sync = SyncService(database)

        try:
            sync.enqueue(KIND_ATTENDANCE,
                         {"student_id": "Q-1", "reason": "x" * 70_000},
                         "S", "Q-1")
            raise AssertionError("an oversized payload was queued")
        except SyncError as error:
            assert "bytes" in str(error), error

        print("PASS  an oversized payload is refused, with its size")
    finally:
        teardown(None, directory)


def test_the_module_imports_no_network_client():
    tree = ast.parse(SERVICE)
    imported = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    forbidden = {"requests", "httpx", "urllib", "socket", "http", "ftplib",
                 "smtplib", "paramiko", "boto3"}
    assert not (imported & forbidden), (
        "the sync module imports %s" % sorted(imported & forbidden))

    for word in ("http://", "https://", "api_key", "API_KEY"):
        assert word not in SERVICE, "the sync module mentions %s" % word

    print("PASS  the sync module imports no network client and names no URL")


# -- reporting --------------------------------------------------------------

def test_the_status_reports_the_age_of_the_oldest_waiting_item():
    database, directory = make_database()
    try:
        register_students()
        SessionExporter(database).export_session(a_session())

        sync = SyncService(database)
        status = sync.status()

        assert status["waiting"] == 4
        assert status["oldest_waiting_at"], (
            "the status does not say how long something has been waiting, "
            "which is the number that matters")
        assert status["counts"][STATUS_PENDING] == 4
        assert "nothing has been lost" in status["note"], (
            "the status does not explain that a full queue is not a loss")

        print("PASS  the status reports the age of the oldest waiting item")
    finally:
        teardown(None, directory)


def test_the_status_reports_the_last_error_in_words():
    database, directory = make_database()
    try:
        register_students()
        SessionExporter(database).export_session(a_session())

        SyncService(database, target=AlwaysFails("DNS did not resolve")).drain()
        status = SyncService(database).status()

        assert status["last_error"] == "DNS did not resolve", status
        assert status["last_error_at"], "the error has no timestamp"
        assert status["counts"][STATUS_FAILED] == 4

        print("PASS  the status reports the last error in words, with a time")
    finally:
        teardown(None, directory)


def test_an_empty_queue_is_reported_as_empty_rather_than_as_healthy():
    database, directory = make_database()
    try:
        result = SyncService(database).drain()

        assert result["attempted"] == 0
        assert result["detail"] == "Nothing is waiting."
        assert SyncService(database).status()["waiting"] == 0

        print("PASS  an empty queue says it is empty")
    finally:
        teardown(None, directory)


# -- the API ----------------------------------------------------------------

def test_the_status_endpoint_is_readable_by_a_teacher():
    """A teacher who has just finished a lesson may ask whether it has gone."""
    client, directory = make_client()
    try:
        setup_school(client)

        teacher = None
        try:
            from backend.app import app
            teacher = TestClient(app)
            signed = teacher.post("/api/auth/login", json={
                "username": "tsmith", "password": TEACHER_PASSWORD})
            assert signed.status_code == 200, signed.text

            assert teacher.get("/api/sync/status").status_code == 200

            # But operating the queue is not theirs.
            assert teacher.post("/api/sync/drain").status_code == 403
            assert teacher.get("/api/sync/pending").status_code == 403
            assert teacher.post("/api/sync/retry").status_code == 403
            assert teacher.post(
                "/api/sync/sessions/Q-S1/export").status_code == 403
        finally:
            if teacher is not None:
                teacher.close()

        print("PASS  a teacher may read the queue's state and not operate it")
    finally:
        teardown(client, directory)


def test_the_listing_does_not_return_payload_bodies():
    client, directory = make_client()
    try:
        setup_school(client)
        a_session()

        exported = client.post("/api/sync/sessions/Q-S1/export")
        assert exported.status_code == 200, exported.text

        listing = client.get("/api/sync/pending")
        assert listing.status_code == 200, listing.text
        body = listing.json()

        assert body["total"] == 4
        for item in body["items"]:
            assert "payload" not in item, (
                "the listing returns attendance bodies, which is a second way "
                "to read the register")
            assert item["kind"] and item["item_key"]

        print("PASS  the listing describes items without returning the "
              "register")
    finally:
        teardown(client, directory)


def test_exporting_a_session_that_does_not_exist_is_404():
    client, directory = make_client()
    try:
        setup_school(client)

        response = client.post("/api/sync/sessions/NOPE/export")
        assert response.status_code == 404, response.text

        print("PASS  exporting a session that does not exist is 404")
    finally:
        teardown(client, directory)


def test_the_endpoints_require_a_signed_in_account():
    client, directory = make_client()
    try:
        setup_school(client)

        from backend.app import app
        anonymous = TestClient(app)
        try:
            assert anonymous.get("/api/sync/status").status_code == 401
            assert anonymous.post("/api/sync/drain").status_code == 401
        finally:
            anonymous.close()

        print("PASS  the sync endpoints are 401 for an unauthenticated caller")
    finally:
        teardown(client, directory)


def test_the_drain_endpoint_reports_that_nothing_was_sent():
    client, directory = make_client()
    try:
        setup_school(client)
        a_session()
        client.post("/api/sync/sessions/Q-S1/export")

        drained = client.post("/api/sync/drain")
        assert drained.status_code == 200, drained.text
        body = drained.json()

        assert body["delivered"] == 0
        assert body["configured"] is False
        assert "No upstream" in body["detail"]

        # Still queued, and the status says so.
        status = client.get("/api/sync/status").json()
        assert status["waiting"] == 4

        print("PASS  the drain endpoint reports honestly that nothing was "
              "sent")
    finally:
        teardown(client, directory)


# -- the schema -------------------------------------------------------------

def test_the_queue_added_exactly_one_table():
    from schema_baseline import EXPECTED_TABLES

    database, directory = make_database()
    try:
        assert sorted(database.table_names()) == EXPECTED_TABLES, (
            "the schema no longer matches the hand-written baseline")
        assert "sync_queue" in EXPECTED_TABLES

        print("PASS  the queue added exactly one table, and it is in the "
              "baseline")
    finally:
        teardown(None, directory)


def test_the_queue_table_refuses_a_status_the_code_never_meant():
    database, directory = make_database()
    try:
        try:
            database.execute(
                "INSERT INTO sync_queue (item_key, kind, payload, status, "
                "queued_at, updated_at) VALUES "
                "('k', 'attendance', '{}', 'PROBABLY_SENT', 'now', 'now')")
            raise AssertionError("SQLite accepted an invented status")
        except AssertionError:
            raise
        except Exception:                                 # noqa: BLE001
            pass

        print("PASS  the table refuses a status the code never meant")
    finally:
        teardown(None, directory)


def test_the_queue_holds_no_blob_column():
    database, directory = make_database()
    try:
        for row in database.connection.execute(
                "PRAGMA table_info(sync_queue)").fetchall():
            assert "BLOB" not in str(row[2]).upper(), (
                "sync_queue declares a BLOB column: %s" % row[1])

        print("PASS  the queue declares no BLOB column")
    finally:
        teardown(None, directory)


def test_sync_tests_never_touch_the_real_database():
    real = PROJECT_ROOT / "data" / "classvision.db"
    before = real.stat().st_mtime if real.exists() else None

    database, directory = make_database()
    try:
        assert Path(database.path).parent != real.parent
        register_students()
        SessionExporter(database).export_session(a_session())
    finally:
        teardown(None, directory)

    after = real.stat().st_mtime if real.exists() else None
    assert after == before, "the real database was modified by a test"

    print("PASS  every test ran against a temporary database")


# -- runner -----------------------------------------------------------------

TESTS = [
    test_a_queued_item_is_in_the_file_not_in_a_buffer,
    test_queueing_the_same_item_twice_is_one_row,
    test_the_item_key_is_what_the_item_is_not_when_it_was_queued,
    test_exporting_the_same_session_twice_queues_nothing_new,
    test_an_export_queues_a_copy_of_what_is_already_recorded,
    test_a_payload_is_a_copy_rather_than_a_pointer,
    test_the_default_target_sends_nothing_and_says_why,
    test_a_failing_target_leaves_every_item_queued_with_the_reason,
    test_a_target_that_raises_an_unexpected_error_does_not_lose_the_queue,
    test_a_partial_acknowledgement_only_marks_what_was_acknowledged,
    test_draining_twice_does_not_present_the_same_item_twice,
    test_an_item_that_keeps_failing_is_abandoned_and_kept,
    test_purging_touches_only_delivered_items,
    test_sessions_are_presented_before_the_attendance_that_needs_them,
    test_a_payload_carrying_a_biometric_is_refused_by_name,
    test_what_an_export_actually_queues_carries_nothing_biometric,
    test_an_unknown_kind_is_refused_rather_than_queued,
    test_an_oversized_payload_is_refused_with_its_size,
    test_the_module_imports_no_network_client,
    test_the_status_reports_the_age_of_the_oldest_waiting_item,
    test_the_status_reports_the_last_error_in_words,
    test_an_empty_queue_is_reported_as_empty_rather_than_as_healthy,
    test_the_status_endpoint_is_readable_by_a_teacher,
    test_the_listing_does_not_return_payload_bodies,
    test_exporting_a_session_that_does_not_exist_is_404,
    test_the_endpoints_require_a_signed_in_account,
    test_the_drain_endpoint_reports_that_nothing_was_sent,
    test_the_queue_added_exactly_one_table,
    test_the_queue_table_refuses_a_status_the_code_never_meant,
    test_the_queue_holds_no_blob_column,
    test_sync_tests_never_touch_the_real_database,
]


def main():
    enable_safe_console()

    passed = 0
    for test in TESTS:
        try:
            test()
            passed += 1
        except AssertionError as error:
            print("FAIL  %s: %s" % (test.__name__, error))
        except Exception as error:                        # noqa: BLE001
            print("ERROR %s: %s: %s"
                  % (test.__name__, type(error).__name__, error))

    print("")
    print("%d/%d tests passed." % (passed, len(TESTS)))
    return 0 if passed == len(TESTS) else 1


if __name__ == "__main__":
    sys.exit(main())
