#!/usr/bin/env python3
"""
The whole catalog in one command.

    python3 scripts/dex.py

Opens http://127.0.0.1:8777. Everything happens in the browser from there:
drop photos in, fill the form, place the pin. Each save rebuilds the site
automatically, so the preview at /site/ is always current. When you're
happy, hit Publish and it commits and pushes to GitHub.

Nothing leaves your machine except the push you ask for. This is a stdlib
HTTP server bound to localhost, not something you deploy.

scripts/build.py still works on its own if you'd rather drive it by hand.
"""

from __future__ import annotations

import csv
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import unicodedata
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    sys.exit("Pillow is required:  pip install Pillow")

ROOT = Path(__file__).resolve().parent.parent
PHOTOS = ROOT / "photos"
STAGING = ROOT / ".staging"
DATA = ROOT / "data" / "species.csv"
PORT = int(os.environ.get("DEX_INGEST_PORT", "8777"))
SITE = ROOT / "site"
MAX_UPLOAD = 80 * 1024 * 1024

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402
from build import gps_from_pillow, pillow_date, slugify, sane  # noqa: E402

LAST_BUILD = {}


def rebuild() -> dict:
    """Regenerate the site. Cheap — build.py reuses thumbnails it already made."""
    try:
        LAST_BUILD.clear()
        LAST_BUILD.update(build.main(quiet=True))
        LAST_BUILD["ok"] = True
    except Exception as err:
        LAST_BUILD.clear()
        LAST_BUILD.update({"ok": False, "error": str(err)})
        print(f"  build failed: {err}")
    return dict(LAST_BUILD)


def git(*args, timeout=120):
    """Run a git command in the repo. Returns (ok, combined output)."""
    try:
        done = subprocess.run(("git",) + args, cwd=ROOT, capture_output=True,
                              text=True, timeout=timeout)
    except FileNotFoundError:
        return (False, "git isn't installed. Install Xcode command line tools.")
    except subprocess.TimeoutExpired:
        return (False, "git timed out. If this is the first push, the repo may "
                       "be waiting on an SSH passphrase — run the push in a "
                       "terminal once.")
    out = (done.stdout + done.stderr).strip()
    return (done.returncode == 0, out)


def git_state() -> dict:
    inside, _ = git("rev-parse", "--is-inside-work-tree")
    if not inside:
        return {"ready": False, "why": "not a git repository yet"}
    has_remote, remotes = git("remote")
    if not has_remote or not remotes.strip():
        return {"ready": False, "why": "no git remote configured"}
    _, status = git("status", "--porcelain")
    return {"ready": True, "changes": len([l for l in status.splitlines() if l.strip()])}


def species_rows() -> list:
    if not DATA.exists():
        return []
    out = []
    with DATA.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("scientific_name") or "").strip()
            if name:
                out.append({
                    "sci": name,
                    "common": (row.get("common_name") or "").strip(),
                    "haw": (row.get("hawaiian_name") or "").strip(),
                    "family": (row.get("family") or "").strip(),
                    "status": (row.get("status") or "").strip(),
                })
    return sorted(out, key=lambda r: r["sci"])


def already_shot() -> set:
    """Scientific names that already have at least one photo filed."""
    names = set()
    for side in PHOTOS.rglob("*.json"):
        try:
            names.add(json.loads(side.read_text(encoding="utf-8"))
                      .get("scientific_name", ""))
        except Exception:
            continue
    return {n for n in names if n}


