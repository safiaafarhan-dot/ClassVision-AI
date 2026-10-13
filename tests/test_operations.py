"""
Alert rules, parent notifications, the model registry and camera health.

NO WEBCAM, NO MODEL, NO NETWORK AND NO SMS ACCOUNT ARE NEEDED. Every test runs
against its own temporary SQLite file, and delivery goes through a provider
that records what it would have sent. Nothing here sends a real message.

WHAT THIS VERIFIES
    That no threshold is hard-coded and no rule exists until an administrator
    asks for one; that an alert is raised from attendance records rather than
    computed from nothing; that the same alert does not go out every night;
    that a notification row never carries a parent's phone number or email;
    that SFace stays the production recogniser until a custom model has been
    measured on a held-out set AND explicitly confirmed, while a rollback is
    always one call; and that a camera is only ever credited with numbers the
    hardware actually reported.

WHAT THIS DOES NOT VERIFY
    That an SMS or an email arrives, or that a trained model recognises
    anybody. Neither can be checked without an account and a classroom, and
    both are stated as limitations rather than faked.

Run with:
    python tests/test_operations.py
"""

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

from attendance_rules import STATUS_ABSENT, STATUS_PRESENT  # noqa: E402
from attendance_rules import AttendanceDecision  # noqa: E402
from attendance_session import AttendanceSession  # noqa: E402
from model_registry import (  # noqa: E402
    ACTIVATION_GATE,
    BASELINE_MODEL_ID,
    STATUS_ACTIVE,
    STATUS_BASELINE,
    STATUS_RETIRED,
    STATUS_TESTING,
    STATUS_VALIDATED,
    ModelRegistry,
    ModelRegistryError,
)
from repositories import (  # noqa: E402
    AttendanceRepository,
    CameraRepository,
    SessionRepository,
    StudentRepository,
)

from backend.services import state  # noqa: E402
from backend.services.notification_service import (  # noqa: E402
    DEFAULT_COOLDOWN_DAYS,
    RULE_ATTENDANCE_BELOW,
    RULE_CONSECUTIVE_ABSENCE,
    RULE_CONSECUTIVE_ABSENT_DAYS,
    RULE_PERIODIC_REPORT,
    STATUS_FAILED,
    STATUS_SENT,
    STATUS_SUPPRESSED,
    CollectingProvider,
    FailingProvider,
    NotificationService,
)
from paths import enable_safe_console  # noqa: E402

CLASS_ID = "MECH-A"

# A parent contact that would be obviously wrong to find in a delivery log.
PARENT = {"parent_name": "Guardian One", "parent_phone": "+10000000001",
          "parent_email": "guardian.one@example.invalid"}

FORBIDDEN_IN_A_NOTIFICATION = (
    "+10000000001", "guardian.one@example.invalid", "parent_phone",
    "parent_email", "embedding", "vector", ".npz", "password", "token",
)


# -- fixtures ---------------------------------------------------------------

def make_client():
    directory = Path(tempfile.mkdtemp(prefix="classvision_operations_"))
    state.configure(directory / "operations.db")

    from backend.app import app
    return TestClient(app), directory


def teardown(client, directory):
    try:
        client.close()
    except Exception:                                    # noqa: BLE001
        pass
    state.close_database()
    shutil.rmtree(directory, ignore_errors=True)


def add_student(client, student_id, name="Test Person", contact=True,
                notification_enabled=True, class_name="MECH", section="A"):
    """Register a student through the API, contact detail and all."""
    payload = {"student_id": student_id, "name": name,
               "class_name": class_name, "section": section,
               "roll_number": student_id[-3:],
               "notification_enabled": notification_enabled}
    if contact:
        payload.update(PARENT)

    response = client.post("/api/students", json=payload)
    assert response.status_code in (200, 201), response.text

    stored = StudentRepository(state.get_database()).get(student_id)
    if contact:
        assert stored["parent_phone"] == PARENT["parent_phone"], (
            "the registration endpoint dropped the parent phone number")
    assert bool(stored["notification_enabled"]) is notification_enabled

    return response.json()


def record_sessions(student_id, statuses, start=None):
    """
    Give a student a completed-session history.

    `statuses` is oldest first, one completed session each, one day apart, so
    a consecutive-absence rule has something real to count.
    """
    database = state.get_database()
    sessions = SessionRepository(database)
    attendance = AttendanceRepository(database)

    start = start or (datetime.now(timezone.utc).replace(microsecond=0)
                      - timedelta(days=len(statuses) + 1))

    for index, status in enumerate(statuses):
        opened = start + timedelta(days=index)
        session_id = "SES-%s-%02d" % (student_id, index)

        session = AttendanceSession(session_id, CLASS_ID, opened,
                                    opened + timedelta(hours=1),
                                    policy_version="1.0",
                                    expected_students=[student_id])
        session.start()
        session.complete()
        sessions.save(session)

        attendance.save_decision(AttendanceDecision(
            student_id=student_id, session_id=session_id, status=status,
            reason="fixture", policy_version="1.0",
            session_seconds=3600.0,
            presence_ratio=1.0 if status == STATUS_PRESENT else 0.0))


def service(provider=None, cooldown_days=None):
    return NotificationService(state.get_database(), provider=provider,
                               cooldown_days=cooldown_days)


# -- alert rules ------------------------------------------------------------

def test_an_installation_starts_with_no_rules_and_alerts_nobody():
    client, directory = make_client()
    try:
        assert client.get("/api/alerts/rules").json()["items"] == [], (
            "a rule appeared in the database uninvited")

        add_student(client, "STU_TEST_001")
        record_sessions("STU_TEST_001", [STATUS_ABSENT] * 10)

        result = client.post("/api/alerts/evaluate", json={}).json()
        assert result["rules"] == 0
        assert result["raised"] == [], (
            "an alert fired with no rule configured")
        assert client.get("/api/alerts/notifications").json()["items"] == []

        print("PASS  ten absences and no rule raises nothing at all")
    finally:
        teardown(client, directory)


