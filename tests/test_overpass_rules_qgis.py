"""QGIS suite: the Overpass client against fake servers on 127.0.0.1 (P2-P6, C1, C2).

Everything goes through the real QgsBlockingNetworkRequest and the real
``osm_download`` code; only the two servers are fake.  QGIS's network
timeout is set to 4 s, so the transfer waits T = 4 - 2 = 2 s and every
request claims ``[timeout:10]`` (the formula, not a patched constant).  Each
download runs on one long-lived worker thread, as on the QgsTask thread the
plugin uses.  No public host is contacted: ``isolate_network()`` runs first,
the answer cache is a new empty folder, and the fake servers bind 127.0.0.1
only.

Every check names the 0.51.1 behaviour it rules out.  Run from the folder
that contains the plugin, with QGIS_CUSTOM_CONFIG_PATH pointing at a
throwaway folder:

    python-qgis-ltr.bat -m osm_quick_3d.tests.test_overpass_rules_qgis   # QGIS 3.44
    python-qgis.bat     -m osm_quick_3d.tests.test_overpass_rules_qgis   # QGIS 4.2
"""
from __future__ import annotations

import json
import os
import queue
import re
import sys
import tempfile
import threading
import time
import traceback
from typing import Dict, List, NamedTuple, Optional

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))

from qgis.core import (  # noqa: E402
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCoordinateTransformContext,
    QgsFeedback,
    QgsGeometry,
    QgsNetworkAccessManager,
    QgsPointXY,
    QgsSettings,
)
from qgis.PyQt.QtCore import QT_VERSION  # noqa: E402

QT6 = int(QT_VERSION) >= 0x060000
QGIS_TIMEOUT_MS = 4000
TRANSFER_S = 2
BBOX = (38.4200, 27.1400, 38.4290, 27.1515)   # min_lat, min_lon, max_lat, max_lon (Izmir), one part
BIG = (38.40, 27.10, 38.50, 27.21)            # about 107 km²: two parts
HUGE = (38.30, 27.00, 38.50, 27.25)           # about 480 km²: nine parts
MIB = 1024 * 1024
#: Qt's own words; none of them may reach the person reading a message.
QT_WORDS = ("Error transferring", "server replied", "Connection closed", "Operation canceled",
            "Connection refused", "Socket operation timed out", "Expecting value", "non-JSON")
REPO = "https://github.com/YusufEminoglu/osm_quick_3d"


# --------------------------------------------------------------- harness
class ShiftClock:
    """Real time plus an offset a test can advance."""

    def __init__(self, source) -> None:
        self.source = source
        self.offset = 0.0

    def __call__(self) -> float:
        return self.source() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += float(seconds)


class Worker:
    """One long-lived worker thread, like the task thread a download runs on."""

    def __init__(self) -> None:
        self.jobs: "queue.Queue" = queue.Queue()
        threading.Thread(target=self._loop, name="oq3d-test-worker", daemon=True).start()

    def _loop(self) -> None:
        while True:
            job, box = self.jobs.get()
            try:
                box["result"] = job()
            except BaseException as error:  # noqa: BLE001 - handed back to the check
                box["error"] = error
            box["done"] = time.monotonic()

    def call(self, job, limit: float = 120.0, on_tick=None) -> Dict:
        box: Dict = {}
        started = time.monotonic()
        self.jobs.put((job, box))
        while "done" not in box and time.monotonic() - started < limit:
            QgsApplication.processEvents()
            if on_tick is not None:
                on_tick(time.monotonic() - started)
            time.sleep(0.01)
        box["started"] = started
        return box


class Run(NamedTuple):
    payload: Optional[dict]
    error: Optional[BaseException]
    status: List[str]
    elapsed: float
    hits: List[Dict]
    finished: bool
    done_at: Optional[float]

    @property
    def message(self) -> str:
        return str(self.error) if self.error is not None else ""

    @property
    def notes(self) -> List[str]:
        if not isinstance(self.payload, dict):
            return []
        info = self.payload.get("_download") or {}
        return list(info.get("notes") or [])

    def count(self, name: str) -> int:
        return len([hit for hit in self.hits if hit["host"] == name])


class Checks:
    def __init__(self) -> None:
        self.results: List[bool] = []

    def ok(self, name: str, condition, detail: str = "") -> bool:
        tag = "PASS" if condition else "FAIL"
        print(f"  [{tag}] {name}" + (f" - {detail}" if detail else ""), flush=True)
        self.results.append(bool(condition))
        return bool(condition)

    @property
    def passed(self) -> int:
        return sum(1 for result in self.results if result)


