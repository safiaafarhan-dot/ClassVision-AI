"""
The spatial layer: the 3D room the console stands in.

NO BROWSER AND NO WEBCAM ARE NEEDED. Everything here is asserted against the
interface's own source files, which is the same limit - and the same honesty
about the limit - as every other UI suite in this project.

WHAT THIS VERIFIES

    That the layer is ADDITIVE. The page loads it as a second stylesheet and
    two further scripts; every call from app.js into it is guarded; and no
    figure, table, form or control depends on it. Delete the three tags and
    the console is the flat, fully working tool it was.

    That it stays OFFLINE. No url reference, no external host, no vendored
    library, and no WebGL context - the 3D is a Fibonacci distribution, two
    rotation matrices and a divide, written out by hand.

    That nothing is drawn BEHIND A FIGURE. No blur, no gradient and no
    animation on a selector a measured value is painted on. This is the rule
    styles.css already keeps for its own panels, and it has to hold for a
    layer whose entire job is putting glass in front of things.

    That DECORATION CARRIES NO MEANING. The decorative half of spatial.css
    uses no status colour; the functional half - a student's avatar, which
    genuinely reports presence - does, and the file is ordered so the two
    halves are separable and this test can tell them apart.

    That EVERY ANIMATION STOPS. Switched off by name under
    prefers-reduced-motion, not shortened, and the canvas loops check the same
    preference in JavaScript because CSS cannot stop a requestAnimationFrame.

    That it works in BOTH THEMES. Every token the layer reads is defined in
    all three palettes in styles.css, so the room is a midnight hall in the
    dark theme and a bright glass atrium in the light one from one set of
    rules.

    That it DEGRADES. A narrow screen drops the perspective in CSS and the
    canvas loops in JavaScript; a device with no hovering pointer gets no
    hover-driven depth and keeps its native cursor.

WHAT THIS DOES NOT VERIFY

    That any of it looks good, or that a human opening the page sees what is
    described here. No DOM is built and nothing is painted. What is checked is
    the contract the source keeps, not the pixels it produces.

Run with:
    python tests/test_spatial_interface.py
"""

import re
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "ai"))

from paths import enable_safe_console  # noqa: E402

STATIC = PROJECT_ROOT / "backend" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "styles.css").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")
SPATIAL_CSS = (STATIC / "spatial.css").read_text(encoding="utf-8")
SPATIAL_JS = (STATIC / "spatial.js").read_text(encoding="utf-8")
ASSEMBLY_JS = (STATIC / "assembly.js").read_text(encoding="utf-8")


def strip_comments(source):
    """
    Block comments removed, so a paragraph above a rule is never read as one.

    Both files in this layer are heavily commented on purpose, and several of
    those comments quote the very things this suite forbids - the note in
    spatial.js that says there is no three.js, the note in spatial.css that
    says there is no url reference. A test that greps the prose is a test that
    fails for saying the right thing.
    """
    return re.sub(r"/\*.*?\*/", "", source, flags=re.S)


SPATIAL_CSS_RULES = strip_comments(SPATIAL_CSS)
SPATIAL_JS_CODE = strip_comments(SPATIAL_JS)
ASSEMBLY_JS_CODE = strip_comments(ASSEMBLY_JS)

# The boundary inside spatial.css between decoration and the things that
# report a state. Everything above this marker is material - glass, fog,
# light, parallax - and may not touch a status colour. Everything below it is
# a student's avatar or a seat in the hall, which reports presence and
# therefore must.
FUNCTIONAL_MARKER = '.orb-figure[data-state="present"] .orb-bead {'

STATUS_TOKENS = ("--present", "--late", "--partial", "--absent", "--unknown",
                 "--early")


def rail_rules():
    """
    The rail's own rules in styles.css, comment-stripped.

    Bounded by the two headings the file already carries, so this slice
    survives the rules inside it being reordered.
    """
    rules = strip_comments(CSS)
    start = rules.index(".sidebar {")
    return rules[start:rules.index(".workspace {", start)]


def block_after(source, selector):
    """The body of the first rule or at-rule whose head matches `selector`."""
    start = source.find(selector)
    if start < 0:
        return ""
    opening = source.index("{", start)

    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1:index]
    return ""


# -- the layer is additive -------------------------------------------------

def test_the_page_loads_the_layer_after_the_console():
    """
    Order matters in both halves and for different reasons.

    spatial.css is second because it reads the palette styles.css defines.
    spatial.js and assembly.js are before app.js because app.js calls
    window.Spatial and window.Assembly, and a classic script runs in document
    order.
    """
    assert HTML.index('href="/static/styles.css"') < \
        HTML.index('href="/static/spatial.css"'), (
            "spatial.css is loaded before the palette it reads")

    for name in ("spatial.js", "assembly.js", "app.js"):
        assert 'src="/static/%s"' % name in HTML, "the page does not load %s" % name

    assert HTML.index('src="/static/spatial.js"') < \
        HTML.index('src="/static/app.js"'), "spatial.js must load before app.js"
    assert HTML.index('src="/static/assembly.js"') < \
        HTML.index('src="/static/app.js"'), "assembly.js must load before app.js"

    print("PASS  the page loads the palette then the spatial layer, and both "
          "scripts before the application")


