#!/usr/bin/env python3
"""
Build the Hawaiian reef fish catalog.

Reads photos from photos/, pulls the species name and GPS position out of
each photo, joins against data/species.csv, and writes a static site to
site/.

How a photo gets matched to a species, in order:
  1. An IPTC/XMP keyword that is a scientific name in the checklist
     (e.g. a Lightroom keyword "Chaetodon miliaris"). Needs exiftool.
  2. An IPTC/XMP keyword that matches a common or Hawaiian name.
  3. The filename, e.g. Chaetodon_miliaris_004.jpg.

Optional keywords:
  site:Pearl and Hermes    label for this frame
  nomap                    keep this frame's coordinates off the map

scripts/ingest.py writes a <photo>.json sidecar beside each image with the
species, date, site and the exact position you pinned. Sidecar values win
over anything in the file's own metadata.

Coordinates are rounded before they are written to the site. See
GPS_PRECISION below.

Usage:  python3 scripts/build.py
"""

from __future__ import annotations

import csv
import json
import html
import os
import re
import shutil
import urllib.parse
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    from PIL import Image, ExifTags
except ImportError:
    sys.exit("Pillow is required:  pip install Pillow")

ROOT = Path(__file__).resolve().parent.parent
PHOTOS = ROOT / "photos"
DATA = ROOT / "data" / "species.csv"
ASSETS = ROOT / "assets"
OUT = ROOT / "site"
IMG = OUT / "img"

PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
SIDECAR_EXT = ".json"
FULL_PX = 1600
CARD_PX = 700
PIN_PX = 260

OWNER = os.environ.get("DEX_OWNER", "Carlos Tramonte")
TITLE = os.environ.get("DEX_TITLE", "Hawaiian Reef Fish")

# Where the site lives, used to build absolute URLs for link previews.
SITE_URL = os.environ.get(
    "DEX_SITE_URL", "https://carlostramonte26.github.io/hawaii-fish-dex").rstrip("/")

# The fish that represents the site when a link is shared.
COVER_SPECIES = os.environ.get("DEX_COVER", "Apolemichthys arcuatus")

BLURB = ("A photographic catalog of the reef fishes of the Hawaiian "
         "Archipelago, with the endemics marked and every frame placed on "
         "the map.")

OG_W, OG_H = 1200, 630

# Coordinates are published exactly as pinned. Set DEX_GPS_PRECISION to blur
# them instead: 2 -> about 1.1 km, 3 -> about 110 m, 4 -> about 11 m.
_precision = os.environ.get("DEX_GPS_PRECISION", "").strip()
GPS_PRECISION = int(_precision) if _precision else None

# Longitude west of this is the Northwestern Hawaiian Islands.
# Nihoa sits at 161.93 W; Niʻihau, the westernmost main island, at 160.1 W.
NWHI_CUTOFF = -161.0

STATUS_LABEL = {
    "endemic": "Hawaiian endemic",
    "endemic_nwhi": "NWHI endemic",
    "indigenous": "Indigenous",
    "introduced": "Introduced",
    "not_in_hawaii": "Not in Hawaiʻi",
    "waif": "Waif",
    "questionable": "Questionable",
}
ENDEMIC_STATUSES = {"endemic", "endemic_nwhi"}

# Phases worth photographing separately. Labrids and scarids are protogynous,
# so initial/terminal is the right vocabulary there — a terminal-phase fish is
# usually a secondary male, and initial phase holds females and some males.
# male/female is for gonochoristic species that are simply dimorphic.
# Keoki Stender's site files species under common-name folders. These are the
# ones confirmed from his own index; anything else falls back to a site search.
KEOKI_FOLDER = {
    "Pomacanthidae": "angelfishes",
    "Serranidae": "groupers", "Epinephelidae": "groupers",
    "Anthiadidae": "groupers",
    "Priacanthidae": "bigeyes", "Kuhliidae": "bigeyes",
    "Holocentridae": "squirrelfishes",
    "Gobiidae": "gobies", "Eleotridae": "gobies", "Oxudercidae": "gobies",
    "Syngnathidae": "pipefishes", "Pegasidae": "pipefishes",
    "Blenniidae": "blennies", "Tripterygiidae": "blennies",
    "Callionymidae": "dragonets",
    "Chaetodontidae": "butterflyfishes",
    "Acanthuridae": "surgeonfishes",
    "Kyphosidae": "chubs",
    "Apogonidae": "cardinalfishes",
    "Pomacentridae": "damselfishes",
    "Mullidae": "goatfishes",
    "Cirrhitidae": "hawkfishes", "Cheilodactylidae": "hawkfishes",
    "Latridae": "hawkfishes",
    "Monacanthidae": "filefishes",
    "Muraenidae": "eels", "Congridae": "eels", "Ophichthidae": "eels",
    "Moridae": "eels",
    "Labridae": "wrasses",
    "Scaridae": "parrotfishes",
    "Tetraodontidae": "puffers",
    "Scorpaenidae": "scorpionfishes",
    "Bothidae": "flatfishes", "Soleidae": "flatfishes",
}
KEOKI = "https://www.marinelifephotography.com"

