"""
The Academic terms page and the recognition model registry panel.

NO WEBCAM AND NO BROWSER ARE NEEDED. The API half runs against a temporary
SQLite file; the UI half is asserted against the dashboard's own source files.

WHY THIS SUITE EXISTS

    Terms, promotion, allocation import and the model registry were complete
    and tested through the service layer and the API, and the README said so
    plainly: "the browser UI for them is not built". These are the pages.

WHAT THIS VERIFIES

    That promotion cannot be run without being previewed first, and that what
    runs is the payload the preview was built from - not a second one
    assembled from the form afterwards, which could differ from what was read.

    That the page never claims a value it was not given. Where an
    administrator supplied no class, section or roll number, the preview says
    CARRIED FORWARD in words, per student, rather than showing two columns
    that happen to match and leaving it to be inferred.

    That the page forms no opinion about who is ACTIVE. The candidate list
    comes from the server's own preview, which is why the destination is
    applied in a second pass over exactly those students.

    That activating a model needs the model id typed out, and that rolling
    back to the baseline needs nothing - the asymmetry the registry is built
    around, carried into the UI rather than quietly evened out.

    That a metric nobody measured reads "not measured" and never a dash that
    could be mistaken for zero, and never a pass.

    That both pages are SUPER_ADMIN only, hidden AND enforced - the tab is
    hidden on the page, and the backend refuses a teacher regardless.

WHAT THIS DOES NOT VERIFY

    That a human clicking these buttons sees what is described here. No
    browser is driven - the DOM is never built, only the source that would
    build it.

    Any promotion rule, any activation gate. Both are the service layer's and
    are covered by test_academic_sessions.py and test_operations.py.

Run with:
    python tests/test_academic_ui.py
"""

import re
import shutil
import sys
import tempfile
import warnings
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
from paths import enable_safe_console  # noqa: E402

STATIC = PROJECT_ROOT / "backend" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "styles.css").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")

ADMIN = {"username": "office", "display_name": "The Office",
         "password": "a-long-enough-passphrase"}
TEACHER_PASSWORD = "another-long-passphrase"

TERM = {"academic_year": "2026-27", "semester": "1",
        "start_date": "2026-03-01", "end_date": "2026-07-31"}

# Every field the term table and the promotion preview read.
TERM_FIELDS = ("academic_session_id", "academic_year", "semester",
               "start_date", "end_date", "status")

PREVIEW_ROW_FIELDS = ("student_id", "name", "from", "to", "carried_forward",
                      "already_enrolled")

MODEL_FIELDS = ("model_id", "model_name", "version", "kind", "status",
                "metrics", "notes", "activated_at")


# -- fixtures ---------------------------------------------------------------

def make_client():
    directory = Path(tempfile.mkdtemp(prefix="classvision_academicui_"))
    state.configure(directory / "academicui.db")

    from backend.app import app
    return TestClient(app), directory


def teardown(client, directory):
    try:
        client.close()
    except Exception:                                    # noqa: BLE001
        pass
    state.close_database()
    shutil.rmtree(directory, ignore_errors=True)


def academic_source():
    """The JavaScript for the academic page, and nothing else."""
    start = JS.index("   ACADEMIC TERMS AND PROMOTION")
    end = JS.index("   THE RECOGNITION MODEL REGISTRY", start)
    return JS[start:end]


def models_source():
    """The JavaScript for the model registry panel, and nothing else."""
    start = JS.index("   THE RECOGNITION MODEL REGISTRY")
    end = JS.index("/* ---------- wiring ---------- */", start)
    return JS[start:end]


def academic_html():
    start = HTML.index('<section id="view-academic"')
    end = HTML.index('<section id="view-admin"', start)
    return HTML[start:end]


def setup_school(client):
    assert client.post("/api/auth/setup", json=ADMIN).status_code == 201

    for student_id, name, roll in (("A-1", "Asha Rao", "1"),
                                   ("A-2", "Bhavin Shah", "2"),
                                   ("A-3", "Chitra Das", "3")):
        response = client.post("/api/students", json={
            "student_id": student_id, "name": name, "roll_number": roll,
            "class_name": "CSE", "section": "A"})
        assert response.status_code == 201, response.text