def test_no_threshold_is_written_into_the_engine():
    """
    75% must be a row, not a constant.

    The check is on the source: a number in the engine cannot be changed by a
    school, and a school that needs 60% is the normal case, not the exception.
    """
    source = (PROJECT_ROOT / "backend" / "services"
              / "notification_service.py").read_text(encoding="utf-8")

    # DEFAULT_RULES legitimately contains 0.75 - it is the suggested rule an
    # administrator may choose to apply, and it is data. What must contain no
    # threshold is the ENGINE that evaluates rules, so the check starts there.
    marker = "class NotificationService"
    assert marker in source
    engine = source.split(marker, 1)[-1]

    # What would be wrong is a COMPARISON against a number: `rate < 0.75`.
    # The one mention of 0.75 that remains is in the message explaining what
    # the field means to whoever is typing it, which is documentation, so the
    # check is for the comparison rather than for the digits.
    import re

    # A fraction on the right of a comparison is what a threshold looks like.
    # Structural bounds - `0 < threshold <= 1` validating the field, or
    # `cooldown_days <= 0` - are not thresholds and are not matched.
    compared = re.findall(r"[<>]=?\s*-?\d*\.\d+", engine)
    assert compared == [], (
        "the evaluation engine compares against a hard-coded fraction: %s"
        % compared)

    # And the threshold it does use comes from the rule row.
    assert 'rule["threshold"]' in engine, (
        "the engine does not read the threshold from the rule")

    print("PASS  the engine compares against no literal and reads every "
          "threshold from a rule row")


def test_the_suggested_rules_are_installed_only_when_asked_for():
    client, directory = make_client()
    try:
        applied = client.post("/api/alerts/rules/defaults").json()
        assert applied["count"] == 4, applied

        rules = {rule["rule_id"]: rule
                 for rule in client.get("/api/alerts/rules").json()["items"]}
        assert set(rules) == {"attendance-below-75", "absent-3-in-a-row",
                              "absent-10-days", "report-15-day"}
        assert rules["attendance-below-75"]["threshold"] == 0.75
        assert rules["attendance-below-75"]["rule_type"] == \
            RULE_ATTENDANCE_BELOW

        # The day-based rule is a SEPARATE type from the session-based one,
        # and both are suggested. A school that meets a class twice a day
        # needs the distinction: ten absent SESSIONS is five days there, and
        # a letter home saying "ten days" would overstate the absence by a
        # factor of the timetable.
        assert rules["absent-10-days"]["rule_type"] == (
            RULE_CONSECUTIVE_ABSENT_DAYS)
        assert rules["absent-10-days"]["threshold"] == 10
        assert rules["absent-3-in-a-row"]["rule_type"] == (
            RULE_CONSECUTIVE_ABSENCE)

        # Idempotent: asking twice does not duplicate or reset anything.
        again = client.post("/api/alerts/rules/defaults").json()
        assert again["count"] == 0, again
        assert len(client.get("/api/alerts/rules").json()["items"]) == 4

        print("PASS  the 4 suggested rules install on request and only once")
    finally:
        teardown(client, directory)


def test_a_school_can_change_a_threshold_and_the_engine_follows_it():
    client, directory = make_client()
    try:
        add_student(client, "STU_TEST_001")
        # 6 of 10 attended: 60%.
        record_sessions("STU_TEST_001",
                        [STATUS_PRESENT] * 6 + [STATUS_ABSENT] * 4)

        client.put("/api/alerts/rules/below-75", json={
            "name": "Below 75%", "rule_type": RULE_ATTENDANCE_BELOW,
            "threshold": 0.75})
        raised = client.post("/api/alerts/evaluate",
                             json={"dry_run": True}).json()["raised"]
        assert len(raised) == 1, "60%% did not trip a 75%% threshold"
        assert raised[0]["measured_value"] == 0.6

        # The same student, under a school that requires 50%.
        client.put("/api/alerts/rules/below-75", json={
            "name": "Below 50%", "rule_type": RULE_ATTENDANCE_BELOW,
            "threshold": 0.5})
        raised = client.post("/api/alerts/evaluate",
                             json={"dry_run": True}).json()["raised"]
        assert raised == [], "60%% tripped a 50%% threshold"

        print("PASS  60%% attendance trips a 75%% rule and clears a 50%% one")
    finally:
        teardown(client, directory)


def test_an_unusable_rule_is_refused():
    client, directory = make_client()
    try:
        for payload, reason in (
                ({"rule_type": "guesswork"}, "rule type"),
                ({"rule_type": RULE_ATTENDANCE_BELOW}, "fraction"),
                ({"rule_type": RULE_ATTENDANCE_BELOW, "threshold": 75},
                 "fraction"),
                ({"rule_type": RULE_ATTENDANCE_BELOW, "threshold": 0},
                 "fraction"),
                ({"rule_type": RULE_CONSECUTIVE_ABSENCE, "threshold": 0},
                 "at least 1")):
            response = client.put("/api/alerts/rules/bad", json=payload)
            assert response.status_code == 400, (
                "%s was accepted: %s" % (payload, response.text))
            assert reason.split()[0].lower() in \
                response.json()["detail"].lower(), (
                "%s was refused without mentioning %r" % (payload, reason))

        assert client.get("/api/alerts/rules").json()["items"] == [], (
            "a refused rule was stored anyway")

        print("PASS  5 unusable rules refused, each with its reason, none "
              "stored")
    finally:
        teardown(client, directory)


def test_a_rule_scoped_to_a_class_applies_only_to_that_class():
    client, directory = make_client()
    try:
        add_student(client, "STU_TEST_001", class_name="MECH", section="A")
        add_student(client, "STU_TEST_002", class_name="CIVIL", section="A")
        record_sessions("STU_TEST_001", [STATUS_ABSENT] * 5)
        record_sessions("STU_TEST_002", [STATUS_ABSENT] * 5)

        client.put("/api/alerts/rules/mech-only", json={
            "name": "MECH only", "rule_type": RULE_ATTENDANCE_BELOW,
            "threshold": 0.75, "class_name": "MECH"})

        raised = client.post("/api/alerts/evaluate",
                             json={"dry_run": True}).json()["raised"]
        assert [row["student_id"] for row in raised] == ["STU_TEST_001"], (
            "a MECH-scoped rule reached another class: %s" % raised)

        print("PASS  a class-scoped rule reaches one of two absent students")
    finally:
        teardown(client, directory)


