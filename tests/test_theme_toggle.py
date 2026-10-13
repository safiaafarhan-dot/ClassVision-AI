"""
Light, dark, and the depth layer that has to survive both.

NO WEBCAM, NO NETWORK AND NO DATABASE. This suite reads the three static files
and checks the properties a stylesheet can be wrong about without anybody
noticing until a teacher complains that the numbers are grey on grey.

WHAT THIS VERIFIES
    That there are genuinely THREE theme states - follow the machine, always
    light, always dark - and that an explicit choice beats the machine's
    setting. A teacher on a laptop set to dark who is working next to a window
    has asked for light and must get it.

    That the light palette, which necessarily appears twice (CSS cannot attach
    one declaration block to both a media query and a plain selector), has not
    DRIFTED. The two copies are parsed and compared property by property. This
    is the test the duplication was accepted on the strength of.

    That every token the dark palette defines is also defined by the light one,
    so no colour silently falls back to a dark value on a white background.

    That the theme is applied before the first paint, and that nothing about it
    is stored on the server - a theme belongs to the machine somebody is
    sitting at, not to their account.

    And that the depth layer keeps its contracts: no status colour is added by
    it, everything that moves is listed under prefers-reduced-motion, and the
    page still reaches no external host.

WHAT THIS DOES NOT VERIFY
    Measured contrast ratios. Checking those needs a renderer; what is checked
    here is that text never sits on a fully transparent surface and that the
    light palette redefines every colour rather than inheriting one.

Run with:
    python tests/test_theme_toggle.py
"""

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "ai"))

from paths import enable_safe_console  # noqa: E402

STATIC = PROJECT_ROOT / "backend" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "styles.css").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")

# The stylesheet with its comments removed.
#
# Needed wherever SELECTORS are parsed. This file is heavily commented on
# purpose, and a paragraph of prose sitting above a rule is picked up as part of
# that rule's selector by any regex simple enough to be worth writing - which is
# how the glass test below first "found" backdrop-filter applied to a sentence.
CSS_RULES = re.sub(r'/\*.*?\*/', '', CSS, flags=re.S)


# -- helpers ----------------------------------------------------------------

def block_after(text, selector, start=0):
    """
    The declarations of the first rule whose selector line matches, brace
    matched rather than regex-guessed so a nested block cannot end it early.
    """
    index = text.find(selector, start)
    if index < 0:
        return None, -1

    open_brace = text.index("{", index)
    depth = 0
    for position in range(open_brace, len(text)):
        if text[position] == "{":
            depth += 1
        elif text[position] == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace + 1:position], position
    return None, -1


def declarations(block):
    """
    Custom properties in a declaration block, as {name: value}.

    Only --custom-properties, because those are the palette. Ordinary
    declarations are not what the two light blocks are being compared on.
    """
    if not block:
        return {}

    found = {}
    for name, value in re.findall(r'(--[a-z0-9-]+)\s*:\s*([^;]+);', block):
        found[name] = " ".join(value.split())
    return found


def reduced_motion_block():
    block, _ = block_after(CSS, "@media (prefers-reduced-motion: reduce)")
    assert block, "there is no prefers-reduced-motion block"
    return block


# -- three states -----------------------------------------------------------

def test_there_are_three_theme_states_and_auto_is_the_default():
    assert 'id="theme-auto"' in HTML, "there is no follow-the-system option"
    assert 'id="theme-light"' in HTML, "there is no light option"
    assert 'id="theme-dark"' in HTML, "there is no dark option"

    # Auto is the one checked in the markup, so a first visit follows the
    # machine rather than imposing a choice nobody made.
    auto = HTML[HTML.index('id="theme-auto"'):HTML.index('id="theme-light"')]
    assert 'aria-checked="true"' in auto, (
        "Auto is not the option checked by default")

    light = HTML[HTML.index('id="theme-light"'):HTML.index('id="theme-dark"')]
    assert 'aria-checked="false"' in light

    assert 'role="radiogroup"' in HTML, (
        "the three options are not grouped as mutually exclusive, so a screen "
        "reader announces them as three independent switches")

    print("PASS  there are three theme states, grouped as one choice, with "
          "follow-the-system as the default")


