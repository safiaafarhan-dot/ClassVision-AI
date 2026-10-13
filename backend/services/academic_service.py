"""
ClassVision AI - Academic Sessions, History and Promotion
----------------------------------------------------------
A student is permanent. Where they sit is not.

The distinction this module exists for

    STU001 is Safia Farhan, and will be for as long as she is at the
    institution. Her roll number, class, section and semester are facts about
    a PERIOD, not about her, and they change every term.

    Before this, the registry held only the current values, so promoting a
    student overwrote where they used to be. Attendance from last year then
    appeared to belong to this year's section, which is wrong in a way that
    is hard to notice and impossible to undo.

    So:

        students                     who they are      - permanent
        student_academic_history     where they were   - one row per term
        academic_sessions            the terms         - 2026-27 sem 6, ...

    The registry keeps the CURRENT class, section and roll as a mirror of the
    is_current history row. That is not duplication for its own sake: every
    existing query, the Students page, the session roster and the recognition
    gallery all read the student row, and making them all join a history table
    to answer "what section is this student in" would be a rewrite with no
    benefit. promote() moves both together, in one transaction, so they cannot
    disagree.

What promotion must never do
    Overwrite a previous record. A promotion CREATES a row and demotes the old
    one to history. Nothing is updated in place, nothing is deleted, and the
    attendance attached to the old term keeps pointing at the sessions it was
    always attached to.

What promotion must never guess
    Roll numbers and sections. A system that invents "roll 21 becomes roll 21
    in the new section" will be wrong for most of a class, and the wrongness
    lands on a register. Allocation is supplied by the administrator, per
    student, or imported; where it is not supplied, the previous value is
    CARRIED FORWARD unchanged, and the report says so.
"""

from datetime import datetime, timezone

from repositories import (
    STUDENT_ACTIVE,
    AcademicRepository,
    AuditRepository,
    StudentRepository,
)

# A student in one of these has left. They keep every record they earned, and
# they are not promoted into a new term by a bulk operation.
PROMOTABLE_STATUSES = (STUDENT_ACTIVE,)

ACADEMIC_PLANNED = "PLANNED"
ACADEMIC_ACTIVE = "ACTIVE"
ACADEMIC_CLOSED = "CLOSED"


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class AcademicError(ValueError):
    """Every message is safe to show a teacher."""


class AcademicConflict(AcademicError):
    """Well formed, but it collides with something that already exists."""


def _clean(value, limit=120):
    return ("" if value is None else str(value).strip())[:limit]


def session_identifier(academic_year, semester):
    """
    A readable, sortable id in the shape the rest of the project uses.

    "2026-27" + "6" becomes "AY-2026-27-SEM-6". Built from the two values
    rather than a counter so the same year and semester always produce the
    same id, which is what makes creating one twice a conflict rather than a
    duplicate.
    """
    year = "".join(character if character.isalnum() else "-"
                   for character in _clean(academic_year).upper()).strip("-")
    while "--" in year:
        year = year.replace("--", "-")

    term = "".join(character for character in _clean(semester).upper()
                   if character.isalnum())

    return "AY-%s%s" % (year or "UNSET", ("-SEM-%s" % term) if term else "")