def test_a_disabled_rule_is_not_evaluated_and_a_deleted_one_is_gone():
    client, directory = make_client()
    try:
        add_student(client, "STU_TEST_001")
        record_sessions("STU_TEST_001", [STATUS_ABSENT] * 5)

        client.put("/api/alerts/rules/off", json={
            "name": "Off", "rule_type": RULE_ATTENDANCE_BELOW,
            "threshold": 0.75, "enabled": False})
        assert client.post("/api/alerts/evaluate",
                           json={"dry_run": True}).json()["raised"] == []

        removed = client.delete("/api/alerts/rules/off")
        assert removed.status_code == 200, removed.text
        assert client.get("/api/alerts/rules").json()["items"] == []
        assert client.delete("/api/alerts/rules/off").status_code == 404

        print("PASS  a disabled rule is skipped and a deleted one is a 404")
    finally:
        teardown(client, directory)


# -- what the rules measure -------------------------------------------------

def test_a_student_with_no_completed_sessions_has_no_rate_to_alert_on():
    client, directory = make_client()
    try:
        add_student(client, "STU_TEST_001")
        client.post("/api/alerts/rules/defaults")

        result = client.post("/api/alerts/evaluate",
                             json={"dry_run": True}).json()
        assert result["checked"] == 1
        assert result["raised"] == [], (
            "an attendance rate was invented for a student with no sessions")

        print("PASS  no completed sessions means no rate, not 0%")
    finally:
        teardown(client, directory)


def test_consecutive_absence_counts_the_run_at_the_end():
    client, directory = make_client()
    try:
        add_student(client, "STU_TEST_001")
        add_student(client, "STU_TEST_002")

        # Three in a row, most recent last.
        record_sessions("STU_TEST_001",
                        [STATUS_PRESENT, STATUS_ABSENT, STATUS_ABSENT,
                         STATUS_ABSENT])
        # Three absences, but not consecutive, and present most recently.
        record_sessions("STU_TEST_002",
                        [STATUS_ABSENT, STATUS_PRESENT, STATUS_ABSENT,
                         STATUS_ABSENT, STATUS_PRESENT])

        client.put("/api/alerts/rules/three-in-a-row", json={
            "name": "Three in a row", "rule_type": RULE_CONSECUTIVE_ABSENCE,
            "threshold": 3})

        raised = client.post("/api/alerts/evaluate",
                             json={"dry_run": True}).json()["raised"]
        assert [row["student_id"] for row in raised] == ["STU_TEST_001"], (
            "the run at the end was miscounted: %s" % raised)
        assert raised[0]["measured_value"] == 3.0

        print("PASS  3 absences in a row alert; 3 scattered absences do not")
    finally:
        teardown(client, directory)


def test_a_periodic_report_only_looks_inside_its_window():
    client, directory = make_client()
    try:
        add_student(client, "STU_TEST_001")

        now = datetime.now(timezone.utc).replace(microsecond=0)
        # Five old sessions, well outside a 15-day window.
        record_sessions("STU_TEST_001", [STATUS_ABSENT] * 5,
                        start=now - timedelta(days=120))

        client.put("/api/alerts/rules/fortnightly", json={
            "name": "Fortnightly", "rule_type": RULE_PERIODIC_REPORT,
            "window_days": 15})

        raised = client.post("/api/alerts/evaluate",
                             json={"dry_run": True}).json()["raised"]
        assert raised == [], (
            "a 15-day report counted sessions from four months ago")

        # And with something inside the window, it reports.
        record_sessions("STU_TEST_001", [STATUS_PRESENT, STATUS_ABSENT],
                        start=now - timedelta(days=3))
        raised = client.post("/api/alerts/evaluate",
                             json={"dry_run": True}).json()["raised"]
        assert len(raised) == 1, raised
        assert raised[0]["measured_value"] == 0.5

        print("PASS  the 15-day report ignores 4-month-old sessions and "
              "reports 1 of 2")
    finally:
        teardown(client, directory)


def test_a_dry_run_writes_nothing():
    client, directory = make_client()
    try:
        add_student(client, "STU_TEST_001")
        record_sessions("STU_TEST_001", [STATUS_ABSENT] * 5)
        client.post("/api/alerts/rules/defaults")

        result = client.post("/api/alerts/evaluate",
                             json={"dry_run": True}).json()
        assert result["dry_run"] is True
        assert result["raised"], "the dry run found nothing to report"
        assert all(row.get("dry_run") for row in result["raised"])

        assert client.get("/api/alerts/notifications").json()["items"] == [], (
            "a dry run created a notification")

        print("PASS  a dry run reports who would be contacted and stores "
              "nothing")
    finally:
        teardown(client, directory)


# -- delivery ---------------------------------------------------------------

def test_a_notification_is_recorded_then_delivered():
    client, directory = make_client()
    try:
        add_student(client, "STU_TEST_001", name="Aarav Menon")
        record_sessions("STU_TEST_001", [STATUS_ABSENT] * 5)

        provider = CollectingProvider()
        notifications = service(provider=provider)
        notifications.apply_default_rules()
        result = notifications.evaluate()

        assert result["raised"], "nothing was raised for a 0% student"
        raised = [row for row in result["raised"]
                  if row["notification_type"] == RULE_ATTENDANCE_BELOW][0]
        assert raised["delivery_status"] == STATUS_SENT
        assert raised["student_id"] == "STU_TEST_001"

        assert len(provider.delivered) == len(result["raised"]), (
            "the provider was handed a different number of messages")
        recipient = provider.delivered[0]["recipient"]
        assert recipient["phone"] == PARENT["parent_phone"], (
            "the address is looked up at send time and should reach the "
            "provider")

        print("PASS  an alert is stored, handed to the provider, and marked "
              "SENT")
    finally:
        teardown(client, directory)