def od():
    from osm_quick_3d import osm_download

    return osm_download


def click(worker: Worker, servers, *, use_cache: bool = False, feedback=None, cancel_check=None,
          legacy: bool = False, bbox=BBOX, limit: float = 120.0, on_tick=None) -> Run:
    """One download, as a click in the dock makes it, on the worker thread.

    The 0.52 API when the module has it; the 0.51 `fetch_overpass`
    otherwise (or with `legacy`), so every check also runs against 0.51.1.
    """
    module = od()
    lines: List[str] = []
    servers.take_hits()

    def job():
        if not legacy and hasattr(module, "fetch_overpass_ex"):
            return module.fetch_overpass_ex(*bbox, use_cache=use_cache, status=lines.append,
                                            feedback=feedback)
        kwargs = {"use_cache": use_cache, "feedback": lines.append}
        if cancel_check is not None:
            kwargs["cancel_check"] = cancel_check
        return module.fetch_overpass(*bbox, **kwargs)

    box = worker.call(job, limit, on_tick)
    finished = "done" in box
    elapsed = box.get("done", time.monotonic()) - box["started"]
    time.sleep(0.05)   # a re-sent request still in flight lands in this run's hits
    return Run(box.get("result"), box.get("error"), list(lines), elapsed, servers.take_hits(),
               finished, box.get("done"))


def fresh(servers, **modes) -> None:
    """A clean start for one check: no rests, no saved answers, new scripts."""
    from osm_quick_3d.tests._offline import forget_servers

    forget_servers()
    od().clear_cache()
    for name, mode in modes.items():
        if isinstance(mode, (tuple, list)):
            servers.set(name, *mode)
        else:
            servers.set(name, mode)
    servers.take_hits()


def plain_message(text: str) -> List[str]:
    """What is wrong with a closing message: jargon or Qt's own words."""
    jargon = ("mirror", "pinned", "backend", "endpoint", "remark", "tile")
    lowered = text.casefold()
    return [word for word in jargon if word in lowered] + [word for word in QT_WORDS if word in text]


_ELAPSED = re.compile(r"\b\d+(?:\.\d+)?\s?s\b|elapsed", re.IGNORECASE)


def remaining(host: str) -> float:
    board = getattr(od(), "BOARD", None)
    return board.remaining(host) if board is not None else 0.0


def attempts(run: Run, name: str, gap: float = 0.5) -> int:
    """The plugin's own requests to one server: wire requests closer than
    `gap` belong to one send (Qt 5 re-sends a dropped POST by itself)."""
    times = sorted(hit["t"] for hit in run.hits if hit["host"] == name)
    if not times:
        return 0
    return 1 + sum(1 for earlier, later in zip(times, times[1:]) if later - earlier > gap)


def cache_files() -> List[str]:
    module = od()
    folder = module._cache_dir()
    return sorted(name for name in os.listdir(folder) if name.endswith(".json"))


# --------------------------------------------------------------- checks
def check_contract(c: Checks) -> None:
    """C1: the names the dock relies on."""
    import inspect

    module = od()
    c.ok("servers: overpass-api.de then private.coffee, one per operator "
         "(0.51.1 also asked the kumi.systems alias of private.coffee)",
         _DEFAULT_ENDPOINTS == ("https://overpass-api.de/api/interpreter",
                                "https://overpass.private.coffee/api/interpreter"),
         repr(_DEFAULT_ENDPOINTS))
    cancelled = getattr(module, "OsmDownloadCancelled", None)
    c.ok("C1: OsmDownloadCancelled is an OsmDownloadError saying 'Download cancelled.' (missing in 0.51.1)",
         cancelled is not None and issubclass(cancelled, module.OsmDownloadError)
         and str(cancelled()) == "Download cancelled.")
    params = inspect.signature(module.download_osm_for_area).parameters
    names = list(params)
    c.ok("C1: download_osm_for_area keeps area_utm, epsg_dest, feedback, use_cache, datasets, "
         "cancel_check and adds status=None (missing in 0.51.1)",
         names[:6] == ["area_utm", "epsg_dest", "feedback", "use_cache", "datasets", "cancel_check"]
         and "status" in params and params["status"].default is None, str(names))
    legacy = list(inspect.signature(module.fetch_overpass).parameters)
    c.ok("the 0.51 fetch_overpass signature is kept", legacy == [
        "min_lat", "min_lon", "max_lat", "max_lon", "timeout_s", "feedback", "use_cache",
        "datasets", "cancel_check"], str(legacy))