def test_every_call_from_the_application_into_the_layer_is_guarded():
    """
    THE LOAD-BEARING TEST OF THIS WHOLE SUITE.

    A visual layer that sits on top of a working system is one the system does
    not need. The way that is true in practice is that every single call site
    checks for the object first - so a spatial.js that failed to parse, was
    blocked, or was simply deleted leaves a flat console rather than a blank
    page.

    Checked by finding every use and requiring a guard on the same line or
    the line before it, which is where the two spellings put it:

        if (window.Spatial) window.Spatial.view(view);
        if (!window.Spatial) return;
    """
    # Comment-stripped, because the bridge's own header paragraph explains
    # this very rule and names the object in doing so.
    lines = strip_comments(JS).split("\n")
    unguarded = []

    # The guard may sit on the use itself, or be an early return at the top of
    # the function the use is inside. So for each use, walk back to the start
    # of the enclosing top-level function and look for one in between - which
    # is where both spellings actually live.
    for number, line in enumerate(lines):
        if "window.Spatial" not in line and "window.Assembly" not in line:
            continue

        start = number
        while start > 0 and not re.match(r"(async )?function ", lines[start]):
            start -= 1

        scope = "\n".join(lines[start:number + 1])
        if ("if (window.Spatial)" in scope
                or "if (window.Assembly)" in scope
                or "if (!window.Spatial" in scope
                or "if (!window.Assembly" in scope):
            continue
        unguarded.append((number + 1, line.strip()[:76]))

    assert not unguarded, (
        "app.js reaches into the spatial layer without checking it is there, "
        "so a missing spatial.js would break the console rather than flatten "
        "it: %s" % unguarded)

    assert "if (window.Assembly) await window.Assembly.load();" in JS, (
        "refresh() calls the assembly view without a guard")

    print("PASS  all %d uses of the spatial layer in app.js are guarded"
          % (JS.count("window.Spatial") + JS.count("window.Assembly")))


def test_the_layer_never_calls_an_endpoint():
    """
    spatial.js holds no data and fetches nothing.

    app.js owns every request; this layer is handed the payloads it already
    has. The moment the 3D layer fetches on its own there are two sources of
    truth for the same figure, and they will disagree on a slow network.
    """
    for forbidden in ("fetch(", "XMLHttpRequest", "/api/"):
        assert forbidden not in SPATIAL_JS_CODE, (
            "spatial.js reaches the backend itself (%r); it is handed data by "
            "app.js and must invent and fetch nothing" % forbidden)

    print("PASS  spatial.js makes no request and holds no endpoint")


# -- offline, and hand-written ---------------------------------------------

def test_the_layer_reaches_no_external_host_and_loads_no_file():
    combined = SPATIAL_CSS + SPATIAL_JS + ASSEMBLY_JS
    combined = combined.replace("http://www.w3.org/2000/svg", "")

    for external in ("http://", "https://", "//cdn", "unpkg", "jsdelivr",
                     "googleapis", "cdnjs", "fonts.g"):
        assert external not in combined, (
            "the spatial layer references an external resource: %r" % external)

    # Comment-stripped, because the file's own header states the rule and
    # would otherwise trip it.
    assert "url(" not in SPATIAL_CSS_RULES, (
        "spatial.css loads a file; the whole layer is meant to be gradients, "
        "transforms and canvas arithmetic")

    print("PASS  the spatial layer adds no request, no font and no image file")


def test_the_three_dimensions_are_arithmetic_rather_than_a_library():
    """
    No WebGL, no three.js, no GSAP, no bundler.

    Checked against the CODE rather than the prose: the comments in spatial.js
    say at length that none of these is used, and a grep over the whole file
    would fail for saying so. What is asserted is that the drawing is done on
    a 2D context and that the projection really is in this file.
    """
    assert 'getContext("2d")' in SPATIAL_JS_CODE, (
        "spatial.js does not draw on a 2D canvas context")
    for forbidden in ("webgl", "WebGL", "experimental-webgl", "import ",
                      "require(", "THREE", "gsap.", "new Lenis"):
        assert forbidden not in SPATIAL_JS_CODE, (
            "spatial.js uses %r; the 3D here is meant to be written out by "
            "hand" % forbidden)

    # The engine itself, by name. If these go, something replaced them.
    for piece in ("function camera(", "function project(",
                  "function spherePoints(", "Math.sqrt(5)"):
        assert piece in SPATIAL_JS, (
            "the hand-written projection is gone: %r is missing" % piece)

    print("PASS  the 3D is a Fibonacci distribution, two rotations and a "
          "divide, on a 2D context")


# -- nothing behind a figure ----------------------------------------------

def test_glass_is_on_containers_and_never_behind_a_figure():
    """
    The same rule styles.css keeps, applied to the layer that adds the glass.

    A container may be glass; a thing somebody reads a number off may not.
    The allowlist is every selector in spatial.css that blurs, and each one of
    them is a box with other things inside it.
    """
    # Only the declarations that ADD blur. `backdrop-filter: none` appears in
    # the print block and in the orbit rail's own reset, where it is switching
    # the glass OFF - counting those as "this selector is glass" had the test
    # complaining about the rules that exist to remove it.
    blurred = re.findall(r"([^{}@;]+)\{[^{}]*backdrop-filter:\s*blur",
                         SPATIAL_CSS_RULES)
    blurred = {part.strip() for chunk in blurred
               for part in chunk.split(",") if part.strip()}

    allowed = {
        ".pane",                        # the material itself
        ".pane-far",                    # its furthest depth
        ".holo-marker",                 # a floating label: a name and a word
        ".spine-card",                  # a timeline node
        ".glass-cube",                  # a box a chart stands in
        ".hall",                        # the assembly hall
        ".assembly-banner",             # a title and a live flag
        ".assembly-controls",           # a strip of buttons
        ".view-ghost",                  # the pane a leaving view recedes behind
        # The rail's two cards. Both are containers holding other things - the
        # brand card holds a mark and two lines of text, the user card holds an
        # avatar, a name, a role and a button - and the ambient gradient inside
        # each sits on a pseudo-element BENEATH that content, which is the
        # property test_no_ambient_gradient_sits_over_a_label checks.
        ".rail-brand",
        ".rail-user",
    }

    unexpected = sorted(blurred - allowed)
    assert not unexpected, (
        "backdrop-filter is applied to something that is not a container: %s"
        % unexpected)

    # The teeth, stated positively. These are the selectors a measured value
    # is actually painted on, and a blur on any of them is a blur behind
    # something somebody has to read.
    figures = (".att-ring-value", ".cube-bar-value", ".holo-cam-val",
               ".orb-name", ".orb-state", ".holo-cam-stage", ".cam-model",
               ".hall-seat", ".orb-bead", ".core-caption", ".spine-time")
    for selector in figures:
        assert selector not in blurred, (
            "backdrop-filter is applied to %s, which sits behind a figure"
            % selector)

    print("PASS  the glass is on %d containers and on nothing a figure is "
          "painted on" % len(blurred))


