# OSM Quick 3D

One-click OpenStreetMap into **native QGIS**: function-styled 2D layers, a flat-roof **3D building massing** model, and a basemap underlay. No browser, no web server — built for **larger areas** than a web 3D scene.

OSM Quick 3D is the native sibling of [`osm_3d_model`](https://github.com/YusufEminoglu/osm_3d_model) (the browser / Three.js viewer). Same OpenStreetMap logic, but the data lands directly in QGIS as plain layers you can analyse, edit and view in QGIS's own 3D Map View.

## What it does

1. Pick the **visible map area** (or the extent of your **selected features**).
2. It downloads OSM via Overpass — buildings, roads, cycleways, water, greens, trees, street furniture.
3. The layers are added to your project as **native memory layers, already styled by function**:
   - buildings by OSM use (residential / commercial / industrial / civic / worship)
   - roads by `highway` class — colour **and** width
   - water blue, greens green
4. Buildings are **extruded** with native QGIS 3D symbology — a clean **flat-roof massing** model. Height comes from OSM: `coalesce("height", "building_levels" * 3, 9)` m. No roofs, no animation.
5. An optional **basemap** layer is moved underneath to be the ground (in 2D and draped under the 3D terrain).
6. Optionally opens a **3D Map View** for you.

## Install (development)

Set `QGIS_PLUGINPATH` to the monorepo root and restart QGIS, or install the packaged zip via *Plugins ▸ Install from ZIP*.

## Notes & limits

- The output is plain native layers, so QGIS handles **large areas** comfortably; the real bottleneck is the public **Overpass API** — big requests can be slow or hit element limits. The dialog clamps the requested area to a maximum about its centre.
- QGIS's 3D module is an optional build. If it is missing, the layers are still added in 2D (styled) and you get a hint to open a 3D Map View manually.

## License

MIT © Yusuf Eminoglu. Data © OpenStreetMap contributors.

[Changelog](CHANGELOG.md) · [PlanX monorepo](https://github.com/YusufEminoglu)
