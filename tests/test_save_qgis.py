"""Saving OSM Quick 3D results: GeoPackages keep their tables, temporary layers say so.

Run under a QGIS Python, from the directory that contains the plugin folder,
with QGIS_CUSTOM_CONFIG_PATH set to a throwaway directory (TRAPS 4.6):

    python-qgis-ltr.bat -m osm_quick_3d.tests.test_save_qgis   # QGIS 3.44
    python-qgis.bat     -m osm_quick_3d.tests.test_save_qgis   # QGIS 4.2

Offscreen, fake Overpass server on 127.0.0.1, every public host guarded
(the rig and guard of tests/test_dock_qgis.py).

Covers item 4 of DEVELOPMENT_SET_v0.52 (P9, P10). Each check's docstring
names what 0.51.1 did instead; every check fails on 0.51.1 for that reason.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3
import stat
import tempfile

from osm_quick_3d.tests.test_dock_qgis import (
    AFTER_RUN,
    ERR_COLOR,
    _enum,
    check,
    fail_if,
    is_idle,
    main,
    pump,
    wait_for,
)

from qgis.core import (  # noqa: E402
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

WORK = tempfile.mkdtemp(prefix="oq3d-save-")
AFTER_RUN.append(lambda: shutil.rmtree(WORK, ignore_errors=True))
USER_TABLE = "precious_survey"


def make_user_gpkg(name):
    """A GeoPackage that already holds the user's own survey table (3 points)."""
    path = os.path.join(WORK, name)
    layer = QgsVectorLayer("Point?crs=EPSG:4326&field=note:string(40)", USER_TABLE, "memory")
    feats = []
    for i in range(3):
        feat = QgsFeature(layer.fields())
        feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(27.14 + i * 0.001, 38.43)))
        feat.setAttribute("note", f"field visit {i + 1}")
        feats.append(feat)
    layer.dataProvider().addFeatures(feats)
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.layerName = USER_TABLE
    result = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, path, QgsProject.instance().transformContext(), options)
    check(result[0] == 0, f"could not prepare the user's GeoPackage: {result[1]}")
    return path


def user_rows(path) -> int:
    layer = QgsVectorLayer(f"{path}|layername={USER_TABLE}", "check", "ogr")
    return layer.featureCount() if layer.isValid() else -1


def tables(path):
    con = sqlite3.connect(path)
    try:
        return [r[0] for r in con.execute("SELECT table_name FROM gpkg_contents ORDER BY table_name")]
    finally:
        con.close()


@contextlib.contextmanager
def save_dialog_returns(path):
    from qgis.PyQt.QtWidgets import QFileDialog

    original = QFileDialog.__dict__.get("getSaveFileName")
    QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (path, "GeoPackage (*.gpkg)"))
    try:
        yield
    finally:
        QFileDialog.getSaveFileName = original


def run_saving(rig, path, dock=None, plugin=None):
    if dock is None:
        plugin, dock = rig.open_dock()
    rig.set_view()
    rig.overpass("ok")
    dock.cb_save_gpkg.setChecked(path is not None)
    if path is None:
        rig.run(dock)
    else:
        with save_dialog_returns(path):
            rig.run(dock)
    check(wait_for(lambda: is_idle(plugin, dock), 30), "the run did not end within 30 s")
    pump(0.2)
    return plugin, dock


def saved_layers(path):
    norm = os.path.normcase(os.path.abspath(path))
    out = []
    for layer in QgsProject.instance().mapLayers().values():
        source = layer.source().split("|")[0]
        if layer.providerType() == "ogr" and os.path.normcase(os.path.abspath(source)) == norm:
            out.append(layer)
    return out


def memory_osm_layers(rig):
    return [lyr for lyr in rig.osm_layers() if lyr.providerType() == "memory"]


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def check_user_table_survives(rig):
    """P9: saving into a GeoPackage that holds the user's table keeps that table.

    0.51.1 wrote its first layer with CreateOrOverwriteFile: the chosen file
    was recreated and the user's own survey table was gone.
    """
    path = make_user_gpkg("survey.gpkg")
    plugin, dock = run_saving(rig, path)
    problems = []
    rows = user_rows(path)
    if rows != 3:
        problems.append(f"the user's table has {rows} rows after the run (3 before); tables {tables(path)}")
    layers = saved_layers(path)
    if not layers:
        problems.append(f"no layer reads from {os.path.basename(path)}: {dock.status.text()!r}")
    if memory_osm_layers(rig):
        problems.append(f"temporary layers instead of saved ones: {[lyr.name() for lyr in memory_osm_layers(rig)]}")
    if rig.boxes.shown:
        problems.append(f"a box appeared: {rig.boxes.shown[0]!r}")
    fail_if(problems)
    return f"tables now {tables(path)}; {len(layers)} layers read from the file"


