"""The dashboard has to be readable, and readable is a number, not an opinion.

Reported from real use: the dashboard is hard to read. The obvious suspect was
``ui.dim_color`` (``#123B18``, 1.59:1 against the page) being used as a text
colour - but it is not. Its only two uses in the stylesheet are the scanline
gradient, which is texture, not text.

The real cause is ``opacity``. ``.dim`` paints the product green at 45%, and the
browser composites that against the page to an effective ``#25772E`` - 3.59:1,
where body text needs 4.5:1.

**That is why this file resolves opacity instead of just reading the declared
colours.** Every hex literal in the stylesheet passes on its own; a checker that
only looked at those would report a clean bill of health for a page nobody can
read. The bug is in what the browser draws, so that is what gets measured.

Ratios are WCAG 2.1: 4.5:1 for body text, 3:1 for large text (>=18.66px bold or
>=24px). The dashboard's base size is 19px regular, which is *not* large text by
that definition, so 4.5:1 is the bar here.
"""

import re
from pathlib import Path

import pytest

#: The stylesheet used to live inside ``webui.py``, in two copies, which is why
#: this file once found ``.empty``, ``.mark`` and ``.tiny`` listed twice. It now
#: lives in one module that both pages are served from, so that is what gets
#: read. Nothing about what counts as a failure changed with the move.
STYLESHEET = Path(__file__).resolve().parent.parent / "retrobox" / "webstyle.py"

#: WCAG AA for body text. Not negotiable downwards - if something cannot meet it,
#: the colour changes, not this number.
AA_BODY = 4.5


# ==========================================================================
# The maths, straight from the WCAG definition
# ==========================================================================
def _channel(value: float) -> float:
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def luminance(rgb) -> float:
    r, g, b = (_channel(v / 255) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def parse_hex(text: str):
    text = text.lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))


def contrast(foreground, background) -> float:
    high, low = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def composite(foreground, background, alpha: float):
    """What the browser actually paints for a partly transparent foreground."""
    return tuple(round(alpha * f + (1 - alpha) * b) for f, b in zip(foreground, background))


def test_the_maths_agrees_with_the_published_examples():
    # Black on white is the canonical 21:1. If this drifts, every other number
    # in this file is worthless, so it is checked before anything else is.
    assert contrast((0, 0, 0), (255, 255, 255)) == pytest.approx(21.0, abs=0.01)
    assert contrast((255, 255, 255), (255, 255, 255)) == pytest.approx(1.0, abs=0.01)


# ==========================================================================
# What the stylesheet actually says
# ==========================================================================
@pytest.fixture(scope="module")
def stylesheet() -> str:
    return STYLESHEET.read_text(encoding="utf-8")


def custom_property(css: str, name: str) -> str:
    """The value of a CSS custom property, however it is spliced together.

    The stylesheet is built in Python, so a property is often concatenated from
    a module constant rather than written as a literal hex. Look for the literal
    first, then fall back to the constant it is built from.
    """
    direct = re.search(rf"--{name}\s*:\s*(#[0-9a-fA-F]{{3,6}})\b", css)
    if direct:
        return direct.group(1)
    constant = {
        "green": "GREEN", "dim": "DIM", "bg": "BG",
        # The three foreground tiers are spliced from the same palette
        # constants, so they are never literals in the CSS either.
        "rb-fg": "GREEN", "rb-fg-muted": "MUTED", "rb-line": "DIM",
    }.get(name)
    if constant:
        built = re.search(rf'^{constant}\s*=\s*"(#[0-9a-fA-F]{{3,6}})"', css, re.M)
        if built:
            return built.group(1)
    raise AssertionError(f"could not find --{name} in the stylesheet")


def page_background(css: str):
    return parse_hex(custom_property(css, "bg"))


# ==========================================================================
# The colours, as declared
# ==========================================================================
def test_the_primary_green_is_comfortably_readable(stylesheet):
    green = parse_hex(custom_property(stylesheet, "green"))
    assert contrast(green, page_background(stylesheet)) >= AA_BODY