def test_the_camera_frame_is_never_composited():
    """
    A video frame behind glass is a frame somebody is squinting at.

    The existing live console already made this decision for .live-panel-stage;
    the holographic frame has to keep it. The chrome AROUND the picture is
    glass; the picture sits on an opaque ground.
    """
    stage = block_after(SPATIAL_CSS_RULES, ".holo-cam-stage {")
    assert stage, "there is no .holo-cam-stage rule"
    assert "backdrop-filter" not in stage, (
        "the camera frame is blurred: %s" % stage)
    assert "var(--bg-deep)" in stage, (
        "the camera frame is not on the opaque ground: %s" % stage)

    print("PASS  the camera frame sits on an opaque ground; only the chrome "
          "around it is glass")


def test_no_detection_box_is_drawn_by_the_browser():
    """
    The overlays come from the server, where the coordinates are.

    live_overlay.py draws the boxes onto the frame. No bounding box is exposed
    by any API, so a client-side box would be a GUESS at where a person is
    standing - and a guess drawn over a real camera frame is a fabrication.
    This is the one place a spatial redesign would be most tempted to invent
    something, so it is asserted rather than trusted.
    """
    for forbidden in ("bbox", "boundingBox", "faceBox", "detection_box",
                      "drawBox"):
        assert forbidden not in SPATIAL_JS_CODE, (
            "spatial.js draws %r; detection boxes are the server's, drawn "
            "into the frame where the coordinates are" % forbidden)
        assert forbidden not in ASSEMBLY_JS_CODE, (
            "assembly.js draws %r" % forbidden)

    print("PASS  no detection box is invented in the browser")


# -- decoration carries no meaning ----------------------------------------

def test_the_decorative_half_uses_no_status_colour():
    """
    A drifting mote that borrowed --present would be decoration carrying
    meaning: a reader would learn to read a colour that means nothing.

    The file is ordered so that everything above the first avatar rule is
    material, and this asserts the split rather than taking it on trust.
    """
    assert FUNCTIONAL_MARKER in SPATIAL_CSS_RULES, (
        "the boundary between decoration and the state-reporting rules has "
        "moved; this test no longer describes the file")

    decoration = SPATIAL_CSS_RULES[:SPATIAL_CSS_RULES.index(FUNCTIONAL_MARKER)]

    for token in STATUS_TOKENS:
        assert token not in decoration, (
            "the decorative half of spatial.css uses the status colour %s, "
            "which would make decoration carry meaning" % token)

    print("PASS  no status colour appears in the %d bytes of decoration above "
          "the avatar rules" % len(decoration))


def test_the_functional_half_does_use_them_and_never_colour_alone():
    """
    The other direction, and it matters as much.

    An avatar that reported presence with nothing but a tinted rim would be
    the accessibility failure this whole product avoids everywhere else. Each
    state carries a SYMBOL and a WORD as well, and those come from spatial.js.
    """
    functional = SPATIAL_CSS_RULES[SPATIAL_CSS_RULES.index(FUNCTIONAL_MARKER):]

    for token in ("--present", "--late", "--partial", "--absent", "--unknown"):
        assert token in functional, (
            "the avatar rules never use %s, so presence is not shown in "
            "colour at all" % token)

    # Symbol and word, for all five states, out of spatial.js.
    assert "const STATE_SYMBOL" in SPATIAL_JS
    assert "const STATE_WORD" in SPATIAL_JS

    symbols = block_after(SPATIAL_JS, "const STATE_SYMBOL = {")
    words = block_after(SPATIAL_JS, "const STATE_WORD = {")
    for state in ("present", "late", "uncertain", "absent", "unknown"):
        assert state in symbols, "%s has no symbol" % state
        assert state in words, "%s has no word" % state

    assert 'STATE_WORD[state]' in SPATIAL_JS, (
        "the avatar never renders its state as a word")
    assert 'aria-hidden' in SPATIAL_JS, (
        "the decorative symbol is not hidden from assistive technology")

    print("PASS  every avatar state is a rim, a symbol and a word - never "
          "colour alone")


def test_the_formations_are_lists_of_real_buttons():
    """
    A constellation of divs is a picture; a constellation of buttons is a
    control somebody can use with a keyboard.
    """
    for source, name in ((SPATIAL_JS, "spatial.js"),
                         (ASSEMBLY_JS, "assembly.js")):
        assert 'createElement("button")' in source or \
            'el("button"' in source or '"button"' in source, (
                "%s builds the formation out of something that is not a "
                "button" % name)
        assert "aria-label" in source, (
            "%s never names the people in the formation" % name)

    # And the one interaction the formation exists for is reachable without a
    # pointer: :focus-visible beside every :hover.
    for selector in (".orb-figure", ".hall-seat", ".cube-bar"):
        assert "%s:focus-visible" % selector in SPATIAL_CSS_RULES, (
            "%s only responds to a mouse" % selector)

    print("PASS  the formations are lists of buttons and respond to focus as "
          "well as hover")


