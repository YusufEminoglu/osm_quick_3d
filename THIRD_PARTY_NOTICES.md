# Third-Party Notices

## OpenStreetMap data

OSM Quick 3D downloads and displays OpenStreetMap data. OpenStreetMap data is
© OpenStreetMap contributors and is available under the Open Database License
1.0 (ODbL).

- Attribution: [OpenStreetMap](https://www.openstreetmap.org/copyright)
- License: [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/)

When you retain or publish downloaded OSM layers, maps, screenshots, exports,
or derived databases, keep the attribution and comply with the ODbL as
applicable.

## OpenStreetMap tile service

The optional standard basemap uses the OpenStreetMap tile service. Keep the
visible attribution and follow the [tile usage policy](https://operations.openstreetmap.org/policies/tiles/).

## Overpass API

OSM downloads use public Overpass API servers. The endpoints are network
services and are not bundled with this plugin:

- <https://overpass-api.de/api/interpreter>
- <https://overpass.private.coffee/api/interpreter>

overpass-api.de is asked first until a server has delivered; after that, the
server that last delivered is asked first. overpass.kumi.systems is no longer
asked: it is the same machine as overpass.private.coffee.

The plugin follows the operators' 2026 rules. Every request carries the
`Referer` https://github.com/YusufEminoglu/osm_quick_3d, because QGIS replaces
a plugin's own User-Agent; where QGIS allows it (QGIS 4.2, not 3.44) the
User-Agent also ends with `OSM-Quick-3D-QGIS-Plugin`. Each server gets one
request per tile (on QGIS 4, one more after a dropped connection), and one
click sends at most 64 requests. A server that answers 429 rests for a
minute, one that refuses with 403 or 406 for an hour, a busy one (5xx) for
45 seconds and one that does not answer for 2 minutes, doubling while it
keeps failing; the rests are kept in the QGIS settings, so they hold across
QGIS sessions, and a download stops early when every server is resting.
Queries carry `[timeout:N][maxsize:134217728]`, a transfer lasts at most 55
seconds, answers are cut at 64 MiB, and only complete answers are stored on
this computer, for a week; empty answers and answers carrying a server remark
are never stored.