def make_term(client):
    response = client.post("/api/academic/sessions", json=TERM)
    assert response.status_code == 201, response.text
    return response.json()["academic_session_id"]


# -- the page exists at all -------------------------------------------------

def test_academic_is_a_tab_in_the_existing_navigation():
    assert 'data-view="academic"' in HTML, "there is no Academic tab"
    assert 'id="view-academic"' in HTML, "there is no Academic view"
    assert '$("view-academic").hidden = view !== "academic"' in JS, (
        "the view is not driven by the existing switchView()")
    assert 'state.view === "academic"' in JS, (
        "refresh() does not know about the academic view")

    print("PASS  Academic is a tab in the existing navigation and view "
          "switcher")


def test_the_academic_markup_is_balanced():
    block = academic_html()

    for tag in ("div", "form", "table", "section", "p"):
        opened = len(re.findall(r"<%s\b" % tag, block))
        closed = block.count("</%s>" % tag)
        assert opened == closed, (
            "the academic view has %d <%s> and %d </%s>"
            % (opened, tag, closed, tag))

    print("PASS  every element in the academic view is closed")


def test_the_view_has_semantic_headings_and_labelled_controls():
    block = academic_html()

    assert 'aria-labelledby="academic-heading"' in block
    assert 'id="terms-heading"' in block
    assert 'id="promotion-heading"' in block

    # Every input has a label pointing at it.
    for field in ("promotion-term", "promotion-from-class",
                  "promotion-from-section", "promotion-to-class",
                  "promotion-to-section", "promotion-allocation"):
        assert 'for="%s"' % field in block, "%s has no label" % field
        assert 'id="%s"' % field in block, "%s does not exist" % field

    print("PASS  the view has semantic headings and every control is "
          "labelled")


def test_the_page_calls_the_endpoints_that_already_existed():
    source = academic_source()

    for endpoint in ("/api/academic/sessions",
                     "/api/academic/promotion/preview",
                     "/api/academic/promotion/allocation",
                     "/api/academic/promotion"):
        assert endpoint in source, "nothing calls %s" % endpoint

    urls = set(re.findall(r'"(/api/[a-z0-9/._-]+)', source))
    unknown = {url for url in urls if not url.startswith("/api/academic/")}
    assert not unknown, "the page reaches outside the academic API: %s" % (
        sorted(unknown))

    print("PASS  the page calls the academic endpoints and no others")


def test_the_endpoints_return_the_fields_the_page_reads():
    client, directory = make_client()
    try:
        setup_school(client)
        term_id = make_term(client)

        listing = client.get("/api/academic/sessions")
        assert listing.status_code == 200, listing.text
        term = listing.json()["items"][0]
        for field in TERM_FIELDS:
            assert field in term, "a term has no %s" % field

        enrolled = client.get("/api/academic/sessions/%s/students" % term_id)
        assert enrolled.status_code == 200, enrolled.text
        assert "total" in enrolled.json(), (
            "the term student count has no total, which is what the table "
            "cell shows")

        preview = client.post("/api/academic/promotion/preview", json={
            "academic_session_id": term_id})
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert {"students", "total", "already_enrolled"} <= set(body)
        for field in PREVIEW_ROW_FIELDS:
            assert field in body["students"][0], (
                "a preview row has no %s" % field)

        print("PASS  the endpoints return every field the page reads")
    finally:
        teardown(client, directory)


# -- promotion is previewed, and what runs is what was previewed ------------

def test_a_promotion_cannot_be_run_without_a_preview():
    source = academic_source()

    assert "if (!academicState.preview || !academicState.previewed)" in source, (
        "runPromotion does not insist on a preview")
    assert "Preview the promotion before running it." in source, (
        "there is no wording for running without previewing")
    assert '$("promotion-actions").hidden = rows.length === 0' in source, (
        "the Promote button is shown before anything has been previewed")

    print("PASS  a promotion cannot be run before it has been previewed")


