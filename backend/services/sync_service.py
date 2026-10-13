"""
ClassVision AI - Offline queue and sync (Step 13)
-------------------------------------------------
What a classroom appliance does with attendance it cannot send yet.

NOTHING HAS EVER SYNCHRONISED TO A REAL SERVER

    Stated first, because it is the most important sentence in this file and
    the easiest one to leave out. The default target is `NoUpstream`: it is
    configured with nowhere to send anything, it records that fact, and it
    delivers nothing. That is the honest behaviour of an installation that has
    not been given an upstream, and it is what every installation is until
    somebody configures one.

    The other two targets exist for testing - one always fails, one always
    succeeds - exactly as the notification providers do. No HTTP client is
    imported anywhere in this module, and a test asserts it.

WHAT THE QUEUE IS FOR

    A lesson recorded while the network is down must not be lost, and must not
    arrive twice when the network comes back. Those are two different
    problems and the table solves them in two different ways.

    NOT LOST: queueing is a write to the same SQLite file the attendance
    itself is in, inside the same transaction boundary the caller controls. If
    the attendance committed, so did its queue entry. There is no in-memory
    buffer to lose on a power cut, because a classroom appliance loses power.

    NOT TWICE: every item carries an `item_key` derived from WHAT IT IS - its
    kind plus its natural identity, such as (attendance, session, student) -
    and that column is UNIQUE. Queueing the same decision again updates the
    one row instead of adding a second. A re-run of the exporter after a crash
    therefore cannot double-send, and neither can a retry whose acknowledgement
    was lost.

WHAT A PAYLOAD IS, AND WHAT IT IS NOT

    A payload is a COPY, made when the item was queued, of a record that
    already exists in the attendance tables - the decision, the presence
    summary, the session metadata. It is not a pointer to a row that might be
    corrected afterwards, so what eventually leaves the building is what was
    true when it was queued, and a later correction queues its own item rather
    than silently rewriting history in flight.

    A payload NEVER contains a face embedding, a face crop, a frame, a file
    path, a password hash or a session token. `_check_payload` refuses them by
    key name before anything reaches the table, and a test asserts the refusal
    rather than trusting the callers.

WHY FAILURE IS LOUD

    A queue that silently stops draining is worse than no queue at all: the
    appliance looks healthy and the term of attendance never arrives. So every
    attempt is counted, the last error is kept in words, and the status
    endpoint reports the oldest pending item's age. An item that has failed
    too many times becomes ABANDONED - still in the table, still readable,
    never deleted - rather than being retried forever or thrown away.
"""

import json
from datetime import datetime, timezone

# Kinds of thing that can be queued. A closed set: an unknown kind is refused
# rather than queued and discovered to be unsendable later.
KIND_SESSION = "session"
KIND_ATTENDANCE = "attendance"
KIND_PRESENCE = "presence"

KINDS = (KIND_SESSION, KIND_ATTENDANCE, KIND_PRESENCE)

STATUS_PENDING = "PENDING"
STATUS_FAILED = "FAILED"
STATUS_DELIVERED = "DELIVERED"
STATUS_ABANDONED = "ABANDONED"

# Sessions before the attendance that references them. A receiver that is
# given a decision for a session it has never heard of has to either reject it
# or invent the session, and both are worse than ordering the queue correctly.
KIND_ORDER = {KIND_SESSION: 0, KIND_ATTENDANCE: 1, KIND_PRESENCE: 2}

# After this many refusals an item stops being retried. It is NOT deleted: it
# stays in the table as ABANDONED so somebody can see what did not go, and
# retry it deliberately once whatever was wrong is fixed.
MAX_ATTEMPTS = 8

# Key names that must never appear in a payload, at any depth.
FORBIDDEN_KEYS = (
    "embedding", "embeddings", "descriptor", "descriptors", "vector",
    "face_crop", "crop", "frame", "image", "photo", "thumbnail",
    "password", "password_hash", "token", "token_hash", "secret",
    "path", "file_path", "filename", "stored_name",
)

