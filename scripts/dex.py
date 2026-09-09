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
import hashlib
import json
import mimetypes
import urllib.request
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
DIVES = ROOT / "data" / "dives.csv"
DIVE_FIELDS = ["id", "label", "date", "site", "lat", "lng", "notes"]
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


def filed_photos() -> list:
    """Everything currently in photos/, newest first."""
    out = []
    for photo in PHOTOS.iterdir():
        if photo.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp",
                                        ".tif", ".tiff"}:
            continue
        side = photo.with_suffix(".json")
        data = {}
        if side.is_file():
            try:
                data = json.loads(side.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        out.append({
            "file": photo.name,
            "url": "/filed/" + photo.name,
            "scientific_name": data.get("scientific_name", ""),
            "date": data.get("date", ""),
            "site": data.get("site", ""),
            "sex": data.get("sex", ""),
            "stage": data.get("stage", ""),
            "dive": data.get("dive", ""),
            "own_position": bool(data.get("own_position")),
            "note": data.get("note", ""),
            "nomap": bool(data.get("nomap")),
            "lat": data.get("lat"),
            "lng": data.get("lng"),
            "sidecar": side.is_file(),
            "mtime": photo.stat().st_mtime,
        })
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


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


STATUSES = ["endemic", "endemic_nwhi", "indigenous", "introduced",
            "not_in_hawaii", "waif", "questionable"]

SEX_LABEL = {"female": "Female", "male": "Male",
             "transitioning": "Transitioning"}
STAGE_LABEL = {"juvenile": "Juvenile", "intermediate": "Intermediate",
               "adult": "Adult"}


def worms_lookup(name: str) -> dict:
    """Ask WoRMS for the accepted name, family and AphiaID. Best effort."""
    if len(name.split()) < 2:
        return {"error": "type a full scientific name first"}
    url = ("https://www.marinespecies.org/rest/AphiaRecordsByName/"
           + urllib.parse.quote(name) + "?like=false&marine_only=true")
    try:
        with urllib.request.urlopen(url, timeout=12) as resp:
            if resp.status == 204:
                return {"error": f"WoRMS has no record for {name}"}
            records = json.loads(resp.read().decode("utf-8"))
    except Exception as err:
        return {"error": f"couldn't reach WoRMS ({err})"}
    if not records:
        return {"error": f"WoRMS has no record for {name}"}
    rec = next((r for r in records if r.get("status") == "accepted"), records[0])
    return {
        "aphia_id": str(rec.get("AphiaID") or ""),
        "family": rec.get("family") or "",
        "accepted": rec.get("valid_name") or "",
        "worms_status": rec.get("status") or "",
    }


def dive_rows() -> list:
    if not DIVES.exists():
        return []
    with DIVES.open(newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if (r.get("id") or "").strip()]
    for r in rows:
        for key in ("lat", "lng"):
            try:
                r[key] = float(r[key])
            except (TypeError, ValueError):
                r[key] = None
    rows.sort(key=lambda r: (r.get("date") or "", r.get("label") or ""),
              reverse=True)
    return rows


def write_dives(rows: list) -> None:
    DIVES.parent.mkdir(parents=True, exist_ok=True)
    with DIVES.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=DIVE_FIELDS, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k, ""))
                        for k in DIVE_FIELDS})


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def known_hashes() -> dict:
    """sha256 -> filename, for everything already filed."""
    out = {}
    for side in PHOTOS.glob("*.json"):
        try:
            meta = json.loads(side.read_text(encoding="utf-8"))
        except Exception:
            continue
        if meta.get("sha256"):
            out[meta["sha256"]] = side.stem
            continue
        for ext in (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"):
            twin = side.with_suffix(ext)
            if twin.is_file():
                out[digest(twin.read_bytes())] = side.stem
                break
    return out


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

        if path == "/api/dives":
            return self.send_json({"dives": dive_rows()})

        if path == "/api/filed":
            return self.send_json({"photos": filed_photos()})

        if path.startswith("/filed/"):
            name = safe_name(path[len("/filed/"):])
            target = PHOTOS / name
            if not target.is_file():
                return self.send_json({"error": "not found"}, 404)
            return self.send_bytes(target.read_bytes(), "image/jpeg")

        if path == "/api/worms":
            name = (urllib.parse.parse_qs(parsed.query).get("name") or [""])[0]
            return self.send_json(worms_lookup(name.strip()))

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
        if parsed.path == "/api/species":
            return self.add_species(body)
        if parsed.path == "/api/dives":
            return self.save_dive(body)
        if parsed.path == "/api/dives/delete":
            return self.remove_dive(body)
        if parsed.path == "/api/photo/update":
            return self.update_photo(body)
        if parsed.path == "/api/photo/delete":
            return self.delete_photo(body)
        if parsed.path == "/api/build":
            return self.send_json({"build": rebuild()})
        if parsed.path == "/api/publish":
            return self.publish(body)
        return self.send_json({"error": "not found"}, 404)

    def add_species(self, body):
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return self.send_json({"error": "bad request"}, 400)

        sci = " ".join((payload.get("scientific_name") or "").split())
        if len(sci.split()) < 2:
            return self.send_json(
                {"error": "give a full scientific name, e.g. Oplegnathus punctatus"}, 400)

        status = (payload.get("status") or "indigenous").strip().lower()
        if status not in STATUSES:
            return self.send_json({"error": f"unknown status '{status}'"}, 400)

        existing = {r["sci"].lower() for r in species_rows()}
        if sci.lower() in existing:
            return self.send_json({"error": f"{sci} is already in the checklist"}, 400)

        row = {
            "scientific_name": sci,
            "aphia_id": (payload.get("aphia_id") or "").strip(),
            "family": (payload.get("family") or "").strip(),
            "common_name": (payload.get("common_name") or "").strip(),
            "hawaiian_name": (payload.get("hawaiian_name") or "").strip(),
            "status": status,
            "notes": (payload.get("notes") or "").strip(),
        }

        with DATA.open(newline="", encoding="utf-8") as fh:
            fields = csv.DictReader(fh).fieldnames or list(row)
        with DATA.open("a", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=fields).writerow(
                {k: row.get(k, "") for k in fields})

        print(f"  added {sci} to the checklist ({status})")
        return self.send_json({"ok": True, "species": row, "build": rebuild()})

    def save_dive(self, body):
        """Create or update one dive. Editing a pin moves every fish on it."""
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return self.send_json({"error": "bad request"}, 400)

        label = (payload.get("label") or "").strip()
        date = (payload.get("date") or "").strip()
        site = (payload.get("site") or "").strip()
        if not (label or site or date):
            return self.send_json({"error": "give the dive a name or a date"}, 400)

        lat, lng = payload.get("lat"), payload.get("lng")
        if lat is not None and lng is not None:
            try:
                lat, lng = float(lat), float(lng)
            except (TypeError, ValueError):
                return self.send_json({"error": "those coordinates don't parse"}, 400)
            if not sane(lat, lng):
                return self.send_json({"error": "coordinates out of range"}, 400)
        else:
            lat = lng = None

        rows = dive_rows()
        ident = (payload.get("id") or "").strip()
        if ident:
            hit = next((r for r in rows if r["id"] == ident), None)
            if not hit:
                return self.send_json({"error": "that dive is gone"}, 400)
        else:
            base = slugify(" ".join(filter(None, [date, label or site]))) or "dive"
            ident, n = base, 2
            taken = {r["id"] for r in rows}
            while ident in taken:
                ident = f"{base}-{n:02d}"
                n += 1
            hit = {"id": ident}
            rows.append(hit)

        hit.update({"label": label, "date": date, "site": site,
                    "lat": lat, "lng": lng,
                    "notes": (payload.get("notes") or "").strip()})
        write_dives(rows)
        print(f"  saved dive {ident}")
        return self.send_json({"ok": True, "id": ident, "dives": dive_rows(),
                               "build": rebuild()})

    def remove_dive(self, body):
        try:
            ident = json.loads(body.decode("utf-8")).get("id", "")
        except Exception:
            return self.send_json({"error": "bad request"}, 400)
        rows = dive_rows()
        keep = [r for r in rows if r["id"] != ident]
        if len(keep) == len(rows):
            return self.send_json({"error": "no such dive"}, 400)

        # Photos keep their own coordinates rather than silently losing them.
        orphaned = 0
        for side in PHOTOS.glob("*.json"):
            try:
                meta = json.loads(side.read_text(encoding="utf-8"))
            except Exception:
                continue
            if meta.get("dive") != ident:
                continue
            gone = next(r for r in rows if r["id"] == ident)
            if meta.get("lat") is None and gone.get("lat") is not None:
                meta["lat"], meta["lng"] = gone["lat"], gone["lng"]
            meta.pop("dive", None)
            meta.pop("own_position", None)
            side.write_text(json.dumps(meta, indent=1, ensure_ascii=False),
                            encoding="utf-8")
            orphaned += 1

        write_dives(keep)
        print(f"  deleted dive {ident}; {orphaned} photo(s) kept their position")
        return self.send_json({"ok": True, "detached": orphaned,
                               "dives": dive_rows(), "build": rebuild()})

    def update_photo(self, body):
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return self.send_json({"error": "bad request"}, 400)

        photo = PHOTOS / safe_name(payload.get("file", ""))
        side = photo.with_suffix(".json")
        if not photo.is_file() or not side.is_file():
            return self.send_json({"error": "that photo is no longer here"}, 400)

        sci = " ".join((payload.get("scientific_name") or "").split())
        known = {r["sci"] for r in species_rows()}
        if sci not in known:
            return self.send_json(
                {"error": f"{sci or 'that name'} isn't in the checklist"}, 400)

        lat, lng = payload.get("lat"), payload.get("lng")
        if lat is not None and lng is not None:
            try:
                lat, lng = float(lat), float(lng)
            except (TypeError, ValueError):
                return self.send_json({"error": "those coordinates don't parse"}, 400)
            if not sane(lat, lng):
                return self.send_json({"error": "coordinates out of range"}, 400)
        else:
            lat = lng = None

        record = {
            "scientific_name": sci,
            "date": (payload.get("date") or "").strip(),
            "site": (payload.get("site") or "").strip(),
            "lat": lat,
            "lng": lng,
            "nomap": bool(payload.get("nomap")),
        }
        dive = (payload.get("dive") or "").strip()
        if dive:
            record["dive"] = dive
            record["own_position"] = bool(payload.get("own_position"))
        sex = (payload.get("sex") or "").strip().lower()
        stage = (payload.get("stage") or "").strip().lower()
        if sex in SEX_LABEL:
            record["sex"] = sex
        if stage in STAGE_LABEL:
            record["stage"] = stage
        try:
            existing = json.loads(side.read_text(encoding="utf-8"))
            if existing.get("sha256"):
                record["sha256"] = existing["sha256"]
        except Exception:
            pass
        if payload.get("note"):
            record["note"] = str(payload["note"]).strip()

        # Keep the filename honest if the species changed.
        stem = slugify(sci).replace("-", "_")
        if not photo.stem.startswith(stem):
            fresh = unique_path(PHOTOS, stem, photo.suffix)
            photo.rename(fresh)
            side.unlink(missing_ok=True)
            photo, side = fresh, fresh.with_suffix(".json")

        side.write_text(json.dumps(record, indent=1, ensure_ascii=False),
                        encoding="utf-8")
        print(f"  updated {photo.name}  ->  {sci}")
        return self.send_json({"ok": True, "file": photo.name,
                               "build": rebuild()})

    def delete_photo(self, body):
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return self.send_json({"error": "bad request"}, 400)
        photo = PHOTOS / safe_name(payload.get("file", ""))
        if not photo.is_file():
            return self.send_json({"error": "that photo is no longer here"}, 400)
        photo.unlink()
        photo.with_suffix(".json").unlink(missing_ok=True)
        print(f"  deleted {photo.name}")
        return self.send_json({"ok": True, "build": rebuild()})

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

        sha = digest(body)
        clash = known_hashes().get(sha)

        lat, lng = gps_from_pillow(staged)
        return self.send_json({
            "sha256": sha,
            "duplicate": clash,
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
        dive = (payload.get("dive") or "").strip()
        if dive:
            sidecar["dive"] = dive
            sidecar["own_position"] = bool(payload.get("own_position"))
        sex = (payload.get("sex") or "").strip().lower()
        stage = (payload.get("stage") or "").strip().lower()
        if sex in SEX_LABEL:
            sidecar["sex"] = sex
        if stage in STAGE_LABEL:
            sidecar["stage"] = stage
        if payload.get("sha256"):
            sidecar["sha256"] = str(payload["sha256"])
        if payload.get("note"):
            sidecar["note"] = str(payload["note"]).strip()
        dest.with_suffix(".json").write_text(
            json.dumps(sidecar, indent=1, ensure_ascii=False), encoding="utf-8")

        preview = STAGING / (staged.stem + "-preview.jpg")
        preview.unlink(missing_ok=True)

        print(f"  filed {dest.name}  ->  {sci}"
              + (f"  @ {lat:.5f}, {lng:.5f}" if lat is not None else "  (no position)"))
        if payload.get("defer_build"):
            return self.send_json({"ok": True, "file": dest.name})
        return self.send_json({"ok": True, "file": dest.name, "build": rebuild()})


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
.map{height:340px;min-height:180px;border:1px solid var(--line);border-radius:2px;
 resize:vertical;overflow:hidden}
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
.dive{border:1px solid var(--line);border-radius:3px;padding:1rem 1.1rem;margin-bottom:1.2rem}
.dive h2{margin:0;font-size:1rem;font-weight:600}
.divegrid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.2fr);gap:1.2rem;margin-top:.8rem}
.divegrid .map{height:400px}
.newsp{border:1px solid var(--line);border-radius:3px;padding:.8rem 1.1rem;margin:1.2rem 0}
.newsp summary{cursor:pointer;font-size:.92rem;color:var(--gold)}
.newgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:1.2rem;margin-top:.9rem}
.newgrid .btn{margin-top:.8rem}
.pick{position:relative}
.opts{display:none;position:absolute;z-index:900;left:0;right:0;top:100%;
 max-height:260px;overflow:auto;background:var(--panel);border:1px solid var(--line);
 border-top:0;border-radius:0 0 3px 3px;box-shadow:0 10px 30px rgba(0,0,0,.45)}
