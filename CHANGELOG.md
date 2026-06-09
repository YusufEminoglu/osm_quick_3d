# Changelog

All notable changes to **OSM Quick 3D** are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) · versioning: [SemVer](https://semver.org/).

## [0.19.0] - 2026-06-08

- **Polygon study area**: a new **"Polygon (selected feature)"** shape clips OSM to your own selected polygon feature(s) in the active layer (dissolved if several, reprojected to the area's UTM zone and cropped to the Max-area cap), so you can work over any custom boundary — not only the inscribed rectangle/rounded/circle/hexagon shapes. Select the polygon(s) first, then run; a hint and a clear error guide the workflow.
- **Four new map themes**: **Anime Cel** (bright cartoon), **Desert Dunes** (warm arid), **Pastel Candy** (soft kawaii) and **Vaporwave** (neon retro) — twelve themes in total. Each recolours buildings, roads, water, greens and the native 3D massing.
- **Readable primary button**: the **"Download & build 3D"** button now pins its teal fill, border and white text together inline, so it can no longer fall back to unreadable white-on-grey on Qt styles that drop a stylesheet `background-color`.
- **Clearer dock**: the **Build** and **Theme & Style** tabs now carry a one-line role banner (build first, then live-tune) so their shared controls read as intentional, not duplicated. The unused legacy modal dialog was removed.
- **QGIS 4 hardening**: scoped-enum-safe `QgsMapLayerComboBox` layer filters and `QPainter` blend-mode lookup, alongside the existing defensive 3D/enum fallbacks.

## [0.18.9] - 2026-06-08

- Remove the stale 3D panel, add 3D layer visibility, metric road widths, 2m base depth, and new themes

## [0.18.8] - 2026-06-08

- Remove unsafe 3D dock embedding and ship a managed native 3D scene controller

## [0.18.7] - 2026-06-08

- Stop premature 3D startup, make basemaps optional, clip underlays, and harden 3D dock fallback

## [0.18.6] - 2026-06-08

- Fix QGIS 4 compatibility, basemap underlay movement, and 3D dock state sync

## [0.18.5] - 2026-06-08

- Remove unsafe QWindowContainer embedding for QGIS 3.44 stability

## [0.18.4] - 2026-06-08

- Fix embedded-only 3D dock scene and bind extrusion renderers to layers

## [0.18.3] - 2026-06-08

- Fix embedded QGIS 3D canvas, extrusion refresh, and compact dock spacing

## [0.18.2] - 2026-06-08

- Fix dock headings, checkbox visibility, contrast, and advanced style panel

## [0.18.1] - 2026-06-08

- Fix dock scrolling and compact tab/category headings

## [0.18.0] - 2026-06-08

- Add unified dock panel with embedded 3D view and fix height coloring/clipping

## [0.17.0] - 2026-06-08

- Release v0.17.0

## [0.16.0] - 2026-06-08

- Fix visual basemap clipping black borders in 3D Map View and add Advanced Layer Styling tab

## [0.15.0] - 2026-06-08

- Fix basemap clipping in 3D Map View, implement dynamic height classification, fix base_3d NameError, and polish dock widget stacking

## [0.14.0] - 2026-06-08

- Add two-layer basemap clipping, robust height value casting, continuous/discrete/quantile classification controls, and programmatically stack 3D view dock widget above the controller.

## [0.13.2] - 2026-06-08

- Fix 3D resolution updates, add building color fallback in 3D, and reorganize dock UI with an Advanced Style panel.

## [0.13.1] - 2026-06-08

- Fix visual basemap clipping, base plinth capping/offset, and add 3D map tile resolution controls.

## [0.13.0] - 2026-06-08

- Visual Basemap Clipping: implemented automatic masking of the underlay basemap to the base plinth (or study area) using QgsInvertedPolygonRenderer, which propagates to 3D drape rendering for a clean floating island model
- Plinth Extrusion & Capping: fixed a double-shift vertical offset bug that caused the base plinth to be buried, and avoided Z-fighting with draped terrain
- 3D Map Resolution Controls: added a "Map resolution (3D)" setting in both the dialog and docked panel to control draped map canvas tile size live (256px to 4096px)
- Dock state preservation: remembers base transparency and resolution settings across runs

## [0.12.0] - 2026-06-08

- Scrollable UI panels: wrapped dialog and dock settings in resizable scroll areas for smaller monitors
- Compact design: reduced padding, margins, and font sizes to 11px to make UI compact and highly professional
- Z-Fighting fix: set default ground base slab top_z to -0.1 to avoid overlapping with draped basemaps
- Solid base capping: enabled back-faces rendering to ensure the ground base plinth is solid and filled

## [0.11.0] - 2026-06-08

- Live 3D Controller Dock: introduced an interactive sidebar panel to adjust building height exaggeration, color modes, labels, and base slab depth dynamically without re-downloading

## [0.10.1] - 2026-06-07

- Cartographic elevation: styled car parks and paved plazas with category-specific dark neutral outlines instead of green outlines

## [0.10.0] - 2026-06-07

- Add relation multipolygon support, water areas, tree scattering, and car parks/plazas

## [0.9.0] - 2026-06-05

- Optional **name labels** for buildings and roads: a "Show name labels" toggle labels each by its OSM `name` (only non-empty names) with a white halo. Fully defensive — degrades to unlabelled on builds without the labeling API.

## [0.8.4] - 2026-06-04

- A **"Clear OSM cache"** button in the dialog deletes all cached Overpass responses from disk and reports how many were removed — handy when the disk cache has grown or you want to force a fresh download.

## [0.8.3] - 2026-06-04

- The ground base is now tinted to harmonise with the chosen building colours — a darkened tone of the selected tint for the 3D plinth and a light tint of it for the 2D ground fill, so plinth and city read as one palette (neutral slate is kept for By function / By height).

## [0.8.2] - 2026-06-04

- The dialog now shows a live gradient **preview swatch** under the building-colour selector, updating as you change the mode (low → tall for the tints, the use palette for By function) — so you see the look before running.

## [0.8.1] - 2026-06-04

- Three more soft building-colour tints: **Soft salmon**, **Soft purple** and **Soft sand**, alongside the existing gray, warm and teal (and By function / By height).

## [0.8.0] - 2026-06-04

- **Selectable building colours**, applied identically in 2D and 3D: by OSM function (the categorized use palette), or a soft height-graduated tint — **By height** (cool neutral), **Soft tinted gray**, **Soft tinted warm** or **Soft teal**. The tint modes drive both the 2D fill and the native 3D diffuse from one `color_rgb` ramp by building height, so the massing always matches the map.
- **Elite-soft dialog**: a cohesive light theme with rounded white group cards, a gradient header, soft focus accents and a teal primary button — a more polished, "perfect" look.

## [0.7.1] - 2026-06-04

- Security/quality fixes to clear the QGIS Plugin Hub scan that blocked 0.7.0: the Overpass cache filename now uses SHA-256 instead of SHA-1 (it was only ever a filename digest, never security), and two Flake8 nits (an unused import and a binary-operator line break) are resolved. No functional change.

## [0.7.0] - 2026-06-04

- **Selectable study-area shape**: rectangle (map extent), rounded rectangle, circle or hexagon. OSM features are clipped to the chosen shape, so it is what gets exported. Circle and hexagon are inscribed in the shorter side of the extent.
- **Ground base**: an optional recessed slab the city stands on — the study area buffered outward by 5 m, extruded in 3D as a plinth from −5 m up to ground level (added at the bottom of the group, styled as a subtle 2D ground fill).
- **Fully English UI**: every dialog, status, error and message-bar string is now English.
- **Redesigned dialog**: a header with the plugin icon and grouped sections (Study area · Layers · 3D & base · Output & data) instead of the old flat form.
- **New plugin icon**: an isometric city on a hexagonal boundary with a ground plinth, in the plugin's function palette.

## [0.6.1] - 2026-06-04

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