MAX_PAYLOAD_BYTES = 64 * 1024


class SyncError(ValueError):
    """Every message is written to be read by a person making a decision."""


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# -- where things go, if anywhere -------------------------------------------

class SyncTarget:
    """
    Somewhere to deliver to.

    An interface rather than a function so that "there is nowhere to send
    this" is a real, named implementation instead of a None check scattered
    through the drain loop.
    """

    name = "target"
    configured = False

    def describe(self):
        """What this target is, in words a person can act on."""
        raise NotImplementedError

    def deliver(self, items):
        """
        Deliver a batch. Return the item_keys that were ACCEPTED.

        Returning the accepted keys rather than a boolean is deliberate: a
        partial delivery is the normal case over a bad link, and a target that
        could only say "worked" or "did not" would force the whole batch to be
        re-sent or the whole batch to be marked delivered. Either one loses
        or duplicates.
        """
        raise NotImplementedError


class NoUpstream(SyncTarget):
    """
    The default, and the honest behaviour of an installation with no upstream.

    It accepts nothing and says why. The queue therefore fills and stays
    filled, which is correct: the attendance is safe in SQLite, and the
    dashboard reports that there is nowhere to send it rather than pretending
    it went.
    """

    name = "none"
    configured = False

    def describe(self):
        return {
            "target": self.name,
            "configured": False,
            "detail": ("No upstream server is configured, so nothing is sent "
                       "anywhere. Attendance is queued and kept. This is the "
                       "state every installation is in until an upstream is "
                       "configured, and nothing in this project has ever "
                       "synchronised to a real server."),
        }

    def deliver(self, items):
        raise SyncError("No upstream server is configured.")


class AlwaysFails(SyncTarget):
    """A target that refuses everything. For testing the failure path."""

    name = "always-fails"
    configured = True

    def __init__(self, reason="The upstream refused the batch."):
        self.reason = reason

    def describe(self):
        return {"target": self.name, "configured": True,
                "detail": "A test target that refuses every batch."}

    def deliver(self, items):
        raise SyncError(self.reason)


class Accepts(SyncTarget):
    """
    A target that accepts everything, and remembers what it was given.

    For testing the success path, and for asserting that a re-drain does not
    present the same item twice.
    """

    name = "accepts"
    configured = True

    def __init__(self):
        self.received = []

    def describe(self):
        return {"target": self.name, "configured": True,
                "detail": "A test target that accepts every batch."}

    def deliver(self, items):
        self.received.extend(items)
        return [item["item_key"] for item in items]


# -- the queue ---------------------------------------------------------------

