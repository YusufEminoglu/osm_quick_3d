# Changelog

All notable changes to **OSM Quick 3D** are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) · versioning: [SemVer](https://semver.org/).

## [0.1.0] - 2026-06-04

- Initial release.
- One-click OpenStreetMap download for the visible map area or the extent of selected features.
- Native QGIS memory layers, styled by function: buildings by OSM use, roads by highway class and width, water blue, greens green.
- Flat-roof 3D building massing via native QGIS 3D symbology; height from OSM (height, then floor count, then a default).
- Optional basemap layer moved underneath as the ground, in 2D and in the 3D Map View.
- Built for larger areas than a web 3D scene; the practical limit is the public Overpass API.