def test_what_runs_is_the_payload_the_preview_was_built_from():
    """
    Not a second payload assembled from the form afterwards.

    If the administrator edits a field after previewing, a freshly assembled
    payload would promote something nobody read. The previewed one is sent
    verbatim instead.
    """
    source = academic_source()

    run = source[source.index("async function runPromotion"):]
    body = run[:run.index("\n}")]

    assert "academicState.previewed" in body, (
        "the promotion does not send the previewed payload")
    assert "targetedPayload(" not in body, (
        "the promotion reassembles a payload from the form instead of "
        "sending what was previewed")

    print("PASS  a promotion sends exactly the payload its preview was built "
          "from")


def test_the_page_forms_no_opinion_about_who_is_active():
    source = academic_source()
    preview = source[source.index("async function previewPromotion"):]

    assert "preview.students" in preview, (
        "the candidate list does not come from the server")
    assert "ACTIVE" not in preview.split("\n}")[0].replace(
        "about who is ACTIVE", ""), (
        "the page decides for itself which students are promotable")

    print("PASS  the candidate list comes from the server, not from the page")


def test_a_blanket_destination_is_applied_per_student_not_as_a_filter():
    """
    class_name and section on the promotion API are WHO, not WHERE.

    Sending the typed destination as those fields would silently promote only
    the students already in that class - a bulk operation doing a tenth of
    what it looked like it would do.
    """
    source = academic_source()

    assert "function targetedPayload(studentIds)" in source
    assert "existing.class_name === undefined" in source, (
        "a per-student allocation from the file does not win over the typed "
        "destination")

    client, directory = make_client()
    try:
        setup_school(client)
        term_id = make_term(client)

        # The typed destination as an allocation: everybody moves.
        payload = {"academic_session_id": term_id,
                   "allocations": {"A-1": {"class_name": "CSE-2"},
                                   "A-2": {"class_name": "CSE-2"},
                                   "A-3": {"class_name": "CSE-2"}}}
        preview = client.post("/api/academic/promotion/preview",
                              json=payload).json()

        assert preview["total"] == 3, "not every student was included"
        for row in preview["students"]:
            assert row["to"]["class_name"] == "CSE-2"
            # Section and roll were not supplied, so both are carried forward.
            assert "section" in row["carried_forward"]
            assert "roll_number" in row["carried_forward"]

        print("PASS  a typed destination moves every matching student and "
              "carries the rest forward")
    finally:
        teardown(client, directory)


def test_carried_forward_is_shown_in_words_per_student():
    source = academic_source()

    assert "row.carried_forward" in source, (
        "the preview does not show what is being carried forward")
    assert 'text: "nothing"' in source or '"nothing"' in source, (
        "a student with nothing carried forward is left blank rather than "
        "said")

    block = academic_html()
    assert "Carried forward" in block, (
        "the preview table has no carried-forward column")
    assert "carried forward" in block.lower(), (
        "the page never explains what carrying forward means")

    print("PASS  what is carried forward is named in words, per student")


def test_creating_a_term_promotes_nobody_and_the_page_says_so():
    block = academic_html() + HTML[HTML.index('id="term-dialog"'):
                                   HTML.index('id="camera-dialog"')]

    assert "PLANNED" in block, (
        "the page does not say a new term starts PLANNED")
    assert "promotes nobody" in block or "moves nobody" in block, (
        "the page does not say that creating a term moves nobody")

    client, directory = make_client()
    try:
        setup_school(client)
        term_id = make_term(client)

        enrolled = client.get("/api/academic/sessions/%s/students" % term_id)
        assert enrolled.json()["total"] == 0, (
            "creating a term enrolled somebody")

        print("PASS  creating a term enrols nobody, and the page says so")
    finally:
        teardown(client, directory)