def check_rate_limit(c: Checks, worker: Worker, servers) -> None:
    a, b = servers.host("a"), servers.host("b")
    fresh(servers, a="h429", b="h429")
    run = click(worker, servers)
    c.ok("429 on both servers: one request each, no retry on the same server "
         "(0.51.1: two each, 1.2 s apart)", run.count("a") == 1 and run.count("b") == 1,
         f"a={run.count('a')} b={run.count('b')}")
    c.ok("429: each server rests 60 s (0.51.1 kept no rest)",
         55 <= remaining(a) <= 60 and 55 <= remaining(b) <= 60,
         f"a rests {remaining(a):.0f} s, b {remaining(b):.0f} s")
    c.ok("429: the message says the servers asked to slow down and when to try again",
         run.message.startswith("The OpenStreetMap servers asked this computer to slow down.")
         and "Wait about a minute before trying again." in run.message, run.message[:160])
    c.ok("429: the message names every server asked and what it said "
         "(0.51.1 kept the last three failures and dropped the first server)",
         f"{a}, {b}: asked this computer to slow down (HTTP 429)" in run.message, run.message[-120:])
    c.ok("429: no jargon and no raw Qt text in the message (0.51.1: 'Error transferring ... server replied')",
         run.error is not None and not plain_message(run.message), str(plain_message(run.message)))
    c.ok("429: the status names the server, its answer, its rest and the next server",
         len(run.status) >= 2 and run.status[0] == f"Asking {a}…"
         and run.status[1] == (f"{a} asked this computer to slow down (HTTP 429), so it rests "
                               f"for a minute. Asking {b}…"), " | ".join(run.status))
    again = click(worker, servers)
    c.ok("429: a second click inside the rest sends nothing and fails at once "
         "(0.51.1 asked every server twice again)", not again.hits and again.elapsed < 1.5,
         f"{len(again.hits)} requests in {again.elapsed:.1f} s")
    c.ok("429: the second click's message says why each server was not asked",
         f"{a}: not asked, because it asked this computer to slow down (HTTP 429)" in again.message
         and "Wait about a minute" in again.message, again.message[-200:])


def check_refused(c: Checks, worker: Worker, servers) -> None:
    a, b = servers.host("a"), servers.host("b")
    fresh(servers, a="h406", b="h406")
    run = click(worker, servers)
    c.ok("406 on both: one request each, each server rests an hour (0.51.1: two each, no rest)",
         run.count("a") == 1 and run.count("b") == 1 and remaining(a) > 3500 and remaining(b) > 3500,
         f"a={run.count('a')} b={run.count('b')} rest {remaining(a):.0f} s")
    c.ok("406: the message says the servers refused this computer and to try again in about an hour",
         run.message.startswith("The OpenStreetMap servers refused this computer.")
         and "Try again in about an hour." in run.message
         and f"{a}, {b}: refused this computer (HTTP 406)" in run.message, run.message[:220])
    fresh(servers, a="h403", b="ok")
    run = click(worker, servers)
    c.ok("403 on the first server: the second delivers, the first rests an hour "
         "(0.51.1 asked the first twice)", isinstance(run.payload, dict) and run.count("a") == 1
         and run.count("b") == 1 and remaining(a) > 3500,
         f"a={run.count('a')} b={run.count('b')} rest {remaining(a):.0f} s")
    c.ok("403: the status tells the person what happened before asking the next server",
         any(line == f"{a} refused this computer (HTTP 403), so it rests for an hour. Asking {b}…"
             for line in run.status), " | ".join(run.status))


def check_invalid(c: Checks, worker: Worker, servers) -> None:
    a, b = servers.host("a"), servers.host("b")
    fresh(servers, a="h400", b="ok")
    run = click(worker, servers)
    c.ok("400: the request is wrong on every server, so only one request is sent "
         "(0.51.1: two to each server)", run.count("a") == 1 and run.count("b") == 0,
         f"a={run.count('a')} b={run.count('b')}")
    c.ok("400: the message says the request was rejected, where to report it, and that the "
         "other server was not asked",
         run.message.startswith("The OpenStreetMap server rejected the download request as invalid.")
         and f"{REPO}/issues" in run.message
         and f"{a}: rejected the request as invalid (HTTP 400); {b}: not asked" in run.message,
         run.message[:240])


