#!/usr/bin/env python3

# Copyright (c) 2025 Ryan Lush <ryan.lush@gmail.com>
#
# Free for personal, educational, and open-source use.
# Commercial use requires written permission from the author.
# Contact: ryan.lush@gmail.com
"""
webui.py — the browser UI, kept out of hailo_tracker.py so the pipeline code
stays readable. Single self-contained page: no CDN, no build step, works on a
Pi with no internet.
"""

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Hailo Tracker</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --bg: #0d0f12; --panel: #161a1f; --line: #262c34;
    --fg: #e6e9ed; --dim: #7d8794; --accent: #24d67c; --warn: #ffb020;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--fg);
         font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }

  header { display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
           padding: 10px 14px; background: var(--panel);
           border-bottom: 1px solid var(--line); position: sticky; top: 0; z-index: 10; }
  header h1 { font-size: 14px; color: var(--accent); white-space: nowrap; }
  .pill { font-size: 11px; color: var(--dim); border: 1px solid var(--line);
          border-radius: 99px; padding: 2px 9px; white-space: nowrap; }
  .pill b { color: var(--fg); font-weight: 600; }
  .spacer { margin-left: auto; }
  .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block;
         background: var(--accent); vertical-align: middle; margin-right: 5px; }
  .dot.stale { background: #e2534a; }

  main { display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: 12px;
         padding: 12px; align-items: start; }
  @media (max-width: 900px) { main { grid-template-columns: 1fr; } }

  .stage { background: #000; border: 1px solid var(--line); border-radius: 6px;
           overflow: hidden; display: flex; justify-content: center; }
  .stage img { max-width: 100%; max-height: 82vh; display: block; }

  aside { display: flex; flex-direction: column; gap: 12px; }
  .card { background: var(--panel); border: 1px solid var(--line);
          border-radius: 6px; padding: 12px; }
  .card h2 { font-size: 11px; letter-spacing: .09em; text-transform: uppercase;
             color: var(--dim); margin-bottom: 10px; font-weight: 600; }

  label.row { display: flex; align-items: center; justify-content: space-between;
              gap: 8px; padding: 4px 0; cursor: pointer; }
  label.row span { color: var(--dim); }
  input[type=range] { width: 100%; accent-color: var(--accent); }
  input[type=checkbox] { accent-color: var(--accent); width: 15px; height: 15px; cursor: pointer; }
  input[type=text] { width: 100%; background: #0d1116; color: var(--fg);
                     border: 1px solid var(--line); border-radius: 4px;
                     padding: 6px 8px; font: inherit; }
  input[type=text]:focus { outline: none; border-color: var(--accent); }

  .chips { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 8px;
           max-height: 168px; overflow-y: auto; }
  .chip { font-size: 11px; padding: 3px 8px; border-radius: 99px; cursor: pointer;
          border: 1px solid var(--line); color: var(--dim); user-select: none;
          background: transparent; }
  .chip.on { color: #06110b; font-weight: 700; }

  button { font: inherit; background: #1e242b; color: var(--fg); cursor: pointer;
           border: 1px solid var(--line); border-radius: 4px; padding: 6px 10px; }
  button:hover { border-color: var(--accent); color: var(--accent); }
  .btn-row { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 10px; }

  table { width: 100%; border-collapse: collapse; font-size: 11.5px; }
  th { text-align: left; color: var(--dim); font-weight: 600; padding: 3px 0;
       border-bottom: 1px solid var(--line); }
  td { padding: 3px 0; border-bottom: 1px solid #1b2027; }
  td.num { text-align: right; color: var(--dim); }
  .empty { color: var(--dim); font-style: italic; padding: 6px 0; }

  .live { display: flex; flex-direction: column; gap: 3px; }
  .live div { display: flex; justify-content: space-between; }
  .live .k { color: var(--dim); }

  .toast { position: fixed; bottom: 16px; left: 50%; transform: translateX(-50%);
           background: var(--accent); color: #06110b; font-weight: 600;
           padding: 8px 16px; border-radius: 6px; opacity: 0;
           transition: opacity .25s; pointer-events: none; }
  .toast.show { opacity: 1; }
  footer { padding: 10px 14px; color: #4c545e; font-size: 11px; text-align: center; }
  footer a { color: #667; }
</style>
</head>
<body>

<header>
  <h1>&#x1F4F9; Hailo Tracker</h1>
  <span class="pill"><span class="dot" id="dot"></span><b id="hFps">–</b> fps</span>
  <span class="pill"><b id="hMs">–</b> ms NPU</span>
  <span class="pill"><b id="hLive">0</b> tracked now</span>
  <span class="pill" id="hModel">–</span>
  <span class="spacer"></span>
  <span class="pill" id="hUptime">–</span>
</header>

<main>
  <div class="stage"><img src="/video" alt="Live detection feed"></div>

  <aside>
    <div class="card">
      <h2>Detection</h2>
      <label class="row">
        <span>Confidence</span><b id="confVal">0.40</b>
      </label>
      <input type="range" id="conf" min="0.05" max="0.95" step="0.05" value="0.40">

      <label class="row"><span>Labels</span>
        <input type="checkbox" id="showLabels" checked></label>
      <label class="row"><span>Track IDs</span>
        <input type="checkbox" id="showIds" checked></label>
      <label class="row"><span>Motion trails</span>
        <input type="checkbox" id="showTrails"></label>
      <label class="row"><span>Confirmed only</span>
        <input type="checkbox" id="confirmedOnly" checked></label>
      <label class="row"><span>Show ROI</span>
        <input type="checkbox" id="showRoi"></label>
    </div>

    <div class="card">
      <h2>Classes</h2>
      <input type="text" id="filter" placeholder="filter 80 classes…" autocomplete="off">
      <div class="chips" id="chips"></div>
      <div class="btn-row">
        <button id="btnAll">All</button>
        <button id="btnNone">None</button>
        <button id="btnPets">Pets</button>
        <button id="btnPeople">People</button>
      </div>
    </div>

    <div class="card">
      <h2>Live</h2>
      <div class="live">
        <div><span class="k">Frames</span><span id="sFrames">–</span></div>
        <div><span class="k">Dropped</span><span id="sDropped">–</span></div>
        <div><span class="k">Camera restarts</span><span id="sErrors">–</span></div>
        <div><span class="k">Total tracks</span><span id="sTracks">–</span></div>
      </div>
      <div class="btn-row">
        <button id="btnSnap">Save snapshot</button>
        <button id="btnCsv">Export CSV</button>
      </div>
    </div>

    <div class="card">
      <h2>Recent events</h2>
      <table>
        <thead><tr><th>Time</th><th>Object</th><th class="num">Dur</th><th class="num">Conf</th></tr></thead>
        <tbody id="events"><tr><td colspan="4" class="empty">waiting…</td></tr></tbody>
      </table>
    </div>
  </aside>
</main>

<footer>
  <a href="/stats">stats</a> &middot; <a href="/events">events</a> &middot;
  <a href="/tracks">tracks</a> &middot; <a href="/snapshot">snapshot</a> &middot;
  <a href="/metrics">metrics</a> &middot; <a href="/healthz">health</a>
</footer>

<div class="toast" id="toast"></div>

<script>
const COCO = __COCO__;
const COLORS = __COLORS__;
let selected = new Set(__TRACKED__);
let pushTimer = null;

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 1600);
}

// ---------- class chips ----------
function renderChips() {
  const q = document.getElementById('filter').value.trim().toLowerCase();
  const box = document.getElementById('chips');
  box.innerHTML = '';
  COCO.forEach((name, i) => {
    if (q && !name.includes(q)) return;
    const el = document.createElement('span');
    el.className = 'chip' + (selected.has(i) ? ' on' : '');
    el.textContent = name;
    if (selected.has(i)) el.style.background = COLORS[i];
    el.onclick = () => {
      selected.has(i) ? selected.delete(i) : selected.add(i);
      renderChips(); queuePush();
    };
    box.appendChild(el);
  });
}

function setClasses(names) {
  selected = new Set(names.map(n => COCO.indexOf(n)).filter(i => i >= 0));
  renderChips(); queuePush();
}

document.getElementById('filter').oninput = renderChips;
document.getElementById('btnAll').onclick = () => {
  selected = new Set(COCO.map((_, i) => i)); renderChips(); queuePush();
};
document.getElementById('btnNone').onclick = () => {
  selected = new Set(); renderChips(); queuePush();
};
document.getElementById('btnPets').onclick = () => setClasses(['cat', 'dog', 'bird']);
document.getElementById('btnPeople').onclick = () => setClasses(['person']);

// ---------- config push ----------
function queuePush() {
  clearTimeout(pushTimer);
  pushTimer = setTimeout(pushConfig, 250);
}

async function pushConfig() {
  // Empty selection means "everything" server-side, so send the full list
  // explicitly when the user has picked some but not all.
  const all = selected.size === COCO.length;
  const body = {
    conf_thresh: parseFloat(document.getElementById('conf').value),
    tracked_classes: all ? [] : [...selected].map(i => COCO[i]),
    show_labels: document.getElementById('showLabels').checked,
    show_ids: document.getElementById('showIds').checked,
    show_trails: document.getElementById('showTrails').checked,
    confirmed_only: document.getElementById('confirmedOnly').checked,
    show_roi: document.getElementById('showRoi').checked,
  };
  try {
    await fetch('/api/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
  } catch (e) { toast('config failed'); }
}

const conf = document.getElementById('conf');
conf.oninput = () => {
  document.getElementById('confVal').textContent = parseFloat(conf.value).toFixed(2);
  queuePush();
};
['showLabels', 'showIds', 'showTrails', 'confirmedOnly', 'showRoi']
  .forEach(id => document.getElementById(id).onchange = queuePush);

// ---------- buttons ----------
document.getElementById('btnSnap').onclick = async () => {
  const r = await fetch('/api/snapshot', {method: 'POST'});
  const j = await r.json();
  toast(j.saved ? 'Saved ' + j.saved : 'Nothing to save');
};
document.getElementById('btnCsv').onclick = () => { window.location = '/events.csv'; };

// ---------- polling ----------
function fmtDur(s) {
  if (s === null || s === undefined) return '–';
  if (s < 60) return s.toFixed(0) + 's';
  return Math.floor(s / 60) + 'm' + String(Math.round(s % 60)).padStart(2, '0');
}

async function poll() {
  try {
    const s = await (await fetch('/stats')).json();
    document.getElementById('hFps').textContent = s.fps.toFixed(1);
    document.getElementById('hMs').textContent = s.inference_ms.toFixed(1);
    document.getElementById('hLive').textContent = s.live_tracks;
    document.getElementById('hModel').textContent = s.model;
    document.getElementById('hUptime').textContent = 'up ' + fmtDur(s.uptime_s);
    document.getElementById('sFrames').textContent = s.frames.toLocaleString();
    document.getElementById('sDropped').textContent = s.dropped.toLocaleString();
    document.getElementById('sErrors').textContent = s.capture_errors;
    document.getElementById('sTracks').textContent = s.total_tracks.toLocaleString();
    document.getElementById('dot').className = 'dot' + (s.healthy ? '' : ' stale');
  } catch (e) {
    document.getElementById('dot').className = 'dot stale';
  }

  try {
    const ev = await (await fetch('/events?limit=12')).json();
    const tb = document.getElementById('events');
    if (!ev.length) {
      tb.innerHTML = '<tr><td colspan="4" class="empty">no events yet</td></tr>';
    } else {
      tb.innerHTML = ev.map(e => {
        const t = (e.started_iso || '').split('T')[1] || '';
        const i = COCO.indexOf(e.class);
        const c = i >= 0 ? COLORS[i] : '#888';
        return '<tr><td>' + t + '</td>' +
               '<td><span style="color:' + c + '">' + e.class + '</span></td>' +
               '<td class="num">' + fmtDur(e.duration_s) + '</td>' +
               '<td class="num">' + (e.max_conf ? Math.round(e.max_conf * 100) + '%' : '–') + '</td></tr>';
      }).join('');
    }
  } catch (e) { /* stream is what matters; table can wait */ }

  setTimeout(poll, 2000);
}

// ---------- init ----------
(async () => {
  try {
    const c = await (await fetch('/api/config')).json();
    conf.value = c.conf_thresh;
    document.getElementById('confVal').textContent = c.conf_thresh.toFixed(2);
    document.getElementById('showLabels').checked = c.show_labels;
    document.getElementById('showIds').checked = c.show_ids;
    document.getElementById('showTrails').checked = c.show_trails;
    document.getElementById('confirmedOnly').checked = c.confirmed_only;
    document.getElementById('showRoi').checked = c.show_roi;
    selected = new Set((c.tracked_classes.length ? c.tracked_classes : COCO)
                        .map(n => COCO.indexOf(n)).filter(i => i >= 0));
  } catch (e) { /* fall back to template defaults */ }
  renderChips();
  poll();
})();
</script>
</body>
</html>"""


def render(coco_classes, css_colors, tracked_ids):
    """Inline the class list and palette so the page needs no extra round-trip."""
    import json
    return (PAGE
            .replace("__COCO__", json.dumps(coco_classes))
            .replace("__COLORS__", json.dumps(css_colors))
            .replace("__TRACKED__", json.dumps(sorted(tracked_ids))))
