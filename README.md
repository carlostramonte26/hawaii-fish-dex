# Hawaiian Reef Fish

A personal photographic catalog of the reef fishes of the Hawaiian Archipelago.
Photos live on disk, species data lives in one CSV, and a build script turns the
two into a static site. There is no database and no server.

## How it works

```
photos/          exported JPEGs, any folder structure
data/species.csv the checklist: names, family, origin, notes
assets/          style.css and app.js, edited by hand
scripts/build.py reads photos + checklist, writes site/
site/            generated, gitignored, published by GitHub Actions
```

## Day to day

```bash
python3 scripts/dex.py
```

Or double-click **Add fish.command** in Finder, which does the same thing.

That one command is the whole workflow. It builds the site, then opens
`127.0.0.1:8777`. From that page you can:

- **Drop photos in.** Each gets a form: species (autocompleting against the
  checklist, with a `+` beside any fish you have no photo of yet), date, site,
  note, and a satellite map you click to place the exact position. Drag the pin
  to adjust. If the file has GPS the pin starts there.
- **Save.** The image is copied into `photos/`, a `<photo>.json` sidecar is
  written beside it, and the site rebuilds immediately. The header shows your
  running count.
- **Preview site.** Opens the real site, current as of your last save.
- **Publish.** Commits everything and pushes to GitHub. The Action redeploys.

Ctrl-C in the terminal when you're finished.

Rebuilds are incremental — thumbnails already generated are reused, so adding
one photo to a catalog of five hundred takes a fraction of a second rather than
regenerating everything.

### Sidecars

```json
{
 "scientific_name": "Apolemichthys arcuatus",
 "date": "2026-08-22",
 "site": "Kure Atoll",
 "lat": 28.413578,
 "lng": -178.343124,
 "nomap": false,
 "note": "over rubble at 40 m"
}
```

The sidecar wins over anything in the file's own metadata, so the pin you place
is the position that reaches the site, to full precision. They're plain text —
fixing a bad pin means editing one line, not re-uploading.

### Doing it by hand

`scripts/build.py` still works standalone if you'd rather drive it yourself, and
it still reads Lightroom keywords and EXIF GPS for photos with no sidecar. See
"Getting a photo in without the tool" below.

## Getting a photo in without the tool

Add the scientific name as a **Lightroom keyword** on the image, export, and drop
the file in `photos/`. Your existing catalog is the CMS. Common names and
Hawaiian names in the checklist match too, so a keyword of `Kole` finds
*Ctenochaetus strigosus*.

Optional keywords:

```
site:Pearl and Hermes    label shown under the photo
nomap                    keep this frame off the map
```

## Positions

Order of precedence: the sidecar's pinned coordinate, then GPS read by exiftool,
then GPS read by Pillow. Coordinates are published **exactly as pinned**.

If you ever want to blur them before publishing:

```bash
DEX_GPS_PRECISION=3 python3 scripts/build.py   # about 110 m
DEX_GPS_PRECISION=2 python3 scripts/build.py   # about 1.1 km
```

The checkbox in the ingest form, or `"nomap": true` in a sidecar, drops one
frame's position entirely while keeping the photo.

The map splits the chain at 161°W, so anything west of Nihoa counts as
Northwestern Hawaiian Islands. Species pages show which half of the archipelago
your own photos came from, which is separate from the published range.

## Basemaps

The map uses USGS The National Map imagery — public domain, no API key, and it
covers the whole archipelago including Papahānaumokuākea. A layer switcher in
the corner offers imagery, imagery with labels, and plain OSM.

Google's satellite tiles need an API key plus a billing account, and their terms
don't allow serving them through Leaflet, so that route means rewriting the map
on the Google Maps JS API. Esri's `server.arcgisonline.com` imagery is keyless
and widely used, but Esri says it needs a licence and is not for commercial use,
and the legacy endpoint may be switched off without notice — worth knowing if
you ever sell prints from this site. If you want to swap, the tile layers are at
the top of the map section in `assets/app.js`.

If exiftool isn't installed, the build falls back to filenames, so
`Chaetodon_miliaris_004.jpg` works with no metadata at all.

## Build it

```bash
pip install -r requirements.txt
brew install exiftool          # optional but recommended
python3 scripts/build.py
python3 -m http.server -d site 8000
```

The run reports how many photos matched a species and how many carried usable
GPS. Anything it can't match is listed by filename, so nothing silently
disappears.