def check_busy(c: Checks, worker: Worker, servers) -> None:
    a, b = servers.host("a"), servers.host("b")
    fresh(servers, a="h502", b="ok")
    run = click(worker, servers, use_cache=True)
    c.ok("502 on the first server: the second delivers; the first rests 45 s "
         "(0.51.1 asked the first twice)", isinstance(run.payload, dict) and run.count("a") == 1
         and run.count("b") == 1 and 40 <= remaining(a) <= 45,
         f"a={run.count('a')} b={run.count('b')} rest {remaining(a):.0f} s")
    saved = cache_files()
    again = click(worker, servers, use_cache=True)
    c.ok("cache: a good answer is kept and the next click sends nothing (kept)", len(saved) == 1
         and not again.hits and isinstance(again.payload, dict),
         f"files={len(saved)} requests={len(again.hits)}")
    c.ok("cache: the hit says how old the copy is (0.51.1: 'Using cached OSM tile (no download) ...')",
         again.status[:1] == ["Using the copy downloaded less than a minute ago; no request sent."],
         str(again.status))
    fresh(servers, a="h504", b="h504")
    run = click(worker, servers)
    c.ok("504 on both: one request each, 45 s rests, 'busy' advice (0.51.1: two each)",
         run.count("a") == 1 and run.count("b") == 1 and remaining(b) > 40
         and run.message.startswith("The OpenStreetMap servers are busy.")
         and "Try again in about a minute." in run.message
         and f"{a}, {b}: was busy (HTTP 504)" in run.message, run.message[:200])


def check_hang(c: Checks, worker: Worker, servers) -> None:
    a, b = servers.host("a"), servers.host("b")
    fresh(servers, a="hang", b="hang")
    run = click(worker, servers)
    c.ok("hang on both: one request each, each cut at T = 2 s", run.count("a") == 1
         and run.count("b") == 1 and run.elapsed < 7,
         f"a={run.count('a')} b={run.count('b')} in {run.elapsed:.1f} s")
    c.ok("hang: each server rests 2 minutes (0.51.1 kept no rest)",
         110 <= remaining(a) <= 120 and 110 <= remaining(b) <= 120, f"{remaining(a):.0f} s")
    c.ok("hang: the message advises later or a smaller area and names the wait "
         "(0.51.1: 'Expecting value: line 1 column 1 (char 0)')",
         run.message.startswith("The OpenStreetMap servers did not deliver this area in time.")
         and "Try again in about 2 minutes, or choose a smaller area." in run.message
         and f"{a}, {b}: did not answer within {TRANSFER_S} seconds" in run.message
         and not plain_message(run.message), run.message[:220])
    queries = [hit["query"] for hit in run.hits]
    c.ok("the request claims [timeout:10][maxsize:134217728] under a 4 s QGIS timeout "
         "(0.51.1: [timeout:60] and no maxsize)",
         queries and all(q.startswith("[out:json][timeout:10][maxsize:134217728];") for q in queries),
         queries[0][:60] if queries else "no request")


def check_dropped(c: Checks, worker: Worker, servers) -> None:
    a, b = servers.host("a"), servers.host("b")
    fresh(servers, a="drop", b="drop")
    run = click(worker, servers)
    reasks = [line for line in run.status if "closed the connection; asking it once more" in line]
    counts = f"wire a={run.count('a')} b={run.count('b')}, sends a={attempts(run, 'a')} b={attempts(run, 'b')}"
    if QT6:
        c.ok("dropped connection, Qt 6: each server is asked once more after 5 s and no more "
             "(0.51.1: once more after 1.2 s)",
             run.count("a") == 2 and run.count("b") == 2 and len(reasks) == 2 and run.elapsed >= 10,
             f"{counts}, re-asks={len(reasks)} in {run.elapsed:.1f} s")
    else:
        c.ok("dropped connection, Qt 5: the plugin sends once per server and adds no re-ask "
             "(Qt re-sends by itself; 0.51.1 sent twice per server, 1.2 s apart)",
             attempts(run, "a") == 1 and attempts(run, "b") == 1 and not reasks
             and 1 <= run.count("a") <= 4 and 1 <= run.count("b") <= 4 and run.elapsed < 4,
             f"{counts}, plugin re-asks={len(reasks)} in {run.elapsed:.1f} s")
    c.ok("dropped connection: each server rests 2 minutes and the message points at the "
         "connection and the proxy",
         remaining(a) > 110 and run.message.startswith("No OpenStreetMap server could be reached.")
         and "proxy settings" in run.message
         and f"{a}, {b}: closed the connection" in run.message and not plain_message(run.message),
         run.message[:200])


