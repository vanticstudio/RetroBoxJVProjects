"""The web dashboard: the on-screen menu, in a browser, over the LAN.

Runs as its own process (see ``scripts/retrobox-web.service``) and never
imports or touches the running player. It does exactly two things:

* reads ``status.json``, which the TV writes every couple of seconds
* writes command lines to ``control.sock``, which the TV's web input backend
  turns into ordinary :class:`~retrobox.actions.InputEvent` values

The rows it renders come from :class:`~retrobox.menu.MenuModel`, the same model
the on-screen menu uses, so the two cannot drift apart. The channel list comes
from ``config.py``'s loader rather than a second YAML parse.

There is no authentication, deliberately and consistently with the LAN file
share: anyone who can reach the box can change the channel or shut it down.
That is the right trade on a home network and the wrong one anywhere else.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .config import Config, ConfigError, load_config
from .menu import SCREEN_CHANNELS, MenuContext, MenuModel
from .status import read_status, send_command

log = logging.getLogger(__name__)

# The phosphor green from the on-screen display, so the page reads as part of
# the same product rather than a bolted-on admin panel.
GREEN = "#4DFF5A"
DIM = "#123B18"


def _context(config: Optional[Config], status: Dict[str, Any]) -> MenuContext:
    """Build the same MenuContext the on-screen menu uses, from the snapshot."""
    channels = [(c.number, c.name) for c in config.channels] if config else []
    current = (status.get("channel") or {}).get("number")
    return MenuContext(
        channels=channels,
        current_channel=current,
        volume=int(status.get("volume") or 0),
        muted=bool(status.get("muted")),
        audio_devices=(),          # switching outputs stays on the box itself
        current_audio=status.get("audio_device"),
        version=str(status.get("version") or ""),
    )


def channel_rows(config: Optional[Config], status: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The channel list, straight off MenuModel's own channels screen."""
    model = MenuModel(_context(config, status))
    model.screen = SCREEN_CHANNELS
    current = (status.get("channel") or {}).get("number")
    rows = []
    for item in model.rows():
        if not item.key.startswith("ch:"):
            continue          # drops the trailing "Back" row
        number = int(item.key[3:])
        rows.append(
            {
                "number": number,
                "label": item.label,
                "name": item.value.replace("   <", "").strip(),
                "current": number == current,
            }
        )
    return rows


def create_app(config_path: Optional[str] = None):
    """Build the Flask app. Imported lazily so Flask is only needed here."""
    from flask import Flask, jsonify, request

    app = Flask(__name__)

    def _config() -> Optional[Config]:
        # Re-read per request: cheap, and it means auto_channels additions and
        # hand edits show up without restarting the dashboard.
        try:
            return load_config(config_path) if config_path else load_config("config.yaml")
        except (ConfigError, OSError):
            log.warning("dashboard could not load the config", exc_info=True)
            return None

    def _dispatch(command: str):
        ok = send_command(command)
        return jsonify({"ok": ok, "sent": command}), (200 if ok else 503)

    # -- pages -------------------------------------------------------------
    @app.get("/")
    def index():
        return PAGE

    # -- read ---------------------------------------------------------------
    @app.get("/api/status")
    def api_status():
        status = read_status()
        return jsonify({"online": bool(status), **status})

    @app.get("/api/channels")
    def api_channels():
        return jsonify({"channels": channel_rows(_config(), read_status())})

    # -- write --------------------------------------------------------------
    @app.post("/api/tune/<int:number>")
    def api_tune(number: int):
        if not 0 <= number <= 999:
            return jsonify({"ok": False, "error": "channel out of range"}), 400
        return _dispatch(f"channel {number}")

    @app.post("/api/volume/<direction>")
    def api_volume(direction: str):
        if direction not in ("up", "down"):
            return jsonify({"ok": False, "error": "use up or down"}), 400
        return _dispatch(f"volume_{direction}")

    @app.post("/api/mute")
    def api_mute():
        return _dispatch("mute")

    @app.post("/api/power")
    def api_power():
        return _dispatch("power")

    @app.post("/api/shutdown")
    def api_shutdown():
        if request.args.get("confirm") != "yes":
            return jsonify({"ok": False, "error": "add ?confirm=yes"}), 400
        return _dispatch("shutdown")

    return app


PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Retro Box</title>
<style>
  :root { --green:""" + GREEN + """; --dim:""" + DIM + """; }
  * { box-sizing:border-box; }
  body { margin:0; padding:1.5rem; background:#05080a; color:var(--green);
         font-family:"VT323",ui-monospace,"SF Mono",Menlo,Consolas,monospace;
         font-size:20px; line-height:1.5;
         text-shadow:0 0 6px rgba(77,255,90,.45); }
  .wrap { max-width:46rem; margin:0 auto; }
  h1 { font-size:2.2rem; margin:0 0 .2rem; letter-spacing:.06em; }
  .sub { color:var(--green); opacity:.55; margin:0 0 1.6rem; font-size:.95rem; }
  .panel { border:1px solid var(--green); border-radius:4px; padding:1rem 1.1rem;
           margin-bottom:1.2rem; background:rgba(77,255,90,.03); }
  .now { font-size:1.6rem; margin:0 0 .3rem; }
  .meta { opacity:.6; font-size:.9rem; }
  .row { display:flex; justify-content:space-between; align-items:center;
         padding:.45rem .2rem; border-bottom:1px solid rgba(77,255,90,.14);
         cursor:pointer; }
  .row:last-child { border-bottom:0; }
  .row:hover, .row:focus-visible { background:rgba(77,255,90,.13); outline:none; }
  .row.on { background:rgba(77,255,90,.2); }
  .num { opacity:.65; min-width:4.5rem; }
  .name { flex:1; }
  button { font:inherit; color:var(--green); background:transparent;
           border:1px solid var(--green); border-radius:3px; padding:.45rem 1rem;
           cursor:pointer; text-shadow:inherit; }
  button:hover, button:focus-visible { background:rgba(77,255,90,.16); outline:none; }
  button.danger { border-color:#ff6b5a; color:#ff6b5a; text-shadow:0 0 6px rgba(255,107,90,.4); }
  .controls { display:flex; gap:.6rem; flex-wrap:wrap; }
  .offline { color:#ff6b5a; text-shadow:0 0 6px rgba(255,107,90,.4); }
  h2 { font-size:1rem; text-transform:uppercase; letter-spacing:.14em;
       opacity:.55; margin:0 0 .6rem; font-weight:normal; }
</style></head><body><div class="wrap">
  <h1>RETRO BOX</h1>
  <p class="sub" id="sub">connecting&hellip;</p>

  <div class="panel">
    <p class="now" id="now">&mdash;</p>
    <p class="meta" id="meta"></p>
  </div>

  <div class="panel">
    <h2>Controls</h2>
    <div class="controls">
      <button onclick="post('/api/volume/down')">VOL &minus;</button>
      <button onclick="post('/api/volume/up')">VOL +</button>
      <button onclick="post('/api/mute')">MUTE</button>
      <button onclick="post('/api/power')">STANDBY</button>
      <button class="danger" onclick="shutdown()">SHUT DOWN</button>
    </div>
  </div>

  <div class="panel">
    <h2>Channels</h2>
    <div id="channels"></div>
  </div>
</div>
<script>
async function post(url){ await fetch(url,{method:'POST'}); setTimeout(refresh,250); }
async function shutdown(){
  if(!confirm('Shut the box down?')) return;
  await fetch('/api/shutdown?confirm=yes',{method:'POST'});
  document.getElementById('sub').textContent = 'shutting down\\u2026';
}
async function refresh(){
  try{
    const s = await (await fetch('/api/status')).json();
    const sub = document.getElementById('sub');
    if(!s.online){
      sub.textContent = 'the TV process is not running';
      sub.className = 'sub offline';
      document.getElementById('now').textContent = '\\u2014';
      document.getElementById('meta').textContent = '';
      return;
    }
    sub.className = 'sub';
    sub.textContent = 'v' + (s.version||'') + ' \\u00b7 up ' + uptime(s.uptime_seconds);
    const ch = s.channel || {};
    document.getElementById('now').textContent =
      s.standby ? 'STANDBY'
      : s.off_air ? ('CH ' + pad(ch.number) + '  OFF AIR')
      : ('CH ' + pad(ch.number) + '  ' + (ch.name||''));
    const bits = [];
    bits.push(s.muted ? 'muted' : ('volume ' + s.volume));
    if(s.now_playing) bits.push(s.now_playing);
    if(s.sleep_minutes) bits.push('sleep ' + s.sleep_minutes + 'm');
    bits.push(s.hwdec ? ('hw decode: ' + s.hwdec) : 'software decode');
    document.getElementById('meta').textContent = bits.join('  \\u00b7  ');
  }catch(e){ /* keep the last good render */ }

  const list = document.getElementById('channels');
  const {channels} = await (await fetch('/api/channels')).json();
  list.innerHTML = channels.length ? '' : '<p class="meta">no channels configured</p>';
  for(const c of channels){
    const row = document.createElement('div');
    row.className = 'row' + (c.current ? ' on' : '');
    row.tabIndex = 0;
    row.innerHTML = '<span class="num">' + c.label + '</span>' +
                    '<span class="name">' + c.name + '</span>';
    const go = () => post('/api/tune/' + c.number);
    row.onclick = go;
    row.onkeydown = e => { if(e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } };
    list.appendChild(row);
  }
}
function pad(n){ return String(n ?? 0).padStart(2,'0'); }
function uptime(s){
  s = Math.floor(s||0);
  const h = Math.floor(s/3600), m = Math.floor(s%3600/60);
  return h ? (h + 'h ' + m + 'm') : (m + 'm');
}
refresh(); setInterval(refresh, 3000);
</script></body></html>
"""


def main(argv: Optional[List[str]] = None) -> int:
    """``python -m retrobox.webui`` - used by the systemd unit."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="retrobox-web", description="Retro Box web dashboard"
    )
    parser.add_argument("--host", default="0.0.0.0", help="bind address")
    parser.add_argument("--port", type=int, default=8080, help="port (default 8080)")
    parser.add_argument("-c", "--config", help="path to config.yaml")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        app = create_app(args.config)
    except ImportError:
        print("Flask is not installed. Run: pip install -e '.[web]'")
        return 1
    app.run(host=args.host, port=args.port, threaded=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