def test_every_colour_used_for_text_passes(stylesheet):
    """Any hex that appears in a `color:` declaration has to clear AA.

    This is the check that is easy to write and easy to be fooled by - it is
    necessary, not sufficient. See the opacity tests below for the one that
    actually catches the reported fault.
    """
    background = page_background(stylesheet)
    failures = []
    for hex_value in set(re.findall(r"color\s*:\s*(#[0-9a-fA-F]{6})\b", stylesheet)):
        ratio = contrast(parse_hex(hex_value), background)
        if ratio < AA_BODY:
            failures.append(f"{hex_value} is {ratio:.2f}:1")
    assert not failures, "text colours below AA: " + ", ".join(sorted(failures))


# ==========================================================================
# The one that matters: what the browser composites
# ==========================================================================
def dimmed_text_rules(css: str):
    """Every rule that FADES text with `opacity`, and the alpha it uses.

    ``opacity:1`` is not a fade - it is the default, written out to cancel an
    animation or a hover. Counting it was a real bug in this file: once the
    readability pass replaced every genuine fade with the muted tier, the only
    rule left was ``#toast.show { opacity:1 }``, and the compositing test below
    went green while measuring a colour at full strength. It passed, and it was
    testing nothing. Only alphas below 1 are fades.
    """
    found = []
    for match in re.finditer(r"\.([A-Za-z][\w-]*)\s*\{([^}]*)\}", css):
        name, body = match.group(1), match.group(2)
        alpha = re.search(r"opacity\s*:\s*([0-9.]+)", body)
        if alpha and float(alpha.group(1)) < 1.0:
            found.append((name, float(alpha.group(1))))
    return found


def test_text_is_no_longer_faded_with_opacity_at_all(stylesheet):
    """The readability pass removed every fade, and that must stay true.

    Fading the product green is how the dashboard became unreadable: eleven
    classes composited between 2.57:1 and 4.25:1 against a 4.5:1 bar. The fix
    was to stop fading and use a muted colour that passes on its own, so the
    honest assertion now is that no fade exists - not that one does.

    ``test_faded_text_is_still_readable_after_compositing`` stays alongside
    this as the guard for the day somebody reintroduces one: it measures what
    the browser would actually paint, and it will fail on a fade that does not
    clear AA. This test is what stops that one going quietly dormant again.
    """
    fades = dimmed_text_rules(stylesheet)
    assert not fades, (
        "text is being faded with opacity again, which is what made this "
        "dashboard unreadable in the first place: "
        + ", ".join(f".{name} at {alpha}" for name, alpha in sorted(fades))
    )


def test_faded_text_is_still_readable_after_compositing(stylesheet):
    """The reported bug, as a number.

    `.dim { opacity:.45 }` over the product green composites to #25772E, which
    is 3.59:1 - below the 4.5:1 a sentence needs. Labels, help text and
    placeholders are all painted this way, which is precisely the text people
    said they could not read.
    """
    background = page_background(stylesheet)
    green = parse_hex(custom_property(stylesheet, "green"))

    failures = []
    for name, alpha in dimmed_text_rules(stylesheet):
        effective = composite(green, background, alpha)
        ratio = contrast(effective, background)
        if ratio < AA_BODY:
            painted = "#" + "".join(f"{c:02X}" for c in effective)
            failures.append(
                f".{name} at opacity {alpha} paints {painted} = {ratio:.2f}:1"
            )
    assert not failures, (
        "faded text below AA once the browser composites it: "
        + "; ".join(sorted(failures))
    )