def test_a_delivery_failure_is_recorded_with_the_providers_reason():
    client, directory = make_client()
    try:
        add_student(client, "STU_TEST_001")
        record_sessions("STU_TEST_001", [STATUS_ABSENT] * 5)

        notifications = service(provider=FailingProvider("the gateway is down"))
        notifications.apply_default_rules()
        result = notifications.evaluate()

        raised = result["raised"][0]
        assert raised["delivery_status"] == STATUS_FAILED
        assert raised["failure_reason"] == "the gateway is down"

        # The row survives the failure, so somebody can retry it.
        history = client.get("/api/alerts/notifications").json()["items"]
        assert len(history) == len(result["raised"])
        assert history[0]["delivery_status"] == STATUS_FAILED

        print("PASS  a failed delivery keeps the row and records the reason")
    finally:
        teardown(client, directory)


def test_a_failed_notification_can_be_retried():
    """
    Retrying is available, and with no messaging account it still fails.

    The default provider RECORDS and delivers nothing, which is the honest
    behaviour of an installation that has no SMS or email account - so the
    retry endpoint is exercised here without ever claiming a message arrived.
    A retry that succeeds is tested through a provider that can succeed, in
    test_a_notification_is_recorded_then_delivered.
    """
    client, directory = make_client()
    try:
        add_student(client, "STU_TEST_001")
        record_sessions("STU_TEST_001", [STATUS_ABSENT] * 5)

        failing = service(provider=FailingProvider("the gateway is down"))
        failing.apply_default_rules()
        raised = failing.evaluate()["raised"][0]
        assert raised["delivery_status"] == STATUS_FAILED

        retried = client.post("/api/alerts/notifications/%d/retry"
                              % raised["notification_id"])
        assert retried.status_code == 200, retried.text
        body = retried.json()
        assert body["delivery_status"] == STATUS_FAILED, (
            "the default provider delivered something - it must not claim to")
        assert "no messaging provider" in body["failure_reason"].lower(), (
            "the retry did not say why it could not deliver: %r"
            % body["failure_reason"])

        # And the same notification, delivered by a provider that can.
        collecting = service(provider=CollectingProvider())
        sent = collecting.deliver(raised["notification_id"])
        assert sent["delivery_status"] == STATUS_SENT

        # Retrying something already sent is a no-op, not a second message.
        again = client.post("/api/alerts/notifications/%d/retry"
                            % raised["notification_id"]).json()
        assert again["delivery_status"] == STATUS_SENT

        assert client.post("/api/alerts/notifications/999999/retry"
                           ).status_code == 404

        print("PASS  a retry runs, reports why it could not deliver, and a "
              "delivered one is never re-sent")
    finally:
        teardown(client, directory)


def test_a_student_with_no_contact_on_file_fails_rather_than_pretending():
    client, directory = make_client()
    try:
        add_student(client, "STU_TEST_001", contact=False)
        record_sessions("STU_TEST_001", [STATUS_ABSENT] * 5)

        provider = CollectingProvider()
        notifications = service(provider=provider)
        notifications.apply_default_rules()
        raised = notifications.evaluate()["raised"][0]

        assert raised["delivery_status"] == STATUS_FAILED
        assert "no parent phone number or email" in raised["failure_reason"]
        assert provider.delivered == [], (
            "a message was handed to the provider with nowhere to send it")

        print("PASS  no contact on file is a recorded failure, not a silent "
              "success")
    finally:
        teardown(client, directory)


def test_notifications_switched_off_for_a_student_are_suppressed():
    client, directory = make_client()
    try:
        add_student(client, "STU_TEST_001", notification_enabled=False)
        record_sessions("STU_TEST_001", [STATUS_ABSENT] * 5)

        provider = CollectingProvider()
        notifications = service(provider=provider)
        notifications.apply_default_rules()
        result = notifications.evaluate()

        assert result["raised"] == [], (
            "an alert was raised for a student whose notifications are off")
        assert result["suppressed"], "the suppression was not reported"
        assert all("switched off" in row["reason"]
                   for row in result["suppressed"])
        assert provider.delivered == []

        print("PASS  %d suppression(s) reported and nothing sent"
              % len(result["suppressed"]))
    finally:
        teardown(client, directory)


def test_the_same_alert_does_not_go_out_every_night():
    client, directory = make_client()
    try:
        add_student(client, "STU_TEST_001")
        record_sessions("STU_TEST_001", [STATUS_ABSENT] * 5)

        provider = CollectingProvider()
        notifications = service(provider=provider)
        notifications.apply_default_rules()

        first = notifications.evaluate()
        assert first["raised"], "nothing was raised on the first run"
        sent_first = len(provider.delivered)

        second = notifications.evaluate()
        assert second["raised"] == [], (
            "the same alert was raised twice in the cooldown window")
        assert second["suppressed"], "the cooldown was not reported"
        assert any("recently" in row["reason"]
                   for row in second["suppressed"])
        assert len(provider.delivered) == sent_first, (
            "a second message was sent inside the cooldown")

        # Past the cooldown, it is allowed again.
        later = notifications.evaluate(
            now=datetime.now(timezone.utc)
            + timedelta(days=DEFAULT_COOLDOWN_DAYS + 1))
        assert later["raised"], (
            "the alert never fires again, even after the cooldown")

        print("PASS  the alert fires, is suppressed for %d days, then fires "
              "again" % DEFAULT_COOLDOWN_DAYS)
    finally:
        teardown(client, directory)