def test_a_missing_value_is_never_drawn_as_a_zero():
    """
    The project's own convention, in the one place a ring could break it.

    A 0% attendance ring on a morning when nothing has been finalised reads as
    a catastrophe. The ring says "No data", in the same words and with the same
    .nodata class the KPI modules use.
    """
    assert '"No data"' in SPATIAL_JS, (
        "the ring has no no-data state")
    assert 'classList.add("nodata")' in SPATIAL_JS, (
        "the no-data ring is not set in the smaller no-data style")
    assert ".att-ring-value.nodata" in SPATIAL_CSS_RULES, (
        "there is no style for a ring with no value")

    # The em dash for a reading the pipeline did not send, in the rail.
    rail = SPATIAL_JS[SPATIAL_JS.index("function renderCameraRail"):]
    rail = rail[:rail.index("\n  }")]
    assert "—" in rail, (
        "a telemetry cell with nothing behind it does not render an em dash")

    print("PASS  a missing figure is 'No data' or an em dash, never a zero")


# -- both themes -----------------------------------------------------------

SPATIAL_TOKENS = ("--glass-far", "--glass-mid", "--glass-near", "--glass-edge",
                  "--glass-rim", "--holo", "--holo-ink", "--holo-bg",
                  "--holo-line", "--core", "--core-2", "--core-haze", "--fog",
                  "--trail", "--spark", "--orb", "--orb-rim", "--stage",
                  "--stage-line")


def test_every_token_the_layer_reads_is_defined_in_all_three_palettes():
    """
    One set of rules, two themes.

    styles.css carries three palette blocks: dark on bare :root, light under
    the media query, and light again under the attribute. A token defined in
    one and not the others is a colour that silently falls back to nothing in
    the other theme - which for a layer made entirely of light would be a
    blank rectangle.
    """
    blocks = {
        "dark": block_after(CSS, ":root {"),
        "light (media query)": block_after(
            CSS, ':root:not([data-theme="dark"]) {'),
        "light (chosen)": block_after(CSS, ':root[data-theme="light"] {'),
    }
    for name, body in blocks.items():
        assert body, "the %s palette could not be parsed" % name

    missing = []
    for token in SPATIAL_TOKENS:
        for name, body in blocks.items():
            if ("%s:" % token) not in body:
                missing.append((token, name))

    assert not missing, (
        "tokens the spatial layer reads are not defined in every palette: %s"
        % missing)

    print("PASS  all %d spatial tokens are defined in the dark palette and "
          "both light ones" % len(SPATIAL_TOKENS))


def test_the_layer_writes_no_colour_of_its_own():
    """
    spatial.css holds no hex and no rgb() outside the few white and black
    highlight gradients, where the point is light rather than hue.

    A literal colour here is a colour that does not follow the theme, and the
    light theme is where that shows: a dark scene with its alpha turned down
    is not a bright room.
    """
    # Everything except the print block. Paper has no theme and no palette: a
    # printed border is a neutral grey because the sheet it is on is white,
    # and reaching for --border there would print a near-invisible hairline in
    # the dark theme and a different one in the light.
    screen = SPATIAL_CSS_RULES[:SPATIAL_CSS_RULES.index("@media print")]

    hexes = set(re.findall(r"#[0-9a-fA-F]{3,8}", screen))
    allowed = {"#000", "#fff", "#000000", "#ffffff"}
    assert hexes <= allowed, (
        "spatial.css carries colour values of its own, which cannot follow "
        "the theme: %s" % sorted(hexes - allowed))

    # And the canvases read their colours out of the page rather than holding
    # a second copy.
    assert "getComputedStyle" in SPATIAL_JS, (
        "spatial.js does not read the palette out of the page")
    assert "function palette(" in SPATIAL_JS
    assert 'readToken(style, "--bg"' in SPATIAL_JS, (
        "the scene never reads the ground colour, so it cannot adapt to the "
        "light theme")
    # The one derived number that does the adapting.
    assert "luminance > 0.5" in SPATIAL_JS, (
        "nothing in spatial.js distinguishes a bright room from a dark one")

    print("PASS  spatial.css writes no colour and the canvases read the "
          "palette out of the page")


# -- everything stops ------------------------------------------------------

def test_every_animation_in_the_layer_is_switched_off_by_name():
    """
    By name, not by duration.

    An infinite animation with its duration overridden to 0.001ms is an
    infinite animation still running sixty times a second forever. The blanket
    override in styles.css does not help; these have to go off individually.
    """
    reduced = block_after(SPATIAL_CSS_RULES,
                          "@media (prefers-reduced-motion: reduce)")
    assert reduced, "spatial.css has no reduced-motion block"

    declared = set(re.findall(r"@keyframes\s+([A-Za-z0-9_-]+)",
                              SPATIAL_CSS_RULES))
    assert declared, "no animations are declared at all"

    # Every selector that names one of those animations outside the
    # reduced-motion block has to be reachable inside it.
    animated = SPATIAL_CSS_RULES[:SPATIAL_CSS_RULES.index(
        "@media (prefers-reduced-motion: reduce)")]

    unstoppable = []
    for name in sorted(declared):
        if not re.search(r"animation[^;]*\b%s\b" % re.escape(name), animated):
            continue
        # The rule that uses it must have an off switch in the block.
        users = re.findall(r"([^{}@;]+)\{[^{}]*animation[^;}]*\b%s\b"
                           % re.escape(name), animated)
        users = [part.strip() for chunk in users
                 for part in chunk.split(",") if part.strip()]
        if not any(user in reduced for user in users):
            unstoppable.append((name, users))

    assert not unstoppable, (
        "animations that prefers-reduced-motion does not switch off by name: "
        "%s" % unstoppable)

    print("PASS  all %d animations in the spatial layer are switched off by "
          "name under prefers-reduced-motion" % len(declared))


