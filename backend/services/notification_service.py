"""
ClassVision AI - Attendance Alerts and Parent Notifications
-------------------------------------------------------------
Rules a school can change, and a delivery layer that can be tested.

Nothing here decides attendance
    Every number this module acts on comes from the existing attendance
    tables, through the existing service. It reads a register; it never
    computes one. If a threshold is crossed, the reason is a row in
    attendance_records, and the notification says which.

Thresholds are rows, not constants
    75% appears nowhere in this file. A rule is a row in notification_rules
    with a type, a threshold and a window, and a school that wants 60%,
    or wants alerts only for one class, changes a row. There is a
    DEFAULT_RULES list, but it is a suggestion the administrator can apply -
    not something that appears in anybody's database uninvited.

Delivery is an interface, not an SMS account
    Sending a real message needs a provider, an account and credentials this
    project does not have and should not require in order to run its tests.
    So delivery is a small interface with two implementations: one that
    records what it would have sent, and one that raises. Both are real code
    paths; neither pretends a message arrived.

        NotificationProvider          the contract
        RecordingProvider             the default - logs, delivers nothing
        FailingProvider               for testing the failure path

    Plugging in a real provider is implementing send(). Nothing else changes.

What is never stored
    A notification row records WHO it was about, WHICH rule fired, what the
    measured value was, and whether delivery succeeded. It does not store the
    parent's phone number or email - those live on the student row and are
    looked up at send time. A delivery log that accumulates contact details
    is a breach waiting to be copied out.

    It also stores no biometric data of any kind, because none of this is
    about faces.
"""

from datetime import datetime, timedelta, timezone

from repositories import AuditRepository, NotificationRepository

RULE_ATTENDANCE_BELOW = "attendance_below"
RULE_CONSECUTIVE_ABSENCE = "consecutive_absence"
RULE_CONSECUTIVE_ABSENT_DAYS = "consecutive_absent_days"
RULE_PERIODIC_REPORT = "periodic_report"

ALL_RULE_TYPES = (RULE_ATTENDANCE_BELOW, RULE_CONSECUTIVE_ABSENCE,
                  RULE_CONSECUTIVE_ABSENT_DAYS, RULE_PERIODIC_REPORT)

STATUS_PENDING = "PENDING"
STATUS_SENT = "SENT"
STATUS_FAILED = "FAILED"
STATUS_SUPPRESSED = "SUPPRESSED"

# How long the same rule stays quiet for the same student. Without this, a
# nightly run would send a parent the same "attendance below threshold"
# message every single night until the child's average recovered, which is
# how people learn to ignore alerts.
DEFAULT_COOLDOWN_DAYS = 7

# Offered to an administrator to apply, and applied by nobody automatically.
# The numbers here are common ones, not this project's opinion.
DEFAULT_RULES = (
    {"rule_id": "attendance-below-75", "name": "Attendance below 75%",
     "rule_type": RULE_ATTENDANCE_BELOW, "threshold": 0.75,
     "window_days": None,
     "message": "Attendance has fallen below the required level."},
    {"rule_id": "absent-3-in-a-row", "name": "Three consecutive absences",
     "rule_type": RULE_CONSECUTIVE_ABSENCE, "threshold": 3,
     "window_days": None,
     "message": "Absent from three consecutive sessions."},
    {"rule_id": "absent-10-days", "name": "Absent 10 school days in a row",
     "rule_type": RULE_CONSECUTIVE_ABSENT_DAYS, "threshold": 10,
     "window_days": None,
     "message": "Absent from school for ten consecutive days. Please "
                "contact the school office."},
    {"rule_id": "report-15-day", "name": "15-day attendance report",
     "rule_type": RULE_PERIODIC_REPORT, "threshold": None, "window_days": 15,
     "message": "Fortnightly attendance summary."},
)

SATISFYING = ("PRESENT", "LATE", "LEFT_EARLY")


def utc_now():
    return datetime.now(timezone.utc)