def test_a_notification_row_never_carries_a_parents_address():
    client, directory = make_client()
    try:
        add_student(client, "STU_TEST_001")
        record_sessions("STU_TEST_001", [STATUS_ABSENT] * 5)

        notifications = service(provider=CollectingProvider())
        notifications.apply_default_rules()
        notifications.evaluate()

        rows = state.get_database().query("SELECT * FROM notifications")
        assert rows, "nothing was stored to check"
        stored = " ".join(str(value) for row in rows
                          for value in dict(row).values()).lower()

        history = client.get("/api/alerts/notifications").text.lower()

        for forbidden in FORBIDDEN_IN_A_NOTIFICATION:
            assert forbidden.lower() not in stored, (
                "the notifications table stores %r" % forbidden)
            assert forbidden.lower() not in history, (
                "the delivery log serves %r" % forbidden)

        print("PASS  %d notification row(s) and the API log carry no address "
              "and nothing biometric" % len(rows))
    finally:
        teardown(client, directory)


def test_raising_an_alert_is_audited():
    client, directory = make_client()
    try:
        add_student(client, "STU_TEST_001")
        record_sessions("STU_TEST_001", [STATUS_ABSENT] * 5)

        notifications = service(provider=CollectingProvider())
        notifications.apply_default_rules()
        notifications.evaluate()

        actions = [row["action"] for row in state.get_database().query(
            "SELECT action FROM audit_log")]
        assert "notification.rule_saved" in actions
        assert "notification.raised" in actions

        print("PASS  saving a rule and raising an alert are both audited")
    finally:
        teardown(client, directory)


# -- who may see a guardian's contact ---------------------------------------
#
# The contact detail is the only genuinely sensitive thing in this area, and it
# is what makes the whole notification feature work, so both halves are
# checked: that it can be written at all, and that a teacher is not handed a
# class list of parents' phone numbers.

ADMIN = {"username": "owner", "display_name": "The Owner",
         "password": "a-long-enough-passphrase"}

TEACHER = {"username": "tsmith", "display_name": "T Smith",
           "password": "another-long-passphrase", "role": "TEACHER"}


def signed_in_teacher(client):
    """Create a teacher and return a second client holding their cookie."""
    assert client.post("/api/auth/setup", json=ADMIN).status_code == 201
    created = client.post("/api/users", json=dict(TEACHER, assignments=[]))
    assert created.status_code in (200, 201), created.text

    from backend.app import app
    teacher = TestClient(app)
    response = teacher.post("/api/auth/login", json={
        "username": TEACHER["username"], "password": TEACHER["password"]})
    assert response.status_code == 200, response.text
    return teacher


def test_a_guardians_contact_can_be_recorded_and_corrected():
    client, directory = make_client()
    try:
        created = add_student(client, "STU_TEST_001")
        assert created["parent_name"] == PARENT["parent_name"]
        assert created["parent_phone"] == PARENT["parent_phone"]
        assert created["notification_enabled"] is True

        # An edit that changes only the section must not blank the number.
        edited = client.patch("/api/students/STU_TEST_001",
                              json={"section": "B"}).json()
        assert edited["section"] == "B"
        assert edited["parent_phone"] == PARENT["parent_phone"], (
            "editing a section blanked the parent's phone number")

        # And correcting the number changes only that.
        corrected = client.patch("/api/students/STU_TEST_001", json={
            "parent_phone": "+10000000002"}).json()
        assert corrected["parent_phone"] == "+10000000002"
        assert corrected["parent_name"] == PARENT["parent_name"]
        assert corrected["section"] == "B"

        print("PASS  contact is recorded, survives an unrelated edit, and can "
              "be corrected")
    finally:
        teardown(client, directory)


def test_switching_notifications_off_for_one_student_is_stored():
    client, directory = make_client()
    try:
        add_student(client, "STU_TEST_001")
        off = client.patch("/api/students/STU_TEST_001",
                           json={"notification_enabled": False}).json()
        assert off["notification_enabled"] is False

        record_sessions("STU_TEST_001", [STATUS_ABSENT] * 5)
        notifications = service(provider=CollectingProvider())
        notifications.apply_default_rules()
        result = notifications.evaluate()

        assert result["raised"] == [], (
            "the switch was stored but the engine ignored it")
        assert result["suppressed"]

        back_on = client.patch("/api/students/STU_TEST_001",
                               json={"notification_enabled": True}).json()
        assert back_on["notification_enabled"] is True

        print("PASS  the notification switch is stored, honoured, and "
              "reversible")
    finally:
        teardown(client, directory)


def test_a_teacher_is_not_handed_a_list_of_parents_phone_numbers():
    client, directory = make_client()
    try:
        add_student(client, "STU_TEST_001", name="Aarav Menon")
        teacher = signed_in_teacher(client)
        try:
            listed = teacher.get("/api/students").json()["items"][0]
            single = teacher.get("/api/students/STU_TEST_001").json()

            for record, where in ((listed, "the students list"),
                                  (single, "the single-student view")):
                assert record["parent_phone"] is None, (
                    "%s hands a teacher a parent's phone number" % where)
                assert record["parent_email"] is None, (
                    "%s hands a teacher a parent's email address" % where)
                assert record["parent_name"] is None
                # What they DO need: the student, and whether alerts go out.
                assert record["name"] == "Aarav Menon"
                assert record["notification_enabled"] is True

            body = teacher.get("/api/students").text
            assert PARENT["parent_phone"] not in body
            assert PARENT["parent_email"] not in body

            # And the administrator still sees it.
            admin_view = client.get("/api/students/STU_TEST_001").json()
            assert admin_view["parent_phone"] == PARENT["parent_phone"]
        finally:
            teacher.close()

        print("PASS  a teacher sees the student and the switch, never the "
              "number")
    finally:
        teardown(client, directory)


