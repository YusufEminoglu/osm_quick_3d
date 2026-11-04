# OSM Quick 3D 0.52.0 — development set

Written 2026-11-04 by the master engineer from two independent audits of
0.51.1 (OQ-CODE: download, fidelity, saving, 3D; OQ-UX: first use, legibility,
truth, demand), both offscreen on QGIS 3.44.12 and 4.2.0 against fake servers
on 127.0.0.1 with zero public requests. Evidence: `oq3/oq-code`, `oq3/oq-ux`
in the session scratchpad. ~19.9k Hub downloads.

The styling and the 3D renderer work (16 rules, data-defined heights, terrain
clamping, styles stored in the GeoPackage). The download layer is the old one
that 3D OSM Model 1.10.0 replaced today, and it runs on the GUI thread.

## 1. Problems
| # | Harm | Where |
|---|------|-------|
| P1 | QGIS freezes during every request (QgsBlockingNetworkRequest on the GUI thread); Cancel is checked only between tiles and a late Cancel throws away a finished download. Worst case ~7 min frozen per tile. | `osm_download.py:334, 407, 422, 967` |
| P2 | Every HTTP status retried: 6 requests per tile (40 tiles → up to 240 per click), 429 retried after 1.2 s, no rests remembered; status never read (the error branch returns first). | `osm_download.py:343-375` |
| P3 | Empty answers cached for a week. | `:386` |
| P4 | No identification (QGIS stock User-Agent, no Referer; USER_AGENT says 0.50.0). kumi.systems listed (alias of private.coffee). | `:44` |
| P5 | Oversize answers downloaded in full (256 MiB before the 64 MiB check); no `[maxsize]`. | |
| P6 | Messages: raw Qt/JSON text, a hang reported as JSON error, `failures[-3:]` drops the first server; a modal critical box plus a 300-character status; jargon. | `main_plugin.py:687` |
| P7 | One absurd tag (levels=inf, 400-digit height) raises OverflowError and the whole download fails (and fails again from cache); levels=1e10 overflows the Int field and the memory provider drops every later building. | `osm_download.py:499/504` |
| P8 | Multipolygon: island inside a hole lost; 2-point outer ways dropped; open segments closed one by one. | `:660-688` |
| P9 | GeoPackage: the first layer uses CreateOrOverwriteFile and wiped a user table in the chosen file; re-running into the same file while layers are loaded fails ("already exists") and silently falls back to memory layers. | `main_plugin.py:326`, `osm_download.py:772` |
| P10 | Memory layers come back empty after reopening the project; no warning. | |
| P11 | Run, status and Cancel off-screen at 1366×728 (Run at y = 1066 in a 611 px viewport). | `dock.py:430-469` |
| P12 | Empty project: modal "Empty area" with no hint to "Add OSM basemap"; Run enabled on a Null extent. | |
| P13 | Shadow and ambient-occlusion toggles do nothing (methods absent on Qgs3DMapSettings in 3.44 and 4.2, calls inside suppress). | `native3d.py:825-841` |
| P14 | Tunnels and proposed/construction roads drawn as ordinary roads. | |
| P15 | Unload leaves the dock alive (extentsChanged connected, dock not deleted); processEvents lets the user act mid-run. | `dock.py:297`, `main_plugin.py:88-93` |
| P16 | Truth: tracker 404; README links (releases path, sibling link, 15 ecosystem links to this repo), "15 plugins", "Version 0.50", "three mirrors", "same download logic", Cycleways/Bike lanes, tree description, docs/index.html → missing file; metadata changelog gaps. | |
| P17 | Tests: the stubs make geometry checks vacuous; two tests wipe the user's real `%TEMP%\osm_quick_3d_cache`; nothing tests the network, saving, 3D or the dock. | `tests/test_pure_logic.py` |

## 2. Delivers
1. The sibling's polite, cancellable Overpass client, per tile: status read,
   rests remembered across sessions, at most one request per server per tile,
   the download stops early when every server is resting, `[timeout][maxsize]`,
   64 MiB cut, Referer, empty/remark never cached, plain messages naming the
   servers (P2-P6).
2. No freeze: the download runs in a QgsTask with a QgsFeedback; Cancel ends
   it within 2 s and never discards a finished result (P1, P15).
3. Faithful data: finite, capped tags; proper ring assembly; tunnels and
   unbuilt roads excluded (P7, P8, P14).
4. Saving that keeps work: never overwrite an existing GeoPackage file or
   other tables in it; re-runs write new layer names (or replace only this
   plugin's own layers) without falling back silently; a clear notice when
   results are temporary layers (P9, P10).
5. A dock that fits: Run/Cancel and status pinned below the scroll area,
   short messages without modal boxes, an empty-project hint (P6, P11, P12);
   remove or implement the dead 3D toggles (P13).
6. Honest docs and listing (P16); honest tests (P17).

## 3. Contracts
- C1 (OQ-NET provides, OQ-UX consumes): `osm_download.download_osm_for_area(
  ..., status=None, feedback=None)` keeps its signature plus these keyword
  arguments; `status(text)` is called on the worker thread with plain
  sentences; `feedback` (QgsFeedback) cancels at once and raises
  `OsmDownloadCancelled(OsmDownloadError)` ("Download cancelled."); the result
  carries `notes` (list of plain sentences, e.g. tiles missing) — the dock
  must say when the result is partial.
- C2 (OQ-NET): all QGIS layer construction that touches the project stays on
  the main thread; the worker only downloads and parses into plain Python
  data (or builds memory layers without adding them to the project).

## 4. Implementers
| Agent | Owns |
|-------|------|
| OQ-NET | `osm_download.py`, new `net/` (ported from osm_3d_model), `tests/test_pure_logic.py`, new `tests/_offline.py`, new `tests/test_overpass_rules_qgis.py`, new `tests/test_osm_fidelity_qgis.py` |
| OQ-UX | `dock.py`, `main_plugin.py`, `native3d.py`, `styling.py`, new `tests/test_dock_qgis.py`, new `tests/test_save_qgis.py` |
| OQ-DOCS | `README.md`, `CHANGELOG.md`, `CITATION.cff`, `metadata.txt`, `docs/**` except this file, `THIRD_PARTY_NOTICES.md` |

## 5. Rules
Offscreen; every QGIS run with a fresh `QGIS_CUSTOM_CONFIG_PATH`; zero public
network traffic (tests use `_offline.isolate_network()` and an isolated cache
folder); QGIS 3.28 / Python 3.9, both runtimes; Hub scan rules; red before
green; no git writes; no pf verify.

## 6. Status
- 2026-11-04: audits done; set written; implementers launched.