.opt{padding:.4rem .6rem;cursor:pointer;font-size:.88rem;display:flex;
 align-items:baseline;gap:.5rem}
.opt:hover,.opt.mark{background:#123c4d}
.opt b{font-weight:500}
.opt span{color:var(--muted);font-size:.8rem}
.opt em{margin-left:auto;color:var(--gold);font-style:normal;font-size:.72rem}
.two{display:grid;grid-template-columns:1fr 1fr;gap:.7rem}
.two label{margin-top:.7rem}
.dupe{color:#ffb454}
.qbar{display:flex;justify-content:space-between;align-items:center;gap:1rem;
 flex-wrap:wrap;margin:1.2rem 0 .4rem;padding:.7rem .9rem;border:1px solid var(--line);
 border-radius:3px;background:rgba(255,255,255,.02)}
#qcount{font-size:.9rem;color:var(--muted)}
.qrow{display:grid;grid-template-columns:120px minmax(0,1fr);gap:1rem;
 padding:.8rem 0;border-bottom:1px solid var(--line);align-items:start}
.qthumb{width:100%;border-radius:2px;display:block}
.qmain{min-width:0}
.qmeta{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;margin-top:.5rem}
.qmeta select{width:auto;min-width:9rem}
.qname{margin:.45rem 0 0;font-size:.78rem;color:var(--muted)}
.small{font-size:.82rem;padding:.35rem .7rem}
.qrow.done{opacity:.45}
.details{margin-top:.9rem;padding-top:.8rem;border-top:1px solid var(--line)}
.details .map{height:300px;margin-top:.6rem}
@media (max-width:760px){.qrow{grid-template-columns:80px minmax(0,1fr)}}
.libhead select{font:inherit;font-size:.9rem;color:var(--fg);background:var(--panel);
 border:1px solid var(--line);border-radius:2px;padding:.4rem .6rem;max-width:22rem}
.library{margin-top:2.5rem;border-top:1px solid var(--line);padding-top:1.4rem}
.libhead{display:flex;justify-content:space-between;align-items:center;gap:1rem}
.library h2{margin:0;font-size:1.05rem;font-weight:600}
.danger{background:transparent;border:1px solid #7d3323;color:#ff9c7d}
.danger:hover{background:#7d3323;color:var(--fg)}
.libempty{color:var(--muted);font-size:.9rem;padding:1.4rem 0}
select:disabled{opacity:.5}
@media (max-width:800px){.divegrid,.newgrid{grid-template-columns:1fr}}
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
  <section class="dive">
    <div class="libhead">
      <h2>Dive</h2>
      <select id="dive-pick"></select>
    </div>
    <p class="hint">Saved on disk, so you can come back tomorrow and file more
    fish onto the same pin. Editing the pin moves every photo on that dive.</p>
    <div class="divegrid">
      <div>
        <label>Name</label><input type="text" id="dive-label" placeholder="North forereef">
        <label>Date</label><input type="date" id="dive-date">
        <label>Site</label><input type="text" id="dive-site" placeholder="Kure Atoll">
        <label>Notes</label><textarea id="dive-notes" rows="2"></textarea>
        <p class="coords" id="dive-coords">No dive pin set</p>
        <div class="row">
          <button class="btn" id="dive-save">Save dive</button>
          <button class="btn ghost" id="dive-new">New dive</button>
          <button class="danger" id="dive-del">Delete</button>
          <span class="msg" id="dive-msg"></span>
        </div>
      </div>
      <div><div class="map" id="dive-map"></div></div>
    </div>
  </section>

  <div id="drop">Drop a whole dive's photos here, or click to choose files
    <input type="file" id="file" multiple accept="image/*"></div>

  <details class="newsp">
    <summary>Fish not on the list? Add it to the checklist</summary>
    <div class="newgrid">
      <div>
        <label>Scientific name</label>
        <input type="text" id="ns-sci" placeholder="Oplegnathus punctatus">
        <button type="button" class="btn ghost" id="ns-look">Look up in WoRMS</button>
        <p class="hint" id="ns-msg"></p>
      </div>
      <div>
        <label>Common name</label><input type="text" id="ns-common">
        <label>Hawaiian name</label><input type="text" id="ns-haw">
      </div>
      <div>
        <label>Family</label><input type="text" id="ns-family">
        <label>Origin</label>
        <select id="ns-status">
          <option value="indigenous">Indigenous</option>
          <option value="endemic">Hawaiian endemic</option>
          <option value="endemic_nwhi">NWHI endemic</option>
          <option value="introduced">Introduced</option>
          <option value="waif">Waif</option>
          <option value="questionable">Questionable</option>
        </select>
        <button type="button" class="btn" id="ns-add">Add to checklist</button>
      </div>
    </div>
  </details>
  <div id="qbar" class="qbar" hidden>
    <span id="qcount"></span>
    <div class="row" style="margin:0">
      <button class="btn" id="saveall">Save all</button>
      <button class="btn ghost" id="clearsaved">Clear filed</button>
    </div>
  </div>
  <div id="queue"></div>

  <section class="library">
    <div class="libhead">
      <h2>Already filed</h2>
      <button type="button" class="btn ghost" id="lib-refresh">Refresh</button>
    </div>
    <p class="hint">Everything published so far. Edit anything, or remove it.</p>
    <div id="lib"></div>
  </section>
</main>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
var SPECIES = [], SHOT = new Set(), LAST_SEX = "", LAST_STAGE = "";
var SEX_OPTS = [["","Sex not recorded"],["female","Female"],
  ["male","Male"],["transitioning","Transitioning"]];
var STAGE_OPTS = [["","Stage not recorded"],["juvenile","Juvenile"],
  ["intermediate","Intermediate"],["adult","Adult"]];

function optionsHtml(pairs, chosen){
  return pairs.map(function(o){
    return '<option value="'+o[0]+'"'+(o[0]===chosen?" selected":"")+'>'+o[1]+'</option>';
  }).join("");
}

// A search box that actually narrows as you type, rather than a datalist.
function makePicker(input, box){
  var open = false, marked = -1, shown = [];

  function hide(){ box.innerHTML = ""; box.style.display = "none"; open = false; marked = -1; }

  function draw(){
    var q = input.value.trim().toLowerCase();
    shown = (q ? SPECIES.filter(function(sp){
      return (sp.sci + " " + sp.common + " " + sp.haw + " " + sp.family)
        .toLowerCase().indexOf(q) !== -1;
    }) : SPECIES).slice(0, 40);
    if (!shown.length){ hide(); return; }
    box.innerHTML = shown.map(function(sp, i){
      var extra = [sp.common, sp.haw].filter(Boolean).join(" / ");
      return '<div class="opt'+(i===marked?" mark":"")+'" data-i="'+i+'">'+
        '<b>'+sp.sci+'</b>'+(extra?'<span>'+extra+'</span>':'')+
        (SHOT.has(sp.sci)?'':'<em>new</em>')+'</div>';
    }).join("");
    box.style.display = "block"; open = true;
  }

  function pick(i){
    if (!shown[i]) return;
    input.value = shown[i].sci;
    hide();
    input.dispatchEvent(new Event("change"));
  }

  input.addEventListener("input", function(){ marked = -1; draw(); });
  input.addEventListener("focus", draw);
  input.addEventListener("blur", function(){ setTimeout(hide, 150); });
  input.addEventListener("keydown", function(ev){
    if (!open) return;
    if (ev.key === "ArrowDown"){ marked = Math.min(marked+1, shown.length-1); ev.preventDefault(); draw(); }
    else if (ev.key === "ArrowUp"){ marked = Math.max(marked-1, 0); ev.preventDefault(); draw(); }
    else if (ev.key === "Enter"){ if (marked >= 0){ ev.preventDefault(); pick(marked); } }
    else if (ev.key === "Escape"){ hide(); }
  });
  box.addEventListener("mousedown", function(ev){
    var opt = ev.target.closest(".opt");
    if (opt) { ev.preventDefault(); pick(parseInt(opt.dataset.i, 10)); }
  });
}
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

var DIVE = { lat:null, lng:null, marker:null, map:null, id:"", list:[] };

function setDivePin(lat, lng, zoom){
  DIVE.lat = lat; DIVE.lng = lng;
  if (DIVE.marker) DIVE.marker.setLatLng([lat,lng]);
  else DIVE.marker = L.marker([lat,lng], {draggable:true}).addTo(DIVE.map)
    .on("dragend", function(){
      var q = DIVE.marker.getLatLng(); setDivePin(q.lat, q.lng);
    });
  document.getElementById("dive-coords").textContent =
    lat.toFixed(6) + ", " + lng.toFixed(6);
}

function clearDivePin(){
  if (DIVE.marker){ DIVE.map.removeLayer(DIVE.marker); DIVE.marker = null; }
  DIVE.lat = DIVE.lng = null;
  document.getElementById("dive-coords").textContent = "No dive pin set";
}

function watchSize(map, node){
  if (window.ResizeObserver){
    new ResizeObserver(function(){ map.invalidateSize(); }).observe(node);
  }
}

function initDiveMap(){
  DIVE.map = L.map("dive-map", { scrollWheelZoom:true });
  watchSize(DIVE.map, document.getElementById("dive-map"));
  L.tileLayer("https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2024_3857/default/g/{z}/{y}/{x}.jpg",
    { maxZoom:19, maxNativeZoom:14,
      attribution:"Sentinel-2 cloudless by EOX (CC BY 4.0)" }).addTo(DIVE.map);
  DIVE.map.fitBounds(HAWAII);
  DIVE.map.on("click", function(e){ setDivePin(e.latlng.lat, e.latlng.lng); });
}

function diveName(d){
  return (d.label || d.site || d.date || d.id) +
    (d.date && d.label ? "  ·  " + d.date : "");
}

function paintDiveList(){
  var sel = document.getElementById("dive-pick");
  sel.innerHTML = '<option value="">— new dive —</option>' +
    DIVE.list.map(function(d){
      return '<option value="'+d.id+'"'+(d.id===DIVE.id?" selected":"")+'>'+
        diveName(d)+'</option>';
    }).join("");
  document.querySelectorAll("select.dive").forEach(function(s){
    var keep = s.value;
    s.innerHTML = '<option value="">No dive</option>' +
      DIVE.list.map(function(d){
        return '<option value="'+d.id+'">'+diveName(d)+'</option>';
      }).join("");
    s.value = keep;
  });
}

function showDive(id){
  DIVE.id = id || "";
  var d = DIVE.list.filter(function(x){ return x.id === DIVE.id; })[0];
  document.getElementById("dive-label").value = d ? (d.label||"") : "";
  document.getElementById("dive-date").value  = d ? (d.date||"")  : "";
  document.getElementById("dive-site").value  = d ? (d.site||"")  : "";
  document.getElementById("dive-notes").value = d ? (d.notes||""): "";
  clearDivePin();
  if (d && d.lat !== null && d.lat !== undefined){
    setDivePin(d.lat, d.lng);
    DIVE.map.setView([d.lat, d.lng], 12);
  }
  document.getElementById("dive-del").disabled = !DIVE.id;
  paintDiveList();
}

function loadDives(then){
  fetch("/api/dives").then(function(r){return r.json()}).then(function(res){
    DIVE.list = res.dives || [];
    paintDiveList();
    if (then) then();
  });
}

function saveDive(){
  var msg = document.getElementById("dive-msg");
  fetch("/api/dives", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({
      id: DIVE.id,
      label: document.getElementById("dive-label").value,
      date: document.getElementById("dive-date").value,
      site: document.getElementById("dive-site").value,
      notes: document.getElementById("dive-notes").value,
      lat: DIVE.lat, lng: DIVE.lng
    })}).then(function(r){return r.json()}).then(function(res){
      msg.className = "msg " + (res.error ? "bad" : "good");
      msg.textContent = res.error || "Dive saved.";
      if (res.error) return;
      DIVE.list = res.dives; DIVE.id = res.id;
      paintDiveList(); showDive(res.id); refreshStatus(); loadLibrary();
    });
}

function deleteDive(){
  if (!DIVE.id) return;
  if (!confirm("Delete this dive? Photos on it keep their coordinates.")) return;
  fetch("/api/dives/delete", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({id: DIVE.id})}).then(function(r){return r.json()})
    .then(function(res){
      var msg = document.getElementById("dive-msg");
      if (res.error){ msg.className="msg bad"; msg.textContent=res.error; return; }
      msg.className = "msg good";
      msg.textContent = res.detached + " photo(s) kept their position.";
      DIVE.list = res.dives; showDive(""); refreshStatus(); loadLibrary();
    });
}

function addSpecies(){
  var msg = document.getElementById("ns-msg");
  var body = {
    scientific_name: document.getElementById("ns-sci").value,
    common_name: document.getElementById("ns-common").value,
    hawaiian_name: document.getElementById("ns-haw").value,
    family: document.getElementById("ns-family").value,
    status: document.getElementById("ns-status").value,
    aphia_id: document.getElementById("ns-sci").dataset.aphia || ""
  };
  fetch("/api/species", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify(body)}).then(function(r){return r.json()}).then(function(res){
    if (res.error){ msg.style.color="#ff9c7d"; msg.textContent=res.error; return; }
    msg.style.color="#7ee0c0";
    msg.textContent = res.species.scientific_name + " added. It's in the dropdown now.";
    SPECIES.push({sci:res.species.scientific_name, common:res.species.common_name,
      haw:res.species.hawaiian_name, family:res.species.family,
      status:res.species.status});
    SPECIES.sort(function(a,b){ return a.sci < b.sci ? -1 : 1; });
    ["ns-sci","ns-common","ns-haw","ns-family"].forEach(function(id){
      document.getElementById(id).value = "";
    });
    refreshStatus();
  });
}

function lookupWorms(){
  var field = document.getElementById("ns-sci");
  var msg = document.getElementById("ns-msg");
  msg.style.color = ""; msg.textContent = "Asking WoRMS…";
  fetch("/api/worms?name=" + encodeURIComponent(field.value.trim()))
    .then(function(r){return r.json()}).then(function(d){
      if (d.error){ msg.style.color="#ff9c7d"; msg.textContent=d.error; return; }
      if (d.family) document.getElementById("ns-family").value = d.family;
      field.dataset.aphia = d.aphia_id || "";
      var note = "AphiaID " + d.aphia_id + (d.family ? " · " + d.family : "");
      if (d.accepted && d.accepted.toLowerCase() !== field.value.trim().toLowerCase()) {
        msg.style.color = "#f5b840";
        note += " · WoRMS accepts this as " + d.accepted;
      } else { msg.style.color = "#7ee0c0"; }
      msg.textContent = note;
    });
}

function renderFiled(d){
  var wrap = document.createElement("div");
  wrap.className = "item";
  var mapId = "lm-" + d.file.replace(/[^a-z0-9]/gi,"");
  wrap.innerHTML =
    '<div><img class="shot" src="'+d.url+'" alt=""><p class="hint">'+d.file+
      (d.sidecar ? "" : ' <span style="color:#ff9c7d">no sidecar</span>')+'</p></div>'+
    '<div>'+
      '<label>Species</label>'+
      '<div class="pick"><input type="text" class="sci" autocomplete="off"></div>'+
      '<div class="two">'+
        '<div><label>Sex</label><select class="sex">'+optionsHtml(SEX_OPTS,d.sex||"")+'</select></div>'+
        '<div><label>Stage</label><select class="stage">'+optionsHtml(STAGE_OPTS,d.stage||"")+'</select></div>'+
      '</div>'+
      '<label>Dive</label><select class="dive"></select>'+
      '<div class="check"><input type="checkbox" class="ownpos" id="'+mapId+'-op"'+
        (d.own_position?" checked":"")+'>'+
        '<label for="'+mapId+'-op" style="margin:0">This fish had its own position</label></div>'+
      '<label>Date</label><input type="date" class="date">'+
      '<label>Site</label><input type="text" class="site">'+
      '<label>Note</label><textarea class="note" rows="2"></textarea>'+
      '<div class="check"><input type="checkbox" class="nomap" id="'+mapId+'-nm">'+
        '<label for="'+mapId+'-nm" style="margin:0">Keep this position off the public map</label></div>'+
    '</div>'+
    '<div>'+
      '<label>Position</label><div class="map" id="'+mapId+'"></div>'+
      '<p class="coords">No position set</p>'+
      '<div class="row"><button class="save">Save changes</button>'+
        '<button class="ghost clear">Clear pin</button>'+
        '<button class="danger del">Delete</button>'+
        '<span class="msg"></span></div>'+
    '</div>';

  document.getElementById("lib").appendChild(wrap);

  var sci = wrap.querySelector(".sci");
  var sexSel = wrap.querySelector(".sex");
  var stageSel = wrap.querySelector(".stage");
  var diveSel = wrap.querySelector(".dive");
  var ownPos = wrap.querySelector(".ownpos");
  paintDiveList();
  diveSel.value = d.dive || "";
  var picker = document.createElement("div");
  picker.className = "opts";
  sci.parentNode.appendChild(picker);
  makePicker(sci, picker);
  sci.value = d.scientific_name || "";
  wrap.querySelector(".date").value = d.date || "";
  wrap.querySelector(".site").value = d.site || "";
  wrap.querySelector(".note").value = d.note || "";
  wrap.querySelector(".nomap").checked = !!d.nomap;

  var map = L.map(mapId, { scrollWheelZoom:true });
  watchSize(map, document.getElementById(mapId));
  L.tileLayer("https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2024_3857/default/g/{z}/{y}/{x}.jpg",
    { maxZoom:19, maxNativeZoom:14,
      attribution:"Sentinel-2 cloudless by EOX (CC BY 4.0)" }).addTo(map);
  var marker = null, coords = wrap.querySelector(".coords");
  function show(lat,lng){
    coords.textContent = lat.toFixed(6)+", "+lng.toFixed(6);
    coords.dataset.lat = lat; coords.dataset.lng = lng;
  }
  function place(lat,lng,zoom){
    if (marker) marker.setLatLng([lat,lng]);
    else marker = L.marker([lat,lng],{draggable:true}).addTo(map)
      .on("dragend", function(){ var q=marker.getLatLng(); show(q.lat,q.lng); });
    show(lat,lng);
    if (zoom) map.setView([lat,lng], zoom);
  }
  if (d.lat !== null && d.lat !== undefined) place(d.lat, d.lng, 11);
  else map.fitBounds(HAWAII);
  map.on("click", function(e){ place(e.latlng.lat, e.latlng.lng); });
  wrap.querySelector(".clear").addEventListener("click", function(){
    if (marker){ map.removeLayer(marker); marker = null; }
    coords.textContent = "No position set";
    delete coords.dataset.lat; delete coords.dataset.lng;
  });

  var msg = wrap.querySelector(".msg");
  wrap.querySelector(".save").addEventListener("click", function(){
    var btn = this; btn.disabled = true;
    fetch("/api/photo/update", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({
        file: d.file,
        scientific_name: sci.value.split(" — ")[0].trim(),
        sex: sexSel.value,
        stage: stageSel.value,
        dive: diveSel.value,
        own_position: ownPos.checked,
        date: wrap.querySelector(".date").value,
        site: wrap.querySelector(".site").value,
        note: wrap.querySelector(".note").value,
        nomap: wrap.querySelector(".nomap").checked,
        lat: coords.dataset.lat ? parseFloat(coords.dataset.lat) : null,
        lng: coords.dataset.lng ? parseFloat(coords.dataset.lng) : null
      })}).then(function(r){return r.json()}).then(function(res){
        btn.disabled = false;
        msg.className = "msg " + (res.error ? "bad" : "good");
        msg.textContent = res.error || "Saved. Site rebuilt.";
        if (!res.error){ d.file = res.file; refreshStatus(); }
      });
  });

  wrap.querySelector(".del").addEventListener("click", function(){
    if (!confirm("Delete " + d.file + " and its metadata? This can't be undone here.")) return;
    fetch("/api/photo/delete", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({file: d.file})})
      .then(function(r){return r.json()}).then(function(res){
        if (res.error){ msg.className="msg bad"; msg.textContent=res.error; return; }
        wrap.remove(); refreshStatus();
      });
  });
}