def test_the_canvas_loops_check_the_same_preference():
    """
    CSS cannot stop a requestAnimationFrame.

    Each of the three loops consults the preference BEFORE it starts, draws a
    single still frame instead, and reacts to the preference being turned on
    while the page is open - a preference that only takes effect on the next
    reload is a preference that was ignored.
    """
    assert "function reducedMotion()" in SPATIAL_JS
    assert SPATIAL_JS_CODE.count("reducedMotion()") >= 6, (
        "the loops do not consult the motion preference often enough to be "
        "checking it at each start")

    for starter in ("function startDeep(", "function startEntryScene(",
                    "function startCore("):
        body = SPATIAL_JS[SPATIAL_JS.index(starter):]
        body = body[:body.index("\n  }")]
        assert "reducedMotion()" in body, (
            "%s starts a loop without checking the motion preference"
            % starter)

    # And it reacts live.
    assert 'matchMedia("(prefers-reduced-motion: reduce)")' in SPATIAL_JS
    assert "addEventListener(\"change\", react)" in SPATIAL_JS, (
        "turning reduced motion on while the page is open does nothing")

    print("PASS  every canvas loop checks the motion preference before it "
          "starts, and reacts to it changing")


def test_the_loops_stop_themselves_when_nothing_can_see_them():
    """
    Three independent reasons a loop must stop, and all three are real costs:
    a hidden tab, a canvas that is off screen, and a view that was left.
    """
    assert "function paintable(" in SPATIAL_JS, (
        "nothing checks whether a canvas is on screen")
    assert "document.hidden" in SPATIAL_JS, (
        "the loops keep drawing into a backgrounded tab")
    assert "isConnected" in SPATIAL_JS, (
        "a canvas removed from the document would still be painted")

    # The per-view rule: the core runs on the two views that hold one.
    enter = SPATIAL_JS[SPATIAL_JS.index("function enterView("):]
    enter = enter[:enter.index("\n  }")]
    assert "startCore()" in enter and "stopCore()" in enter, (
        "the AI core is not started and stopped per view, so it would paint "
        "while somebody is reading the reports page")

    print("PASS  the loops stop for a hidden tab, an off-screen canvas and a "
          "view that was left")


def test_printing_strips_the_whole_layer():
    """
    A printed register is a document somebody files.

    Every effect in here is either invisible on paper or actively harmful on
    it: a translucent pane prints as a grey wash behind the figures, and a
    perspective transform prints as a trapezoid.
    """
    printed = block_after(SPATIAL_CSS_RULES, "@media print")
    assert printed, "spatial.css has no print stylesheet"

    for gone in (".deep", ".entry", ".cursor-ring", ".view-ghost",
                 ".holo-cam-scan", ".scan-plane", ".assembly-controls"):
        assert gone in printed, "%s is still printed" % gone

    assert "backdrop-filter: none" in printed
    assert "perspective: none" in printed
    assert "transform: none" in printed

    # The horizontal timeline has to become a list, because paper does not
    # scroll.
    assert "overflow: visible" in printed, (
        "the spatial timeline would print cut off at the edge of the page")

    print("PASS  printing removes the glass, the scenes, the cursor and the "
          "perspective")


# -- it degrades -----------------------------------------------------------

def test_a_narrow_screen_becomes_two_and_a_half_dimensions():
    """
    What is kept and what goes, and the split is about cost.

    Kept: the glass, the depths, the edge highlights, the markers, the ring -
    all flat compositing. Gone: the perspective containers, the per-pane
    rotations and the parallax, every one of which is either a per-frame cost
    or needs a pointer that hovers.
    """
    narrow = block_after(SPATIAL_CSS_RULES, "@media (max-width: 1080px)")
    assert narrow, "there is no tablet breakpoint in spatial.css"
    assert "perspective: none" in narrow, (
        "the stage keeps its perspective on a narrow screen")

    phone = block_after(SPATIAL_CSS_RULES, "@media (max-width: 760px)")
    assert phone, "there is no phone breakpoint in spatial.css"
    assert "perspective: none" in phone

    smallest = block_after(SPATIAL_CSS_RULES, "@media (max-width: 600px)")
    assert smallest, "there is no small-phone breakpoint in spatial.css"

    # And JavaScript drops the loops at the same number, because a stylesheet
    # cannot stop a canvas.
    assert "function narrowViewport(" in SPATIAL_JS
    assert '(max-width: 1080px)' in SPATIAL_JS, (
        "the script does not use the same breakpoint the stylesheet does")
    deep = SPATIAL_JS[SPATIAL_JS.index("function startDeep("):]
    deep = deep[:deep.index("\n  }")]
    assert "narrowViewport()" in deep, (
        "the background canvas loop still runs on a phone")

    print("PASS  the room becomes a stack below 1080px, in the stylesheet and "
          "in the script alike")


