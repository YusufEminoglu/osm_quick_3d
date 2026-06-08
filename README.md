# OSM Quick 3D

One-click OpenStreetMap into **native QGIS**: function-styled 2D layers, a flat-roof **3D building massing** model, and an optional basemap underlay. No browser, no web server — built for **larger areas** than a web 3D scene.

OSM Quick 3D is the native sibling of [`osm_3d_model`](https://github.com/YusufEminoglu/osm_3d_model) (the browser / Three.js viewer). Same OpenStreetMap logic, but the data lands directly in QGIS as plain layers you can analyse, edit and view in QGIS's own 3D Map View.

## What it does

1. Pick the **visible map area** (or the extent of your **selected features**) and a **study-area shape** — rectangle, rounded rectangle, circle or hexagon. Features are clipped to that shape.
2. It downloads OSM via Overpass — buildings, roads, cycleways, water, greens, trees, street furniture.
3. The layers are added to your project — inside one tidy **layer-tree group** — as native layers **already styled by function**:
   - buildings by OSM use (residential / commercial / industrial / civic / worship)
   - roads by `highway` class — colour and metric width, using OSM `width` when available
   - water blue, greens green
4. Buildings are **extruded** with native QGIS 3D symbology — a clean **flat-roof massing** model. Height comes from OSM: `coalesce("height", "building_levels" * 3, 9)` m. **Building colours are selectable** (and identical in 2D and 3D): by OSM function, or a soft height-graduated tint — **gray, warm, teal, salmon, purple or sand**. A **height-exaggeration** factor (0.5×–5.0×) makes low-rise districts read. No roofs, no animation.
5. Trees get a matching **3D pass** — simple green canopies on the ground — when 3D is on.
   - Each building also carries a computed **`footprint_m2`** and an estimated **`gfa_m2`** (footprint × floors) column, and the run reports the area totals — ready for quick planning quantities.
   - Optionally **label** buildings and roads by their OSM `name` (white halo).
6. An optional **ground base** — the study area buffered outward by 5 m, extruded as a recessed plinth from −2 m up to ground level — gives the city something to stand on in 3D.
7. An optional **basemap** layer is moved underneath to be the ground (in 2D and draped under the 3D terrain).
8. Optionally opens a native **3D Map View** for you. The **Theme & Style** dock controls the scene, including theme presets, 3D refresh/focus, resolution, and per-layer visibility in the 3D view without hiding those layers from the 2D map.

## Persistence & caching

- **Save to GeoPackage** (optional): every downloaded layer is written into one `.gpkg` and reloaded from it, so the result survives a project reload instead of vanishing as a memory layer.
- **Overpass disk cache** (on by default): responses are cached for a week, keyed by the exact query, so re-running on the same area opens instantly without hitting the rate-limited public API again.

## Install (development)

Set `QGIS_PLUGINPATH` to the monorepo root and restart QGIS, or install the packaged zip via *Plugins ▸ Install from ZIP*.

## Notes & limits

- The output is plain native layers, so QGIS handles **large areas** comfortably; the real bottleneck is the public **Overpass API** — big requests can be slow or hit element limits. The dialog clamps the requested area to a maximum about its centre.
- QGIS's 3D module is an optional build. If it is missing, the layers are still added in 2D (styled) and you get a hint to open a 3D Map View manually.

## License

MIT © Yusuf Eminoglu. Data © OpenStreetMap contributors.

[Changelog](CHANGELOG.md) · [PlanX monorepo](https://github.com/YusufEminoglu)