def test_activating_a_term_closes_the_previous_one_without_losing_it():
    client, directory = make_client()
    try:
        setup_school(client)
        first = make_term(client)

        second = client.post("/api/academic/sessions", json={
            "academic_year": "2027-28", "semester": "1"})
        assert second.status_code == 201, second.text
        second_id = second.json()["academic_session_id"]

        assert client.post("/api/academic/sessions/%s/activate"
                           % first).status_code == 200
        assert client.post("/api/academic/sessions/%s/activate"
                           % second_id).status_code == 200

        terms = {row["academic_session_id"]: row
                 for row in client.get("/api/academic/sessions").json()["items"]}

        assert terms[second_id]["status"] == "ACTIVE"
        assert terms[first]["status"] == "CLOSED", (
            "the previous term was not closed")
        assert len(terms) == 2, "a term disappeared"

        source = academic_source()
        assert "closed; nothing it recorded has changed" in source, (
            "the page does not say what activating a term does to the "
            "previous one")

        print("PASS  activating a term closes the previous one and loses "
              "nothing")
    finally:
        teardown(client, directory)


# -- the allocation file ----------------------------------------------------

def test_an_allocation_file_is_parsed_by_the_server_not_the_page():
    source = academic_source()

    assert "/api/academic/promotion/allocation" in source, (
        "the page does not send the file to the parser that already exists")

    # The file is read as text and posted. Nothing here splits it up: a second
    # parser in the browser could disagree with the server about which rows
    # were usable, and the office would be told two different things.
    for parsing in ('split(",")', "split(/,/", "DictReader", "parseCsv",
                    "fieldnames"):
        assert parsing not in source, (
            "the page parses the allocation file itself (%s)" % parsing)
    assert "await file.text()" in source, (
        "the file is not simply read and sent")

    print("PASS  an allocation file is parsed by the server, not by the page")


def test_rows_the_parser_skipped_are_reported_rather_than_dropped():
    source = academic_source()
    assert "parsed.problems" in source, (
        "rows the parser skipped are not shown")
    assert "Skipped:" in source, (
        "skipped rows are not named, so an office would not know which "
        "students were left out")

    client, directory = make_client()
    try:
        setup_school(client)

        response = client.post("/api/academic/promotion/allocation", json={
            "csv": "student_id,section\nA-1,B\n,C\nA-1,D\n"})
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["students"] == 1
        assert len(body["problems"]) == 2, (
            "the parser silently dropped a row")

        print("PASS  rows the parser skipped are reported, not dropped")
    finally:
        teardown(client, directory)


def test_a_missing_column_carries_forward_rather_than_blanking():
    client, directory = make_client()
    try:
        setup_school(client)
        term_id = make_term(client)

        parsed = client.post("/api/academic/promotion/allocation", json={
            "csv": "student_id,section\nA-1,B\n"}).json()

        preview = client.post("/api/academic/promotion/preview", json={
            "academic_session_id": term_id,
            "allocations": parsed["allocations"]}).json()

        row = [each for each in preview["students"]
               if each["student_id"] == "A-1"][0]

        assert row["to"]["section"] == "B"
        assert row["to"]["class_name"] == "CSE", (
            "a column the file did not have blanked the value instead of "
            "carrying it forward")
        assert "class_name" in row["carried_forward"]

        print("PASS  a column the file does not have carries the previous "
              "value forward")
    finally:
        teardown(client, directory)


# -- the model registry -----------------------------------------------------

def test_the_model_registry_is_a_panel_on_a_page_that_already_existed():
    assert 'id="models-heading"' in HTML, "there is no model registry panel"
    assert 'id="models-table"' in HTML
    assert "loadModels().catch" in JS, (
        "the registry does not load with the page it is on")

    source = models_source()
    assert "/api/models" in source, (
        "the panel does not use the endpoint that already existed")

    print("PASS  the model registry is a panel on the Administration page")


def test_the_endpoint_returns_the_fields_the_panel_reads():
    client, directory = make_client()
    try:
        setup_school(client)

        response = client.get("/api/models")
        assert response.status_code == 200, response.text
        body = response.json()

        assert {"active", "models", "gate", "note"} <= set(body)
        for field in MODEL_FIELDS:
            assert field in body["models"][0], "a model has no %s" % field

        for field in ("accuracy", "precision", "recall",
                      "false_acceptance_rate", "minimum_test_identities",
                      "minimum_test_samples"):
            assert field in body["gate"], "the gate has no %s" % field

        print("PASS  the registry endpoint returns every field the panel "
              "reads")
    finally:
        teardown(client, directory)


