"""
Attendance report endpoints.

Two routes over one service call: the report as JSON for the page, and the same
rows as a CSV file for the filing cabinet. They share
ReportService.attendance, so the file a school keeps and the table they looked
at cannot disagree about a student.

Permissions

    Authentication is applied to the whole router, and the class-and-student
    scope is the Principal handed to the service. A teacher's report contains
    their own classes and their own students; a Super Admin's contains the
    school. There is no report-level override and no "export everything"
    parameter, because a CSV is the easiest thing in a system to walk out of a
    building with.

    A filter naming a class the caller cannot see is not an error - it simply
    matches nothing, which is the same answer they get for a class that does
    not exist.

Nothing here decides attendance

    Every row was written by the Step 6 rule engine. These endpoints select and
    format; they apply no threshold and recompute no status. See the note at
    the top of backend/services/report_service.py.
"""

from datetime import date as _date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from database import ATTENDANCE_STATUSES

from backend.services.authz import Principal, require_user
from backend.services.report_service import MAX_REPORT_ROWS, ReportService
from backend.services.state import get_database

router = APIRouter(prefix="/api/reports", tags=["reports"],
                   dependencies=[Depends(require_user)])

ISO_DATE = r"^\d{4}-\d{2}-\d{2}$"


def _service():
    return ReportService(get_database())


def _check_date(value, label):
    """
    A date that matches the pattern AND exists.

    The ISO_DATE pattern below accepts "2026-13-45" - four digits, two, two -
    and SQLite compares session_date as TEXT, so `date_to=2026-13-45` is
    lexically greater than every real date and quietly matches the whole table.
    A filter that silently WIDENS a report is the dangerous direction for this
    to fail in, so the month and day are checked to be a real day.
    """
    if not value:
        return None
    try:
        _date.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="%s is not a real date: %r. Use YYYY-MM-DD."
                   % (label, value))
    return value


def _filters(principal, student_id, class_id, class_name, section, date_from,
             date_to, status, policy_version, session_id, include_unevaluated,
             limit):
    """
    The one place the query string becomes service arguments.

    Shared by both endpoints so the CSV cannot gain or lose a filter the JSON
    report has - which is how an export ends up containing rows the page that
    offered it never showed.
    """
    return {
        "principal": principal,
        "student_id": student_id or None,
        "class_id": class_id or None,
        "class_name": class_name or None,
        "section": section or None,
        "date_from": _check_date(date_from, "date_from"),
        "date_to": _check_date(date_to, "date_to"),
        "status": status or None,
        "policy_version": policy_version or None,
        "session_id": session_id or None,
        "include_unevaluated": include_unevaluated,
        "limit": limit,
    }


@router.get("/attendance", summary="Attendance records, filtered")
def attendance_report(
        student_id: Optional[str] = Query(None, max_length=120),
        class_id: Optional[str] = Query(
            None, max_length=120,
            description="The session's class identifier, e.g. CLASS-10A"),
        class_name: Optional[str] = Query(
            None, max_length=120,
            description="The student's registered class"),
        section: Optional[str] = Query(
            None, max_length=40,
            description="The student's registered section"),
        date_from: Optional[str] = Query(None, pattern=ISO_DATE),
        date_to: Optional[str] = Query(None, pattern=ISO_DATE),
        status: Optional[str] = Query(
            None, pattern="^(%s)$" % "|".join(ATTENDANCE_STATUSES)),
        policy_version: Optional[str] = Query(None, max_length=50),
        session_id: Optional[str] = Query(None, max_length=200),
        include_unevaluated: bool = Query(
            False,
            description="Also list students expected at a completed session "
                        "who were never evaluated. Reported with a null "
                        "status, never as absent."),
        limit: int = Query(MAX_REPORT_ROWS, ge=1, le=MAX_REPORT_ROWS),
        principal: Principal = Depends(require_user)):
    """
    A report is a whole-selection answer, so it is not paged the way a table
    is: `total` is how many rows matched and `truncated` says whether `limit`
    cut any off. The summary counts exactly the rows returned, so the figures
    under a table are the figures OF that table.
    """
    return _service().attendance(**_filters(
        principal, student_id, class_id, class_name, section, date_from,
        date_to, status, policy_version, session_id, include_unevaluated,
        limit))