def check_oversize(c: Checks, worker: Worker, servers) -> None:
    a, b = servers.host("a"), servers.host("b")
    fresh(servers, a="oversize", b="ok")
    run = click(worker, servers)
    sent = max((hit.get("sent", 0) for hit in run.hits if hit["host"] == "a"), default=0)
    c.ok("oversize: the answer is cut once it passes 64 MiB, never downloaded in full "
         "(0.51.1 read all of it first)", 64 * MIB <= sent < 120 * MIB,
         f"server sent {sent / MIB:.0f} MiB of 128")
    c.ok("oversize: the other server is not asked for the same too-big area, and the advice is a "
         "smaller area (0.51.1 asked every server)", run.count("a") == 1 and run.count("b") == 0
         and run.message.startswith("This area holds too much OpenStreetMap data for one download.")
         and f"{a}: sent more than 64 MB; {b}: not asked" in run.message, run.message[:220])
    board = getattr(od(), "BOARD", None)
    last = board.last_answer(a) if board is not None else None
    c.ok("oversize: remembered as too big but costs the server no rest",
         last is not None and last[0] == "too_large" and remaining(a) == 0.0,
         f"last answer={last} rest={remaining(a):.0f} s")


def check_empty_and_remark(c: Checks, worker: Worker, servers) -> None:
    a = servers.host("a")
    fresh(servers, a="empty", b="ok")
    run = click(worker, servers, use_cache=True)
    c.ok("empty answer: used once (0 objects, from the first server) and never cached "
         "(0.51.1 kept it for a week)",
         isinstance(run.payload, dict) and run.payload.get("elements") == [] and run.count("a") == 1
         and cache_files() == [],
         f"a={run.count('a')} b={run.count('b')} cache={cache_files()} error={run.message[:80]!r}")
    again = click(worker, servers, use_cache=True)
    c.ok("empty answer: the next click asks again instead of replaying it "
         "(0.51.1 sent nothing and returned the empty copy)", again.count("a") == 1,
         f"requests={len(again.hits)}")
    fresh(servers, a="remark", b="ok")
    run = click(worker, servers, use_cache=True)
    c.ok("remark on the first server: the second delivers; the first rests 30 s",
         isinstance(run.payload, dict) and not run.payload.get("remark") and run.count("a") == 1
         and run.count("b") == 1 and 25 <= remaining(a) <= 30,
         f"a={run.count('a')} b={run.count('b')} rest {remaining(a):.0f} s")
    kept = []
    for name in cache_files():
        with open(os.path.join(od()._cache_dir(), name), encoding="utf-8") as handle:
            kept.append(json.load(handle))
    c.ok("remark: the incomplete answer is never cached; the complete one is",
         len(kept) == 1 and not kept[0].get("remark"), f"{len(kept)} kept")


def check_identity(c: Checks, worker: Worker, servers) -> None:
    fresh(servers, a="ok")
    run = click(worker, servers)
    c.ok(f"every request carries the Referer {REPO} (0.51.1 sent none)",
         run.hits and all(hit["referer"] == REPO for hit in run.hits),
         run.hits[0]["referer"] if run.hits else "no request")
    ua = run.hits[0]["ua"] if run.hits else ""
    if QT6:
        c.ok("QGIS 4.2 appends OSM-Quick-3D to its User-Agent (0.51.1's own header was replaced)",
             "OSM-Quick-3D-QGIS-Plugin/" in ua and REPO in ua and "QGIS/" in ua, ua)
    else:
        c.ok("QGIS 3.44 cannot append a suffix; its stock User-Agent arrives and the Referer "
             "identifies the plugin", "QGIS/" in ua, ua)
    c.ok("the status ends with the server that delivered",
         run.status[-1:] == [f"{servers.host('a')} delivered the data."], " | ".join(run.status))
    elapsed = [line for line in run.status if _ELAPSED.search(line)]
    c.ok("status sentences carry no elapsed seconds", not elapsed, str(elapsed))