# ==========================================================================
# The dim value is texture, not text
# ==========================================================================
def test_the_dim_value_is_never_used_to_paint_text(stylesheet):
    """`#123B18` is 1.59:1. It draws borders and unlit segments, and that is all.

    It was designed as the unlit half of a volume bar, which is a fine job for
    it. The moment it is used for a word, that word is unreadable.
    """
    dim = custom_property(stylesheet, "dim")
    assert contrast(parse_hex(dim), page_background(stylesheet)) < 3, (
        "the dim value got brighter; this test's premise needs rechecking"
    )
    for match in re.finditer(r"(?<!-)\bcolor\s*:\s*([^;}]+)", stylesheet):
        value = match.group(1)
        assert "--dim" not in value and dim.lower() not in value.lower(), (
            f"the dim value is painting text: color:{value.strip()}"
        )


# ==========================================================================
# Reading the stylesheet as rules, not as a wall of text
# ==========================================================================
def _selector(blob: str) -> str:
    """The selector out of everything that precedes a ``{``.

    The stylesheet is a Python module, so what sits in front of an opening
    brace is the module docstring, a ``return \"\"\"``, a comment, or the tail
    of the previous rule. The selector is the last line of it - plus any lines
    immediately above that end in a comma, which is how a selector list is
    wrapped.
    """
    blob = re.sub(r"/\*.*?\*/", " ", blob, flags=re.S)
    lines = [line.strip() for line in blob.splitlines() if line.strip()]
    if not lines:
        return ""
    picked = [lines[-1]]
    above = len(lines) - 2
    while above >= 0 and lines[above].endswith(","):
        picked.insert(0, lines[above])
        above -= 1
    selector = " ".join(picked)
    # Drop the Python that opens the string the CSS is written inside.
    return selector.rsplit('"""', 1)[-1].strip()


def css_rules(css: str):
    """Every ``selector { ... }`` pair in the module, in source order.

    ``[^{}]`` on both sides means a nested at-rule yields its inner rule and
    drops the ``@media`` wrapper, which is what these tests want: they ask
    what a selector declares, not which query it sits under.
    """
    return [(_selector(m.group(1)), m.group(2))
            for m in re.finditer(r"([^{}]*)\{([^{}]*)\}", css)]


def rules_for(css: str, selector: str):
    """Every rule whose selector list names ``selector`` exactly."""
    pattern = re.compile(rf"(^|,)\s*{re.escape(selector)}\s*(,|$)")
    return [body for sel, body in css_rules(css) if pattern.search(sel)]


def declaration(body: str, name: str):
    """The value of one declaration, or ``None`` if the rule does not set it."""
    found = re.search(rf"(?<![\w-]){re.escape(name)}\s*:\s*([^;}}]+)", body)
    return found.group(1).strip() if found else None


def lengths_in_px(value: str):
    """Every length in a declaration, in CSS pixels. ``rem`` is root-relative,
    and the root here is the browser default of 16px - nothing changes it."""
    return [float(n) * (16 if unit == "rem" else 1)
            for n, unit in re.findall(r"([0-9.]+)(rem|px)\b", value)]


# ==========================================================================
# The three foreground tiers
# ==========================================================================
def test_the_three_foreground_tiers_are_declared(stylesheet):
    """Foreground comes from exactly three named values, and each is what it
    claims to be: two that can be read, and one that must never hold a word."""
    background = page_background(stylesheet)
    fg = parse_hex(custom_property(stylesheet, "rb-fg"))
    muted = parse_hex(custom_property(stylesheet, "rb-fg-muted"))
    line = parse_hex(custom_property(stylesheet, "rb-line"))

    assert contrast(fg, background) >= AA_BODY
    assert contrast(muted, background) >= AA_BODY, (
        "the muted tier is what every label and every piece of help text is "
        "painted in; if it drops below AA the readability fix is undone"
    )
    assert contrast(line, background) < 3, (
        "--rb-line got brighter, which means it now looks like a text colour "
        "and somebody will use it as one"
    )


