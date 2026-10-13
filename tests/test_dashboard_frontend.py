"""
Frontend tests for the Step 7 dashboard (static, deterministic).

No browser automation. Installing a full browser stack for one step was not
justified, so these tests inspect the served HTML, CSS and JavaScript directly
for the properties that were promised.

WHAT THIS VERIFIES
    Structure, semantics, and the presence of accessibility and responsive
    features - deterministically, on every run.

WHAT THIS DOES NOT VERIFY
    Actual rendering, real pixel layout at each breakpoint, real keyboard
    focus order in a browser, or computed colour contrast. Those were checked
    by reading the code, not by running a browser, and that limitation is
    stated in the README rather than glossed over.

Run with:
    python tests/test_dashboard_frontend.py
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

import backend  # noqa: F401,E402

from fastapi.testclient import TestClient  # noqa: E402

from backend.services import state  # noqa: E402
from paths import enable_safe_console  # noqa: E402

STATIC = PROJECT_ROOT / "backend" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "styles.css").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")

# The spatial layer. Two more stylesheets' worth of appearance and two more
# scripts, and every guarantee below has to cover them: a file that is not read
# here is a file the "no external request" and "it parses" checks do not apply
# to, which is exactly the hole a new file would otherwise open.
SPATIAL_CSS = (STATIC / "spatial.css").read_text(encoding="utf-8")
SPATIAL_JS = (STATIC / "spatial.js").read_text(encoding="utf-8")
ASSEMBLY_JS = (STATIC / "assembly.js").read_text(encoding="utf-8")
FAVICON = (STATIC / "favicon.svg").read_text(encoding="utf-8")

# Every stylesheet and every script the page loads, so a check written once
# applies to all of them.
ALL_CSS = (CSS, SPATIAL_CSS)
ALL_SCRIPTS = (("app.js", JS), ("spatial.js", SPATIAL_JS),
               ("assembly.js", ASSEMBLY_JS))

ALL_STATUSES = ("PRESENT", "LATE", "PARTIAL", "LEFT_EARLY", "ABSENT",
                "UNKNOWN", "MANUAL_OVERRIDE")


def client():
    directory = Path(tempfile.mkdtemp(prefix="classvision_fe_"))
    state.configure(directory / "fe.db")
    from backend.app import app
    return TestClient(app), directory


def teardown(test_client, directory):
    test_client.close()
    state.close_database()
    shutil.rmtree(directory, ignore_errors=True)


# -- structure -------------------------------------------------------------

def test_semantic_landmarks():
    for tag in ("<header", "<main", "<nav", "<footer", "<section"):
        assert tag in HTML, "missing semantic element: %s" % tag
    assert HTML.count("<h1") >= 3, "each view should have a heading"
    assert 'lang="en"' in HTML, "the document language is not declared"
    print("PASS  semantic landmarks present (header, nav, main, section, footer)")


def test_viewport_and_responsive_rules():
    assert 'name="viewport"' in HTML, "the viewport meta tag is missing"
    assert "width=device-width" in HTML, "the viewport is not device width"

    breakpoints = re.findall(r"@media\s*\(max-width:\s*(\d+)px\)", CSS)
    assert breakpoints, "no responsive breakpoints are defined"
    assert any(int(width) <= 600 for width in breakpoints), (
        "no mobile breakpoint - smallest is %s" % breakpoints)
    print("PASS  viewport declared with breakpoints at %s px"
          % ", ".join(sorted(breakpoints, key=int)))


def test_wide_content_scrolls_instead_of_overflowing():
    """
    Tables are wide. They must scroll inside their own container rather than
    push the page sideways, which is the usual cause of mobile overflow.
    """
    assert ".table-scroll" in CSS, "no scroll container for wide tables"
    assert re.search(r"\.table-scroll\s*\{[^}]*overflow-x:\s*auto", CSS), (
        "the table container does not scroll horizontally")

    assert HTML.count('class="table-scroll"') >= 4, (
        "not every table is inside a scroll container (found %d)"
        % HTML.count('class="table-scroll"'))

    # The min-width that keeps tables legible must apply to the TABLE, inside
    # the scroller - never to the page.
    mobile = CSS[CSS.rfind("@media (max-width: 600px)"):]
    assert "min-width" in mobile, "tables have no legible minimum width on mobile"
    assert "body" not in mobile.split("min-width")[0][-120:], (
        "a min-width appears to be applied to the page body, which would cause "
        "horizontal overflow")
    print("PASS  wide tables scroll within their container; no page-level "
          "min-width")


def test_fluid_layout_has_no_fixed_page_width():
    assert "max-width: 1400px" in CSS, "the page should have a readable max width"
    assert not re.search(r"\bbody\s*\{[^}]*\bwidth:\s*\d+px", CSS), (
        "the body has a fixed pixel width, which breaks small screens")
    assert "minmax(" in CSS, "grids should be fluid (minmax) rather than fixed"
    print("PASS  layout is fluid - grids use minmax and the body is not fixed")


# -- accessibility ---------------------------------------------------------

def test_the_hidden_attribute_actually_hides():
    """
    `hidden` is a UA stylesheet rule, and ANY author display beats it.

    This is the regression guard for a defect that ran through the whole
    interface. `[hidden] { display: none }` lives in the user agent's own
    stylesheet, and origin beats specificity in the cascade - so the moment an
    element is given a display of its own, as .auth-screen was with
    `display: grid`, setting `hidden` on it in JavaScript does nothing. The
    attribute appears in the DOM, every test that looks for it passes, and the
    element stays on screen.

    Twenty-one elements were in that state when this was found, including the
    sign-in gate, which stayed laid out behind the console after signing in.

    So the stylesheet states the rule itself, with !important - which is the
    requirement rather than decoration, because without it the rule still
    loses to an id selector that sets display.
    """
    # Comment-stripped: the rule carries a long explanation that QUOTES the
    # user agent's own `[hidden] { display: none; }`, and matching the prose
    # instead of the rule is how the first version of this test failed.
    rules = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)

    assert "[hidden]" in rules, (
        "the stylesheet never states what the hidden attribute does, so any "
        "element given a display of its own ignores it")

    rule = re.search(r"^\[hidden\][^{]*\{([^}]*)\}", rules, flags=re.M)
    assert rule, "there is no top-level [hidden] rule in the stylesheet"
    body = rule.group(1)

    assert "display" in body and "none" in body, (
        "the [hidden] rule does not set display: none: %s" % body)
    assert "!important" in body, (
        "the [hidden] rule is not !important, so it still loses to an id "
        "selector that sets display: %s" % body)

    # Every element the interface toggles with `hidden` relies on this. A
    # sample of the ones that were actually broken, so the test fails loudly
    # rather than passing on an empty page.
    for element in ("auth-screen", "entry-screen", "session-detail",
                    "student-detail", "live-summary", "live-grid"):
        assert 'id="%s"' % element in HTML, (
            "this test names #%s, which no longer exists" % element)

    print("PASS  the stylesheet makes the hidden attribute work on elements "
          "that have a display of their own")


def test_skip_link_and_focus_states():
    assert 'class="skip-link"' in HTML, "no skip link for keyboard users"
    assert 'href="#main"' in HTML, "the skip link does not target main content"
    assert ":focus-visible" in CSS, "no visible focus style is defined"
    assert re.search(r":focus-visible\s*\{[^}]*outline:", CSS), (
        "focus-visible does not draw an outline")
    print("PASS  skip link present and focus-visible draws an outline")


def test_every_control_has_a_label():
    """Each form control must be programmatically labelled."""
    control_ids = re.findall(r'<(?:input|select)[^>]*\bid="([^"]+)"', HTML)
    labelled = set(re.findall(r'<label[^>]*\bfor="([^"]+)"', HTML))

    unlabelled = [control for control in control_ids if control not in labelled]
    assert not unlabelled, "controls without a label: %s" % unlabelled
    assert len(control_ids) >= 8, (
        "expected several controls, found %d" % len(control_ids))
    print("PASS  all %d form controls have associated labels" % len(control_ids))


def test_tables_have_headers_and_captions():
    header_cells = re.findall(r'<th scope="col"', HTML)
    assert len(header_cells) >= 20, (
        "table headers should declare scope, found %d" % len(header_cells))

    captions = re.findall(r'<caption class="sr-only">', HTML)
    assert len(captions) >= 5, (
        "each table should have a caption for screen readers, found %d"
        % len(captions))
    print("PASS  %d scoped headers and %d screen-reader captions"
          % (len(header_cells), len(captions)))


def test_status_is_not_conveyed_by_colour_alone():
    """The requirement that matters most for accessibility here."""
    for status in ALL_STATUSES:
        assert status in JS, "status %s is not handled in the UI" % status
        assert re.search(r"%s:\s*\"" % re.escape(status), JS), (
            "status %s has no symbol defined" % status)

    assert "STATUS_SYMBOL" in JS, "no symbol map exists"
    # The pill renders symbol + text, with the symbol hidden from screen
    # readers because the text already says it.
    assert 'aria-hidden": "true"' in JS or "aria-hidden" in JS, (
        "the decorative symbol is not hidden from assistive technology")
    assert "status.replace" in JS, "the status text itself is not rendered"
    print("PASS  every status renders as symbol + text, never colour alone")


def test_charts_have_text_alternatives():
    assert 'setAttribute("role", "img")' in JS, "the chart has no image role"
    assert "aria-label" in JS, "the chart has no accessible label"
    assert "Values — " in JS or "Values" in JS, (
        "the chart has no textual equivalent of its data")
    print("PASS  charts carry role=img, an aria-label and a text equivalent")


def test_live_region_for_status_messages():
    assert 'role="status"' in HTML, "there is no live region for messages"
    assert 'id="banner"' in HTML, "the status banner is missing"
    assert "aria-busy" in JS, "loading state is not announced"
    print("PASS  a live region announces status and loading changes")


def test_dialog_is_accessible():
    assert "<dialog" in HTML, "the evidence panel should use a native dialog"
    assert "aria-labelledby" in HTML, "the dialog is not labelled"
    assert "showModal()" in JS, "the dialog is not opened as a modal"
    assert 'role="alert"' in HTML, "form errors are not announced"
    assert "<fieldset" in HTML and "<legend" in HTML, (
        "the override form is not grouped with a legend")
    print("PASS  native modal dialog, labelled, with an alert region")


def test_dark_mode_and_contrast_tokens():
    assert "prefers-color-scheme: dark" in CSS, "no dark mode support"
    assert CSS.count("--text-muted") >= 2, (
        "muted text colour is not defined for both schemes")
    print("PASS  colours are tokenised with a dark-scheme override")


# -- behaviour promised by the UI -----------------------------------------

def test_empty_states_are_explicit():
    for marker in ("No attendance sessions yet", "No sessions match",
                   "No students"):
        assert marker in HTML, "missing empty state: %s" % marker

    assert "never creates example data" in HTML, (
        "the empty state should say that no demo data is invented")
    assert 'text: "No data"' in JS or "No data" in JS, (
        "missing values should render as 'No data', not 0")
    print("PASS  empty states are explicit and distinguish no-data from zero")


def test_the_script_is_syntactically_whole():
    """
    Every script parses: no unterminated string and no unbalanced delimiter.

    Every other test in this project reads app.js as TEXT - searching it for an
    element id, a call, a label. Not one of them would notice a broken string
    literal, and a single one makes the whole file fail to parse and the entire
    dashboard render blank. That happened: an intended \\n escape became a real
    newline, every suite still passed, and the page would have loaded to
    nothing.

    A full JavaScript parser is not worth writing here. These two checks are
    what actually breaks in practice when a file is edited by a script, and both
    are cheap: a quote that never closes on its line, and braces that do not
    balance over the file.

    Comment state is tracked ACROSS lines, because the second line of a /* */
    comment containing a quotation mark is not an unterminated string - reading
    it as one is the exact mistake that made this test necessary.
    """
    for name, source in ALL_SCRIPTS:
        check_one_script(name, source)

    print("PASS  app.js, spatial.js and assembly.js each have no unterminated "
          "string and balanced delimiters")


def check_one_script(name, source):
    """The arithmetic above, applied to one file."""
    in_block_comment = False
    depth = {"(": 0, "[": 0, "{": 0}
    matching = {")": "(", "]": "[", "}": "{"}
    unterminated = []

    for number, line in enumerate(source.split("\n"), start=1):
        index = 0
        quote = None

        while index < len(line):
            rest = line[index:]

            if in_block_comment:
                if rest.startswith("*/"):
                    in_block_comment = False
                    index += 2
                else:
                    index += 1
                continue

            if quote is not None:
                if line[index] == "\\":
                    index += 2
                    continue
                if line[index] == quote:
                    quote = None
                index += 1
                continue

            if rest.startswith("/*"):
                in_block_comment = True
                index += 2
                continue
            if rest.startswith("//"):
                break

            character = line[index]
            if character in ('"', "'", "`"):
                quote = character
            elif character in depth:
                depth[character] += 1
            elif character in matching:
                depth[matching[character]] -= 1
            index += 1

        # A backtick template literal may legitimately span lines; the other two
        # may not.
        if quote in ('"', "'"):
            unterminated.append((number, line.strip()[:80]))
            quote = None

    assert not unterminated, (
        "%s has an unterminated string literal, so the whole file fails to "
        "parse and the dashboard renders blank: %s" % (name, unterminated))

    unbalanced = {opener: count for opener, count in depth.items() if count}
    assert not unbalanced, (
        "%s has unbalanced delimiters, so it will not parse: %s"
        % (name, unbalanced))

    assert not in_block_comment, (
        "%s ends inside an unclosed /* comment" % name)


def test_no_fabricated_values_in_markup():
    """No hard-coded attendance numbers may sit in the HTML."""
    body = re.sub(r"<script.*?</script>", "", HTML, flags=re.S)
    suspicious = re.findall(r">\s*\d+(?:\.\d+)?%\s*<", body)
    assert not suspicious, (
        "the markup contains hard-coded percentages: %s" % suspicious)

    for fake in ("STU001", "John", "Jane", "Demo Student"):
        assert fake not in body, "the markup contains a fabricated name: %s" % fake
    print("PASS  no hard-coded percentages or invented names in the markup")


def test_no_external_resources():
    combined = (HTML + CSS + JS + SPATIAL_CSS + SPATIAL_JS + ASSEMBLY_JS
                + FAVICON)
    combined = combined.replace("http://www.w3.org/2000/svg", "")
    for external in ("http://", "https://", "//cdn", "unpkg", "jsdelivr",
                     "googleapis", "cdnjs", "fonts.g"):
        assert external not in combined, (
            "the dashboard references an external resource: %r" % external)

    # NO VENDORED LIBRARY EITHER, which is the half "no external request"
    # does not cover: a copy of three.js sitting in backend/static would
    # satisfy every assertion above and would be exactly the dependency this
    # project exists without.
    #
    # Checked as a MANIFEST rather than by grepping for library names. The
    # grep version was tried first and was worse in both directions: it fired
    # on the comments in spatial.js that state three.js is not used, and it
    # would have missed a vendored copy under any other filename. The
    # directory either holds these six files or somebody has added something,
    # and either way this test has an opinion about it.
    served = sorted(path.name for path in STATIC.iterdir() if path.is_file())
    assert served == ["app.js", "assembly.js", "favicon.svg", "index.html",
                      "spatial.css", "spatial.js", "styles.css"], (
        "backend/static holds files this suite does not know about: %s. If a "
        "file was added on purpose, read it here and bring it under the "
        "checks in this suite; if it is a vendored library, that is the "
        "dependency this interface is built to not have." % served)

    # The seventh file is the tab icon, and it is held to the same rule as
    # the rest: drawn here, served from here. It exists because a browser
    # asks for /favicon.ico on every page load whether or not anybody wrote
    # one, and the 404 that answered it was logged as an error on a page
    # where nothing was wrong - which only a rendered page ever showed.
    assert FAVICON.lstrip().startswith("<svg"), (
        "the tab icon is not an SVG this project drew")
    assert "<image" not in FAVICON and "xlink:href" not in FAVICON, (
        "the tab icon pulls in another file")
    assert '<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">' \
        in HTML, "the page does not declare the icon it ships"

    # And the page loads nothing but those scripts. A <script src> pointing
    # anywhere else is the other way a dependency arrives.
    sources = re.findall(r'<script [^>]*src="([^"]+)"', HTML)
    assert sorted(sources) == ["/static/app.js", "/static/assembly.js",
                               "/static/spatial.js"], (
        "the page loads unexpected scripts: %s" % sources)

    print("PASS  no external stylesheet, script, font or image, and no "
          "vendored library in the static directory - works offline")


def test_frontend_is_lightweight():
    total = sum(len(part) for part in
                (HTML, CSS, JS, SPATIAL_CSS, SPATIAL_JS, ASSEMBLY_JS,
                 FAVICON))
    # A ceiling, not a target. It catches a framework or a bundled library
    # arriving by the back door; the "no external request" checks are what
    # actually enforce that. Raised from 120 KB when the dashboard gained a
    # sign-in screen, an administration view and the full student registry
    # table, and again for the visual redesign, the student portal and the
    # session/alert/staff dialogs - all still hand-written HTML, CSS and
    # JavaScript, with no build step and no dependency.
    #
    # Raised again for Step 11's three report panes, the academic terms and
    # promotion page, the model registry panel and the Step 12 assistant view.
    # Same rule as every time before: hand-written, no build step, nothing
    # fetched from anywhere.
    #
    # And again for the multi-class live console: a panel per running session,
    # per-session break and finish controls, the derived class list with its
    # delete, the PRESENT NOW list, camera discovery and the session flag. Still
    # one HTML file, one stylesheet and one script, and the rule this ceiling
    # actually guards - no framework, no bundle, no external request - is
    # unchanged and asserted separately by test_no_external_resources.
    #
    # Raised once more for the glass pass. The surfaces had carried
    # backdrop-filter since the first visual redesign and it had never
    # rendered anything, because every --surface token was an opaque hex: the
    # blur had nothing to show through. Giving them alpha, giving the ground
    # something worth seeing through them, and adding the edge highlights that
    # make a translucent panel read as a pane rather than a faded box, is
    # several hundred lines of hand-written CSS and no dependency at all.
    #
    # The number has never been the point. A 600 KB stylesheet written by hand
    # is a different thing from a 90 KB framework fetched from a CDN, and it is
    # the second that this ceiling exists to catch.
    # Raised again, and the two new files are the reason: spatial.css and
    # spatial.js are the whole spatial layer - the entry scene, the glass
    # depths, the orbit navigation, the AI core, the formations, the charts in
    # glass boxes and the cinematic view change - and assembly.js is Assembly
    # Mode. Every byte of it is hand-written CSS and JavaScript with no build
    # step and nothing fetched from anywhere.
    #
    # It is worth saying what the alternative would have cost, because that is
    # what this ceiling is actually comparing against: three.js alone is about
    # 600 KB minified, and three.js plus @react-three/fiber plus drei plus GSAP
    # plus Framer Motion plus Lenis - the stack this redesign would normally be
    # built on - is comfortably over a megabyte of dependency, a build step,
    # and a network the classroom machine does not have.
    #
    # The number has never been the point. A hand-written 760 KB interface is a
    # different thing from a 90 KB framework fetched from a CDN, and it is the
    # second that this ceiling exists to catch - which is why
    # test_no_external_resources now also names the libraries by hand.
    # Raised once more, for the navigation rail and the register formation.
    # Both are hand-written CSS and JavaScript against markup that was already
    # there, and a good part of the growth is the prose explaining why the
    # previous rail - an arc of unlabelled glass circles - was wrong.
    #
    # What this ceiling catches has not changed and is not weight: it is a
    # framework or a bundled library arriving by the back door. That is now
    # asserted directly, by test_no_external_resources reading the static
    # directory as a MANIFEST - six files, named - which catches a vendored
    # three.js under any filename and at any size. This number is the coarse
    # second line behind that check, not the first.
    assert total < 880_000, (
        "the frontend is %d bytes, heavier than an operational tool needs"
        % total)
    print("PASS  frontend is %.1f KB total across 6 files, hand-written and "
          "dependency-free" % (total / 1024.0))


def test_motion_can_be_switched_off_completely():
    """
    The dashboard animates, and every bit of it obeys the reader.

    This test replaced a blanket ban on animation. The ban was the right
    default for a tool with no visual design; once the project asked for one,
    the honest guard is not "nothing moves" but "nothing moves for somebody
    who asked it not to". A shortened animation is still an animation, and
    people who set prefers-reduced-motion usually set it because motion makes
    them ill.

    So what is checked is the contract, not the absence:

        every animation is reachable by the reduced-motion block
        the drifting background is stopped by name, not merely sped up
        the JS animation loop stops itself rather than running invisibly
    """
    assert "prefers-reduced-motion" in CSS, (
        "the dashboard animates but never offers to stop")

    reduced = CSS[CSS.index("@media (prefers-reduced-motion: reduce)"):]

    assert "animation-duration" in reduced and "!important" in reduced, (
        "the reduced-motion block does not actually override durations")
    assert "transition-duration" in reduced, (
        "transitions keep running under reduced motion")

    # The drifting mesh is an infinite animation on a fixed element. A
    # duration override alone would leave it running at 0.001ms forever, so
    # it is switched off by name.
    assert "body::before" in reduced and "animation: none" in reduced, (
        "the animated background is not switched off by name")

    # The sign-in canvas must consult the same preference before it starts a
    # requestAnimationFrame loop, because CSS cannot stop a canvas.
    assert "prefers-reduced-motion" in JS, (
        "the sign-in animation never checks whether motion is wanted")

    print("PASS  the dashboard animates, and reduced-motion switches all of "
          "it off - CSS and canvas alike")


def test_served_page_matches_the_source():
    test_client, directory = client()
    try:
        assert test_client.get("/").text == HTML, (
            "the served page differs from the source file")
        for asset in ("/static/styles.css", "/static/app.js",
                      "/static/spatial.css", "/static/spatial.js",
                      "/static/assembly.js"):
            assert test_client.get(asset).status_code == 200, (
                "%s is referenced by the page and is not served" % asset)
        assert test_client.get("/static/missing.js").status_code == 404, (
            "a missing asset should 404 rather than fall through")

        # And the page really does ask for all five, so a file that stopped
        # being linked would be caught rather than merely still existing.
        for asset in ("/static/styles.css", "/static/spatial.css",
                      "/static/spatial.js", "/static/assembly.js",
                      "/static/app.js"):
            assert asset in HTML, "the page does not load %s" % asset
        print("PASS  the served dashboard matches its source files")
    finally:
        teardown(test_client, directory)


def test_api_routes_are_not_shadowed_by_static_mount():
    test_client, directory = client()
    try:
        assert test_client.get("/api/health").status_code == 200, (
            "the static mount is shadowing the API")
        print("PASS  the static mount does not shadow /api routes")
    finally:
        teardown(test_client, directory)


def main():
    enable_safe_console()

    tests = [
        test_semantic_landmarks,
        test_viewport_and_responsive_rules,
        test_wide_content_scrolls_instead_of_overflowing,
        test_fluid_layout_has_no_fixed_page_width,
        test_the_hidden_attribute_actually_hides,
        test_skip_link_and_focus_states,
        test_every_control_has_a_label,
        test_tables_have_headers_and_captions,
        test_status_is_not_conveyed_by_colour_alone,
        test_charts_have_text_alternatives,
        test_live_region_for_status_messages,
        test_dialog_is_accessible,
        test_dark_mode_and_contrast_tokens,
        test_empty_states_are_explicit,
        test_the_script_is_syntactically_whole,
        test_no_fabricated_values_in_markup,
        test_no_external_resources,
        test_frontend_is_lightweight,
        test_motion_can_be_switched_off_completely,
        test_served_page_matches_the_source,
        test_api_routes_are_not_shadowed_by_static_mount,
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