def test_auto_removes_the_attribute_rather_than_resolving_it():
    """
    The whole design rests on this. "Auto" must remove data-theme so the media
    queries in the stylesheet keep doing the work - which is how a machine that
    switches to dark at sunset follows, with no listener, and how the page
    behaves correctly with the script disabled.
    """
    apply_block = JS[JS.index("function applyTheme"):]
    apply_block = apply_block[:apply_block.index("\nfunction bindTheme")]

    assert "removeAttribute" in apply_block, (
        "Auto does not remove data-theme, so it must be resolving the system "
        "setting in JavaScript - which stops following it")
    assert 'setAttribute("data-theme"' in apply_block

    # And it must NOT consult matchMedia to resolve auto into a value.
    assert "matchMedia" not in apply_block, (
        "applyTheme resolves the system setting itself instead of leaving it "
        "to the media queries")

    print("PASS  Auto removes the attribute and leaves the system setting to "
          "the media queries, so it keeps following")


def test_an_explicit_choice_beats_the_machines_setting():
    """
    Every light media-query block must exclude an explicit dark choice, or a
    teacher who asked for dark on a machine set to light gets light.
    """
    offenders = []
    position = 0
    while True:
        index = CSS.find("@media (prefers-color-scheme: light)", position)
        if index < 0:
            break
        block, end = block_after(CSS, "@media (prefers-color-scheme: light)",
                                 position)
        assert block is not None
        # Every selector inside must be guarded.
        for selector in re.findall(r'([^{}]+)\{', block):
            selector = selector.strip()
            if not selector or selector.startswith("--"):
                continue
            if ':root:not([data-theme="dark"])' not in selector:
                offenders.append(selector)
        position = end + 1

    assert not offenders, (
        "a light media-query rule is not guarded against an explicit dark "
        "choice, so choosing Dark on a light machine would not work: %s"
        % offenders)

    # And the mirror: an explicit dark choice has a rule of its own to win with.
    assert ':root[data-theme="dark"]' in CSS, (
        "there is no rule for an explicitly chosen dark theme")
    assert ':root[data-theme="light"]' in CSS, (
        "there is no rule for an explicitly chosen light theme")

    print("PASS  every light media-query rule stands aside for an explicit "
          "dark choice, and both explicit choices have rules of their own")


# -- the duplication that had to be accepted --------------------------------

def test_the_two_light_palettes_have_not_drifted():
    """
    The test the duplicated palette was accepted on the strength of.

    CSS cannot attach one declaration block to both a media query and a plain
    selector, so the light tokens appear twice. Here they are parsed and
    compared, property by property, so the copies cannot diverge unnoticed.
    """
    from_media, _ = block_after(CSS, ':root:not([data-theme="dark"]) {')
    from_attribute, _ = block_after(CSS, ':root[data-theme="light"] {')

    media_tokens = declarations(from_media)
    attribute_tokens = declarations(from_attribute)

    assert media_tokens, "the light media-query palette could not be parsed"
    assert attribute_tokens, "the light attribute palette could not be parsed"

    only_media = sorted(set(media_tokens) - set(attribute_tokens))
    only_attribute = sorted(set(attribute_tokens) - set(media_tokens))

    assert not only_media, (
        "these tokens are in the light media-query palette but not the "
        "explicit one: %s" % only_media)
    assert not only_attribute, (
        "these tokens are in the explicit light palette but not the "
        "media-query one: %s" % only_attribute)

    different = {name: (media_tokens[name], attribute_tokens[name])
                 for name in media_tokens
                 if media_tokens[name] != attribute_tokens[name]}
    assert not different, (
        "the two light palettes disagree about these tokens: %s" % different)

    print("PASS  the two copies of the light palette declare the same %d "
          "tokens with the same values" % len(media_tokens))


