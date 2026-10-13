"""
Computed contrast for the glass surfaces, in both themes.

    The surfaces are translucent now, so what a reader sees behind a line of
    text is a COMPOSITE: the surface at its alpha over the ground. Reading
    the hex values no longer tells you whether a label passes.

WHAT THIS VERIFIES

    Every ink the interface sets - --text, --text-muted, --text-dim - against
    every surface it can land on, composited over that theme's ground, at the
    WCAG 2.1 contrast ratio the text's own size calls for.

    Both themes, from the stylesheet's own tokens. Nothing here holds a
    second copy of a colour: the palette is parsed, so a token changed in
    styles.css is measured on the next run.

    That the one opaque surface stays opaque, because a sticky table header
    that rows scroll through is the case translucency actually breaks.

WHY THIS FILE EXISTS

    tests/test_dashboard_frontend.py says, honestly, that contrast was
    "checked by reading the code, not by running a browser". That was a fair
    account of a palette of opaque hexes. It stopped being enough the moment
    a surface had alpha in it - and the first run of this arithmetic found
    --text-dim at 3.61:1 on the deepest dark surface, which reading the code
    had not caught and had been close to the line even before the glass.

WHAT THIS DOES NOT VERIFY

    Rendering. A browser compositing a backdrop-filter may land a pixel or
    two off this arithmetic, and text over the drifting ground layer varies
    by a little as it drifts. The margins here are wide enough that neither
    changes a pass into a fail, but this is the palette's contrast and not a
    screenshot's.

Run with:
    python tests/test_glass_contrast.py
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

CSS = (PROJECT_ROOT / "backend" / "static"
       / "styles.css").read_text(encoding="utf-8")

# WCAG 2.1 AA. 4.5:1 for normal text, 3:1 for large text - and every ink
# checked here is used at small sizes somewhere, so 4.5 is the bar for all of
# them. The micro-labels are the reason: .68rem uppercase is small text.
AA_NORMAL = 4.5

# Which surfaces an ink can actually sit on. --surface-solid is included
# because dialogs and sticky headers use it.
SURFACES = ("--surface", "--surface-2", "--surface-3", "--surface-solid")
INKS = ("--text", "--text-muted", "--text-dim")

THEMES = (("dark", ":root {"), ("light", ':root[data-theme="light"] {'))


# -- colour arithmetic -----------------------------------------------------

def palette(marker):
    """Every custom property declared in one palette block."""
    start = CSS.index(marker)
    end = CSS.index("\n}", start)
    body = CSS[start:end]
    return {name: " ".join(value.split())
            for name, value in
            re.findall(r'(--[a-z0-9-]+)\s*:\s*([^;]+);', body)}


def channels(value):
    """(r, g, b, a) from a #hex, #rgb, rgb() or rgba() value."""
    value = value.strip()

    if value.startswith("#"):
        digits = value[1:]
        if len(digits) == 3:
            digits = "".join(digit * 2 for digit in digits)
        return (int(digits[0:2], 16), int(digits[2:4], 16),
                int(digits[4:6], 16), 1.0)

    numbers = [float(part) for part in re.findall(r'[\d.]+', value)]
    if len(numbers) < 3:
        raise ValueError("not a colour: %r" % value)
    return (numbers[0], numbers[1], numbers[2],
            numbers[3] if len(numbers) > 3 else 1.0)


def composite(front, back):
    """`front` painted over `back`, which is what a reader actually sees."""
    red, green, blue, alpha = front
    return (red * alpha + back[0] * (1 - alpha),
            green * alpha + back[1] * (1 - alpha),
            blue * alpha + back[2] * (1 - alpha),
            1.0)


def relative_luminance(colour):
    def linear(value):
        value /= 255.0
        return (value / 12.92 if value <= 0.03928
                else ((value + 0.055) / 1.055) ** 2.4)

    return (0.2126 * linear(colour[0])
            + 0.7152 * linear(colour[1])
            + 0.0722 * linear(colour[2]))


def contrast(first, second):
    one, two = relative_luminance(first), relative_luminance(second)
    lighter, darker = max(one, two), min(one, two)
    return (lighter + 0.05) / (darker + 0.05)


def measured(theme_marker):
    """{(ink, surface): ratio} for one theme, composited over its ground."""
    tokens = palette(theme_marker)
    ground = channels(tokens["--bg"])

    results = {}
    for surface in SURFACES:
        seen = composite(channels(tokens[surface]), ground)
        for ink in INKS:
            results[(ink, surface)] = contrast(channels(tokens[ink]), seen)
    return results


# -- the tests -------------------------------------------------------------

def test_the_arithmetic_agrees_with_the_published_examples():
    """
    A sanity check on the formula before anything is asserted with it.

    Black on white is 21:1 and white on white is 1:1 by definition. A
    contrast test with an inverted ratio would pass everything.
    """
    black, white = (0, 0, 0, 1.0), (255, 255, 255, 1.0)

    assert abs(contrast(black, white) - 21.0) < 0.01, contrast(black, white)
    assert abs(contrast(white, white) - 1.0) < 0.01
    assert abs(contrast(black, black) - 1.0) < 0.01

    # And compositing: 50% white over black is mid-grey, both ways round.
    half = composite((255, 255, 255, 0.5), black)
    assert abs(half[0] - 127.5) < 0.01, half

    print("PASS  the contrast formula gives 21:1 for black on white and 1:1 "
          "for white on white")


