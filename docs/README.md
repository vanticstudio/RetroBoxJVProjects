# The Retro Box site

The project website. It ships from this folder and is served by GitHub Pages
straight off `main`, so edits go out with the code they describe.

**There is no build step.** No npm, no bundler, no CI, no preprocessor. Two
files and a folder of assets. If you can read HTML you can edit this site.

## Files

```
docs/
├── index.html                     the page, and its stylesheet
├── site.js                        the commercial flag and the splash video
├── .nojekyll                      stops GitHub running Jekyll over it
├── CNAME.example                  a custom domain, when there is one
├── screenshots/                   the desktop-width shots the repo README
│                                  embeds. Nothing on this site uses them -
│                                  the site has its own, below, cut to phone
│                                  width and to webp because it pays for
│                                  every byte on the hero.
└── assets/
    ├── boot_splash.mp4            copied from retrobox/assets/
    ├── boot_splash_poster.jpg     frame at 2s — what the hero shows first
    ├── social-card.png            1200×630 link preview
    ├── favicon.svg                primary icon
    ├── favicon-32.png             fallback icon
    ├── apple-touch-icon.png       180×180
    ├── shots/                     real dashboard screens, at phone width
    │   ├── viewer.webp
    │   ├── schedule.webp
    │   └── upload.webp
    └── fonts/
        ├── VT323-subset.woff2     VT323, cut down to the characters used here
        └── OFL.txt                the font's licence. It must stay beside the
                                   font wherever the font goes.
```

**The CSS is inside `index.html`, in one `<style>` block near the top.** That is
deliberate: a separate stylesheet costs a round trip before the page can paint,
and on a phone on bad data that round trip is the difference between fast and
not. One file to edit, one request to make.

## Working on it

```bash
python3 -m http.server 8000 --directory docs
# then open http://localhost:8000
```

Opening `index.html` off the disk works too, but a server is closer to the real
thing.

## Turning on the commercial section

`docs/site.js`, line 10:

```js
var COMMERCIAL_ENABLED = false;
```

Set it to `true` and three things happen:

- The **Or buy one ready to go** section appears.
- The hero's **Build your own** button is replaced by **Get one ready to go**.
- The channel numbers renumber themselves. They come from a CSS counter over
  the visible sections, so nothing needs editing by hand.

Set the two placeholders directly beneath it at the same time:

```js
var COMMERCIAL_PRICE   = 'Price to be announced';
var COMMERCIAL_CONTACT = 'mailto:hello@example.com?subject=Retro%20Box';
```

One caveat: the flag is JavaScript, so a visitor with JavaScript off never sees
the commercial section. That is the safe way round while it is off. Once it is
permanently on, delete the `hidden` attribute from `<section id="buy" … hidden>`
in `index.html` as well, and the section works with no JavaScript at all.

## The guide

Press **G** anywhere on the page and the channel guide comes up: the sections as
channels, arrow keys to browse, Enter to tune, and it clears itself off the
screen after eight seconds. There is a `GUIDE` button bottom-right for anyone
without a keyboard; it appears once you are past the hero.

This is not decoration. `g` is the box's own guide key (`retrobox/input/keymap.py`
maps `"g"` to `Action.GUIDE`), the panel reuses the same markup and styling as the
example guide in *What it actually is*, `*` and `>` mean what they mean on the box,
and eight seconds is `guide_seconds` from the config.

Rows are built from `main > section` at the moment you press G, so the lineup
follows the commercial flag with nothing to keep in sync. Every section is
reachable by scrolling regardless — with JavaScript off, the button, the panel
and the hint are never rendered.

It costs 3.3 KB gzipped.

## Speed, and how it is kept

The page is built for a phone on bad mobile data first and a desktop second.

**36 KB over five requests before anything is blocked on.** That is the HTML
(with its CSS inside it), the font, the poster frame, the icon and a 1.4 KB
script. Two round trips from cold: one for the HTML, one for everything the
preload scanner finds in it.

Three things keep it there. Don't undo them without a reason:

1. **The CSS is inlined.** See above.
2. **The font is subsetted.** `VT323-subset.woff2` is 10.8 KB, down from 32 KB
   for the whole face, because it carries only the characters this page uses.
3. **The 116 KB splash video is not on the critical path.** Its markup has no
   `<source>` and `preload="none"`, so nothing fetches it. `site.js` attaches it
   after the `load` event, and only if the visitor is not on Save Data, not on a
   2G/3G connection, and has not asked for reduced motion. Everyone else gets
   the 8 KB poster frame, which is the same picture standing still.

The dashboard screenshots are `loading="lazy"` and below the fold, so they cost
nothing until you scroll to them. They carry explicit `width`/`height`, so
nothing shifts when they arrive.

If you add anything, check it against the network tab: **the page must still
make zero requests off its own origin.**

## GitHub Pages

Settings → Pages → Source: **Deploy from a branch**, branch `main`, folder
`/docs`. That is the whole setup.

