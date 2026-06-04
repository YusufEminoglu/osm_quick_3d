# Changelog

All notable changes to **OSM Quick 3D** are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) · versioning: [SemVer](https://semver.org/).

## [0.7.0] - 2026-06-04

- **Selectable study-area shape**: rectangle (map extent), rounded rectangle, circle or hexagon. OSM features are clipped to the chosen shape, so it is what gets exported. Circle and hexagon are inscribed in the shorter side of the extent.
- **Ground base**: an optional recessed slab the city stands on — the study area buffered outward by 5 m, extruded in 3D as a plinth from −5 m up to ground level (added at the bottom of the group, styled as a subtle 2D ground fill).
- **Fully English UI**: every dialog, status, error and message-bar string is now English.
- **Redesigned dialog**: a header with the plugin icon and grouped sections (Study area · Layers · 3D & base · Output & data) instead of the old flat form.
- **New plugin icon**: an isometric city on a hexagonal boundary with a ground plinth, in the plugin's function palette.

- Fix: building/green/road/water/bike layers are now created as Multi* (MultiPolygon / MultiLineString), so a feature that boundary-clipping splits into several parts is no longer silently dropped — matters most on larger areas where clipping is common. Adds a headless `tests/test_pure_logic.py` harness (25 checks) covering the pure-Python parsing, floor-count, UTM, waterway-width, colour-expression and cache logic.

## [0.6.0] - 2026-06-04

- Buildings now carry a computed `footprint_m2` (polygon area in the metric CRS) and an estimated `gfa_m2` (footprint × floors) column — present in both the memory and GeoPackage outputs — so the result is ready to label, sum or analyse for quick planning quantities.
- The run summary now reports the total building footprint and estimated gross floor area.

## [0.5.0] - 2026-06-04

- Tree points now get a matching 3D pass — simple green sphere canopies resting on the terrain — whenever 3D extrusion is on, so the 3D scene gains greenery instead of bare ground. Degrades to the 2D tree markers on builds without a usable 3D point symbol.
- README refreshed to document the function-coloured massing, height exaggeration, layer group, GeoPackage save and Overpass cache.

## [0.4.0] - 2026-06-04

- Overpass responses are now cached on disk for a week, keyed by the exact query. Re-running on the same area (or just toggling a layer) opens instantly without hitting the rate-limited public API again. A new "use cache when possible" toggle (on by default) controls it.

## [0.3.0] - 2026-06-04

- Optional "Save to GeoPackage" — when enabled, every downloaded layer is written into one `.gpkg` and reloaded from it, so the result survives a project reload instead of vanishing as a memory layer. If a write fails, that layer falls back to a memory layer and a warning is shown.

## [0.2.0] - 2026-06-04

- 3D massing is now coloured by OSM function (residential, commercial, industrial, civic, worship) in the same palette as the 2D legend, instead of a single flat grey — via a data-defined diffuse colour on the 3D material.
- New height-exaggeration factor (0.5×–5.0×) in the dialog multiplies the extruded building heights, so low-rise districts read in the 3D Map View. 1.0× keeps true OSM heights.
- Every downloaded layer now lands inside one tidy "OSM Quick 3D — EPSG:xxxx" layer-tree group, keeping the legend clean on larger areas.

## [0.1.0] - 2026-06-04

- Initial release.
- One-click OpenStreetMap download for the visible map area or the extent of selected features.
- Native QGIS memory layers, styled by function: buildings by OSM use, roads by highway class and width, water blue, greens green.
- Flat-roof 3D building massing via native QGIS 3D symbology; height from OSM (height, then floor count, then a default).
- Optional basemap layer moved underneath as the ground, in 2D and in the 3D Map View.
- Built for larger areas than a web 3D scene; the practical limit is the public Overpass API.