The map is Leaflet loaded from a CDN. If the network is unavailable the map area
explains itself and the rest of the page still works.

## Publish it

```bash
git init && git branch -M main
git remote add origin git@github.com:carlostramonte26/hawaii-fish-dex.git
git add . && git commit -m "First catalog"
git push -u origin main
```

Then in the repo: **Settings → Pages → Source: GitHub Actions**. The workflow in
`.github/workflows/build.yml` installs exiftool, runs the build, and deploys.
The site lands at `https://carlostramonte26.github.io/hawaii-fish-dex/`.

`site/` is gitignored on purpose — the Action rebuilds it from your photos on
every push, so the generated HTML never has to be committed or merged.

`.gitattributes` routes images through Git LFS. Run `git lfs install` once
before the first push. Free LFS is 1 GB, so export at 2000px rather than
shipping full-resolution files. If you'd rather not use LFS at all, delete
`.gitattributes` and keep exports under a few hundred KB each.

## The checklist is a starting point, not a source

`data/species.csv` ships with 154 common Hawaiian reef fishes. Columns are
scientific name, AphiaID, family, common name, Hawaiian name, origin, notes. I compiled it as
scaffolding so the site has something to render. **Verify it before you publish
anything.** The authoritative source for origin status is:

> Mundy, B.C. 2005. *Checklist of the Fishes of the Hawaiian Archipelago.*
> Bishop Museum Bulletins in Zoology 6.
> https://hbs.bishopmuseum.org/pubs-online/pdf/bz06.pdf

It classifies every species in the 200-nmi EEZ as endemic, indigenous,
successfully or unsuccessfully introduced, waif, questionably occurring, or
falsely recorded. A machine-readable version is distributed through OBIS and
NODC (accession 0001486), which is far easier to work with than the PDF.

The `status` column accepts `endemic` (Hawaiian endemic, archipelago-wide),
`endemic_nwhi` (found only in the Northwestern Hawaiian Islands), `indigenous`,
`introduced`, `not_in_hawaii`, `waif`, and `questionable`.

**No row is currently marked `endemic_nwhi`.** The category is wired through the
badges, filters, map colours, and counters, but I would not assign it from
memory — NWHI-restricted fish endemics are genuinely few and the popular sources
disagree with each other. Two data points from checking:

- *Prognathodes basabei* is archipelago-wide, not NWHI-only. The description
  paper reports it throughout the chain, shallower in the NWHI and deeper in the
  main islands. It stays `endemic` here.
- A NOAA press item calls the bandit angelfish *Apolemichthys arcuatus* endemic
  to NWHI deep reefs, which conflicts with its usual treatment as a
  Hawaiʻi-plus-Johnston species. Worth resolving before you badge it.

Mundy gives each species' range *within* the archipelago alongside its status,
which is exactly the field this column needs. That's the pass to make once you
have the checklist loaded.

Species I have flagged in the `notes` column as needing a check:
*Chaetodon tinkeri* and *Cirrhitops fasciatus* have both been treated as
Hawaiian endemics historically but have records elsewhere.

### Filling in AphiaIDs

```bash
python3 scripts/enrich.py
```

This queries WoRMS, writes the AphiaID into `species.csv`, and lists every name
WoRMS now treats as a synonym in `data/name_review.csv`. It deliberately does
not rewrite your names or touch the `status` column — those are your calls.

### Cross-checking status against FishBase

FishBase records status per country. Since you already work in R:

```r
library(rfishbase); library(dplyr); library(readr)

mine <- read_csv("data/species.csv")
fb <- fb_tbl("country") |>
  filter(C_Code == "840") |>          # USA; Hawaii records sit under this
  select(SpecCode, Status)

fb_tbl("species") |>
  mutate(scientific_name = paste(Genus, Species)) |>
  inner_join(fb, by = "SpecCode") |>
  right_join(mine, by = "scientific_name") |>
  filter(tolower(Status) != status) |>
  select(scientific_name, mine = status, fishbase = Status) |>
  write_csv("data/status_review.csv")
```

Disagreements are expected — FishBase aggregates at country level and Mundy is
archipelago-specific. Mundy wins.

## Adding species

Append a row to `data/species.csv` and rebuild. Species with no photo render as
an empty slot in the grid, which is the point: the gaps are the target list.

## Later

Upload from the browser is the obvious next step. The clean version is a small
form that writes into `photos/` and opens a pull request, so the file-on-disk
model stays intact and the site keeps working if the upload path ever breaks.
