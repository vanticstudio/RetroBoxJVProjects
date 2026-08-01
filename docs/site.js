/* ===================================================================
   Retro Box — project site
   =================================================================== */

/* THE COMMERCIAL FLAG.
   false = the "Or buy one ready to go" section and its hero button stay
   hidden, and the hero offers "Build your own" instead. Set it to true
   when there is real pricing and a real way to get in touch. Set the
   two placeholders below at the same time. Nothing else changes. */
var COMMERCIAL_ENABLED = false;

var COMMERCIAL_PRICE   = 'Price to be announced';
var COMMERCIAL_CONTACT = 'mailto:hello@example.com?subject=Retro%20Box';

/* =================================================================== */

(function () {
  'use strict';

  var reduced = window.matchMedia('(prefers-reduced-motion: reduce)');

  /* --- the commercial section ------------------------------------- */

  function setCommercial(on) {
    var i, els;

    els = document.querySelectorAll('[data-commercial]');
    for (i = 0; i < els.length; i++) { els[i].hidden = !on; }

    els = document.querySelectorAll('[data-selfbuild]');
    for (i = 0; i < els.length; i++) { els[i].hidden = on; }

    if (!on) { return; }

    els = document.querySelectorAll('[data-price]');
    for (i = 0; i < els.length; i++) { els[i].textContent = COMMERCIAL_PRICE; }

    els = document.querySelectorAll('[data-contact]');
    for (i = 0; i < els.length; i++) { els[i].href = COMMERCIAL_CONTACT; }
  }

  setCommercial(COMMERCIAL_ENABLED === true);

  /* --- the boot splash --------------------------------------------
     The markup ships with no <source> and preload="none", so the 116 KB
     clip is not on the critical path at all — the page paints against the
     8 KB poster frame. We attach the video afterwards, and only when the
     connection can spare it. Somebody on a train, on Save Data, or asking
     for less motion keeps the poster and never pays for the video. */

  var splash = document.querySelector('.tube-picture');
  var attached = false;

  function connectionAllows() {
    var c = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
    if (!c) { return true; }                    /* unknown: assume it is fine */
    if (c.saveData) { return false; }
    return !/^(slow-2g|2g|3g)$/.test(c.effectiveType || '');
  }

  function attachSplash() {
    if (attached || !splash || reduced.matches || !connectionAllows()) { return; }
    var src = splash.getAttribute('data-splash');
    if (!src) { return; }
    attached = true;
    var source = document.createElement('source');
    source.src = src;
    source.type = 'video/mp4';
    splash.appendChild(source);
    splash.load();
    var playing = splash.play();
    if (playing && playing.catch) { playing.catch(function () {}); }
  }

  function honourMotion() {
    if (!splash) { return; }
    if (reduced.matches) {
      splash.pause();
      splash.currentTime = 0;
    } else {
      attachSplash();
    }
  }

  /* after the page is up, and only then */
  if (document.readyState === 'complete') {
    window.setTimeout(attachSplash, 0);
  } else {
    window.addEventListener('load', function () {
      if (window.requestIdleCallback) {
        window.requestIdleCallback(attachSplash, { timeout: 1500 });
      } else {
        window.setTimeout(attachSplash, 200);
      }
    });
  }

  if (reduced.addEventListener) {
    reduced.addEventListener('change', honourMotion);
  } else if (reduced.addListener) {
    reduced.addListener(honourMotion);
  }

  /* --- the guide ---------------------------------------------------
     The box has exactly one menu. You press guide, a green grid comes up
     over the picture, you arrow to a row and press OK, and it takes
     itself off the screen after guide_seconds. This page has the same
     menu, on the same key, over the same grid. `*` is the section you
     are in, `>` is the row under the cursor - the same two markers the
     box uses. Nothing on the page needs it: every section is reachable
     by scrolling, and with no JavaScript none of this exists. */

  var GUIDE_SECONDS = 8;          /* config: guide_seconds */

  var tuner = document.getElementById('tuner');
  var button = document.getElementById('tune-btn');
  var rowHost = document.getElementById('tuner-rows');
  var clock = document.getElementById('tuner-clock');
  var tip = document.querySelector('[data-tip]');

  if (!tuner || !button || !rowHost) { return; }

  var sections = [];
  var cursor = 0;
  var idle = null;
  var ticker = null;
  var lastFocus = null;

  function visible() {
    var all = document.querySelectorAll('main > section');
    var out = [], i;
    for (i = 0; i < all.length; i++) {
      if (!all[i].hidden) { out.push(all[i]); }
    }
    return out;
  }

  function pad(n) { return (n < 10 ? '0' : '') + n; }

  /* which section is behind the guide right now */
  function onNow() {
    var y = (document.scrollingElement || document.documentElement).scrollTop;
    var best = 0, i;
    for (i = 0; i < sections.length; i++) {
      if (sections[i].offsetTop - 120 <= y) { best = i; }
    }
    return best;
  }

  function cell(className, text) {
    var td = document.createElement('td');
    td.className = className;
    if (text) { td.textContent = text; }
    return td;
  }

  function build() {
    sections = visible();
    rowHost.textContent = '';
    var playing = onNow();

    sections.forEach(function (section, i) {
      var label = section.querySelector('.ident-label');
      var heading = section.querySelector('h2');
      var tr = document.createElement('tr');
      tr.dataset.index = i;

      var mark = cell('g-mark');
      if (i === playing) {
        mark.appendChild(document.createTextNode('*'));
        mark.title = 'The section you are in';
      }
      tr.appendChild(mark);
      tr.appendChild(cell('g-num', pad(i + 2)));
      tr.appendChild(cell('g-name', label ? label.textContent.trim() : ''));
      tr.appendChild(cell('g-show', heading ? heading.textContent.trim() : ''));

      tr.addEventListener('click', function () { tune(i); });
      tr.addEventListener('mousemove', function () { move(i); });
      rowHost.appendChild(tr);
    });

    cursor = playing;
    paintCursor();
  }

  function paintCursor() {
    var rows = rowHost.children, i, mark;
    for (i = 0; i < rows.length; i++) {
      var on = i === cursor;
      rows[i].classList.toggle('is-cursor', on);
      rows[i].setAttribute('aria-current', on ? 'true' : 'false');
      mark = rows[i].firstChild;
      /* `>` replaces `*` on the row the cursor is on, exactly as on the box */
      if (on) { mark.textContent = '>'; }
      else if (i === onNow()) { mark.textContent = '*'; }
      else { mark.textContent = ''; }
    }
  }

  function move(to) {
    if (!sections.length) { return; }
    cursor = Math.max(0, Math.min(sections.length - 1, to));
    paintCursor();
    hold();
  }

  function tick() {
    if (!clock) { return; }
    var d = new Date();
    clock.textContent = pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  /* it clears itself, the way the box's guide does */
  function hold() {
    window.clearTimeout(idle);
    idle = window.setTimeout(close, GUIDE_SECONDS * 1000);
  }

  function open() {
    lastFocus = document.activeElement;
    build();
    tick();
    tuner.hidden = false;
    document.body.style.overflow = 'hidden';
    button.setAttribute('aria-expanded', 'true');
    ticker = window.setInterval(tick, 20000);
    hold();
    tuner.focus();
    if (tip) { tip.hidden = true; }
  }

  function close(restore) {
    window.clearTimeout(idle);
    window.clearInterval(ticker);
    tuner.hidden = true;
    document.body.style.overflow = '';
    button.setAttribute('aria-expanded', 'false');
    /* Only hand focus back to something that can genuinely hold it.
       Calling focus() on <body> scrolls the page to the top in Chrome. */
    if (restore !== false && lastFocus && lastFocus !== document.body &&
        typeof lastFocus.focus === 'function') {
      lastFocus.focus({ preventScroll: true });
    }
  }

  function tune(i) {
    var section = sections[i];
    close(false);        /* focus is going to the heading, not back */
    if (!section) { return; }
    /* Wait a frame: close() has just given the page its scrollbar back, and
       the page cannot be scrolled until that is laid out. Then simply move
       focus to the heading. Focusing brings the element into view, honours
       scroll-margin-top, and puts the focus ring where the eye is going -
       one operation instead of a scroll and a focus racing each other. */
    window.requestAnimationFrame(function () {
      var target = section.querySelector('h2') || section;
      target.setAttribute('tabindex', '-1');
      target.focus();
    });
  }

  tuner.tabIndex = -1;
  button.addEventListener('click', function () {
    if (tuner.hidden) { open(); } else { close(); }
  });
  tuner.addEventListener('click', function (e) {
    if (e.target === tuner) { close(); }
  });

  document.addEventListener('keydown', function (e) {
    if (e.metaKey || e.ctrlKey || e.altKey) { return; }
    var typing = /^(input|textarea|select)$/i.test((e.target.tagName || ''));
    if (typing) { return; }

    if (tuner.hidden) {
      if (e.key === 'g' || e.key === 'G') { e.preventDefault(); open(); }
      return;
    }

    switch (e.key) {
      case 'ArrowDown': e.preventDefault(); move(cursor + 1); break;
      case 'ArrowUp': e.preventDefault(); move(cursor - 1); break;
      case 'Home': e.preventDefault(); move(0); break;
      case 'End': e.preventDefault(); move(sections.length - 1); break;
      case 'Enter': case ' ': e.preventDefault(); tune(cursor); break;
      case 'Escape': case 'g': case 'G': e.preventDefault(); close(); break;
      default: break;
    }
  });

  /* the button turns up once you are past the hero, and the hint only
     where there is a keyboard to press G with */
  button.hidden = false;
  if (tip && window.matchMedia('(hover: hover)').matches) { tip.hidden = false; }

  function watchScroll() {
    var hero = document.querySelector('.hero');
    var past = window.scrollY > (hero ? hero.offsetHeight * 0.6 : 400);
    if (past) { button.setAttribute('data-ready', ''); }
    else { button.removeAttribute('data-ready'); }
  }
  window.addEventListener('scroll', watchScroll, { passive: true });
  window.addEventListener('load', watchScroll);   /* scroll restoration */
  watchScroll();
}());
