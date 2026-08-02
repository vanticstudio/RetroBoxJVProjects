"""The dashboard stylesheet, written down once and handed to both pages.

The box serves two pages: the read-only viewer (``/``) and the console
(``/manage``). Until recently each carried its own full copy of the stylesheet
inside ``webui.py``. The copies were never kept in step, so the same class was
defined twice with two different answers, and a contrast audit reading the file
found ``.empty``, ``.mark`` and ``.tiny`` listed twice and had to guess which
one the browser would actually use.

This module holds the stylesheet instead. Every rule the two pages agree on is
written here once. Where they genuinely disagree - and they do, in five places -
the disagreement is named rather than flattened, because both answers are load
bearing:

* ``h1``     the viewer's title is a heading above a card; the console's is the
             masthead of the whole app, and is bigger.
* ``.meta``  different margin; the viewer's sits under a show title, the
             console's inside a panel that already has padding.
* ``.row``   on the viewer a row is a line of text; on the console it is a
             ``<button>`` you press, so it needs the touch target, the reset of
             the browser's button styling and the pointer cursor.
* ``.row.on``the console's highlight is stronger because its rows are pressable
             and the current one has to be obvious under a fingertip.
* ``.led``   the console's channel badge is larger.

Anything that differs by a single value - the page's bottom padding, a panel's
padding, the height of a meter - is a parameter on the shared rule, not a second
copy of it.

**The stylesheet is generated, not static.** The phosphor green, the muted tier
and the line value are spliced in from Python, so a future config-driven palette
only has to reach ``viewer_css`` and ``console_css``. That is why these are
functions taking a palette rather than string constants.

READ THIS BEFORE CHANGING A COLOUR OR A SIZE
--------------------------------------------
Customers reported that the dashboard was hard to read, and they were right.
The cause was not a colour: every hex here clears WCAG AA on its own. The cause
was ``opacity``. Eleven classes faded the product green to somewhere between
35% and 50%, and the browser composites that against the page to as little as
2.57:1, where a sentence needs 4.5:1.

So there are now exactly three foreground values, they are named for the job
they do, and none of them is a fade. The rules are short:

* Fade a box if you like. **Never fade a word.** If something should be
  quieter, paint it in ``--rb-fg-muted``, which is quieter and still readable.
* ``--rb-line`` draws borders, rules and unlit segments. It is 1.59:1. It never
  holds a word and never holds an icon that means anything.
* Nothing is set below 14px, form fields are never below 16px (iOS Safari zooms
  the whole page when you focus one that is), and running text has a line
  height of at least 1.5.

``tests/test_dashboard_contrast.py`` measures all of that on every run,
including the ratios quoted in the comments, so none of it can quietly stop
being true.
"""

# The phosphor green from the on-screen display, so the pages read as part of
# the same product rather than a bolted-on admin panel.
GREEN = "#4DFF5A"
# The same green taken down until it reads as secondary but is still comfortably
# readable - 8.39:1 on the page background, where the bar is 4.5:1. This exists
# because the alternative people reach for is opacity, and opacity is what made
# the dashboard unreadable in the first place.
MUTED = "#3FBF4D"
# Borders, rules and unlit segments. Not type, ever: it is 1.59:1.
DIM = "#123B18"


# ==========================================================================
# The rules both pages agree on. One definition each - that is the point of
# this module. A parameter here means the two pages differ by exactly one
# value; anything more than that lives in the per-page sections below.
# ==========================================================================
def _root(green: str, muted: str, line: str, extra: str = "") -> str:
    """The palette. ``extra`` is for a variable only one page needs."""
    return """
  :root {
    /* Say out loud that this is a dark page. It is what makes the parts we
       cannot style come back dark instead of light-on-light: the list a
       select opens, the scrollbars, the clock on a time field. */
    color-scheme: dark;

    /* ---- the three foreground tiers ---------------------------------
       Every foreground on both pages is one of these three. The ratios are
       measured against --bg (#05080a) with the WCAG 2.1 formula, and the
       contrast test re-measures them on every run and checks these very
       numbers, so the comment cannot drift away from the values:

         --rb-fg        15.08:1  values, headings, active state, primary text
         --rb-fg-muted   8.39:1  labels, secondary text, help, placeholders
         --rb-line       1.59:1  BORDERS, RULES AND UNLIT SEGMENTS ONLY.
                                 Never a word. Never an icon that carries
                                 meaning. At 1.59:1 it is not a colour you
                                 can read, and no font size rescues it. */
    --rb-fg:""" + green + """; --rb-fg-muted:""" + muted + """; --rb-line:""" + line + """;

    --bg:#05080a; --fill:rgba(77,255,90,.04);
    --red:#ff6b5a;""" + extra + """

    /* The display face is the product's voice: the wordmark, the section
       headings, the tab labels, the channel number, the version. VT323 is
       the on-screen display's own font, bundled on the box for the TV; the
       phone reading this page almost certainly does not have it, so the
       stack falls through to whatever terminal face the device does have.
       Nothing is ever downloaded - the box may have no internet at all. */
    --rb-display:"VT323",ui-monospace,"SF Mono",Menlo,Consolas,monospace;
    /* Body copy is set in the device's own interface face, because that is
       the face its owner already reads everything else in. A pixel terminal
       font is wonderful for a channel banner and hard work for a paragraph. */
    --rb-sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
    /* Log output only, where the columns have to line up. */
    --rb-mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
  }
"""


