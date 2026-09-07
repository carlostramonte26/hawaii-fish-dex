# Drop exported JPEGs here

Subfolders are fine — the build walks the whole tree.

The build finds the species two ways:

1. **Lightroom keyword** (preferred). Add the scientific name as a keyword,
   e.g. `Chaetodon miliaris`. Common and Hawaiian names in the checklist
   work too. Requires exiftool on the build machine.
2. **Filename**, e.g. `Chaetodon_miliaris_004.jpg`.

Optional keywords:

    site:Pearl and Hermes
    nomap

GPS comes from the photo's own EXIF, so nothing extra is needed if your camera
or housing logs position. Lightroom's Map module writes the same fields if you
want to place frames by hand. Published coordinates are rounded — see the main
README.

Export at 2000px on the long edge or larger. The build makes its own
thumbnails, so there is no need to ship full-resolution files.