def check_rerun_into_the_same_file_while_loaded(rig):
    """P9: a second run into the same file works while the first run's layers are loaded.

    0.51.1 failed with "A file system object called … already exists" and,
    without saying so, added the second run as temporary memory layers.
    """
    path = make_user_gpkg("rerun.gpkg")
    plugin, dock = run_saving(rig, path)
    first = {lyr.id(): lyr.featureCount() for lyr in saved_layers(path)}
    check(first, f"the first run saved nothing: {dock.status.text()!r}")
    run_saving(rig, path, dock=dock, plugin=plugin)
    problems = []
    now = saved_layers(path)
    new = [lyr for lyr in now if lyr.id() not in first]
    if not new:
        problems.append(f"the second run added no saved layer: {dock.status.text()!r} / {rig.summary(dock)!r}")
    for layer_id, count in first.items():
        layer = QgsProject.instance().mapLayer(layer_id)
        if layer is None or not layer.isValid() or layer.featureCount() != count:
            problems.append(f"a first-run layer changed ({count} features before)")
            break
    if memory_osm_layers(rig):
        problems.append(f"temporary layers appeared: {[lyr.name() for lyr in memory_osm_layers(rig)]}")
    if user_rows(path) != 3:
        problems.append("the user's table did not survive")
    kind = dock.status_kind() if hasattr(dock, "status_kind") else "unknown"
    if kind != "ok":
        problems.append(f"the run did not end cleanly ({kind}): {dock.status.text()!r}")
    fail_if(problems)
    return f"{len(first)} + {len(new)} layers in {tables(path)}"


def check_not_a_geopackage_is_refused(rig):
    """P9: a file that is not a GeoPackage is left alone, and the dock says why.

    0.51.1 overwrote whatever file was chosen.
    """
    path = os.path.join(WORK, "notes.gpkg")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("my notes, not a database\n")
    before = open(path, "rb").read()
    plugin, dock = run_saving(rig, path)
    problems = []
    if open(path, "rb").read() != before:
        problems.append("the file was changed")
    if ERR_COLOR not in dock.status.styleSheet() or "GeoPackage" not in dock.status.text():
        problems.append(f"the dock does not say the file is no GeoPackage: {dock.status.text()!r}")
    if rig.boxes.shown:
        problems.append(f"a box appeared: {rig.boxes.shown[0]!r}")
    fail_if(problems)
    return f"file untouched; status {dock.status.text()!r}"


@contextlib.contextmanager
def writer_fails(message):
    """Every GeoPackage write the plugin attempts fails with ``message``."""
    original = QgsVectorFileWriter.__dict__.get("writeAsVectorFormatV3")
    code = _enum(QgsVectorFileWriter, "WriterError", "ErrCreateDataSource")
    QgsVectorFileWriter.writeAsVectorFormatV3 = staticmethod(lambda *a, **k: (code, message, "", ""))
    try:
        yield
    finally:
        QgsVectorFileWriter.writeAsVectorFormatV3 = original


def check_write_failure_is_reported(rig):
    """P9: when writing fails the dock says so plainly; nothing falls back silently.

    0.51.1 put a success summary in the dock and only a passing message-bar
    note about memory layers. (A read-only file is now refused before the
    download, so the failure is made at the writer.)
    """
    path = make_user_gpkg("writefails.gpkg")
    with writer_fails("disk full (test)"):
        plugin, dock = run_saving(rig, path)
    shown = f"{dock.status.text()} {rig.summary(dock)}".lower()
    problems = []
    if not any(word in shown for word in ("could not save", "saving to the geopackage failed", "not saved")):
        problems.append(f"the dock does not say saving failed: {shown[:200]!r}")
    if memory_osm_layers(rig) and "temporary" not in shown:
        problems.append("temporary layers were added without saying they are temporary")
    if rig.boxes.shown:
        problems.append(f"a box appeared: {rig.boxes.shown[0]!r}")
    if user_rows(path) != 3:
        problems.append("the user's table did not survive")
    fail_if(problems)
    return f"status {dock.status.text()!r}"