def check_parts(c: Checks, worker: Worker, servers) -> None:
    """A large area in parts: stop early, the request budget, gaps in notes."""
    module = od()
    a, b = servers.host("a"), servers.host("b")
    parts = len(module.tile_bbox(*HUGE))
    fresh(servers, a="h429", b="h429")
    run = click(worker, servers, bbox=HUGE)
    c.ok(f"429 everywhere on a {parts}-part area: two requests in all, then the click stops "
         f"because every server rests (0.51.1: 6 per part, up to {6 * parts})",
         len(run.hits) == 2 and run.error is not None, f"{len(run.hits)} requests")
    c.ok("the closing message names both servers in plain words",
         f"{a}, {b}: asked this computer to slow down (HTTP 429)" in run.message
         and not plain_message(run.message), run.message[:200])

    fresh(servers, a=("ok", "h502"), b="h502")
    run = click(worker, servers, bbox=BIG)
    info = (run.payload or {}).get("_download", {}) if isinstance(run.payload, dict) else {}
    c.ok("2 parts, the second busy everywhere: part 1 arrives, part 2 is reported missing "
         "(3 requests; 0.51.1: 5)", len(run.hits) == 3 and info.get("tiles_failed") == (2,)
         and info.get("tiles_succeeded") == 1, f"{len(run.hits)} requests, {info}")
    c.ok("the gap is a plain note naming every server (0.51.1: 'Missing tiles: 2' in a status line)",
         len(run.notes) == 1 and run.notes[0].startswith("1 of 2 parts of the area could not be downloaded")
         and a in run.notes[0] and b in run.notes[0] and not plain_message(run.notes[0]),
         run.notes[0][:200] if run.notes else "no note")

    fresh(servers, a="ok", b="ok")
    saved_limit = module.MAX_CLICK_REQUESTS if hasattr(module, "MAX_CLICK_REQUESTS") else None
    if saved_limit is not None:
        module.MAX_CLICK_REQUESTS = 3
    try:
        run = click(worker, servers, bbox=HUGE)
    finally:
        if saved_limit is not None:
            module.MAX_CLICK_REQUESTS = saved_limit
    c.ok("a click spends at most its request budget (here 3 of 64) and says the rest is missing "
         "(0.51.1 had no budget)", saved_limit == 64 and len(run.hits) == 3
         and any("all 3 of its requests" in note for note in run.notes),
         f"budget={saved_limit} requests={len(run.hits)} notes={run.notes}")


def check_timeouts_stop(c: Checks, worker: Worker, servers) -> None:
    """Every server runs out of time on every part: the click must stop soon,
    not wait out each 30 s rest and ask again for every part."""
    module = od()
    a, b = servers.host("a"), servers.host("b")
    parts = len(module.tile_bbox(*HUGE))
    try:
        from osm_quick_3d.net.hosts import HostBoard
    except ImportError:
        c.ok("server timeouts everywhere stop the click", False, "no net/hosts.py")
        return
    mono, wall = ShiftClock(time.monotonic), ShiftClock(time.time)
    original_board, original_wait = module.BOARD, module._cancellable_wait
    module.BOARD = HostBoard(clock=mono, wall=wall)
    waited: List[float] = []

    def fake_wait(seconds, _feedback=None):
        # The fake clock jumps over every wait, so a click that keeps waiting
        # out rests shows all its requests in moments.
        waited.append(float(seconds))
        mono.advance(seconds)
        wall.advance(seconds)

    module._cancellable_wait = fake_wait
    try:
        fresh(servers, a="remark", b="remark")
        run = click(worker, servers, bbox=HUGE)
    finally:
        module.BOARD, module._cancellable_wait = original_board, original_wait
    c.ok(f"'Query timed out' on every server, {parts} parts: at most 2 parts' worth of requests, "
         f"then the click stops (before this fix: {2 * parts} requests, waiting out each 30 s rest)",
         run.finished and len(run.hits) <= 4, f"{len(run.hits)} requests, waited {sum(waited):.0f} s")
    c.ok("server timeouts everywhere: one plain message naming both servers",
         run.error is not None
         and run.message.startswith("The OpenStreetMap servers did not deliver this area in time.")
         and f"{a}, {b}: ran out of time on this query" in run.message
         and not plain_message(run.message), run.message[:220])