_RESET = """  * { box-sizing:border-box; }
  /* Every display: rule below would otherwise beat the browser's own
     [hidden] { display:none }, leaving hidden panels on screen. */
  [hidden] { display:none !important; }
  html { -webkit-text-size-adjust:100%; }
  /* One answer for the whole page, so the next thing that animates does not
     have to remember to ask. */
  @media (prefers-reduced-motion: reduce) {
    * { transition-duration:.01ms !important; animation-duration:.01ms !important;
        animation-iteration-count:1 !important; scroll-behavior:auto !important; }
  }
  /* A focus ring in the product's own colour, page-wide, so anything added
     later is visibly focusable without anybody remembering to style it. The
     browser default is a blue halo, which on a black-and-green page looks
     like something has gone wrong. */
  :focus-visible { outline:2px solid var(--rb-fg); outline-offset:2px; }
"""


def _body(bottom_padding: str) -> str:
    """The page itself. The console reserves more room at the foot for the
    toast, which is fixed to the bottom of the window and would otherwise sit
    on top of the last control."""
    return """  body { margin:0; padding:1.2rem .9rem """ + bottom_padding + """; background:var(--bg); color:var(--rb-fg);
         font-family:var(--rb-sans); font-size:17px; line-height:1.55;
         text-shadow:0 0 6px rgba(77,255,90,.3); }
"""


_SCANLINES = """  /* The scanlines the on-screen display has. A third of every letter sits
     under one, so this is as strong as it can be and still let type through;
     anyone who has asked their device for more contrast gets none at all. */
  body::after { content:""; position:fixed; inset:0; pointer-events:none; z-index:50;
    background:repeating-linear-gradient(0deg,rgba(0,0,0,.18) 0 1px,transparent 1px 3px); }
  @media (prefers-contrast: more) { body::after { display:none; } }
  .wrap { max-width:44rem; margin:0 auto; }
"""


# The wordmark above the title. Muted rather than faded: it is a label, it
# should sit behind the heading, and it still has to be legible at 14px.
_MARK = """  .mark { font-size:.875rem; letter-spacing:.42em; color:var(--rb-fg-muted);
    font-family:var(--rb-display); margin:0 0 .1rem; }
"""


def _panel(padding: str) -> str:
    return """  .panel { border:1px solid var(--rb-line); border-radius:3px; padding:""" + padding + """;
           margin-bottom:1rem; background:var(--fill); }
"""


# A section divider: a short uppercase label with a rule running off it. It is
# muted because it labels the panel rather than saying anything, and it keeps
# the display face because these headings are part of how the product reads.
_H2 = """  h2 { font-size:.875rem; text-transform:uppercase; letter-spacing:.2em;
       color:var(--rb-fg-muted); font-family:var(--rb-display);
       margin:0 0 .7rem; font-weight:normal; display:flex; gap:.6rem; align-items:center; }
  h2::after { content:""; flex:1; height:1px; background:var(--rb-line); }
"""


def _meter(height: str, flow: str) -> str:
    """The volume bar off the front panel, reused for upload progress.

    ``flow`` is how the meter sits among its neighbours: the viewer's stands
    alone under the times, the console's shares a row with a percentage.

    The unlit half of the bar is ``--rb-line``, which is exactly the job that
    value was designed for - it is a segment that is off, not a word.
    """
    return """  .meter { height:""" + height + """; border:1px solid var(--rb-line); border-radius:1px; """ + flow + """;
    background:repeating-linear-gradient(90deg,var(--rb-line) 0 6px,transparent 6px 9px); }
  .meter i { display:block; height:100%; background:var(--rb-fg);
    box-shadow:0 0 8px rgba(77,255,90,.6);
    -webkit-mask:repeating-linear-gradient(90deg,#000 0 6px,transparent 6px 9px);
    mask:repeating-linear-gradient(90deg,#000 0 6px,transparent 6px 9px); }
"""


_ROW_LAST = """  .row:last-child { border-bottom:0; }
"""


_GROW = """  .grow { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
"""


def _tiny(extra: str = "") -> str:
    """Small print beside a row. The viewer's must not be squeezed by a long
    title next to it, which is what ``flex:none`` is doing there.

    "Small" now stops at 14px. Below that it is decoration, not information.
    """
    return """  .tiny { font-size:.875rem; color:var(--rb-fg-muted);""" + extra + """ }
"""


def _empty(extra: str = "") -> str:
    """What a list says when it has nothing in it. The console's sits inside a
    panel and needs its own padding; the viewer's is already inside one.

    This is a whole sentence addressed to the customer, so it is set at the
    size a sentence gets read at.
    """
    return """  .empty { color:var(--rb-fg-muted); font-size:1rem;""" + extra + """ }
"""