@router.get("/attendance.csv", summary="The same report as a CSV file")
def attendance_report_csv(
        student_id: Optional[str] = Query(None, max_length=120),
        class_id: Optional[str] = Query(None, max_length=120),
        class_name: Optional[str] = Query(None, max_length=120),
        section: Optional[str] = Query(None, max_length=40),
        date_from: Optional[str] = Query(None, pattern=ISO_DATE),
        date_to: Optional[str] = Query(None, pattern=ISO_DATE),
        status: Optional[str] = Query(
            None, pattern="^(%s)$" % "|".join(ATTENDANCE_STATUSES)),
        policy_version: Optional[str] = Query(None, max_length=50),
        session_id: Optional[str] = Query(None, max_length=200),
        include_unevaluated: bool = Query(False),
        limit: int = Query(MAX_REPORT_ROWS, ge=1, le=MAX_REPORT_ROWS),
        principal: Principal = Depends(require_user)):
    """
    Downloadable CSV of the rows the caller may see.

    Content-Disposition names the file after the filters that produced it, so
    three exports in a morning do not all land in Downloads as
    "attendance.csv". Cache-Control is no-store: this is somebody's register,
    and a shared classroom machine should not keep a copy in its HTTP cache.
    """
    body, report = _service().attendance_csv(**_filters(
        principal, student_id, class_id, class_name, section, date_from,
        date_to, status, policy_version, session_id, include_unevaluated,
        limit))

    parts = ["attendance"]
    for value in (class_id or class_name, section, student_id, date_from,
                  date_to):
        if value:
            # Only characters that are safe in a filename on every platform.
            parts.append("".join(character for character in str(value)
                                 if character.isalnum() or character in "-_"))

    filename = "-".join(parts)[:120] + ".csv"

    return Response(
        content=body,
        # text/csv with an explicit charset: the names in here are not ASCII in
        # every school, and a spreadsheet that guesses gets them wrong.
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="%s"' % filename,
            "Cache-Control": "no-store, no-cache",
            # So the page can tell the teacher what it just handed them
            # without parsing the file back.
            "X-Report-Rows": str(report["returned"]),
            "X-Report-Truncated": "1" if report["truncated"] else "0",
        })


# ---------------------------------------------------------------------------
# Step 11 - summaries, the seat map, and PDF
#
# Every route below is a THIN wrapper over a ReportService call. None of them
# computes a rate: the per-class figures are AnalyticsService.class_analytics
# and the per-term figures are AnalyticsService.day_summary, which is the
# constraint the roadmap set for this step. A summary endpoint that did its own
# arithmetic would be a second, unverifiable source of truth about a student's
# attendance.
#
# The PDF routes exist beside the JSON ones rather than as a `?format=pdf`
# parameter on them, because the two return different media types and a client
# that asks for JSON and gets a file back is a worse API than two paths.
# ---------------------------------------------------------------------------

# Shared by every PDF response. no-store because this is somebody's register
# and a shared classroom machine should not keep a copy in its HTTP cache.
PDF_MEDIA_TYPE = "application/pdf"


def _pdf_filename(*parts):
    """
    A download name built from the filters that produced the file.

    Three exports in a morning should not all land in Downloads as
    "report.pdf". Only characters that are safe in a filename on every
    platform survive.
    """
    safe = []
    for value in parts:
        if not value:
            continue
        cleaned = "".join(character for character in str(value)
                          if character.isalnum() or character in "-_")
        if cleaned:
            safe.append(cleaned)
    return "-".join(safe)[:120] + ".pdf"


def _pdf_response(body, meta, filename):
    """
    One place where a PDF becomes an HTTP response.

    X-Report-Unencodable is not decoration. Helvetica cannot draw a name
    outside Latin-1, so those characters are printed as "?" - and the page
    that offered the download needs to be able to say so and point at the CSV
    instead. A silent "???" on a printed register is the failure this header
    exists to prevent. See backend/services/pdf_writer.py.
    """
    return Response(
        content=body,
        media_type=PDF_MEDIA_TYPE,
        headers={
            "Content-Disposition": 'attachment; filename="%s"' % filename,
            "Cache-Control": "no-store, no-cache",
            "X-Report-Pages": str(meta["pages"]),
            "X-Report-Rows": str(meta["rows"]),
            "X-Report-Unencodable": str(meta["unencodable_characters"]),
        })