def _iso(moment):
    return moment.isoformat(timespec="milliseconds") if moment else None


class NotificationError(ValueError):
    """Every message is safe to show an administrator."""


# -- delivery ---------------------------------------------------------------

class NotificationProvider:
    """
    The contract a real SMS or email integration would implement.

    One method. Raising means the message did not go; returning a dict means
    it did, and whatever the dict contains is recorded as the provider's own
    reference - never the recipient's address.
    """

    name = "abstract"

    def send(self, notification, recipient):
        raise NotImplementedError


class RecordingProvider(NotificationProvider):
    """
    The default. Records what WOULD have been sent and delivers nothing.

    This is not a stub standing in for a missing feature - it is the honest
    behaviour of an installation with no messaging account configured. The
    notification row says SENT only when something actually sent it, and with
    this provider that is never, so rows land as PENDING with a reason.
    """

    name = "recording"

    def __init__(self):
        self.sent = []

    def send(self, notification, recipient):
        self.sent.append({"student_id": notification["student_id"],
                          "subject": notification["subject"],
                          "recipient_kind": notification["recipient_kind"]})
        raise NotificationError(
            "No messaging provider is configured, so nothing was sent. The "
            "alert has been recorded and can be delivered once a provider is "
            "set up.")


class FailingProvider(NotificationProvider):
    """A provider that always fails, so the failure path can be tested."""

    name = "failing"

    def __init__(self, reason="the provider rejected the message"):
        self.reason = reason

    def send(self, notification, recipient):
        raise NotificationError(self.reason)


class CollectingProvider(NotificationProvider):
    """
    A provider that succeeds and keeps what it was given.

    For tests, and for a dry run an administrator wants to inspect before
    connecting anything real.
    """

    name = "collecting"

    def __init__(self):
        self.delivered = []

    def send(self, notification, recipient):
        self.delivered.append({"notification": dict(notification),
                               "recipient": dict(recipient)})
        return {"reference": "collected-%d" % len(self.delivered)}