def check_unwritable_target_refused_before_download(rig):
    """A read-only GeoPackage or a folder that takes no new files is refused
    plainly before anything is downloaded.

    The first 0.52 build downloaded everything first and only then failed to
    save (and offered temporary layers instead).
    """
    from osm_quick_3d.main_plugin import GpkgTarget

    problems = []
    if not GpkgTarget.folder_writable(WORK):
        problems.append(f"the writable test folder {WORK} reads as not writable")
    share = GpkgTarget.sqlite_uri(r"\\host\share\x.gpkg")
    if share != "file:////host/share/x.gpkg?mode=ro":
        problems.append(f"a network-share path loses its host: {share!r}")
    path = make_user_gpkg("readonly.gpkg")
    before = open(path, "rb").read()
    os.chmod(path, stat.S_IREAD)
    try:
        plugin, dock = run_saving(rig, path)
    finally:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    status = dock.status.text()
    if rig.overpass_fake.requests:
        problems.append(f"{len(rig.overpass_fake.requests)} request(s) sent for a read-only file")
    if "read-only" not in status or ERR_COLOR not in dock.status.styleSheet():
        problems.append(f"the dock does not say the file is read-only: {status!r}")
    if open(path, "rb").read() != before:
        problems.append("the read-only file was changed")
    # A folder that refuses new files (an ACL or a read-only share; Windows
    # ignores the read-only flag on folders, so the probe is stood in for).
    real_probe = GpkgTarget.__dict__["folder_writable"]
    GpkgTarget.folder_writable = staticmethod(lambda folder: False)
    try:
        plugin, dock = run_saving(rig, os.path.join(WORK, "new_in_locked_folder.gpkg"))
    finally:
        GpkgTarget.folder_writable = real_probe
    folder_status = dock.status.text()
    if rig.overpass_fake.requests:
        problems.append(f"{len(rig.overpass_fake.requests)} request(s) sent for an unwritable folder")
    if "folder cannot be written" not in folder_status:
        problems.append(f"the dock does not say the folder cannot be written to: {folder_status!r}")
    if rig.boxes.shown:
        problems.append(f"a box appeared: {rig.boxes.shown[0]!r}")
    fail_if(problems)
    return f"no request; {status!r} / {folder_status!r}"


def check_locked_geopackage_is_not_called_foreign(rig):
    """A GeoPackage another program holds locked is not called "not a GeoPackage".

    The first 0.52 build read any SQLite error as "not a GeoPackage".
    """
    path = make_user_gpkg("locked.gpkg")
    holder = sqlite3.connect(path, isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        plugin, dock = run_saving(rig, path)
    finally:
        holder.close()
    status = dock.status.text()
    problems = []
    if rig.overpass_fake.requests:
        problems.append(f"{len(rig.overpass_fake.requests)} request(s) sent for a locked file")
    if "another program" not in status:
        problems.append(f"the dock does not say the file may be open elsewhere: {status!r}")
    if "not a GeoPackage" in status:
        problems.append("a locked GeoPackage is called not a GeoPackage")
    if user_rows(path) != 3:
        problems.append("the user's table did not survive")
    fail_if(problems)
    return f"status {status!r}"


def check_temporary_layers_say_so(rig):
    """P10: without a GeoPackage the dock says the layers vanish on reopening.

    0.51.1 said nothing; the memory layers came back empty after reopening.
    """
    plugin, dock = run_saving(rig, None)
    shown = f"{dock.status.text()} {rig.summary(dock)}".lower()
    problems = []
    if not memory_osm_layers(rig):
        problems.append("no temporary layers were added")
    if "temporary" not in shown or "reopen" not in shown:
        problems.append(f"no notice that the layers vanish when the project is reopened: {shown[:200]!r}")
    fail_if(problems)
    return "notice shown with the summary"


CHECKS = (
    check_user_table_survives,
    check_rerun_into_the_same_file_while_loaded,
    check_not_a_geopackage_is_refused,
    check_write_failure_is_reported,
    check_unwritable_target_refused_before_download,
    check_locked_geopackage_is_not_called_foreign,
    check_temporary_layers_say_so,
)


if __name__ == "__main__":
    main("saving to GeoPackage", CHECKS)
