#!/usr/bin/env python3
"""
Fill in AphiaIDs and flag names that WoRMS no longer accepts.

Run this on your own machine (it needs the network). It updates
data/species.csv in place and writes data/name_review.csv listing every
name WoRMS treats as a synonym, so you can decide what to rename rather
than having a script silently rewrite your taxonomy.

    python3 scripts/enrich.py

Nothing here touches the status column. Endemism is a regional call and
should come from the Bishop Museum checklist, not from a general-purpose
taxonomic backbone. See README for the FishBase cross-check.
"""

from __future__ import annotations

import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "species.csv"
REVIEW = ROOT / "data" / "name_review.csv"
WORMS = "https://www.marinespecies.org/rest/AphiaRecordsByName/"
PAUSE = 0.4


def lookup(name: str) -> dict | None:
    url = WORMS + urllib.parse.quote(name) + "?like=false&marine_only=true"
    try:
        with urllib.request.urlopen(url, timeout=25) as resp:
            if resp.status == 204:
                return None
            records = json.loads(resp.read().decode("utf-8"))
    except Exception as err:
        print(f"    lookup failed for {name}: {err}")
        return None
    if not records:
        return None
    for rec in records:
        if rec.get("status") == "accepted":
            return rec
    return records[0]


def main() -> None:
    if not DATA.exists():
        sys.exit(f"Missing {DATA}")

    with DATA.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fields = reader.fieldnames or []
        rows = list(reader)

    review = []
    filled = 0

    for row in rows:
        name = (row.get("scientific_name") or "").strip()
        if not name or (row.get("aphia_id") or "").strip():
            continue
        print(f"  {name}")
        rec = lookup(name)
        time.sleep(PAUSE)
        if not rec:
            review.append([name, "", "not found in WoRMS", ""])
            continue

        row["aphia_id"] = str(rec.get("AphiaID", "") or "")
        filled += 1

        valid = (rec.get("valid_name") or "").strip()
        if valid and valid != name:
            review.append([name, row["aphia_id"], rec.get("status", ""), valid])

        if not (row.get("family") or "").strip() and rec.get("family"):
            row["family"] = rec["family"]

    with DATA.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    with REVIEW.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["name_in_checklist", "aphia_id", "worms_status",
                         "worms_accepted_name"])
        writer.writerows(review)

    print(f"\nFilled {filled} AphiaIDs.")
    print(f"{len(review)} name(s) need a look: {REVIEW}")


if __name__ == "__main__":
    main()