# The muted tier goes back to full brightness for anyone whose device asks for
# more contrast, and the phosphor glow comes off - the glow is what softens the
# edge of a letter, which is the last thing that reader wants.
_CONTRAST = """  @media (prefers-contrast: more) {
    :root { --rb-fg-muted:var(--rb-fg); }
    body { text-shadow:none; }
  }
"""


# ==========================================================================
# The viewer's own rules
# ==========================================================================
# The viewer title heads a card, not the whole application, so it is smaller
# than the console's and carries the gap to the first panel itself.
_VIEWER_H1 = """  h1 { font-size:1.5rem; margin:0 0 1rem; letter-spacing:.08em; font-weight:normal;
       font-family:var(--rb-display); }
"""

_VIEWER_SCREEN = """  /* The picture goes here. Until there is a stream to put in it this stays
     empty and takes no space; a <video> dropped straight in needs no other
     change to this page. */
  #screen:empty { display:none; }
  #screen { margin:0 0 1.1rem; border:1px solid var(--rb-line); border-radius:3px;
    overflow:hidden; background:#000; aspect-ratio:4/3; }
  #screen video, #screen img { width:100%; height:100%; object-fit:contain;
    display:block; }
"""

# .meta here trails a show title, so it needs the gap above it that the
# console's - which starts a panel - does not. The channel number is the one
# thing on this page that is meant to look like a television, so it keeps the
# display face; the show title underneath is read as words, so it does not.
_VIEWER_NOW = """  .ch { font-size:2.6rem; letter-spacing:.04em; margin:0; line-height:1.1;
    font-family:var(--rb-display); }
  .show { font-size:1.15rem; margin:.35rem 0 0; }
  .meta { color:var(--rb-fg-muted); font-size:1rem; margin:.2rem 0 0; }
"""

_VIEWER_TIMES = """  .times { display:flex; justify-content:space-between; font-size:.875rem;
    color:var(--rb-fg-muted); margin-top:.35rem; font-variant-numeric:tabular-nums; }
"""

# A viewer row is a line of text you read, so it aligns on the baseline and
# stays compact. The console's is a button you press - see _CONSOLE_ROW.
_VIEWER_ROW = """  .row { display:flex; gap:.7rem; align-items:baseline; padding:.4rem .1rem;
    border-bottom:1px solid rgba(77,255,90,.12); }
"""

_VIEWER_ROW_ON = """  .row.on { background:rgba(77,255,90,.14); }
"""

# Nothing on the viewer is pressable, so its channel badge is quieter than the
# console's. It keeps the display face because it is pretending to be an LED.
_VIEWER_LED = """  .led { font-size:1rem; border:1px solid var(--rb-line); border-radius:2px;
    padding:.02rem .45rem; background:rgba(77,255,90,.07); min-width:3rem;
    text-align:center; flex:none; font-family:var(--rb-display); }
"""

# Off air. The red is 7.18:1 on this page at full strength, which is where it
# stays - it used to be faded to 85% for no reason anybody could name.
_VIEWER_OFF = """  .off { color:var(--red); }
"""

_VIEWER_FOOT = """  .foot { display:flex; justify-content:space-between; align-items:center;
    margin-top:1.2rem; font-size:.875rem; }
  a { color:var(--rb-fg); text-decoration:none; border:1px solid var(--rb-line);
      border-radius:2px; padding:.6rem 1rem; letter-spacing:.12em;
      display:inline-block; min-height:2.8rem; }
  a:hover, a:focus-visible { background:rgba(77,255,90,.16); outline:none; }
  a:focus-visible { outline:2px solid var(--rb-fg); outline-offset:1px; }
  .dim { color:var(--rb-fg-muted); letter-spacing:.06em; }
"""


# ==========================================================================
# The console's own rules
# ==========================================================================
# The console's h1 is the masthead of the whole application and the .sub line
# underneath carries the gap to the tabs, so h1 has no bottom margin.
_CONSOLE_MASTHEAD = """  /* -- masthead -------------------------------------------------------- */
"""

_CONSOLE_H1 = """  h1 { font-size:2.1rem; margin:0; letter-spacing:.08em; font-weight:normal;
       font-family:var(--rb-display); }
  .sub { color:var(--rb-fg-muted); margin:.1rem 0 1.1rem; font-size:1rem; }
  .sub.offline { color:var(--red); text-shadow:0 0 6px rgba(255,107,90,.4); }

  /* -- tabs, as the service menu's top row ------------------------------ */
  /* The tab labels keep the display face: they are the closest thing this
     page has to a channel banner, and they are short enough to stay legible
     in it as long as the letters are held apart. */
  nav { display:flex; gap:.4rem; margin-bottom:1.1rem; }
  nav button { flex:1; min-height:3rem; border:1px solid var(--rb-line); background:transparent;
    color:var(--rb-fg); font:inherit; font-family:var(--rb-display); font-size:1rem;
    letter-spacing:.16em; text-shadow:inherit; border-radius:2px; cursor:pointer; }
  nav button[aria-selected="true"] { background:rgba(77,255,90,.16); border-color:var(--rb-fg); }

  /* -- panels ----------------------------------------------------------- */
"""

