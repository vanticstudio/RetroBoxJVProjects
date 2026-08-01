# Retro Box brand

Everything that is the identity but is not the product or the site: the logo,
the share cards, the design tokens, and the deck.

Nothing in here is loaded by the software or by `docs/`. It is a source folder.
The site keeps its own copies of the two assets it needs, in `docs/assets/`.

```
brand/
├── logo/            SVG lockups, outlined — no font needed to render them
│   └── png/         raster exports, transparent background
├── social/          share cards for GitHub, X and LinkedIn
├── tokens/          the design system, as JSON and CSS
├── deck/            an eight-slide presentation, one HTML file
└── dashboard-moments.md   two designed-but-unbuilt moments on the box itself
```

---

## Logo

The mark is not new. It is the boot splash — `retrobox/assets/boot_splash.mp4`
— redrawn as vectors. The letterforms are VT323 glyph outlines, so the files
render identically on a machine that has never heard of the font.

| File | Use it for |
|---|---|
| `retrobox-lockup-stacked-*.svg` | The default. Anywhere there is vertical room: title cards, the deck, merch, a splash. |
| `retrobox-lockup-horizontal-*.svg` | Wide, short spaces: a site header, a letterhead, a banner. |
| `retrobox-wordmark-*.svg` | The name alone, where JV Projects is already established on the page. |
| `retrobox-mark-*.svg` | Small and square: favicons, avatars, app icons, a stamp on a box. |

Two colourways, and only two:

- **phosphor** `#4DFF5A` — on black or near-black. This is the default; the
  brand lives on a dark screen.
- **ink** `#05080A` — on white or light. For print, invoices, anything on paper.

There is no white/reverse version on purpose. If the background is busy enough
to need one, use the mark on a solid `#05080A` tile instead.

### Clear space and minimum size

Clear space on all four sides is the **cap height of the wordmark** — the height
of the `R`. The SVGs already carry that padding inside their viewBox, so you can
set them flush and be right.

| Asset | Minimum |
|---|---|
| Stacked lockup | 180 px wide. Below that `JV PROJECTS` stops being letters. |
| Horizontal lockup | 240 px wide |
| Wordmark | 150 px wide |
| Mark | 16 px. It was drawn on a 32-unit grid to survive that. |

### Don't

- Don't set the wordmark in live VT323 text when a logo is wanted — the tracking
  is `0.07em` on the wordmark and `0.42em` on the sub, and getting it wrong is
  obvious. Use the SVG.
- Don't recolour it. Two colourways is the whole system.
- Don't add a glow in software. The bloom belongs on the screen, not on the file.
- Don't stretch it, outline it, or put it on a photograph.
- Don't separate the wordmark from its rule. The rule is the memorable part.

---

## Share cards

| File | Size | Where it goes |
|---|---|---|
| `social/github-social-preview.png` | 1280 × 640 | Repo → Settings → General → Social preview. **Not set yet** — without it every link to the repo unfurls as a grey box. |
| `social/x-card.png` | 1200 × 675 | X / Twitter, when posting an image rather than a link |
| `social/linkedin-card.png` | 1200 × 627 | LinkedIn posts |
| `../docs/assets/social-card.png` | 1200 × 630 | Already wired into the site's `og:image` and `twitter:image`. Lives with the site because the site serves it. |

One design across four canvases. All text sits inside the central 80%, so
nothing important is lost to a platform's own cropping.

---

## Tokens

`tokens/tokens.json` and `tokens/tokens.css` are the same values in two shapes.
Read the JSON — every token carries a `$description` saying what it is for and
which surface uses it.

**The source of truth for colour is `retrobox/config.py`** — `ui.color` and
`ui.dim_color`. If green ever changes, it changes there first, and then here.
Everything else is derived from those two and from the CRT the product imitates.

Three rules the tokens exist to protect:

1. **`--rb-unlit` (`#123B18`) is never text.** It is 1.59:1 on the background.
   It is an unlit segment, a hairline, an inactive border. That mistake has been
   made on this project once already.
2. **VT323 is never body copy.** Headings, idents, numbers, controls, the guide.
   A paragraph set in a pixel terminal face is unreadable, and it is the fastest
   way to make this look amateur.
3. **The glow goes on green only.** Bloom on off-white body text is the other
   fastest way.

The site declares the same values inline, in the `<style>` block at the top of
`docs/index.html`, rather than importing this file — a second stylesheet costs a
round trip before the page can paint. If you change a token, change both.

---

## Deck

`deck/index.html` — eight slides, one file, no build step and no framework, the
same as everything else here.

- **Present:** open it and use → / ↓ / space to advance, ← / ↑ to go back,
  Home and End for the ends. Full screen with F11 or ⌃⌘F.
- **Jump:** press **G** for the guide — the slides as channels, the same device
  the box and the site use. Arrow to a row, Enter to go there.
- **PDF:** print to PDF. It is styled for print — one slide per page, dark ink
  on white, chrome hidden.
- **It loads one thing:** the font, from `docs/assets/fonts/VT323-subset.woff2`.
  Keep them together or the deck falls back to a system mono. That file is
  subsetted; if you add a character it does not carry, see `docs/README.md`.

There are no charts, and no adoption numbers. There aren't any yet, and a made-up
one would be the least trustworthy slide in the deck. Slide 7 says so out loud.

Slide 6 shows the commercial offer as **Not live yet**, matching the site's
`COMMERCIAL_ENABLED` flag. If that flag goes true, update the slide too.

---

## Regenerating

The logo SVGs are generated from the bundled TTF by outlining glyphs, and the
PNGs and cards are drawn with Pillow from the same geometry. The generators are
dev-time scripts, not part of the project — the output is what is committed.

If the wordmark, its tracking, or the green ever change, the files are cheap to
rebuild: outline `RETRO BOX` and `JV PROJECTS` from
`retrobox/assets/fonts/VT323-Regular.ttf` at `0.07em` and `0.42em` tracking, with
a rule under the wordmark at `0.055em` thick sitting `0.10em` below the baseline.
That is the whole lockup.