The live URL is `https://vanticstudio.github.io/RetroBoxJVProjects/`. It appears
in four places in `index.html` — `canonical`, `og:url`, `og:image` and
`twitter:image`. If the URL ever changes, change all four.

### A custom domain, later

`CNAME.example` holds a sample. GitHub reads `docs/CNAME` as the whole file with
no comments allowed, so the example is deliberately kept under a different name
and is inactive. To use a real domain: point a DNS record at GitHub Pages,
rename the file to `CNAME`, put your domain in it on one line, and update the
four absolute URLs above.

## Rules this site is built to

These are not style preferences. Breaking any of them changes what the site is.

**Nothing external.** No CDN, no Google Fonts, no analytics, no trackers, no
third-party script, no remote image.

**Every claim is checked against the code.** Not against a plan, a changelog or
a prompt — against the route, the module or the test that implements it. If it
is not in the code, it does not go on the site. A site that promises something
the box does not do generates refunds; one that undersells generates a pleasant
surprise.

**Never imply the product comes with content.** Not in copy, not in a
screenshot, not in the video, not in alt text, not in a filename. The box ships
empty. The site says so in the hero eyebrow, the guide caption, the screenshot
caption, the FAQ's first answer and the commercial section.

**Every example channel and programme name is invented.** They appear in two
places:

| Where | Channel | Programme |
|---|---|---|
| The guide table, *What it actually is* | Night Shift | The Vending Machine at the End of the Hall |
| | Static City | Nobody Answers the Phone |
| | The Late Block | Sixteen Ways to Lose a Tuesday |
| | Bumper Reel | — station ident — |
| | Sign Off | Off air |
| `assets/shots/*.webp` | the same five | the same three |

None of them exist. If you add more, keep it that way, and keep the captions
that say so.

**The setup instructions live in the main README, not here.** The site links to
them. Two copies drift, and the README is the one that gets maintained.

## Accessibility notes

- `#123B18` (`ui.dim_color`) is **never** used for text. It is 1.59:1 on the
  background. It is an unlit segment, a hairline, or an inactive border. That
  mistake has been made on this project once already.
- `prefers-reduced-motion: reduce` kills the drifting scanline on the hero and
  keeps the splash on its poster frame.
- The splash video is decorative. Nothing on the page depends on it playing.
- Channel numbers are CSS generated content on an `aria-hidden` span, so screen
  readers get the heading text and nothing else.
- Every screenshot has alt text describing what is on the screen, not what the
  screenshot is of.

## Where the design system lives

`../brand/tokens/` holds the same colours, type scale and timings as JSON and
CSS, with a note on each one saying what it is for. The page declares them
inline instead of importing that file, for the reason at the top — so if you
change a token, change both.

`../brand/` also holds the logo files, the share cards, and a deck.

## Regenerating the assets

The video and the font licence are copies:

```bash
cp retrobox/assets/boot_splash.mp4  docs/assets/
cp retrobox/assets/fonts/OFL.txt    docs/assets/fonts/
```

The font is subsetted to the characters the page uses. VT323 carries no Reserved
Font Name, so this is allowed under the OFL as long as `OFL.txt` travels with
it — which it does.

```bash
python3 - <<'PY'
from fontTools import subset
opts = subset.Options(); opts.flavor = "woff2"; opts.layout_features = []
opts.name_IDs = ['*']            # keep the attribution the OFL asks for
font = subset.load_font("retrobox/assets/fonts/VT323-Regular.ttf", opts)
s = subset.Subsetter(options=opts)
s.populate(text="".join(chr(c) for c in range(0x20, 0x7F)) + "—–·’‘“”→↗×…©°")
s.subset(font)
subset.save_font(font, "docs/assets/fonts/VT323-subset.woff2", opts)
PY
```

If you add a character the subset does not carry, it silently falls back to the
system font. Re-run the above after any copy change that introduces new symbols.

The poster frame is a still from the splash at two seconds:

```bash
ffmpeg -ss 2 -i retrobox/assets/boot_splash.mp4 -frames:v 1 -q:v 7 \
       -vf scale=800:-2 docs/assets/boot_splash_poster.jpg
```

The screenshots in `assets/shots/` are **real captures of the real dashboard**,
not mockups. To retake them: run the dashboard against a scratch config with
invented channel names, point a browser at it at 420 px wide, and capture. Keep
them as WebP around 10 KB each — they are near-monochrome and compress hard.

```bash
RETROBOX_STATUS_PATH=/tmp/rb-status.json \
  .venv/bin/python -c "from retrobox.webui import create_app; \
  create_app('/tmp/rb/config.yaml').run(host='127.0.0.1', port=8736)"
```

`RETROBOX_STATUS_PATH` matters: without it the dashboard reads the shared status
file, and anything else running on the machine will overwrite what your
screenshots show.

`social-card.png` was drawn once with Pillow. See `../brand/README.md`.