# .meta opens a panel here, so it has no margin of its own. What is on now is
# the console's channel banner, so it keeps the display face.
_CONSOLE_NOW = """  .now { font-size:1.55rem; margin:0 0 .2rem; font-family:var(--rb-display); }
  .meta { color:var(--rb-fg-muted); font-size:1rem; margin:0; }
"""

_CONSOLE_LED = """
  /* -- the LED channel number, straight off the front panel -------------- */
  .led { font-size:1.15rem; letter-spacing:.06em; color:var(--rb-fg);
    font-family:var(--rb-display);
    border:1px solid var(--rb-line); border-radius:2px; padding:.05rem .45rem;
    background:rgba(77,255,90,.07); min-width:3.1rem; text-align:center; flex:none; }
"""

# A console row is a real <button>: it needs the browser's button styling
# undone, a target big enough for a fingertip, and a cursor that says so.
_CONSOLE_ROW = """
  /* -- rows ------------------------------------------------------------- */
  .row { display:flex; gap:.7rem; align-items:center; width:100%; min-height:3.1rem;
    padding:.35rem .2rem; border:0; border-bottom:1px solid rgba(77,255,90,.12);
    background:transparent; color:inherit; font:inherit; text-shadow:inherit;
    text-align:left; cursor:pointer; }
"""

_CONSOLE_ROW_STATES = """  .row:hover, .row:focus-visible { background:rgba(77,255,90,.12); outline:none; }
  .row.on { background:rgba(77,255,90,.2); }
"""

