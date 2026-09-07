/* Catalog filtering + the archipelago map. */

(function () {
  var grid = document.getElementById("grid");
  if (!grid) return;

  var cards = Array.prototype.slice.call(grid.querySelectorAll(".card"));
  var q = document.getElementById("q");
  var family = document.getElementById("family");
  var status = document.getElementById("status");
  var shown = document.getElementById("shown");
  var empty = document.getElementById("empty");
  var buttons = Array.prototype.slice.call(
    document.querySelectorAll(".segmented button")
  );
  var seenFilter = "";
  var partialOnly = false;

  function apply() {
    var term = (q.value || "").trim().toLowerCase();
    var fam = family.value;
    var st = status.value;
    var visible = 0;

    cards.forEach(function (card) {
      var ok =
        (!term || card.dataset.search.indexOf(term) !== -1) &&
        (!fam || card.dataset.family === fam) &&
        (!st || card.dataset.status === st) &&
        (!seenFilter || card.dataset.seen === seenFilter) &&
        (!partialOnly || card.dataset.partial === "1");
      card.hidden = !ok;
      if (ok) visible++;
    });

    shown.textContent =
      visible === cards.length
        ? cards.length + " species"
        : visible + " of " + cards.length + " species";
    empty.hidden = visible !== 0;
  }

  buttons.forEach(function (button) {
    button.addEventListener("click", function () {
      buttons.forEach(function (b) { b.classList.remove("on"); });
      button.classList.add("on");
      partialOnly = button.dataset.partial === "1";
      seenFilter = partialOnly ? "" : (button.dataset.seen || "");
      apply();
    });
  });

  [q, family, status].forEach(function (el) {
    el.addEventListener("input", apply);
  });

  apply();
})();


(function () {
  var node = document.getElementById("map");
  if (!node) return;

  var pins = window.DEX_PINGS || [];
  var base = window.DEX_BASE || "";
  var note = document.querySelector(".mapempty");

  function fail(message) {
    node.classList.add("off");
    if (note) { note.hidden = false; note.textContent = message; }
  }

  if (typeof L === "undefined") {
    fail("The map needs a network connection to load.");
    return;
  }
  if (!pins.length) {
    fail("No photos with coordinates yet. Turn on GPS logging, or add the position in Lightroom.");
    return;
  }
  if (note) note.hidden = true;

  var map = L.map(node, {
    scrollWheelZoom: false,
    worldCopyJump: false,
    minZoom: 2,
    maxZoom: 19
  });

  // Sentinel-2 cloudless is the default because it covers the whole planet at
  // a consistent 10 m. USGS/NAIP is sharper but is only flown over the main
  // islands — over the NWHI it serves a placeholder tile, which is where the
  // grey came from. It stays available as an opt-in layer.
  var sentinel = L.tileLayer(
    "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2024_3857/default/g/{z}/{y}/{x}.jpg",
    {
      maxZoom: 19,
      maxNativeZoom: 14,
      attribution:
        'Sentinel-2 cloudless by <a href="https://eox.at">EOX</a> (CC BY 4.0), ' +
        "contains modified Copernicus Sentinel data"
    }
  );

  var sharper = L.tileLayer(
    "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}",
    {
      maxZoom: 19,
      maxNativeZoom: 16,
      attribution: "U.S. Geological Survey, The National Map"
    }
  );

  var plain = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution:
      '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  });

  sentinel.addTo(map);

  L.control.layers(
    {
      "Satellite": sentinel,
      "Satellite — sharper, main islands only": sharper,
      "Map": plain
    },
    null,
    { position: "topright", collapsed: true }
  ).addTo(map);

  // Papahānaumokuākea sits far northwest of the main islands. Draw the line
  // so the two halves of the chain read as different places.
  L.polyline([[26.6, -161.0], [21.0, -161.0]], {
    color: "#7ee0c0",
    weight: 1,
    opacity: 0.35,
    dashArray: "5 6",
    interactive: false
  }).addTo(map);

  var group = L.featureGroup();

  pins.forEach(function (pin) {
    var marker = L.circleMarker([pin.lat, pin.lng], {
      radius: 6,
      color: pin.color,
      weight: 2,
      fillColor: pin.color,
      fillOpacity: 0.45
    });

    var meta = [pin.date, pin.site].filter(Boolean).join(" &middot; ");
    marker.bindPopup(
      '<a class="pop" href="' + base + "species/" + pin.slug + '.html">' +
        '<img src="' + base + pin.thumb + '" alt="">' +
        "<strong>" + pin.name + "</strong>" +
        "<em>" + pin.sci + "</em>" +
        '<span class="pop-status">' + pin.statusLabel + "</span>" +
        (meta ? '<span class="pop-meta">' + meta + "</span>" : "") +
      "</a>",
      { minWidth: 190, closeButton: false }
    );

    marker.bindTooltip(pin.name, { direction: "top", offset: [0, -6] });
    group.addLayer(marker);
  });

  group.addTo(map);
  map.fitBounds(group.getBounds(), { padding: [40, 40], maxZoom: 10 });

  // Scroll-zoom is off so the page still scrolls; click the map to enable it.
  map.on("click", function () { map.scrollWheelZoom.enable(); });
  map.on("mouseout", function () { map.scrollWheelZoom.disable(); });
})();