function loadLibrary(){
  var box = document.getElementById("lib");
  box.innerHTML = "";
  fetch("/api/filed").then(function(r){return r.json()}).then(function(d){
    if (!d.photos.length){
      box.innerHTML = '<p class="libempty">Nothing filed yet.</p>';
      return;
    }
    d.photos.forEach(renderFiled);
  });
}

document.addEventListener("DOMContentLoaded", function(){
  refreshStatus();
  initDiveMap();
  document.getElementById("lib-refresh").addEventListener("click", loadLibrary);
  document.getElementById("saveall").addEventListener("click", saveAll);
  document.getElementById("clearsaved").addEventListener("click", clearSaved);
  setTimeout(loadLibrary, 400);
  loadDives();
  document.getElementById("dive-pick").addEventListener("change", function(){
    showDive(this.value);
  });
  document.getElementById("dive-save").addEventListener("click", saveDive);
  document.getElementById("dive-del").addEventListener("click", deleteDive);
  document.getElementById("dive-new").addEventListener("click", function(){
    showDive("");
  });
  document.getElementById("ns-look").addEventListener("click", lookupWorms);
  document.getElementById("ns-add").addEventListener("click", addSpecies);
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
  wrap.className = "qrow";
  var mapId = "m-" + d.id.replace(/[^a-z0-9]/gi,"");

  wrap.innerHTML =
    '<img class="qthumb" src="'+d.preview+'" alt="">'+
    '<div class="qmain">'+
      '<div class="pick"><input type="text" class="sci" autocomplete="off" '+
        'placeholder="Species — type to search"><div class="opts"></div></div>'+
      '<div class="qmeta">'+
        '<select class="sex">'+optionsHtml(SEX_OPTS,"")+'</select>'+
        '<select class="stage">'+optionsHtml(STAGE_OPTS,"")+'</select>'+
        '<select class="dive"></select>'+
        '<button type="button" class="ghost small more">Details</button>'+
        '<button type="button" class="small save">Save</button>'+
        '<span class="msg"></span>'+
      '</div>'+
      '<p class="qname">'+d.original+
        (d.duplicate ? ' <span class="dupe">already filed as '+d.duplicate+'</span>' : '')+
      '</p>'+
      '<div class="details" hidden>'+
        '<div class="two">'+
          '<div><label>Date</label><input type="date" class="date" value="'+(d.date||"")+'"></div>'+
          '<div><label>Site</label><input type="text" class="site"></div>'+
        '</div>'+
        '<label>Note</label><textarea class="note" rows="2"></textarea>'+
        '<div class="check"><input type="checkbox" class="ownpos" id="'+mapId+'-op">'+
          '<label for="'+mapId+'-op" style="margin:0">This fish had its own position</label></div>'+
        '<div class="check"><input type="checkbox" class="nomap" id="'+mapId+'-nm">'+
          '<label for="'+mapId+'-nm" style="margin:0">Keep this position off the public map</label></div>'+
        '<div class="map" id="'+mapId+'"></div>'+
        '<p class="coords">Using the dive position</p>'+
        '<button type="button" class="ghost small clear">Clear pin</button>'+
      '</div>'+
    '</div>';

  document.getElementById("queue").appendChild(wrap);
  queueBar();

  var sci = wrap.querySelector(".sci");
  var sexSel = wrap.querySelector(".sex");
  var stageSel = wrap.querySelector(".stage");
  var diveSel = wrap.querySelector(".dive");
  var ownPos = wrap.querySelector(".ownpos");
  var coords = wrap.querySelector(".coords");
  var msg = wrap.querySelector(".msg");
  var details = wrap.querySelector(".details");

  paintDiveList();
  if (DIVE.id) diveSel.value = DIVE.id;
  makePicker(sci, wrap.querySelector(".opts"));
  sci.value = matchGuess(d.guess);
  if (LAST_SEX) sexSel.value = LAST_SEX;
  if (LAST_STAGE) stageSel.value = LAST_STAGE;

  function chosenDive(){
    return DIVE.list.filter(function(x){ return x.id === diveSel.value; })[0];
  }
  function applyDive(){
    var pd = chosenDive();
    if (!pd) return;
    if (pd.date && !wrap.querySelector(".date").value)
      wrap.querySelector(".date").value = pd.date;
    if (pd.site && !wrap.querySelector(".site").value)
      wrap.querySelector(".site").value = pd.site;
  }
  applyDive();
  diveSel.addEventListener("change", applyDive);

  // The map is expensive, so it is only built if you actually open Details.
  var map = null, marker = null;
  function show(lat,lng){
    coords.textContent = lat.toFixed(6)+", "+lng.toFixed(6);
    coords.dataset.lat = lat; coords.dataset.lng = lng;
  }
  function place(lat,lng,zoom){
    if (marker) marker.setLatLng([lat,lng]);
    else marker = L.marker([lat,lng],{draggable:true}).addTo(map)
      .on("dragend", function(){ var q=marker.getLatLng(); show(q.lat,q.lng); });
    show(lat,lng);
    if (zoom) map.setView([lat,lng], zoom);
  }
  function buildMap(){
    if (map) { map.invalidateSize(); return; }
    map = L.map(mapId, { scrollWheelZoom:true });
    L.tileLayer("https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2024_3857/default/g/{z}/{y}/{x}.jpg",
      { maxZoom:19, maxNativeZoom:14,
        attribution:"Sentinel-2 cloudless by EOX (CC BY 4.0)" }).addTo(map);
    watchSize(map, document.getElementById(mapId));
    var pd = chosenDive();
    if (d.lat !== null && d.lng !== null) place(d.lat, d.lng, 13);
    else if (pd && pd.lat !== null && pd.lat !== undefined) place(pd.lat, pd.lng, 12);
    else map.fitBounds(HAWAII);
    map.on("click", function(e){ place(e.latlng.lat, e.latlng.lng); });
  }

  wrap.querySelector(".more").addEventListener("click", function(){
    details.hidden = !details.hidden;
    this.textContent = details.hidden ? "Details" : "Hide";
    if (!details.hidden) setTimeout(buildMap, 30);
  });
  wrap.querySelector(".clear").addEventListener("click", function(){
    if (marker && map){ map.removeLayer(marker); marker = null; }
    coords.textContent = "Using the dive position";
    delete coords.dataset.lat; delete coords.dataset.lng;
  });

  wrap.payload = function(){
    return {
      id: d.id,
      scientific_name: sci.value.split(" — ")[0].trim(),
      sex: sexSel.value,
      stage: stageSel.value,
      dive: diveSel.value,
      own_position: ownPos.checked,
      sha256: d.sha256 || "",
      date: wrap.querySelector(".date").value,
      site: wrap.querySelector(".site").value,
      note: wrap.querySelector(".note").value,
      nomap: wrap.querySelector(".nomap").checked,
      lat: coords.dataset.lat ? parseFloat(coords.dataset.lat) : null,
      lng: coords.dataset.lng ? parseFloat(coords.dataset.lng) : null
    };
  };

  wrap.commit = function(deferBuild){
    var body = wrap.payload();
    if (!body.scientific_name){
      msg.className = "msg bad"; msg.textContent = "needs a species";
      return Promise.resolve(false);
    }
    body.defer_build = !!deferBuild;
    msg.className = "msg"; msg.textContent = "saving…";
    return fetch("/api/commit", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(body)}).then(function(r){return r.json()}).then(function(res){
        if (res.error){ msg.className="msg bad"; msg.textContent=res.error; return false; }
        msg.className = "msg good"; msg.textContent = "filed";
        SHOT.add(body.scientific_name);
        LAST_SEX = body.sex; LAST_STAGE = body.stage;
        wrap.classList.add("done");
        wrap.saved = true;
        queueBar();
        return true;
      });
  };

  wrap.querySelector(".save").addEventListener("click", function(){
    var btn = this; btn.disabled = true;
    wrap.commit(false).then(function(ok){
      btn.disabled = false;
      if (ok){ refreshStatus(); loadLibrary(); }
    });
  });
}

function queueBar(){
  var rows = Array.prototype.slice.call(document.querySelectorAll(".qrow"));
  var left = rows.filter(function(r){ return !r.saved; });
  var bar = document.getElementById("qbar");
  bar.hidden = rows.length === 0;
  document.getElementById("qcount").textContent =
    left.length ? left.length + " waiting to be filed" : "All filed.";
  document.getElementById("saveall").disabled = left.length === 0;
}

function saveAll(){
  var btn = document.getElementById("saveall");
  btn.disabled = true; btn.textContent = "Saving…";
  var rows = Array.prototype.slice.call(document.querySelectorAll(".qrow"))
    .filter(function(r){ return !r.saved; });

  // Save one at a time with the rebuild deferred, then rebuild once at the end.
  var i = 0;
  function next(){
    if (i >= rows.length){
      return fetch("/api/build", {method:"POST"}).then(function(){
        btn.textContent = "Save all";
        queueBar(); refreshStatus(); loadLibrary();
      });
    }
    var row = rows[i++];
    return row.commit(true).then(next);
  }
  next();
}

function clearSaved(){
  document.querySelectorAll(".qrow.done").forEach(function(r){ r.remove(); });
  queueBar();
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
