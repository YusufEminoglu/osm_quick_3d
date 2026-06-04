# Changelog

All notable changes to **OSM Quick 3D** are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) · versioning: [SemVer](https://semver.org/).

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