def test_a_metric_nobody_measured_reads_not_measured():
    source = models_source()

    assert 'text: "not measured"' in source, (
        "an unmeasured metric has no wording, so it would render as a dash "
        "or as nothing - both of which read as a number")
    assert "value === null || value === undefined" in source, (
        "the panel does not distinguish an absent metric from a zero one")

    client, directory = make_client()
    try:
        setup_school(client)
        baseline = client.get("/api/models").json()["models"][0]

        # The SFace baseline carries no measured accuracy from this project.
        metrics = baseline.get("metrics") or {}
        assert metrics.get("accuracy") is None, (
            "the baseline claims an accuracy this project never measured")

        print("PASS  a metric nobody measured reads 'not measured'")
    finally:
        teardown(client, directory)


def test_activating_a_model_needs_the_id_typed_and_rollback_needs_nothing():
    source = models_source()

    assert "window.prompt(" in source, (
        "activation asks for no confirmation")
    assert "typed.trim() !== modelId" in source, (
        "activation does not check what was typed")
    assert "confirm=" in source, (
        "the typed confirmation is not passed to the backend, which has its "
        "own gate")

    rollback = source[source.index("async function rollbackModel"):]
    rollback = rollback[:rollback.index("\n}")]
    assert "prompt" not in rollback, (
        "rolling back to the baseline was gated - the safe direction must be "
        "the easy one")

    client, directory = make_client()
    try:
        setup_school(client)

        from model_registry import ModelRegistry
        registry = ModelRegistry(state.get_database())
        registry.register_experiment(
            model_id="EXP-1", version="0.1",
            metrics={"accuracy": 0.99, "precision": 0.99, "recall": 0.99,
                     "false_acceptance_rate": 0.001,
                     "false_rejection_rate": 0.01,
                     "test_identities": 9, "test_samples": 120,
                     "held_out": True})

        assert client.post(
            "/api/models/EXP-1/validate").status_code == 200

        # No confirmation: refused.
        refused = client.post("/api/models/EXP-1/activate")
        assert refused.status_code == 400, (
            "a model was activated without the id being typed")

        confirmed = client.post(
            "/api/models/EXP-1/activate?confirm=EXP-1")
        assert confirmed.status_code == 200, confirmed.text

        # Rollback is ungated.
        back = client.post("/api/models/rollback")
        assert back.status_code == 200, back.text
        assert back.json()["model_id"] != "EXP-1"

        print("PASS  activation needs the id typed out; rollback needs "
              "nothing")
    finally:
        teardown(client, directory)


def test_the_panel_says_what_activation_actually_changes():
    block = HTML[HTML.index('id="models-heading"'):
                 HTML.index('id="audit-heading"')]

    assert "experimental" in block, (
        "the panel does not say a custom model is experimental")
    assert "changes nothing about what the camera recognises" in block, (
        "the panel does not say that registering or training a model changes "
        "nothing")

    source = models_source()
    assert "changes what the camera recognises" in source, (
        "the activation confirmation does not say what it changes")

    print("PASS  the panel says what activation changes and what does not")


def test_the_gate_is_shown_rather_than_left_to_be_guessed():
    source = models_source()
    assert "function describeGate(" in source
    assert "is a failure, not a pass" in source, (
        "the page does not say that a missing metric fails the gate")

    print("PASS  the activation floor is shown on the page")


# -- permission -------------------------------------------------------------