class AcademicService:
    """Academic years, the terms inside them, and moving students between."""

    def __init__(self, database):
        self.database = database
        self.academic = AcademicRepository(database)
        self.students = StudentRepository(database)
        self.audit = AuditRepository(database)

    # -- academic sessions -------------------------------------------------

    def create_session(self, academic_year, semester="", start_date=None,
                       end_date=None, status=ACADEMIC_PLANNED, actor=None):
        """
        Add a term. Does not touch a single student.

        Creating 2027-28 while 2026-27 is running is normal and safe: the new
        term sits PLANNED until somebody activates it, and promotion is a
        separate, deliberate step.
        """
        academic_year = _clean(academic_year, 60)
        semester = _clean(semester, 40)

        if not academic_year:
            raise AcademicError("An academic year is required.")

        if status not in (ACADEMIC_PLANNED, ACADEMIC_ACTIVE, ACADEMIC_CLOSED):
            raise AcademicError(
                "Status must be PLANNED, ACTIVE or CLOSED.")

        session_id = session_identifier(academic_year, semester)

        if self.academic.get_session(session_id) is not None:
            raise AcademicConflict(
                "%s semester %s already exists."
                % (academic_year, semester or "-"))

        with self.database.transaction():
            record = self.academic.create_session(
                session_id, academic_year, semester, start_date, end_date,
                status)

            if status == ACADEMIC_ACTIVE:
                self.academic.deactivate_other_sessions(session_id)

        self.audit.record("academic.session_create", actor=actor,
                          subject=session_id,
                          detail="%s %s" % (academic_year, semester))
        return record

    def list_sessions(self):
        return self.academic.list_sessions()

    def current_session(self):
        """
        The term the institution is in, or None.

        None is a real answer on an installation that has not set one up, and
        every caller treats it as "no academic context" rather than guessing.
        """
        return self.academic.current_session()

    def activate_session(self, academic_session_id, actor=None):
        """
        Make one term current. Exactly one is, and the others are not.

        Closing the previous one rather than deleting it is the whole point:
        last year's enrolments and attendance stay exactly where they were.
        """
        record = self.academic.get_session(academic_session_id)
        if record is None:
            raise AcademicError("No such academic session.")

        with self.database.transaction():
            self.academic.set_session_status(academic_session_id,
                                             ACADEMIC_ACTIVE)
            self.academic.deactivate_other_sessions(academic_session_id)

        self.audit.record("academic.session_activate", actor=actor,
                          subject=academic_session_id)
        return self.academic.get_session(academic_session_id)

    # -- a student's academic history --------------------------------------

    def enrol(self, student_id, academic_session_id=None, class_name=None,
              section=None, roll_number=None, semester=None,
              academic_year=None, start_date=None, make_current=True,
              actor=None):
        """
        Record where a student is for one term.

        Called when a student is registered, and again by promote() for each
        new term. Enrolling into a term the student is already in UPDATES that
        row - there is a UNIQUE constraint saying so - because a student is in
        a section once, not twice.
        """
        if self.students.get(student_id) is None:
            raise AcademicError("No student with the id %s." % student_id)

        session = (self.academic.get_session(academic_session_id)
                   if academic_session_id else self.current_session())

        record = self.academic.upsert_enrolment(
            student_id=student_id,
            academic_session_id=(session or {}).get("academic_session_id"),
            academic_year=_clean(academic_year
                                 or (session or {}).get("academic_year"), 60),
            semester=_clean(semester or (session or {}).get("semester"), 40),
            class_name=_clean(class_name, 120),
            section=_clean(section, 40),
            roll_number=_clean(roll_number, 40),
            start_date=start_date,
            is_current=bool(make_current))

        if make_current:
            with self.database.transaction():
                self.academic.mark_only_current(
                    student_id, record["academic_history_id"])
                # The registry mirrors the current term, so every existing
                # query keeps working without learning about history.
                # Four mirrored columns now, not three. academic_year was
                # the one the registry could not filter on because it was
                # the one that was never copied here.
                self.students.update(student_id,
                                     class_name=record["class_name"],
                                     section=record["section"],
                                     roll_number=record["roll_number"],
                                     academic_year=record["academic_year"])

        return record

    def history(self, student_id):
        """Every term this student has been enrolled in, newest first."""
        return self.academic.history(student_id)

    def current_enrolment(self, student_id):
        return self.academic.current_enrolment(student_id)

    # -- promotion ---------------------------------------------------------

    def preview_promotion(self, academic_session_id, student_ids=None,
                          class_name=None, section=None,
                          allocations=None):
        """
        What a promotion WOULD do, without doing any of it.

        The dashboard shows this as a confirmation summary, because a bulk
        operation across forty students is exactly the kind of thing that
        should be read before it is run.
        """
        session = self.academic.get_session(academic_session_id)
        if session is None:
            raise AcademicError("No such academic session.")

        allocations = {key: value for key, value in (allocations or {}).items()}
        rows = []

        for student in self._promotion_candidates(student_ids, class_name,
                                                  section):
            student_id = student["student_id"]
            allocation = allocations.get(student_id, {})
            existing = self.academic.enrolment_for_session(
                student_id, academic_session_id)

            target = {
                "class_name": allocation.get("class_name",
                                             student["class_name"]),
                "section": allocation.get("section", student["section"]),
                "roll_number": allocation.get("roll_number",
                                              student["roll_number"]),
            }

            rows.append({
                "student_id": student_id,
                "name": student["name"],
                "from": {"class_name": student["class_name"],
                         "section": student["section"],
                         "roll_number": student["roll_number"]},
                "to": target,
                # Said out loud rather than hidden: where the administrator
                # supplied nothing, the previous value is carried forward.
                # Nothing is invented.
                "carried_forward": sorted(
                    field for field in ("class_name", "section", "roll_number")
                    if field not in allocation),
                "already_enrolled": existing is not None,
            })

        return {
            "academic_session": session,
            "students": rows,
            "total": len(rows),
            "already_enrolled": sum(1 for row in rows
                                    if row["already_enrolled"]),
        }

    def promote(self, academic_session_id, student_ids=None, class_name=None,
                section=None, allocations=None, actor=None):
        """
        Move students into a term. Creates history; destroys none.

        Every student gets a NEW row in student_academic_history for the
        target term, and their previous row stops being is_current. The
        previous row is otherwise untouched - same class, same section, same
        roll - so the attendance recorded against it still describes the term
        it happened in.
        """
        preview = self.preview_promotion(academic_session_id, student_ids,
                                         class_name, section, allocations)

        if not preview["students"]:
            raise AcademicError(
                "No eligible students matched. Only ACTIVE students are "
                "promoted; transferred and graduated students are left alone.")

        session = preview["academic_session"]
        promoted = []

        with self.database.transaction():
            for row in preview["students"]:
                record = self.academic.upsert_enrolment(
                    student_id=row["student_id"],
                    academic_session_id=academic_session_id,
                    academic_year=session["academic_year"],
                    semester=session["semester"],
                    class_name=row["to"]["class_name"],
                    section=row["to"]["section"],
                    roll_number=row["to"]["roll_number"],
                    start_date=session.get("start_date"),
                    is_current=True)

                self.academic.mark_only_current(
                    row["student_id"], record["academic_history_id"])

                self.students.update(row["student_id"],
                                     class_name=row["to"]["class_name"],
                                     section=row["to"]["section"],
                                     roll_number=row["to"]["roll_number"])

                promoted.append(row["student_id"])

        self.audit.record(
            "academic.promote", actor=actor, subject=academic_session_id,
            detail="%d student(s)" % len(promoted))

        return {
            "academic_session_id": academic_session_id,
            "promoted": promoted,
            "count": len(promoted),
            "carried_forward": {
                row["student_id"]: row["carried_forward"]
                for row in preview["students"] if row["carried_forward"]},
        }

    def _promotion_candidates(self, student_ids, class_name, section):
        """
        Who a bulk promotion applies to.

        An explicit list wins. Otherwise the class and section filters apply,
        and in every case only ACTIVE students are included: promoting a
        student who transferred out last term into this term's register would
        create an absence record for somebody who is not there.
        """
        clauses = ["status IN (%s)"
                   % ", ".join("?" for _ in PROMOTABLE_STATUSES)]
        parameters = list(PROMOTABLE_STATUSES)

        if student_ids:
            wanted = [str(value).strip() for value in student_ids
                      if str(value).strip()]
            if not wanted:
                return []
            clauses.append("student_id IN (%s)"
                           % ", ".join("?" for _ in wanted))
            parameters.extend(wanted)
        else:
            if class_name:
                clauses.append("class_name = ? COLLATE NOCASE")
                parameters.append(class_name)
            if section:
                clauses.append("section = ? COLLATE NOCASE")
                parameters.append(section)

        rows = self.database.query(
            "SELECT student_id, name, roll_number, class_name, section, status "
            "FROM students WHERE %s ORDER BY student_id" % " AND ".join(clauses),
            tuple(parameters))

        return [dict(row) for row in rows]

    # -- allocation import -------------------------------------------------

    @staticmethod
    def parse_allocation_csv(text):
        """
        Roll and section allocation for a whole class, as CSV.

        Deliberately hand-parsed with the standard library's csv module rather
        than pandas or openpyxl: a class list is three columns, and adding a
        spreadsheet dependency to read three columns would be the wrong trade.
        A .csv exported from Excel is what a school office actually produces.

            student_id,class_name,section,roll_number
            STU001,CSE(AI),C,001

        Unknown columns are ignored. A missing column means "carry the
        previous value forward" - the same rule promotion applies everywhere -
        rather than blanking it.
        """
        import csv
        import io as _io

        if not text or not text.strip():
            raise AcademicError("The allocation file is empty.")

        reader = csv.DictReader(_io.StringIO(text))

        if not reader.fieldnames:
            raise AcademicError("The allocation file has no header row.")

        headers = {name.strip().lower(): name for name in reader.fieldnames
                   if name}

        if "student_id" not in headers:
            raise AcademicError(
                "The allocation file needs a student_id column. Found: %s"
                % ", ".join(sorted(headers)) or "nothing")

        allocations = {}
        problems = []

        for number, row in enumerate(reader, start=2):
            student_id = _clean(row.get(headers["student_id"]), 64)
            if not student_id:
                problems.append("row %d has no student id" % number)
                continue

            if student_id in allocations:
                problems.append("row %d repeats %s" % (number, student_id))
                continue

            entry = {}
            for field in ("class_name", "section", "roll_number"):
                if field in headers:
                    value = _clean(row.get(headers[field]), 120)
                    if value:
                        entry[field] = value

            allocations[student_id] = entry

        if not allocations:
            raise AcademicError("The allocation file contained no students.")

        return {"allocations": allocations, "problems": problems,
                "students": len(allocations)}