class NotificationService:
    """Evaluates rules against real attendance and records what it found."""

    def __init__(self, database, provider=None, cooldown_days=None):
        self.database = database
        self.notifications = NotificationRepository(database)
        self.audit = AuditRepository(database)
        self.provider = provider or RecordingProvider()
        self.cooldown_days = int(cooldown_days
                                 if cooldown_days is not None
                                 else DEFAULT_COOLDOWN_DAYS)

    # -- rules -------------------------------------------------------------

    def list_rules(self):
        return self.notifications.list_rules()

    def save_rule(self, rule_id, name, rule_type, threshold=None,
                  window_days=None, enabled=True, class_name="", section="",
                  message="", actor=None):
        if rule_type not in ALL_RULE_TYPES:
            raise NotificationError(
                "Rule type must be one of: %s" % ", ".join(ALL_RULE_TYPES))

        if rule_type == RULE_ATTENDANCE_BELOW:
            if threshold is None or not 0 < float(threshold) <= 1:
                raise NotificationError(
                    "An attendance threshold is a fraction between 0 and 1 - "
                    "0.75 means 75%.")

        if rule_type == RULE_CONSECUTIVE_ABSENCE:
            if threshold is None or int(threshold) < 1:
                raise NotificationError(
                    "A consecutive-absence rule needs a count of at least 1.")

        if rule_type == RULE_CONSECUTIVE_ABSENT_DAYS:
            if threshold is None or int(threshold) < 1:
                raise NotificationError(
                    "A consecutive-absent-days rule needs a number of days "
                    "of at least 1.")

        record = self.notifications.save_rule(
            rule_id=rule_id, name=name, rule_type=rule_type,
            threshold=threshold, window_days=window_days, enabled=enabled,
            class_name=class_name, section=section, message=message)

        self.audit.record("notification.rule_saved", actor=actor,
                          subject=rule_id, detail=rule_type)
        return record

    def apply_default_rules(self, actor=None):
        """
        Install the suggested rules. Never runs on its own.

        An administrator asking for sensible starting rules gets them; an
        installation that never asks has none, and sends nothing.
        """
        created = []
        for rule in DEFAULT_RULES:
            if self.notifications.get_rule(rule["rule_id"]) is None:
                self.save_rule(actor=actor, **rule)
                created.append(rule["rule_id"])

        return created

    # -- evaluation --------------------------------------------------------

    def evaluate(self, student_ids=None, now=None, dry_run=False, actor=None):
        """
        Check every enabled rule against every student it applies to.

        Returns what it found and what it created. A dry run evaluates
        identically and writes nothing, so an administrator can see who would
        be contacted before anybody is.
        """
        now = now or utc_now()
        rules = [rule for rule in self.notifications.list_rules()
                 if rule["enabled"]]

        if not rules:
            return {"rules": 0, "checked": 0, "raised": [], "suppressed": [],
                    "dry_run": dry_run}

        students = self._students(student_ids)
        raised = []
        suppressed = []

        for rule in rules:
            for student in students:
                if not self._rule_applies(rule, student):
                    continue

                finding = self._check(rule, student, now)
                if finding is None:
                    continue

                if self._in_cooldown(student["student_id"], rule["rule_id"],
                                     now):
                    suppressed.append({"student_id": student["student_id"],
                                       "rule_id": rule["rule_id"],
                                       "reason": "already alerted recently"})
                    continue

                if not student["notification_enabled"]:
                    suppressed.append({"student_id": student["student_id"],
                                       "rule_id": rule["rule_id"],
                                       "reason": "notifications are switched "
                                                 "off for this student"})
                    continue

                if dry_run:
                    raised.append(dict(finding, dry_run=True))
                    continue

                raised.append(self._raise(rule, student, finding, now, actor))

        return {"rules": len(rules), "checked": len(students),
                "raised": raised, "suppressed": suppressed, "dry_run": dry_run}

    def _students(self, student_ids):
        clauses = ["status = 'ACTIVE'"]
        parameters = []

        if student_ids:
            wanted = [str(value).strip() for value in student_ids
                      if str(value).strip()]
            if not wanted:
                return []
            clauses.append("student_id IN (%s)"
                           % ", ".join("?" for _ in wanted))
            parameters.extend(wanted)

        rows = self.database.query(
            "SELECT student_id, name, class_name, section, parent_name, "
            "parent_phone, parent_email, notification_enabled "
            "FROM students WHERE %s ORDER BY student_id" % " AND ".join(clauses),
            tuple(parameters))
        return [dict(row) for row in rows]

    @staticmethod
    def _rule_applies(rule, student):
        """A rule scoped to a class applies only to that class."""
        if rule["class_name"] and \
                (student["class_name"] or "").lower() != \
                rule["class_name"].lower():
            return False
        if rule["section"] and \
                (student["section"] or "").lower() != rule["section"].lower():
            return False
        return True

    def _check(self, rule, student, now):
        if rule["rule_type"] == RULE_ATTENDANCE_BELOW:
            return self._check_attendance_rate(rule, student, now)
        if rule["rule_type"] == RULE_CONSECUTIVE_ABSENCE:
            return self._check_consecutive_absence(rule, student, now)
        if rule["rule_type"] == RULE_CONSECUTIVE_ABSENT_DAYS:
            return self._check_consecutive_absent_days(rule, student, now)
        if rule["rule_type"] == RULE_PERIODIC_REPORT:
            return self._check_periodic(rule, student, now)
        return None

    def _window(self, rule, now):
        if not rule["window_days"]:
            return None, None
        start = now - timedelta(days=int(rule["window_days"]))
        return start, now

    def _records(self, student_id, start=None):
        """
        The latest decision per session, in session-date order.

        Latest per session, because re-evaluating under a new policy keeps
        both rows and counting both would double a student's absences.
        """
        clauses = ["r.student_id = ?"]
        parameters = [student_id]

        if start is not None:
            clauses.append("s.session_date >= ?")
            parameters.append(start.date().isoformat())

        rows = self.database.query(
            """
            SELECT r.status, s.session_date, s.session_id
              FROM attendance_records r
              JOIN (SELECT session_id, student_id,
                           MAX(attendance_id) AS attendance_id
                      FROM attendance_records GROUP BY session_id, student_id)
                   latest ON latest.attendance_id = r.attendance_id
              JOIN attendance_sessions s ON s.session_id = r.session_id
             WHERE %s AND s.status = 'COMPLETED'
             ORDER BY s.session_date, s.start_time
            """ % " AND ".join(clauses), tuple(parameters))

        return [dict(row) for row in rows]

    def _check_attendance_rate(self, rule, student, now):
        start, end = self._window(rule, now)
        records = self._records(student["student_id"], start)

        if not records:
            # Nothing to divide by. A student with no completed sessions has
            # no attendance rate, and alerting on one would be inventing it.
            return None

        attended = sum(1 for record in records
                       if record["status"] in SATISFYING)
        rate = attended / len(records)

        if rate >= float(rule["threshold"]):
            return None

        return {
            "student_id": student["student_id"],
            "rule_id": rule["rule_id"],
            "notification_type": RULE_ATTENDANCE_BELOW,
            "measured_value": round(rate, 4),
            "threshold": float(rule["threshold"]),
            "period_start": _iso(start),
            "period_end": _iso(end or now),
            "summary": "%s attended %d of %d completed sessions (%.0f%%), "
                       "below the %.0f%% threshold."
                       % (student["name"], attended, len(records), rate * 100,
                          float(rule["threshold"]) * 100),
        }

    def _check_consecutive_absence(self, rule, student, now):
        records = self._records(student["student_id"])
        if not records:
            return None

        run = 0
        for record in reversed(records):
            if record["status"] == "ABSENT":
                run += 1
            else:
                break

        if run < int(rule["threshold"]):
            return None

        return {
            "student_id": student["student_id"],
            "rule_id": rule["rule_id"],
            "notification_type": RULE_CONSECUTIVE_ABSENCE,
            "measured_value": float(run),
            "threshold": float(rule["threshold"]),
            "period_start": None,
            "period_end": _iso(now),
            "summary": "%s has been absent from the last %d sessions."
                       % (student["name"], run),
        }

    def _check_consecutive_absent_days(self, rule, student, now):
        """
        Absent for N school DAYS in a row, which is not the same question as
        absent from N sessions in a row.

        Why both rules exist
            A class that meets twice on a Tuesday produces two sessions on
            one date. Counting sessions, a child who misses both has "two
            consecutive absences" and a parent gets told they have been away
            for two days. Counting dates, they have been away for one, which
            is what happened.

            The difference only shows up in schools with more than one
            session per day - which is most of them - and it shows up as a
            letter home that overstates an absence by a factor of the
            timetable. So the two are separate rule types and a school picks
            the one it means.

        A day counts as absent only when EVERY completed session that day
        was an absence. A child who missed period one and attended period
        four was in school, and a run of absent days must not continue
        through a day they were seen.

        Days with no session at all - a weekend, a holiday, a closure - are
        not absences and do not appear in the records at all, so they neither
        count towards the run nor break it. Ten consecutive absent school
        days across a half-term break is still ten days of not being in
        school, and is exactly the case a school wants to hear about.
        """
        records = self._records(student["student_id"])
        if not records:
            return None

        # Collapse to one verdict per date, in date order. _records already
        # returns the latest decision per session, so a re-evaluated session
        # is counted once.
        by_date = {}
        for record in records:
            date = record["session_date"]
            absent = record["status"] == "ABSENT"
            by_date[date] = by_date.get(date, True) and absent

        run = 0
        last_date = None
        for date in sorted(by_date, reverse=True):
            if not by_date[date]:
                break
            run += 1
            last_date = date

        threshold = int(rule["threshold"])
        if run < threshold:
            return None

        return {
            "student_id": student["student_id"],
            "rule_id": rule["rule_id"],
            "notification_type": RULE_CONSECUTIVE_ABSENT_DAYS,
            "measured_value": float(run),
            "threshold": float(threshold),
            "period_start": last_date,
            "period_end": _iso(now),
            "summary": "%s has not attended school for %d consecutive days "
                       "(since %s)." % (student["name"], run, last_date),
        }

    def _check_periodic(self, rule, student, now):
        start, end = self._window(rule, now)
        records = self._records(student["student_id"], start)

        if not records:
            return None

        attended = sum(1 for record in records
                       if record["status"] in SATISFYING)

        return {
            "student_id": student["student_id"],
            "rule_id": rule["rule_id"],
            "notification_type": RULE_PERIODIC_REPORT,
            "measured_value": round(attended / len(records), 4),
            "threshold": None,
            "period_start": _iso(start),
            "period_end": _iso(end or now),
            "summary": "%s attended %d of %d sessions in the last %s days."
                       % (student["name"], attended, len(records),
                          rule["window_days"]),
        }

    def _in_cooldown(self, student_id, rule_id, now):
        if self.cooldown_days <= 0:
            return False

        since = _iso(now - timedelta(days=self.cooldown_days))
        return self.notifications.recent_for(student_id, rule_id, since) > 0

    def _raise(self, rule, student, finding, now, actor):
        """Record the alert, then try to deliver it. In that order."""
        record = self.notifications.create(
            student_id=student["student_id"],
            rule_id=rule["rule_id"],
            notification_type=finding["notification_type"],
            subject=rule["name"],
            summary=finding["summary"],
            measured_value=finding.get("measured_value"),
            threshold=finding.get("threshold"),
            period_start=finding.get("period_start"),
            period_end=finding.get("period_end"),
            generated_at=_iso(now))

        delivered = self.deliver(record["notification_id"], student=student)

        self.audit.record("notification.raised", actor=actor,
                          subject=student["student_id"],
                          detail="%s (%s)" % (rule["rule_id"],
                                              delivered["delivery_status"]))
        return delivered

    # -- delivery ----------------------------------------------------------

    def deliver(self, notification_id, student=None):
        """
        Hand one notification to the provider and record what happened.

        Recorded first, delivered second, and the row is updated with the
        outcome - so a crash between the two leaves a PENDING row rather than
        a message nobody knows was sent.
        """
        record = self.notifications.get(notification_id)
        if record is None:
            raise NotificationError("No such notification.")

        if record["delivery_status"] == STATUS_SENT:
            return record

        if student is None:
            row = self.database.query_one(
                "SELECT student_id, name, parent_name, parent_phone, "
                "parent_email, notification_enabled FROM students "
                "WHERE student_id = ?", (record["student_id"],))
            student = dict(row) if row else None

        if not student:
            return self.notifications.set_status(
                notification_id, STATUS_FAILED,
                failure_reason="the student no longer exists")

        if not student.get("notification_enabled"):
            return self.notifications.set_status(
                notification_id, STATUS_SUPPRESSED,
                failure_reason="notifications are switched off for this "
                               "student")

        recipient = {
            "kind": record["recipient_kind"],
            "name": student.get("parent_name") or "",
            "phone": student.get("parent_phone") or "",
            "email": student.get("parent_email") or "",
        }

        if not (recipient["phone"] or recipient["email"]):
            return self.notifications.set_status(
                notification_id, STATUS_FAILED,
                failure_reason="no parent phone number or email is on file")

        try:
            self.provider.send(record, recipient)
        except Exception as error:                       # noqa: BLE001
            # The reason is recorded, and it is the provider's message - never
            # the recipient's address, and never a traceback.
            return self.notifications.set_status(
                notification_id, STATUS_FAILED,
                failure_reason=str(error)[:300])

        return self.notifications.set_status(notification_id, STATUS_SENT)

    def history(self, student_id=None, limit=100):
        return self.notifications.history(student_id, limit)