def test_the_comment_beside_the_tiers_states_the_measured_ratios(stylesheet):
    """The numbers in the comment have to be the numbers, not a memory of them.

    A comment that says 8.39:1 next to a value that no longer measures 8.39:1
    is worse than no comment, because the next person believes it.
    """
    background = page_background(stylesheet)
    for name in ("rb-fg", "rb-fg-muted", "rb-line"):
        ratio = contrast(parse_hex(custom_property(stylesheet, name)), background)
        assert f"{ratio:.2f}:1" in stylesheet, (
            f"--{name} measures {ratio:.2f}:1 but the stylesheet does not say so"
        )


def test_the_muted_tier_is_what_replaced_the_opacity_dimming(stylesheet):
    """Labels, small print and help text are painted, not faded.

    This is the positive half of ``test_faded_text_is_still_readable_after_
    compositing``: that one says no rule may fade text below AA, this one says
    the reason no rule does is that there is a readable colour for the job.
    """
    painted = re.findall(r"color\s*:\s*var\(--rb-fg-muted\)", stylesheet)
    assert len(painted) >= 10, (
        f"only {len(painted)} rules use the muted tier; the eleven classes "
        "that used to fade text with opacity should be using it"
    )


def test_the_line_tier_never_paints_text_either(stylesheet):
    """``--rb-line`` is the same 1.59:1 value as ``--dim``, under a name that
    says what it is for. Naming it did not make it readable."""
    for match in re.finditer(r"(?<!-)\bcolor\s*:\s*([^;}]+)", stylesheet):
        value = match.group(1)
        assert "--rb-line" not in value, (
            f"the line tier is painting text: color:{value.strip()}"
        )


# ==========================================================================
# Type: which face, and how big
# ==========================================================================
def test_body_copy_is_not_set_in_the_display_face(stylesheet):
    """VT323 is the product's voice, not its body copy.

    It is a 1970s terminal face at a single weight; a paragraph set in it is
    hard work. It stays for the wordmark, the headings, the tab labels and the
    big numbers - the things that are meant to feel like a channel banner.
    """
    families = [declaration(body, "font-family") for body in rules_for(stylesheet, "body")]
    families = [family for family in families if family]
    assert families, "no body rule sets a font at all"
    for family in families:
        assert "var(--rb-sans)" in family, (
            f"body copy is set in {family!r}; it should be the system sans stack"
        )
        assert "VT323" not in family


def test_the_display_face_still_dresses_the_things_that_carry_the_brand(stylesheet):
    """A legibility pass that took the wordmark's face away would be a redesign."""
    for selector in ("h1", "h2", ".mark", "nav button", ".led"):
        bodies = rules_for(stylesheet, selector)
        assert bodies, f"{selector} has no rule at all"
        for body in bodies:
            family = declaration(body, "font-family")
            assert family and "var(--rb-display)" in family, (
                f"{selector} lost the display face"
            )


def test_the_log_viewer_is_set_in_a_real_monospace(stylesheet):
    """Log output is columns. It needs a font where the columns line up, and
    the display face is not one - it is a terminal face by looks only."""
    for body in rules_for(stylesheet, ".raw"):
        family = declaration(body, "font-family")
        assert family and "var(--rb-mono)" in family


def test_no_font_downloads_anything(stylesheet):
    """The box may have no internet. A page that waits on a font is a page
    nobody can read."""
    assert "@font-face" not in stylesheet
    assert "@import" not in stylesheet
    for word in ("http://", "https://", ".woff", ".ttf", "fonts.googleapis"):
        assert word not in stylesheet, f"the stylesheet reaches for {word}"


def test_nothing_is_set_smaller_than_fourteen_pixels(stylesheet):
    """Below 14px this stops being small print and starts being decoration."""
    too_small = []
    for match in re.finditer(r"font-size\s*:\s*([0-9.]+)(rem|px)", stylesheet):
        px = float(match.group(1)) * (16 if match.group(2) == "rem" else 1)
        if px < 14:
            too_small.append(f"{match.group(0)} = {px:g}px")
    assert not too_small, "text below 14px: " + ", ".join(sorted(set(too_small)))