PHASE_LABEL = {
    "juvenile": "Juvenile",
    "subadult": "Subadult",
    "adult": "Adult",
    "initial": "Initial phase",
    "terminal": "Terminal phase",
    "male": "Male",
    "female": "Female",
}

# Set by main() so helpers can stay quiet when the tool drives the build.
SAY = [print]

PIN_COLOR = {
    "endemic": "#f5b840",
    "endemic_nwhi": "#7ee0c0",
    "indigenous": "#9fbfcb",
    "introduced": "#c1543a",
    "not_in_hawaii": "#86a4ad",
    "waif": "#86a4ad",
    "questionable": "#86a4ad",
}


# ---------------------------------------------------------------- data model

@dataclass
class Species:
    scientific_name: str
    family: str = ""
    common_name: str = ""
    hawaiian_name: str = ""
    status: str = "indigenous"
    phases: tuple = ()
    notes: str = ""
    aphia_id: str = ""
    photos: list = field(default_factory=list)

    @property
    def slug(self) -> str:
        return slugify(self.scientific_name)

    @property
    def seen(self) -> bool:
        return bool(self.photos)

    @property
    def display_name(self) -> str:
        return self.common_name or self.scientific_name

    @property
    def phases_seen(self) -> list:
        got = {p.phase for p in self.photos if p.phase}
        return [ph for ph in self.phases if ph in got]

    @property
    def phases_missing(self) -> list:
        return [ph for ph in self.phases if ph not in self.phases_seen]

    @property
    def complete(self) -> bool:
        """Nothing left to photograph for this species."""
        if not self.phases:
            return self.seen
        return self.seen and not self.phases_missing

    @property
    def regions(self) -> list:
        """Where my own photos of this species came from."""
        found = []
        for p in self.photos:
            if p.lng is None:
                continue
            label = "Northwestern Hawaiian Islands" if p.lng < NWHI_CUTOFF \
                else "Main Hawaiian Islands"
            if label not in found:
                found.append(label)
        return found


@dataclass
class Photo:
    src: Path
    card: str = ""
    full: str = ""
    pin: str = ""
    width: int = 0
    height: int = 0
    date: str = ""
    site: str = ""
    phase: str = ""
    note: str = ""
    lat: float | None = None
    lng: float | None = None


# ------------------------------------------------------------------- helpers

def slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text.strip().lower())
    return re.sub(r"[\s_]+", "-", text)


def norm(text: str) -> str:
    text = text.lower()
    for a, b in (("ʻ", ""), ("'", ""), ("`", ""), ("ā", "a"), ("ē", "e"),
                 ("ī", "i"), ("ō", "o"), ("ū", "u"), ("-", " "), ("_", " ")):
        text = text.replace(a, b)
    return re.sub(r"[^a-z0-9 ]", "", text).strip()


def e(text) -> str:
    return html.escape(str(text or ""), quote=True)


# ------------------------------------------------------------- read checklist

