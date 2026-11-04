"""Keep a QGIS test suite off every public host, and off the user's desktop.

Every QGIS suite calls :func:`isolate_network` before it runs anything.  It
points each host the plugin asks at a closed port on this computer -- the
Overpass servers (``osm_download.OVERPASS_ENDPOINTS``, read at call time)
and the OpenStreetMap tiles of "Add OSM basemap" -- and then guards QGIS's
network access itself: a request for any host other than this computer is
turned into a request for that closed port, recorded, and reported with a
``[FAIL]`` line.  A host the plugin gains later therefore cannot reach the
internet from a test either.  A suite then points the hosts it exercises at
its own fake servers on 127.0.0.1 (:class:`FakeOverpass`).

It also stops a suite from opening the user's browser or file manager
(``webbrowser.open`` and ``QDesktopServices.openUrl`` only record what they
were asked to open, :func:`opened`), points the Overpass answer cache at a
new empty folder (:func:`isolate_download_cache`), so the user's real cache
in the system temp folder is never read, written or cleared, and forgets the
servers' rests between checks (:func:`forget_servers`; QgsSettings must live
in a throwaway QGIS_CUSTOM_CONFIG_PATH).

Ported from 3D OSM Model 1.10.0 ``tests/_offline.py``.
"""
from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
import urllib.parse
from typing import Dict, List, Optional
from urllib.parse import urlsplit

#: Nothing listens on the discard port: a request there is refused locally.
CLOSED = "http://127.0.0.1:9"
#: "Add OSM basemap" in a suite: tiles from the closed port, never from
#: tile.openstreetmap.org.
CLOSED_TILES_URI = (
    "type=xyz&url=http://127.0.0.1:9/%7Bz%7D/%7Bx%7D/%7By%7D.png"
    "&zmax=19&zmin=0&crs=EPSG3857"
)
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_GUARDED_SCHEMES = frozenset({"http", "https", "ftp", "ws", "wss"})
_LOCK = threading.Lock()
_REFUSED: List[str] = []
_OPENED: List[str] = []
_GUARD: List[object] = []
_CACHE_DIRS: List[str] = []


def refused() -> List[str]:
    """Every public URL the guard has refused in this process, in order."""
    with _LOCK:
        return list(_REFUSED)


def opened() -> List[str]:
    """Every URL or folder a suite asked the browser or file manager to open."""
    with _LOCK:
        return list(_OPENED)


def _guard(request) -> None:
    """QGIS request preprocessor: no request leaves this computer."""
    from qgis.PyQt.QtCore import QUrl

    url = request.url()
    host = str(url.host()).lower()
    if str(url.scheme()).lower() not in _GUARDED_SCHEMES or host in _LOCAL_HOSTS:
        return
    text = str(url.toString())
    with _LOCK:
        _REFUSED.append(text)
    request.setUrl(QUrl(f"{CLOSED}/refused-by-the-offline-guard"))
    print(f"  [FAIL] offline guard refused a request for {host}: {text[:160]}", flush=True)


def _record_open(target, *_args, **_kwargs) -> bool:
    text = target.toString() if hasattr(target, "toString") else str(target)
    with _LOCK:
        _OPENED.append(str(text))
    return True


def block_desktop() -> None:
    """Record, instead of performing, every browser and file-manager open."""
    import webbrowser

    from qgis.PyQt.QtGui import QDesktopServices

    webbrowser.open = _record_open
    webbrowser.open_new = _record_open
    webbrowser.open_new_tab = _record_open
    QDesktopServices.openUrl = staticmethod(_record_open)


def isolate_network(guard_qgis: bool = True) -> None:
    """Point every plugin host at a closed local port and guard the rest.

    `guard_qgis` installs the request guard, which covers every request this
    QGIS makes; a suite passes False when it runs inside a QGIS session it
    did not start, whose own network must keep working.  Calling it again is
    harmless: the guard is installed once.
    """
    from qgis.core import QgsNetworkAccessManager

    from osm_quick_3d import osm_download

    osm_download.OVERPASS_ENDPOINTS = (f"{CLOSED}/api/interpreter",)
    # Any fault importing the GUI side must not stop a download suite.
    with contextlib.suppress(Exception):
        from osm_quick_3d import main_plugin

        main_plugin.OsmQuick3DPlugin.OSM_BASEMAP_URI = CLOSED_TILES_URI
    # Never read or write the user's real Overpass answer cache.
    isolate_download_cache()
    block_desktop()
    if not guard_qgis:
        return
    with _LOCK:
        if not _GUARD:
            _GUARD.append(QgsNetworkAccessManager.setRequestPreprocessor(_guard))