_CONSOLE_CONTROLS = """
  /* -- controls --------------------------------------------------------- */
  button, select, input, textarea { font:inherit; font-size:1rem; color:var(--rb-fg);
    background:transparent; border:1px solid var(--rb-line); border-radius:2px;
    text-shadow:inherit; }
  button { min-height:3rem; padding:0 .9rem; cursor:pointer; letter-spacing:.08em; }
  button:hover, button:focus-visible { background:rgba(77,255,90,.16); outline:none; }
  button:focus-visible, .row:focus-visible { outline:2px solid var(--rb-fg); outline-offset:1px; }
  /* A button you cannot press keeps a colour you can read - it still tells
     you what it would have done - and loses the phosphor glow, which on a
     screen made of light is what "off" looks like. */
  button[disabled] { color:var(--rb-fg-muted); border-color:var(--rb-line);
    text-shadow:none; cursor:not-allowed; }
  button[disabled]:hover { background:transparent; }
  .bar { display:flex; gap:.45rem; flex-wrap:wrap; }
  .bar button { flex:1 1 auto; min-width:5.2rem; }
  .ghost { min-height:2.75rem; font-size:.875rem; letter-spacing:.14em; padding:0 .7rem; }
  .danger { border-color:var(--red); color:var(--red); text-shadow:0 0 6px rgba(255,107,90,.35); }
  .danger:hover, .danger:focus-visible { background:rgba(255,107,90,.14); }
  .danger.armed { background:rgba(255,107,90,.22); }
  /* 3rem is 48px, and 1rem is the 16px below which iOS Safari zooms the whole
     page the moment a field takes focus. Neither number is decorative. */
  input, select, textarea { width:100%; min-height:3rem; padding:0 .6rem; }
  /* A placeholder is text. This one used to be the green at 30%, which paints
     about 1.8:1 - a hint nobody could see. */
  input::placeholder, textarea::placeholder { color:var(--rb-fg-muted); opacity:1; }
  label { display:block; font-size:.875rem; letter-spacing:.16em; text-transform:uppercase;
    color:var(--rb-fg-muted); margin:.8rem 0 .25rem; }
  .field { margin-bottom:.2rem; }

  /* -- the native controls, drawn in the product's own colours ------------
     Everything from here to the end of this block replaces what the operating
     system draws. Left alone, a range slider is a blue thumb on a grey track
     and a select is a grey box with a blue highlight, in the middle of a
     black-and-green CRT interface. Both sliders are built in JavaScript
     rather than written into the page, which is why searching the markup for
     them finds nothing and why they went unstyled for so long.
     ---------------------------------------------------------------------- */

  /* The whole control is the touch target: 2.75rem is 44px, a fingertip. */
  input[type=range] { -webkit-appearance:none; appearance:none; width:100%;
    min-height:2.75rem; padding:0; border:0; background:transparent; cursor:pointer; }
  /* WebKit and Firefox each throw away an entire rule that so much as mentions
     the other one's pseudo-element, so the track and the thumb are written
     out twice rather than shared. Keep the two copies in step. */
  input[type=range]::-webkit-slider-runnable-track { height:.5rem; border-radius:1px;
    border:1px solid var(--rb-line);
    background:repeating-linear-gradient(90deg,var(--rb-line) 0 6px,transparent 6px 9px); }
  input[type=range]::-moz-range-track { height:.5rem; border-radius:1px;
    border:1px solid var(--rb-line);
    background:repeating-linear-gradient(90deg,var(--rb-line) 0 6px,transparent 6px 9px); }
  /* The thumb is 24px across and stays that way. It is not shrunk to look
     neater; it is the part a fingertip has to catch on a moving slider. The
     negative margin is what centres it on a 10px track in WebKit. */
  input[type=range]::-webkit-slider-thumb { -webkit-appearance:none; appearance:none;
    width:1.5rem; height:1.5rem; margin-top:-.4375rem; border-radius:2px; border:0;
    background:var(--rb-fg); box-shadow:0 0 8px rgba(77,255,90,.6); }
  input[type=range]::-moz-range-thumb { width:1.5rem; height:1.5rem; border-radius:2px;
    border:0; background:var(--rb-fg); box-shadow:0 0 8px rgba(77,255,90,.6); }
  input[type=range]:focus-visible { outline:none; }
  input[type=range]:focus-visible::-webkit-slider-thumb { outline:2px solid var(--rb-fg);
    outline-offset:2px; }
  input[type=range]:focus-visible::-moz-range-thumb { outline:2px solid var(--rb-fg);
    outline-offset:2px; }

  /* The closed select only. The list it opens is drawn by the operating
     system and cannot be styled at all - that is what color-scheme is for.
     The caret is two gradients rather than an image, because this page has to
     work with nothing beside it and no network to fetch anything from. */
  select { -webkit-appearance:none; appearance:none; cursor:pointer; padding-right:2.2rem;
    background-image:linear-gradient(45deg,transparent 50%,var(--rb-fg) 50%),
      linear-gradient(135deg,var(--rb-fg) 50%,transparent 50%);
    background-position:calc(100% - 1.15rem) calc(50% - .1rem),
      calc(100% - .75rem) calc(50% - .1rem);
    background-size:.4rem .4rem; background-repeat:no-repeat; }

  /* There is no checkbox on the page today. When one arrives it must not
     arrive as a blue-and-white system checkbox, and it must not arrive as a
     16px square nobody can hit. The control is 44px; the mark drawn inside it
     is the part you see. A filled block is "on" here, the same as every other
     lit thing on this page. */
  input[type=checkbox], input[type=radio] { -webkit-appearance:none; appearance:none;
    accent-color:var(--rb-fg); width:2.75rem; height:2.75rem; min-height:2.75rem;
    flex:none; margin:0; padding:0; border:0; background:transparent; cursor:pointer;
    display:inline-grid; place-content:center; }
  input[type=checkbox]::before, input[type=radio]::before { content:""; display:block;
    width:1.5rem; height:1.5rem; border:1px solid var(--rb-fg-muted); border-radius:2px; }
  input[type=radio]::before { border-radius:50%; }
  input[type=checkbox]:checked::before, input[type=radio]:checked::before {
    background:var(--rb-fg); border-color:var(--rb-fg);
    box-shadow:0 0 8px rgba(77,255,90,.6); }

  /* The spinner on a number field and the clock on a time field are drawn by
     the browser and cannot be recoloured. They are left alone deliberately:
     they are useful, and color-scheme:dark is what makes them legible here.
     The file pickers are hidden and opened by a button, but if one is ever
     shown, its button should not be the system's either. */
  input[type=file] { min-height:2.75rem; padding:.5rem; }
  input[type=file]::file-selector-button { font:inherit; font-size:1rem; color:var(--rb-fg);
    background:transparent; border:1px solid var(--rb-line); border-radius:2px;
    min-height:2.75rem; padding:0 .9rem; margin-right:.6rem; cursor:pointer; }

  /* -- the editor ------------------------------------------------------- */
  .edit { border:1px solid var(--rb-line); border-radius:2px; padding:.75rem;
    margin:.4rem 0 .8rem; background:rgba(77,255,90,.03); }
  .split { display:flex; gap:.5rem; }
  .split .field { flex:1; }
  .split .field.narrow { flex:0 0 6.5rem; }

  /* -- upload meter, drawn like the volume bars on the TV ---------------- */
"""

_CONSOLE_PROGRESS = """  .progress { display:flex; gap:.6rem; align-items:center; margin-top:.5rem; }
  .progress span { font-variant-numeric:tabular-nums; min-width:3.4rem; text-align:right; }

  .note { font-size:1rem; color:var(--rb-fg-muted); margin:.5rem 0 0; }
  .note.warn { color:var(--amber); }
  .note.bad { color:var(--red); }
"""