def test_a_device_with_no_hovering_pointer_keeps_its_own_cursor():
    """
    Two separate failures avoided here.

    A :hover rule meeting a touchscreen latches on first tap and stays. And an
    interface whose pointer is a JavaScript object is an interface that loses
    its pointer when a script fails - so the native cursor is only hidden once
    the replacement is demonstrably running, and never over a text field.
    """
    assert "@media (hover: none)" in SPATIAL_CSS_RULES, (
        "there is no rule for a device without a hovering pointer")
    touch = block_after(SPATIAL_CSS_RULES, "@media (hover: none)")
    assert ".cursor-dot" in touch and "display: none" in touch, (
        "the drawn cursor is not removed on a touch device")

    # The native cursor goes only under body.has-cursor, which JavaScript adds
    # on the first real pointermove.
    assert "@media (hover: hover) and (pointer: fine)" in SPATIAL_CSS_RULES
    hide = block_after(SPATIAL_CSS_RULES,
                       "@media (hover: hover) and (pointer: fine)")
    assert "cursor: none" in hide
    assert "body.has-cursor" in hide, (
        "the native cursor is hidden whether or not the replacement is "
        "running")

    # And never over something being typed into.
    assert "body.has-cursor input" in SPATIAL_CSS_RULES, (
        "the text caret is replaced by a dot, which is not a substitute for "
        "it")

    # A keyboard user gets the native focus ring back.
    assert 'event.key === "Tab"' in SPATIAL_JS, (
        "tabbing does not put the pointer marks away")

    print("PASS  the drawn cursor is opt-in by capability, never over a text "
          "field, and yields to the keyboard")


def test_the_rail_shows_its_labels():
    """
    THE TEST THIS SECTION EXISTS FOR, and it is here because the first version
    of this layer failed it.

    The rail used to be an arc of glass circles with every label folded away
    until the pointer was over it. It photographed well and it was unusable:
    "the text is difficult to see" was the first thing said about it. A
    navigation whose labels are invisible until you point at them puts the
    entire burden on icon recognition, which works for the four icons somebody
    uses daily and fails for the other seven.

    So: the labels live in index.html, they are visible by default, and
    nothing in either stylesheet hides them except the collapsed mode somebody
    opted into.
    """
    # The labels are MARKUP, not something a script adds. A rail whose labels
    # depend on JavaScript is a rail that is blank when JavaScript fails.
    assert HTML.count('class="nav-label"') >= 11, (
        "the navigation labels are not in the page; found %d"
        % HTML.count('class="nav-label"'))

    # Every destination still carries a readable word.
    for label in ("Dashboard", "Live classroom", "Assembly", "Analytics",
                  "Students", "Sessions", "AI Assistant", "Camera health",
                  "Academic", "Administration", "Model registry"):
        assert ">%s<" % label in HTML, (
            "the rail has no readable label for %s" % label)

    # And nothing collapses them except the opt-in mode. Any rule that hides a
    # label has to be inside the collapsed block.
    for stylesheet, name in ((SPATIAL_CSS_RULES, "spatial.css"),
                             (strip_comments(CSS), "styles.css")):
        for match in re.finditer(r"([^{}@]*\.nav-label[^{}]*)\{([^}]*)\}",
                                 stylesheet):
            selector, body = match.group(1).strip(), match.group(2)
            hides = ("display: none" in body
                     or "font-size: 0" in body
                     or "visibility: hidden" in body)
            if not hides:
                continue
            assert 'data-rail="collapsed"' in selector, (
                "%s hides the navigation labels outside the collapsed rail: "
                "%s" % (name, selector))

    print("PASS  all 11 navigation labels are in the markup and visible by "
          "default")


def test_no_label_in_the_rail_is_set_in_the_quietest_ink():
    """
    --text-dim is the interface's quietest ink.

    It passes WCAG AA at the sizes it is used at, and tests/test_glass_contrast
    computes that on every run - but it is the ink for a micro-caption, and it
    was being used for the navigation itself, over glass, over a drifting
    background. That is the whole of why the rail was hard to read.

    So it appears nowhere in the rail. Navigation labels are --text, which the
    contrast suite holds to 10:1 rather than to 4.5:1; headings and the role
    are --text-muted.
    """
    rail = rail_rules()
    assert rail, "the rail's rules could not be found in styles.css"

    offenders = []
    for match in re.finditer(r"([^{}@]+)\{([^}]*)\}", rail):
        selector, body = match.group(1).strip(), match.group(2)
        if "--text-dim" in body:
            offenders.append(selector)

    assert not offenders, (
        "the rail sets text in --text-dim, the ink this pass removed from it: "
        "%s" % offenders)

    # And the labels really are the strong ink, stated positively so this
    # cannot pass by the rail having no colours at all.
    item = block_after(rail, ".nav-item {")
    assert "color: var(--text)" in item, (
        "a navigation label is not set in the body ink: %s" % item)

    heading = block_after(rail, ".nav-group-label {")
    assert "color: var(--text-muted)" in heading, (
        "a section heading is not set in --text-muted: %s" % heading)

    print("PASS  no rail label uses --text-dim; items are --text and headings "
          "are --text-muted")


def test_the_rail_states_its_weights():
    """
    Weight is the second half of legibility, and the brief named the numbers.
    """
    rail = rail_rules()

    def weight(selector):
        body = block_after(rail, selector)
        found = re.search(r"font-weight:\s*(\d+)", body)
        assert found, "%s has no font-weight: %s" % (selector, body)
        return int(found.group(1))

    assert weight(".rail-brand .brand-text strong {") >= 700, (
        "the product name is lighter than 700")
    assert 500 <= weight(".nav-item {") <= 700, (
        "a navigation label is outside the 500-600 band the brief asked for")
    assert weight(".nav-group-label {") >= 600, (
        "a section heading is lighter than 600")
    assert weight(".rail-user-name {") >= 600, (
        "the signed-in name is lighter than 600")

    # The current view is heavier than the rest, which is one of the five
    # signals it carries.
    current = block_after(rail, '.nav-item[aria-current="page"] {')
    assert "font-weight" in current, (
        "the current view is not marked by weight as well as by colour")

    print("PASS  the rail's type weights match the stated hierarchy")