def safe_name(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._")
    return text or "photo"


def unique_path(folder: Path, stem: str, suffix: str) -> Path:
    candidate = folder / f"{stem}{suffix}"
    n = 2
    while candidate.exists():
        candidate = folder / f"{stem}-{n:02d}{suffix}"
        n += 1
    return candidate


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else ""):
            sys.stderr.write("  %s\n" % (fmt % args))

    # ---------------------------------------------------------------- helpers

    def send_json(self, payload, code=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -------------------------------------------------------------------- GET

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            return self.send_bytes(PAGE.encode("utf-8"), "text/html; charset=utf-8")

        if path == "/api/species":
            return self.send_json({"species": species_rows(),
                                   "shot": sorted(already_shot())})

        if path == "/api/status":
            return self.send_json({"build": dict(LAST_BUILD), "git": git_state()})

        if path == "/site" or path == "/site/":
            self.send_response(302)
            self.send_header("Location", "/site/index.html")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path.startswith("/site/"):
            rel = urllib.parse.unquote(path[len("/site/"):])
            target = (SITE / rel).resolve()
            if not str(target).startswith(str(SITE.resolve())) or not target.is_file():
                return self.send_json({"error": "not built yet"}, 404)
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            return self.send_bytes(target.read_bytes(), ctype)

        if path.startswith("/staging/"):
            name = safe_name(path[len("/staging/"):])
            target = STAGING / name
            if not target.exists():
                return self.send_json({"error": "not found"}, 404)
            return self.send_bytes(target.read_bytes(), "image/jpeg")

        return self.send_json({"error": "not found"}, 404)

    # ------------------------------------------------------------------- POST

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            return self.send_json({"error": "file too large"}, 413)
        body = self.rfile.read(length) if length else b""

        if parsed.path == "/api/stage":
            return self.stage(parsed, body)
        if parsed.path == "/api/commit":
            return self.commit(body)
        if parsed.path == "/api/build":
            return self.send_json({"build": rebuild()})
        if parsed.path == "/api/publish":
            return self.publish(body)
        return self.send_json({"error": "not found"}, 404)

    def publish(self, body):
        state = git_state()
        if not state.get("ready"):
            return self.send_json({"error": state.get("why", "git isn't set up")}, 400)

        try:
            message = json.loads(body or b"{}").get("message", "")
        except Exception:
            message = ""
        message = (message or "").strip() or "Add photos"

        summary = rebuild()
        if not summary.get("ok"):
            return self.send_json({"error": "build failed: "
                                            + summary.get("error", "")}, 500)

        ok, out = git("add", "-A")
        if not ok:
            return self.send_json({"error": out}, 500)

        _, staged = git("diff", "--cached", "--name-only")
        if not staged.strip():
            return self.send_json({"ok": True, "note": "nothing new to publish"})

        ok, out = git("commit", "-m", message)
        if not ok:
            return self.send_json({"error": out}, 500)

        ok, push_out = git("push")
        if not ok and "no upstream branch" in push_out:
            # First push of a fresh repo: set the tracking branch and retry.
            got_branch, branch = git("rev-parse", "--abbrev-ref", "HEAD")
            if got_branch and branch.strip():
                ok, push_out = git("push", "--set-upstream", "origin", branch.strip())
        if not ok:
            return self.send_json({
                "error": "committed, but the push failed:\n" + push_out}, 500)

        count = len([l for l in staged.splitlines() if l.strip()])
        print(f"  published {count} file(s)")
        return self.send_json({"ok": True,
                               "note": f"pushed {count} file(s). "
                                       "GitHub Pages takes a minute or two."})

    def stage(self, parsed, body):
        """Save the upload to .staging/ and report what its EXIF already knows."""
        query = urllib.parse.parse_qs(parsed.query)
        original = (query.get("name") or ["photo.jpg"])[0]
        STAGING.mkdir(exist_ok=True)

        suffix = Path(original).suffix.lower() or ".jpg"
        staged = unique_path(STAGING, safe_name(Path(original).stem), suffix)
        staged.write_bytes(body)

        try:
            with Image.open(staged) as im:
                im.verify()
        except Exception:
            staged.unlink(missing_ok=True)
            return self.send_json({"error": "that file isn't a readable image"}, 400)

        preview = STAGING / (staged.stem + "-preview.jpg")
        with Image.open(staged) as im:
            im = im.convert("RGB")
            im.thumbnail((900, 900), Image.LANCZOS)
            im.save(preview, "JPEG", quality=82)

        lat, lng = gps_from_pillow(staged)
        return self.send_json({
            "id": staged.name,
            "original": original,
            "preview": "/staging/" + preview.name,
            "date": pillow_date(staged),
            "lat": lat if sane(lat, lng) else None,
            "lng": lng if sane(lat, lng) else None,
            "guess": Path(original).stem.replace("_", " "),
        })

    def commit(self, body):
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return self.send_json({"error": "bad request"}, 400)

        staged = STAGING / safe_name(payload.get("id", ""))
        if not staged.exists():
            return self.send_json({"error": "that upload expired, add it again"}, 400)

        sci = (payload.get("scientific_name") or "").strip()
        if not sci:
            return self.send_json({"error": "pick a species first"}, 400)

        lat, lng = payload.get("lat"), payload.get("lng")
        if lat is not None and lng is not None:
            try:
                lat, lng = float(lat), float(lng)
            except (TypeError, ValueError):
                return self.send_json({"error": "those coordinates don't parse"}, 400)
            if not sane(lat, lng):
                return self.send_json({"error": "those coordinates are out of range"}, 400)
        else:
            lat = lng = None

        PHOTOS.mkdir(exist_ok=True)
        stem = slugify(sci).replace("-", "_")
        dest = unique_path(PHOTOS, stem, staged.suffix)
        shutil.move(str(staged), dest)

        sidecar = {
            "scientific_name": sci,
            "date": (payload.get("date") or "").strip(),
            "site": (payload.get("site") or "").strip(),
            "lat": lat,
            "lng": lng,
            "nomap": bool(payload.get("nomap")),
        }
        if payload.get("note"):
            sidecar["note"] = str(payload["note"]).strip()
        dest.with_suffix(".json").write_text(
            json.dumps(sidecar, indent=1, ensure_ascii=False), encoding="utf-8")

        preview = STAGING / (staged.stem + "-preview.jpg")
        preview.unlink(missing_ok=True)

        print(f"  filed {dest.name}  ->  {sci}"
              + (f"  @ {lat:.5f}, {lng:.5f}" if lat is not None else "  (no position)"))
        summary = rebuild()
        return self.send_json({"ok": True, "file": dest.name, "build": summary})


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><title>Add photos</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
:root{--bg:#061a24;--panel:#0d2c3a;--line:#1f5063;--fg:#eaf2f2;--muted:#86a4ad;--gold:#f5b840}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font:400 15px/1.55 "IBM Plex Sans",system-ui,sans-serif}
header{padding:1.4rem 5vw 1rem;border-bottom:1px solid var(--line);
 position:sticky;top:0;background:var(--bg);z-index:500}
.bar{display:flex;justify-content:space-between;align-items:center;gap:1rem;flex-wrap:wrap}
h1{margin:0;font-size:1.3rem;font-weight:600}
header p{margin:.3rem 0 0;color:var(--muted);font-size:.88rem}
.actions{display:flex;gap:.5rem;align-items:center}
.btn{font:inherit;font-size:.9rem;padding:.5rem .95rem;border-radius:2px;cursor:pointer;
 background:var(--gold);color:#16202a;border:0;font-weight:500;text-decoration:none;
 display:inline-block}
.btn.ghost{background:transparent;color:var(--muted);border:1px solid var(--line)}
.btn:disabled{opacity:.45;cursor:default}
.pubmsg{white-space:pre-wrap;font-size:.85rem}
.pubmsg.bad{color:#ff9c7d}.pubmsg.good{color:#7ee0c0}
main{padding:1.4rem 5vw 5rem;max-width:1100px}
#drop{border:1px dashed var(--line);border-radius:3px;padding:2.4rem 1rem;text-align:center;
 color:var(--muted);cursor:pointer;background:rgba(255,255,255,.015)}
#drop.hot{border-color:var(--gold);color:var(--fg)}
#drop input{display:none}
.item{display:grid;grid-template-columns:minmax(0,240px) minmax(0,1fr) minmax(0,360px);
 gap:1.2rem;padding:1.2rem 0;border-bottom:1px solid var(--line);align-items:start}
.item img.shot{width:100%;border-radius:2px;display:block}
label{display:block;font-size:.8rem;color:var(--muted);margin:.7rem 0 .25rem}
label:first-child{margin-top:0}
input[type=text],input[type=date],select,textarea{width:100%;font:inherit;font-size:.92rem;
 color:var(--fg);background:var(--panel);border:1px solid var(--line);border-radius:2px;padding:.45rem .6rem}
input:focus,select:focus,textarea:focus{outline:2px solid var(--gold);outline-offset:1px}
.map{height:250px;border:1px solid var(--line);border-radius:2px}
.coords{font-size:.8rem;color:var(--muted);margin:.4rem 0 0;font-variant-numeric:tabular-nums}
.row{display:flex;gap:.6rem;align-items:center;margin-top:.8rem;flex-wrap:wrap}
button{font:inherit;font-size:.9rem;padding:.5rem .95rem;border-radius:2px;cursor:pointer;
 background:var(--gold);color:#16202a;border:0;font-weight:500}
button.ghost{background:transparent;color:var(--muted);border:1px solid var(--line)}
button:disabled{opacity:.45;cursor:default}
.msg{font-size:.85rem;margin-left:.2rem}
.msg.bad{color:#ff9c7d}.msg.good{color:#7ee0c0}
.done{opacity:.45}
.hint{font-size:.78rem;color:var(--muted);margin:.3rem 0 0}
.check{display:flex;align-items:center;gap:.45rem;margin-top:.7rem;font-size:.85rem;color:var(--muted)}
.check input{accent-color:var(--gold)}
.new{color:var(--gold)}
</style></head><body>
<header>
  <div class="bar">
    <div>
      <h1>Hawaiian Reef Fish</h1>
      <p id="count">Counting…</p>
    </div>
    <div class="actions">
      <a class="btn ghost" href="/site/index.html" target="_blank" rel="noopener">Preview site</a>
      <button id="publish" class="btn">Publish</button>
    </div>
  </div>
  <p id="pubmsg" class="pubmsg"></p>
</header>
<main>
  <div id="drop">Drop photos here, or click to choose files
    <input type="file" id="file" multiple accept="image/*"></div>
  <div id="list"></div>
</main>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
var SPECIES = [], SHOT = new Set();
var HAWAII = [[18.6,-179.5],[22.6,-154.6]];

function refreshStatus(){
  return fetch("/api/status").then(function(r){return r.json()}).then(function(d){
    var b = d.build || {};
    var el = document.getElementById("count");
    if (b.ok === false) { el.textContent = "Build error: " + (b.error||""); return d; }
    if (b.total) {
      el.textContent = b.seen + " of " + b.total + " species · " +
        b.frames + " frames · " + b.located + " placed on the map";
    }
    var pub = document.getElementById("publish");
    if (d.git && !d.git.ready) {
      pub.disabled = true; pub.title = d.git.why;
      pub.textContent = "Publish (" + d.git.why + ")";
    }
    return d;
  });
}

document.addEventListener("DOMContentLoaded", function(){
  refreshStatus();
  document.getElementById("publish").addEventListener("click", function(){
    var pub = this, msg = document.getElementById("pubmsg");
    var what = prompt("Commit message", "Add photos");
    if (what === null) return;
    pub.disabled = true; pub.textContent = "Publishing…";
    msg.className = "pubmsg"; msg.textContent = "";
    fetch("/api/publish", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({message: what})})
      .then(function(r){return r.json()}).then(function(res){
        pub.disabled = false; pub.textContent = "Publish";
        msg.className = "pubmsg " + (res.error ? "bad" : "good");
        msg.textContent = res.error || res.note || "Published.";
      });
  });
});

fetch("/api/species").then(function(r){return r.json()}).then(function(d){
  SPECIES = d.species; SHOT = new Set(d.shot);
});

var drop = document.getElementById("drop"), input = document.getElementById("file");
drop.addEventListener("click", function(){ input.click(); });
input.addEventListener("change", function(){ add(input.files); input.value = ""; });
["dragenter","dragover"].forEach(function(ev){
  drop.addEventListener(ev, function(e){ e.preventDefault(); drop.classList.add("hot"); });
});
["dragleave","drop"].forEach(function(ev){
  drop.addEventListener(ev, function(e){ e.preventDefault(); drop.classList.remove("hot"); });
});
drop.addEventListener("drop", function(e){ add(e.dataTransfer.files); });

function add(files){
  Array.prototype.forEach.call(files, function(f){
    if (f.type.indexOf("image/") !== 0) return;
    fetch("/api/stage?name=" + encodeURIComponent(f.name), {
      method:"POST", headers:{"Content-Type":"application/octet-stream"}, body:f
    }).then(function(r){return r.json()}).then(function(d){
      if (d.error) { alert(f.name + ": " + d.error); return; }
      render(d);
    });
  });
}

function matchGuess(text){
  var q = (text||"").toLowerCase().replace(/[^a-z ]/g," ").replace(/\s+/g," ").trim();
  if (!q) return "";
  for (var i=0;i<SPECIES.length;i++){
    var s = SPECIES[i];
    if (q.indexOf(s.sci.toLowerCase()) !== -1) return s.sci;
    if (s.common && q.indexOf(s.common.toLowerCase()) !== -1) return s.sci;
  }
  return "";
}

function render(d){
  var wrap = document.createElement("div");
  wrap.className = "item";

  var mapId = "m-" + d.id.replace(/[^a-z0-9]/gi,"");
  var opts = SPECIES.map(function(s){
    var extra = [s.common, s.haw].filter(Boolean).join(" / ");
    var flag = SHOT.has(s.sci) ? "" : " +";
    return '<option value="'+s.sci+'">'+s.sci+(extra?" — "+extra:"")+flag+'</option>';
  }).join("");

  wrap.innerHTML =
    '<div><img class="shot" src="'+d.preview+'" alt=""><p class="hint">'+d.original+'</p></div>'+
    '<div>'+
      '<label>Species</label>'+
      '<input type="text" class="sci" list="'+mapId+'-list" placeholder="Start typing a name">'+
      '<datalist id="'+mapId+'-list">'+opts+'</datalist>'+
      '<p class="hint">A <span class="new">+</span> marks a species you have no photo of yet.</p>'+
      '<label>Date</label><input type="date" class="date" value="'+(d.date||"")+'">'+
      '<label>Site</label><input type="text" class="site" placeholder="Kāneʻohe Bay">'+
      '<label>Note</label><textarea class="note" rows="2"></textarea>'+
      '<div class="check"><input type="checkbox" class="nomap" id="'+mapId+'-nm">'+
        '<label for="'+mapId+'-nm" style="margin:0">Keep this position off the public map</label></div>'+
    '</div>'+
    '<div>'+
      '<label>Position — click the map to place it</label>'+
      '<div class="map" id="'+mapId+'"></div>'+
      '<p class="coords">No position set</p>'+
      '<div class="row"><button class="save">Save</button>'+
        '<button class="ghost clear">Clear pin</button>'+
        '<span class="msg"></span></div>'+
    '</div>';

  document.getElementById("list").prepend(wrap);

  var sci = wrap.querySelector(".sci");
  sci.value = matchGuess(d.guess);

  var map = L.map(mapId, { scrollWheelZoom:true });
  L.tileLayer("https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2024_3857/default/g/{z}/{y}/{x}.jpg",
    { maxZoom:18, maxNativeZoom:14,
      attribution:"Sentinel-2 cloudless by EOX (CC BY 4.0)" }).addTo(map);

  var marker = null;
  var coords = wrap.querySelector(".coords");

  function place(lat, lng, zoom){
    if (marker) marker.setLatLng([lat,lng]);
    else marker = L.marker([lat,lng], {draggable:true}).addTo(map)
      .on("dragend", function(){ var p = marker.getLatLng(); show(p.lat, p.lng); });
    show(lat, lng);
    if (zoom) map.setView([lat,lng], zoom);
  }
  function show(lat, lng){
    coords.textContent = lat.toFixed(6) + ", " + lng.toFixed(6);
    coords.dataset.lat = lat; coords.dataset.lng = lng;
  }

  if (d.lat !== null && d.lng !== null) { place(d.lat, d.lng, 13); }
  else { map.fitBounds(HAWAII); }

  map.on("click", function(e){ place(e.latlng.lat, e.latlng.lng); });
  wrap.querySelector(".clear").addEventListener("click", function(){
    if (marker) { map.removeLayer(marker); marker = null; }
    coords.textContent = "No position set";
    delete coords.dataset.lat; delete coords.dataset.lng;
  });

  var msg = wrap.querySelector(".msg");
  var save = wrap.querySelector(".save");
  save.addEventListener("click", function(){
    msg.className = "msg"; msg.textContent = "";
    var body = {
      id: d.id,
      scientific_name: sci.value.split(" — ")[0].trim(),
      date: wrap.querySelector(".date").value,
      site: wrap.querySelector(".site").value,
      note: wrap.querySelector(".note").value,
      nomap: wrap.querySelector(".nomap").checked,
      lat: coords.dataset.lat ? parseFloat(coords.dataset.lat) : null,
      lng: coords.dataset.lng ? parseFloat(coords.dataset.lng) : null
    };
    save.disabled = true;
    fetch("/api/commit", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(body)}).then(function(r){return r.json()}).then(function(res){
      if (res.error){ msg.className="msg bad"; msg.textContent=res.error; save.disabled=false; return; }
      msg.className = "msg good";
      msg.textContent = "Filed as " + res.file + " · site rebuilt";
      SHOT.add(body.scientific_name);
      refreshStatus();
      wrap.classList.add("done");
      wrap.querySelector(".clear").disabled = true;
    });
  });
}
</script></body></html>
"""


def main() -> None:
    PHOTOS.mkdir(exist_ok=True)
    STAGING.mkdir(exist_ok=True)
    url = f"http://127.0.0.1:{PORT}"
    print("Starting up — building the site first")
    summary = rebuild()
    if summary.get("ok"):
        print(f"  {summary['seen']} of {summary['total']} species, "
              f"{summary['frames']} frames")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"\nRunning at {url}")
    print("  add photos, preview, and publish all from that page")
    print("  stop with Ctrl-C\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
        for leftover in STAGING.glob("*-preview.jpg"):
            leftover.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