def test_light_redefines_every_colour_the_dark_palette_sets():
    """
    A token the light palette forgets keeps its DARK value on a white page,
    which is how a dashboard ends up with near-black text on near-black.
    """
    dark, _ = block_after(CSS, ":root {")
    light, _ = block_after(CSS, ':root[data-theme="light"] {')

    dark_tokens = declarations(dark)
    light_tokens = declarations(light)

    assert dark_tokens, "the dark palette could not be parsed"

    # Tokens that are not colours are legitimately shared: a radius, a duration
    # and an easing curve do not change between themes.
    shared = {"--radius", "--radius-sm", "--blur", "--ease", "--fast",
              "--slow"}

    missing = sorted(name for name in dark_tokens
                     if name not in light_tokens and name not in shared)

    assert not missing, (
        "the light palette does not redefine these, so they keep their dark "
        "values on a white background: %s" % missing)

    print("PASS  the light palette redefines all %d colour tokens the dark "
          "palette sets" % (len(dark_tokens) - len(shared & set(dark_tokens))))


# -- when it is applied, and where it is kept -------------------------------

def test_the_theme_is_applied_before_the_first_paint():
    head = HTML[:HTML.index("</head>")]

    assert "<script>" in head, (
        "the saved theme is not applied before the first paint, so a chosen "
        "light theme flashes dark on every load")
    assert "classvision-theme" in head, (
        "the head script does not read the saved theme")
    assert "data-theme" in head

    # It must be tiny and self-contained: no reference to app.js's helpers,
    # which have not loaded yet.
    inline = head[head.index("<script>"):head.index("</script>")]
    assert "applyTheme" not in inline, (
        "the head script calls a function that has not loaded yet")
    assert "try" in inline and "catch" in inline, (
        "the head script does not guard localStorage, which throws rather "
        "than returning nothing in a private window on some browsers")

    print("PASS  the saved theme is applied in <head> before the first paint, "
          "guarded, and without depending on app.js")


def test_the_theme_is_never_sent_to_the_server():
    """
    A theme belongs to the machine somebody is sitting at, not to their
    account: the same teacher wants different answers on the projector laptop
    and on their own screen.
    """
    theme_code = JS[JS.index("const THEME_KEY"):]
    theme_code = theme_code[:theme_code.index("\nfunction percent")]

    for forbidden in ("apiSend", "fetch(", '"/api/'):
        assert forbidden not in theme_code, (
            "the theme code talks to the server (%r)" % forbidden)

    assert "localStorage" in theme_code

    # And no endpoint exists for it.
    assert "theme" not in JS[JS.index("async function api"):
                             JS.index("async function api") + 400], (
        "the API helper knows about themes")

    print("PASS  the theme is kept in localStorage only and never sent to the "
          "server")


def test_a_theme_that_cannot_be_saved_does_not_break_the_page():
    theme_code = JS[JS.index("const THEME_KEY"):]
    theme_code = theme_code[:theme_code.index("\nfunction percent")]

    # Both the read and the write must be guarded, not just one.
    assert theme_code.count("try {") >= 2, (
        "reading and writing the saved theme are not both guarded; "
        "localStorage throws in a private window on some browsers")
    assert theme_code.count("catch") >= 2

    # An unknown stored value must fall back rather than being set as an
    # attribute nothing styles.
    assert "THEMES.indexOf" in theme_code, (
        "a stored value is trusted without being checked against the three "
        "known states")

    print("PASS  a theme that cannot be read or written falls back to Auto "
          "instead of failing the page")


# -- the depth layer keeps the old contracts --------------------------------