def check_sessions(c: Checks, worker: Worker, servers) -> None:
    module = od()
    a, b = servers.host("a"), servers.host("b")
    try:
        from osm_quick_3d.net.hosts import HostBoard
    except ImportError:
        c.ok("rests are kept across QGIS sessions (0.51.1 kept nothing)", False, "no net/hosts.py")
        return
    mono, wall = ShiftClock(time.monotonic), ShiftClock(time.time)
    original = module.BOARD
    module.BOARD = HostBoard(clock=mono, wall=wall)
    try:
        fresh(servers, a="h429", b="ok")
        click(worker, servers)
        settings = QgsSettings()
        stored = settings.value("osm_quick_3d/hosts/board", "", type=str)
        last = settings.value("osm_quick_3d/hosts/last_worked", "", type=str)
        board = json.loads(stored) if stored else {}
        rest_until = (board.get(a) or {}).get("rest_until", 0)
        c.ok("the rest and the last server that worked are kept in QgsSettings under "
             "osm_quick_3d/hosts/ (0.51.1 kept nothing)",
             last == b and 55 <= rest_until - wall() <= 60,
             f"last_worked={last!r} rest_until-now={rest_until - wall():.0f}" if rest_until
             else f"stored={stored[:80]!r}")
        module.BOARD = HostBoard(clock=mono, wall=wall)       # QGIS restarted
        servers.set("a", "ok")
        servers.set("b", "ok")
        module.clear_cache()
        run = click(worker, servers)
        c.ok("after a restart the resting server is still not asked and the one that worked "
             "is asked first", run.count("a") == 0 and run.count("b") == 1,
             f"a={run.count('a')} b={run.count('b')}")
    finally:
        module.BOARD = original


def check_area_worker(c: Checks, worker: Worker, servers) -> None:
    """C1 + C2 end to end: download_osm_for_area on the worker thread."""
    from osm_quick_3d.tests import _offline

    module = od()
    fresh(servers, a=("ok", "h502"), b="h502")
    wgs = QgsCoordinateReferenceSystem.fromEpsgId(4326)
    utm = QgsCoordinateReferenceSystem.fromEpsgId(32635)
    to_utm = QgsCoordinateTransform(wgs, utm, QgsCoordinateTransformContext())
    corners = [to_utm.transform(QgsPointXY(lon, lat)) for lat, lon in ((BIG[0], BIG[1]), (BIG[2], BIG[3]))]
    from qgis.core import QgsRectangle

    area = QgsGeometry.fromRect(QgsRectangle(corners[0], corners[1]))     # two parts
    project_calls = []
    real_project = module.QgsProject

    class _ProjectSpy:
        """Records any project access made while the worker runs (C2)."""

        @staticmethod
        def instance():
            if threading.current_thread() is not threading.main_thread():
                project_calls.append(threading.current_thread().name)
            return real_project.instance()

    module.QgsProject = _ProjectSpy
    lines: List[str] = []
    feedback = QgsFeedback()
    try:
        def job():
            # 0.51.1 has no status keyword: a TypeError here is a failed check.
            return module.download_osm_for_area(area, 32635, use_cache=False, status=lines.append,
                                                feedback=feedback)

        box = worker.call(job, 60.0)
    finally:
        module.QgsProject = real_project
    result, error = box.get("result"), box.get("error")
    c.ok("download_osm_for_area runs on a worker thread with status= and a QgsFeedback (C1)",
         isinstance(result, dict) and error is None, repr(error)[:160])
    if not isinstance(result, dict):
        return
    c.ok("C2: the worker never touches QgsProject (0.51.1 read QgsProject.instance() there)",
         not project_calls, f"{len(project_calls)} project calls off the main thread")
    buildings = result.get("buildings")
    main = QgsApplication.instance().thread()
    c.ok("C2: the memory layers are handed to the main thread, not added to a project",
         buildings is not None and buildings.thread() == main
         and buildings.id() not in real_project.instance().mapLayers(),
         f"layer thread is main: {buildings is not None and buildings.thread() == main}")
    notes = result.get("notes") or []
    c.ok("C1: the result carries notes; a missing part is said in plain words "
         "(0.51.1 had no notes)", len(notes) == 1 and "1 of 2 parts" in notes[0]
         and not plain_message(notes[0]), str(notes)[:200])
    c.ok("status sentences arrive on the worker thread as plain words",
         lines and not any(plain_message(line) for line in lines), " | ".join(lines)[:200])
    _offline.forget_servers()