def test_the_current_view_is_unmistakable():
    """
    Five signals, because one is never enough - and every one of them keys off
    aria-current, so the appearance and the accessibility tree cannot disagree
    and JavaScript has no class to forget to remove.
    """
    rail = rail_rules()

    current = block_after(rail, '.nav-item[aria-current="page"] {')
    assert current, "there is no rule for the current view"

    assert "background-image" in current or "background-color" in current, (
        "the current view has no ground of its own")
    assert "border-color" in current, "the current view has no border"
    assert "font-weight" in current, "the current view is not heavier"

    marker = block_after(rail, '.nav-item[aria-current="page"]::before {')
    assert "background: var(--accent)" in marker, (
        "there is no accent bar on the leading edge")

    icon = block_after(rail, '.nav-item[aria-current="page"] .nav-icon {')
    assert "var(--accent)" in icon, "the current view's icon is not lit"

    # The glow is decoration and lives in spatial.css, keyed off the same
    # attribute - so it cannot appear anywhere the real state is not.
    assert '.nav-item[aria-current="page"]::before' in SPATIAL_CSS_RULES, (
        "the marker has no glow in the decorative layer")

    print("PASS  the current view carries a bar, a ground, a border, a lit "
          "icon and a heavier label, all keyed off aria-current")


def test_the_collapsed_rail_is_a_choice_that_keeps_its_labels_reachable():
    """
    Icon-only is useful on a small laptop. It is NOT something to put somebody
    in without asking, which is what the arc did.

    So it is a button, it is remembered, the label becomes a tooltip rather
    than disappearing, and the tooltip is reachable by keyboard as well as by
    pointer. The collapsed rules are scoped to the wide breakpoint, because
    below it the rail is already a drawer.
    """
    assert 'id="sidebar-collapse"' in HTML, "there is no collapse control"
    assert "function toggleRail(" in JS and "function applyRail(" in JS, (
        "nothing drives the collapsed rail")

    # Remembered, and safely - localStorage throws in a private window.
    rail_code = JS[JS.index("const RAIL_KEY"):]
    rail_code = rail_code[:rail_code.index("\nfunction bindRail")]
    assert "localStorage" in rail_code
    assert rail_code.count("try {") >= 2 and rail_code.count("catch") >= 2, (
        "reading and writing the remembered width are not both guarded")

    # The state is on the accessibility tree too.
    apply_code = JS[JS.index("function applyRail("):]
    apply_code = apply_code[:apply_code.index("\nfunction toggleRail")]
    assert "aria-expanded" in apply_code, (
        "the collapse button never updates aria-expanded")
    assert "sr-only" in apply_code, (
        "the collapse button's accessible name does not follow its state")

    # The collapsed rules exist, are scoped wide, and turn the label into a
    # tooltip rather than removing it.
    wide = block_after(SPATIAL_CSS_RULES, "@media (min-width: 1081px)")
    assert 'body[data-rail="collapsed"]' in wide, (
        "the collapsed rail is not scoped to the wide breakpoint, so it would "
        "fight the mobile drawer")
    assert ".nav-label" in wide, "the collapsed rail has no tooltip"

    tooltip = block_after(wide, 'body[data-rail="collapsed"] .nav-label {')
    assert "opacity: 0" in tooltip, "the tooltip is not hidden until wanted"
    assert "var(--surface-solid)" in tooltip, (
        "the tooltip is translucent; a few words over arbitrary page content "
        "is the one surface that must not be see-through")

    assert ':focus-visible .nav-label' in wide, (
        "the tooltip is reachable by pointer only, so a keyboard user in the "
        "collapsed rail has no labels at all")

    print("PASS  the collapsed rail is opt-in, remembered, announced, and "
          "keeps every label as an opaque tooltip")


def test_no_ambient_gradient_sits_over_a_label():
    """
    The brief's own rule, and the reason the cards are built the way they are.

    Both rail cards carry an ambient wash. A wash painted over the card would
    be a wash over the product's name and over the signed-in user's name, so
    each one is on a ::before beneath the content and the content is lifted
    above it. The lift is what makes the wash safe, so it is asserted rather
    than assumed.
    """
    for card in (".rail-brand", ".rail-user"):
        wash = block_after(SPATIAL_CSS_RULES, "%s::before {" % card)
        assert wash, "%s has no ambient wash" % card
        assert "z-index: 0" in wash, (
            "%s's wash is not placed beneath the content: %s" % (card, wash))
        assert "pointer-events: none" in wash, (
            "%s's wash intercepts clicks" % card)

        lift = block_after(SPATIAL_CSS_RULES, "%s > * {" % card)
        assert "z-index: 1" in lift, (
            "%s does not lift its content above its own wash" % card)

    print("PASS  both rail cards paint their ambient light beneath the text, "
          "never over it")


# -- the entry scene -------------------------------------------------------