def isolate_download_cache() -> str:
    """Point the Overpass answer cache at a new empty folder; return its path.

    Without it a suite reads and writes the user's real cache in the system
    temp folder: a fake server's answer could later be served for a real
    area, and ``clear_cache()`` in a check would wipe the user's copies.
    0.51.1 has no CACHE_DIR, so there the system temp folder itself is
    redirected for this process.
    """
    import tempfile

    from osm_quick_3d import osm_download

    folder = tempfile.mkdtemp(prefix="osm_quick_3d-cache-")
    _CACHE_DIRS.append(folder)
    if hasattr(osm_download, "CACHE_DIR"):
        osm_download.CACHE_DIR = folder
    else:
        tempfile.tempdir = folder
    return folder


def cleanup() -> None:
    """Remove the cache folders this process made."""
    import shutil

    for folder in _CACHE_DIRS:
        shutil.rmtree(folder, ignore_errors=True)


def forget_servers() -> None:
    """Forget every Overpass server's rest and success, in memory and in
    QgsSettings, so one check's rests cannot decide the next one."""
    from osm_quick_3d import osm_download

    forget = getattr(osm_download, "forget_hosts", None)
    if forget is not None:
        forget()


def allow_loopback_http(module) -> bool:
    """Let a build without loopback support (0.51.1 accepts HTTPS only) talk
    to the plain-HTTP fake servers, so the suites can show it red.

    Returns True when the seam was needed.  The current build accepts
    http://127.0.0.1 itself and is never patched.
    """
    if hasattr(module, "_address_allowed"):
        return False
    from qgis.PyQt.QtCore import QUrl

    class _LoopbackAsHttps(QUrl):
        def scheme(self):  # noqa: D401 - Qt naming
            return "https" if self.host() in _LOCAL_HOSTS else super().scheme()

    module.QUrl = _LoopbackAsHttps
    return True


# ------------------------------------------------------------------ servers
MIB = 1024 * 1024
#: The oversize answer: twice the 64 MiB limit, so a cut there is
#: unmistakable even after loopback socket buffers swallowed a few MiB more.
OVERSIZE_TOTAL = 128 * MIB
FRESH = "2026-11-04T06:58:39Z"
REMARK = 'runtime error: Query timed out in "query" at line 3 after 41 seconds.'


def _overpass_page(text: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml">'
        "\n<head>\n  <title>OSM3S Response</title>\n</head>\n<body>\n"
        f'<p><strong style="color:#FF0000">Error</strong>: {text} </p>\n</body>\n</html>\n'
    ).encode("utf-8")


#: A tiny answer: one tree and one building near Izmir.
ELEMENTS = [
    {"type": "node", "id": 301, "lat": 38.4245, "lon": 27.1457, "tags": {"natural": "tree"}},
    {"type": "way", "id": 101, "tags": {"building": "yes", "building:levels": "4"},
     "geometry": [{"lat": 38.4240, "lon": 27.1450}, {"lat": 38.4240, "lon": 27.1452},
                  {"lat": 38.4242, "lon": 27.1452}, {"lat": 38.4242, "lon": 27.1450},
                  {"lat": 38.4240, "lon": 27.1450}]},
]
STATUS_PAGES = {
    "h429": (429, "Too Many Requests", _overpass_page(
        "runtime error: open64: 0 Success /osm3s_osm_base Dispatcher_Client::"
        "request_read_and_idx::rate_limited. Please check /api/status for the quota of your IP address."),
        "text/html"),
    "h406": (406, "Not Acceptable", b"<html><body>Not Acceptable</body></html>", "text/html"),
    "h403": (403, "Forbidden", b"<html><body>Forbidden</body></html>", "text/html"),
    "h400": (400, "Bad Request", _overpass_page("line 2: parse error: Unknown type &quot;&quot;"),
             "text/html"),
    "h502": (502, "Bad Gateway", b"# 502 Gateway Timeout\n\nAll query workers are busy.\n", "text/plain"),
    "h504": (504, "Gateway Timeout", _overpass_page(
        "runtime error: open64: 0 Success /osm3s_osm_base Dispatcher_Client::"
        "request_read_and_idx::timeout. The server is probably too busy to handle your request."),
        "text/html"),
    "h404": (404, "Not Found", b"<html><head><title>404 Not Found</title></head></html>", "text/html"),
}


def overpass_doc(elements, remark: Optional[str] = None) -> bytes:
    """Output_JSON's byte layout: header, elements, optional remark."""
    text = (
        '{\n  "version": 0.6,\n  "generator": "Overpass API 0.7.62 fake",\n'
        f'  "osm3s": {{\n    "timestamp_osm_base": "{FRESH}",\n'
        '    "copyright": "The data included in this document is from www.openstreetmap.org. '
        'The data is made available under ODbL."\n  },\n'
        '  "elements": [\n\n' + ",\n".join(json.dumps(item) for item in elements) + "\n\n  ]"
    )
    if remark:
        text += ',\n"remark": ' + json.dumps(remark)
    return (text + "\n}\n").encode("utf-8")