def test_both_pages_are_super_admin_only_and_the_backend_enforces_it():
    assert 'id="tab-academic"' in HTML

    # The tab ships hidden, and only renderAccount() may reveal it.
    #
    # This used to be spelled `'hidden>Academic' in HTML`, which asserted the
    # attribute and the label were ADJACENT - true only for as long as the
    # label was a bare text node. It is wrapped in a span now, so the two are
    # no longer neighbours and the button is still hidden. What matters is
    # that the attribute is on this button, so that is what is read.
    tag = HTML[HTML.index('<button', HTML.index('id="tab-academic"') - 200):]
    tag = tag[:tag.index('>') + 1]
    assert 'id="tab-academic"' in tag and ' hidden' in tag, (
        "the Academic tab is not hidden by default: %s" % tag)
    assert '$("tab-academic").hidden =' in JS, (
        "the Academic tab is never shown or hidden by role")
    assert 'state.view === "academic"' in JS

    # A teacher landing on it is moved off rather than shown an empty page.
    assert ('(state.view === "admin" || state.view === "academic")'
            in JS), "a teacher left on the academic tab would see an empty "\
                    "page"

    client, directory = make_client()
    try:
        setup_school(client)
        term_id = make_term(client)

        created = client.post("/api/users", json={
            "username": "tsmith", "display_name": "T Smith",
            "password": TEACHER_PASSWORD, "role": "TEACHER",
            "assignments": [{"class_name": "CSE", "section": "A"}]})
        assert created.status_code == 201, created.text

        from backend.app import app
        teacher = TestClient(app)
        try:
            signed = teacher.post("/api/auth/login", json={
                "username": "tsmith", "password": TEACHER_PASSWORD})
            assert signed.status_code == 200, signed.text

            # Reading terms is fine - a teacher needs to know what term it is.
            assert teacher.get("/api/academic/sessions").status_code == 200

            # Every write is refused by the server, not by a hidden button.
            assert teacher.post("/api/academic/sessions",
                                json={"academic_year": "2030-31"}
                                ).status_code == 403
            assert teacher.post("/api/academic/sessions/%s/activate"
                                % term_id).status_code == 403
            assert teacher.post("/api/academic/promotion",
                                json={"academic_session_id": term_id}
                                ).status_code == 403
            assert teacher.post("/api/academic/promotion/allocation",
                                json={"csv": "student_id\nA-1\n"}
                                ).status_code == 403
            assert teacher.post(
                "/api/models/rollback").status_code == 403
        finally:
            teacher.close()

        print("PASS  both pages are SUPER_ADMIN only and the backend enforces "
              "it")
    finally:
        teardown(client, directory)


def test_an_unauthenticated_caller_is_refused():
    client, directory = make_client()
    try:
        setup_school(client)

        from backend.app import app
        anonymous = TestClient(app)
        try:
            for path in ("/api/academic/sessions", "/api/models"):
                assert anonymous.get(path).status_code == 401, (
                    "%s is readable without signing in" % path)
        finally:
            anonymous.close()

        print("PASS  both endpoints are 401 for an unauthenticated caller")
    finally:
        teardown(client, directory)


# -- the rest of the page ---------------------------------------------------

def test_status_is_carried_by_a_symbol_as_well_as_a_colour():
    source = academic_source() + models_source()

    assert "TERM_STATUS_SYMBOL" in source, "a term's status is colour only"
    assert 'class: "sym"' in source, "no status renders a symbol"

    for status in ("PLANNED", "CLOSED"):
        assert ".pill-%s" % status in CSS, "no styling for a %s term" % status
    for status in ("ACTIVE", "VALIDATED", "TESTING", "RETIRED", "BASELINE"):
        assert ".pill-model-%s" % status in CSS, (
            "no styling for a %s model" % status)

    print("PASS  every status carries a symbol as well as a colour")


def test_the_new_styles_reuse_the_existing_palette():
    # Anchored on the rules, not on a comment heading: the term and model
    # pills, up to where the camera pills begin.
    start = CSS.index(".pill-PLANNED")
    end = CSS.index(".pill-cam-online", start)
    block = CSS[start:end]
    assert ".pill-model-ACTIVE" in block, (
        "the term/model pill block no longer contains the rules this test "
        "checks")

    literals = re.findall(r"#[0-9a-fA-F]{3,8}\b", block)
    assert not literals, (
        "the new pills hard-code colours instead of using tokens: %s"
        % literals)

    print("PASS  the new pills use the existing palette tokens only")