def test_form_controls_are_at_least_sixteen_pixels(stylesheet):
    """Under 16px, iOS Safari zooms the whole page the moment a field takes
    focus, and the customer is left scrolling sideways to find the button."""
    checked = 0
    for selector, body in css_rules(stylesheet):
        if not re.search(r"\b(input|select|textarea)\b", selector):
            continue
        for match in re.finditer(r"font-size\s*:\s*([0-9.]+)(rem|px)", body):
            px = float(match.group(1)) * (16 if match.group(2) == "rem" else 1)
            assert px >= 16, f"{selector} sets {match.group(0)} = {px:g}px"
            checked += 1
    assert checked, "no rule sets a font size on a form control at all"


def test_anything_read_as_a_sentence_has_room_to_breathe(stylesheet):
    """Line height of at least 1.5 on running text. Display sizes are exempt:
    a 2.6rem channel number is one line and wants to be tight."""
    for selector, body in css_rules(stylesheet):
        height = declaration(body, "line-height")
        if height is None:
            continue
        size = declaration(body, "font-size")
        if size and lengths_in_px(size) and lengths_in_px(size)[0] > 20:
            continue
        assert float(height) >= 1.5, f"{selector} sets line-height:{height}"


# ==========================================================================
# Native controls, which were entirely unstyled
# ==========================================================================
def test_both_range_sliders_are_drawn_by_this_stylesheet(stylesheet):
    """The HOW CURVED slider rendered as a blue thumb on a grey track.

    Both sliders are built in JavaScript, so grepping the markup for them
    finds nothing - which is exactly how they went unstyled for so long.
    """
    track_and_thumb = (
        "::-webkit-slider-runnable-track",
        "::-webkit-slider-thumb",
        "::-moz-range-track",
        "::-moz-range-thumb",
    )
    for pseudo in track_and_thumb:
        assert pseudo in stylesheet, f"nothing styles {pseudo}"

    ranges = [body for sel, body in css_rules(stylesheet)
              if "input[type=range]" in sel and "::" not in sel]
    assert ranges, "there is no rule for input[type=range]"
    assert any("appearance:none" in body.replace(" ", "") for body in ranges), (
        "the slider still renders the way the operating system draws it"
    )


def test_the_slider_thumb_is_still_big_enough_to_drag(stylesheet):
    """A thumb shrunk for looks is a thumb a thumb cannot catch."""
    for selector, body in css_rules(stylesheet):
        if "thumb" not in selector:
            continue
        for axis in ("width", "height"):
            value = declaration(body, axis)
            if value is None:
                continue
            assert lengths_in_px(value)[0] >= 22, (
                f"{selector} sets {axis}:{value}, too small to drag"
            )


def test_the_closed_select_is_drawn_in_the_product_colours(stylesheet):
    """Only the closed control - the open list belongs to the operating system
    and cannot be styled, which is why ``color-scheme`` is set instead."""
    selects = [body for sel, body in css_rules(stylesheet)
               if re.search(r"(^|,)\s*select\s*(,|$)", sel)]
    assert any("appearance:none" in body.replace(" ", "") for body in selects), (
        "the select still renders as the operating system draws it"
    )
    assert "color-scheme" in stylesheet, (
        "without color-scheme the open list, the scrollbars and the date "
        "pickers come back as light-on-light"
    )


def test_checkboxes_and_radios_are_the_product_colour(stylesheet):
    assert re.search(r"input\[type=(checkbox|radio)\]", stylesheet), (
        "nothing styles a checkbox or a radio"
    )