def test_a_teacher_cannot_write_a_guardians_contact():
    client, directory = make_client()
    try:
        add_student(client, "STU_TEST_001")
        teacher = signed_in_teacher(client)
        try:
            refused = teacher.patch("/api/students/STU_TEST_001",
                                    json={"parent_phone": "+19999999999"})
            assert refused.status_code == 403, refused.text
            assert "Super Admin" in refused.json()["detail"]

            # Refused entirely - not half-applied.
            stored = StudentRepository(state.get_database()).get(
                "STU_TEST_001")
            assert stored["parent_phone"] == PARENT["parent_phone"]

            # Registering a student is still their job; only the contact is not.
            allowed = teacher.post("/api/students", json={
                "student_id": "STU_TEST_009", "name": "Teacher Added",
                "class_name": "MECH", "section": "A"})
            assert allowed.status_code == 201, allowed.text

            with_contact = teacher.post("/api/students", json={
                "student_id": "STU_TEST_010", "name": "With Contact",
                "parent_phone": "+19999999999"})
            assert with_contact.status_code == 403, with_contact.text
            assert StudentRepository(state.get_database()).get(
                "STU_TEST_010") is None, (
                "a refused registration created the student anyway")

            # The switch is not contact detail, and a class teacher may set it.
            switched = teacher.patch("/api/students/STU_TEST_001",
                                     json={"notification_enabled": False})
            assert switched.status_code == 200, switched.text
            assert switched.json()["notification_enabled"] is False
        finally:
            teacher.close()

        print("PASS  a teacher may register students and set the switch, and "
              "may not write a number")
    finally:
        teardown(client, directory)


# -- the model registry -----------------------------------------------------

def test_sface_is_the_baseline_and_is_active_out_of_the_box():
    client, directory = make_client()
    try:
        body = client.get("/api/models").json()

        assert body["active"]["model_id"] == BASELINE_MODEL_ID
        assert body["active"]["status"] in (STATUS_BASELINE, STATUS_ACTIVE)
        assert body["active"]["kind"] == "baseline"
        assert "experimental" in body["note"].lower(), (
            "the disclaimer does not say a custom model is experimental")
        assert body["gate"]["accuracy"] == ACTIVATION_GATE["accuracy"]

        print("PASS  SFace is the active baseline and the gate is published")
    finally:
        teardown(client, directory)


def test_a_registered_experiment_lands_in_testing_and_changes_nothing():
    client, directory = make_client()
    try:
        registry = ModelRegistry(state.get_database())
        registry.ensure_baseline()

        record = registry.register_experiment(
            "custom-v1", "1.0", metrics={"accuracy": 0.99})

        assert record["status"] == STATUS_TESTING, (
            "a registered experiment should not be higher than TESTING")
        assert registry.active()["model_id"] == BASELINE_MODEL_ID, (
            "registering an experiment changed the production model")

        body = client.get("/api/models").json()
        assert body["active"]["model_id"] == BASELINE_MODEL_ID
        assert {model["model_id"] for model in body["models"]} == {
            BASELINE_MODEL_ID, "custom-v1"}

        print("PASS  an experiment is recorded as TESTING and SFace stays in "
              "production")
    finally:
        teardown(client, directory)


def test_the_baseline_cannot_be_overwritten_by_an_experiment():
    client, directory = make_client()
    try:
        registry = ModelRegistry(state.get_database())
        registry.ensure_baseline()

        try:
            registry.register_experiment(BASELINE_MODEL_ID, "2.0")
            raise AssertionError(
                "an experiment was allowed to take the baseline's id")
        except ModelRegistryError as error:
            assert "baseline" in str(error).lower()

        assert registry.get(BASELINE_MODEL_ID)["kind"] == "baseline"

        print("PASS  the baseline's id cannot be claimed by an experiment")
    finally:
        teardown(client, directory)


def test_an_unmeasured_model_cannot_be_validated():
    client, directory = make_client()
    try:
        registry = ModelRegistry(state.get_database())
        registry.ensure_baseline()
        registry.register_experiment("custom-v1", "1.0")

        refused = client.post("/api/models/custom-v1/validate", json={})
        assert refused.status_code == 400, refused.text
        detail = refused.json()["detail"]

        # Silence is a failure, and the refusal says which numbers are missing.
        for field in ("accuracy", "false_acceptance_rate", "held-out"):
            assert field in detail, (
                "the refusal does not mention %r: %s" % (field, detail))

        assert registry.get("custom-v1")["status"] == STATUS_TESTING

        print("PASS  a model that measured nothing is refused, and told what "
              "is missing")
    finally:
        teardown(client, directory)


def test_numbers_below_the_gate_are_refused_field_by_field():
    client, directory = make_client()
    try:
        registry = ModelRegistry(state.get_database())
        registry.ensure_baseline()
        registry.register_experiment("custom-v1", "1.0")

        good = {"accuracy": 0.99, "precision": 0.99, "recall": 0.99,
                "false_acceptance_rate": 0.001,
                "false_rejection_rate": 0.01,
                "test_identities": 10, "test_samples": 200, "held_out": True}

        ok, reasons = registry.evaluate_gate(good)
        assert ok, reasons

        checks = [
            ({"accuracy": 0.90}, "accuracy"),
            ({"false_acceptance_rate": 0.02}, "false_acceptance_rate"),
            ({"test_identities": 2}, "identities"),
            ({"test_samples": 10}, "samples"),
            ({"held_out": False}, "held-out"),
        ]

        for override, expected in checks:
            ok, reasons = registry.evaluate_gate(dict(good, **override))
            assert not ok, "%s cleared the gate" % override
            assert any(expected in reason for reason in reasons), (
                "%s was refused without mentioning %r: %s"
                % (override, expected, reasons))

        print("PASS  good numbers clear the gate; 5 single-field regressions "
              "do not")
    finally:
        teardown(client, directory)