def check_cancel(c: Checks, worker: Worker, servers) -> None:
    """Last: on 0.51.1 a cancel cannot stop a transfer, and its worker stays blocked."""
    module = od()
    a = servers.host("a")
    cancelled = getattr(module, "OsmDownloadCancelled", None) or module.OsmDownloadError
    QgsNetworkAccessManager.setTimeout(60000)        # T = 55 s: only Cancel can end it soon
    try:
        for label, legacy in (("QgsFeedback (C1)", False), ("the 0.51 cancel_check", True)):
            fresh(servers, a="hang", b="hang")
            feedback = QgsFeedback()
            stop = {"flag": False}
            marks: Dict[str, float] = {}

            def tick(seconds: float) -> None:
                if seconds > 0.6 and "cancel" not in marks:
                    marks["cancel"] = time.monotonic()
                    stop["flag"] = True
                    feedback.cancel()

            run = click(worker, servers, feedback=feedback, cancel_check=lambda: stop["flag"],
                        legacy=legacy, limit=12.0, on_tick=tick)
            took = (run.done_at - marks["cancel"]) if run.finished and "cancel" in marks else None
            c.ok(f"Cancel through {label} ends a hung transfer within 2 s with 'Download cancelled.' "
                 "(0.51.1 ran on until the transfer ended)",
                 took is not None and took < 2.0 and isinstance(run.error, cancelled)
                 and str(run.error) == "Download cancelled.",
                 f"finished={run.finished} took={None if took is None else round(took, 2)} s "
                 f"error={run.message!r}")
            c.ok(f"Cancel through {label}: the server earns no rest and the other is not asked",
                 run.finished and run.count("b") == 0 and remaining(a) == 0.0,
                 f"b={run.count('b')} rest={remaining(a):.0f} s")
            if not run.finished:
                return      # the worker is still blocked in the transfer
    finally:
        QgsNetworkAccessManager.setTimeout(QGIS_TIMEOUT_MS)


_DEFAULT_ENDPOINTS = ()


# ------------------------------------------------------------------ main
def run_all() -> bool:
    global _DEFAULT_ENDPOINTS
    from osm_quick_3d import osm_download
    from osm_quick_3d.tests._offline import (FakeOverpass, allow_loopback_http, cleanup,
                                             isolate_network, refused)

    _DEFAULT_ENDPOINTS = tuple(osm_download.OVERPASS_ENDPOINTS)
    isolate_network()
    print("=" * 62)
    print(" osm_quick_3d - Overpass rules against fake servers on 127.0.0.1")
    print("=" * 62, flush=True)
    if allow_loopback_http(osm_download):
        print("  (a build without loopback support: plain HTTP to 127.0.0.1 is let through)")
    c = Checks()
    QgsNetworkAccessManager.setTimeout(QGIS_TIMEOUT_MS)
    servers = FakeOverpass()
    worker = Worker()
    osm_download.OVERPASS_ENDPOINTS = (servers.url("a"), servers.url("b"))
    started = time.monotonic()
    steps = (check_contract, check_rate_limit, check_refused, check_invalid, check_busy,
             check_hang, check_dropped, check_oversize, check_empty_and_remark,
             check_identity, check_parts, check_timeouts_stop, check_sessions, check_area_worker,
             check_cancel)
    for step in steps:
        step_started = time.monotonic()
        try:
            if step is check_contract:
                step(c)
            else:
                step(c, worker, servers)
        except Exception as error:  # noqa: BLE001 - a crashed step is a failed step
            traceback.print_exc()
            c.ok(f"{step.__name__} ran to the end", False, f"{type(error).__name__}: {error}")
        print(f"  ({step.__name__}: {time.monotonic() - step_started:.1f} s)", flush=True)
    c.ok("no request left this computer (guard)", not refused(), str(refused()[:3]))
    cleanup()
    print("-" * 62)
    print(f"  {c.passed}/{len(c.results)} passed in {time.monotonic() - started:.0f} s")
    return c.passed == len(c.results)


def main() -> None:
    profile = tempfile.TemporaryDirectory(prefix="osm_quick_3d-rules-")
    app = QgsApplication([], False, profile.name, "external")
    app.initQgis()
    ok = False
    try:
        ok = run_all()
    except Exception as error:  # noqa: BLE001 - a crash is a failed suite
        traceback.print_exc()
        print(f"  [FAIL] {type(error).__name__}: {error}")
    sys.stdout.flush()
    sys.stderr.flush()
    import shutil

    shutil.rmtree(profile.name, ignore_errors=True)     # os._exit skips its cleanup
    # Exit from the frame that holds the application; a worker still blocked
    # in a 0.51.1 transfer cannot keep the process alive.
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    main()