def load_species() -> dict:
    if not DATA.exists():
        sys.exit(f"Missing checklist: {DATA}")
    out = {}
    unknown = set()
    with DATA.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("scientific_name") or "").strip()
            if not name:
                continue
            status = (row.get("status") or "indigenous").strip().lower()
            if status not in STATUS_LABEL:
                unknown.add(status)
                status = "indigenous"
            out[name] = Species(
                scientific_name=name,
                family=(row.get("family") or "").strip(),
                common_name=(row.get("common_name") or "").strip(),
                hawaiian_name=(row.get("hawaiian_name") or "").strip(),
                status=status,
                phases=tuple(
                    ph for ph in
                    (x.strip().lower() for x in (row.get("phases") or "").split("|"))
                    if ph in PHASE_LABEL),
                notes=(row.get("notes") or "").strip(),
                aphia_id=(row.get("aphia_id") or "").strip(),
            )
    for bad in sorted(unknown):
        print(f"  unrecognised status '{bad}' treated as indigenous")
    return out


def build_index(species: dict) -> dict:
    idx = {}
    for sp in species.values():
        for name in (sp.scientific_name, sp.common_name, sp.hawaiian_name):
            if name:
                idx[norm(name)] = sp.scientific_name
    return idx


# ----------------------------------------------------------------------- GPS

def to_decimal(dms, ref) -> float | None:
    try:
        d, m, s = (float(x) for x in dms)
    except (TypeError, ValueError):
        return None
    value = d + m / 60 + s / 3600
    if str(ref).upper() in ("S", "W"):
        value = -value
    return value


def gps_from_pillow(path: Path) -> tuple:
    try:
        with Image.open(path) as im:
            gps = im.getexif().get_ifd(0x8825)
    except Exception:
        return (None, None)
    if not gps:
        return (None, None)
    lat = to_decimal(gps.get(2), gps.get(1))
    lng = to_decimal(gps.get(4), gps.get(3))
    return (lat, lng)


def gps_from_tags(tags: dict) -> tuple:
    pos = tags.get("GPSPosition")
    if isinstance(pos, str):
        parts = pos.replace(",", " ").split()
        if len(parts) == 2:
            try:
                return (float(parts[0]), float(parts[1]))
            except ValueError:
                pass
    lat, lng = tags.get("GPSLatitude"), tags.get("GPSLongitude")
    if lat is None or lng is None:
        return (None, None)
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return (None, None)
    if str(tags.get("GPSLatitudeRef", "")).upper().startswith("S") and lat > 0:
        lat = -lat
    if str(tags.get("GPSLongitudeRef", "")).upper().startswith("W") and lng > 0:
        lng = -lng
    return (lat, lng)


def sane(lat, lng) -> bool:
    return (lat is not None and lng is not None
            and -90 <= lat <= 90 and -180 <= lng <= 180
            and not (lat == 0 and lng == 0))


# -------------------------------------------------------------- photo reading

def have_exiftool() -> bool:
    return shutil.which("exiftool") is not None


def exiftool_dump(paths: list) -> dict:
    cmd = ["exiftool", "-j", "-n", "-charset", "utf8",
           "-Keywords", "-Subject", "-Title", "-Description",
           "-DateTimeOriginal", "-CreateDate",
           "-Sub-location", "-Location", "-City",
           "-GPSPosition", "-GPSLatitude", "-GPSLongitude",
           "-GPSLatitudeRef", "-GPSLongitudeRef"] + [str(p) for p in paths]
    try:
        raw = subprocess.run(cmd, capture_output=True, text=True,
                             check=True).stdout
        return {Path(rec["SourceFile"]).resolve().as_posix(): rec
                for rec in json.loads(raw)}
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as err:
        print(f"  exiftool failed ({err}); falling back to Pillow")
        return {}


def keywords_from(tags: dict) -> list:
    words = []
    for key in ("Keywords", "Subject"):
        val = tags.get(key)
        if isinstance(val, list):
            words += val
        elif isinstance(val, str):
            words += [w.strip() for w in val.split(",")]
    return [w for w in words if w]


def pillow_date(path: Path) -> str:
    try:
        with Image.open(path) as im:
            tagmap = {ExifTags.TAGS.get(k, k): v for k, v in im.getexif().items()}
            for key in ("DateTimeOriginal", "DateTime"):
                if tagmap.get(key):
                    return str(tagmap[key])[:10].replace(":", "-")
    except Exception:
        pass
    return ""