_CONSOLE_REST = """
  /* -- the drop zone ---------------------------------------------------- */
  .drop { border:1px dashed var(--rb-line); border-radius:3px; padding:1.4rem 1rem;
    text-align:center; transition:background .15s, border-color .15s; }
  .drop.over { border-color:var(--rb-fg); border-style:solid;
    background:rgba(77,255,90,.12); }
  .dropline { margin:0 0 .3rem; letter-spacing:.12em; font-size:1rem; }
  @media (prefers-reduced-motion: reduce) { .drop { transition:none; } }

  /* -- the upload queue -------------------------------------------------- */
  .job { display:flex; gap:.6rem; align-items:center; padding:.4rem .2rem;
    border-bottom:1px solid rgba(77,255,90,.12); }
  .job:last-child { border-bottom:0; }
  .job .grow { font-size:1rem; }
  .state { font-size:.875rem; letter-spacing:.12em; text-transform:uppercase;
    color:var(--rb-fg-muted); min-width:5.4rem; text-align:right; flex:none; }
  .state.done { color:var(--rb-fg); }
  .state.failed { color:var(--red); }
  .state.warn { color:var(--amber); }
  .totals { display:flex; justify-content:space-between; font-size:.875rem;
    color:var(--rb-fg-muted); margin-top:.4rem; }

  /* -- system ------------------------------------------------------------ */
  .fact { display:flex; gap:.7rem; align-items:baseline; padding:.32rem .1rem;
    border-bottom:1px solid rgba(77,255,90,.1); font-size:1rem; }
  .fact:last-child { border-bottom:0; }
  .fact .key { color:var(--rb-fg-muted); min-width:9.5rem; flex:none; font-size:.875rem;
    text-transform:uppercase; letter-spacing:.1em; }
  .fact .val { flex:1; min-width:0; word-break:break-word; }
  .fact .val.bad { color:var(--red); }
  .fact .val.warn { color:var(--amber); }
  /* Log output is columns, so it needs a font whose columns line up. The
     display face only looks like a terminal; it does not behave like one. */
  .raw { background:#040a05; border:1px solid var(--rb-line); border-radius:2px;
    padding:.7rem; font-family:var(--rb-mono); font-size:.875rem; line-height:1.5;
    max-height:26rem; overflow:auto; white-space:pre-wrap; word-break:break-word;
    margin:.7rem 0 0; }
  .press { display:flex; gap:.7rem; align-items:baseline; padding:.3rem .1rem;
    border-bottom:1px solid rgba(77,255,90,.1); }
  .press:last-child { border-bottom:0; }
  .press .who { font-size:.875rem; color:var(--rb-fg-muted); min-width:5.5rem;
    flex:none; text-transform:uppercase; letter-spacing:.1em; }
  .press .what { flex:1; letter-spacing:.06em; }
  .press.fresh { background:rgba(77,255,90,.22); }

  /* -- updates ----------------------------------------------------------- */
  .notes { border:1px solid var(--rb-line); border-radius:2px; padding:.2rem .9rem;
    margin:.6rem 0; background:rgba(77,255,90,.03); max-height:22rem;
    overflow:auto; }
  .notes h3, .notes h4, .notes h5, .notes h6 { font-size:.875rem; margin:.9rem 0 .3rem;
    letter-spacing:.12em; text-transform:uppercase; color:var(--rb-fg-muted);
    font-weight:normal; }
  .notes ul { margin:.2rem 0 .7rem; padding-left:1.1rem; }
  .notes li { margin:.15rem 0; font-size:1rem; }
  .notes p { font-size:1rem; margin:.4rem 0; }
  .notes code { font-family:var(--rb-mono); background:rgba(77,255,90,.12);
    padding:0 .25rem; border-radius:2px; }
  .rel { border-bottom:1px solid var(--rb-line); }
  .rel:last-child { border-bottom:0; }
  .rel h3:first-child { margin-top:.5rem; }
  .relhead { display:flex; gap:.7rem; align-items:baseline; margin:.8rem 0 0; }
  .relhead .v { font-size:1.15rem; letter-spacing:.04em; font-family:var(--rb-display); }
  .relhead .d { font-size:.875rem; color:var(--rb-fg-muted); }
  .stages { display:flex; flex-wrap:wrap; gap:.35rem; margin:.6rem 0; }
  /* Three states, told apart by colour rather than by how faded they are.
     Pending used to be the green at 35%, which paints 2.57:1 - the least
     readable thing on either page, and it was a word telling somebody what
     their box was about to do. */
  .stage { font-size:.875rem; letter-spacing:.1em; text-transform:uppercase;
    border:1px solid var(--rb-line); border-radius:2px; padding:.25rem .5rem;
    color:var(--rb-fg-muted); }
  .stage.on { color:var(--rb-fg); background:rgba(77,255,90,.2); border-color:var(--rb-fg); }
  .stage.done { color:var(--rb-fg); }

  /* -- the permission banner ---------------------------------------------- */
  /* Amber, like the network trial: something needs doing, nothing is lost. */
  .alarm { border-color:var(--amber); background:rgba(255,193,77,.08);
    color:var(--amber); }
  .alarm .now { font-size:1.3rem; }
  .alarm ul { margin:.3rem 0 .6rem; padding-left:1.2rem; }
  .alarm li { font-size:1rem; margin:.15rem 0; }
  /* The one command anybody is ever asked to type. Selectable in one go,
     because it gets pasted verbatim, and it wraps rather than scrolling off
     the side of a phone where the end of it would be invisible. */
  .typeit { display:block; border:1px solid var(--amber); border-radius:2px;
    padding:.6rem; margin:.4rem 0 .7rem; background:rgba(255,193,77,.06);
    font-family:var(--rb-mono); font-size:1rem; word-break:break-all;
    white-space:pre-wrap; -webkit-user-select:all; user-select:all; }

  /* -- network ----------------------------------------------------------- */
  .probation { border:1px solid var(--amber); border-radius:3px; padding:.9rem;
    margin-bottom:1rem; background:rgba(255,193,77,.08); color:var(--amber); }
  .probation .count { font-size:2rem; letter-spacing:.04em; font-family:var(--rb-display); }
  .netlist .row { cursor:pointer; }
  /* Signal strength. The unlit bars are unlit segments, which is the one job
     --rb-line exists for; the lit ones carry the meaning and are full green. */
  .bars { display:inline-flex; gap:2px; align-items:flex-end; height:.9rem;
    flex:none; }
  .bars i { width:3px; background:var(--rb-line); }
  .bars i.lit { background:var(--rb-fg); }

  /* -- the day, laid out -------------------------------------------------- */
  /* A block's name only appears when its segment is wide enough to hold it,
     and every segment carries the full name in a tooltip. At 14px fewer names
     fit than at 10px did - but a name at 10px was not being read, it was
     being squinted at, and the list underneath says the same thing in full. */
  .day { display:flex; height:2.6rem; border:1px solid var(--rb-line); border-radius:2px;
    overflow:hidden; margin:.8rem 0 .2rem; }
  /* Names run from the left rather than from the centre. A segment narrower
     than its name is clipped either way, and half a name read from the start
     is a name; half a name clipped at both ends is not. */
  .day .seg { display:flex; align-items:center; justify-content:flex-start;
    font-size:.875rem; letter-spacing:.06em; overflow:hidden; white-space:nowrap;
    border-right:1px solid var(--rb-line); padding:0 .25rem; }
  .day .seg:last-child { border-right:0; }
  .day .seg.block { background:rgba(77,255,90,.24); }
  .day .seg.gap { background:rgba(77,255,90,.03); color:var(--rb-fg-muted); }
  .day .seg.off { background:rgba(255,107,90,.22); color:var(--red); }
  .day .seg.on { outline:1px solid var(--rb-fg); outline-offset:-1px; }
  /* Eight times across the width of the phone, each one lined up with the
     start of its three-hour slot above. min-width:0 is what stops the last
     one pushing the whole page sideways on a very narrow screen: without it
     a flex item refuses to shrink below its text and the page grows a
     horizontal scrollbar. Tabular figures keep the columns even. */
  .hours { display:flex; font-size:.875rem; color:var(--rb-fg-muted);
    margin-bottom:.9rem; font-variant-numeric:tabular-nums; }
  .hours span { flex:1; min-width:0; overflow:hidden; text-align:left; }
  .blockrow { display:flex; gap:.4rem; align-items:center; margin:.35rem 0; }
  .blockrow input { min-height:2.75rem; }
  .blockrow .t { flex:0 0 6.5rem; }
  .blockrow .n { flex:1; }

  /* -- the file manager --------------------------------------------------
     Written after .row and after the controls block on purpose: a library row
     is a .row that has grown a checkbox, and these rules are the part that
     wins the tie. Moving this fragment above either of them un-styles the
     whole page's worth of it.
     --------------------------------------------------------------------- */
  /* A row is a tick, a name and a size. The name is the wide part because the
     name is what the eye is on; the tick is 44px because the thumb is not. */
  .lib { display:flex; gap:.4rem; align-items:center; min-height:2.75rem;
    padding:.1rem 0; border-bottom:1px solid rgba(77,255,90,.12); }
  .lib:last-child { border-bottom:0; }
  .lib.on { background:rgba(77,255,90,.14); }
  .lib .name { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis;
    white-space:nowrap; font-size:1rem; text-align:left; }
  /* A folder name opens the folder, so it is a real button - it keeps the
     page-wide focus ring and the 44px, and loses the box and the centring. */
  .lib button.name { border:0; padding:0 .2rem; min-height:2.75rem;
    letter-spacing:0; background:transparent; }
  .lib .size { flex:none; font-size:.875rem; color:var(--rb-fg-muted);
    font-variant-numeric:tabular-nums; }
  .lib .kind { flex:none; min-width:4.4rem; text-align:right; font-size:.875rem;
    letter-spacing:.1em; text-transform:uppercase; color:var(--rb-fg-muted); }
  /* The box's own folders are shown rather than hidden - somebody looking for
     a missing forty gigabytes has to be able to see where they went - but
     nothing can be selected in them. Quieter means the muted tier, which is a
     colour that passes AA, not a fade. */
  .lib.system .name, .lib.system .kind { color:var(--rb-fg-muted); }
  .lib .when { flex:none; font-size:.875rem; color:var(--rb-fg-muted);
    font-variant-numeric:tabular-nums; }

  /* Where you are, and one tap back to anywhere above you. */
  .crumbs { display:flex; flex-wrap:wrap; gap:.3rem; align-items:center;
    margin:0 0 .6rem; }
  .crumbs button { min-height:2.75rem; font-size:.875rem; letter-spacing:.1em;
    padding:0 .55rem; }
  .crumbs .sep { color:var(--rb-fg-muted); font-size:.875rem; }

  /* What is selected, and what can be done with it. Stuck to the bottom of
     the screen because SELECT ALL on a folder of six hundred episodes
     otherwise puts DELETE six hundred rows away from the tick that armed it.
     The background is the page's own, so the rows scroll under it rather
     than showing through it. */
  .libbar { position:sticky; bottom:0; z-index:20; display:flex; gap:.45rem;
    align-items:center; flex-wrap:wrap; margin-top:.6rem; padding:.5rem 0;
    background:var(--bg); border-top:1px solid var(--rb-line); }
  .libbar .count { flex:1 1 8rem; font-size:1rem; }
  .libbar button { flex:0 1 auto; }
  .pager { display:flex; gap:.45rem; align-items:center;
    justify-content:space-between; margin-top:.7rem; }
  .pager .of { font-size:.875rem; color:var(--rb-fg-muted);
    font-variant-numeric:tabular-nums; }

  /* The confirmation, which is the whole point of the feature.
     Never the browser's own confirm(): that box can say "Are you sure?" and
     nothing else, and "Are you sure?" is not a question anybody can answer.
     This one has room for the three facts that make it answerable - how many
     files, how much space, and which channels go dark - so it is red, like
     the buttons it is confirming, and it is part of the page. */
  .peril { border:1px solid var(--red); border-radius:3px; padding:.9rem;
    margin:.6rem 0; background:rgba(255,107,90,.08); }
  .peril h3 { font-family:var(--rb-display); font-size:1.3rem; margin:0 0 .4rem;
    letter-spacing:.06em; color:var(--red); }
  .peril .cost { font-size:1rem; margin:.2rem 0 .5rem; line-height:1.5; }
  .peril ul { margin:.3rem 0 .7rem; padding-left:1.2rem; }
  .peril li { font-size:1rem; margin:.25rem 0; line-height:1.5; color:var(--red); }
  .peril .bar { margin-top:.7rem; }

  /* -- toast ------------------------------------------------------------ */
  #toast { position:fixed; left:50%; bottom:1rem; transform:translateX(-50%);
    max-width:calc(100% - 2rem); border:1px solid var(--rb-fg); border-radius:2px;
    background:#071109; padding:.6rem 1rem; z-index:60; font-size:1rem;
    opacity:0; transition:opacity .18s; pointer-events:none; }
  #toast.show { opacity:1; }
  #toast.bad { border-color:var(--red); color:var(--red); }
  @media (prefers-reduced-motion: reduce) { #toast { transition:none; } }
"""