class SyncService:
    """The outbound queue, and draining it."""

    def __init__(self, database, target=None):
        self.database = database
        self.target = target or NoUpstream()

    # -- queueing -----------------------------------------------------------

    @staticmethod
    def item_key(kind, *identity):
        """
        The idempotency key: what the item IS, never when it was queued.

        Two queue calls for the same attendance decision produce the same key
        and therefore one row. That is the whole protection against a re-run
        after a crash sending a term of attendance twice.
        """
        parts = [str(part) for part in identity if part not in (None, "")]
        if not parts:
            raise SyncError("An item needs an identity to be queued.")
        return "%s:%s" % (kind, "|".join(parts))

    @staticmethod
    def _check_payload(payload):
        """
        Refuse a payload carrying anything that must not leave the machine.

        Checked by key name at every depth, before the row is written, rather
        than trusted to the callers - there are several and there will be
        more. A refusal names the key, because a silent strip would send a
        record that looks complete and is not.
        """
        if not isinstance(payload, dict):
            raise SyncError("A payload must be an object.")

        def walk(value, path):
            if isinstance(value, dict):
                for key, item in value.items():
                    lowered = str(key).lower()
                    for forbidden in FORBIDDEN_KEYS:
                        if lowered == forbidden or lowered.endswith(
                                "_" + forbidden):
                            raise SyncError(
                                "A payload may not carry %s (at %s)."
                                % (key, path or "the top level"))
                    walk(item, "%s.%s" % (path, key) if path else str(key))
            elif isinstance(value, (list, tuple)):
                for index, item in enumerate(value):
                    walk(item, "%s[%d]" % (path, index))

        walk(payload, "")

        encoded = json.dumps(payload, sort_keys=True, default=str)
        if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise SyncError(
                "That payload is %d bytes, over the %d-byte limit. Queue it "
                "as several items rather than one."
                % (len(encoded.encode("utf-8")), MAX_PAYLOAD_BYTES))

        return encoded

    def enqueue(self, kind, payload, *identity):
        """
        Put one item in the queue, or refresh the one that is already there.

        Re-queueing an item that has already been DELIVERED does create work
        again - deliberately. A correction to an attendance decision is a new
        fact about the same thing, and the receiver needs it; the item_key
        stays the same so the receiver can recognise it as a replacement
        rather than a second record.
        """
        if kind not in KINDS:
            raise SyncError(
                "%r is not something this queue carries. It carries: %s."
                % (kind, ", ".join(KINDS)))

        encoded = self._check_payload(payload)
        key = self.item_key(kind, *identity)
        now = utc_now_iso()

        self.database.execute(
            """
            INSERT INTO sync_queue
                (item_key, kind, payload, status, attempts, last_error,
                 queued_at, updated_at, delivered_at)
            VALUES (?, ?, ?, 'PENDING', 0, NULL, ?, ?, NULL)
            ON CONFLICT(item_key) DO UPDATE SET
                payload = excluded.payload,
                status = 'PENDING',
                attempts = 0,
                last_error = NULL,
                updated_at = excluded.updated_at,
                delivered_at = NULL
            """, (key, kind, encoded, now, now))

        return self.get(key)

    def get(self, item_key):
        row = self.database.query_one(
            "SELECT * FROM sync_queue WHERE item_key = ?", (item_key,))
        return self._row(row) if row else None

    @staticmethod
    def _row(row):
        record = dict(row)
        try:
            record["payload"] = json.loads(record["payload"])
        except (TypeError, ValueError):
            # A payload that will not parse is reported as unreadable rather
            # than crashing the listing it appears in. It stays in the table.
            record["payload"] = None
            record["payload_unreadable"] = True
        return record

    # -- draining ------------------------------------------------------------

    def pending(self, limit=100):
        """
        What is waiting, in the order it must be sent.

        Sessions before attendance, then oldest first within a kind. FAILED
        items are included: a failure is "not delivered yet", not "finished".
        """
        rows = self.database.query(
            "SELECT * FROM sync_queue WHERE status IN (?, ?) "
            "ORDER BY queue_id", (STATUS_PENDING, STATUS_FAILED))

        items = [self._row(row) for row in rows]
        items.sort(key=lambda item: (KIND_ORDER.get(item["kind"], 99),
                                     item["queue_id"]))
        return items[:limit]

    def drain(self, limit=100):
        """
        Hand the waiting items to the target and record exactly what happened.

        Nothing is marked delivered that the target did not name. A target
        that raises leaves every item in the batch still queued, with its
        attempt counted and the reason recorded in words - never a bare
        exception class, because the person reading it is deciding whether to
        call somebody about a network.
        """
        items = self.pending(limit=limit)

        if not items:
            return {"attempted": 0, "delivered": 0, "failed": 0,
                    "abandoned": 0, "target": self.target.name,
                    "configured": self.target.configured,
                    "detail": "Nothing is waiting."}

        try:
            accepted = set(self.target.deliver(items) or [])
            failure = None
        except SyncError as error:
            accepted = set()
            failure = str(error)
        except Exception as error:                        # noqa: BLE001
            # A target is other people's code. It must not be able to take
            # down the drain and leave the queue in an unknown state.
            accepted = set()
            failure = "%s: %s" % (type(error).__name__, error)

        delivered = 0
        failed = 0
        abandoned = 0
        now = utc_now_iso()

        for item in items:
            if item["item_key"] in accepted:
                self.database.execute(
                    "UPDATE sync_queue SET status = ?, last_error = NULL, "
                    "updated_at = ?, delivered_at = ? WHERE item_key = ?",
                    (STATUS_DELIVERED, now, now, item["item_key"]))
                delivered += 1
                continue

            attempts = item["attempts"] + 1
            reason = failure or ("The upstream did not acknowledge this "
                                 "item.")

            if attempts >= MAX_ATTEMPTS:
                # Kept, not deleted. Somebody has to be able to see what did
                # not go, and retry it once the cause is fixed.
                self.database.execute(
                    "UPDATE sync_queue SET status = ?, attempts = ?, "
                    "last_error = ?, updated_at = ? WHERE item_key = ?",
                    (STATUS_ABANDONED, attempts, reason, now,
                     item["item_key"]))
                abandoned += 1
            else:
                self.database.execute(
                    "UPDATE sync_queue SET status = ?, attempts = ?, "
                    "last_error = ?, updated_at = ? WHERE item_key = ?",
                    (STATUS_FAILED, attempts, reason, now, item["item_key"]))
                failed += 1

        return {
            "attempted": len(items),
            "delivered": delivered,
            "failed": failed,
            "abandoned": abandoned,
            "target": self.target.name,
            "configured": self.target.configured,
            "detail": failure or "The target accepted %d of %d item(s)."
                      % (delivered, len(items)),
        }

    def retry_abandoned(self):
        """
        Put abandoned items back in the queue. Deliberate, never automatic.

        An item is abandoned because something was wrong that retrying did not
        fix. Retrying it on a timer would hide the problem; retrying it when
        somebody says so means somebody has looked.
        """
        now = utc_now_iso()
        self.database.execute(
            "UPDATE sync_queue SET status = ?, attempts = 0, updated_at = ? "
            "WHERE status = ?", (STATUS_PENDING, now, STATUS_ABANDONED))
        return self.status()

    # -- reporting -----------------------------------------------------------

    def status(self):
        """
        What is waiting, what failed, and how long the oldest has been there.

        The age of the oldest pending item is the number that matters: a queue
        of 400 items that is draining is fine, and a queue of 3 items whose
        oldest is eleven days old means nobody has noticed that the link has
        been down since term started.
        """
        counts = {status: 0 for status in
                  (STATUS_PENDING, STATUS_FAILED, STATUS_DELIVERED,
                   STATUS_ABANDONED)}

        for row in self.database.query(
                "SELECT status, COUNT(*) AS total FROM sync_queue "
                "GROUP BY status"):
            counts[row["status"]] = row["total"]

        oldest = self.database.query_one(
            "SELECT queued_at FROM sync_queue WHERE status IN (?, ?) "
            "ORDER BY queue_id LIMIT 1", (STATUS_PENDING, STATUS_FAILED))

        last_error = self.database.query_one(
            "SELECT last_error, updated_at FROM sync_queue "
            "WHERE last_error IS NOT NULL ORDER BY updated_at DESC LIMIT 1")

        waiting = counts[STATUS_PENDING] + counts[STATUS_FAILED]

        return {
            "counts": counts,
            "waiting": waiting,
            "oldest_waiting_at": oldest["queued_at"] if oldest else None,
            "last_error": last_error["last_error"] if last_error else None,
            "last_error_at": last_error["updated_at"] if last_error else None,
            "max_attempts": MAX_ATTEMPTS,
            "upstream": self.target.describe(),
            "note": ("Queued attendance is already safe in this database. "
                     "The queue is about getting a COPY of it somewhere else; "
                     "a full queue means nothing has been lost, only that "
                     "nothing has been sent."),
        }

    def purge_delivered(self, keep=1000):
        """
        Forget the oldest DELIVERED receipts once there are plenty.

        Only ever DELIVERED rows, and only the oldest beyond `keep`. Nothing
        pending, failed or abandoned is touched by this method at all - the
        queue must never be the thing that loses an attendance record.
        """
        rows = self.database.query(
            "SELECT queue_id FROM sync_queue WHERE status = ? "
            "ORDER BY queue_id DESC LIMIT -1 OFFSET ?",
            (STATUS_DELIVERED, keep))

        for row in rows:
            self.database.execute(
                "DELETE FROM sync_queue WHERE queue_id = ? AND status = ?",
                (row["queue_id"], STATUS_DELIVERED))

        return len(rows)