@router.get("/attendance.pdf", summary="The same report as a PDF file")
def attendance_report_pdf(
        student_id: Optional[str] = Query(None, max_length=120),
        class_id: Optional[str] = Query(None, max_length=120),
        class_name: Optional[str] = Query(None, max_length=120),
        section: Optional[str] = Query(None, max_length=40),
        date_from: Optional[str] = Query(None, pattern=ISO_DATE),
        date_to: Optional[str] = Query(None, pattern=ISO_DATE),
        status: Optional[str] = Query(
            None, pattern="^(%s)$" % "|".join(ATTENDANCE_STATUSES)),
        policy_version: Optional[str] = Query(None, max_length=50),
        session_id: Optional[str] = Query(None, max_length=200),
        include_unevaluated: bool = Query(False),
        limit: int = Query(MAX_REPORT_ROWS, ge=1, le=MAX_REPORT_ROWS),
        principal: Principal = Depends(require_user)):
    """
    Printable register, from the same rows as the JSON report and the CSV.

    Characters outside Latin-1 cannot be drawn by the built-in font and print
    as "?"; X-Report-Unencodable counts them and the CSV export is lossless.
    """
    body, meta = _service().attendance_pdf(**_filters(
        principal, student_id, class_id, class_name, section, date_from,
        date_to, status, policy_version, session_id, include_unevaluated,
        limit))

    return _pdf_response(body, meta, _pdf_filename(
        "attendance", class_id or class_name, section, student_id, date_from,
        date_to))


@router.get("/classes", summary="Attendance summarised per class")
def class_summary(
        class_id: Optional[str] = Query(None, max_length=120),
        date_from: Optional[str] = Query(None, pattern=ISO_DATE),
        date_to: Optional[str] = Query(None, pattern=ISO_DATE),
        principal: Principal = Depends(require_user)):
    """
    One row per class the caller may see, worst attendance first.

    The rates are AnalyticsService.class_analytics's own. Dates narrow which
    of that class's already-computed sessions are counted; they do not cause
    anything to be recomputed a second way.
    """
    return _service().class_summary(
        principal=principal, class_id=class_id,
        date_from=_check_date(date_from, "date_from"),
        date_to=_check_date(date_to, "date_to"))


@router.get("/classes.pdf", summary="The per-class summary as a PDF")
def class_summary_pdf(
        class_id: Optional[str] = Query(None, max_length=120),
        date_from: Optional[str] = Query(None, pattern=ISO_DATE),
        date_to: Optional[str] = Query(None, pattern=ISO_DATE),
        principal: Principal = Depends(require_user)):
    body, meta = _service().class_summary_pdf(
        principal=principal, class_id=class_id,
        date_from=_check_date(date_from, "date_from"),
        date_to=_check_date(date_to, "date_to"))

    return _pdf_response(body, meta, _pdf_filename(
        "classes", class_id, date_from, date_to))


@router.get("/terms", summary="Attendance summarised per academic term")
def term_summary(
        academic_session_id: Optional[str] = Query(None, max_length=200),
        principal: Principal = Depends(require_user)):
    """
    One row per term, each with its classes underneath.

    Lessons recorded before academic terms existed have no term and are
    reported under a null term labelled as unassigned, rather than dropped or
    folded into a term nobody filed them under.
    """
    return _service().term_summary(principal=principal,
                                   academic_session_id=academic_session_id)


@router.get("/terms.pdf", summary="The per-term summary as a PDF")
def term_summary_pdf(
        academic_session_id: Optional[str] = Query(None, max_length=200),
        principal: Principal = Depends(require_user)):
    body, meta = _service().term_summary_pdf(
        principal=principal, academic_session_id=academic_session_id)

    return _pdf_response(body, meta,
                         _pdf_filename("terms", academic_session_id))


@router.get("/sessions/{session_id}/seatmap",
            summary="One session as a printable grid of students")
def seat_map(session_id: str,
             columns: int = Query(6, ge=1, le=12),
             principal: Principal = Depends(require_user)):
    """
    The session's students in a stable grid, in roll order.

    NOT a seating plan. No seat position is stored by this system and none is
    inferred from the camera - see ReportService.seat_map. A session outside
    the caller's classes is 404, the same answer as one that does not exist.
    """
    report = _service().seat_map(session_id, principal=principal,
                                 columns=columns)
    if report is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return report


@router.get("/sessions/{session_id}/seatmap.pdf",
            summary="The seat map as a PDF")
def seat_map_pdf(session_id: str,
                 columns: int = Query(6, ge=1, le=12),
                 principal: Principal = Depends(require_user)):
    result = _service().seat_map_pdf(session_id, principal=principal,
                                     columns=columns)
    if result is None:
        raise HTTPException(status_code=404, detail="Session not found")

    body, meta = result
    return _pdf_response(body, meta, _pdf_filename("seatmap", session_id))