def test_every_moving_thing_in_the_depth_layer_can_be_switched_off():
    reduced = reduced_motion_block()

    # Everything the depth layer transforms on hover.
    for selector in (".panel:hover", ".cards .card:hover",
                     ".theme-switch button:hover"):
        assert selector in reduced, (
            "%s moves and is not switched off under prefers-reduced-motion"
            % selector)

    assert "transform: none" in reduced

    # The perspective container too: a 3D container with no rotation renders
    # identically to a flat one, so this costs nothing and removes the tilt.
    assert ".cards { perspective: none; }" in reduced, (
        "the cards keep their perspective, so they would still tilt")

    print("PASS  every hover transform the depth layer adds is switched off "
          "under prefers-reduced-motion")


def test_the_depth_layer_adds_no_status_colour():
    # Stripped of comments: the paragraph above these rules explains what they
    # must not do and names the tokens in doing so.
    depth = CSS_RULES[CSS_RULES.index(".theme-switch {"):]

    # It may USE the status tokens nowhere - a rule that set --present or drew
    # with it would be adding meaning to decoration.
    for token in ("--present", "--late", "--partial", "--absent", "--unknown",
                  "--early"):
        assert token not in depth, (
            "the depth layer references the status colour %s, which would "
            "make decoration carry meaning" % token)

    print("PASS  the depth layer uses no status colour, so no meaning is "
          "carried by decoration")


def test_glass_is_applied_to_chrome_and_not_behind_a_number():
    # Comment-stripped, so a paragraph above a rule is not read as a selector.
    depth = CSS_RULES[CSS_RULES.index(".theme-switch {"):]

    blurred = re.findall(r'([^{}@;]+)\{[^{}]*backdrop-filter', depth)
    blurred = [part.strip() for chunk in blurred
               for part in chunk.split(",") if part.strip()]

    # Only chrome - meaning a CONTAINER, never a figure.
    #
    # The rule has not changed and the list has. Four surfaces were added
    # when the glass became real: until then every --surface token was an
    # opaque hex, so backdrop-filter rendered nothing anywhere and the list
    # was describing an intention rather than an effect.
    #
    #   .sidebar        the rail. Its old comment said deliberately not
    #                   glass, because nothing sat behind it and a blur would
    #                   cost a composite on every scroll. Both halves stopped
    #                   being true - the ground gained a drifting layer, and
    #                   the rail is sticky and painted once.
    #   .live-panel     a classroom module. Chrome around a camera, and the
    #                   camera is NOT glass: .live-panel-stage keeps an opaque
    #                   --bg-deep so a video frame is never composited.
    #   .hero           states what the product is and holds no figure. The
    #                   numbers near it are the KPI modules below it.
    #   .status-module  a status container; its own word and symbol sit on it.
    #
    # Every one of those is a box with other things inside it. What stays
    # forbidden is the half of the rule that matters, and it is asserted
    # positively below rather than left to the absence of a selector.
    allowed = {".panel", ".card", ".empty", "dialog", ".auth-card",
               ".topbar", "dialog::backdrop",
               ".sidebar", ".live-panel", ".hero", ".status-module"}
    unexpected = sorted(set(blurred) - allowed)

    assert not unexpected, (
        "backdrop-filter is applied to something that is not chrome: %s"
        % unexpected)

    # The teeth. These are the selectors a figure is actually drawn on, and a
    # blur on any of them is a blur behind something somebody has to read.
    forbidden = (".data-table", ".mini-table", ".metric-value",
                 ".telemetry-val", ".live-panel-telemetry",
                 ".live-panel-stage", ".live-panel-frame", ".card .value",
                 ".live-value", ".bar-value", ".seat")
    for selector in forbidden:
        assert selector not in blurred, (
            "backdrop-filter is applied to %s, which sits behind a figure"
            % selector)

    print("PASS  the glass is on %d containers and on nothing that sits "
          "behind a figure" % len(set(blurred)))