def test_there_is_a_focus_ring_and_it_is_not_the_browser_default(stylesheet):
    """The default is a blue halo, which on this page reads as a fault."""
    rings = [(sel, body) for sel, body in css_rules(stylesheet)
             if ":focus-visible" in sel and re.match(
                 r"\s*[0-9.]+px", declaration(body, "outline") or "")]
    assert rings, "nothing draws a focus ring"
    assert any(sel == ":focus-visible" for sel, _ in rings), (
        "there is no page-wide focus ring, so anything new is focusable "
        "without showing it"
    )
    for selector, body in rings:
        outline = declaration(body, "outline")
        assert "var(--rb-fg)" in outline, f"{selector} focuses in {outline}"
        assert lengths_in_px(outline)[0] >= 2, f"{selector} focuses in {outline}"


def test_everything_you_press_is_at_least_forty_four_pixels(stylesheet):
    """44px is the size of a fingertip. Anything smaller is a miss and a
    mis-tap, and this box is operated from a phone."""
    interactive = re.compile(r"(^|[\s,])(a|button|input|select|textarea)(\s|\[|:|,|$)"
                             r"|\.row\b|\.ghost\b")
    small = []
    for selector, body in css_rules(stylesheet):
        if not interactive.search(selector):
            continue
        value = declaration(body, "min-height")
        if value and lengths_in_px(value)[0] < 44:
            small.append(f"{selector} is {lengths_in_px(value)[0]:g}px tall")
    assert not small, "touch targets under 44px: " + "; ".join(sorted(small))


# ==========================================================================
# What the customer's own settings ask for
# ==========================================================================
def test_reduced_motion_is_respected_page_wide(stylesheet):
    assert re.search(r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{\s*\*\s*\{",
                     stylesheet), (
        "individual transitions opt out one at a time; there is no page-wide "
        "answer, so the next animation added will ignore the setting"
    )


def test_more_contrast_lifts_the_muted_tier_to_full_brightness(stylesheet):
    windows = [stylesheet[m.start():m.start() + 500]
               for m in re.finditer(r"prefers-contrast:\s*more", stylesheet)]
    assert any("--rb-fg-muted:var(--rb-fg)" in w.replace(" ", "") for w in windows), (
        "asking the device for more contrast does nothing to the muted tier"
    )


# ==========================================================================
# And all of it has to reach the pages, not just sit in the module
# ==========================================================================
@pytest.fixture(scope="module")
def rendered():
    """The two stylesheets as the browser receives them.

    Everything above reads the source. This reads the output, because a
    fragment that is written but never concatenated into a page is a fragment
    that does nothing - and that is a silent way to lose all of the above.
    """
    from retrobox import webstyle
    return {"viewer": webstyle.VIEWER_CSS, "console": webstyle.CONSOLE_CSS}


def test_the_control_styling_actually_reaches_the_console(rendered):
    console = rendered["console"].replace(" ", "")
    for needed in ("input[type=range]", "::-webkit-slider-thumb",
                   "::-moz-range-thumb", "accent-color", "appearance:none"):
        assert needed.replace(" ", "") in console, (
            f"{needed} is defined in the module but never spliced into the "
            "console page, so the browser never sees it"
        )


def test_both_pages_get_the_tiers_the_fonts_and_the_focus_ring(rendered):
    for page, css in rendered.items():
        flat = css.replace(" ", "")
        for needed in ("--rb-fg:", "--rb-fg-muted:", "--rb-line:",
                       "--rb-sans:", "--rb-mono:", "--rb-display:",
                       ":focus-visible", "color-scheme:dark"):
            assert needed.replace(" ", "") in flat, f"the {page} page has no {needed}"


def test_neither_page_fades_text_with_opacity_any_more(rendered):
    """The rendered proof of the fix, per page.

    The source-level test above cannot tell which page a rule lands on. This
    one composites every faded rule the browser will actually apply.
    """
    from retrobox import webstyle
    background = parse_hex("#05080a")
    green = parse_hex(webstyle.GREEN)
    for page, css in rendered.items():
        for name, alpha in dimmed_text_rules(css):
            ratio = contrast(composite(green, background, alpha), background)
            assert ratio >= AA_BODY, (
                f"the {page} page fades .{name} to {alpha}, which paints "
                f"{ratio:.2f}:1"
            )