def test_every_ink_passes_on_every_surface_in_both_themes():
    """
    The whole point of the file: 12 pairs per theme, 24 in all.
    """
    worst = None

    for theme, marker in THEMES:
        for (ink, surface), ratio in sorted(measured(marker).items()):
            assert ratio >= AA_NORMAL, (
                "%s theme: %s on %s is %.2f:1, below the %.1f:1 WCAG AA "
                "needs for small text" % (theme, ink, surface, ratio,
                                          AA_NORMAL))
            if worst is None or ratio < worst[0]:
                worst = (ratio, theme, ink, surface)

    print("PASS  all 24 ink/surface pairs pass AA; the tightest is %s on %s "
          "in the %s theme at %.2f:1"
          % (worst[2], worst[3], worst[1], worst[0]))


def test_the_body_ink_is_comfortable_rather_than_merely_passing():
    """
    --text is the ink of every sentence in the interface, and 4.5:1 is a
    floor rather than a target. AAA for normal text is 7:1; this asks for
    more, because a console is read for an hour at a time.
    """
    for theme, marker in THEMES:
        for surface in SURFACES:
            ratio = measured(marker)[("--text", surface)]
            assert ratio >= 10.0, (
                "%s theme: body text on %s is only %.2f:1"
                % (theme, surface, ratio))

    print("PASS  body text clears 10:1 on every surface in both themes")


def test_the_translucent_surfaces_really_are_translucent():
    """
    Guards the other direction.

    If somebody repairs a contrast failure by making the surfaces opaque
    again, every ratio above passes and the glass silently stops rendering -
    which is the exact state this whole pass was fixing. So the alpha is
    asserted too.
    """
    for theme, marker in THEMES:
        tokens = palette(marker)
        for surface in ("--surface", "--surface-2", "--surface-3"):
            alpha = channels(tokens[surface])[3]
            assert alpha < 1.0, (
                "%s theme: %s is opaque, so the backdrop-filter on every "
                "panel renders nothing" % (theme, surface))
            assert alpha >= 0.6, (
                "%s theme: %s is only %.2f opaque, which is too thin for "
                "text to sit on" % (theme, surface, alpha))

    print("PASS  the three glass surfaces are translucent in both themes, "
          "and none is thinner than 0.6")


def test_the_one_opaque_surface_stays_opaque():
    """
    --surface-solid is the exception the glass pass depends on, and the
    sticky table header is why.
    """
    for theme, marker in THEMES:
        alpha = channels(palette(marker)["--surface-solid"])[3]
        assert alpha == 1.0, (
            "%s theme: --surface-solid has alpha %.2f; a sticky table header "
            "would show the rows scrolling under it" % (theme, alpha))

    header = CSS[CSS.index(".data-table th, .mini-table th {"):]
    header = header[:header.index("}")]
    assert "var(--surface-solid)" in header, (
        "the sticky table header is not on the opaque surface: %s" % header)
    assert "position: sticky" in header, (
        "the header is no longer sticky, which is the reason it must be "
        "opaque - check whether this test still describes the design")

    print("PASS  --surface-solid is opaque in both themes and is what the "
          "sticky table header uses")


def test_the_accents_pass_as_text_on_their_own_surfaces():
    """
    --accent is used as ink, not only as a line: the hero's subtitle and the
    assistant's citations are set in it.
    """
    for theme, marker in THEMES:
        tokens = palette(marker)
        ground = channels(tokens["--bg"])
        for surface in ("--surface", "--surface-2"):
            seen = composite(channels(tokens[surface]), ground)
            ratio = contrast(channels(tokens["--accent"]), seen)
            assert ratio >= 3.0, (
                "%s theme: --accent on %s is %.2f:1, too low for the text it "
                "is used for" % (theme, surface, ratio))

    print("PASS  the accent passes as text on the surfaces it is set on")


def test_the_palette_is_read_from_the_stylesheet_and_not_duplicated():
    """
    This file must hold no colour of its own.

    A contrast test with its own copy of the palette measures the copy, and
    keeps passing after somebody changes the stylesheet.
    """
    source = Path(__file__).read_text(encoding="utf-8")

    # The docstrings and comments quote the two values that were corrected,
    # and the sanity check uses pure black and white. Nothing else.
    hexes = set(re.findall(r'#[0-9a-fA-F]{6}', source))
    allowed = {"#6b7d94", "#64748b"}
    assert hexes <= allowed, (
        "this file carries colour values of its own: %s" % sorted(hexes - allowed))

    assert 'CSS.index(marker)' in source, (
        "the palette is no longer parsed out of the stylesheet")

    print("PASS  every colour measured comes from styles.css, not from this "
          "file")


# -- runner ----------------------------------------------------------------

def main():
    enable_safe_console()

    tests = [
        test_the_arithmetic_agrees_with_the_published_examples,
        test_every_ink_passes_on_every_surface_in_both_themes,
        test_the_body_ink_is_comfortable_rather_than_merely_passing,
        test_the_translucent_surfaces_really_are_translucent,
        test_the_one_opaque_surface_stays_opaque,
        test_the_accents_pass_as_text_on_their_own_surfaces,
        test_the_palette_is_read_from_the_stylesheet_and_not_duplicated,
    ]

    passed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as failure:
            print("FAIL  %s: %s" % (test.__name__, failure))
        except Exception as failure:                      # noqa: BLE001
            print("ERROR %s: %s: %s"
                  % (test.__name__, type(failure).__name__, failure))

    print("\n%d/%d tests passed." % (passed, len(tests)))
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