def test_activation_needs_validation_and_the_id_typed_out():
    client, directory = make_client()
    try:
        registry = ModelRegistry(state.get_database())
        registry.ensure_baseline()
        registry.register_experiment("custom-v1", "1.0")

        metrics = {"accuracy": 0.99, "precision": 0.99, "recall": 0.99,
                   "false_acceptance_rate": 0.001,
                   "false_rejection_rate": 0.01,
                   "test_identities": 10, "test_samples": 200,
                   "held_out": True}

        # Not validated yet: refused whatever the caller types.
        premature = client.post("/api/models/custom-v1/activate"
                                "?confirm=custom-v1")
        assert premature.status_code == 400, premature.text
        assert "VALIDATED" in premature.json()["detail"]

        validated = client.post("/api/models/custom-v1/validate",
                                json={"metrics": metrics})
        assert validated.status_code == 200, validated.text
        assert validated.json()["status"] == STATUS_VALIDATED

        # Validated, but still in production only after a typed confirmation.
        unconfirmed = client.post("/api/models/custom-v1/activate")
        assert unconfirmed.status_code == 400, unconfirmed.text
        assert "type its model id" in unconfirmed.json()["detail"]
        assert registry.active()["model_id"] == BASELINE_MODEL_ID

        wrong = client.post("/api/models/custom-v1/activate?confirm=custom")
        assert wrong.status_code == 400, wrong.text
        assert registry.active()["model_id"] == BASELINE_MODEL_ID

        activated = client.post("/api/models/custom-v1/activate"
                                "?confirm=custom-v1")
        assert activated.status_code == 200, activated.text
        assert registry.active()["model_id"] == "custom-v1"
        assert registry.get(BASELINE_MODEL_ID)["status"] == STATUS_RETIRED, (
            "the baseline was removed rather than retired")

        print("PASS  activation needs VALIDATED plus the id typed exactly")
    finally:
        teardown(client, directory)


def test_rollback_is_one_call_and_always_available():
    client, directory = make_client()
    try:
        registry = ModelRegistry(state.get_database())
        registry.ensure_baseline()
        registry.register_experiment("custom-v1", "1.0")
        registry.validate("custom-v1", metrics={
            "accuracy": 0.99, "precision": 0.99, "recall": 0.99,
            "false_acceptance_rate": 0.001, "false_rejection_rate": 0.01,
            "test_identities": 10, "test_samples": 200, "held_out": True})
        registry.activate("custom-v1", confirm="custom-v1")
        assert registry.active()["model_id"] == "custom-v1"

        # No confirmation, no gate, no query string.
        rolled = client.post("/api/models/rollback")
        assert rolled.status_code == 200, rolled.text
        assert rolled.json()["model_id"] == BASELINE_MODEL_ID
        assert registry.active()["model_id"] == BASELINE_MODEL_ID
        assert registry.get("custom-v1")["status"] == STATUS_RETIRED

        # And the experiment is still there to be looked at afterwards.
        models = {model["model_id"] for model in
                  client.get("/api/models").json()["models"]}
        assert "custom-v1" in models

        print("PASS  rollback restores SFace with one call and keeps the "
              "experiment")
    finally:
        teardown(client, directory)


def test_exactly_one_model_is_ever_active():
    client, directory = make_client()
    try:
        registry = ModelRegistry(state.get_database())
        registry.ensure_baseline()

        metrics = {"accuracy": 0.99, "precision": 0.99, "recall": 0.99,
                   "false_acceptance_rate": 0.001,
                   "false_rejection_rate": 0.01,
                   "test_identities": 10, "test_samples": 200,
                   "held_out": True}

        for index in (1, 2, 3):
            model_id = "custom-v%d" % index
            registry.register_experiment(model_id, "1.%d" % index)
            registry.validate(model_id, metrics=metrics)
            registry.activate(model_id, confirm=model_id)

            active = [model for model in registry.list_all()
                      if model["status"] == STATUS_ACTIVE]
            assert len(active) == 1, (
                "%d models are active at once" % len(active))
            assert active[0]["model_id"] == model_id

        registry.rollback()
        active = [model for model in registry.list_all()
                  if model["status"] == STATUS_ACTIVE]
        assert len(active) == 1 and \
            active[0]["model_id"] == BASELINE_MODEL_ID

        print("PASS  one model is active after each of 3 activations and the "
              "rollback")
    finally:
        teardown(client, directory)


def test_no_model_response_carries_a_weight_or_a_path():
    client, directory = make_client()
    try:
        registry = ModelRegistry(state.get_database())
        registry.ensure_baseline()
        registry.register_experiment("custom-v1", "1.0",
                                     metrics={"accuracy": 0.99})

        body = client.get("/api/models").text.lower()
        for forbidden in ("embedding", "vector", "weights", ".npz", ".pth",
                          "c:\\", "/users/", "db_path", "file_path",
                          "password", "token"):
            assert forbidden not in body, (
                "the model listing carries %r" % forbidden)

        print("PASS  the model listing carries metrics and status, no weights "
              "and no paths")
    finally:
        teardown(client, directory)


# -- cameras ----------------------------------------------------------------

def test_a_camera_is_registered_and_listed_with_the_live_runtime():
    client, directory = make_client()
    try:
        saved = client.put("/api/cameras/CAM-01", json={
            "label": "Front of room", "classroom": "Lab 2",
            "camera_index": 0})
        assert saved.status_code == 200, saved.text
        assert saved.json()["camera_id"] == "CAM-01"

        body = client.get("/api/cameras").json()
        assert len(body["items"]) == 1
        assert body["items"][0]["classroom"] == "Lab 2"
        assert body["items"][0]["status"] == "unknown", (
            "a camera nobody has heard from should not read as online")

        # The runtime is reported alongside, and with nothing running it is
        # honest about that rather than inventing a frame rate.
        assert "live" in body
        assert body["live"]["fps"] in (None, 0, 0.0)
        assert "reported" in body["note"].lower()

        print("PASS  a registered camera reads 'unknown' until it answers")
    finally:
        teardown(client, directory)


def test_a_heartbeat_records_only_what_the_hardware_reported():
    client, directory = make_client()
    try:
        client.put("/api/cameras/CAM-01", json={"label": "Front"})

        first = client.post("/api/cameras/CAM-01/heartbeat",
                            json={"fps": 12.5, "latency_ms": 40})
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "online"
        assert first.json()["last_fps"] == 12.5
        assert first.json()["last_heartbeat"], "no heartbeat time was recorded"

        # A device that reports no frame rate must not be credited with one,
        # and must not lose the last real measurement either.
        second = client.post("/api/cameras/CAM-01/heartbeat", json={}).json()
        assert second["last_fps"] == 12.5, (
            "a heartbeat with no fps overwrote a measured value")
        assert second["last_latency_ms"] == 40

        print("PASS  a heartbeat with no fps keeps the last measured 12.5, "
              "invents nothing")
    finally:
        teardown(client, directory)