class FakeOverpass:
    """Overpass servers on 127.0.0.1, each on its own port.

    `set(name, *modes)` scripts the answers: the n-th request gets the n-th
    mode and the last one repeats.  Modes: ok (``self.elements``), empty,
    remark, hang, drop (read the request, close without answering),
    oversize (128 MiB, paced) and the status pages h429, h406, h403, h400,
    h502, h504, h404.
    """

    def __init__(self, names=("a", "b"), elements=None) -> None:
        self.lock = threading.Lock()
        self.hits: List[Dict] = []
        self.modes: Dict[str, Dict] = {}
        self.ports: Dict[str, int] = {}
        self.elements = list(ELEMENTS if elements is None else elements)
        for name in names:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listener.listen(64)
            self.ports[name] = listener.getsockname()[1]
            threading.Thread(target=self._serve, args=(name, listener), daemon=True).start()
            self.set(name, "ok")

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.ports[name]}/api/interpreter"

    def host(self, name: str) -> str:
        return f"127.0.0.1:{self.ports[name]}"

    def set(self, name: str, *modes: str) -> None:
        with self.lock:
            self.modes[name] = {"seq": list(modes) or ["ok"], "seen": 0}

    def take_hits(self) -> List[Dict]:
        with self.lock:
            hits, self.hits = self.hits, []
        return hits

    def _serve(self, name, listener) -> None:
        while True:
            connection, _address = listener.accept()
            threading.Thread(target=self._handle, args=(name, connection), daemon=True).start()

    @staticmethod
    def _read(connection):
        data = b""
        connection.settimeout(30)
        while b"\r\n\r\n" not in data:
            chunk = connection.recv(65536)
            if not chunk:
                return None
            data += chunk
        head, _sep, body = data.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        headers = {}
        for line in lines[1:]:
            key, _colon, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
        need = int(headers.get("content-length", "0") or 0)
        while len(body) < need:
            chunk = connection.recv(65536)
            if not chunk:
                break
            body += chunk
        method, path = (lines[0].split(" ") + ["", ""])[:2]
        return method, path, headers, body

    @staticmethod
    def _send(connection, status, reason, body, ctype="application/json") -> None:
        head = [f"HTTP/1.1 {status} {reason}", f"Content-Type: {ctype}",
                f"Content-Length: {len(body)}", "Connection: close"]
        connection.sendall(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body)

    @staticmethod
    def _hold(connection, limit=120.0) -> None:
        start = time.monotonic()
        connection.settimeout(0.2)
        while time.monotonic() - start < limit:
            try:
                if connection.recv(4096) == b"":
                    return
            except socket.timeout:
                continue
            except OSError:
                return

    def _handle(self, name, connection) -> None:
        with contextlib.closing(connection):
            try:
                request = self._read(connection)
                if request is None:
                    return
                method, path, headers, body = request
                query = urllib.parse.parse_qs(body.decode("latin-1")).get("data", [""])[0]
                with self.lock:
                    state = self.modes[name]
                    mode = state["seq"][min(state["seen"], len(state["seq"]) - 1)]
                    state["seen"] += 1
                    record = {"host": name, "method": method, "path": path, "query": query,
                              "referer": headers.get("referer", ""), "ua": headers.get("user-agent", ""),
                              "accept": headers.get("accept", ""), "t": time.monotonic(), "mode": mode}
                    self.hits.append(record)
                    elements = list(self.elements)
                self._answer(connection, mode, record, elements)
            except OSError:
                return

    def _answer(self, connection, mode, record, elements) -> None:
        if mode == "ok":
            self._send(connection, 200, "OK", overpass_doc(elements))
        elif mode == "empty":
            self._send(connection, 200, "OK", overpass_doc([]))
        elif mode == "remark":
            self._send(connection, 200, "OK", overpass_doc(elements[:1], remark=REMARK))
        elif mode == "hang":
            self._hold(connection)
        elif mode == "drop":
            return
        elif mode == "oversize":
            connection.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(OVERSIZE_TOTAL).encode() + b"\r\nConnection: close\r\n\r\n")
            chunk, sent = b" " * MIB, 0
            with contextlib.suppress(OSError):
                while sent < OVERSIZE_TOTAL:
                    connection.sendall(chunk)
                    sent += len(chunk)
                    with self.lock:
                        record["sent"] = sent
                    time.sleep(0.004)
        elif mode in STATUS_PAGES:
            status, reason, body, ctype = STATUS_PAGES[mode]
            self._send(connection, status, reason, body, ctype)
        else:
            self._send(connection, 500, "Fake", b"unknown mode", "text/plain")


def host_of(url: str) -> str:
    return urlsplit(url).netloc