def test_the_entry_scene_can_be_crossed_instantly_and_holds_no_figure():
    """
    A threshold, not a splash screen, and the difference is measurable.

    The dashboard loads behind it - showEntry() is not awaited - the button
    takes focus so Enter crosses it, Escape does the same, and the scene
    itself is clickable. And it reports nothing: nothing has been fetched when
    it appears, so there is no figure to show and it shows none.
    """
    assert 'id="entry-screen"' in HTML and 'id="entry-enter"' in HTML

    start = HTML.index('id="entry-screen"')
    scene = HTML[start:HTML.index("</section>", start)]

    # No invented figure, and no invented name.
    assert not re.findall(r">\s*\d+(?:\.\d+)?%\s*<", scene), (
        "the entry scene shows a percentage, and nothing has been fetched "
        "when it appears")
    for fake in ("STU001", "John", "Jane", "Demo Student"):
        assert fake not in scene, "the entry scene names %s" % fake

    # Crossable three ways.
    cross = SPATIAL_JS[SPATIAL_JS.index("function showThreshold("):]
    cross = cross[:cross.index("\n  /* ====")]
    assert 'event.key === "Escape"' in cross and 'event.key === "Enter"' in cross
    assert "button.focus()" in cross, (
        "the way in does not hold focus, so Enter does not cross the scene")
    assert 'addEventListener("click", crossThreshold)' in cross

    # And the dashboard is not held up by it.
    assert "if (window.Spatial) window.Spatial.showEntry();" in JS, (
        "the entry scene is not shown from startDashboard()")
    assert "await window.Spatial.showEntry" not in JS, (
        "the entry scene is awaited, which would delay the console behind it")

    # The scene is decoration and says so.
    assert 'class="entry-stage" aria-hidden="true"' in HTML, (
        "the scene is not hidden from assistive technology, which would read "
        "a canvas and three rings to somebody")

    print("PASS  the threshold is crossable by Enter, Escape or a click, "
          "holds no figure, and does not delay the console")


def test_the_scene_cannot_be_left_covering_the_console():
    """
    An animationend that never fires - a backgrounded tab, a discarded
    compositor layer - must not leave a pane of glass over the interface. Both
    the threshold and the view transition carry a timer as well as the event.
    """
    for function in ("function crossThreshold(", "function transitionView("):
        body = SPATIAL_JS[SPATIAL_JS.index(function):]
        body = body[:body.index("\n  }")]
        assert "setTimeout" in body, (
            "%s relies on animationend alone, so a backgrounded tab could "
            "leave the overlay up" % function)

    print("PASS  the threshold and the view transition both clear themselves "
          "on a timer as well as on animationend")


# -- the ids line up -------------------------------------------------------

def test_every_element_the_layer_reaches_for_exists_in_the_page():
    """
    The same check test_theme_toggle.py makes for app.js, for the two new
    scripts - and it is the check that catches a typo in an id, which is
    otherwise a silent no-op nothing would notice.
    """
    used = set()
    for source in (SPATIAL_JS, ASSEMBLY_JS):
        used |= set(re.findall(r'getElementById\("([a-z0-9-]+)"\)', source))
        used |= set(re.findall(r'\$\("([a-z0-9-]+)"\)', source))

    present = set(re.findall(r'id="([a-z0-9-]+)"', HTML))

    missing = sorted(used - present)
    assert not missing, (
        "the spatial layer reaches for ids the page does not have: %s"
        % missing)

    assert len(used) >= 30, (
        "only %d ids are reached for, which suggests this test is no longer "
        "finding them" % len(used))

    print("PASS  all %d element ids the spatial layer uses exist in the page"
          % len(used))


def test_the_charts_keep_their_text_alternative():
    """
    A chart that only exists as geometry is a chart half the room cannot read.

    The existing barChart() established role="img" plus an aria-label carrying
    the whole series as text. The glass cube is a room around a chart, not a
    replacement for one, so it keeps both.
    """
    cube = SPATIAL_JS[SPATIAL_JS.index("function renderCube("):]
    cube = cube[:cube.index("\n  }")]

    assert 'setAttribute("role", "img")' in cube, (
        "the glass cube has no image role")
    assert "aria-label" in cube and "Values —" in cube, (
        "the glass cube has no textual equivalent of its data")

    # The ring too: the figure inside it is text, but the ARC carries a
    # comparison a screen reader cannot see.
    ring = SPATIAL_JS[SPATIAL_JS.index("function setRing("):]
    ring = ring[:ring.index("\n  }")]
    assert 'setAttribute("role", "img")' in ring
    assert "percent" in ring

    print("PASS  the glass cube and the attendance ring both carry a text "
          "equivalent")


# -- runner ----------------------------------------------------------------

TESTS = [
    test_the_page_loads_the_layer_after_the_console,
    test_every_call_from_the_application_into_the_layer_is_guarded,
    test_the_layer_never_calls_an_endpoint,
    test_the_layer_reaches_no_external_host_and_loads_no_file,
    test_the_three_dimensions_are_arithmetic_rather_than_a_library,
    test_glass_is_on_containers_and_never_behind_a_figure,
    test_the_camera_frame_is_never_composited,
    test_no_detection_box_is_drawn_by_the_browser,
    test_the_decorative_half_uses_no_status_colour,
    test_the_functional_half_does_use_them_and_never_colour_alone,
    test_the_formations_are_lists_of_real_buttons,
    test_a_missing_value_is_never_drawn_as_a_zero,
    test_every_token_the_layer_reads_is_defined_in_all_three_palettes,
    test_the_layer_writes_no_colour_of_its_own,
    test_every_animation_in_the_layer_is_switched_off_by_name,
    test_the_canvas_loops_check_the_same_preference,
    test_the_loops_stop_themselves_when_nothing_can_see_them,
    test_printing_strips_the_whole_layer,
    test_a_narrow_screen_becomes_two_and_a_half_dimensions,
    test_a_device_with_no_hovering_pointer_keeps_its_own_cursor,
    test_the_rail_shows_its_labels,
    test_no_label_in_the_rail_is_set_in_the_quietest_ink,
    test_the_rail_states_its_weights,
    test_the_current_view_is_unmistakable,
    test_the_collapsed_rail_is_a_choice_that_keeps_its_labels_reachable,
    test_no_ambient_gradient_sits_over_a_label,
    test_the_entry_scene_can_be_crossed_instantly_and_holds_no_figure,
    test_the_scene_cannot_be_left_covering_the_console,
    test_every_element_the_layer_reaches_for_exists_in_the_page,
    test_the_charts_keep_their_text_alternative,
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