def read_sidecar(path: Path) -> dict:
    """A <photo>.json written by scripts/ingest.py. Wins over EXIF."""
    side = path.with_suffix(".json")
    if not side.exists():
        return {}
    try:
        data = json.loads(side.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as err:
        print(f"  could not read {side.name}: {err}")
        return {}


def match_filename(path: Path, idx: dict) -> str | None:
    words = norm(path.stem).split()
    for size in range(min(4, len(words)), 0, -1):
        for start in range(len(words) - size + 1):
            hit = idx.get(" ".join(words[start:start + size]))
            if hit:
                return hit
    return None


def make_og(src: Path, dest: Path) -> None:
    if fresh(src, dest):
        return
    """A 1200x630 centre crop, the shape every link preview expects."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        im = im.convert("RGB")
        target = OG_W / OG_H
        w, h = im.size
        if w / h > target:
            new_w = int(h * target)
            box = ((w - new_w) // 2, 0, (w - new_w) // 2 + new_w, h)
        else:
            new_h = int(w / target)
            box = (0, (h - new_h) // 2, w, (h - new_h) // 2 + new_h)
        im.crop(box).resize((OG_W, OG_H), Image.LANCZOS).save(
            dest, "JPEG", quality=84, optimize=True)


def fresh(src: Path, dest: Path) -> bool:
    """True if dest already exists and is at least as new as src."""
    try:
        return dest.exists() and dest.stat().st_mtime >= src.stat().st_mtime
    except OSError:
        return False


def resize(src: Path, dest: Path, box: int) -> tuple:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if fresh(src, dest):
        with Image.open(dest) as done:
            return done.size
    with Image.open(src) as im:
        im = im.convert("RGB")
        im.thumbnail((box, box), Image.LANCZOS)
        im.save(dest, "JPEG", quality=86, optimize=True, progressive=True)
        return im.size


def collect_photos(species: dict, idx: dict) -> tuple:
    files = sorted(p for p in PHOTOS.rglob("*")
                   if p.suffix.lower() in PHOTO_EXT and not p.name.startswith("."))
    if not files:
        SAY[0]("  no photos yet — building an empty catalog")
        return (0, 0)

    tags_by_path = exiftool_dump(files) if have_exiftool() else {}
    if not tags_by_path:
        SAY[0]("  exiftool not found — using filenames and basic EXIF only")

    matched = located = 0
    unmatched = []

    for path in files:
        side = read_sidecar(path)
        tags = tags_by_path.get(path.resolve().as_posix(), {})
        words = keywords_from(tags)

        name = None
        if side.get("scientific_name") in species:
            name = side["scientific_name"]
        elif side.get("scientific_name"):
            print(f"  {path.name}: sidecar names "
                  f"'{side['scientific_name']}', which is not in the checklist")
        for word in [] if name else words:
            hit = idx.get(norm(word))
            if hit:
                name = hit
                break
        if not name:
            name = match_filename(path, idx)
        if not name:
            unmatched.append(path.name)
            continue

        site = ""
        nomap = bool(side.get("nomap"))
        for word in words:
            low = word.lower()
            if low.startswith("site:"):
                site = word.split(":", 1)[1].strip()
            elif low in ("nomap", "no-map"):
                nomap = True
        site = side.get("site") or site or tags.get("Sub-location") \
            or tags.get("Location") or tags.get("City") or ""

        date = ""
        for key in ("DateTimeOriginal", "CreateDate"):
            if tags.get(key):
                date = str(tags[key])[:10].replace(":", "-")
                break
        date = side.get("date") or date or pillow_date(path)

        if side.get("lat") is not None and side.get("lng") is not None:
            lat, lng = side["lat"], side["lng"]
        else:
            lat, lng = gps_from_tags(tags) if tags else (None, None)
            if not sane(lat, lng):
                lat, lng = gps_from_pillow(path)
        if nomap or not sane(lat, lng):
            lat = lng = None
        else:
            if GPS_PRECISION is not None:
                lat = round(lat, GPS_PRECISION)
                lng = round(lng, GPS_PRECISION)
            located += 1

        sp = species[name]
        stem = f"{sp.slug}-{len(sp.photos) + 1:02d}"
        w, h = resize(path, IMG / f"{stem}.jpg", FULL_PX)
        resize(path, IMG / f"{stem}-card.jpg", CARD_PX)
        resize(path, IMG / f"{stem}-pin.jpg", PIN_PX)
        if not sp.photos:
            make_og(path, IMG / f"{sp.slug}-og.jpg")
        sp.photos.append(Photo(src=path, full=f"img/{stem}.jpg",
                               card=f"img/{stem}-card.jpg",
                               pin=f"img/{stem}-pin.jpg",
                               width=w, height=h, date=date,
                               site=str(site), lat=lat, lng=lng,
                               phase=str(side.get("phase") or "").strip().lower(),
                               note=str(side.get("note") or "")))
        matched += 1

    if unmatched:
        print(f"  {len(unmatched)} photo(s) had no species match:")
        for name in unmatched[:12]:
            print(f"    - {name}")
        if len(unmatched) > 12:
            print(f"    ... and {len(unmatched) - 12} more")
    return (matched, located)


def pings(species: dict, only: Species | None = None) -> list:
    out = []
    for sp in ([only] if only else species.values()):
        for p in sp.photos:
            if p.lat is None:
                continue
            out.append({
                "lat": p.lat, "lng": p.lng,
                "name": sp.display_name,
                "sci": sp.scientific_name,
                "slug": sp.slug,
                "status": sp.status,
                "statusLabel": STATUS_LABEL[sp.status],
                "color": PIN_COLOR.get(sp.status, "#9fbfcb"),
                "thumb": p.pin,
                "date": p.date,
                "site": p.site,
            })
    return out


# ------------------------------------------------------------------ rendering

_COVER_WARNED = False


def keoki_link(sp: Species) -> str:
    """Deep link where the folder is known, otherwise a search of his site."""
    folder = KEOKI_FOLDER.get(sp.family)
    if folder:
        return f"{KEOKI}/fishes/{folder}/{slugify(sp.scientific_name)}.htm"
    query = urllib.parse.quote(f"site:marinelifephotography.com {sp.scientific_name}")
    return f"https://duckduckgo.com/?q={query}"


def cover_image(species: dict) -> str:
    """Relative path to the image that represents the whole site."""
    global _COVER_WARNED
    wanted = species.get(COVER_SPECIES)
    if wanted and wanted.seen:
        return f"img/{wanted.slug}-og.jpg"
    if wanted and not wanted.seen and not _COVER_WARNED:
        _COVER_WARNED = True
        SAY[0](f"  cover species {COVER_SPECIES} has no photo yet — "
               f"using the first one that does")
    for sp in sorted(species.values(), key=lambda s: s.scientific_name):
        if sp.seen:
            return f"img/{sp.slug}-og.jpg"
    return ""


def card(sp: Species) -> str:
    seen = "seen" if sp.seen else "unseen"
    if sp.seen:
        p = sp.photos[0]
        media = (f'<img src="{e(p.card)}" alt="{e(sp.display_name)}" '
                 f'loading="lazy" width="{p.width}" height="{p.height}">')
        if len(sp.photos) > 1:
            media += f'<span class="count">{len(sp.photos)}</span>'
    else:
        media = '<span class="blank"></span>'
    haw = f'<span class="haw">{e(sp.hawaiian_name)}</span>' if sp.hawaiian_name else ""
    pips = ""
    if sp.phases:
        dots = "".join(
            f'<i class="{"on" if ph in sp.phases_seen else ""}" '
            f'title="{e(PHASE_LABEL[ph])}"></i>' for ph in sp.phases)
        pips = (f'<span class="pips" aria-label="{len(sp.phases_seen)} of '
                f'{len(sp.phases)} phases">{dots}</span>')
    return f"""<a class="card {seen}" href="species/{sp.slug}.html"
   data-status="{e(sp.status)}" data-family="{e(sp.family)}"
   data-seen="{'1' if sp.seen else '0'}"
   data-partial="{'1' if (sp.seen and sp.phases_missing) else '0'}"
   data-search="{e(' '.join(filter(None, [sp.scientific_name, sp.common_name, sp.hawaiian_name, sp.family])).lower())}">
  <figure>{media}</figure>
  <div class="meta">
    <h3>{e(sp.display_name)}</h3>
    <p class="sci">{e(sp.scientific_name)}</p>
    {haw}
    <span class="badge s-{e(sp.status)}">{e(STATUS_LABEL[sp.status])}</span>
    {pips}
  </div>
</a>"""


def page(title: str, body: str, depth: int = 0, tail: str = "",
         desc: str = "", image: str = "", url: str = "") -> str:
    up = "../" * depth
    social = f"""<meta name="description" content="{e(desc)}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="{e(TITLE)}">
<meta property="og:title" content="{e(title)}">
<meta property="og:description" content="{e(desc)}">
<meta property="og:url" content="{e(SITE_URL + url)}">"""
    if image:
        social += f"""
<meta property="og:image" content="{e(SITE_URL + '/' + image)}">
<meta property="og:image:width" content="{OG_W}">
<meta property="og:image:height" content="{OG_H}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="{e(SITE_URL + '/' + image)}">"""
    else:
        social += '\n<meta name="twitter:card" content="summary">'
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
{social}
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,300..700;1,9..144,300..600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
      integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
<link rel="stylesheet" href="{up}style.css">
</head>
<body>
{body}
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
        integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
{tail}
</body>
</html>"""


def render_index(species: dict) -> str:
    ordered = sorted(species.values(), key=lambda s: (s.family, s.scientific_name))
    total = len(ordered)
    seen = sum(1 for s in ordered if s.seen)
    frames = sum(len(s.photos) for s in ordered)
    endemic_total = sum(1 for s in ordered if s.status in ENDEMIC_STATUSES)
    endemic_seen = sum(1 for s in ordered if s.status in ENDEMIC_STATUSES and s.seen)
    nwhi_total = sum(1 for s in ordered if s.status == "endemic_nwhi")
    nwhi_seen = sum(1 for s in ordered if s.status == "endemic_nwhi" and s.seen)
    pct = (seen / total * 100) if total else 0
    phase_total = sum(len(s.phases) for s in ordered)
    phase_seen = sum(len(s.phases_seen) for s in ordered)
    part_done = sum(1 for s in ordered if s.seen and s.phases_missing)

    data = pings(species)
    from_nwhi = sum(1 for p in data if p["lng"] < NWHI_CUTOFF)

    families = sorted({s.family for s in ordered if s.family})
    fam_opts = "".join(f'<option value="{e(f)}">{e(f)}</option>' for f in families)
    cards = "\n".join(card(s) for s in ordered)

    legend = "".join(
        f'<span><i style="background:{PIN_COLOR[k]}"></i>{e(STATUS_LABEL[k])}</span>'
        for k in ("endemic_nwhi", "endemic", "indigenous", "introduced"))

    body = f"""<header class="masthead">
  <div class="wrap">
    <p class="who">{e(OWNER)}</p>
    <h1>{e(TITLE)}</h1>
    <p class="lede">Reef fishes of the Hawaiian Archipelago, photographed as I
    find them. Where each one was, what it is, and how much of the list is
    still ahead of me.</p>
  </div>
</header>

<section class="tally">
  <div class="wrap">
    <p class="big"><strong>{seen}</strong><span>of {total} photographed</span></p>
    <div class="progress"><span style="width:{pct:.1f}%"></span></div>
    <div class="mapwrap">
      <div id="map" data-empty="{'1' if not data else '0'}"></div>
      <p class="mapempty" {'hidden' if not data else ''}>Loading the archipelago…</p>
    </div>
    <div class="legend">{legend}</div>
    <dl class="numbers">
      <div><dt>Frames in the catalog</dt><dd>{frames}</dd></div>
      <div><dt>Phases photographed</dt><dd>{phase_seen} of {phase_total}</dd></div>
      <div><dt>Frames with a position</dt><dd>{len(data)}</dd></div>
      <div><dt>Shot in the NWHI</dt><dd>{from_nwhi}</dd></div>
      <div><dt>Endemics</dt><dd>{endemic_seen} of {endemic_total}</dd></div>
      <div><dt>NWHI endemics</dt><dd>{nwhi_seen} of {nwhi_total}</dd></div>
    </dl>
  </div>
</section>

<nav class="controls wrap" aria-label="Filter the catalog">
  <input type="search" id="q" placeholder="Search names or families" aria-label="Search">
  <select id="family" aria-label="Family"><option value="">All families</option>{fam_opts}</select>
  <select id="status" aria-label="Origin">
    <option value="">All origins</option>
    <option value="endemic">Hawaiian endemic</option>
    <option value="endemic_nwhi">NWHI endemic</option>
    <option value="indigenous">Indigenous</option>
    <option value="introduced">Introduced</option>
  </select>
  <div class="segmented" role="group" aria-label="Photographed">
    <button type="button" data-seen="" class="on">All</button>
    <button type="button" data-seen="1">Photographed</button>
    <button type="button" data-seen="0">Still missing</button>
    <button type="button" data-partial="1">Missing a phase ({part_done})</button>
  </div>
  <p class="shown" id="shown"></p>
</nav>

<main class="wrap"><div class="grid" id="grid">
{cards}
</div>
<p class="empty" id="empty" hidden>Nothing matches those filters.</p>
</main>

<footer class="wrap">
  <p>Origin categories follow the Bishop Museum checklist of the fishes of the
  Hawaiian Archipelago.{" Positions are rounded to %d decimal places." % GPS_PRECISION if GPS_PRECISION is not None else ""}
  Photographs © {e(OWNER)}.</p>
</footer>"""

    tail = (f'<script>window.DEX_PINGS = {json.dumps(data)};'
            f'window.DEX_BASE = "";</script>\n<script src="app.js"></script>')
    return page(f"{TITLE} — {OWNER}", body, 0, tail,
                desc=BLURB, image=cover_image(species), url="/")


def render_species(sp: Species, species: dict) -> str:
    same_family = [s for s in species.values()
                   if s.family == sp.family and s.scientific_name != sp.scientific_name]
    same_family.sort(key=lambda s: (not s.seen, s.scientific_name))

    def plate(p):
        caption = " · ".join(filter(None, [p.date, p.site, p.note]))
        return f"""<figure>
  <img src="../{e(p.full)}" alt="{e(sp.display_name)}" loading="lazy"
       width="{p.width}" height="{p.height}">
  {f'<figcaption>{e(caption)}</figcaption>' if caption else ''}
</figure>"""

    if sp.seen:
        if sp.phases:
            blocks = []
            for ph in sp.phases:
                got = [p for p in sp.photos if p.phase == ph]
                if got:
                    blocks.append(f'<h2 class="phasehead">{e(PHASE_LABEL[ph])}</h2>'
                                  f'{"".join(plate(p) for p in got)}')
                else:
                    blocks.append(
                        f'<h2 class="phasehead">{e(PHASE_LABEL[ph])}</h2>'
                        f'<div class="gap"><p>Not photographed yet.</p></div>')
            loose = [p for p in sp.photos if p.phase not in sp.phases]
            if loose:
                blocks.append('<h2 class="phasehead">Unassigned</h2>'
                              + "".join(plate(p) for p in loose))
            gallery = f'<div class="plates">{"".join(blocks)}</div>'
        else:
            gallery = f'<div class="plates">{"".join(plate(p) for p in sp.photos)}</div>'
    else:
        gallery = '<div class="plates missing"><p>Not photographed yet.</p></div>'

    rows = [("Family", sp.family or "—"),
            ("Hawaiian name", sp.hawaiian_name or "—"),
            ("Origin", STATUS_LABEL[sp.status])]
    if sp.phases:
        got = ", ".join(PHASE_LABEL[ph] for ph in sp.phases_seen) or "none yet"
        rows.append(("Phases photographed",
                     f"{got} ({len(sp.phases_seen)} of {len(sp.phases)})"))
    if sp.regions:
        rows.append(("I've found it in", ", ".join(sp.regions)))
    if sp.notes:
        rows.append(("Note", sp.notes))
    table = "".join(f"<div><dt>{e(k)}</dt><dd>{e(v)}</dd></div>" for k, v in rows)

    query = sp.scientific_name.replace(" ", "+")
    links = (f'<a href="https://www.marinespecies.org/aphia.php?p=taxlist&searchpar=0&tComp=contains&tName={query}">WoRMS</a>'
             f'<a href="https://www.fishbase.se/summary/{sp.scientific_name.replace(" ", "-")}.html">FishBase</a>'
             f'<a href="https://www.gbif.org/species/search?q={query}">GBIF</a>'
             f'<a href="{e(keoki_link(sp))}">Marine Life Photography</a>')

    siblings = "".join(
        f'<a href="{s.slug}.html" class="{"seen" if s.seen else "unseen"}">'
        f'{e(s.common_name or s.scientific_name)}</a>' for s in same_family[:18])

    data = pings(species, only=sp)
    minimap = '<div id="map" class="minimap"></div>' if data else ""

    body = f"""<header class="specimen">
  <div class="wrap">
    <a class="back" href="../index.html">All species</a>
    <span class="badge s-{e(sp.status)}">{e(STATUS_LABEL[sp.status])}</span>
    <h1>{e(sp.display_name)}</h1>
    <p class="sci">{e(sp.scientific_name)}</p>
  </div>
</header>
<main class="wrap specimen-body">
  {gallery}
  <aside>
    <dl class="facts">{table}</dl>
    {minimap}
    <div class="links">{links}</div>
  </aside>
</main>
{f'<section class="wrap kin"><h2>Other {e(sp.family)}</h2><div>{siblings}</div></section>' if siblings else ''}"""

    bits = [STATUS_LABEL[sp.status]]
    if sp.family:
        bits.append(sp.family)
    if sp.hawaiian_name:
        bits.append(sp.hawaiian_name)
    desc = f"{sp.display_name} ({sp.scientific_name}). " + ", ".join(bits) + "."
    if not sp.seen:
        desc += " Not photographed yet."
    image = f"img/{sp.slug}-og.jpg" if sp.seen else cover_image(species)

    tail = (f'<script>window.DEX_PINGS = {json.dumps(data)};'
            f'window.DEX_BASE = "../";</script>\n<script src="../app.js"></script>')
    return page(f"{sp.display_name} — {TITLE}", body, 1, tail,
                desc=desc, image=image, url=f"/species/{sp.slug}.html")


# ----------------------------------------------------------------------- main

def main(quiet: bool = False) -> dict:
    """Build the site. Returns a summary the ingest tool displays."""
    global _COVER_WARNED
    _COVER_WARNED = False
    say = (lambda *a: None) if quiet else print
    SAY[0] = say
    say("Building the catalog")
    species = load_species()
    say(f"  {len(species)} species in the checklist")

    # site/img survives between runs so rebuilds don't redo every thumbnail.
    # Stale files are pruned below against what this run actually referenced.
    if (OUT / "species").exists():
        shutil.rmtree(OUT / "species")
    (OUT / "species").mkdir(parents=True, exist_ok=True)
    IMG.mkdir(parents=True, exist_ok=True)

    idx = build_index(species)
    matched, located = collect_photos(species, idx)
    seen = sum(1 for s in species.values() if s.seen)
    say(f"  {matched} photo(s) matched to {seen} species")
    blur = f", rounded to {GPS_PRECISION} dp" if GPS_PRECISION is not None else ""
    say(f"  {located} photo(s) have a position{blur}")
    if matched and not located:
        say("  no coordinates found — the map will stay empty")

    (OUT / "index.html").write_text(render_index(species), encoding="utf-8")
    for sp in species.values():
        (OUT / "species" / f"{sp.slug}.html").write_text(
            render_species(sp, species), encoding="utf-8")
    (OUT / "pings.json").write_text(
        json.dumps(pings(species), indent=1), encoding="utf-8")

    for asset in ("style.css", "app.js"):
        src = ASSETS / asset
        if src.exists():
            shutil.copy2(src, OUT / asset)
    (OUT / ".nojekyll").write_text("", encoding="utf-8")

    wanted = {"og.jpg"}
    for sp in species.values():
        if sp.seen:
            wanted.add(f"{sp.slug}-og.jpg")
        for photo in sp.photos:
            for ref in (photo.full, photo.card, photo.pin):
                wanted.add(Path(ref).name)
    dropped = 0
    for stale in IMG.glob("*.jpg"):
        if stale.name not in wanted:
            stale.unlink(missing_ok=True)
            dropped += 1
    if dropped:
        say(f"  pruned {dropped} image(s) with no photo behind them")

    say(f"  wrote {len(species) + 1} pages to {OUT}")
    say(f"  preview:  python3 -m http.server -d {OUT} 8000")

    return {"total": len(species), "seen": seen,
            "frames": matched, "located": located}


if __name__ == "__main__":
    main()