def test_printing_strips_the_decoration():
    block, _ = block_after(CSS, "@media print")
    assert block, "there is no print stylesheet"

    assert "backdrop-filter: none" in block, (
        "a translucent panel over a gradient prints as a grey wash behind the "
        "numbers")
    assert "box-shadow: none" in block
    assert "body::before { display: none; }" in block, (
        "the drifting mesh is still printed")

    # The controls a printed register has no use for.
    for hidden in (".tabs", ".theme-switch", ".filters", ".pager"):
        assert hidden in block, "%s is still printed" % hidden

    # And the tables must not be cut off by their scroll container.
    assert "overflow: visible" in block

    print("PASS  printing drops the glass, the mesh and the controls, and "
          "lets the tables print in full")


def test_the_depth_layer_reaches_no_external_host():
    combined = (HTML + CSS + JS).replace("http://www.w3.org/2000/svg", "")
    for external in ("http://", "https://", "//cdn", "unpkg", "jsdelivr",
                     "googleapis", "cdnjs", "fonts.g", "url(http"):
        assert external not in combined, (
            "the dashboard references an external resource: %r" % external)

    # No image file either: every gradient and edge is CSS the browser has.
    assert "url(" not in CSS, (
        "the stylesheet loads a file; the depth layer is meant to be gradients "
        "and transforms only")

    print("PASS  the theme and the depth layer add no request, no font and no "
          "image file")


def test_the_switch_states_its_choice_in_more_than_colour():
    switch, _ = block_after(CSS, ".theme-switch button[aria-checked=\"true\"]")
    assert switch, "the chosen theme has no style of its own"

    # aria-checked is the state, so the selector keying off it means the
    # accessibility tree and the appearance cannot disagree.
    assert "font-weight" in switch or "box-shadow" in switch, (
        "the chosen theme is marked by colour alone")

    apply_block = JS[JS.index("function applyTheme"):]
    apply_block = apply_block[:apply_block.index("\nfunction bindTheme")]
    assert "aria-checked" in apply_block, (
        "the switch never updates aria-checked, so a screen reader cannot tell "
        "which theme is selected")

    print("PASS  the selected theme is marked by weight and outline as well "
          "as colour, and announced through aria-checked")


def test_every_element_the_theme_code_uses_exists_in_the_page():
    used = set(re.findall(r'\$\("([a-z0-9-]+)"\)', JS))
    present = set(re.findall(r'id="([a-z0-9-]+)"', HTML))

    missing = sorted(used - present)
    assert not missing, "app.js reaches for ids the page does not have: %s" \
        % missing

    for element in ("theme-auto", "theme-light", "theme-dark", "theme-switch"):
        assert element in present, "the page has no #%s" % element

    print("PASS  all %d element ids the script uses exist in the page, the "
          "three theme buttons included" % len(used))


# -- runner -----------------------------------------------------------------

def main():
    enable_safe_console()

    tests = [
        test_there_are_three_theme_states_and_auto_is_the_default,
        test_auto_removes_the_attribute_rather_than_resolving_it,
        test_an_explicit_choice_beats_the_machines_setting,
        test_the_two_light_palettes_have_not_drifted,
        test_light_redefines_every_colour_the_dark_palette_sets,
        test_the_theme_is_applied_before_the_first_paint,
        test_the_theme_is_never_sent_to_the_server,
        test_a_theme_that_cannot_be_saved_does_not_break_the_page,
        test_every_moving_thing_in_the_depth_layer_can_be_switched_off,
        test_the_depth_layer_adds_no_status_colour,
        test_glass_is_applied_to_chrome_and_not_behind_a_number,
        test_printing_strips_the_decoration,
        test_the_depth_layer_reaches_no_external_host,
        test_the_switch_states_its_choice_in_more_than_colour,
        test_every_element_the_theme_code_uses_exists_in_the_page,
    ]

    failures = 0
    for test in tests:
        try:
            test()
        except AssertionError as error:
            failures += 1
            print("FAIL  %s: %s" % (test.__name__, error))
        except Exception as error:                       # noqa: BLE001
            failures += 1
            print("ERROR %s: %s: %s" % (test.__name__, type(error).__name__,
                                        error))

    print("\n%d/%d tests passed." % (len(tests) - failures, len(tests)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