def test_an_offline_camera_records_its_error_and_recovers():
    client, directory = make_client()
    try:
        client.put("/api/cameras/CAM-01", json={"label": "Front"})
        client.post("/api/cameras/CAM-01/heartbeat", json={"fps": 12.0})

        down = client.post("/api/cameras/CAM-01/heartbeat", json={
            "status": "offline", "error": "the device stopped responding"
        }).json()
        assert down["status"] == "offline"
        assert down["last_error"] == "the device stopped responding"

        back = client.post("/api/cameras/CAM-01/heartbeat",
                           json={"fps": 11.0}).json()
        assert back["status"] == "online"
        assert back["last_error"] is None, (
            "a recovered camera still reads as failing")

        print("PASS  an offline camera records the error and clears it on "
              "recovery")
    finally:
        teardown(client, directory)


def test_a_heartbeat_for_a_camera_nobody_registered_is_a_404():
    client, directory = make_client()
    try:
        response = client.post("/api/cameras/GHOST/heartbeat",
                               json={"fps": 30})
        assert response.status_code == 404, response.text

        assert CameraRepository(state.get_database()).list_all() == [], (
            "a camera row was created by a heartbeat")

        print("PASS  a heartbeat does not conjure a camera into existence")
    finally:
        teardown(client, directory)


def test_a_camera_can_be_removed_and_removing_it_twice_is_a_404():
    client, directory = make_client()
    try:
        client.put("/api/cameras/CAM-01", json={"label": "Front"})
        assert client.delete("/api/cameras/CAM-01").status_code == 200
        assert client.get("/api/cameras").json()["items"] == []
        assert client.delete("/api/cameras/CAM-01").status_code == 404

        print("PASS  removing a camera works once and then reports 404")
    finally:
        teardown(client, directory)


def test_a_camera_row_says_nothing_about_anybody_it_has_seen():
    client, directory = make_client()
    try:
        add_student(client, "STU_TEST_001", name="Aarav Menon")
        client.put("/api/cameras/CAM-01", json={"label": "Front",
                                                "classroom": "Lab 2"})
        client.post("/api/cameras/CAM-01/heartbeat", json={"fps": 12.0})

        columns = [row[1] for row in state.get_database().connection.execute(
            "PRAGMA table_info(cameras)").fetchall()]
        for forbidden in ("student", "identity", "embedding", "face", "name"):
            assert not any(forbidden in column.lower() for column in columns
                           if column != "classroom"), (
                "the cameras table has a %r column: %s" % (forbidden, columns))

        body = client.get("/api/cameras").text
        assert "Aarav" not in body, "the camera view names a student"
        assert "STU_TEST_001" not in body

        print("PASS  the cameras table and view carry no identity at all")
    finally:
        teardown(client, directory)


# -- isolation --------------------------------------------------------------

def test_operations_tests_never_touch_the_real_database():
    real = PROJECT_ROOT / "data" / "classvision.db"
    before = real.stat().st_mtime if real.exists() else None

    client, directory = make_client()
    try:
        assert Path(state.get_database().path).parent != real.parent
        client.post("/api/alerts/rules/defaults")
        client.put("/api/cameras/CAM-01", json={"label": "Front"})
        client.get("/api/models")
    finally:
        teardown(client, directory)

    after = real.stat().st_mtime if real.exists() else None
    assert after == before, "the real database was modified by a test"

    print("PASS  every test ran against a temporary database")


# -- runner -----------------------------------------------------------------

TESTS = [
    test_an_installation_starts_with_no_rules_and_alerts_nobody,
    test_no_threshold_is_written_into_the_engine,
    test_the_suggested_rules_are_installed_only_when_asked_for,
    test_a_school_can_change_a_threshold_and_the_engine_follows_it,
    test_an_unusable_rule_is_refused,
    test_a_rule_scoped_to_a_class_applies_only_to_that_class,
    test_a_disabled_rule_is_not_evaluated_and_a_deleted_one_is_gone,
    test_a_student_with_no_completed_sessions_has_no_rate_to_alert_on,
    test_consecutive_absence_counts_the_run_at_the_end,
    test_a_periodic_report_only_looks_inside_its_window,
    test_a_dry_run_writes_nothing,
    test_a_notification_is_recorded_then_delivered,
    test_a_delivery_failure_is_recorded_with_the_providers_reason,
    test_a_failed_notification_can_be_retried,
    test_a_student_with_no_contact_on_file_fails_rather_than_pretending,
    test_notifications_switched_off_for_a_student_are_suppressed,
    test_the_same_alert_does_not_go_out_every_night,
    test_a_notification_row_never_carries_a_parents_address,
    test_raising_an_alert_is_audited,
    test_a_guardians_contact_can_be_recorded_and_corrected,
    test_switching_notifications_off_for_one_student_is_stored,
    test_a_teacher_is_not_handed_a_list_of_parents_phone_numbers,
    test_a_teacher_cannot_write_a_guardians_contact,
    test_sface_is_the_baseline_and_is_active_out_of_the_box,
    test_a_registered_experiment_lands_in_testing_and_changes_nothing,
    test_the_baseline_cannot_be_overwritten_by_an_experiment,
    test_an_unmeasured_model_cannot_be_validated,
    test_numbers_below_the_gate_are_refused_field_by_field,
    test_activation_needs_validation_and_the_id_typed_out,
    test_rollback_is_one_call_and_always_available,
    test_exactly_one_model_is_ever_active,
    test_no_model_response_carries_a_weight_or_a_path,
    test_a_camera_is_registered_and_listed_with_the_live_runtime,
    test_a_heartbeat_records_only_what_the_hardware_reported,
    test_an_offline_camera_records_its_error_and_recovers,
    test_a_heartbeat_for_a_camera_nobody_registered_is_a_404,
    test_a_camera_can_be_removed_and_removing_it_twice_is_a_404,
    test_a_camera_row_says_nothing_about_anybody_it_has_seen,
    test_operations_tests_never_touch_the_real_database,
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