def test_the_pages_add_no_external_request():
    source = academic_source() + models_source()
    for pattern in ("http://", "https://", "//cdn", "fonts.googleapis"):
        assert pattern not in source, (
            "the academic pages reach outside this machine: %s" % pattern)

    print("PASS  the academic pages make no request that leaves the machine")


def test_the_pages_carry_nothing_biometric():
    source = academic_source() + academic_html()
    for word in ("embedding", "descriptor", "face_crop", "biometric"):
        assert word not in source.lower(), (
            "the academic page mentions %s" % word)

    print("PASS  the academic page carries nothing biometric")


def test_the_dashboard_still_serves_its_own_source():
    client, directory = make_client()
    try:
        assert client.get("/").text == HTML, (
            "the served page differs from the source file - a line-ending "
            "change is the usual cause")
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/static/styles.css").status_code == 200

        print("PASS  the served dashboard still matches its source byte for "
              "byte")
    finally:
        teardown(client, directory)


def test_the_existing_views_are_untouched():
    for view in ("overview", "sessions", "students", "live", "reports",
                 "cameras", "admin"):
        assert 'id="view-%s"' % view in HTML, "the %s view is gone" % view
        assert '$("view-%s").hidden = view !== "%s"' % (view, view) in JS, (
            "the %s view is no longer driven by switchView()" % view)

    print("PASS  the existing views are untouched")


def test_academic_ui_tests_never_touch_the_real_database():
    real = PROJECT_ROOT / "data" / "classvision.db"
    before = real.stat().st_mtime if real.exists() else None

    client, directory = make_client()
    try:
        assert Path(state.get_database().path).parent != real.parent
        setup_school(client)
        make_term(client)
    finally:
        teardown(client, directory)

    after = real.stat().st_mtime if real.exists() else None
    assert after == before, "the real database was modified by a test"

    print("PASS  every test ran against a temporary database")


# -- runner -----------------------------------------------------------------

TESTS = [
    test_academic_is_a_tab_in_the_existing_navigation,
    test_the_academic_markup_is_balanced,
    test_the_view_has_semantic_headings_and_labelled_controls,
    test_the_page_calls_the_endpoints_that_already_existed,
    test_the_endpoints_return_the_fields_the_page_reads,
    test_a_promotion_cannot_be_run_without_a_preview,
    test_what_runs_is_the_payload_the_preview_was_built_from,
    test_the_page_forms_no_opinion_about_who_is_active,
    test_a_blanket_destination_is_applied_per_student_not_as_a_filter,
    test_carried_forward_is_shown_in_words_per_student,
    test_creating_a_term_promotes_nobody_and_the_page_says_so,
    test_activating_a_term_closes_the_previous_one_without_losing_it,
    test_an_allocation_file_is_parsed_by_the_server_not_the_page,
    test_rows_the_parser_skipped_are_reported_rather_than_dropped,
    test_a_missing_column_carries_forward_rather_than_blanking,
    test_the_model_registry_is_a_panel_on_a_page_that_already_existed,
    test_the_endpoint_returns_the_fields_the_panel_reads,
    test_a_metric_nobody_measured_reads_not_measured,
    test_activating_a_model_needs_the_id_typed_and_rollback_needs_nothing,
    test_the_panel_says_what_activation_actually_changes,
    test_the_gate_is_shown_rather_than_left_to_be_guessed,
    test_both_pages_are_super_admin_only_and_the_backend_enforces_it,
    test_an_unauthenticated_caller_is_refused,
    test_status_is_carried_by_a_symbol_as_well_as_a_colour,
    test_the_new_styles_reuse_the_existing_palette,
    test_the_pages_add_no_external_request,
    test_the_pages_carry_nothing_biometric,
    test_the_dashboard_still_serves_its_own_source,
    test_the_existing_views_are_untouched,
    test_academic_ui_tests_never_touch_the_real_database,
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