# ==========================================================================
# The two stylesheets, assembled
# ==========================================================================
# Each page keeps its own running order, because the order a stylesheet is
# read in is part of what it means: a rule later in the file wins a tie with
# one earlier. Assembling in the page's own order is what lets this refactor
# claim the browser sees exactly what it saw before.
#
# _CONTRAST goes last on both pages. It re-declares --rb-fg-muted, and a
# custom property is resolved where it is used, not where it is declared, so
# the media query has to be the last word on the subject.
def viewer_css(green: str = GREEN, dim: str = DIM, muted: str = MUTED) -> str:
    """Everything between the viewer page's ``<style>`` tags."""
    return (
        _root(green, muted, dim)
        + _RESET
        + _body("3rem")
        + _SCANLINES
        + "\n"
        + _MARK
        + _VIEWER_H1
        + "\n"
        + _VIEWER_SCREEN
        + "\n"
        + _panel("1rem")
        + _H2
        + "\n"
        + _VIEWER_NOW
        + "\n"
        + _meter(".7rem", "margin-top:.9rem")
        + _VIEWER_TIMES
        + "\n"
        + _VIEWER_ROW
        + _ROW_LAST
        + _VIEWER_ROW_ON
        + _VIEWER_LED
        + _GROW
        + _tiny(" flex:none;")
        + _VIEWER_OFF
        + _empty()
        + "\n"
        + _VIEWER_FOOT
        + "\n"
        + _CONTRAST
    )


def console_css(green: str = GREEN, dim: str = DIM, muted: str = MUTED) -> str:
    """Everything between the console page's ``<style>`` tags."""
    return (
        _root(green, muted, dim, " --amber:#ffc14d;")
        + _RESET
        + _body("4rem")
        + _SCANLINES
        + "\n"
        + _CONSOLE_MASTHEAD
        + _MARK
        + _CONSOLE_H1
        + _panel(".9rem")
        + _H2
        + _CONSOLE_NOW
        + _CONSOLE_LED
        + _CONSOLE_ROW
        + _ROW_LAST
        + _CONSOLE_ROW_STATES
        + _GROW
        + _tiny()
        + _CONSOLE_CONTROLS
        + _meter(".85rem", "flex:1")
        + _CONSOLE_PROGRESS
        + _empty(" padding:.5rem .2rem;")
        + _CONSOLE_REST
        + "\n"
        + _CONTRAST
    )


#: Rendered once at import, which is when the pages themselves are built.
VIEWER_CSS = viewer_css()
CONSOLE_CSS = console_css()