# -- filling the queue from what is already recorded -------------------------

class SessionExporter:
    """
    Turns one completed session into queue items.

    Reads the attendance tables and queues a copy of what it finds. It makes
    no attendance decision, applies no rule and corrects nothing: if a session
    has three records, three items are queued, and if it has none, none are.

    Run after finalisation, or run again later for a session recorded while
    the appliance was offline - the item keys make the second run a no-op
    rather than a duplicate.
    """

    def __init__(self, database, sync=None):
        self.database = database
        self.sync = sync or SyncService(database)

    def export_session(self, session_id):
        session = self.database.query_one(
            "SELECT * FROM attendance_sessions WHERE session_id = ?",
            (session_id,))

        if session is None:
            raise SyncError("No session called %s." % session_id)

        queued = {KIND_SESSION: 0, KIND_ATTENDANCE: 0, KIND_PRESENCE: 0}

        # Who was expected is its own table, and the ids go with the session
        # rather than a count: a receiver cannot tell who was absent from a
        # number, and absence is only meaningful against this list.
        expected = [row["student_id"] for row in self.database.query(
            "SELECT student_id FROM session_expected_students "
            "WHERE session_id = ? ORDER BY student_id", (session_id,))]

        self.sync.enqueue(KIND_SESSION, {
            "session_id": session["session_id"],
            "class_id": session["class_id"],
            "session_date": session["session_date"],
            "section": session["section"],
            "teacher": session["teacher"],
            "start_time": session["start_time"],
            "end_time": session["end_time"],
            "status": session["status"],
            "expected_students": expected,
            "academic_session_id": session["academic_session_id"],
            "policy_version": session["policy_version"],
            "completed_at": session["completed_at"],
        }, session["session_id"])
        queued[KIND_SESSION] += 1

        for row in self.database.query(
                "SELECT * FROM attendance_records WHERE session_id = ? "
                "ORDER BY student_id, created_at", (session_id,)):
            self.sync.enqueue(KIND_ATTENDANCE, {
                "session_id": row["session_id"],
                "student_id": row["student_id"],
                "status": row["status"],
                "presence_ratio": row["presence_ratio"],
                "present_seconds": row["present_seconds"],
                "was_late": bool(row["was_late"]),
                "left_early": bool(row["left_early"]),
                "is_override": bool(row["is_override"]),
                "original_status": row["original_status"],
                "reason": row["reason"],
                "policy_version": row["policy_version"],
                "created_at": row["created_at"],
            }, row["session_id"], row["student_id"], row["policy_version"])
            queued[KIND_ATTENDANCE] += 1

        # Presence is queued as a per-student SUMMARY, not as every segment.
        # The segments are a recording of when a camera saw somebody, and a
        # receiver that wanted them could reconstruct nothing a teacher needs
        # from them that the decision does not already say.
        for row in self.database.query(
                "SELECT student_id, COUNT(*) AS segments, "
                "       SUM(duration_seconds) AS seconds, "
                "       MIN(start_time) AS first_seen, "
                "       MAX(end_time) AS last_seen "
                "  FROM presence_segments WHERE session_id = ? "
                " GROUP BY student_id ORDER BY student_id", (session_id,)):
            self.sync.enqueue(KIND_PRESENCE, {
                "session_id": session_id,
                "student_id": row["student_id"],
                "segments": row["segments"],
                "present_seconds": row["seconds"],
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
            }, session_id, row["student_id"])
            queued[KIND_PRESENCE] += 1

        return {
            "session_id": session_id,
            "queued": queued,
            "total": sum(queued.values()),
            "note": ("A copy of what is already recorded. Nothing was "
                     "decided, recomputed or corrected by this export."),
        }
